"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from fb_rent import storage


@pytest.fixture()
def conn():
    c = storage.init_db(":memory:")
    yield c
    c.close()


def _make_listing(**overrides):
    base = {
        "fb_id": "1234567890",
        "url": "https://www.facebook.com/marketplace/item/1234567890",
        "title": "Cozy 2BR villa in Rawai",
        "price_thb": 22000,
        "price_raw": "THB 22,000/month",
        "district": "Rawai",
        "bedrooms": 2,
        "property_type": "villa",
        "description": "Nice villa near the beach.",
        "published_at": "Listed 3 days ago",
        "raw_json": '{"foo": "bar"}',
    }
    base.update(overrides)
    return base


def test_init_db_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "rentals.db"
    conn = storage.init_db(db_path)
    conn.close()
    # calling twice should not raise
    conn = storage.init_db(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='listings'"
    ).fetchall()
    assert len(tables) == 1
    indexes = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_last_seen" in indexes
    assert "idx_price" in indexes
    conn.close()


def test_upsert_insert_new(conn):
    result = storage.upsert_listing(conn, _make_listing(), now="2026-08-24T10:00:00Z")
    assert result == "inserted"
    row = conn.execute("SELECT * FROM listings WHERE fb_id = ?", ("1234567890",)).fetchone()
    assert row["title"] == "Cozy 2BR villa in Rawai"
    assert row["first_seen_at"] == "2026-08-24T10:00:00Z"
    assert row["last_seen_at"] == "2026-08-24T10:00:00Z"


def test_upsert_update_preserves_first_seen_at(conn):
    storage.upsert_listing(conn, _make_listing(), now="2026-08-24T10:00:00Z")
    result = storage.upsert_listing(
        conn,
        _make_listing(price_thb=24000, description="Price bumped."),
        now="2026-08-25T12:00:00Z",
    )
    assert result == "updated"
    row = conn.execute("SELECT * FROM listings WHERE fb_id = ?", ("1234567890",)).fetchone()
    assert row["first_seen_at"] == "2026-08-24T10:00:00Z"
    assert row["last_seen_at"] == "2026-08-25T12:00:00Z"
    assert row["price_thb"] == 24000
    assert row["description"] == "Price bumped."


def test_upsert_requires_fb_id(conn):
    with pytest.raises(ValueError):
        storage.upsert_listing(conn, {"url": "x", "title": "y"})


def test_upsert_allows_null_price_and_unicode(conn):
    storage.upsert_listing(
        conn,
        _make_listing(
            fb_id="unicode-1",
            price_thb=None,
            price_raw=None,
            description="Вилла у моря — 🌴 chill vibes",
            title="Studio in Kata",
        ),
    )
    row = conn.execute("SELECT * FROM listings WHERE fb_id = ?", ("unicode-1",)).fetchone()
    assert row["price_thb"] is None
    assert row["description"] == "Вилла у моря — 🌴 chill vibes"


def test_upsert_dedup_no_duplicate_rows(conn):
    for _ in range(3):
        storage.upsert_listing(conn, _make_listing())
    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert count == 1


def test_export_csv_empty_db_writes_header_only(conn, tmp_path: Path):
    out = tmp_path / "rentals.csv"
    storage.export_csv(conn, out)
    with out.open() as fh:
        rows = list(csv.reader(fh))
    assert rows == [list(storage.CSV_COLUMNS)]


def test_export_csv_column_order_and_sorting(conn, tmp_path: Path):
    storage.upsert_listing(
        conn,
        _make_listing(fb_id="a", title="First"),
        now="2026-08-24T09:00:00Z",
    )
    storage.upsert_listing(
        conn,
        _make_listing(fb_id="b", title="Second"),
        now="2026-08-24T11:00:00Z",
    )
    storage.upsert_listing(
        conn,
        _make_listing(fb_id="c", title="Third"),
        now="2026-08-24T10:00:00Z",
    )
    out = tmp_path / "rentals.csv"
    storage.export_csv(conn, out)
    with out.open() as fh:
        reader = csv.reader(fh)
        header = next(reader)
        data = list(reader)
    assert header == list(storage.CSV_COLUMNS)
    titles = [r[header.index("title")] for r in data]
    assert titles == ["Second", "Third", "First"]


def test_export_csv_creates_parent_dir(conn, tmp_path: Path):
    out = tmp_path / "nested" / "dir" / "rentals.csv"
    storage.export_csv(conn, out)
    assert out.exists()


def test_upsert_serialises_dict_raw_json(conn):
    # scraper_detail returns raw_json as a dict; sqlite3 cannot bind dicts
    # directly, so the storage layer must serialise them to JSON text.
    raw = {"url": "u", "text_lines": ["a", "b"], "title": "Villa"}
    storage.upsert_listing(conn, _make_listing(fb_id="dict-raw", raw_json=raw))
    row = conn.execute(
        "SELECT raw_json FROM listings WHERE fb_id = ?", ("dict-raw",)
    ).fetchone()
    assert isinstance(row["raw_json"], str)
    assert json.loads(row["raw_json"]) == raw


def test_upsert_passes_through_string_raw_json(conn):
    storage.upsert_listing(
        conn, _make_listing(fb_id="str-raw", raw_json='{"a": 1}')
    )
    row = conn.execute(
        "SELECT raw_json FROM listings WHERE fb_id = ?", ("str-raw",)
    ).fetchone()
    assert row["raw_json"] == '{"a": 1}'


def test_export_html_empty_db(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    assert "<title>fb-rent listings</title>" in text
    assert "fb-rent — 0 listings" in text
    assert "const DATA = [];" in text


def test_export_html_embeds_rows_sorted(conn, tmp_path: Path):
    storage.upsert_listing(
        conn, _make_listing(fb_id="a", title="First"), now="2026-08-24T09:00:00Z"
    )
    storage.upsert_listing(
        conn, _make_listing(fb_id="b", title="Second"), now="2026-08-24T11:00:00Z"
    )
    storage.upsert_listing(
        conn, _make_listing(fb_id="c", title="Third"), now="2026-08-24T10:00:00Z"
    )
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")

    assert "fb-rent — 3 listings" in text
    # Data payload is embedded as a JSON array; extract and verify order.
    marker = "const DATA = "
    start = text.index(marker) + len(marker)
    end = text.index(";", start)
    data = json.loads(text[start:end])
    assert [row[0] for row in data] == ["b", "c", "a"]


def test_export_html_neutralises_script_breakout(conn, tmp_path: Path):
    # A malicious listing must not be able to close the embedded <script>
    # block by embedding a literal </script> in one of its fields.
    storage.upsert_listing(
        conn,
        _make_listing(
            fb_id="xss",
            district="pwn</script><script>alert(1)</script>",
        ),
    )
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in text
    # The payload should be present in escaped form.
    assert "<\\/script>" in text
