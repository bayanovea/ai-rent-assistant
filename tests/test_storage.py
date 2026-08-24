"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from fb_rent import ai_schema, rules, storage


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


def _inline_payload(text: str, name: str):
    """Extract and decode one ``const <name> = ...;`` payload from the export."""
    marker = "const " + name + " = "
    start = text.index(marker) + len(marker)
    end = text.index(";\n", start)
    raw = text[start:end].replace("<\\/", "</").replace("<\\!--", "<!--")
    return json.loads(raw)


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
    data = _inline_payload(text, "DATA")
    assert [row["fb_id"] for row in data] == ["b", "c", "a"]


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


# --- AI analysis / decisions tables ------------------------------------


def _registry(version: int = 1, added_in: int = 1):
    return ai_schema.Registry(
        schema_version=version,
        fields=(
            {"key": "pets", "label": "Pets", "type": "enum",
             "values": ["allowed", "not_allowed", "unclear"], "added_in": 1},
            {"key": "parking", "label": "Parking", "type": "enum",
             "values": ["yes", "no", "unclear"], "added_in": added_in},
        ),
    )


def _make_analysis(**overrides):
    base = {
        "fb_id": "1234567890",
        "schema_version": 1,
        "analyzed_at": "2026-08-25T10:00:00Z",
        "source_hash": "hash-a",
        "model": "claude-opus-5 (claude-code)",
        "verdict": "Дом в посёлке, про животных ни слова.",
        "score": 62,
        "data_quality": "good",
        "data_json": {"pets": {"v": "unclear", "conf": "high", "ev": ""}},
    }
    base.update(overrides)
    return base


def test_init_db_creates_ai_tables_idempotently(tmp_path: Path):
    db_path = tmp_path / "rentals.db"
    storage.init_db(db_path).close()
    conn = storage.init_db(db_path)  # second call must not raise
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"listings", "ai_analysis", "decisions"} <= tables
    conn.close()


def test_init_db_keeps_existing_listings(tmp_path: Path):
    db_path = tmp_path / "rentals.db"
    conn = storage.init_db(db_path)
    storage.upsert_listing(conn, _make_listing())
    conn.close()

    conn = storage.init_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    conn.close()


def test_upsert_ai_analysis_inserts_and_updates(conn):
    assert storage.upsert_ai_analysis(conn, _make_analysis()) == "inserted"
    assert (
        storage.upsert_ai_analysis(
            conn, _make_analysis(verdict="Пересмотрел: студия.", score=10)
        )
        == "updated"
    )
    rows = conn.execute("SELECT * FROM ai_analysis").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "Пересмотрел: студия."
    assert rows[0]["score"] == 10
    assert json.loads(rows[0]["data_json"])["pets"]["v"] == "unclear"


def test_upsert_ai_analysis_requires_key_fields(conn):
    with pytest.raises(ValueError):
        storage.upsert_ai_analysis(conn, _make_analysis(fb_id=None))
    with pytest.raises(ValueError):
        storage.upsert_ai_analysis(conn, _make_analysis(verdict=""))


def test_upsert_ai_analysis_accepts_serialised_data_json(conn):
    storage.upsert_ai_analysis(
        conn, _make_analysis(data_json='{"pets": {"v": "allowed"}}')
    )
    row = conn.execute("SELECT data_json FROM ai_analysis").fetchone()
    assert json.loads(row["data_json"])["pets"]["v"] == "allowed"


def test_fetch_pending_new_listing(conn):
    storage.upsert_listing(conn, _make_listing())
    pending, no_raw = storage.fetch_pending(conn, _registry())
    assert no_raw == []
    assert [p["fb_id"] for p in pending] == ["1234567890"]
    assert pending[0]["pending_reason"] == "new"
    assert pending[0]["source_hash"]


def test_fetch_pending_skips_up_to_date_listing(conn):
    listing = _make_listing()
    storage.upsert_listing(conn, listing)
    current = ai_schema.source_hash(dict(conn.execute(
        "SELECT * FROM listings WHERE fb_id = ?", (listing["fb_id"],)
    ).fetchone()))
    storage.upsert_ai_analysis(conn, _make_analysis(source_hash=current))

    pending, no_raw = storage.fetch_pending(conn, _registry())
    assert pending == []
    assert no_raw == []


def test_fetch_pending_detects_changed_source_hash(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_ai_analysis(conn, _make_analysis(source_hash="stale-hash"))

    pending, _ = storage.fetch_pending(conn, _registry())
    assert [p["pending_reason"] for p in pending] == ["changed"]


def test_fetch_pending_detects_older_schema_version(conn):
    listing = _make_listing()
    storage.upsert_listing(conn, listing)
    current = ai_schema.source_hash(dict(conn.execute(
        "SELECT * FROM listings WHERE fb_id = ?", (listing["fb_id"],)
    ).fetchone()))
    storage.upsert_ai_analysis(
        conn, _make_analysis(source_hash=current, schema_version=1)
    )

    pending, _ = storage.fetch_pending(conn, _registry(version=2, added_in=2))
    assert [p["pending_reason"] for p in pending] == ["schema"]


def test_fetch_pending_separates_listings_without_raw_data(conn):
    storage.upsert_listing(
        conn,
        _make_listing(fb_id="no-raw", description=None, raw_json=None),
    )
    storage.upsert_listing(
        conn,
        _make_listing(
            fb_id="only-text-lines",
            description=None,
            raw_json='{"text_lines": ["Описание", "Дом в посёлке"]}',
        ),
    )
    storage.upsert_listing(conn, _make_listing(fb_id="has-description"))

    pending, no_raw = storage.fetch_pending(conn, _registry())
    assert [p["fb_id"] for p in no_raw] == ["no-raw"]
    assert {p["fb_id"] for p in pending} == {"only-text-lines", "has-description"}


def test_fetch_enriched_joins_analysis_and_fields(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_ai_analysis(conn, _make_analysis())

    rows = storage.fetch_enriched(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Cozy 2BR villa in Rawai"
    assert row["verdict"].startswith("Дом в посёлке")
    assert row["score"] == 62
    assert row["fields"]["pets"]["v"] == "unclear"
    assert row["analyzed"] is True
    assert row["status"] == "active"
    assert row["decided_by"] is None


def test_fetch_enriched_marks_unanalysed_listing(conn):
    storage.upsert_listing(conn, _make_listing())

    row = storage.fetch_enriched(conn)[0]
    assert row["analyzed"] is False
    assert row["fields"] == {}
    assert row["verdict"] is None
    assert row["status"] == "active"


def test_fetch_enriched_auto_rejects_from_rules(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(data_json={"pets": {"v": "not_allowed", "ev": "no pets"}}),
    )

    row = storage.fetch_enriched(conn)[0]
    assert row["status"] == "rejected"
    assert row["reason"] == rules.REASON_PETS
    assert row["decided_by"] == "ai"


def test_fetch_enriched_user_decision_overrides_ai(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_ai_analysis(
        conn, _make_analysis(data_json={"pets": {"v": "not_allowed"}})
    )
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, decided_at) "
        "VALUES (?, ?, ?, ?)",
        ("1234567890", "shortlist", "Хозяин сказал, что с собакой можно",
         "2026-08-25T12:00:00Z"),
    )
    conn.commit()

    row = storage.fetch_enriched(conn)[0]
    assert row["status"] == "shortlist"
    assert row["reason"] == "Хозяин сказал, что с собакой можно"
    assert row["decided_by"] == "user"
    # the AI verdict stays visible next to it
    assert row["ai_status"] == "rejected"
    assert row["ai_reason"] == rules.REASON_PETS


# --- HTML export against the AI field registry --------------------------


def test_export_html_colours_pets_and_marks_rejected(conn, tmp_path: Path):
    # Three synthetic listings, one per pets state: the export must colour
    # them green / yellow / red and strike the rejected one out with its
    # reason and who rejected it.
    registry = ai_schema.load_registry()
    cases = {"lgood": "allowed", "lwarn": "unclear", "lbad": "not_allowed"}
    for fb_id, pets in cases.items():
        storage.upsert_listing(conn, _make_listing(fb_id=fb_id))
        storage.upsert_ai_analysis(
            conn,
            _make_analysis(
                fb_id=fb_id,
                schema_version=registry.schema_version,
                data_json={
                    "pets": {"v": pets, "conf": "high", "ev": "цитата: " + pets},
                    "price_month_thb": {"v": 22000, "conf": "high", "ev": ""},
                },
            ),
        )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")

    rows = {r["fb_id"]: r for r in _inline_payload(text, "DATA")}
    assert rows["lgood"]["cells"]["pets"]["c"] == rules.CLASS_GOOD
    assert rows["lwarn"]["cells"]["pets"]["c"] == rules.CLASS_WARN
    assert rows["lbad"]["cells"]["pets"]["c"] == rules.CLASS_BAD
    # every AI claim is traceable to the quote behind it
    assert rows["lgood"]["cells"]["pets"]["ev"] == "цитата: allowed"

    assert rows["lgood"]["status"] == "active"
    assert rows["lbad"]["status"] == "rejected"
    assert rows["lbad"]["decided_by"] == "ai"
    assert rows["lbad"]["cells"]["reason"]["t"] == rules.REASON_PETS

    for needle in (rules.CLASS_GOOD, rules.CLASS_WARN, rules.CLASS_BAD,
                   rules.CLASS_STALE, rules.CLASS_NONE, "row-rejected"):
        assert needle in text
    assert rules.REASON_PETS in text

    # Every registry field is reachable as a column, plus the fixed Reason one.
    labels = [c["label"] for c in _inline_payload(text, "COLUMNS")]
    for field in registry.fields:
        assert field["label"] in labels
    assert "Reason" in labels
    assert len(labels) == len(storage._BASE_COLUMNS) + len(registry.fields)


def test_export_html_marks_user_rejection_as_user(conn, tmp_path: Path):
    storage.upsert_listing(conn, _make_listing(fb_id="mine"))
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, decided_at) "
        "VALUES (?, ?, ?, ?)",
        ("mine", "rejected", "Слишком далеко от школы", "2026-08-25T12:00:00Z"),
    )
    conn.commit()

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")

    row = _inline_payload(text, "DATA")[0]
    assert row["status"] == "rejected"
    assert row["decided_by"] == "user"
    assert row["cells"]["reason"]["t"] == "Слишком далеко от школы"
    assert "Слишком далеко от школы" in text


def test_export_html_stale_only_for_fields_added_after_analysis(conn, tmp_path: Path):
    # `parking` arrives in schema v2; the row was analysed under v1, so its
    # empty parking cell is hatched (c-stale), while the empty `pets` cell —
    # a field that existed then — stays an ordinary grey dash.
    registry = _registry(version=2, added_in=2)
    storage.upsert_listing(conn, _make_listing(fb_id="old"))
    storage.upsert_ai_analysis(
        conn, _make_analysis(fb_id="old", schema_version=1, data_json={})
    )
    storage.upsert_listing(conn, _make_listing(fb_id="fresh"))

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    rows = {
        r["fb_id"]: r
        for r in _inline_payload(out.read_text(encoding="utf-8"), "DATA")
    }

    assert rows["old"]["cells"]["parking"]["c"] == rules.CLASS_STALE
    assert rows["old"]["cells"]["pets"]["c"] == rules.CLASS_NONE
    # A listing that was never analysed has no stale cells at all: nothing
    # was ever looked at, so hatching would claim more than we know.
    assert rows["fresh"]["cells"]["parking"]["c"] == rules.CLASS_NONE


def test_export_html_prices_coloured_against_budget(conn, tmp_path: Path):
    registry = _registry()
    storage.upsert_listing(conn, _make_listing(fb_id="cheap", price_thb=22000))
    storage.upsert_listing(conn, _make_listing(fb_id="edge", price_thb=32000))
    storage.upsert_listing(conn, _make_listing(fb_id="over", price_thb=40000))

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    rows = {
        r["fb_id"]: r
        for r in _inline_payload(out.read_text(encoding="utf-8"), "DATA")
    }

    assert rows["cheap"]["cells"]["price_thb"]["c"] == rules.CLASS_PLAIN
    assert rows["edge"]["cells"]["price_thb"]["c"] == rules.CLASS_WARN
    assert rows["over"]["cells"]["price_thb"]["c"] == rules.CLASS_BAD
    assert rows["cheap"]["cells"]["price_thb"]["t"] == "22 000"
