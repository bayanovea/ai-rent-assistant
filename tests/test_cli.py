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

from fb_rent import ai_batch, ai_schema, cli, config, photos, storage
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
    assert "scrape --refresh" in result.output
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
        lambda ctx, url, max_items: [
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


# --- scrape: detail budget and partial writes ---------------------------


def _card(fb_id: str) -> dict:
    return {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
        "title": "2 Bed 1 Bath - House",
        "price_raw": "22 000 ฿",
    }


def _stub_feed(monkeypatch, cards, on_detail):
    """Browser stack serving a fixed feed and a scripted detail page."""

    class _Context:
        def close(self) -> None:
            pass

    class _Playwright:
        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "start_playwright", lambda: _Playwright())
    monkeypatch.setattr(cli, "launch_persistent", lambda pw, headless=True: _Context())
    monkeypatch.setattr(cli, "is_logged_in", lambda ctx: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli, "scrape_list", lambda ctx, url, max_items: list(cards))
    monkeypatch.setattr(cli, "scrape_detail", lambda ctx, url: on_detail(url))


def _detail(url: str) -> dict:
    return {
        "url": url,
        "title": "2 Bed 1 Bath - House",
        "description": f"Дом в посёлке, объявление {url.rsplit('/', 1)[-1]}.",
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
