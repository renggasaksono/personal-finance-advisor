from pathlib import Path

import pytest

import ingest
import ingest_receipt
from ingest_receipt import categorize, parse_receipt_text

RECEIPT = """\
INDOMARET
Jl. Kemang Raya No. 5
NPWP 01.234.567.8-901.000
12/08/2026 19:42
AQUA 600ML      2   7.000
ROTI TAWAR          18.500
SUBTOTAL            25.500
PPN                  2.805
TOTAL BELANJA       28.305
TUNAI               50.000
KEMBALI             21.695
"""


def test_parse_receipt_text():
    assert parse_receipt_text(RECEIPT) == {
        "merchant": "INDOMARET",
        "date": "2026-08-12",
        "total": 28_305,     # not TUNAI (larger) or SUBTOTAL
    }


def test_total_on_following_line():
    text = "Apotek K24\n2026-08-07\nGRAND TOTAL\nRp 120.000\n"
    assert parse_receipt_text(text)["total"] == 120_000


def test_total_fallback_to_largest_amount():
    text = "Warung\n2026-08-07\nNasi 15.000\nTeh 5.000\n"
    assert parse_receipt_text(text)["total"] == 15_000


def test_categorize_longest_known_name():
    merchants = {"apotek": 1, "apotek k24": 7}
    assert categorize("APOTEK K24", "", merchants) == 7
    assert categorize(None, "struk dari indomaret", {"indomaret": 2}) == 2
    assert categorize("X", "y", {}) is None


class FakeEngine:
    def __init__(self, texts):
        self.texts = texts

    def extract_text(self, path):
        return self.texts[Path(path).name]


def test_receipt_ingest_with_fake_ocr(seeded_db_path, seeded_conn, tmp_path, monkeypatch):
    good, bad = tmp_path / "good.jpg", tmp_path / "bad.jpg"
    good.write_bytes(b"")
    bad.write_bytes(b"")
    engine = FakeEngine({"good.jpg": RECEIPT, "bad.jpg": "blurry nothing"})
    monkeypatch.setattr(ingest_receipt, "get_engine", lambda name: engine)

    n = ingest_receipt.ingest([good, bad], seeded_db_path, "fake", "Cash", False)
    assert n == 1
    row = seeded_conn.execute(
        "SELECT date, amount, category_id, account_id, source FROM transactions "
        "WHERE source='receipt' AND date='2026-08-12'").fetchone()
    assert row == ("2026-08-12", 28_305, 2, 2, "receipt")
    # Same photo again -> deduped.
    assert ingest_receipt.ingest([good], seeded_db_path, "fake", "Cash", False) == 0


# ---- ingest.py dispatcher ------------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("a.csv", "sheet"), ("a.XLSX", "sheet"), ("a.jpg", "receipt"),
    ("a.PNG", "receipt"), ("a.pdf", "pdf"), ("a.docx", None),
])
def test_classify(name, kind):
    assert ingest.classify(Path(name)) == kind


def test_collect_groups_mixed_folder(tmp_path):
    for n in ("s.csv", "r.jpg", "p.pdf", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    groups = ingest.collect(tmp_path)
    assert {k: [p.name for p in v] for k, v in groups.items()} == {
        "sheet": ["s.csv"], "receipt": ["r.jpg"], "pdf": ["p.pdf"]}
