"""Shared fixtures. Every test gets its own throwaway SQLite DB under tmp_path;
nothing touches data/finance.db, the network, or the repo's .env."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import init_db  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path to an empty DB with the current schema."""
    path = tmp_path / "finance.db"
    conn = init_db.connect(path)
    init_db.create_schema(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_db_path(db_path: Path) -> Path:
    """DB with the sample data from docs/finance_db_schema.md."""
    conn = init_db.connect(db_path)
    init_db.seed(conn)
    conn.close()
    return db_path


@pytest.fixture
def conn(db_path):
    c = init_db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def seeded_conn(seeded_db_path):
    c = init_db.connect(seeded_db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Keep the repo's real .env (Dropbox/Anthropic keys, PDF password) out of
    tests: load_env becomes a no-op and the password env var is cleared."""
    import dropbox_sync
    monkeypatch.setattr(dropbox_sync, "load_env", lambda *a, **k: None)
    monkeypatch.delenv("PFA_PDF_PASSWORD", raising=False)
