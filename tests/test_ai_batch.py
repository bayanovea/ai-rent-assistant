"""Unit tests for batch assembly and result application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rent_assistant import ai_batch, ai_schema, config, photos, storage


@pytest.fixture()
def conn():
    c = storage.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture()
def registry():
    return ai_schema.load_registry()


def _raw_json(*, description_lines: list[str] | None = None) -> str:
    lines = ["Недавно опубликовано", "16 000 ฿", "Описание"]
    lines += description_lines or ["Дом в посёлке, 2 спальни.", "Собаки разрешены."]
    lines += ["Ещё", "Информация о продавце", "A. Example"]
    return json.dumps({"title": "(6) Marketplace", "text_lines": lines})


def _add_listing(conn, fb_id: str, **overrides):
    listing = {
        "fb_id": fb_id,
        "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
        "title": "2 Bed 1 Bath - House",
        "price_thb": 22000,
        "price_raw": "22 000 ฿/месяц",
        "district": "Kathu, Phuket",
        "description": "Дом в посёлке, 2 спальни.\nСобаки разрешены.",
        "raw_json": _raw_json(),
    }
    listing.update(overrides)
    storage.upsert_listing(conn, listing)
    return listing


# The registry version a freshly produced result would claim. Hard-coding it
# would re-break every apply test on the next `ai-field add`.
_CURRENT_VERSION = ai_schema.load_registry().schema_version


def _result(fb_id: str, *, fields: dict | None = None, **overrides) -> dict:
    entry = {
        "fb_id": fb_id,
        "verdict": "Дом в посёлке, животные разрешены, цена в бюджете.",
        "data_quality": "good",
        "fields": fields
        if fields is not None
        else {
            "pets": {"v": "allowed", "conf": "high", "ev": "Собаки разрешены"},
            "bedrooms": {"v": 2, "conf": "high", "ev": ""},
            "price_month_thb": {"v": 22000, "conf": "high", "ev": ""},
        },
    }
    entry.update(overrides)
    return {
        "schema_version": _CURRENT_VERSION,
        "model": "claude-opus-5 (claude-code)",
        "results": [entry],
    }


# --- build_batch ---------------------------------------------------------


def test_build_batch_carries_registry_and_requirements(conn, registry):
    _add_listing(conn, "1")

    batch = ai_batch.build_batch(conn, registry, "письмо агенту", now="2026-08-25T10:00:00Z")

    assert batch["schema_version"] == registry.schema_version
    assert batch["generated_at"] == "2026-08-25T10:00:00Z"
    assert batch["requirements_md"] == "письмо агенту"
    assert [f["key"] for f in batch["fields"]] == registry.field_keys()
    assert len(batch["items"]) == 1

    item = batch["items"][0]
    assert item["fb_id"] == "1"
    assert item["listing_title"] == "2 Bed 1 Bath - House"
    assert item["price_thb"] == 22000
    assert item["district"] == "Kathu, Phuket"
    assert item["text_lines"]
    assert item["source_hash"]


def test_build_batch_carries_the_listing_source(conn, registry):
    _add_listing(conn, "1")
    _add_listing(
        conn,
        "2",
        url="https://www.facebook.com/groups/phuketrentsale/posts/2/",
        source="group:phuketrentsale",
    )

    batch = ai_batch.build_batch(conn, registry, "")
    by_id = {item["fb_id"]: item for item in batch["items"]}

    assert by_id["1"]["source"] == "marketplace"
    assert by_id["2"]["source"] == "group:phuketrentsale"


def test_build_batch_only_includes_pending(conn, registry):
    _add_listing(conn, "1")
    _add_listing(conn, "2")

    ai_batch.apply_results(conn, registry, _result("1"))
    batch = ai_batch.build_batch(conn, registry, "")

    assert [item["fb_id"] for item in batch["items"]] == ["2"]


def test_build_batch_honours_limit_and_ids(conn, registry):
    for fb_id in ("1", "2", "3"):
        _add_listing(conn, fb_id)

    assert len(ai_batch.build_batch(conn, registry, "", limit=2)["items"]) == 2

    picked = ai_batch.build_batch(conn, registry, "", ids=["2"])
    assert [item["fb_id"] for item in picked["items"]] == ["2"]


def test_build_batch_replaces_sidebar_description_with_text_lines(conn, registry):
    _add_listing(
        conn,
        "1",
        description="Выбор дня\nПохожие объявления рядом с вами\n" + "мусор " * 200,
        raw_json=_raw_json(description_lines=["Настоящий текст объявления."]),
    )

    item = ai_batch.build_batch(conn, registry, "")["items"][0]

    assert item["description"] == "Настоящий текст объявления."
    assert "Выбор дня" not in item["description"]


def test_build_batch_falls_back_to_column_without_markers(conn, registry):
    _add_listing(
        conn,
        "1",
        description="Короткое описание без маркеров.",
        raw_json=json.dumps({"text_lines": ["16 000 ฿", "Kathu"]}),
    )

    item = ai_batch.build_batch(conn, registry, "")["items"][0]

    assert item["description"] == "Короткое описание без маркеров."


def test_write_batch_creates_directory_and_file(conn, registry, tmp_path: Path):
    _add_listing(conn, "1")
    batch = ai_batch.build_batch(conn, registry, "", now="2026-08-25T10:00:00Z")

    path = ai_batch.write_batch(batch, tmp_path / "ai")

    assert path.name == "batch-20260825-100000.json"
    assert json.loads(path.read_text(encoding="utf-8"))["items"][0]["fb_id"] == "1"


# --- apply_results -------------------------------------------------------


def test_apply_results_writes_analysis(conn, registry):
    _add_listing(conn, "1")

    report = ai_batch.apply_results(conn, registry, _result("1"))

    assert (report.inserted, report.updated) == (1, 0)
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert row["schema_version"] == registry.schema_version
    assert row["model"] == "claude-opus-5 (claude-code)"
    assert row["data_quality"] == "good"
    assert row["score"] is not None
    assert json.loads(row["data_json"])["pets"]["v"] == "allowed"


def test_apply_results_updates_on_second_run(conn, registry):
    _add_listing(conn, "1")
    ai_batch.apply_results(conn, registry, _result("1"))

    report = ai_batch.apply_results(conn, registry, _result("1", verdict="Пересмотрено."))

    assert (report.inserted, report.updated) == (0, 1)
    verdict = conn.execute(
        "SELECT verdict FROM ai_analysis WHERE fb_id = '1'"
    ).fetchone()["verdict"]
    assert verdict == "Пересмотрено."


def test_apply_results_reports_auto_rejections(conn, registry):
    _add_listing(conn, "1")

    report = ai_batch.apply_results(
        conn,
        registry,
        _result("1", fields={"pets": {"v": "not_allowed", "ev": "no pets"}}),
    )

    assert report.rejected == [("1", "Животные запрещены")]


def test_apply_results_purges_photos_of_a_rejected_listing(
    conn, registry, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    _add_listing(conn, "1")
    _add_listing(conn, "2")
    for fb_id in ("1", "2"):
        directory = photos.photos_dir(fb_id)
        directory.mkdir(parents=True)
        (directory / "01.jpg").write_bytes(b"jpeg")

    rejected = _result("1", fields={"pets": {"v": "not_allowed", "ev": "no pets"}})
    kept = _result("2")
    ai_batch.apply_results(
        conn,
        registry,
        {**kept, "results": rejected["results"] + kept["results"]},
    )

    assert not photos.photos_dir("1").exists()
    assert photos.photos_dir("2").exists()


def test_apply_results_dry_run_keeps_photos_of_a_rejected_listing(
    conn, registry, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    _add_listing(conn, "1")
    directory = photos.photos_dir("1")
    directory.mkdir(parents=True)
    (directory / "01.jpg").write_bytes(b"jpeg")

    ai_batch.apply_results(
        conn,
        registry,
        _result("1", fields={"pets": {"v": "not_allowed", "ev": "no pets"}}),
        dry_run=True,
    )

    assert photos.photos_dir("1").exists()


def test_apply_results_rejects_unknown_field_keys(conn, registry):
    _add_listing(conn, "1")
    result = _result(
        "1",
        fields={"pets": {"v": "unclear"}, "electricity_rate": {"v": "7"}, "wifi": {"v": "yes"}},
    )

    with pytest.raises(ai_batch.BatchError) as exc:
        ai_batch.apply_results(conn, registry, result)

    message = str(exc.value)
    assert "electricity_rate" in message and "wifi" in message
    assert conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()[0] == 0


def test_apply_results_collects_proposed_new_fields(conn, registry):
    _add_listing(conn, "1")
    result = _result("1")
    result["proposed_new_fields"] = [
        {"key": "electricity_rate", "label": "Свет ฿/kWh", "type": "text", "why": "3 из 8"},
        {"key": "pets", "label": "Pets", "type": "enum"},
    ]

    report = ai_batch.apply_results(conn, registry, result)

    assert [f["key"] for f in report.proposed_new_fields] == ["electricity_rate"]
    assert any("pets" in w for w in report.warnings)


def test_apply_results_warns_on_source_hash_mismatch(conn, registry):
    _add_listing(conn, "1")

    report = ai_batch.apply_results(conn, registry, _result("1", source_hash="stale"))

    assert report.inserted == 1
    assert any("source_hash" in w for w in report.warnings)
    assert conn.execute(
        "SELECT source_hash FROM ai_analysis WHERE fb_id = '1'"
    ).fetchone()["source_hash"] == "stale"


def test_apply_results_matching_hash_clears_pending(conn, registry):
    _add_listing(conn, "1")
    batch = ai_batch.build_batch(conn, registry, "")
    hash_ = batch["items"][0]["source_hash"]

    ai_batch.apply_results(conn, registry, _result("1", source_hash=hash_))

    assert storage.fetch_pending(conn, registry).pending == []


def test_apply_results_skips_unknown_listing(conn, registry):
    report = ai_batch.apply_results(conn, registry, _result("404"))

    assert report.skipped == ["404"]
    assert report.applied == 0


def test_apply_results_dry_run_writes_nothing(conn, registry):
    _add_listing(conn, "1")

    report = ai_batch.apply_results(conn, registry, _result("1"), dry_run=True)

    assert report.dry_run and report.applied == 1
    assert conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()[0] == 0


@pytest.mark.parametrize(
    "result",
    [
        {"results": []},
        {"schema_version": "1", "results": []},
        {"schema_version": 1},
        {"schema_version": 1, "results": [{"verdict": "нет id"}]},
        {"schema_version": 1, "results": [{"fb_id": "1", "verdict": "  "}]},
    ],
)
def test_apply_results_rejects_malformed_envelope(conn, registry, result):
    with pytest.raises(ai_batch.BatchError):
        ai_batch.apply_results(conn, registry, result)


def test_apply_results_rejects_newer_schema_version(conn, registry):
    result = _result("1")
    result["schema_version"] = registry.schema_version + 1

    with pytest.raises(ai_batch.BatchError):
        ai_batch.apply_results(conn, registry, result)


# --- schema versions, values and selection ------------------------------


def _registry_with_extra_field(tmp_path, registry):
    """A copy of the real registry with one more field, one version up."""
    raw = json.loads(ai_schema.config.AI_FIELDS_PATH.read_text(encoding="utf-8"))
    version = int(raw["schema_version"]) + 1
    raw["schema_version"] = version
    raw["fields"].append(
        {
            "key": "balcony",
            "label": "Балкон",
            "group": "amenity",
            "type": "enum",
            "values": ["yes", "no", "unclear"],
            "added_in": version,
            "prompt": "Есть ли балкон?",
        }
    )
    target = tmp_path / "ai_fields.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return ai_schema.load_registry(target)


def test_apply_results_stores_the_version_the_ai_saw(conn, tmp_path, registry):
    """A result produced before `ai-field add` must not claim the new version."""
    _add_listing(conn, "1")
    newer = _registry_with_extra_field(tmp_path, registry)
    result = _result("1")  # built before `balcony` existed

    report = ai_batch.apply_results(conn, newer, result)

    assert any("stay queued" in w for w in report.warnings)
    stored = conn.execute(
        "SELECT schema_version FROM ai_analysis WHERE fb_id = '1'"
    ).fetchone()["schema_version"]
    assert stored == result["schema_version"]
    # ... and the row is therefore still queued for the field it never saw.
    pending, _no_raw = storage.fetch_pending(conn, newer)
    assert [row["fb_id"] for row in pending] == ["1"]
    assert pending[0]["pending_reason"] == "schema"


def test_apply_results_rejects_values_outside_the_registry(conn, registry):
    _add_listing(conn, "1")
    result = _result("1", fields={"pets": {"v": "not allowed", "conf": "high"}})

    with pytest.raises(ai_batch.BatchError) as exc:
        ai_batch.apply_results(conn, registry, result)

    assert "pets" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()[0] == 0


def test_apply_results_accepts_declared_values_and_empty_ones(conn, registry):
    _add_listing(conn, "1")
    result = _result(
        "1", fields={"pets": {"v": "unclear"}, "parking": {"v": None}}
    )

    assert ai_batch.apply_results(conn, registry, result).inserted == 1


def test_bad_data_quality_blocks_the_auto_rejection(conn, registry):
    _add_listing(conn, "1")
    result = _result(
        "1",
        data_quality="sidebar_noise",
        fields={"pets": {"v": "not_allowed", "ev": "из сайдбара"}},
    )

    report = ai_batch.apply_results(conn, registry, result)

    assert report.rejected == []
    item = next(i for i in storage.fetch_enriched(conn) if i["fb_id"] == "1")
    assert item["status"] == "active"


def test_dry_run_counts_an_existing_analysis_as_an_update(conn, registry):
    _add_listing(conn, "1")
    ai_batch.apply_results(conn, registry, _result("1"))

    report = ai_batch.apply_results(conn, registry, _result("1"), dry_run=True)

    assert (report.inserted, report.updated) == (0, 1)


def test_build_batch_takes_the_rows_the_caller_selected(conn, registry):
    """`--ids` reaches past the queue, so the rows arrive pre-selected."""
    _add_listing(conn, "1")
    ai_batch.apply_results(conn, registry, _result("1"))
    row = dict(conn.execute("SELECT * FROM listings WHERE fb_id = '1'").fetchone())

    assert ai_batch.build_batch(conn, registry, "")["items"] == []
    batch = ai_batch.build_batch(conn, registry, "", listings=[row])
    assert [item["fb_id"] for item in batch["items"]] == ["1"]
    # A row read straight from `listings` carries no hash; one is computed.
    assert batch["items"][0]["source_hash"]


def test_build_batch_limit_zero_selects_nothing(conn, registry):
    _add_listing(conn, "1")
    _add_listing(conn, "2")

    assert ai_batch.build_batch(conn, registry, "", limit=0)["items"] == []
    assert len(ai_batch.build_batch(conn, registry, "", limit=1)["items"]) == 1


def test_write_batch_never_overwrites_a_batch_from_the_same_second(conn, registry, tmp_path):
    _add_listing(conn, "1")
    batch = ai_batch.build_batch(conn, registry, "", now="2026-08-25T10:00:00Z")

    first = ai_batch.write_batch(batch, tmp_path / "ai")
    second = ai_batch.write_batch(batch, tmp_path / "ai")

    assert first != second
    assert first.exists() and second.exists()


def test_write_batch_falls_back_when_generated_at_is_not_a_timestamp(tmp_path):
    path = ai_batch.write_batch({"generated_at": "today", "items": []}, tmp_path)

    assert path.name.startswith("batch-")
    assert path.name != "batch--.json"
    assert len(path.stem.split("-")[1]) == 8
