"""Unit tests for photo batch assembly and photo result validation."""

from __future__ import annotations

import json

import pytest

from fb_rent import ai_schema, config, photo_batch, photos, rules, storage


@pytest.fixture()
def conn():
    c = storage.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture()
def registry():
    return ai_schema.load_registry()


@pytest.fixture()
def photos_home(tmp_path, monkeypatch):
    """Point the photo store at a temp directory."""
    root = tmp_path / "photos"
    monkeypatch.setattr(config, "PHOTOS_DIR", root)
    return root


def _shoot(photos_home, fb_id: str, count: int = 3, body: bytes = b"jpeg"):
    directory = photos_home / fb_id
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (directory / f"{index:02d}.jpg").write_bytes(body)
    return directory


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


def _analysed(conn, registry, fb_id: str, fields: dict | None = None, **overrides):
    """Give a listing a text analysis the rules leave `active` by default."""
    _add_listing(conn, fb_id, **overrides)
    data = {
        "pets": {"v": "allowed", "conf": "high", "ev": "Собаки разрешены"},
        "furnished": {"v": "full", "conf": "high", "ev": "fully furnished"},
    }
    if fields is not None:
        data.update(fields)
    storage.upsert_ai_analysis(
        conn,
        {
            "fb_id": fb_id,
            "schema_version": registry.schema_version,
            "analyzed_at": "2026-08-25T10:00:00Z",
            "source_hash": "hash",
            "model": "claude-opus-5",
            "verdict": "Годится.",
            "comment": None,
            "score": 5,
            "data_quality": "good",
            "data_json": data,
        },
    )


# --- build_photo_batch ---------------------------------------------------


def test_build_photo_batch_describes_the_pass(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1", count=2)

    batch = photo_batch.build_photo_batch(
        conn,
        registry,
        requirements_md="письмо агенту",
        photo_analysis_version=7,
        now="2026-08-25T10:00:00Z",
    )

    assert batch["schema_version"] == registry.schema_version
    assert batch["photo_analysis_version"] == 7
    assert batch["generated_at"] == "2026-08-25T10:00:00Z"
    assert batch["requirements_md"] == "письмо агенту"

    (item,) = batch["items"]
    assert item["fb_id"] == "1"
    assert item["url"].endswith("/1")
    assert item["listing_title"] == "2 Bed 1 Bath - House"
    assert item["photos_hash"] == photos.photos_hash("1")
    assert [p.rsplit("/", 1)[-1] for p in item["photos"]] == ["01.jpg", "02.jpg"]


def test_build_photo_batch_carries_only_photo_fields(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    batch = photo_batch.build_photo_batch(conn, registry, now="2026-08-25T10:00:00Z")

    sources = {ai_schema.field_source(f) for f in batch["fields"]}
    assert sources <= {"photo", "both"}
    keys = {f["key"] for f in batch["fields"]}
    assert "condition" in keys and "photo_quality" in keys
    # `pets` is a text-only field: the camera is not asked about it.
    assert "pets" not in keys


def test_build_photo_batch_shows_what_the_text_claimed(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    batch = photo_batch.build_photo_batch(conn, registry, now="2026-08-25T10:00:00Z")

    current = batch["items"][0]["current_fields"]
    # Unwrapped values, photo fields only — `pets` stays out even though the
    # text pass filled it.
    assert current == {"furnished": "full"}


def test_build_photo_batch_skips_rejected_listings(conn, registry, photos_home):
    _analysed(conn, registry, "keep")
    _analysed(conn, registry, "drop", fields={"pets": {"v": "not_allowed"}})
    _shoot(photos_home, "keep")
    _shoot(photos_home, "drop")

    batch = photo_batch.build_photo_batch(conn, registry, now="2026-08-25T10:00:00Z")

    assert [i["fb_id"] for i in batch["items"]] == ["keep"]


def test_build_photo_batch_narrows_to_ids_and_limit(conn, registry, photos_home):
    for fb_id in ("1", "2", "3"):
        _analysed(conn, registry, fb_id)
        _shoot(photos_home, fb_id)

    picked = photo_batch.build_photo_batch(
        conn, registry, ids=["2"], now="2026-08-25T10:00:00Z"
    )
    assert [i["fb_id"] for i in picked["items"]] == ["2"]

    capped = photo_batch.build_photo_batch(
        conn, registry, limit=2, now="2026-08-25T10:00:00Z"
    )
    assert len(capped["items"]) == 2


def test_build_photo_batch_drops_listings_without_photos(conn, registry, photos_home):
    _analysed(conn, registry, "bare")

    batch = photo_batch.build_photo_batch(
        conn,
        registry,
        listings=[{"fb_id": "bare", "url": None, "title": None}],
        now="2026-08-25T10:00:00Z",
    )

    assert batch["items"] == []


# --- write_photo_batch ---------------------------------------------------


def test_write_photo_batch_names_the_file_after_its_timestamp(tmp_path):
    batch = {"generated_at": "2026-08-25T10:00:00Z", "items": []}

    first = photo_batch.write_photo_batch(batch, tmp_path)
    second = photo_batch.write_photo_batch(batch, tmp_path)

    assert first.name == "photo-batch-20260825-100000.json"
    assert second.name == "photo-batch-20260825-100000-2.json"
    assert json.loads(first.read_text(encoding="utf-8"))["items"] == []


# --- validate_photo_results ----------------------------------------------


def _entry(**overrides):
    entry = {
        "fb_id": "1",
        "photos_hash": "abc",
        "fields": {"condition": {"v": "good", "conf": "high", "ev": "фото 2: ремонт"}},
    }
    entry.update(overrides)
    return entry


def test_validate_photo_results_accepts_an_entry_without_a_verdict(registry):
    # The verdict stays the text pass's business — its absence is not an error.
    photo_batch.validate_photo_results([_entry()], registry)


def test_validate_photo_results_requires_fb_id_and_photos_hash(registry):
    with pytest.raises(photo_batch.BatchError, match="missing fb_id"):
        photo_batch.validate_photo_results([_entry(fb_id="")], registry)

    with pytest.raises(photo_batch.BatchError, match="missing photos_hash"):
        photo_batch.validate_photo_results([_entry(photos_hash="")], registry)


def test_validate_photo_results_rejects_non_object_entries(registry):
    with pytest.raises(photo_batch.BatchError, match="must be a JSON object"):
        photo_batch.validate_photo_results(["nope"], registry)

    with pytest.raises(photo_batch.BatchError, match="non-object fields"):
        photo_batch.validate_photo_results([_entry(fields=[])], registry)


def test_validate_photo_results_rejects_unknown_keys(registry):
    with pytest.raises(photo_batch.BatchError, match="unknown field keys"):
        photo_batch.validate_photo_results(
            [_entry(fields={"vibe": {"v": "nice"}})], registry
        )


def test_validate_photo_results_rejects_values_outside_the_vocabulary(registry):
    with pytest.raises(photo_batch.BatchError, match="outside the registry"):
        photo_batch.validate_photo_results(
            [_entry(fields={"photo_quality": {"v": "beautiful"}})], registry
        )


# --- apply_photo_results -------------------------------------------------


def _result(registry, *entries, **overrides):
    result = {
        "schema_version": registry.schema_version,
        "model": "claude-sonnet-5",
        "results": list(entries),
    }
    result.update(overrides)
    return result


def _score_of(conn, fb_id: str):
    return conn.execute(
        "SELECT score FROM ai_analysis WHERE fb_id = ?", (fb_id,)
    ).fetchone()["score"]


def test_apply_photo_results_writes_the_photo_columns(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")
    digest = photos.photos_hash("1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": digest,
                "comment": "Ремонт свежий, но кухня — плитка.",
                "fields": {
                    "condition": {"v": "good", "conf": "high", "ev": "фото 2: ремонт"}
                },
            },
        ),
        now="2026-08-25T12:00:00Z",
    )

    assert report.applied == 1
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert storage.photo_fields_of(dict(row))["condition"]["v"] == "good"
    assert row["photo_comment"] == "Ремонт свежий, но кухня — плитка."
    assert row["photos_hash"] == digest
    assert row["photo_schema_version"] == registry.schema_version
    assert row["photos_analyzed_at"] == "2026-08-25T12:00:00Z"
    assert row["photo_model"] == "claude-sonnet-5"
    # The text pass's own columns are untouched.
    assert row["verdict"] == "Годится."
    assert storage.fields_of(row["data_json"])["pets"]["v"] == "allowed"


def test_apply_photo_results_reports_where_photos_contradicted_the_text(
    conn, registry, photos_home
):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": photos.photos_hash("1"),
                "fields": {
                    # The text said `full`; the pictures show a bare room.
                    "furnished": {"v": "none", "conf": "high", "ev": "фото 1: пусто"},
                    # Nothing in the text to disagree with — not an override.
                    "living_room": {"v": "yes", "conf": "high", "ev": "фото 3"},
                },
            },
        ),
    )

    assert report.overrides == [("1", "furnished", "full", "none")]
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert storage.photo_fields_of(dict(row))["furnished"]["v"] == "none"


def test_apply_photo_results_rescores_from_the_merged_fields(
    conn, registry, photos_home
):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")
    before = _score_of(conn, "1")

    photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": photos.photos_hash("1"),
                "fields": {
                    "living_room": {"v": "yes", "conf": "high", "ev": "фото 3: зал"}
                },
            },
        ),
    )

    after = _score_of(conn, "1")
    assert after != before
    # The merge, not the photo fields alone: `pets: allowed` came from the text.
    assert after == rules.score(
        {
            "pets": {"v": "allowed"},
            "furnished": {"v": "full"},
            "living_room": {"v": "yes"},
        },
        "Kathu, Phuket",
    )


def test_apply_photo_results_can_drop_a_listing_into_rejected(
    conn, registry, photos_home
):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": photos.photos_hash("1"),
                "fields": {
                    "property_type": {
                        "v": "studio",
                        "conf": "high",
                        "ev": "фото 1: одна комната",
                    },
                    "size_sqm": {"v": 18, "conf": "medium", "ev": "фото 1"},
                },
            },
        ),
    )

    assert [fb for fb, _ in report.rejected] == ["1"]
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    merged = {
        **storage.fields_of(row["data_json"]),
        **storage.photo_fields_of(dict(row)),
    }
    status, _reason = rules.decide(merged, 22000, "good")
    assert status == "rejected"


def test_apply_photo_results_dry_run_writes_nothing(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": photos.photos_hash("1"),
                "fields": {"furnished": {"v": "none", "conf": "high", "ev": "фото 1"}},
            },
        ),
        dry_run=True,
    )

    assert report.applied == 1
    assert report.overrides == [("1", "furnished", "full", "none")]
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert row["photo_data_json"] is None
    assert row["photos_analyzed_at"] is None
    assert row["score"] == 5


def test_apply_photo_results_skips_a_stale_photos_hash(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": "hash-of-pictures-long-gone",
                "fields": {"condition": {"v": "worn", "conf": "high", "ev": "фото 2"}},
            },
        ),
    )

    assert report.applied == 0
    assert report.skipped == ["1"]
    assert any("photos_hash" in w for w in report.warnings)
    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert row["photo_data_json"] is None


def test_apply_photo_results_skips_a_listing_without_a_text_analysis(
    conn, registry, photos_home
):
    _add_listing(conn, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {
                "fb_id": "1",
                "photos_hash": photos.photos_hash("1"),
                "fields": {},
            },
        ),
    )

    assert report.skipped == ["1"]
    assert report.applied == 0


def test_apply_photo_results_is_idempotent(conn, registry, photos_home):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")
    entry = {
        "fb_id": "1",
        "photos_hash": photos.photos_hash("1"),
        "fields": {"living_room": {"v": "yes", "conf": "high", "ev": "фото 3"}},
    }

    photo_batch.apply_photo_results(
        conn, registry, _result(registry, entry), now="2026-08-25T12:00:00Z"
    )
    first = dict(
        conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    )
    photo_batch.apply_photo_results(
        conn, registry, _result(registry, entry), now="2026-08-25T12:00:00Z"
    )
    second = dict(
        conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    )

    assert first == second
    # And the queue is empty afterwards — the same round trip run twice.
    pending, _no_photos = storage.fetch_pending_photos(conn, registry)
    assert pending == []


def test_apply_photo_results_collects_proposed_new_fields(
    conn, registry, photos_home
):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    report = photo_batch.apply_photo_results(
        conn,
        registry,
        _result(
            registry,
            {"fb_id": "1", "photos_hash": photos.photos_hash("1"), "fields": {}},
            proposed_new_fields=[
                {"key": "balcony_view", "type": "text", "label": "Вид с балкона"},
                {"key": "condition", "type": "enum", "label": "уже есть"},
            ],
        ),
    )

    assert [p["key"] for p in report.proposed_new_fields] == ["balcony_view"]
    assert any("already exists" in w for w in report.warnings)


def test_apply_photo_results_rejects_the_whole_file_on_an_unknown_key(
    conn, registry, photos_home
):
    _analysed(conn, registry, "1")
    _shoot(photos_home, "1")

    with pytest.raises(photo_batch.BatchError, match="unknown field keys"):
        photo_batch.apply_photo_results(
            conn,
            registry,
            _result(
                registry,
                {
                    "fb_id": "1",
                    "photos_hash": photos.photos_hash("1"),
                    "fields": {"vibe": {"v": "cosy"}},
                },
            ),
        )

    row = conn.execute("SELECT * FROM ai_analysis WHERE fb_id = '1'").fetchone()
    assert row["photo_data_json"] is None
