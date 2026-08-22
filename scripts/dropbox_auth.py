#!/usr/bin/env python3
"""One-time helper: obtain a long-lived Dropbox refresh token.

Access tokens from the App Console are short-lived (~4h). A refresh token does
not expire and lets dropbox_sync.py mint fresh access tokens automatically.

Prereqs in .env (already present if you created a Dropbox app):
    DROPBOX_APP_KEY=...
    DROPBOX_APP_SECRET=...

Run it and follow the prompts:
    .venv/bin/python scripts/dropbox_auth.py

It opens an authorization URL (you sign in / click Allow in your browser),
you paste back the short code it shows, and this writes DROPBOX_REFRESH_TOKEN
into .env for you.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dropbox_sync import ENV_PATH, load_env  # noqa: E402


def upsert_env(path: Path, key: str, value: str) -> None:
    """Add or replace KEY=value in the .env file, preserving other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    load_env()
    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    if not (app_key and app_secret):
        raise SystemExit("DROPBOX_APP_KEY and DROPBOX_APP_SECRET must be set in "
                         ".env first (from your Dropbox app console).")

    try:
        from dropbox import DropboxOAuth2FlowNoRedirect
    except ImportError as e:
        raise SystemExit("dropbox SDK not installed. Run: pip install dropbox") from e

    # token_access_type='offline' is what makes Dropbox return a refresh token.
    flow = DropboxOAuth2FlowNoRedirect(app_key, app_secret,
                                       token_access_type="offline")
    url = flow.start()
    print("\n1. Open this URL in your browser and click 'Allow':\n")
    print(f"   {url}\n")
    print("2. Copy the authorization code Dropbox shows you.\n")
    try:
        code = input("3. Paste the code here: ").strip()
    except EOFError:
        raise SystemExit("\nNo input received. Run this in an interactive terminal.")

    if not code:
        raise SystemExit("No code entered; aborting.")

    try:
        result = flow.finish(code)
    except Exception as e:  # dropbox raises various auth errors here
        raise SystemExit(f"Authorization failed: {e}")

    refresh = result.refresh_token
    if not refresh:
        raise SystemExit("No refresh token returned. Ensure the app uses "
                         "token_access_type=offline and try again.")

    upsert_env(ENV_PATH, "DROPBOX_REFRESH_TOKEN", refresh)
    print(f"\n✓ Wrote DROPBOX_REFRESH_TOKEN to {ENV_PATH}")

    # Verify it works right away.
    import dropbox
    dbx = dropbox.Dropbox(oauth2_refresh_token=refresh,
                          app_key=app_key, app_secret=app_secret)
    acct = dbx.users_get_current_account()
    print(f"✓ Verified: authenticated as {acct.name.display_name} ({acct.email}).")
    print("\nYou can now remove DROPBOX_ACCESS_TOKEN from .env — dropbox_sync.py "
          "prefers the refresh token automatically.")


if __name__ == "__main__":
    main()
