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
        len(storage._BASE_COLUMNS)
        + len(storage._TAIL_COLUMNS)
        + len(registry.fields)
        - len(merged)
    )


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


def test_html_columns_fold_extras_behind_the_toggle():
    registry = ai_schema.load_registry()
    columns = storage.html_columns(registry)
    hidden = [c["key"] for c in columns if c["hidden"]]

    # The five that most often decide whether a viewing is worth it open the
    # folded group.
    assert hidden[:5] == ["security", "deposit", "parking", "aircon", "kitchen"]
    for key in ("pets", "bedrooms", "property_type"):
        assert next(c for c in columns if c["key"] == key)["hidden"] is False

    # Bands are contiguous, and `edge` marks where one gives way to the next.
    sources = [c["src"] for c in columns]
    assert sources[0] == "fb" and sources[-1] == "sys"
    edges = [c["key"] for c in columns if c["edge"]]
    assert edges == ["score", "reason"]


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
        {"canonical": "decided", "removed": ["analysed", "oldest"]}
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
