#!/usr/bin/env python3
"""One ingestion entry point — auto-routes a file or folder by type.

Instead of remembering which script handles which format, run:

    python scripts/ingest.py data/august.csv
    python scripts/ingest.py data/receipts/            # folder of images
    python scripts/ingest.py data/statements/bca.pdf --type cc --account "BCA Checking"
    python scripts/ingest.py data/inbox/               # mixed: routes each file

Dispatch by extension:
    .csv .xlsx .xls        -> sheet ingester (pandas)
    .jpg .png .webp ...     -> receipt ingester (OCR, one total each)
    .pdf                    -> statement ingester (text/OCR, many rows)

All formats share one dedup + insert core, so duplicates are skipped uniformly.
Format-specific flags (--ai/--type/--force for PDFs, --engine for images) are
passed through when relevant.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import DEFAULT_DB                       # noqa: E402
import ingest_sheet, ingest_receipt, ingest_pdf       # noqa: E402
from ingest_receipt import IMAGE_EXTS                 # noqa: E402
from ocr_engines import DEFAULT_ENGINE                # noqa: E402

SHEET_EXTS = {".csv", ".xlsx", ".xls"}


def classify(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in SHEET_EXTS:
        return "sheet"
    if ext in IMAGE_EXTS:
        return "receipt"
    if ext == ".pdf":
        return "pdf"
    return None


def collect(target: Path) -> dict[str, list[Path]]:
    files = ([target] if target.is_file()
             else sorted(p for p in target.iterdir() if p.is_file()))
    groups: dict[str, list[Path]] = {"sheet": [], "receipt": [], "pdf": []}
    skipped = []
    for p in files:
        kind = classify(p)
        (groups[kind].append(p) if kind else skipped.append(p.name))
    if skipped:
        print(f"Ignoring {len(skipped)} unsupported file(s): "
              f"{', '.join(skipped[:6])}{' ...' if len(skipped) > 6 else ''}")
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified ingestion entry point.")
    parser.add_argument("target", type=Path, help="File or folder to ingest")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--account", help="Account name to assign")
    parser.add_argument("--dry-run", action="store_true")
    # receipt
    parser.add_argument("--engine", default=DEFAULT_ENGINE, help="OCR engine for images")
    # pdf
    parser.add_argument("--type", dest="source_type",
                        choices=ingest_pdf.SOURCE_TYPES, help="Statement type (PDFs)")
    parser.add_argument("--ai", action="store_true", help="AI-structure PDF statements")
    parser.add_argument("--model", default=ingest_pdf.AI_MODEL_DEFAULT)
    parser.add_argument("--force", action="store_true", help="Re-ingest processed PDFs")
    parser.add_argument("--no-redact", dest="redact", action="store_false",
                        help="Do NOT redact PII before an --ai send (not recommended)")
    parser.add_argument("--password", help="Password for locked PDFs "
                        "(or set PFA_PDF_PASSWORD in .env, or be prompted)")
    args = parser.parse_args()

    if not args.target.exists():
        raise SystemExit(f"Path not found: {args.target}")

    groups = collect(args.target)
    total = 0

    for sheet in groups["sheet"]:
        print(f"\n=== sheet: {sheet.name} ===")
        total += ingest_sheet.ingest(sheet, args.db, args.account, None, args.dry_run) or 0

    if groups["receipt"]:
        print(f"\n=== receipts: {len(groups['receipt'])} image(s) ===")
        total += ingest_receipt.ingest(groups["receipt"], args.db, args.engine,
                                       args.account, args.dry_run) or 0

    if groups["pdf"]:
        print(f"\n=== statements: {len(groups['pdf'])} PDF(s) ===")
        total += ingest_pdf.ingest(groups["pdf"], args.db, args.account,
                                   args.source_type, args.ai, args.model,
                                   args.dry_run, args.force, args.redact,
                                   args.password) or 0

    if not any(groups.values()):
        raise SystemExit("No supported files found to ingest.")
    print(f"\nDone. {total} transaction(s) inserted"
          + (" (dry-run: 0 written)." if args.dry_run else "."))


if __name__ == "__main__":
    main()
