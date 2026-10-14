"""Tests for the CLI wiring the other test modules cannot reach.

Everything here is about what the commands do *around* the library calls:
which listings end up in a batch, whether the decisions mirror is refreshed
before an export, and how a hand-edited decisions file is reported.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from rent_assistant import (
    ai_batch,
    ai_schema,
    buildings,
    cli,
    config,
    decisions,
    messages as messages_module,
    photos,
    rules,
    storage,
)
from rent_assistant.cli import main


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every rent-assistant path at a temp directory."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(config, "RENT_ASSISTANT_HOME", root)
    monkeypatch.setattr(config, "DB_PATH", root / "rentals.db")
    monkeypatch.setattr(config, "CSV_PATH", root / "rentals.csv")
    monkeypatch.setattr(config, "HTML_PATH", root / "rentals.html")
    monkeypatch.setattr(config, "AI_DIR", root / "ai")
    monkeypatch.setattr(config, "PHOTOS_DIR", root / "photos")
    monkeypatch.setattr(config, "DECISIONS_PATH", root / "decisions.json")
    monkeypatch.setattr(config, "BUILDINGS_PATH", root / "buildings.json")
    monkeypatch.setattr(config, "MESSAGES_SENT_PATH", root / "messages_sent.json")
    return root


def _run(*args):
    return CliRunner().invoke(main, list(args))


def _add_listing(conn, fb_id: str, **overrides):
    listing = {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
        "title": "2 Bed 1 Bath - House",
        "price_thb": 22000,
        "price_raw": "22 000 ฿/month",
        "district": "Kathu, Phuket",
        "description": "House in a village, 2 bedrooms. Dogs allowed.",
        "raw_json": json.dumps({"text_lines": ["Описание", "House in a village."]}),
    }
    listing.update(overrides)
    storage.upsert_listing(conn, listing)
    return listing


def _analyse(conn, registry, fb_id: str):
    ai_batch.apply_results(
        conn,
        registry,
        {
            "schema_version": registry.schema_version,
            "results": [
                {
                    "fb_id": fb_id,
                    "verdict": "View.",
                    "data_quality": "good",
                    "fields": {"pets": {"v": "allowed"}},
                }
            ],
        },
    )


def _batch_ids(home):
    batch = json.loads(
        sorted((home / "ai").glob("batch-*.json"))[-1].read_text(encoding="utf-8")
    )
    return [item["fb_id"] for item in batch["items"]]


# --- ai-batch --ids -----------------------------------------------------


def test_ids_reanalyse_an_already_analysed_listing(home):
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, registry, "1")
    conn.close()

    result = _run("ai-batch", "--ids", "1")

    assert result.exit_code == 0, result.output
    assert _batch_ids(home) == ["1"]


def test_ids_on_a_listing_without_raw_text_says_so(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1", description=None, raw_json=json.dumps({"text_lines": []}))
    conn.close()

    result = _run("ai-batch", "--ids", "1")

    assert result.exit_code == 0, result.output
    assert "scrape --ids" in result.output
    assert "Nothing to analyse" in result.output
    assert list((home / "ai").glob("batch-*.json")) == []


def test_ids_unknown_listing_is_named(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("ai-batch", "--ids", "999")

    assert "Unknown listing(s): 999" in result.output


def test_limit_must_be_positive(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("ai-batch", "--limit", "0")

    assert result.exit_code != 0
    assert "--limit must be a positive number" in result.output


# --- the stale / pending split -------------------------------------------


def _registry_copy(tmp_path, *, schema_version: int, analysis_version: int | None):
    target = tmp_path / "ai_fields.json"
    shutil.copy(config.AI_FIELDS_PATH, target)
    raw = json.loads(target.read_text(encoding="utf-8"))
    base = int(raw["schema_version"])
    for bump in range(base + 1, schema_version + 1):
        raw["fields"].append(
            {
                "key": f"extra_{bump}",
                "label": f"Extra {bump}",
                "group": "amenity",
                "type": "text",
                "added_in": bump,
                "prompt": "…",
            }
        )
    raw["schema_version"] = schema_version
    if analysis_version is not None:
        raw["analysis_version"] = analysis_version
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def test_queue_leaves_rows_stale_without_backfill(home, tmp_path):
    conn = storage.init_db(config.DB_PATH)
    base = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, base, "1")

    path = _registry_copy(tmp_path, schema_version=base.schema_version + 1,
                          analysis_version=base.schema_version)
    pending, stale, _no_raw = cli._queue(conn, ai_schema.load_registry(path), path)
    conn.close()

    assert [row["fb_id"] for row in stale] == ["1"]
    assert pending == []


def test_queue_keeps_an_earlier_backfill_alive_after_a_plain_add(home, tmp_path):
    """`add --backfill` then `add` must not silently cancel the backfill."""
    conn = storage.init_db(config.DB_PATH)
    base = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, base, "1")

    # v2 was added with --backfill (analysis_version caught up), v3 without.
    path = _registry_copy(tmp_path, schema_version=base.schema_version + 2,
                          analysis_version=base.schema_version + 1)
    pending, stale, _no_raw = cli._queue(conn, ai_schema.load_registry(path), path)
    conn.close()

    assert [row["fb_id"] for row in pending] == ["1"]
    assert stale == []


# --- decisions ------------------------------------------------------------


def test_export_reports_a_broken_decisions_file(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()
    config.DECISIONS_PATH.write_text(
        json.dumps({"1": {"status": "maybe"}}), encoding="utf-8"
    )

    result = _run("export")

    assert result.exit_code != 0
    assert "unknown decision status 'maybe'" in result.output
    assert "Traceback" not in result.output


def test_decisions_file_is_group_readable(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    assert _run("reject", "1", "--reason", "too far").exit_code == 0

    assert config.DECISIONS_PATH.stat().st_mode & 0o077 == 0o044


# --- stage (contact / schedule-viewing / mark-viewed / clear-stage) -------


def test_contact_defaults_the_posting_to_the_row_itself(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("contact", "1")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["stage"] == "written"
    assert entry["stage_fb_id"] == "1"


def test_contact_accepts_a_merged_posting(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, url, noticed_at) "
        "VALUES ('2', '1', 'x', 'https://example.com/2', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = _run("contact", "1", "--posting", "2")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["stage_fb_id"] == "2"


def test_contact_refuses_a_posting_outside_the_group(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    conn.close()

    result = _run("contact", "1", "--posting", "2")

    assert result.exit_code != 0
    assert "not one of" in result.output
    assert not config.DECISIONS_PATH.exists()


def test_contact_accepts_a_posting_merged_at_write_time_with_no_own_row(home):
    """A repost caught by content_key at scrape time gets no `listings` row

    of its own (see upsert_listing) — only a `duplicates` entry. It must
    still be a valid --posting target, the same as one dedupe merged later.
    """
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, url, noticed_at) "
        "VALUES ('2', '1', 'x', 'https://example.com/2', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = _run("contact", "1", "--posting", "2")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["stage_fb_id"] == "2"


def test_contact_accepts_several_postings_comma_separated(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    _add_listing(conn, "3")
    for other in ("2", "3"):
        conn.execute(
            "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, url, noticed_at) "
            f"VALUES ('{other}', '1', 'x', 'https://example.com/{other}', '2026-08-01T00:00:00Z')"
        )
    conn.commit()
    conn.close()

    result = _run("contact", "1", "--posting", "2, 3", "--note", "only these two")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["stage_fb_id"] == "2,3"


def test_contact_refuses_if_any_of_several_postings_is_outside_the_group(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    _add_listing(conn, "3")
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, url, noticed_at) "
        "VALUES ('2', '1', 'x', 'https://example.com/2', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = _run("contact", "1", "--posting", "2,3")

    assert result.exit_code != 0
    assert "not one of" in result.output
    assert not config.DECISIONS_PATH.exists()


def test_contact_refuses_an_unknown_posting(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("contact", "1", "--posting", "999")

    assert result.exit_code != 0
    assert "no such listing" in result.output


def test_schedule_viewing_inherits_the_posting_contact_recorded(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, url, noticed_at) "
        "VALUES ('2', '1', 'x', 'https://example.com/2', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    assert _run("contact", "1", "--posting", "2").exit_code == 0
    result = _run("schedule-viewing", "1", "2026-09-25 14:00")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["stage"] == "viewing_scheduled"
    assert entry["stage_fb_id"] == "2"
    assert entry["stage_at"] == "2026-09-25 14:00"


def test_mark_viewed_keeps_status_and_stage_survives_reject(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    assert _run("shortlist", "1", "--reason", "good district").exit_code == 0
    assert _run("mark-viewed", "1", "--note", "a bit cramped").exit_code == 0
    assert _run("reject", "1", "--reason", "neighbours with dogs").exit_code == 0

    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["status"] == "rejected"
    assert entry["stage"] == "viewed"
    assert entry["stage_note"] == "a bit cramped"


def test_clear_stage_leaves_status_alone(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    assert _run("shortlist", "1").exit_code == 0
    assert _run("contact", "1").exit_code == 0
    result = _run("clear-stage", "1")

    assert result.exit_code == 0
    entry = json.loads(config.DECISIONS_PATH.read_text(encoding="utf-8"))["1"]
    assert entry["status"] == "shortlist"
    assert "stage" not in entry


def test_clear_stage_without_a_decision_is_an_error(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("clear-stage", "1")

    assert result.exit_code != 0
    assert "no decision recorded" in result.output


def test_in_progress_row_reaches_the_export(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    assert _run("shortlist", "1").exit_code == 0
    assert _run("contact", "1").exit_code == 0
    result = _run("export")

    assert result.exit_code == 0
    html = config.HTML_PATH.read_text(encoding="utf-8")
    assert '"stage": "written"' in html


# --- title merge ----------------------------------------------------------


def test_detail_title_only_replaces_a_junk_card_title():
    card = {"fb_id": "1", "title": "Недавно опубликовано"}
    detail = {"title": "2 ห้องนอน 2 ห้องน้ำ - บ้าน", "description": "..."}

    assert cli._merge_listing(card, detail)["title"] == detail["title"]


def test_a_real_card_title_survives_the_detail_page():
    card = {"fb_id": "1", "title": "2 Bed 1 Bath - House"}
    detail = {"title": "Marketplace", "description": "..."}

    assert cli._merge_listing(card, detail)["title"] == "2 Bed 1 Bath - House"


def test_missing_detail_title_leaves_the_card_alone():
    card = {"fb_id": "1", "title": "Недавно опубликовано"}

    assert cli._merge_listing(card, {"title": None})["title"] == "Недавно опубликовано"


def test_ids_are_not_capped_by_the_default_limit(home):
    conn = storage.init_db(config.DB_PATH)
    for fb_id in "123":
        _add_listing(conn, fb_id)
    conn.close()

    result = _run("ai-batch", "--limit", "1", "--ids", "1", "--ids", "2", "--ids", "3")

    assert result.exit_code == 0, result.output
    assert _batch_ids(home) == ["1", "2", "3"]


# --- scrape --no-photos -------------------------------------------------


def _stub_scrape(monkeypatch, gallery_urls):
    """Replace the whole browser stack with one card and one detail page."""

    class _Response:
        ok = True

        def body(self) -> bytes:
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (200, 100), "red").save(buf, "PNG")
            return buf.getvalue()

    class _Request:
        def get(self, url: str) -> _Response:
            return _Response()

    class _Context:
        request = _Request()

        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", lambda pw, headless=True: _Context())
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        cli,
        "scrape_list",
        lambda ctx, url, max_items, allow_empty=False: [
            {
                "fb_id": "900",
                "url": "https://www.facebook.com/marketplace/item/900",
                "title": "2 Bed 1 Bath - House",
                "price_raw": "22 000 ฿",
            }
        ],
    )
    monkeypatch.setattr(
        cli,
        "scrape_detail",
        lambda ctx, url: {
            "url": url,
            "title": "2 Bed 1 Bath - House",
            "description": "House in a village.",
            "published_at": None,
            "price_raw": "22 000 ฿",
            "raw_json": {"text_lines": [], "gallery_urls": gallery_urls},
        },
    )


def test_scrape_no_photos_creates_no_directories(home, monkeypatch):
    _stub_scrape(monkeypatch, ["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])

    result = _run("scrape", "--max", "1", "--no-photos")

    assert result.exit_code == 0, result.output
    assert not (home / "photos").exists()


def test_scrape_downloads_the_gallery_by_default(home, monkeypatch):
    _stub_scrape(monkeypatch, ["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])

    result = _run("scrape", "--max", "1")

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (home / "photos" / "900").iterdir()) == ["01.jpg"]


def test_scrape_without_gallery_urls_creates_no_directories(home, monkeypatch):
    _stub_scrape(monkeypatch, [])

    result = _run("scrape", "--max", "1")

    assert result.exit_code == 0, result.output
    assert not (home / "photos").exists()


def _stored_hashes() -> list[tuple]:
    conn = storage.init_db(config.DB_PATH)
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT fb_id, position, md5, dhash FROM photo_hashes ORDER BY position"
            )
        ]
    finally:
        conn.close()


def test_scrape_hashes_photos_even_with_no_photos(home, monkeypatch):
    _stub_scrape(
        monkeypatch,
        [f"https://scontent.xx.fbcdn.net/v/t39.30808-6/{n}.jpg" for n in range(1, 5)],
    )

    result = _run("scrape", "--max", "1", "--no-photos")

    assert result.exit_code == 0, result.output
    assert not (home / "photos").exists()
    rows = _stored_hashes()
    # The first three shots only, and none of them written to disk.
    assert [(fb_id, pos) for fb_id, pos, _md5, _dh in rows] == [
        ("900", 1),
        ("900", 2),
        ("900", 3),
    ]
    assert "photo hashes: 1" in result.output


def test_scrape_dry_run_neither_fetches_nor_writes_hashes(home, monkeypatch):
    _stub_scrape(
        monkeypatch, ["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"]
    )
    calls: list = []
    monkeypatch.setattr(
        cli.fingerprint,
        "fetch_fingerprints",
        lambda ctx, urls, limit=None: calls.append(urls) or [],
    )

    result = _run("scrape", "--max", "1", "--dry-run")

    assert result.exit_code == 0, result.output
    assert calls == []
    assert _stored_hashes() == []


def test_scrape_without_gallery_urls_stores_no_hashes(home, monkeypatch):
    _stub_scrape(monkeypatch, [])

    result = _run("scrape", "--max", "1", "--no-photos")

    assert result.exit_code == 0, result.output
    assert _stored_hashes() == []
    assert "photo hashes: 0" in result.output


# --- scrape: detail budget and partial writes ---------------------------


def _card(fb_id: str) -> dict:
    return {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
        "title": "2 Bed 1 Bath - House",
        "price_raw": "22 000 ฿",
    }


def _stub_feed(monkeypatch, cards, on_detail, queried=None):
    """Browser stack serving a scripted feed and detail page.

    ``cards`` is either a fixed list or a callable taking the list URL, which
    is what the price-band tests need: one feed per band. Every URL asked for
    is appended to ``queried``.
    """

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    def _list(ctx, url, max_items, allow_empty=False):
        if queried is not None:
            queried.append(url)
        feed = cards(url) if callable(cards) else list(cards)
        return feed[:max_items]

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", lambda pw, headless=True: _Context())
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "scrape_list", _list)
    monkeypatch.setattr(cli, "scrape_detail", lambda ctx, url: on_detail(url))


def _detail(url: str) -> dict:
    return {
        "url": url,
        "title": "2 Bed 1 Bath - House",
        "description": f"House in a village, listing {url.rstrip('/').rsplit('/', 1)[-1]}.",
        "published_at": None,
        "price_raw": "22 000 ฿",
        "raw_json": {"text_lines": [], "gallery_urls": []},
    }


def test_known_listings_do_not_spend_the_detail_budget(home, monkeypatch):
    """The budget counts pages opened, not positions in the feed.

    Known listings sit at the top of a sorted feed and their detail pages are
    skipped, so an index-based cap handed the whole budget to listings that
    never used it — and the new ones further down stayed without ad text.
    """
    conn = storage.init_db(config.DB_PATH)
    for fb_id in ("1", "2", "3"):
        _add_listing(conn, fb_id)
    conn.close()

    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url.rsplit("/", 1)[-1])
        return _detail(url)

    _stub_feed(monkeypatch, [_card(i) for i in "12345"], on_detail)

    result = _run("scrape", "--max", "5", "--detail-cap", "2", "--no-photos")

    assert result.exit_code == 0, result.output
    assert fetched == ["4", "5"]
    conn = storage.init_db(config.DB_PATH)
    rows = {
        row["fb_id"]: row["description"]
        for row in conn.execute("SELECT fb_id, description FROM listings")
    }
    conn.close()
    assert rows["4"] and rows["5"]


def test_detail_cap_stops_after_its_own_number_of_pages(home, monkeypatch):
    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url)
        return _detail(url)

    _stub_feed(monkeypatch, [_card(i) for i in "1234"], on_detail)

    result = _run("scrape", "--max", "4", "--detail-cap", "2", "--no-photos")

    assert result.exit_code == 0, result.output
    assert len(fetched) == 2
    assert "Detail budget spent" in result.output


def test_a_dead_session_mid_run_keeps_what_was_already_scraped(home, monkeypatch):
    """Rows are written as they are scraped, not all at once at the end.

    A wide run walks detail pages for the better part of an hour; when the
    session died on the last one, the whole run used to be thrown away.
    """

    def on_detail(url: str) -> dict:
        if url.endswith("3"):
            raise cli.LoginRequiredError("session expired")
        return _detail(url)

    _stub_feed(monkeypatch, [_card(i) for i in "123"], on_detail)

    result = _run("scrape", "--max", "3", "--no-photos")

    assert result.exit_code == 3
    conn = storage.init_db(config.DB_PATH)
    rows = {
        row["fb_id"]: row["description"]
        for row in conn.execute("SELECT fb_id, description FROM listings")
    }
    conn.close()
    assert sorted(rows) == ["1", "2"]
    assert all(rows.values())


# --- scrape: price-band sweep -------------------------------------------


def test_a_sweep_asks_one_query_per_band(home, monkeypatch):
    queried: list[str] = []
    _stub_feed(monkeypatch, [], _detail, queried=queried)

    result = _run(
        "scrape",
        "--min-price",
        "15000",
        "--max-price",
        "30000",
        "--bands",
        "3",
        "--no-photos",
    )

    assert result.exit_code == 3  # nothing came back from any band
    assert [
        (
            url.split("minPrice=")[1].split("&")[0],
            url.split("maxPrice=")[1].split("&")[0],
        )
        for url in queried
    ] == [("15000", "19999"), ("20000", "24999"), ("25000", "30000")]
    assert "Every band came back empty" in result.output


def test_a_listing_in_two_bands_is_scraped_once(home, monkeypatch):
    """FB leaks results outside its own price filters, so bands overlap."""
    feeds = {
        "15000": [_card("1"), _card("2")],
        "22500": [_card("2"), _card("3")],
    }
    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url.rsplit("/", 1)[-1])
        return _detail(url)

    _stub_feed(
        monkeypatch,
        lambda url: feeds[url.split("minPrice=")[1].split("&")[0]],
        on_detail,
    )

    result = _run(
        "scrape", "--min-price", "15000", "--max-price", "30000",
        "--bands", "2", "--no-photos",
    )

    assert result.exit_code == 0, result.output
    assert sorted(fetched) == ["1", "2", "3"]
    assert "Collected 3 list cards." in result.output


def test_the_detail_budget_is_spread_across_the_bands(home, monkeypatch):
    """Band after band, a small budget would never reach the dearer flats."""
    feeds = {
        "15000": [_card("cheap-1"), _card("cheap-2")],
        "22500": [_card("dear-1"), _card("dear-2")],
    }
    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url.rsplit("/", 1)[-1])
        return _detail(url)

    _stub_feed(
        monkeypatch,
        lambda url: feeds[url.split("minPrice=")[1].split("&")[0]],
        on_detail,
    )

    result = _run(
        "scrape", "--min-price", "15000", "--max-price", "30000",
        "--bands", "2", "--detail-cap", "2", "--no-photos",
    )

    assert result.exit_code == 0, result.output
    assert fetched == ["cheap-1", "dear-1"]


def test_an_empty_band_does_not_stop_the_sweep(home, monkeypatch):
    feeds = {"15000": [], "22500": [_card("7")]}
    _stub_feed(
        monkeypatch,
        lambda url: feeds[url.split("minPrice=")[1].split("&")[0]],
        _detail,
    )

    result = _run(
        "scrape", "--min-price", "15000", "--max-price", "30000",
        "--bands", "2", "--no-photos",
    )

    assert result.exit_code == 0, result.output
    assert "Collected 1 list cards." in result.output


def test_an_impossible_band_split_is_refused(home, monkeypatch):
    _stub_feed(monkeypatch, [_card("1")], _detail)

    result = _run(
        "scrape", "--min-price", "20000", "--max-price", "20001",
        "--bands", "5", "--no-photos",
    )

    assert result.exit_code == 1
    assert "cannot split" in result.output


# --- scrape --ids --------------------------------------------------------


def test_ids_reach_a_known_listing_without_touching_the_feed(home, monkeypatch):
    """Naming an id is how a bad copy gets repaired — it outranks the DB."""
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1", description="Выбор дня")
    conn.close()

    def _no_feed(*args, **kwargs):
        raise AssertionError("the feed must not be opened for --ids")

    _stub_feed(monkeypatch, [], _detail)
    monkeypatch.setattr(cli, "scrape_list", _no_feed)

    result = _run("scrape", "--ids", "1", "--no-photos")

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT description FROM listings WHERE fb_id = ?", ("1",)
    ).fetchone()
    conn.close()
    assert row["description"] == "House in a village, listing 1."


def test_an_id_with_no_listing_behind_it_is_skipped(home, monkeypatch):
    _stub_feed(monkeypatch, [], lambda url: None)

    result = _run("scrape", "--ids", "404", "--no-photos")

    assert result.exit_code == 0, result.output
    assert "no listing behind 404" in result.output
    conn = storage.init_db(config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    conn.close()


# --- scrape-groups --------------------------------------------------------


def _group_card(fb_id: str, group_id: str = "phuketrentsale") -> dict:
    return {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/groups/{group_id}/posts/{fb_id}/",
        "title": None,
        "source": f"group:{group_id}",
    }


def _group_detail(url: str) -> dict:
    post_id = url.rstrip("/").rsplit("/", 1)[-1]
    return {
        "url": url,
        "title": f"Villa {post_id}",
        "description": f"Group post, listing {post_id}.",
        "published_at": None,
        "price_raw": "22 000 ฿",
        "raw_json": {"text_lines": [], "gallery_urls": [], "group": "phuketrentsale"},
    }


def _stub_group_feed(monkeypatch, cards_by_group, on_detail):
    """Browser stack serving scripted group feeds and a group post reader.

    ``cards_by_group`` maps group id to its feed's cards (fixed list or a
    callable taking the group id, for tests needing per-group behaviour).
    """

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    def _feed(ctx, group_id, max_items, days=None, allow_empty=False):
        cards = cards_by_group(group_id) if callable(cards_by_group) else cards_by_group[group_id]
        return list(cards)[:max_items]

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", lambda pw, headless=True: _Context())
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "scrape_group_feed", _feed)
    monkeypatch.setattr(cli, "scrape_group_post", lambda ctx, url, gid, pid: on_detail(url))


def test_scrape_groups_stores_the_group_source(home, monkeypatch):
    _stub_group_feed(monkeypatch, {"phuketrentsale": [_group_card("1")]}, _group_detail)

    result = _run(
        "scrape-groups", "--group", "phuketrentsale", "--max", "1", "--no-photos"
    )

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT source, title, description FROM listings WHERE fb_id = '1'"
    ).fetchone()
    conn.close()
    assert row["source"] == "group:phuketrentsale"
    assert row["title"] == "Villa 1"
    assert row["description"] == "Group post, listing 1."


def test_scrape_groups_unknown_group_is_refused(home, monkeypatch):
    _stub_group_feed(monkeypatch, {}, _group_detail)

    result = _run("scrape-groups", "--group", "not-a-real-group", "--no-photos")

    assert result.exit_code == 1
    assert "Unknown group" in result.output


def test_scrape_groups_round_robins_across_groups(home, monkeypatch):
    """The detail budget is shared: each group contributes in turn, not
    group after group, mirroring the marketplace price-band sweep."""
    feeds = {
        "phuketrentsale": [_group_card("a1"), _group_card("a2")],
        "Villa.For.Rent.Phuket": [_group_card("b1"), _group_card("b2")],
    }
    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url.rstrip("/").rsplit("/", 1)[-1])
        return _group_detail(url)

    _stub_group_feed(monkeypatch, feeds, on_detail)

    result = _run(
        "scrape-groups",
        "--group", "phuketrentsale",
        "--group", "Villa.For.Rent.Phuket",
        "--max", "2",
        "--detail-cap", "2",
        "--no-photos",
    )

    assert result.exit_code == 0, result.output
    assert fetched == ["a1", "b1"]


def test_scrape_groups_known_post_does_not_spend_the_detail_budget(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(
        conn,
        "1",
        url="https://www.facebook.com/groups/phuketrentsale/posts/1/",
        source="group:phuketrentsale",
    )
    conn.close()

    fetched: list[str] = []

    def on_detail(url: str) -> dict:
        fetched.append(url.rstrip("/").rsplit("/", 1)[-1])
        return _group_detail(url)

    _stub_group_feed(
        monkeypatch,
        {"phuketrentsale": [_group_card("1"), _group_card("2")]},
        on_detail,
    )

    result = _run(
        "scrape-groups", "--group", "phuketrentsale", "--max", "2", "--no-photos"
    )

    assert result.exit_code == 0, result.output
    assert fetched == ["2"]


def test_scrape_groups_ids_requires_exactly_one_group(home, monkeypatch):
    _stub_group_feed(monkeypatch, {}, _group_detail)

    result = _run(
        "scrape-groups",
        "--group", "phuketrentsale",
        "--group", "Villa.For.Rent.Phuket",
        "--ids", "1",
        "--no-photos",
    )

    assert result.exit_code == 1
    assert "requires exactly one --group" in result.output


def test_scrape_groups_ids_builds_the_permalink_and_skips_the_feed(home, monkeypatch):
    def _no_feed(*args, **kwargs):
        raise AssertionError("the feed must not be opened for --ids")

    _stub_group_feed(monkeypatch, {}, _group_detail)
    monkeypatch.setattr(cli, "scrape_group_feed", _no_feed)

    result = _run(
        "scrape-groups", "--group", "phuketrentsale", "--ids", "99", "--no-photos"
    )

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT url, source FROM listings WHERE fb_id = '99'"
    ).fetchone()
    conn.close()
    assert row["url"] == "https://www.facebook.com/groups/phuketrentsale/posts/99/"
    assert row["source"] == "group:phuketrentsale"


def test_scrape_ids_reuses_the_stored_url_and_source_for_a_group_row(home, monkeypatch):
    """`scrape --ids` on a group post must repair it through the group
    reader, not open a dead `/marketplace/item/` URL built from the id."""
    conn = storage.init_db(config.DB_PATH)
    _add_listing(
        conn,
        "1",
        url="https://www.facebook.com/groups/phuketrentsale/posts/1/",
        source="group:phuketrentsale",
        description="stale",
    )
    conn.close()

    def on_group_detail(ctx, url, group_id, post_id):
        assert group_id == "phuketrentsale"
        assert post_id == "1"
        return _group_detail(url)

    _stub_feed(monkeypatch, [], _detail)
    monkeypatch.setattr(cli, "scrape_group_post", on_group_detail)

    result = _run("scrape", "--ids", "1", "--no-photos")

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute("SELECT description FROM listings WHERE fb_id = '1'").fetchone()
    conn.close()
    assert row["description"] == "Group post, listing 1."


# --- photos-fetch --------------------------------------------------------


def _stub_cdn(monkeypatch):
    """A logged-in browser context whose request client serves a red square."""

    class _Response:
        ok = True

        def body(self) -> bytes:
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (200, 100), "red").save(buf, "PNG")
            return buf.getvalue()

    class _Request:
        def get(self, url: str) -> _Response:
            return _Response()

    class _Context:
        request = _Request()

        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", lambda pw, headless=True: _Context())
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)


def _queued_for_photos(fb_id: str, *, gallery: list[str]):
    """A listing the text pass read, with its gallery links still stored."""
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(
        conn,
        fb_id,
        raw_json=json.dumps({"text_lines": ["Описание"], "gallery_urls": gallery}),
    )
    _analyse(conn, registry, fb_id)
    conn.close()


def test_photos_fetch_downloads_the_stored_gallery(home, monkeypatch):
    _queued_for_photos("1", gallery=["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])
    _stub_cdn(monkeypatch)

    result = _run("photos-fetch")

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (home / "photos" / "1").iterdir()) == ["01.jpg"]
    assert "Fetched galleries for 1 listing(s)." in result.output


def test_photos_fetch_names_the_listings_whose_links_were_never_stored(
    home, monkeypatch
):
    """Scraped before photos existed — only the detail page can help now."""
    _queued_for_photos("1", gallery=[])
    _stub_cdn(monkeypatch)

    result = _run("photos-fetch")

    assert result.exit_code == 0, result.output
    assert "No stored gallery links for 1 listing(s)" in result.output
    assert "rent-assistant scrape --ids 1" in result.output


def test_photos_fetch_leaves_photos_that_are_already_on_disk(home, monkeypatch):
    """A gallery on disk is never re-downloaded: that would re-queue the pass.

    ``photos_hash`` is what tells the photo pass the pictures changed, so a
    second download of the same gallery would send an analysed listing back
    into the queue for nothing.
    """
    _queued_for_photos("1", gallery=["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])
    _shoot(home, "1")
    _stub_cdn(monkeypatch)

    result = _run("photos-fetch", "--ids", "1")

    assert result.exit_code == 0, result.output
    assert "1 listing(s) already have their photos on disk." in result.output
    assert sorted(p.name for p in (home / "photos" / "1").iterdir()) == [
        "01.jpg",
        "02.jpg",
    ]


def test_photos_fetch_ids_reach_a_listing_the_queue_does_not_hold(home, monkeypatch):
    """No analysis yet, so the photo queue does not know about it."""
    conn = storage.init_db(config.DB_PATH)
    _add_listing(
        conn,
        "5",
        raw_json=json.dumps(
            {"gallery_urls": ["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"]}
        ),
    )
    conn.close()
    _stub_cdn(monkeypatch)

    result = _run("photos-fetch", "--ids", "5")

    assert result.exit_code == 0, result.output
    assert (home / "photos" / "5" / "01.jpg").exists()


# --- photo-batch ---------------------------------------------------------


def _shoot(home, fb_id: str, count: int = 2):
    directory = home / "photos" / fb_id
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (directory / f"{index:02d}.jpg").write_bytes(b"jpeg")


def _photo_batch_ids(home):
    written = sorted((home / "ai").glob("photo-batch-*.json"))[-1]
    return [item["fb_id"] for item in json.loads(written.read_text(encoding="utf-8"))["items"]]


def test_photo_batch_queues_analysed_listings_with_photos(home):
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, registry, "1")
    conn.close()
    _shoot(home, "1")

    result = _run("photo-batch")

    assert result.exit_code == 0, result.output
    assert _photo_batch_ids(home) == ["1"]


def test_photo_batch_reports_listings_without_photos(home):
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, registry, "1")
    conn.close()

    result = _run("photo-batch")

    assert result.exit_code == 0, result.output
    assert "no photos on disk" in result.output
    assert "Nothing pending" in result.output
    assert list((home / "ai").glob("photo-batch-*.json")) == []


def test_photo_batch_ids_reach_past_the_queue(home):
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    conn.close()
    _shoot(home, "1")

    result = _run("photo-batch", "--ids", "1")

    assert result.exit_code == 0, result.output
    assert _photo_batch_ids(home) == ["1"]


def test_photo_batch_ids_without_photos_say_so(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("photo-batch", "--ids", "1")

    assert result.exit_code == 0, result.output
    assert "No photos on disk" in result.output
    assert "Nothing to analyse" in result.output


def test_photo_batch_unknown_listing_is_named(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()
    _shoot(home, "999")

    result = _run("photo-batch", "--ids", "999")

    assert "Unknown listing(s): 999" in result.output


def test_photo_batch_limit_must_be_positive(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "1")
    conn.close()

    result = _run("photo-batch", "--limit", "0")

    assert result.exit_code != 0
    assert "--limit must be a positive number" in result.output


def test_photo_batch_writes_to_an_explicit_out_path(home, tmp_path):
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    _analyse(conn, registry, "1")
    conn.close()
    _shoot(home, "1")
    out = tmp_path / "nested" / "photo.json"

    result = _run("photo-batch", "--out", str(out))

    assert result.exit_code == 0, result.output
    assert [i["fb_id"] for i in json.loads(out.read_text(encoding="utf-8"))["items"]] == ["1"]


# --- photo-apply ---------------------------------------------------------


def _photo_ready(home):
    """A listing the text pass already read, with photos waiting on disk."""
    conn = storage.init_db(config.DB_PATH)
    registry = ai_schema.load_registry()
    _add_listing(conn, "1")
    ai_batch.apply_results(
        conn,
        registry,
        {
            "schema_version": registry.schema_version,
            "results": [
                {
                    "fb_id": "1",
                    "verdict": "View.",
                    "data_quality": "good",
                    # The claim the pictures are there to check.
                    "fields": {"pets": {"v": "allowed"}, "furnished": {"v": "full"}},
                }
            ],
        },
    )
    conn.close()
    _shoot(home, "1")


def _photo_result(tmp_path, fields, **entry_overrides):
    registry = ai_schema.load_registry()
    entry = {
        "fb_id": "1",
        "photos_hash": photos.photos_hash("1"),
        "comment": "Good light, dated renovation.",
        "fields": fields,
    }
    entry.update(entry_overrides)
    path = tmp_path / "photo-result.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": registry.schema_version,
                "model": "claude-sonnet-5",
                "results": [entry],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_photo_apply_writes_and_reports_disagreements(home, tmp_path):
    _photo_ready(home)
    path = _photo_result(
        tmp_path, {"furnished": {"v": "none", "conf": "high", "ev": "photo 1: empty"}}
    )

    result = _run("photo-apply", str(path))

    assert result.exit_code == 0, result.output
    assert "Applied 1" in result.output
    assert "1.furnished" in result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    conn.close()
    assert storage.photo_fields_of(dict(row))["furnished"]["v"] == "none"


def test_photo_apply_dry_run_writes_nothing(home, tmp_path):
    _photo_ready(home)
    path = _photo_result(
        tmp_path, {"condition": {"v": "worn", "conf": "high", "ev": "photo 2"}}
    )

    result = _run("photo-apply", str(path), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Dry-run: would apply 1" in result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    conn.close()
    assert row["photo_data_json"] is None


def test_photo_apply_skips_a_stale_photos_hash(home, tmp_path):
    _photo_ready(home)
    path = _photo_result(
        tmp_path,
        {"condition": {"v": "worn", "conf": "high", "ev": "photo 2"}},
        photos_hash="gone",
    )

    result = _run("photo-apply", str(path))

    assert result.exit_code == 0, result.output
    assert "Skipped 1: 1" in result.output
    assert "photos_hash" in result.output


def test_photo_apply_rejects_a_malformed_file(home, tmp_path):
    _photo_ready(home)
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = _run("photo-apply", str(path))

    assert result.exit_code != 0
    assert "not valid JSON" in result.output


# --- photo-hash ----------------------------------------------------------


def _hashed(fb_id: str, *, gallery: list[str]):
    """A listing with stored gallery links and no photo hashes yet."""
    conn = storage.init_db(config.DB_PATH)
    _add_listing(
        conn,
        fb_id,
        raw_json=json.dumps({"text_lines": ["Описание"], "gallery_urls": gallery}),
    )
    conn.close()


def _hash_rows(fb_id: str) -> list[dict]:
    conn = storage.init_db(config.DB_PATH)
    rows = storage.photo_hashes_by_listing(conn).get(fb_id, [])
    conn.close()
    return rows


def test_photo_hash_skips_listings_that_already_have_hashes(home, monkeypatch):
    _hashed("1", gallery=["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])
    _hashed("2", gallery=["https://scontent.xx.fbcdn.net/v/t39.30808-6/2.jpg"])
    conn = storage.init_db(config.DB_PATH)
    storage.save_photo_hashes(conn, "1", [(0, "md5", "dhash")])
    conn.close()
    _stub_cdn(monkeypatch)

    result = _run("photo-hash")

    assert result.exit_code == 0, result.output
    assert "Hashed photos for 1 listing(s)." in result.output
    # Untouched: the queue never handed listing 1 over.
    assert _hash_rows("1") == [{"position": 0, "md5": "md5", "dhash": "dhash"}]
    assert len(_hash_rows("2")) == 1


def test_photo_hash_ids_reach_a_listing_the_queue_does_not_hold(home, monkeypatch):
    """Already hashed, so out of the queue — but named explicitly."""
    _hashed("1", gallery=["https://scontent.xx.fbcdn.net/v/t39.30808-6/1.jpg"])
    conn = storage.init_db(config.DB_PATH)
    storage.save_photo_hashes(conn, "1", [(0, "stale", "stale")])
    conn.close()
    _stub_cdn(monkeypatch)

    result = _run("photo-hash", "--ids", "1")

    assert result.exit_code == 0, result.output
    rows = _hash_rows("1")
    assert len(rows) == 1
    assert rows[0]["md5"] != "stale"


def test_photo_hash_reports_listings_without_gallery_links(home, monkeypatch):
    _hashed("1", gallery=[])
    _stub_cdn(monkeypatch)

    result = _run("photo-hash")

    assert result.exit_code == 0, result.output
    assert "No stored gallery links for 1 listing(s)" in result.output
    assert "rent-assistant scrape --ids 1" in result.output
    assert _hash_rows("1") == []


# --- the pets policy per building ---------------------------------------


def _condo(fb_id: str, project: str = "The Base Central Phuket", *, pets="unclear"):
    """One analysed listing advertising a flat in ``project``."""
    conn = storage.init_db(config.DB_PATH)
    try:
        _add_listing(conn, fb_id, description="Flat in a condo.")
        storage.upsert_ai_analysis(
            conn,
            {
                "fb_id": fb_id,
                "schema_version": 1,
                "analyzed_at": "2026-08-25T10:00:00Z",
                "source_hash": f"hash-{fb_id}",
                "model": "claude-opus-5 (claude-code)",
                "verdict": "View, a flat in a condo.",
                "data_quality": "good",
                "data_json": {
                    "project_name": {"v": project},
                    "area": {"v": "Phuket Town"},
                    "pets": {"v": pets},
                },
            },
        )
    finally:
        conn.close()


def _result_file(home, **overrides):
    entry = {
        "key": "the-base-central-phuket",
        "name": "The Base Central Phuket",
        "area": "Phuket Town",
        "policy": "not_allowed",
        "confidence": "high",
        "note": "Management: no pets of any kind.",
        "sources": [
            {
                "url": "https://example.com/rules",
                "title": "The Base Central — House Rules",
                "quote": "Pets are not allowed in the building.",
            }
        ],
    }
    entry.update(overrides)
    path = home / "buildings-result.json"
    path.write_text(
        json.dumps({"model": "claude-opus-5", "results": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _batch(home):
    written = sorted((home / "ai").glob("buildings-batch-*.json"))
    return json.loads(written[-1].read_text(encoding="utf-8"))


def test_buildings_batch_writes_the_queue(home):
    _condo("1")
    _condo("2")

    result = _run("buildings-batch")

    assert result.exit_code == 0, result.output
    assert "1 building(s)" in result.output
    batch = _batch(home)
    assert [b["key"] for b in batch["buildings"]] == ["the-base-central-phuket"]
    assert batch["buildings"][0]["listings"] == 2
    assert batch["research_prompt"]


def test_buildings_batch_narrows_to_the_named_key(home):
    _condo("1", "The Base Central Phuket")
    _condo("2", "Zcape III")

    result = _run("buildings-batch", "--key", "Zcape III")

    assert result.exit_code == 0, result.output
    assert [b["key"] for b in _batch(home)["buildings"]] == ["zcape-iii"]


def test_buildings_batch_caps_at_the_limit(home):
    _condo("1", "The Base Central Phuket")
    _condo("2", "Zcape III")

    result = _run("buildings-batch", "--limit", "1")

    assert result.exit_code == 0, result.output
    assert len(_batch(home)["buildings"]) == 1


def test_buildings_batch_writes_nothing_on_an_empty_queue(home):
    result = _run("buildings-batch")

    assert result.exit_code == 0, result.output
    assert "Nothing to research" in result.output
    assert not list((home / "ai").glob("buildings-batch-*.json"))


def test_buildings_batch_limit_must_be_positive(home):
    result = _run("buildings-batch", "--limit", "0")

    assert result.exit_code != 0
    assert "--limit must be a positive number" in result.output


def test_buildings_apply_stores_a_proposal_and_rejects_nothing(home):
    _condo("1")
    path = _result_file(home)

    result = _run("buildings-apply", str(path))

    assert result.exit_code == 0, result.output
    assert "Proposed 1 building(s)" in result.output
    assert "1 listing(s)" in result.output
    store = buildings.load_buildings(config.BUILDINGS_PATH)
    assert store["the-base-central-phuket"]["status"] == "proposed"

    conn = storage.init_db(config.DB_PATH)
    try:
        rows = storage.fetch_enriched(conn)
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["active"]


def test_buildings_apply_dry_run_writes_no_file(home):
    _condo("1")
    path = _result_file(home)

    result = _run("buildings-apply", str(path), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Dry-run: would propose 1 building(s)" in result.output
    assert buildings.load_buildings(config.BUILDINGS_PATH) == {}


def test_buildings_apply_reports_an_unknown_policy(home):
    _condo("1")
    path = _result_file(home, policy="no-pets-ever")

    result = _run("buildings-apply", str(path))

    assert result.exit_code != 0
    assert "unknown pets policy" in result.output
    assert buildings.load_buildings(config.BUILDINGS_PATH) == {}


def test_buildings_apply_reports_a_missing_file(home):
    result = _run("buildings-apply", str(home / "nope.json"))

    assert result.exit_code != 0
    assert "nope.json" in result.output


def test_buildings_apply_reports_a_broken_file(home):
    path = home / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = _run("buildings-apply", str(path))

    assert result.exit_code != 0
    assert "not valid JSON" in result.output


def test_buildings_list_shows_the_evidence(home):
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))

    result = _run("buildings", "list")

    assert result.exit_code == 0, result.output
    assert "The Base Central Phuket" in result.output
    assert "not_allowed / proposed" in result.output
    assert "1 listing(s)" in result.output
    assert "https://example.com/rules" in result.output
    assert "Pets are not allowed in the building." in result.output


def test_buildings_list_filters_by_status(home):
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))

    result = _run("buildings", "list", "--status", "confirmed")

    assert result.exit_code == 0, result.output
    assert "match" in result.output
    assert "The Base Central Phuket" not in result.output


def test_buildings_set_confirms_and_rejects_the_listings(home):
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))

    result = _run(
        "buildings", "set", "the-base-central-phuket", "--policy", "not_allowed"
    )

    assert result.exit_code == 0, result.output
    assert "confirmed" in result.output
    entry = buildings.load_buildings(config.BUILDINGS_PATH)["the-base-central-phuket"]
    assert entry["status"] == "confirmed"
    # The sources the proposal carried survive the confirmation.
    assert entry["sources"][0]["url"] == "https://example.com/rules"

    conn = storage.init_db(config.DB_PATH)
    try:
        rows = storage.fetch_enriched(conn)
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["rejected"]
    assert rows[0]["reason"] == "Building forbids pets"


def test_buildings_set_does_not_count_the_ads_that_survive_it(home):
    # Two of the three ads promise animals outright and stay active, so the
    # number the user confirms on must be one, not three.
    _condo("1")
    _condo("2", pets="allowed")
    _condo("3", pets="allowed")
    _run("buildings-apply", str(_result_file(home)))

    result = _run(
        "buildings", "set", "the-base-central-phuket", "--policy", "not_allowed"
    )

    assert result.exit_code == 0, result.output
    assert "Rejects listings: 1" in result.output
    assert "Survive: 2" in result.output

    conn = storage.init_db(config.DB_PATH)
    try:
        rows = {row["fb_id"]: row["status"] for row in storage.fetch_enriched(conn)}
    finally:
        conn.close()
    assert rows == {"1": "rejected", "2": "active", "3": "active"}


def test_buildings_conflicts_lists_the_ads_that_argue_with_the_rule(home):
    _condo("1", pets="allowed")
    _condo("2", pets="allowed")
    _run("buildings-apply", str(_result_file(home)))

    result = _run("buildings", "conflicts")

    assert result.exit_code == 0, result.output
    assert "The Base Central Phuket" in result.output
    assert "2 ads promise pets" in result.output


def test_buildings_conflicts_is_quiet_on_a_single_ad(home):
    _condo("1", pets="allowed")
    _run("buildings-apply", str(_result_file(home)))

    result = _run("buildings", "conflicts")

    assert result.exit_code == 0, result.output
    assert "No building" in result.output


def test_buildings_set_finds_the_building_by_name_and_takes_aliases(home):
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))

    result = _run(
        "buildings",
        "set",
        "The Base Central Phuket",
        "--policy",
        "negotiable",
        "--note",
        "Management: small dogs only",
        "--alias",
        "base central",
    )

    assert result.exit_code == 0, result.output
    entry = buildings.load_buildings(config.BUILDINGS_PATH)["the-base-central-phuket"]
    assert entry["policy"] == "negotiable"
    assert entry["aliases"] == ["base central"]
    assert entry["note"] == "Management: small dogs only"

    # negotiable keeps the listing — the user's stated rule.
    conn = storage.init_db(config.DB_PATH)
    try:
        rows = storage.fetch_enriched(conn)
    finally:
        conn.close()
    assert [row["status"] for row in rows] == ["active"]


def test_buildings_set_reports_an_unknown_key(home):
    result = _run("buildings", "set", "no-such-condo", "--policy", "not_allowed")

    assert result.exit_code != 0
    assert "unknown building" in result.output


def test_buildings_set_reports_an_invalid_policy(home):
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))

    result = _run(
        "buildings", "set", "the-base-central-phuket", "--policy", "no-pets-ever"
    )

    assert result.exit_code != 0
    assert "no-pets-ever" in result.output
    store = buildings.load_buildings(config.BUILDINGS_PATH)
    assert store["the-base-central-phuket"]["status"] == "proposed"


def test_ai_status_counts_the_buildings(home):
    _condo("1")

    unresearched = _run("ai-status")
    assert unresearched.exit_code == 0, unresearched.output
    assert "Unresearched: 1" in unresearched.output
    assert "Proposed:       0" in unresearched.output

    _run("buildings-apply", str(_result_file(home)))
    proposed = _run("ai-status")
    assert "Unresearched: 0" in proposed.output
    assert "Proposed:       1" in proposed.output
    assert "Confirmed:      0" in proposed.output

    _run("buildings", "set", "the-base-central-phuket", "--policy", "not_allowed")
    confirmed = _run("ai-status")
    assert "Confirmed:      1" in confirmed.output


def test_export_repopulates_the_buildings_mirror_after_a_db_wipe(home):
    # `data/buildings.json` is the source of truth and the table is only a
    # mirror: a wiped DB must come back with the same rejections, without any
    # research being re-run.
    _condo("1")
    _run("buildings-apply", str(_result_file(home)))
    assert (
        _run("buildings", "set", "the-base-central-phuket", "--policy", "not_allowed")
    ).exit_code == 0

    config.DB_PATH.unlink()
    _condo("1")  # a fresh scrape into a fresh database
    conn = storage.init_db(config.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0] == 0
    finally:
        conn.close()

    result = _run("export")

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    try:
        assert storage.building_policies(conn) == {
            "the-base-central-phuket": "not_allowed"
        }
        row = storage.fetch_enriched(conn)[0]
    finally:
        conn.close()
    assert (row["status"], row["reason"]) == ("rejected", rules.REASON_BUILDING_PETS)


def test_export_reports_a_broken_buildings_file(home):
    _condo("1")
    config.BUILDINGS_PATH.write_text(
        json.dumps({"x": {"name": "X", "policy": "maybe", "status": "confirmed"}}),
        encoding="utf-8",
    )

    result = _run("export")

    assert result.exit_code != 0
    assert "unknown pets policy" in result.output
    assert "Traceback" not in result.output


# --- messages sync / send / status --------------------------------------


def _stub_browser(monkeypatch, *, logged_in=True):
    """Same shape as `_stub_scrape`'s browser stack, without the scrape stubs."""

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(
        cli,
        "launch_persistent",
        lambda pw, headless=True, profile_dir=None: _Context(),
    )
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: logged_in)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)


def test_messages_sync_skips_pre_cutoff_threads(home, monkeypatch):
    _stub_browser(monkeypatch)
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    conn.close()

    monkeypatch.setattr(
        cli,
        "list_marketplace_threads",
        lambda ctx, **kw: [
            {"thread_id": "recent", "peer_name": "A", "last_activity_raw": "14 мин."},
            {"thread_id": "ancient", "peer_name": "B", "last_activity_raw": "2 г."},
        ],
    )
    read_calls = []

    def _read_thread(ctx, thread_id):
        read_calls.append(thread_id)
        return {
            "thread_id": thread_id,
            "peer_name": "A",
            "listing_fb_id": "900",
            "messages": [{"direction": "out", "text": "Hello!", "sent_at": None}],
        }

    monkeypatch.setattr(cli, "read_thread", _read_thread)

    result = _run("messages", "sync")

    assert result.exit_code == 0, result.output
    assert read_calls == ["recent"]
    assert "skipped 1 too old" in result.output


def test_messages_sync_writes_conversation(home, monkeypatch):
    _stub_browser(monkeypatch)
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    conn.close()

    monkeypatch.setattr(
        cli,
        "list_marketplace_threads",
        lambda ctx, **kw: [
            {"thread_id": "t1", "peer_name": "A", "last_activity_raw": "14 мин."}
        ],
    )
    monkeypatch.setattr(
        cli,
        "read_thread",
        lambda ctx, thread_id: {
            "thread_id": thread_id,
            "peer_name": "A",
            "listing_fb_id": "900",
            "messages": [
                {"direction": "out", "text": "Hello!", "sent_at": None},
                {"direction": "in", "text": "Sure, come by", "sent_at": None},
            ],
        },
    )

    result = _run("messages", "sync")
    assert result.exit_code == 0, result.output

    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT fb_id, state FROM conversations WHERE thread_id = 't1'"
    ).fetchone()
    conn.close()
    assert row["fb_id"] == "900"
    assert row["state"] == "replied"


def test_messages_sync_resolves_merged_posting(home, monkeypatch):
    _stub_browser(monkeypatch)
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    conn.execute(
        "INSERT INTO duplicates (fb_id, canonical_fb_id, content_key, noticed_at, url)"
        " VALUES ('901', '900', 'k', '2026-09-01', NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        cli,
        "list_marketplace_threads",
        lambda ctx, **kw: [
            {"thread_id": "t1", "peer_name": "A", "last_activity_raw": "14 мин."}
        ],
    )
    monkeypatch.setattr(
        cli,
        "read_thread",
        lambda ctx, thread_id: {
            "thread_id": thread_id,
            "peer_name": "A",
            "listing_fb_id": "901",
            "messages": [{"direction": "out", "text": "Hello!", "sent_at": None}],
        },
    )

    result = _run("messages", "sync")
    assert result.exit_code == 0, result.output

    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT fb_id, posting_fb_id FROM conversations WHERE thread_id = 't1'"
    ).fetchone()
    conn.close()
    assert row["fb_id"] == "900"
    assert row["posting_fb_id"] == "901"


def test_messages_send_dry_run_writes_nothing(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    result = _run("messages", "send")

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert messages_module.load_send_log() == {}
    entry = decisions.load_decisions(None).get("900") or {}
    assert "stage" not in entry


def test_messages_send_apply_sends_and_records_stage(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    _stub_browser(monkeypatch)
    sent_texts = []

    def _send_first_message(ctx, url, text):
        sent_texts.append((url, text))
        return True

    monkeypatch.setattr(cli, "send_first_message", _send_first_message)

    result = _run("messages", "send", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert len(sent_texts) == 1
    log = messages_module.load_send_log()
    assert "900:900:first_contact" in log
    entry = decisions.load_decisions(None)["900"]
    assert entry["stage"] == "written"


def test_messages_send_skips_already_sent(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()
    messages_module.append_send_log("900", "900", "first_contact", "Hello!")

    result = _run("messages", "send")

    assert result.exit_code == 0, result.output
    assert "Nothing to send" in result.output


def test_messages_send_respects_cap(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    _add_listing(conn, "901")
    decisions.save_decision(None, "900", "shortlist")
    decisions.save_decision(None, "901", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    result = _run("messages", "send", "--cap", "1")

    assert result.exit_code == 0, result.output
    assert result.output.count("---") == 2  # one draft block: 2 dashes-lines


def test_messages_status_reports_needs_action(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    storage.save_conversation(
        conn,
        thread_id="t1",
        fb_id="900",
        posting_fb_id="900",
        peer_name="Landlord",
        messages=[
            {"direction": "out", "text": "Hello!", "sent_at": None},
            {"direction": "in", "text": "No pets allowed", "sent_at": None},
        ],
        state="pets_declined",
        needs_action=True,
        needs_action_reason="pets_declined",
        reply_hash="h1",
    )
    conn.close()

    result = _run("messages", "status")

    assert result.exit_code == 0, result.output
    assert "900" in result.output
    assert "pets_declined" in result.output


def test_messages_status_empty(home):
    result = _run("messages", "status")
    assert result.exit_code == 0, result.output
    assert "Nothing needs your attention" in result.output


def test_messages_classify_apply_updates_state(home):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    storage.save_conversation(
        conn,
        thread_id="t1",
        fb_id="900",
        posting_fb_id="900",
        peer_name="Landlord",
        messages=[
            {"direction": "out", "text": "Hello!", "sent_at": None},
            {"direction": "in", "text": "Saturday works", "sent_at": None},
        ],
        state="replied",
        needs_action=False,
        needs_action_reason=None,
        reply_hash="h1",
    )
    conn.close()

    result_path = home / "result.json"
    result_path.write_text(
        json.dumps(
            [
                {
                    "thread_id": "t1",
                    "reply_hash": "h1",
                    "facts": {"viewing_proposed": True, "proposed_times": ["Saturday"]},
                }
            ]
        ),
        encoding="utf-8",
    )

    result = _run("messages", "classify-apply", str(result_path))

    assert result.exit_code == 0, result.output
    conn = storage.init_db(config.DB_PATH)
    row = conn.execute(
        "SELECT state, needs_action FROM conversations WHERE thread_id = 't1'"
    ).fetchone()
    conn.close()
    assert row["state"] == "viewing_proposed"
    assert row["needs_action"] == 1


def test_messages_send_apply_continues_after_one_failure(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    _add_listing(conn, "901")
    decisions.save_decision(None, "900", "shortlist")
    decisions.save_decision(None, "901", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    _stub_browser(monkeypatch)
    sent_urls = []

    def _send_first_message(ctx, url, text):
        if "900" in url:
            raise RuntimeError("stray overlay intercepted the click")
        sent_urls.append(url)
        return True

    monkeypatch.setattr(cli, "send_first_message", _send_first_message)

    result = _run("messages", "send", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert "FAILED: 900" in result.output
    assert len(sent_urls) == 1
    log = messages_module.load_send_log()
    assert "900:900:first_contact" not in log
    assert "901:901:first_contact" in log
    entry_900 = decisions.load_decisions(None).get("900") or {}
    assert "stage" not in entry_900
    entry_901 = decisions.load_decisions(None)["901"]
    assert entry_901["stage"] == "written"


def test_messages_send_stops_on_daily_limit(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    _add_listing(conn, "901")
    decisions.save_decision(None, "900", "shortlist")
    decisions.save_decision(None, "901", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    _stub_browser(monkeypatch)
    from rent_assistant.messenger import MessagingLimitReachedError

    calls = []

    def _send_first_message(ctx, url, text):
        calls.append(url)
        if len(calls) == 1:
            return True
        raise MessagingLimitReachedError("Facebook: daily messaging limit reached")

    monkeypatch.setattr(cli, "send_first_message", _send_first_message)

    result = _run("messages", "send", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert "daily cap" in result.output
    log = messages_module.load_send_log()
    assert len(log) == 1


# --- multi-account (--profile) plumbing ----------------------------------


def test_browser_profile_dir_default(home):
    assert config.browser_profile_dir(None) == config.BROWSER_PROFILE_DIR
    assert config.browser_profile_dir("default") == config.BROWSER_PROFILE_DIR


def test_browser_profile_dir_named(home):
    partner_dir = config.browser_profile_dir("partner")
    assert partner_dir != config.BROWSER_PROFILE_DIR
    assert partner_dir.name == "browser-profile-partner"
    assert partner_dir.parent == config.RENT_ASSISTANT_HOME


def test_messages_sync_uses_named_profile(home, monkeypatch):
    captured = {}

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    def _launch(pw, headless=True, profile_dir=None):
        captured["profile_dir"] = profile_dir
        return _Context()

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", _launch)
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "list_marketplace_threads", lambda ctx, **kw: [])

    result = _run("messages", "sync", "--profile", "partner")

    assert result.exit_code == 0, result.output
    assert captured["profile_dir"] == config.browser_profile_dir("partner")
    assert captured["profile_dir"] != config.BROWSER_PROFILE_DIR


def test_messages_send_uses_named_profile(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    captured = {}

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    def _launch(pw, headless=True, profile_dir=None):
        captured["profile_dir"] = profile_dir
        return _Context()

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", _launch)
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "send_first_message", lambda ctx, url, text: True)

    result = _run("messages", "send", "--apply", "--yes", "--profile", "partner")

    assert result.exit_code == 0, result.output
    assert captured["profile_dir"] == config.browser_profile_dir("partner")


def test_messages_send_default_profile_unchanged(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    captured = {}

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    def _launch(pw, headless=True, profile_dir=None):
        captured["profile_dir"] = profile_dir
        return _Context()

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", _launch)
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "send_first_message", lambda ctx, url, text: True)

    result = _run("messages", "send", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert captured["profile_dir"] == config.BROWSER_PROFILE_DIR


def test_messages_send_records_stage_profile_default(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    _stub_browser(monkeypatch)
    monkeypatch.setattr(cli, "send_first_message", lambda ctx, url, text: True)

    result = _run("messages", "send", "--apply", "--yes")
    assert result.exit_code == 0, result.output

    entry = decisions.load_decisions(None)["900"]
    assert entry["stage_profile"] == "default"
    log = messages_module.load_send_log()
    assert log["900:900:first_contact"]["profile"] == "default"


def test_messages_send_records_stage_profile_named(home, monkeypatch):
    conn = storage.init_db(config.DB_PATH)
    _add_listing(conn, "900")
    decisions.save_decision(None, "900", "shortlist")
    decisions.sync_to_db(conn)
    conn.close()

    _stub_browser(monkeypatch)
    monkeypatch.setattr(cli, "send_first_message", lambda ctx, url, text: True)

    result = _run("messages", "send", "--apply", "--yes", "--profile", "partner")
    assert result.exit_code == 0, result.output

    entry = decisions.load_decisions(None)["900"]
    assert entry["stage_profile"] == "partner"
    log = messages_module.load_send_log()
    assert log["900:900:first_contact"]["profile"] == "partner"


# --- seed: the sample data set behind the README's two-minute path -------


SAMPLE_LISTINGS = config.REPO_ROOT / "examples" / "sample_listings.json"
SAMPLE_RESULT = config.REPO_ROOT / "examples" / "sample_ai_result.json"


def test_seed_loads_the_sample_listings_and_records_the_repost(home):
    result = _run("seed", str(SAMPLE_LISTINGS))
    assert result.exit_code == 0, result.output
    assert "inserted: 8" in result.output
    assert "duplicate: 1" in result.output
    conn = storage.init_db(config.DB_PATH)
    try:
        assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 8
        assert conn.execute("SELECT count(*) FROM duplicates").fetchone()[0] == 1
    finally:
        conn.close()


def test_seed_rejects_a_file_that_is_not_a_list(home, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"fb_id": "1"}', encoding="utf-8")
    result = _run("seed", str(bad))
    assert result.exit_code != 0
    assert "JSON list" in result.output


def test_two_minute_path_runs_on_the_sample_data(home):
    """seed -> ai-apply -> rate -> export, exactly as the README describes it."""
    assert _run("seed", str(SAMPLE_LISTINGS)).exit_code == 0
    applied = _run("ai-apply", str(SAMPLE_RESULT))
    assert applied.exit_code == 0, applied.output
    assert "Applied 8" in applied.output
    assert "Auto-rejected 4" in applied.output
    assert "warning" not in applied.output
    assert _run("rate", "9000000000000001", "5", "--reason", "looks right").exit_code == 0
    exported = _run("export")
    assert exported.exit_code == 0, exported.output
    html = config.HTML_PATH.read_text(encoding="utf-8")
    assert "Sample Garden Village" in html
    assert config.CSV_PATH.exists()
    rows = {r["fb_id"]: r for r in storage.fetch_enriched(storage.init_db(config.DB_PATH))}
    assert rows["9000000000000001"]["status"] == "shortlist"
    assert rows["9000000000000003"]["status"] == "rejected"  # Patong
    assert rows["9000000000000004"]["status"] == "rejected"  # price
    assert rows["9000000000000005"]["status"] == "rejected"  # studio
    assert rows["9000000000000006"]["status"] == "rejected"  # pets
    assert rows["9000000000000002"]["status"] == "active"
