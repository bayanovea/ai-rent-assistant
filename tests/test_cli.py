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

from fb_rent import ai_batch, ai_schema, buildings, cli, config, photos, storage
from fb_rent.cli import main


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every fb-rent path at a temp directory."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(config, "FB_RENT_HOME", root)
    monkeypatch.setattr(config, "DB_PATH", root / "rentals.db")
    monkeypatch.setattr(config, "CSV_PATH", root / "rentals.csv")
    monkeypatch.setattr(config, "HTML_PATH", root / "rentals.html")
    monkeypatch.setattr(config, "AI_DIR", root / "ai")
    monkeypatch.setattr(config, "PHOTOS_DIR", root / "photos")
    monkeypatch.setattr(config, "DECISIONS_PATH", root / "decisions.json")
    monkeypatch.setattr(config, "BUILDINGS_PATH", root / "buildings.json")
    return root


def _run(*args):
    return CliRunner().invoke(main, list(args))


def _add_listing(conn, fb_id: str, **overrides):
    listing = {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
        "title": "2 Bed 1 Bath - House",
        "price_thb": 22000,
        "price_raw": "22 000 ฿/месяц",
        "district": "Kathu, Phuket",
        "description": "Дом в посёлке, 2 спальни. Собаки разрешены.",
        "raw_json": json.dumps({"text_lines": ["Описание", "Дом в посёлке."]}),
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
                    "verdict": "Годится.",
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

    assert _run("reject", "1", "--reason", "далеко").exit_code == 0

    assert config.DECISIONS_PATH.stat().st_mode & 0o077 == 0o044


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
            "description": "Дом в посёлке.",
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
        "description": f"Дом в посёлке, объявление {url.rstrip('/').rsplit('/', 1)[-1]}.",
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
    assert row["description"] == "Дом в посёлке, объявление 1."


def test_an_id_with_no_listing_behind_it_is_skipped(home, monkeypatch):
    _stub_feed(monkeypatch, [], lambda url: None)

    result = _run("scrape", "--ids", "404", "--no-photos")

    assert result.exit_code == 0, result.output
    assert "no listing behind 404" in result.output
    conn = storage.init_db(config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    conn.close()


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
    assert "fb-rent scrape --ids 1" in result.output


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
                    "verdict": "Годится.",
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
        "comment": "Свет хороший, ремонт старый.",
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
        tmp_path, {"furnished": {"v": "none", "conf": "high", "ev": "фото 1: пусто"}}
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
        tmp_path, {"condition": {"v": "worn", "conf": "high", "ev": "фото 2"}}
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
        {"condition": {"v": "worn", "conf": "high", "ev": "фото 2"}},
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
    assert "fb-rent scrape --ids 1" in result.output
    assert _hash_rows("1") == []


# --- the pets policy per building ---------------------------------------


def _condo(fb_id: str, project: str = "The Base Central Phuket", *, pets="unclear"):
    """One analysed listing advertising a flat in ``project``."""
    conn = storage.init_db(config.DB_PATH)
    try:
        _add_listing(conn, fb_id, description="Квартира в кондо.")
        storage.upsert_ai_analysis(
            conn,
            {
                "fb_id": fb_id,
                "schema_version": 1,
                "analyzed_at": "2026-08-25T10:00:00Z",
                "source_hash": f"hash-{fb_id}",
                "model": "claude-opus-5 (claude-code)",
                "verdict": "Подходит, квартира в кондо.",
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
        "note": "УК: no pets of any kind.",
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
    assert rows[0]["reason"] == "Кондо запрещает животных"


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
        "УК: только мелкие собаки",
        "--alias",
        "base central",
    )

    assert result.exit_code == 0, result.output
    entry = buildings.load_buildings(config.BUILDINGS_PATH)["the-base-central-phuket"]
    assert entry["policy"] == "negotiable"
    assert entry["aliases"] == ["base central"]
    assert entry["note"] == "УК: только мелкие собаки"

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
    assert "Не исследованы: 1" in unresearched.output
    assert "Предложены:     0" in unresearched.output

    _run("buildings-apply", str(_result_file(home)))
    proposed = _run("ai-status")
    assert "Не исследованы: 0" in proposed.output
    assert "Предложены:     1" in proposed.output
    assert "Подтверждены:   0" in proposed.output

    _run("buildings", "set", "the-base-central-phuket", "--policy", "not_allowed")
    confirmed = _run("ai-status")
    assert "Подтверждены:   1" in confirmed.output
