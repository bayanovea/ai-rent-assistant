"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from fb_rent import (
    ai_schema,
    buildings,
    config,
    decisions,
    photos,
    rules,
    storage,
)


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


def test_export_html_hides_the_help_behind_two_buttons(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    # Хоткеи и легенда занимали три строки над таблицей и читались дважды за
    # всю жизнь файла. Тест сторожит узлы, по которым скрипт открывает их:
    # без data-pop кнопка перестала бы открывать что-либо молча.
    assert 'class="help" data-pop="keys"' in text
    assert 'class="help" data-pop="legend"' in text
    assert "function toggleHelp(name)" in text
    # Счётчик строк живёт на вкладках, отдельного заголовка над таблицей нет.
    assert "<h1" not in text


def test_export_html_ships_the_preset_row(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    # Пресеты рисуются из PRESETS в контейнер — по кнопке на запись, и
    # включён всегда не больше одного.
    assert 'id="presets"' in text
    assert 'key: "candidates"' in text
    assert 'key: "kathu2"' in text
    assert 'bedrooms: { kind: "range", min: 2, max: 2 }' in text
    assert 'area: areaFilter("Kathu")' in text


def test_export_html_opens_the_cursor_row_in_a_tab(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    # Строка открывается в соседней вкладке и курсор при этом не уезжает —
    # иначе вернувшись из объявления вы решаете уже про соседа.
    assert 'if (key === "o") {' in text
    assert 'window.open(row.url, "_blank", "noopener")' in text


def test_export_html_queues_dedupe_by_a_key(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    # Единственная команда блока, которая не про строку: клавиша работает без
    # курсора, а флаг живёт со штампом сборки — иначе очередь не сбросится.
    assert 'if (key === "d") { ev.preventDefault(); toggleDedupe(); return; }' in text
    assert 'lines.push("fb-rent dedupe")' in text
    assert 'const GENERATED_AT = "__GENERATED_AT__"' not in text
    assert "const GENERATED_AT = " in text


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
    # Свой отказ надо уметь отделить от машинного: их на вкладке в полсотни
    # раз больше. Значение короткое — панель фильтров даёт по нему чипы.
    assert row["cells"]["decided_by"]["t"] == "вы"


def test_the_decider_is_a_column_bound_to_the_rejected_tabs(conn, tmp_path: Path):
    # Кто вынес вердикт, раньше было суффиксом «(вы)» в хвосте «Причины»:
    # мимо поиска и мимо фильтров. Теперь это колонка — и она живёт только
    # там, где среди своих отказов лежат машинные.
    registry = ai_schema.load_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="auto"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="auto",
            schema_version=registry.schema_version,
            data_json={"pets": {"v": "not_allowed", "conf": "high", "ev": ""}},
        ),
    )
    storage.upsert_listing(conn, _make_listing(fb_id="untouched"))

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")

    rows = {r["fb_id"]: r for r in _inline_payload(text, "DATA")}
    assert rows["auto"]["cells"]["decided_by"]["t"] == "AI"
    # Никто не решал — пустая ячейка, а не выдуманный автор.
    assert rows["untouched"]["cells"]["decided_by"]["t"] == ""

    columns = {c["key"]: c for c in _inline_payload(text, "COLUMNS")}
    assert columns["decided_by"]["tabs"] == ["rejected", "all"]
    assert columns["reason"]["tabs"] == ["rejected", "all"]
    # На Longlist и Shortlist решали только вы — там колонка не нужна.
    assert "longlist" not in columns["decided_by"]["tabs"]


def test_a_marked_listing_leaves_the_active_queue(conn, tmp_path: Path):
    # Active — очередь на разбор. Оценка 4 уводит строку в longlist, и
    # строка с оценкой в очереди уже не лежит.
    storage.upsert_listing(conn, _make_listing(fb_id="middling"))
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, rating, decided_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("middling", decisions.status_for_rating(4), None, 4.0,
         "2026-08-25T12:00:00Z"),
    )
    conn.commit()

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")

    row = _inline_payload(text, "DATA")[0]
    assert row["status"] == "longlist"
    assert row["cells"]["rating"]["n"] == 4.0
    # Вкладка и полоса на строке — иначе средний вариант нечем отличить
    # от лучшего.
    assert 'data-tab="longlist"' in text
    assert "row-longlist" in text


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


def test_the_verdict_column_is_a_tag_with_the_sentence_behind_it(conn, tmp_path: Path):
    # The AI's sentence was 240px of clipped prose whose first two words were
    # the same on most of the table. The column shows the tag, sorts by
    # urgency, and keeps the sentence as the tooltip.
    registry = ai_schema.load_registry()
    storage.upsert_listing(conn, _make_listing(fb_id="v1"))
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="v1",
            schema_version=registry.schema_version,
            verdict="Уточнить — дом в Катху, но про животных молчат",
        ),
    )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")
    row = _inline_payload(text, "DATA")[0]

    assert row["cells"]["verdict"]["t"] == "спросить"
    assert row["cells"]["verdict"]["tag"] == "ask"
    assert row["cells"]["verdict"]["ev"].startswith("Уточнить —")
    assert row["cells"]["verdict"]["n"] == rules.verdict_order("ask")
    # Short enough for chips: the filter panel enumerates values under 30 chars.
    assert len(row["cells"]["verdict"]["t"]) < 30

    columns = {c["key"]: c for c in _inline_payload(text, "COLUMNS")}
    assert columns["verdict"]["kind"] == "tag"


def test_the_district_rating_is_a_column_so_the_preset_can_filter_on_it(
    conn, tmp_path: Path
):
    # Free-text `Район` has hundreds of values and no order; "хороший район"
    # is only expressible as a filter once the 1-5 scale is a column.
    registry = ai_schema.load_registry()
    for fb_id, area in (("kathu", "Kathu"), ("chalong", "Chalong"), ("nowhere", "")):
        storage.upsert_listing(conn, _make_listing(fb_id=fb_id, district=None))
        storage.upsert_ai_analysis(
            conn,
            _make_analysis(
                fb_id=fb_id,
                schema_version=registry.schema_version,
                data_json={"area": {"v": area, "conf": "high", "ev": ""}},
            ),
        )

    out = tmp_path / "rentals.html"
    storage.export_html(conn, out, registry)
    text = out.read_text(encoding="utf-8")
    rows = {r["fb_id"]: r for r in _inline_payload(text, "DATA")}

    assert rows["kathu"]["cells"]["area_rating"]["n"] == 5.0
    assert rows["chalong"]["cells"]["area_rating"]["n"] == 3.0
    assert rows["nowhere"]["cells"]["area_rating"]["t"] == ""
    # It is machinery, not reading matter, so it ships folded away.
    columns = {c["key"]: c for c in _inline_payload(text, "COLUMNS")}
    assert columns["area_rating"]["hidden"] is True
    # The preset is an ordinary set of filters over real columns.
    assert 'verdict: { kind: "enum"' in text
    assert 'area_rating: { kind: "range", min: 4' in text


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


# --- buildings: the mirror, the policy map and the research queue ---------


def _building(**overrides):
    base = {
        "name": "The Base Central Phuket",
        "aliases": ["Base Central"],
        "area": "Phuket Town",
        "policy": "not_allowed",
        "status": "confirmed",
        "confidence": "high",
        "note": "УК: no pets of any kind.",
        "sources": [
            {
                "url": "https://example.com/rules",
                "title": "House Rules",
                "quote": "Pets are not allowed in the building.",
            }
        ],
        "researched_at": "2026-08-27T10:00:00Z",
        "researched_by": "claude-opus-5 (claude-code)",
        "confirmed_at": "2026-08-27T10:12:00Z",
    }
    base.update(overrides)
    return {buildings.building_key(base["name"]): base}


def _in_building(conn, fb_id, project, *, pets="unclear", area="Phuket Town",
                 district="Phuket Town", price=22000, last_seen="2026-08-25T10:00:00Z"):
    """One analysed listing advertising a flat in ``project``."""
    storage.upsert_listing(
        conn,
        _make_listing(fb_id=fb_id, price_thb=price, district=district),
        now=last_seen,
    )
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id=fb_id,
            data_json={
                "project_name": {"v": project},
                "area": {"v": area},
                "pets": {"v": pets},
            },
        ),
    )


def test_init_db_creates_the_buildings_table_idempotently(tmp_path: Path):
    db_path = tmp_path / "rentals.db"
    storage.init_db(db_path).close()
    conn = storage.init_db(db_path)  # second call must not raise
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "buildings" in tables
    assert conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0] == 0
    conn.close()


def test_sync_buildings_inserts_the_whole_entry(conn):
    assert storage.sync_buildings_to_db(conn, _building()) == 1
    row = conn.execute("SELECT * FROM buildings").fetchone()
    assert row["key"] == "the-base-central-phuket"
    assert row["name"] == "The Base Central Phuket"
    assert row["policy"] == "not_allowed"
    assert row["status"] == "confirmed"
    assert row["confidence"] == "high"
    assert json.loads(row["aliases_json"]) == ["Base Central"]
    assert json.loads(row["sources_json"])[0]["quote"].startswith("Pets are not")
    assert row["confirmed_at"] == "2026-08-27T10:12:00Z"


def test_sync_buildings_updates_a_changed_entry(conn):
    storage.sync_buildings_to_db(conn, _building())
    storage.sync_buildings_to_db(conn, _building(policy="negotiable", note="Спросить УК"))
    rows = conn.execute("SELECT policy, note FROM buildings").fetchall()
    assert len(rows) == 1
    assert rows[0]["policy"] == "negotiable"
    assert rows[0]["note"] == "Спросить УК"


def test_sync_buildings_deletes_what_the_file_no_longer_has(conn):
    storage.sync_buildings_to_db(conn, _building())
    assert storage.sync_buildings_to_db(conn, {}) == 0
    assert conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0] == 0


def test_sync_buildings_reads_the_file_when_no_store_is_passed(conn, tmp_path: Path):
    path = tmp_path / "buildings.json"
    path.write_text(json.dumps(_building()), encoding="utf-8")
    assert storage.sync_buildings_to_db(conn, path=path) == 1
    assert conn.execute("SELECT key FROM buildings").fetchone()["key"] == (
        "the-base-central-phuket"
    )


@pytest.mark.parametrize(
    "broken",
    [{"policy": "maybe"}, {"status": "vetted"}, {"policy": None}, {"status": None}],
)
def test_sync_buildings_rejects_an_invalid_entry(conn, broken):
    with pytest.raises(buildings.BuildingError):
        storage.sync_buildings_to_db(conn, _building(**broken))
    # Nothing half-written: the guard runs before the table is touched.
    assert conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0] == 0


def test_building_policies_maps_every_spelling(conn):
    storage.sync_buildings_to_db(conn, _building())
    policies = storage.building_policies(conn)
    assert policies["the-base-central-phuket"] == "not_allowed"
    assert policies[buildings.building_key("Base Central")] == "not_allowed"


def test_building_policies_ignores_a_proposal(conn):
    storage.sync_buildings_to_db(conn, _building(status="proposed"))
    assert storage.building_policies(conn) == {}


def test_building_policies_ignores_a_confirmed_unknown(conn):
    # Research that found nothing looks exactly like an unresearched building.
    storage.sync_buildings_to_db(conn, _building(policy="unknown"))
    assert storage.building_policies(conn) == {}


def test_building_policies_keeps_negotiable_and_allowed(conn):
    storage.sync_buildings_to_db(
        conn,
        {
            **_building(policy="negotiable"),
            **_building(name="Zcape III", aliases=[], policy="allowed"),
        },
    )
    assert storage.building_policies(conn) == {
        "the-base-central-phuket": "negotiable",
        buildings.building_key("Base Central"): "negotiable",
        "zcape-iii": "allowed",
    }


def test_building_policies_on_an_empty_table(conn):
    assert storage.building_policies(conn) == {}


def test_pending_buildings_groups_listings_by_building(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "a2", "the base central phuket")
    _in_building(conn, "b1", "Zcape III", area="Kathu", district="Kathu")

    queue = storage.fetch_pending_buildings(conn, {})

    assert [(e["key"], e["listings"]) for e in queue] == [
        ("the-base-central-phuket", 2),
        ("zcape-iii", 1),
    ]
    assert queue[0]["area"] == "Phuket Town"
    assert queue[0]["fb_ids"] == ["a1", "a2"]


def test_pending_buildings_names_the_building_the_way_most_ads_do(conn):
    _in_building(conn, "a1", "the base central phuket")
    _in_building(conn, "a2", "The Base Central Phuket")
    _in_building(conn, "a3", "The Base Central Phuket")

    queue = storage.fetch_pending_buildings(conn, {})
    assert queue[0]["name"] == "The Base Central Phuket"


def test_pending_buildings_reports_what_the_ads_claimed_about_pets(conn):
    # A contradiction the researcher should see coming.
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a2", "The Base Central Phuket", pets="unclear")
    _in_building(conn, "a3", "The Base Central Phuket", pets="unclear")

    assert storage.fetch_pending_buildings(conn, {})[0]["pets"] == [
        "allowed",
        "unclear",
    ]


def test_pending_buildings_samples_at_most_five_ids(conn):
    for index in range(7):
        _in_building(conn, f"a{index}", "The Base Central Phuket")
    entry = storage.fetch_pending_buildings(conn, {})[0]
    assert entry["listings"] == 7
    assert len(entry["fb_ids"]) == storage.QUEUE_SAMPLE_IDS


def test_pending_buildings_counts_only_listings_still_worth_keeping(conn):
    _in_building(conn, "keep", "The Base Central Phuket")
    # Auto-rejected by the ad's own words.
    _in_building(conn, "ai-out", "The Base Central Phuket", pets="not_allowed")
    # Rejected by hand.
    _in_building(conn, "user-out", "The Base Central Phuket")
    decisions.sync_to_db(
        conn, {"user-out": {"status": "rejected", "reason": "Не тот дом"}}
    )

    queue = storage.fetch_pending_buildings(conn, {})
    assert [(e["key"], e["listings"]) for e in queue] == [
        ("the-base-central-phuket", 1)
    ]
    assert queue[0]["fb_ids"] == ["keep"]


def test_pending_buildings_keeps_a_hand_kept_listing_the_rules_reject(conn):
    _in_building(conn, "kept", "The Base Central Phuket", pets="not_allowed")
    decisions.sync_to_db(conn, {"kept": {"status": "shortlist"}})

    assert storage.fetch_pending_buildings(conn, {})[0]["listings"] == 1


def test_pending_buildings_skips_a_listing_naming_no_building(conn):
    _in_building(conn, "a1", "")
    _in_building(conn, "a2", "   ")
    _in_building(conn, "a3", "—")
    assert storage.fetch_pending_buildings(conn, {}) == []


def test_pending_buildings_skips_an_unanalysed_listing(conn):
    storage.upsert_listing(conn, _make_listing(fb_id="raw"))
    assert storage.fetch_pending_buildings(conn, {}) == []


def test_pending_buildings_skips_what_is_already_researched(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "b1", "Zcape III")

    queue = storage.fetch_pending_buildings(conn, _building())
    assert [e["key"] for e in queue] == ["zcape-iii"]


def test_pending_buildings_refresh_returns_the_researched_ones(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    queue = storage.fetch_pending_buildings(conn, _building(), refresh=True)
    assert [e["key"] for e in queue] == ["the-base-central-phuket"]


def test_pending_buildings_collapses_an_alias_spelling(conn):
    # Two spellings the key alone would never merge; the alias does.
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "a2", "Base Central")

    queue = storage.fetch_pending_buildings(conn, _building(), refresh=True)
    assert [(e["key"], e["listings"]) for e in queue] == [
        ("the-base-central-phuket", 2)
    ]


def test_pending_buildings_does_not_fuzzy_match_an_unknown_spelling(conn):
    _in_building(conn, "a1", "The Base Downtown Phuket")
    queue = storage.fetch_pending_buildings(conn, _building())
    assert [e["key"] for e in queue] == ["the-base-downtown-phuket"]


def test_pending_buildings_falls_back_to_the_listing_district(conn):
    _in_building(conn, "a1", "Zcape III", area="", district="Kathu")
    assert storage.fetch_pending_buildings(conn, {})[0]["area"] == "Kathu"


def test_pending_buildings_loads_the_store_itself(conn, monkeypatch, tmp_path: Path):
    path = tmp_path / "buildings.json"
    path.write_text(json.dumps(_building()), encoding="utf-8")
    monkeypatch.setattr(config, "BUILDINGS_PATH", path)
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "b1", "Zcape III")

    assert [e["key"] for e in storage.fetch_pending_buildings(conn)] == ["zcape-iii"]


# --- buildings: the policy joined into the read funnel ---------------------


def _enriched_by_id(conn):
    return {item["fb_id"]: item for item in storage.fetch_enriched(conn)}


def test_enriched_rejects_every_listing_of_a_confirmed_building(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "a2", "Base Central")  # the alias spelling
    _in_building(conn, "b1", "Zcape III")
    storage.sync_buildings_to_db(conn, _building())

    items = _enriched_by_id(conn)
    for fb_id in ("a1", "a2"):
        assert items[fb_id]["status"] == "rejected"
        assert items[fb_id]["reason"] == rules.REASON_BUILDING_PETS
        assert items[fb_id]["decided_by"] == "ai"
    assert items["b1"]["status"] == "active"
    assert items["b1"]["reason"] is None


def test_enriched_carries_the_building_for_the_export(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    _in_building(conn, "b1", "Zcape III")
    storage.sync_buildings_to_db(conn, _building())

    items = _enriched_by_id(conn)
    assert items["a1"]["building_policy"] == "not_allowed"
    assert items["a1"]["building_status"] == "confirmed"
    assert items["a1"]["building_name"] == "The Base Central Phuket"
    assert items["a1"]["building_sources"] == [
        {
            "url": "https://example.com/rules",
            "title": "House Rules",
            "quote": "Pets are not allowed in the building.",
        }
    ]
    # An unresearched building says nothing rather than saying "unknown".
    assert items["b1"]["building_policy"] is None
    assert items["b1"]["building_status"] is None
    assert items["b1"]["building_name"] is None
    assert items["b1"]["building_sources"] == []


def test_enriched_lets_a_user_decision_outrank_the_building(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.sync_buildings_to_db(conn, _building())
    decisions.sync_to_db(conn, {"a1": {"status": "shortlist", "rating": 5}})

    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "shortlist"
    assert item["decided_by"] == "user"
    # The rule still says what it says — the row just does not obey it.
    assert item["ai_status"] == "rejected"
    assert item["ai_reason"] == rules.REASON_BUILDING_PETS


def test_enriched_keeps_the_ads_own_reason_when_both_apply(conn):
    _in_building(conn, "a1", "The Base Central Phuket", pets="not_allowed")
    storage.sync_buildings_to_db(conn, _building())
    assert _enriched_by_id(conn)["a1"]["reason"] == rules.REASON_PETS


@pytest.mark.parametrize("policy", ["negotiable", "allowed", "unknown"])
def test_enriched_never_rejects_on_a_policy_other_than_not_allowed(conn, policy):
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    storage.sync_buildings_to_db(conn, _building(policy=policy))

    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "active"
    # Visible on the row all the same: `negotiable` is what the user asks about.
    assert item["building_policy"] == policy
    # And the ad's own claim is never overwritten by the research.
    assert item["fields"]["pets"]["v"] == "allowed"


def test_enriched_leaves_a_proposal_inert(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.sync_buildings_to_db(conn, _building(status="proposed"))

    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "active"
    assert item["reason"] is None
    # Still on the row, so the export can show it as the unvetted thing it is.
    assert (item["building_policy"], item["building_status"]) == (
        "not_allowed",
        "proposed",
    )


def test_enriched_building_rejection_needs_good_data_quality(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.upsert_ai_analysis(
        conn,
        _make_analysis(
            fb_id="a1",
            data_quality="sidebar_noise",
            data_json={"project_name": {"v": "The Base Central Phuket"}},
        ),
    )
    storage.sync_buildings_to_db(conn, _building())
    assert _enriched_by_id(conn)["a1"]["status"] == "active"


def test_enriched_scores_nothing_for_a_building_policy(conn):
    # No weight in either direction — the tie-at-100 trap in `CLAUDE.md`.
    _in_building(conn, "a1", "The Base Central Phuket")
    before = _enriched_by_id(conn)["a1"]["score"]
    storage.sync_buildings_to_db(conn, _building(policy="allowed"))
    assert _enriched_by_id(conn)["a1"]["score"] == before


def test_enriched_on_an_empty_buildings_table_is_unchanged(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "active"
    assert item["building_policy"] is None


def test_enriched_survives_a_database_without_the_buildings_table(conn):
    _in_building(conn, "a1", "The Base Central Phuket")
    conn.execute("DROP TABLE buildings")
    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "active"
    assert item["building_policy"] is None


def test_enriched_rejects_a_listing_scraped_after_the_research(conn):
    # The mirror is read on every `fetch_enriched`, so a building confirmed
    # today rejects the units that turn up tomorrow with no pass re-running.
    storage.sync_buildings_to_db(conn, _building())
    _in_building(conn, "later", "The Base Central Phuket")

    item = _enriched_by_id(conn)["later"]
    assert item["status"] == "rejected"
    assert item["reason"] == rules.REASON_BUILDING_PETS


def test_enriched_keeps_the_ad_that_promises_pets_in_a_forbidding_building(conn):
    # The contradiction is the point: an ad promising "pet friendly" in a
    # building that forbids animals stays readable as the disagreement it is —
    # and stays active, because it is the more specific claim about this flat.
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    storage.sync_buildings_to_db(conn, _building())

    item = _enriched_by_id(conn)["a1"]
    assert item["status"] == "active"
    assert item["reason"] is None
    assert item["fields"]["pets"]["v"] == "allowed"
    stored = conn.execute(
        "SELECT data_json FROM ai_analysis WHERE fb_id = 'a1'"
    ).fetchone()["data_json"]
    assert json.loads(stored)["pets"]["v"] == "allowed"


# --- fetch_pets_conflicts ---------------------------------------------------


def test_conflicts_collects_a_building_its_ads_keep_contradicting(conn):
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a2", "Base Central", pets="allowed")
    _in_building(conn, "a3", "The Base Central Phuket", pets="unclear")
    store = _building()

    conflicts = storage.fetch_pets_conflicts(conn, store)

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["key"] == buildings.building_key("The Base Central Phuket")
    assert conflict["policy"] == "not_allowed"
    # The alias spelling counts for the same building, and the ad that said
    # nothing is not a signal.
    assert {row["fb_id"] for row in conflict["listings"]} == {"a1", "a2"}


def test_conflicts_ignores_a_single_ad(conn):
    # One landlord with a permission is noise; the pile is the claim.
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")

    assert storage.fetch_pets_conflicts(conn, _building()) == []
    assert len(storage.fetch_pets_conflicts(conn, _building(), min_signals=1)) == 1


def test_conflicts_only_look_at_buildings_researched_as_forbidding(conn):
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a2", "The Base Central Phuket", pets="allowed")

    for policy in ("negotiable", "allowed", "unknown"):
        assert storage.fetch_pets_conflicts(conn, _building(policy=policy)) == []


def test_conflicts_keep_a_proposal(conn):
    # A proposal rejects nothing, but a contradiction is exactly what should be
    # seen before it is confirmed.
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a2", "The Base Central Phuket", pets="allowed")

    conflicts = storage.fetch_pets_conflicts(conn, _building(status="proposed"))
    assert [c["status"] for c in conflicts] == ["proposed"]


def test_conflicts_drop_hand_rejected_and_unreadable_rows(conn):
    _in_building(conn, "a1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a2", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "a3", "The Base Central Phuket", pets="allowed")
    # The user threw this one out: it is not worth arguing about any more.
    conn.execute(
        "INSERT INTO decisions (fb_id, status, reason, decided_at) VALUES (?,?,?,?)",
        ("a1", "rejected", "не нравится", "2026-08-26T10:00:00Z"),
    )
    # This one was read off the sidebar, so its "pets allowed" is not evidence.
    conn.execute(
        "UPDATE ai_analysis SET data_quality = 'sidebar_noise' WHERE fb_id = 'a2'"
    )
    conn.commit()

    assert storage.fetch_pets_conflicts(conn, _building()) == []
    assert len(
        storage.fetch_pets_conflicts(conn, _building(), min_signals=1)[0]["listings"]
    ) == 1


@pytest.mark.parametrize("policy", sorted(buildings.POLICIES))
@pytest.mark.parametrize("status", sorted(buildings.STATUSES))
def test_enriched_score_never_moves_with_the_building(conn, policy, status):
    # Nothing the research says is worth a point in either direction: a
    # rejection needs no weight and a reward would lift the rows already
    # easiest to rank. See the tie-at-100 trap in `CLAUDE.md`.
    _in_building(conn, "a1", "The Base Central Phuket")
    before = _enriched_by_id(conn)["a1"]["score"]
    storage.sync_buildings_to_db(conn, _building(policy=policy, status=status))
    assert _enriched_by_id(conn)["a1"]["score"] == before


# --- buildings in the table: the badge on «Проект/посёлок» ------------------


def _export_rows(conn, tmp_path: Path):
    out = tmp_path / "rentals.html"
    storage.export_html(conn, out)
    text = out.read_text(encoding="utf-8")
    return {r["fb_id"]: r for r in _inline_payload(text, "DATA")}, text


def test_export_html_badges_a_confirmed_building_on_the_project_cell(
    conn, tmp_path: Path
):
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.sync_buildings_to_db(conn, _building())

    rows, text = _export_rows(conn, tmp_path)
    cell = rows["a1"]["cells"]["project_name"]
    assert cell["t"] == "The Base Central Phuket"
    assert (cell["bp"], cell["bs"], cell["bt"]) == ("not_allowed", "confirmed", "нельзя")
    assert cell["bc"] == rules.CLASS_BAD
    # The rejection can be checked from the table: quote and URL are both there.
    assert "Pets are not allowed in the building." in cell["bev"]
    assert "https://example.com/rules" in cell["bev"]
    assert "подтверждено" in cell["bev"]
    # It stays a badge, not a column of its own.
    columns = _inline_payload(text, "COLUMNS")
    assert not [c for c in columns if c["key"].startswith("building")]
    # And the class the page hangs the colour on is styled.
    assert ".bpol.c-bad" in text


@pytest.mark.parametrize(
    "policy,cls,label",
    [
        ("not_allowed", rules.CLASS_BAD, "нельзя"),
        ("negotiable", rules.CLASS_WARN, "по договорённости"),
        ("allowed", rules.CLASS_GOOD, "можно"),
        ("unknown", rules.CLASS_NONE, "не найдено"),
    ],
)
def test_export_html_colours_the_badge_by_policy(
    conn, tmp_path: Path, policy, cls, label
):
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.sync_buildings_to_db(conn, _building(policy=policy))

    cell = _export_rows(conn, tmp_path)[0]["a1"]["cells"]["project_name"]
    assert (cell["bp"], cell["bc"], cell["bt"]) == (policy, cls, label)


def test_export_html_renders_a_proposal_in_its_own_class(conn, tmp_path: Path):
    # A proposal rejects nothing, so it must not look like a decision.
    _in_building(conn, "a1", "The Base Central Phuket")
    storage.sync_buildings_to_db(conn, _building(status="proposed"))

    rows, text = _export_rows(conn, tmp_path)
    cell = rows["a1"]["cells"]["project_name"]
    assert cell["bs"] == "proposed"
    assert "предложение, не подтверждено" in cell["bev"]
    assert rows["a1"]["status"] == "active"
    assert ".bpol.b-proposed" in text


def test_export_html_badges_an_alias_spelling_too(conn, tmp_path: Path):
    _in_building(conn, "a1", "Base Central")
    storage.sync_buildings_to_db(conn, _building())

    cell = _export_rows(conn, tmp_path)[0]["a1"]["cells"]["project_name"]
    # The cell keeps the ad's own spelling; the tooltip names the building.
    assert cell["t"] == "Base Central"
    assert cell["bp"] == "not_allowed"
    assert "The Base Central Phuket" in cell["bev"]


def test_export_html_has_no_badge_when_nothing_is_researched(conn, tmp_path: Path):
    _in_building(conn, "a1", "The Base Central Phuket")

    rows, text = _export_rows(conn, tmp_path)
    cell = rows["a1"]["cells"]["project_name"]
    assert cell["t"] == "The Base Central Phuket"
    for key in ("bp", "bs", "bc", "bt", "bev"):
        assert key not in cell
    # Nothing else on the row picked the keys up either.
    assert '"bp":' not in json.dumps(rows, ensure_ascii=False)


def test_dedupe_carries_earlier_reposts_over_to_the_keeper(conn):
    """A repost of the copy being dropped moves onto the surviving row."""
    for fb_id, seen in (("first", "2026-08-01"), ("second", "2026-08-02")):
        listing = _with_body(fb_id, _AD)
        listing["url"] = f"https://www.facebook.com/marketplace/item/{fb_id}/"
        conn.execute(
            "INSERT INTO listings (fb_id, url, title, price_thb, content_key,"
            " raw_json, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (fb_id, listing["url"], "t", 16000, storage.content_key(listing),
             json.dumps(listing["raw_json"]), seen, "2026-08-25"),
        )
    # "old" was already recorded as a repost of the copy dedupe will drop.
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key,"
        " noticed_at, url) VALUES (?,?,?,?,?)",
        ("old", "second", "k", "2026-08-02", None),
    )
    conn.commit()

    storage.dedupe_listings(conn)

    grouped = storage.duplicates_by_canonical(conn)
    assert {row["fb_id"] for row in grouped["first"]} == {"second", "old"}
    assert "second" not in grouped


def test_migrate_repoints_reposts_left_behind_by_an_older_dedupe(conn):
    """A chain of dead canonicals is followed to the row that survived."""
    listing = _with_body("keeper", _AD)
    storage.upsert_listing(conn, listing)
    for fb_id, canonical in (("gone", "keeper"), ("orphan", "gone")):
        conn.execute(
            "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key,"
            " noticed_at, url) VALUES (?,?,?,?,?)",
            (fb_id, canonical, "k", "2026-08-02", None),
        )
    conn.commit()

    assert storage._flatten_duplicate_chains(conn) == 1
    conn.commit()

    grouped = storage.duplicates_by_canonical(conn)
    assert {row["fb_id"] for row in grouped["keeper"]} == {"gone", "orphan"}
    # Idempotent: a second pass has nothing left to move.
    assert storage._flatten_duplicate_chains(conn) == 0
