import json

import pytest

import export_analysis
import generate_snapshot as gs
import reconcile
from ingest_core import commit_transactions


def _t(date, amount, source, merchant):
    return (date, amount, "expense", None, merchant, 1, None, source, None)


# ---- reconcile -----------------------------------------------------------

@pytest.fixture
def pair_conn(conn):
    conn.execute("INSERT INTO accounts (id, name, type) VALUES (1, 'A', 'bank')")
    commit_transactions(conn, [
        _t("2026-08-01", 150_000, "receipt", "Indomaret Kemang"),
        _t("2026-08-03", 150_000, "statement", "IDM T123 JAKARTA"),   # 2 days later
        _t("2026-08-10", 150_000, "statement", "Far away"),           # 9 days
        _t("2026-08-01", 99_000, "sheet", "Lunch"),
        _t("2026-08-01", 99_000, "sheet", "Lunch again"),             # same source
    ])
    return conn


def _ids(pairs):
    return {(p[0], p[5]) for p in pairs}


def test_find_pairs_within_window(pair_conn):
    assert _ids(reconcile.find_pairs(pair_conn, 3, False)) == {(1, 2), (4, 5)}


def test_find_pairs_cross_only(pair_conn):
    assert _ids(reconcile.find_pairs(pair_conn, 3, True)) == {(1, 2)}


def test_find_pairs_wider_window(pair_conn):
    # ids 2 and 3 are 7 days apart, both from 'statement' (so not cross-source).
    assert (2, 3) in _ids(reconcile.find_pairs(pair_conn, 7, False))
    assert (2, 3) not in _ids(reconcile.find_pairs(pair_conn, 7, True))


def test_drop(pair_conn):
    reconcile.cmd_drop(pair_conn, 2)
    assert pair_conn.execute("SELECT COUNT(*) FROM transactions WHERE id=2").fetchone()[0] == 0
    with pytest.raises(SystemExit):
        reconcile.cmd_drop(pair_conn, 2)


# ---- export_analysis -----------------------------------------------------

@pytest.fixture
def snap_conn(seeded_conn):
    commit_transactions(seeded_conn, [_t("2026-09-02", 10_000, "sheet", "x")])
    for m in ("2026-08", "2026-09"):
        gs.upsert_snapshot(seeded_conn, gs.build_snapshot(seeded_conn, m))
    seeded_conn.commit()
    return seeded_conn


def test_fetch_snapshots_all_and_filters(snap_conn):
    allm = export_analysis.fetch_snapshots(snap_conn, None, False)
    assert [s["month"] for s in allm] == ["2026-08", "2026-09"]
    assert isinstance(allm[0]["category_totals"], dict)       # JSON decoded
    assert [s["month"] for s in export_analysis.fetch_snapshots(
        snap_conn, ["2026-08"], False)] == ["2026-08"]
    assert [s["month"] for s in export_analysis.fetch_snapshots(
        snap_conn, None, True)] == ["2026-09"]


def test_build_prompt_contains_only_aggregates(snap_conn):
    prompt = export_analysis.build_prompt(
        export_analysis.fetch_snapshots(snap_conn, None, False))
    assert prompt.startswith("# Personal Finance Analysis Request")
    body = prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    payload = json.loads(body)
    assert payload["currency"] == "IDR"
    assert len(payload["snapshots"]) == 2
    for leaked in ("Indomaret", "Cinema XXI", "Istri", "Bank Mandiri", "BCA Checking"):
        assert leaked not in prompt
