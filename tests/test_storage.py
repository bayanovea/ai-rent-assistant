"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from fb_rent import ai_schema, config, photos, rules, storage


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


def test_a_card_only_resighting_keeps_what_the_detail_page_gave(conn):
    """A listing re-seen in the feed must not lose its ad text.

    ``scrape`` skips the detail page of a listing already in the DB, so what
    comes back is a bare list card. Writing its missing fields as NULL erased
    the description, the gallery URLs behind the photo pass and the content
    key the repost detection runs on — once per wide sweep.
    """
    ad = (
        "Spacious two-bedroom villa a short walk from Rawai beach, private "
        "pool, parking for two cars, pets welcome, minimum six months."
    )
    storage.upsert_listing(
        conn,
        _make_listing(description=ad, raw_json='{"gallery_urls": ["a.jpg"]}'),
        now="2026-08-24T10:00:00Z",
    )
    before = conn.execute(
        "SELECT * FROM listings WHERE fb_id = ?", ("1234567890",)
    ).fetchone()

    storage.upsert_listing(
        conn,
        {
            "fb_id": "1234567890",
            "url": "https://www.facebook.com/marketplace/item/1234567890",
            "title": "Cozy 2BR villa in Rawai",
            "price_raw": "THB 22,000/month",
            "price_thb": 22000,
            "district": "Rawai",
        },
        now="2026-08-25T12:00:00Z",
    )

    row = conn.execute(
        "SELECT * FROM listings WHERE fb_id = ?", ("1234567890",)
    ).fetchone()
    assert row["description"] == ad
    assert row["raw_json"] == '{"gallery_urls": ["a.jpg"]}'
    assert row["published_at"] == before["published_at"]
    assert row["content_key"] == before["content_key"]
    assert row["last_seen_at"] == "2026-08-25T12:00:00Z"


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
    # Открывают таблицу ради лучшего, поэтому она сортируется сама.
    assert 'let sorts = [{ key: "score", dir: -1 }];' in text


def test_export_html_ships_the_filter_panel(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    # Фильтры и мульти-сортировка живут целиком в шаблоне; тест сторожит те
    # узлы, по которым скрипт находит панель — без них она молча не соберётся.
    assert 'id="filters"' in text
    assert 'id="flt-grid"' in text
    assert 'id="flt-reset"' in text


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
    # Recomputed from the fields and the district, not the stored 62.
    assert row["score"] == rules.score(row["fields"], "Rawai")
    assert row["fields"]["pets"]["v"] == "unclear"
    assert row["analyzed"] is True
    assert row["status"] == "active"
    assert row["decided_by"] is None


def test_fetch_enriched_rescores_instead_of_reading_the_stored_column(conn):
    # A weights change has to reach rows analysed before it, so the score is
    # derived on read exactly like the verdict is.
    storage.upsert_listing(conn, _make_listing(district="Kathu, Phuket, Thailand"))
    storage.upsert_ai_analysis(conn, _make_analysis(score=7))

    row = storage.fetch_enriched(conn)[0]
    assert row["score"] != 7
    assert row["score"] == rules.score(row["fields"], "Kathu, Phuket, Thailand")


def test_fetch_enriched_rejects_a_listing_facebook_tagged_as_patong(conn):
    storage.upsert_listing(conn, _make_listing(district="Patong"))
    storage.upsert_ai_analysis(conn, _make_analysis())

    row = storage.fetch_enriched(conn)[0]
    assert row["ai_status"] == "rejected"
    assert row["ai_reason"] == rules.REASON_AREA


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

    # Every registry field is reachable as a column — the two that merge into
    # a base column under their own label, the rest under the registry's.
    columns = _inline_payload(text, "COLUMNS")
    labels = [c["label"] for c in columns]
    merged = storage._MERGED_FIELD_KEYS
    for field in registry.fields:
        if field["key"] not in merged:
            assert field["label"] in labels
    assert "Причина" in labels
    assert len(labels) == (
        len(storage._FIXED_COLUMNS) + len(registry.fields) - len(merged)
    )
    # `Качество данных` is no longer one of them: it rides on the ID cell.
    assert "Качество данных" not in labels


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


def _photo_registry(version: int = 2, photo_added_in: int = 1):
    """Registry with all three sources represented: text, both, photo-only."""
    return ai_schema.Registry(
        schema_version=version,
        fields=(
            {"key": "pets", "label": "Pets", "type": "enum",
             "values": ["allowed", "not_allowed", "unclear"], "added_in": 1},
            {"key": "parking", "label": "Parking", "type": "enum",
             "values": ["yes", "no", "unclear"], "added_in": version,
             "source": "text"},
            {"key": "furnished", "label": "Furnished", "type": "enum",
             "values": ["full", "partial", "no", "unclear"],
             "added_in": photo_added_in, "source": "both"},
            {"key": "photo_quality", "label": "Photo quality", "type": "enum",
             "values": ["real", "render", "unclear"],
             "added_in": photo_added_in, "source": "photo"},
        ),
    )


def _attach_photos(conn, fb_id: str, data: dict, **overrides):
    """Give an already-analysed listing a photo pass."""
    row = {
        "fb_id": fb_id,
        "photo_schema_version": 2,
        "photos_hash": "photos-a",
        "photos_analyzed_at": "2026-08-25T12:00:00Z",
        "photo_model": "claude-sonnet-5 (claude-code)",
        "photo_comment": "Мебель на снимках выглядит старой.",
        "photo_data_json": data,
    }
    row.update(overrides)
    storage.upsert_photo_analysis(conn, row)


def test_fetch_enriched_merges_photo_fields_over_text(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="p1"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="p1",
            schema_version=2,
            data_json={
                "furnished": {"v": "full", "ev": "fully furnished"},
                "pets": {"v": "allowed", "ev": "pets ok"},
            },
        ),
    )
    _attach_photos(
        conn,
        "p1",
        {
            "furnished": {"v": "partial", "ev": "фото 2: пустая спальня"},
            "photo_quality": {"v": "real", "ev": "фото 1: реальный снимок"},
        },
    )

    item = storage.fetch_enriched(conn)[0]
    # The photo wins where it answered, the text survives where it did not.
    assert rules.value_of(item["fields"], "furnished") == "partial"
    assert rules.value_of(item["fields"], "pets") == "allowed"
    assert rules.value_of(item["fields"], "photo_quality") == "real"
    assert item["photo_keys"] == ["furnished", "photo_quality"]
    # Only the contradiction is an override: filling `photo_quality` is not.
    assert item["photo_overrides"] == {"furnished": "full"}
    assert item["photo_analyzed"] is True
    assert item["photo_comment"] == "Мебель на снимках выглядит старой."


def test_fetch_enriched_without_photos_has_no_overrides(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="t1"))
    storage.upsert_ai_analysis(conn, _make_analysis(fb_id="t1"))

    item = storage.fetch_enriched(conn)[0]
    assert item["photo_keys"] == []
    assert item["photo_overrides"] == {}
    assert item["photo_analyzed"] is False


def test_export_html_marks_the_photo_columns(conn, tmp_path: Path):
    registry = _photo_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="p1"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="p1",
            schema_version=2,
            data_json={"furnished": {"v": "full", "ev": "fully furnished"}},
        ),
    )
    _attach_photos(conn, "p1", {"furnished": {"v": "partial", "ev": "фото 2"}})

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")

    # The columns the gallery pass fills are tagged `photo` wherever they sit
    # in the layout, and that tag is the one source distinction still painted.
    columns = _inline_payload(text, "COLUMNS")
    photo = {c["key"] for c in columns if c["src"] == "photo"}
    assert photo == {"furnished", "photo_quality", "photo_comment"}
    assert ".rt th.src-photo, .rt td.src-photo" in text
    # The header bands are gone; nothing spans columns any more.
    assert "colspan" not in text

    row = _inline_payload(text, "DATA")[0]
    assert row["cells"]["furnished"]["t"] == "partial"
    assert row["cells"]["furnished"]["was"] == "текст: full"
    assert row["cells"]["photo_comment"]["t"] == "Мебель на снимках выглядит старой."


def test_export_html_photo_override_badge_is_absent_without_disagreement(
    conn, tmp_path: Path
):
    registry = _photo_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="p2"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="p2",
            schema_version=2,
            data_json={"furnished": {"v": "partial", "ev": "partly furnished"}},
        ),
    )
    _attach_photos(conn, "p2", {"furnished": {"v": "partial", "ev": "фото 2"}})

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    row = _inline_payload(out.read_text(encoding="utf-8"), "DATA")[0]
    assert "was" not in row["cells"]["furnished"]


def test_export_html_photo_staleness_uses_the_photo_schema_version(
    conn, tmp_path: Path
):
    # `furnished` and `photo_quality` arrive in v2 of the registry. The photo
    # pass on `seen` ran at v2, so its empty photo cells are ordinary dashes —
    # even though the text pass behind them is still stuck at v1, which is
    # what hatches its own `parking`. `blind` was photographed at v1, so the
    # same cells there are hatched.
    registry = _photo_registry(version=2, photo_added_in=2)
    for fb_id in ("seen", "blind", "textonly"):
        storage.upsert_listing(conn, _make_listing(fb_id=fb_id))
        storage.upsert_ai_analysis(
            conn, _make_analysis(fb_id=fb_id, schema_version=1, data_json={})
        )
    _attach_photos(conn, "seen", {}, photo_schema_version=2)
    _attach_photos(conn, "blind", {}, photo_schema_version=1)

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    rows = {
        r["fb_id"]: r
        for r in _inline_payload(out.read_text(encoding="utf-8"), "DATA")
    }

    assert rows["seen"]["cells"]["photo_quality"]["c"] == rules.CLASS_NONE
    assert rows["seen"]["cells"]["furnished"]["c"] == rules.CLASS_NONE
    assert rows["seen"]["cells"]["parking"]["c"] == rules.CLASS_STALE
    assert rows["blind"]["cells"]["photo_quality"]["c"] == rules.CLASS_STALE
    # No photo pass at all: the photo band falls back to the text pass's
    # version, so a field added after that analysis is hatched as before.
    assert rows["textonly"]["cells"]["photo_quality"]["c"] == rules.CLASS_STALE


def test_export_html_photo_fields_reach_the_verdict(conn, tmp_path: Path):
    # The merged fields are what `rules` sees, so a photo can reject a row.
    registry = _photo_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="p3"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="p3",
            schema_version=2,
            data_json={"pets": {"v": "allowed", "ev": "pets ok"}},
        ),
    )
    _attach_photos(conn, "p3", {"pets": {"v": "not_allowed", "ev": "фото 3: знак"}})

    item = storage.fetch_enriched(conn)[0]
    assert item["status"] == "rejected"
    assert item["ai_reason"] == rules.REASON_PETS


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

    assert rows["cheap"]["cells"]["price"]["c"] == rules.CLASS_PLAIN
    assert rows["edge"]["cells"]["price"]["c"] == rules.CLASS_WARN
    assert rows["over"]["cells"]["price"]["c"] == rules.CLASS_BAD
    assert rows["cheap"]["cells"]["price"]["t"] == "22 000"
    # Nothing analysed these, so the price on screen is the parsed one.
    assert rows["cheap"]["cells"]["price"]["src"] == "fb"


# --- merged columns, district cleanup and the AI comment ----------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rawai, Phuket, Thailand", "Rawai"),
        ("Amphoe Thalang", "Thalang"),
        ("Ban Ket Ho, Phuket, Thailand", "Ban Ket Ho"),
        ("Patong", "Patong"),
        # The island and the province name no district — better an empty cell
        # than "Пхукет" repeated down the whole column.
        ("Пхукет", ""),
        ("Changwat Phuket, Phuket, Thailand", ""),
        (None, ""),
    ],
)
def test_normalise_district(raw, expected):
    assert storage.normalise_district(raw) == expected


def test_export_html_price_and_area_prefer_the_ai_reading(conn, tmp_path: Path):
    # The card says 22 000 in Rawai; the ad text says 16 000 on a 12-month
    # contract in Kathu. One column each, showing the AI's reading, with the
    # card's own value kept in the tooltip rather than dropped.
    registry = ai_schema.load_registry()
    storage.upsert_listing(
        conn,
        _make_listing(fb_id="merged", price_thb=22000, district="Rawai, Phuket, Thailand"),
    )
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="merged",
            schema_version=registry.schema_version,
            data_json={
                "price_month_thb": {"v": 16000, "ev": "• 12 months: 16,000 THB/month"},
                "area": {"v": "Kathu", "ev": "กะทู้, 83120"},
            },
        ),
    )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    row = _inline_payload(out.read_text(encoding="utf-8"), "DATA")[0]

    assert row["cells"]["price"]["t"] == "16 000"
    assert row["cells"]["price"]["src"] == "ai"
    assert "22 000" in row["cells"]["price"]["ev"]
    assert row["cells"]["area"]["t"] == "Kathu"
    assert row["cells"]["area"]["src"] == "ai"
    # Neither registry field gets a second column of its own.
    keys = [c["key"] for c in _inline_payload(out.read_text(encoding="utf-8"), "COLUMNS")]
    assert "price_month_thb" not in keys
    assert "area" in keys and keys.count("area") == 1


def test_unclear_reads_as_the_grey_dash_except_for_pets(conn, tmp_path: Path):
    # "Не сказано" is what the dash already means, and the word takes as much
    # room on screen as a real answer — on five columns where it is the most
    # common value. Pets is the exception: not knowing is the reason to call.
    registry = ai_schema.load_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="u1"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="u1",
            schema_version=registry.schema_version,
            data_json={
                "pets": {"v": "unclear", "conf": "low", "ev": ""},
                "parking": {"v": "unclear", "conf": "low", "ev": ""},
                "green_area": {"v": "unclear", "conf": "low", "ev": ""},
                "kitchen": {"v": "full", "conf": "high", "ev": "kitchen"},
            },
        ),
    )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    row = _inline_payload(out.read_text(encoding="utf-8"), "DATA")[0]

    assert row["cells"]["pets"]["t"] == "unclear"
    assert row["cells"]["pets"]["c"] == rules.CLASS_WARN
    assert row["cells"]["parking"]["t"] == ""
    assert row["cells"]["parking"]["c"] == rules.CLASS_NONE
    assert row["cells"]["green_area"]["t"] == ""
    assert row["cells"]["kitchen"]["t"] == "full"


def test_bad_data_quality_is_a_badge_on_the_id(conn, tmp_path: Path):
    registry = ai_schema.load_registry()
    for fb_id, quality in (("ok", "good"), ("bad", "sidebar_noise")):
        storage.upsert_listing(conn, _make_listing(fb_id=fb_id))
        storage.upsert_ai_analysis(
            conn,
            _make_analysis(
                fb_id=fb_id,
                schema_version=registry.schema_version,
                data_quality=quality,
            ),
        )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")
    rows = {r["fb_id"]: r for r in _inline_payload(text, "DATA")}

    assert rows["bad"]["cells"]["fb_id"]["dq"] == "sidebar_noise"
    assert rows["ok"]["cells"]["fb_id"]["dq"] == ""
    assert "data_quality" not in rows["ok"]["cells"]
    # The badge is searchable even though it is not a column.
    assert '(id.dq || "")' in text


def test_html_columns_fold_extras_behind_the_toggle():
    registry = ai_schema.load_registry()
    columns = storage.html_columns(registry)
    hidden = [c["key"] for c in columns if c["hidden"]]

    # The ones that most often decide whether a viewing is worth it open the
    # folded group.
    assert hidden[:3] == ["security", "deposit", "parking"]
    assert {"kitchen", "furnished", "aircon"} <= set(hidden)
    for key in ("pets", "bedrooms", "property_type"):
        assert next(c for c in columns if c["key"] == key)["hidden"] is False

    # The default view is `_VISIBLE_ORDER` verbatim — numbers first, prose
    # after — and nothing in it is folded away.
    shown = [c["key"] for c in columns if not c["hidden"]]
    assert tuple(shown) == storage._VISIBLE_ORDER
    assert shown.index("size_sqm") < shown.index("verdict")

    # `first_seen_at` is ours rather than the ad's, so it ships folded; the
    # date the ad itself carries is the one on screen.
    assert "first_seen_at" in hidden
    assert "published_at" in shown


def test_verdict_and_comment_are_stored_side_by_side(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            verdict="Стоит посмотреть — дом с гостиной",
            comment="Дом 2 спальни в Раваи, интернет включён в цену.",
        ),
    )

    row = storage.fetch_enriched(conn)[0]
    assert row["verdict"] == "Стоит посмотреть — дом с гостиной"
    assert row["comment"] == "Дом 2 спальни в Раваи, интернет включён в цену."


# --- reposts: one flat, one row -----------------------------------------


def _with_body(fb_id: str, body: str, price: int = 16000):
    """A listing whose detail page carried ``body`` as its ad text."""
    return _make_listing(
        fb_id=fb_id,
        url=f"https://www.facebook.com/marketplace/item/{fb_id}/",
        price_thb=price,
        raw_json={"text_lines": ["Описание", body, "Информация о продавце"]},
    )


_AD = (
    "🔥 Condo for rent near Central Phuket, 1 bedroom, fully furnished, "
    "pool and gym, 12-month contract, contact us for a viewing"
)


def test_repost_under_a_new_id_does_not_get_its_own_row(conn):
    # Agencies republish to lift an ad back up the feed and Facebook mints a
    # fresh fb_id each time; the ad text is what identifies the flat.
    assert storage.upsert_listing(conn, _with_body("first", _AD)) == "inserted"
    assert storage.upsert_listing(conn, _with_body("repost", _AD)) == "duplicate"

    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    row = conn.execute(
        "SELECT canonical_fb_id FROM duplicates WHERE fb_id = 'repost'"
    ).fetchone()
    assert row["canonical_fb_id"] == "first"
    # The scraper reads this set to skip the repost before fetching its page.
    assert storage.duplicate_fb_ids(conn) == {"repost"}


def test_rescraping_the_same_listing_still_updates_it(conn):
    storage.upsert_listing(conn, _with_body("first", _AD))
    assert storage.upsert_listing(conn, _with_body("first", _AD)) == "updated"
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM duplicates").fetchone()[0] == 0


def test_same_ad_at_a_different_price_is_a_separate_listing(conn):
    storage.upsert_listing(conn, _with_body("cheap", _AD, price=16000))
    assert storage.upsert_listing(
        conn, _with_body("pricier", _AD, price=19000)
    ) == "inserted"
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 2


def test_thin_text_is_never_treated_as_a_repost(conn):
    # Two listings whose scrape caught only the "Выбор дня" sidebar share that
    # text without being the same flat — fingerprinting them would merge
    # unrelated ads, so a body this short yields no key at all.
    assert storage.content_key({"description": "Выбор дня"}) is None
    storage.upsert_listing(conn, _with_body("thin-a", "Выбор дня"))
    assert storage.upsert_listing(conn, _with_body("thin-b", "Выбор дня")) == "inserted"
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 2


def test_dedupe_listings_keeps_the_copy_carrying_the_most_work(conn):
    # Seen first, analysed, decided on — three copies, and the keeper is the
    # one the user already ruled on, not merely the oldest.
    for fb_id in ("oldest", "analysed", "decided"):
        conn.execute(
            "INSERT INTO listings (fb_id, url, title, price_thb, content_key,"
            " raw_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                fb_id,
                f"https://www.facebook.com/marketplace/item/{fb_id}/",
                "t",
                16000,
                "shared-key",
                json.dumps({"text_lines": ["Описание", _AD]}),
                {"oldest": "2026-08-01", "analysed": "2026-08-02", "decided": "2026-08-03"}[fb_id],
                "2026-08-25",
            ),
        )
    storage.upsert_ai_analysis(conn, _make_analysis(fb_id="analysed"))
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, decided_at) VALUES (?,?,?,?)",
        ("decided", "shortlist", "звонил хозяину", "2026-08-25T12:00:00Z"),
    )
    conn.commit()

    assert storage.dedupe_listings(conn, dry_run=True) == [
        {"canonical": "decided", "removed": ["analysed", "oldest"], "reason": "exact"}
    ]
    # A dry run touches nothing.
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 3

    storage.dedupe_listings(conn)
    left = [r["fb_id"] for r in conn.execute("SELECT fb_id FROM listings")]
    assert left == ["decided"]
    # The dropped copy's analysis goes with it, and both are remembered so a
    # rescrape skips them instead of adding them back.
    assert conn.execute("SELECT count(*) FROM ai_analysis").fetchone()[0] == 0
    assert storage.duplicate_fb_ids(conn) == {"oldest", "analysed"}


def test_reposts_collapse_into_one_row_keeping_every_link(conn, tmp_path: Path):
    # Six postings of one flat are one line in the table, but each posting
    # keeps its own id and link — the user still has to message someone.
    storage.upsert_listing(conn, _with_body("keep", _AD))
    for fb_id in ("repost-a", "repost-b"):
        storage.upsert_listing(conn, _with_body(fb_id, _AD))

    grouped = storage.duplicates_by_canonical(conn)
    assert [d["fb_id"] for d in grouped["keep"]] == ["repost-a", "repost-b"]
    assert grouped["keep"][0]["url"].endswith("/marketplace/item/repost-a/")

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    rows = _inline_payload(text, "DATA")

    assert len(rows) == 1
    dups = rows[0]["cells"]["fb_id"]["dups"]
    assert [d["fb_id"] for d in dups] == ["repost-a", "repost-b"]
    # Each repost's link survives into the page.
    for fb_id in ("repost-a", "repost-b"):
        assert f"/marketplace/item/{fb_id}/" in text


def test_listing_without_reposts_has_an_empty_dup_list(conn, tmp_path: Path):
    storage.upsert_listing(conn, _with_body("alone", _AD))
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    row = _inline_payload(out.read_text(encoding="utf-8"), "DATA")[0]
    assert row["cells"]["fb_id"]["dups"] == []


def test_dedupe_records_the_url_of_every_collapsed_repost(conn):
    for fb_id in ("first", "second"):
        listing = _with_body(fb_id, _AD)
        listing["url"] = f"https://www.facebook.com/marketplace/item/{fb_id}/"
        conn.execute(
            "INSERT INTO listings (fb_id, url, title, price_thb, content_key,"
            " raw_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (fb_id, listing["url"], "t", 16000, storage.content_key(listing),
             json.dumps(listing["raw_json"]), "2026-08-0" + ("1" if fb_id == "first" else "2"),
             "2026-08-25"),
        )
    conn.commit()

    storage.dedupe_listings(conn)
    grouped = storage.duplicates_by_canonical(conn)
    assert grouped["first"] == [
        {"fb_id": "second", "url": "https://www.facebook.com/marketplace/item/second/"}
    ]


# --- one flat, several agents -------------------------------------------


def _analysed(conn, fb_id: str, body: str, price: int, data_quality="good", **fields):
    """A listing plus the AI fields the near-duplicate rules read."""
    listing = _with_body(fb_id, body, price=price)
    storage.upsert_listing(conn, listing)
    data = {k: {"v": v, "conf": "high", "ev": ""} for k, v in fields.items()}
    data["price_month_thb"] = {"v": price, "conf": "high", "ev": ""}
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id=fb_id,
            source_hash="h-" + fb_id,
            data_json=data,
            data_quality=data_quality,
        ),
    )
    return listing


_AD_B = (
    "Rent this bright one bedroom flat in the same project, walking distance "
    "to the mall, washing machine and a balcony, viewing any day"
)

_AD_C = (
    "Brand new unit ready to move in, mountain view, covered parking, "
    "shuttle bus to the beach, one year contract preferred"
)


def test_same_flat_from_two_agents_collapses(conn):
    # Different agents write their own text, so the exact fingerprint misses
    # them; project + price + bedrooms is what identifies the flat.
    _analysed(conn, "agent-a", _AD, 17000, project_name="Phanason Green Place",
              bedrooms=1, size_sqm=48)
    _analysed(conn, "agent-b", _AD_B, 17000, project_name="Panason Green Place",
              bedrooms=1, size_sqm=50)
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 2

    collapsed = storage.dedupe_listings(conn)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "facts"
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    assert storage.duplicate_fb_ids(conn) == {collapsed[0]["removed"][0]}


def test_an_ad_naming_no_floor_does_not_chain_two_floors_together(conn):
    # The trap the group-wide veto exists for: the silent ad contradicts
    # nobody, so on `facts` alone it matches both stacked flats and would
    # drag floor 2 and floor 8 into one row through itself.
    _analysed(conn, "floor-2", _AD + " Building B, 2nd Floor", 20000,
              project_name="The Base Bukit", bedrooms=1)
    _analysed(conn, "floor-8", _AD_B + " Building B, 8th Floor", 20000,
              project_name="The Base Bukit", bedrooms=1)
    _analysed(conn, "silent", _AD_C, 20000,
              project_name="The Base Bukit", bedrooms=1)

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert len(collapsed) == 1
    group = {collapsed[0]["canonical"], *collapsed[0]["removed"]}
    assert "silent" in group
    assert not {"floor-2", "floor-8"} <= group


def test_different_floors_of_one_project_stay_apart(conn):
    # Same project, same price, same size — but the ads name different
    # floors, so they are two flats.
    _analysed(conn, "floor-2", _AD + " Building B, 2nd Floor", 15000,
              project_name="D Condo Reef", bedrooms=1, size_sqm=28)
    _analysed(conn, "floor-1", _AD_B + " Building B, 1st Floor", 15000,
              project_name="D Condo Reef", bedrooms=1, size_sqm=28)

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_different_buildings_of_one_project_stay_apart(conn):
    _analysed(conn, "bld-a", _AD + " Building A", 15000,
              project_name="The Origin", bedrooms=1)
    _analysed(conn, "bld-b", _AD + " Building B", 15000,
              project_name="The Origin", bedrooms=1)

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_same_project_at_a_different_price_stays_apart(conn):
    _analysed(conn, "cheap", _AD, 15000, project_name="The Base Downtown", bedrooms=1)
    _analysed(conn, "pricey", _AD_B, 16000, project_name="The Base Downtown", bedrooms=1)

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_a_trailing_word_in_the_project_name_still_matches(conn):
    # One agent writes the estate's full name, the other trims it. The ratio
    # alone drops to 0.82 on that suffix, below the threshold.
    _analysed(conn, "long", _AD, 26000, project_name="Pruksa Ville Thalang Hillside",
              bedrooms=3, property_type="house")
    _analysed(conn, "short", _AD_B, 26000, project_name="Pruksa Ville Thalang",
              bedrooms=3, property_type="house")

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "facts"


def test_one_house_priced_a_thousand_apart_collapses(conn):
    # Each agent adds his own margin to the same house.
    _analysed(conn, "cheap", _AD, 25000, project_name="Pruksa Ville Thalang",
              bedrooms=3, property_type="house")
    _analysed(conn, "pricey", _AD_B, 26000, project_name="Pruksa Ville Thalang",
              bedrooms=3, property_type="house")

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "facts"


def test_two_condo_units_priced_a_thousand_apart_stay_apart(conn):
    # The same slack inside a condo project would merge two different units,
    # so it is not granted there.
    _analysed(conn, "unit-a", _AD, 15000, project_name="The Base Downtown",
              bedrooms=1, property_type="condo")
    _analysed(conn, "unit-b", _AD_B, 16000, project_name="The Base Downtown",
              bedrooms=1, property_type="condo")

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_houses_priced_far_apart_stay_apart(conn):
    _analysed(conn, "cheap", _AD, 25000, project_name="Pruksa Ville Thalang",
              bedrooms=3, property_type="house")
    _analysed(conn, "pricey", _AD_B, 27000, project_name="Pruksa Ville Thalang",
              bedrooms=3, property_type="house")

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_sidebar_noise_never_matches_on_text(conn):
    # When the scrape stored the Facebook sidebar instead of the ad, that text
    # is identical across listings — a house and a condo in another project
    # would read 100% alike and collapse into one.
    sidebar = (
        "Выбор дня Пхукет 40 км Недавно опубликовано 12 000 ฿ 1 Bed 1 Bath "
        "Apartment Sale Rent Phuket Property MacBook Air 2015 Phuket Market"
    )
    _analysed(conn, "house", sidebar, 28000, data_quality="sidebar_noise",
              project_name="Chaofa Villa", bedrooms=3, property_type="house")
    _analysed(conn, "condo", sidebar, 20000, data_quality="sidebar_noise",
              project_name="The Base Central", bedrooms=1, property_type="condo")

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_a_readable_ad_still_matches_on_text(conn):
    # The guard is about bad data, not about the text rule itself.
    _analysed(conn, "a", _AD, 17000, project_name="Phanason Green Place", bedrooms=1)
    _analysed(conn, "b", _AD + " Call today", 17000,
              project_name="Phanason Green Place", bedrooms=1)

    assert len(storage.dedupe_listings(conn, dry_run=True)) == 1


def test_an_unrelated_project_sharing_a_word_stays_apart(conn):
    # Subset matching is on whole words, so "Green Place" is not swallowed by
    # "Green Place Two" unless every word lines up.
    _analysed(conn, "a", _AD, 20000, project_name="Chanakan Bo Ore",
              bedrooms=3, property_type="house")
    _analysed(conn, "b", _AD_B, 20000, project_name="Chanakan Delight",
              bedrooms=3, property_type="house")

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_exact_only_skips_the_near_duplicate_rules(conn):
    _analysed(conn, "agent-a", _AD, 17000, project_name="Phanason Green Place", bedrooms=1)
    _analysed(conn, "agent-b", _AD_B, 17000, project_name="Phanason Green Place", bedrooms=1)

    assert storage.dedupe_listings(conn, dry_run=True, fuzzy=False) == []
    assert len(storage.dedupe_listings(conn, dry_run=True, fuzzy=True)) == 1


def test_a_collapsed_listing_cannot_return_on_the_next_scrape(conn):
    # Its text no longer matches anything — that is the point of the fuzzy
    # rules — so only the `duplicates` record keeps it out.
    _analysed(conn, "keeper", _AD, 17000, project_name="Phanason Green Place", bedrooms=1)
    _analysed(conn, "merged", _AD_B, 17000, project_name="Panason Green Place", bedrooms=1)
    storage.dedupe_listings(conn)
    gone = storage.duplicate_fb_ids(conn).pop()

    again = _with_body(gone, "yet another agent writing this flat up from scratch again")
    assert storage.upsert_listing(conn, again) == "duplicate"
    assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1


def test_dedupe_keeps_the_copy_with_the_fullest_ad_text(conn):
    # Neither is decided on or analysed differently, so the longer ad wins:
    # it is the one worth reading.
    _analysed(conn, "terse", _AD, 17000, project_name="Zcape", bedrooms=1)
    _analysed(conn, "detailed", _AD_B + " " + _AD_B, 17000,
              project_name="Zcape", bedrooms=1)

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert collapsed[0]["canonical"] == "detailed"


def test_dedupe_purges_photos_of_the_collapsed_copies(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    for fb_id in ("keeper", "copy"):
        conn.execute(
            "INSERT INTO listings (fb_id, url, title, price_thb, content_key,"
            " raw_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                fb_id,
                f"https://www.facebook.com/marketplace/item/{fb_id}/",
                "t",
                16000,
                "shared-key",
                json.dumps({"text_lines": ["Описание", _AD]}),
                "2026-08-01" if fb_id == "keeper" else "2026-08-02",
                "2026-08-25",
            ),
        )
    conn.commit()
    for fb_id in ("keeper", "copy"):
        directory = photos.photos_dir(fb_id)
        directory.mkdir(parents=True)
        (directory / "01.jpg").write_bytes(b"jpeg")

    storage.dedupe_listings(conn, dry_run=True)
    assert photos.photos_dir("copy").exists()

    collapsed = storage.dedupe_listings(conn)

    assert collapsed[0]["canonical"] == "keeper"
    assert photos.photos_dir("keeper").exists()
    assert not photos.photos_dir("copy").exists()


# --- photo queue ---------------------------------------------------------


@pytest.fixture()
def photos_home(tmp_path, monkeypatch):
    """Point the photo store at a temp directory."""
    root = tmp_path / "photos"
    monkeypatch.setattr(config, "PHOTOS_DIR", root)
    return root


def _shoot(photos_home, fb_id: str, count: int = 3, body: bytes = b"jpeg"):
    """Put `count` fake photo files on disk for a listing."""
    directory = photos_home / fb_id
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (directory / f"{index:02d}.jpg").write_bytes(body)
    return directory


def _text_analysed(conn, fb_id: str, **fields):
    """Give a listing a text analysis, defaulting to one the rules accept."""
    storage.upsert_listing(conn, _make_listing(fb_id=fb_id))
    data = {"pets": {"v": "unclear", "conf": "high", "ev": ""}}
    data.update(fields)
    storage.upsert_ai_analysis(conn, _make_analysis(fb_id=fb_id, data_json=data))


def test_migration_adds_photo_columns_to_an_old_database(tmp_path: Path):
    db_path = tmp_path / "rentals.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE ai_analysis (
            fb_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
            analyzed_at TEXT NOT NULL, source_hash TEXT NOT NULL,
            model TEXT, verdict TEXT NOT NULL, score INTEGER,
            data_quality TEXT, data_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    conn = storage.init_db(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_analysis)")}
    assert {
        "photo_data_json",
        "photos_hash",
        "photo_schema_version",
        "photos_analyzed_at",
        "photo_model",
        "photo_comment",
    } <= columns
    conn.close()


def test_fetch_pending_photos_queues_a_listing_with_photos(conn, photos_home):
    _text_analysed(conn, "with-photos")
    _shoot(photos_home, "with-photos")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert no_photos == []
    assert [p["fb_id"] for p in pending] == ["with-photos"]
    assert pending[0]["pending_reason"] == "new"
    assert pending[0]["photos_hash"] == photos.photos_hash("with-photos")


def test_fetch_pending_photos_skips_rejected_listings(conn, photos_home):
    _text_analysed(conn, "no-pets", pets={"v": "not_allowed", "conf": "high", "ev": ""})
    _shoot(photos_home, "no-pets")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert pending == []
    assert no_photos == []


def _decide(conn, fb_id: str, status: str, reason: str = ""):
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, decided_at) VALUES (?, ?, ?, ?)",
        (fb_id, status, reason, "2026-08-25T10:00:00Z"),
    )


def test_fetch_pending_photos_skips_a_hand_rejected_listing(conn, photos_home):
    # The point of the review-first workflow: once the user has said no, not
    # a single image is worth paying for.
    _text_analysed(conn, "user-said-no")
    _shoot(photos_home, "user-said-no")
    _decide(conn, "user-said-no", "rejected", "уже сдана")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert pending == []
    assert no_photos == []


def test_fetch_pending_photos_keeps_what_the_user_kept(conn, photos_home):
    # A user verdict beats the rules in both directions, as in fetch_enriched:
    # a listing the rules reject is back in the queue once the user keeps it.
    forbidden = {"v": "not_allowed", "conf": "high", "ev": ""}
    _text_analysed(conn, "user-said-yes", pets=forbidden)
    _shoot(photos_home, "user-said-yes")
    _decide(conn, "user-said-yes", "active", "договорился про собаку")

    pending, _ = storage.fetch_pending_photos(conn, _registry())
    assert [p["fb_id"] for p in pending] == ["user-said-yes"]


def test_fetch_pending_photos_queues_a_shortlisted_listing(conn, photos_home):
    forbidden = {"v": "not_allowed", "conf": "high", "ev": ""}
    _text_analysed(conn, "short", pets=forbidden)
    _shoot(photos_home, "short")
    _decide(conn, "short", "shortlist")

    pending, _ = storage.fetch_pending_photos(conn, _registry())
    assert [p["fb_id"] for p in pending] == ["short"]


def test_fetch_pending_photos_reports_listings_without_photos(conn, photos_home):
    _text_analysed(conn, "bare")
    (photos_home / "empty-dir").mkdir(parents=True)
    _text_analysed(conn, "empty-dir")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert pending == []
    assert {row["fb_id"] for row in no_photos} == {"bare", "empty-dir"}


def _photo_pass(conn, fb_id: str, *, version: int = 1):
    conn.execute(
        """
        UPDATE ai_analysis
           SET photo_data_json = '{}', photos_hash = ?,
               photo_schema_version = ?, photos_analyzed_at = ?
         WHERE fb_id = ?
        """,
        (photos.photos_hash(fb_id), version, "2026-08-25T12:00:00Z", fb_id),
    )
    conn.commit()


def test_fetch_pending_photos_is_idempotent_after_a_pass(conn, photos_home):
    _text_analysed(conn, "done")
    _shoot(photos_home, "done")
    _photo_pass(conn, "done")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert pending == []
    assert no_photos == []


def test_fetch_pending_photos_requeues_a_changed_photo_set(conn, photos_home):
    _text_analysed(conn, "reshot")
    _shoot(photos_home, "reshot")
    _photo_pass(conn, "reshot")
    _shoot(photos_home, "reshot", count=5, body=b"another jpeg")

    pending, _ = storage.fetch_pending_photos(conn, _registry())
    assert [p["pending_reason"] for p in pending] == ["changed"]


def test_fetch_pending_photos_requeues_on_a_newer_photo_analysis_version(
    conn, photos_home
):
    _text_analysed(conn, "older")
    _shoot(photos_home, "older")
    _photo_pass(conn, "older", version=1)

    pending, _ = storage.fetch_pending_photos(
        conn, _registry(version=2, added_in=2), photo_analysis_version=2
    )
    assert [p["pending_reason"] for p in pending] == ["schema"]
    assert pending[0]["analysed_photo_version"] == 1


def test_fetch_pending_photos_ignores_unanalysed_listings(conn, photos_home):
    storage.upsert_listing(conn, _make_listing(fb_id="never-read"))
    _shoot(photos_home, "never-read")

    pending, no_photos = storage.fetch_pending_photos(conn, _registry())
    assert pending == []
    assert no_photos == []


# --- photo hashes ---------------------------------------------------------


def test_save_and_read_photo_hashes(conn):
    storage.upsert_listing(conn, _make_listing())
    written = storage.save_photo_hashes(
        conn,
        "1234567890",
        [(0, "md0", "dh0"), (1, "md1", "dh1")],
    )
    assert written == 2

    grouped = storage.photo_hashes_by_listing(conn)
    assert grouped == {
        "1234567890": [
            {"position": 0, "md5": "md0", "dhash": "dh0"},
            {"position": 1, "md5": "md1", "dhash": "dh1"},
        ]
    }


def test_save_photo_hashes_replaces_previous_rows(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.save_photo_hashes(
        conn, "1234567890", [(0, "md0", "dh0"), (1, "md1", "dh1")]
    )
    storage.save_photo_hashes(conn, "1234567890", [(0, "new", "newdh")])

    assert storage.photo_hashes_by_listing(conn) == {
        "1234567890": [{"position": 0, "md5": "new", "dhash": "newdh"}]
    }


def test_empty_entries_erase_photo_hashes(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.save_photo_hashes(conn, "1234567890", [(0, "md0", "dh0")])

    assert storage.save_photo_hashes(conn, "1234567890", []) == 0
    assert storage.photo_hashes_by_listing(conn) == {}


def test_delete_photo_hashes_leaves_other_listings(conn):
    storage.upsert_listing(conn, _make_listing())
    storage.upsert_listing(conn, _make_listing(fb_id="222"))
    storage.save_photo_hashes(conn, "1234567890", [(0, "a", "b")])
    storage.save_photo_hashes(conn, "222", [(0, "c", "d")])

    storage.delete_photo_hashes(conn, "1234567890")

    assert set(storage.photo_hashes_by_listing(conn)) == {"222"}


def test_init_db_creates_photo_hashes_on_an_existing_db(tmp_path: Path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute(storage._SCHEMA)
    old.commit()
    old.close()

    conn = storage.init_db(path)
    try:
        storage.save_photo_hashes(conn, "1", [(0, "md", "dh")])
        assert "1" in storage.photo_hashes_by_listing(conn)
    finally:
        conn.close()


def test_listings_missing_photo_hashes_skips_hashed_rows(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="111"))
    storage.upsert_listing(conn, _make_listing(fb_id="222"))
    storage.save_photo_hashes(conn, "111", [(0, "md", "dh")])

    missing = storage.listings_missing_photo_hashes(conn)

    assert [row["fb_id"] for row in missing] == ["222"]


def test_listings_missing_photo_hashes_needs_raw_json(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="333", raw_json=None))
    storage.upsert_listing(conn, _make_listing(fb_id="444"))

    missing = storage.listings_missing_photo_hashes(conn)

    assert [row["fb_id"] for row in missing] == ["444"]


def test_listings_missing_photo_hashes_returns_oldest_first(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="new"), now="2026-08-20T00:00:00Z")
    storage.upsert_listing(conn, _make_listing(fb_id="old"), now="2026-08-01T00:00:00Z")

    missing = storage.listings_missing_photo_hashes(conn)

    assert [row["fb_id"] for row in missing] == ["old", "new"]


def _shots(*specs: tuple[str, str]) -> list[tuple[int, str, str]]:
    """``(md5, dhash)`` pairs numbered by their position in the gallery."""
    return [(index, md5, dh) for index, (md5, dh) in enumerate(specs)]


def _photographed(conn, fb_id: str, body: str, shots, price: int = 16000):
    """A listing whose first three shots hashed to ``shots``."""
    storage.upsert_listing(conn, _with_body(fb_id, body, price=price))
    storage.save_photo_hashes(conn, fb_id, shots)


def test_two_ads_sharing_two_shots_collapse(conn):
    # Different text, different price band, no AI analysis at all — the
    # pictures are the only thing these two have in common.
    _photographed(conn, "agent-a", _AD, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "agent-b", _AD_B, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m9", "d9"),
    ), price=19000)

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "photos"


def test_one_shared_shot_is_not_enough(conn):
    # A single common picture is the pool, the lobby or the agency logo.
    _photographed(conn, "flat-a", _AD, _shots(
        ("pool", "poold"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "flat-b", _AD_B, _shots(
        ("pool", "poold"), ("m8", "d8"), ("m9", "d9"),
    ), price=19000)

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_a_recompressed_repost_matches_on_dhash(conn):
    # The pictures went through another phone: every byte differs, the
    # perceptual hash does not.
    _photographed(conn, "orig", _AD, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "resaved", _AD_B, _shots(
        ("x1", "d1"), ("x2", "d2"), ("x3", "dz"),
    ), price=19000)

    collapsed = storage.dedupe_listings(conn, dry_run=True)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "photos"


def test_a_hash_on_every_listing_glues_nothing(conn):
    # Five ads carrying the same two pictures: that is a splash screen, not
    # one flat listed five times.
    shots = _shots(("splash", "splashd"), ("logo", "logod"))
    for index in range(5):
        _photographed(conn, f"ad-{index}", f"{_AD} offer number {index}",
                      shots, price=16000 + index * 1000)

    # ``fuzzy=False`` so nothing but the photo signal can speak here: the
    # five bodies are variations of one text and would match on that.
    assert storage.dedupe_listings(conn, dry_run=True, fuzzy=False) == []


def test_matching_photos_lose_to_a_different_floor(conn):
    # Agencies reuse one photo set across identical units in a stack; the
    # named floor is what tells them apart.
    _photographed(conn, "floor-2", _AD + " Building B, 2nd Floor", _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "floor-1", _AD_B + " Building B, 1st Floor", _shots(
        ("m1", "d1"), ("m2", "d2"), ("m9", "d9"),
    ), price=19000)

    assert storage.dedupe_listings(conn, dry_run=True) == []


def test_photos_do_not_chain_two_floors_together(conn):
    # Same trap on the photo side: an agency reuses shots across a stack,
    # and the ad that names no floor is the bridge between the floors.
    _photographed(conn, "floor-2", _AD + " Building B, 2nd Floor", _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "silent", _AD_C, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m4", "d4"),
    ))
    _photographed(conn, "floor-8", _AD_B + " Building B, 8th Floor", _shots(
        ("m1", "d1"), ("m4", "d4"), ("m5", "d5"),
    ))

    collapsed = storage.dedupe_listings(conn, dry_run=True, fuzzy=False)
    assert len(collapsed) == 1
    group = {collapsed[0]["canonical"], *collapsed[0]["removed"]}
    assert "silent" in group
    assert not {"floor-2", "floor-8"} <= group


def test_the_photo_signal_survives_exact_only(conn):
    # It reads no AI field, so it is not a near-duplicate rule.
    _photographed(conn, "agent-a", _AD, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "agent-b", _AD_B, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m9", "d9"),
    ), price=19000)

    collapsed = storage.dedupe_listings(conn, dry_run=True, fuzzy=False)
    assert len(collapsed) == 1
    assert collapsed[0]["reason"] == "photos"


def test_dedupe_forgets_the_hashes_of_the_collapsed_copy(conn):
    _photographed(conn, "keeper", _AD, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "merged", _AD_B, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m9", "d9"),
    ), price=19000)

    collapsed = storage.dedupe_listings(conn)
    gone = collapsed[0]["removed"][0]

    assert gone not in storage.photo_hashes_by_listing(conn)
    assert collapsed[0]["canonical"] in storage.photo_hashes_by_listing(conn)


def test_a_photo_collapsed_repost_is_remembered(conn):
    # The point of the whole signal: the merged copy must not come back on
    # the next sweep, and ``duplicates`` is what stops it.
    _photographed(conn, "keeper", _AD, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m3", "d3"),
    ))
    _photographed(conn, "repost", _AD_B, _shots(
        ("m1", "d1"), ("m2", "d2"), ("m9", "d9"),
    ), price=19000)

    collapsed = storage.dedupe_listings(conn)
    gone = collapsed[0]["removed"][0]

    assert gone in storage.duplicate_fb_ids(conn)
    assert gone not in storage.known_fb_ids(conn)
