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

from fb_rent import ai_batch, ai_schema, cli, config, storage
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
