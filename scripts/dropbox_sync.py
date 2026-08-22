#!/usr/bin/env python3
"""Sync receipts and sheets from a Dropbox folder into the local pipeline.

Files are downloaded to a temporary directory, ingested via the existing
ingest_receipt / ingest_sheet logic, and the temp copies are deleted
immediately. Receipt images are never retained locally (matches the
no-image-retention rule in docs/PLANNING.md); the Dropbox originals remain
your source of truth. Only derived transaction data is written to the local DB.

Auth — put credentials in a git-ignored .env file (see .env.example):
    # Recommended: long-lived refresh-token app auth
    DROPBOX_APP_KEY=...
    DROPBOX_APP_SECRET=...
    DROPBOX_REFRESH_TOKEN=...
    # OR a single (possibly short-lived) access token
    DROPBOX_ACCESS_TOKEN=...

Usage:
    python scripts/dropbox_sync.py list          --folder /Finance/Receipts
    python scripts/dropbox_sync.py pull-receipts --folder /Finance/Receipts --dry-run
    python scripts/dropbox_sync.py pull-receipts --folder /Finance/Receipts --account Cash
    python scripts/dropbox_sync.py pull-sheets   --folder /Finance/Sheets --account "BCA Checking"

    # Optionally move processed originals aside in Dropbox so they aren't re-ingested:
    python scripts/dropbox_sync.py pull-receipts --folder /Finance/Receipts \
        --archive /Finance/Processed
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB                       # noqa: E402
from ingest_receipt import IMAGE_EXTS               # noqa: E402
from ingest_receipt import ingest as ingest_receipts  # noqa: E402
from ingest_sheet import ingest as ingest_sheet      # noqa: E402
from ingest_pdf import ingest as ingest_pdf_fn        # noqa: E402
from ingest_pdf import AI_MODEL_DEFAULT, SOURCE_TYPES  # noqa: E402
from ingest import classify                           # noqa: E402
from ocr_engines import DEFAULT_ENGINE               # noqa: E402

SHEET_EXTS = {".csv", ".xlsx", ".xls"}
PDF_EXTS = {".pdf"}
ALL_EXTS = SHEET_EXTS | IMAGE_EXTS | PDF_EXTS
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Minimal .env loader (KEY=VALUE lines); does not overwrite real env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def get_client():
    """Build a Dropbox client from env credentials (refresh token preferred)."""
    try:
        import dropbox
    except ImportError as e:
        raise SystemExit("dropbox SDK not installed. Run: pip install dropbox") from e

    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh = os.getenv("DROPBOX_REFRESH_TOKEN")
    access = os.getenv("DROPBOX_ACCESS_TOKEN")

    if refresh and app_key and app_secret:
        dbx = dropbox.Dropbox(oauth2_refresh_token=refresh,
                              app_key=app_key, app_secret=app_secret)
    elif access:
        dbx = dropbox.Dropbox(access)
    else:
        raise SystemExit(
            "No Dropbox credentials found. Set DROPBOX_REFRESH_TOKEN + "
            "DROPBOX_APP_KEY + DROPBOX_APP_SECRET (recommended), or "
            "DROPBOX_ACCESS_TOKEN, in .env (see .env.example)."
        )

    from dropbox.exceptions import AuthError
    try:
        acct = dbx.users_get_current_account()
        print(f"Connected to Dropbox as {acct.name.display_name} "
              f"({acct.email}).")
    except AuthError as e:
        raise SystemExit(f"Dropbox authentication failed: {e}") from e
    return dbx


def list_files(dbx, folder: str, exts: set[str] | None = None) -> list:
    """List file entries directly under `folder`, optionally filtered by ext."""
    from dropbox.exceptions import ApiError
    from dropbox.files import FileMetadata

    folder = "" if folder in ("/", "") else folder.rstrip("/")
    try:
        res = dbx.files_list_folder(folder)
    except ApiError as e:
        raise SystemExit(f"Could not list {folder!r}: {e}") from e

    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)

    files = [e for e in entries if isinstance(e, FileMetadata)]
    if exts is not None:
        files = [e for e in files if Path(e.name).suffix.lower() in exts]
    return sorted(files, key=lambda e: e.name)


def _archive(dbx, entries, downloaded_names: set[str], archive: str) -> None:
    """Move successfully-processed originals to an archive folder in Dropbox."""
    from dropbox.exceptions import ApiError
    from dropbox.files import CreateFolderError

    archive = archive.rstrip("/")
    try:
        dbx.files_create_folder_v2(archive)
    except ApiError as e:
        # Ignore "already exists"; re-raise anything else.
        if not (e.error.is_path() and isinstance(e.error.get_path(),
                                                  CreateFolderError)):
            pass  # best-effort; fall through to moves
    moved = 0
    for e in entries:
        if e.name not in downloaded_names:
            continue
        try:
            dbx.files_move_v2(e.path_display, f"{archive}/{e.name}", autorename=True)
            moved += 1
        except ApiError as err:
            print(f"  ! could not archive {e.name}: {err}")
    print(f"Archived {moved} file(s) to {archive} in Dropbox.")


def _download_all(dbx, entries, tmp: Path) -> list[Path]:
    paths = []
    for e in entries:
        local = tmp / e.name
        dbx.files_download_to_file(str(local), e.path_display)
        paths.append(local)
    return paths


def cmd_list(dbx, folder: str) -> None:
    files = list_files(dbx, folder)
    if not files:
        print(f"No files in {folder!r}.")
        return
    print(f"{len(files)} file(s) in {folder!r}:")
    kinds = {"sheet": "sheet", "receipt": "img", "pdf": "pdf"}
    for e in files:
        kind = kinds.get(classify(Path(e.name)), "other")
        print(f"  [{kind:5}] {e.name}  ({e.size:,} bytes)")


def cmd_pull_receipts(dbx, folder: str, db: Path, engine: str,
                      account: str | None, archive: str | None,
                      dry_run: bool) -> None:
    entries = list_files(dbx, folder, IMAGE_EXTS)
    if not entries:
        print(f"No receipt images in {folder!r}.")
        return
    print(f"Found {len(entries)} receipt image(s) in Dropbox {folder!r}.")
    with tempfile.TemporaryDirectory(prefix="pfa_receipts_") as tmp:
        paths = _download_all(dbx, entries, Path(tmp))
        inserted = ingest_receipts(paths, db, engine, account, dry_run)
    # temp dir (and downloaded images) deleted here.
    if archive and not dry_run and inserted:
        _archive(dbx, entries, {p.name for p in paths}, archive)


def cmd_pull_sheets(dbx, folder: str, db: Path, account: str | None,
                    archive: str | None, dry_run: bool) -> None:
    entries = list_files(dbx, folder, SHEET_EXTS)
    if not entries:
        print(f"No sheets in {folder!r}.")
        return
    print(f"Found {len(entries)} sheet(s) in Dropbox {folder!r}.")
    processed: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="pfa_sheets_") as tmp:
        for e in entries:
            local = Path(tmp) / e.name
            dbx.files_download_to_file(str(local), e.path_display)
            print(f"\n--- {e.name} ---")
            try:
                ingest_sheet(local, db, account, None, dry_run)
                processed.add(e.name)
            except SystemExit as err:
                print(f"  ! skipped {e.name}: {err}")
    if archive and not dry_run and processed:
        _archive(dbx, entries, processed, archive)


def cmd_pull(dbx, folder: str, db: Path, account: str | None,
             source_type: str | None, use_ai: bool, model: str, engine: str,
             archive: str | None, dry_run: bool) -> None:
    """Download every supported file (sheets, images, PDFs) and route each."""
    entries = list_files(dbx, folder, ALL_EXTS)
    if not entries:
        print(f"No supported files in {folder!r}.")
        return
    print(f"Found {len(entries)} file(s) in Dropbox {folder!r}.")
    processed: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="pfa_pull_") as tmp:
        paths = _download_all(dbx, entries, Path(tmp))
        groups: dict[str, list[Path]] = {"sheet": [], "receipt": [], "pdf": []}
        for p in paths:
            kind = classify(p)
            if kind:
                groups[kind].append(p)
        for s in groups["sheet"]:
            print(f"\n--- sheet: {s.name} ---")
            try:
                ingest_sheet(s, db, account, None, dry_run)
                processed.add(s.name)
            except SystemExit as err:
                print(f"  ! skipped {s.name}: {err}")
        if groups["receipt"]:
            print(f"\n--- receipts: {len(groups['receipt'])} image(s) ---")
            ingest_receipts(groups["receipt"], db, engine, account, dry_run)
            processed.update(p.name for p in groups["receipt"])
        if groups["pdf"]:
            print(f"\n--- statements: {len(groups['pdf'])} PDF(s) ---")
            ingest_pdf_fn(groups["pdf"], db, account, source_type, use_ai,
                          model, dry_run, False)
            processed.update(p.name for p in groups["pdf"])
    if archive and not dry_run and processed:
        _archive(dbx, entries, processed, archive)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync a Dropbox folder into the pipeline.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite DB path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--folder", required=True, help="Dropbox folder path, e.g. /Finance/Receipts")
        p.add_argument("--archive", help="Dropbox folder to move processed originals into")
        p.add_argument("--dry-run", action="store_true", help="Preview without writing / archiving")

    p_list = sub.add_parser("list", help="List files in a Dropbox folder")
    p_list.add_argument("--folder", required=True)

    p_r = sub.add_parser("pull-receipts", help="Ingest receipt images from Dropbox")
    add_common(p_r)
    p_r.add_argument("--engine", default=DEFAULT_ENGINE, help=f"OCR engine (default: {DEFAULT_ENGINE})")
    p_r.add_argument("--account", help="Account name to assign to these receipts")

    p_s = sub.add_parser("pull-sheets", help="Ingest CSV/XLSX sheets from Dropbox")
    add_common(p_s)
    p_s.add_argument("--account", help="Default account name for rows with no account column")

    p_p = sub.add_parser("pull", help="Ingest ALL supported files (sheets/images/PDFs)")
    add_common(p_p)
    p_p.add_argument("--account", help="Account name to assign")
    p_p.add_argument("--engine", default=DEFAULT_ENGINE, help="OCR engine for images")
    p_p.add_argument("--type", dest="source_type", choices=SOURCE_TYPES,
                     help="Statement type for PDFs (cc/ewallet/bank/other)")
    p_p.add_argument("--ai", action="store_true", help="AI-structure PDF statements")
    p_p.add_argument("--model", default=AI_MODEL_DEFAULT)

    args = parser.parse_args()
    load_env()
    dbx = get_client()

    if args.command == "list":
        cmd_list(dbx, args.folder)
    elif args.command == "pull-receipts":
        cmd_pull_receipts(dbx, args.folder, args.db, args.engine,
                          args.account, args.archive, args.dry_run)
    elif args.command == "pull-sheets":
        cmd_pull_sheets(dbx, args.folder, args.db, args.account,
                        args.archive, args.dry_run)
    elif args.command == "pull":
        cmd_pull(dbx, args.folder, args.db, args.account, args.source_type,
                 args.ai, args.model, args.engine, args.archive, args.dry_run)


if __name__ == "__main__":
    main()
