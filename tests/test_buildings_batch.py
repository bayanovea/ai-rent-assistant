"""Unit tests for the pets-policy batch and its apply step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rent_assistant import buildings, buildings_batch, storage


@pytest.fixture()
def conn():
    c = storage.init_db(":memory:")
    yield c
    c.close()


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "buildings.json"


def _in_building(conn, fb_id: str, project: str, *, pets="unclear",
                 area="Phuket Town", price=22000):
    """One analysed listing advertising a flat in ``project``."""
    storage.upsert_listing(
        conn,
        {
            "fb_id": fb_id,
            "url": f"https://www.facebook.com/marketplace/item/{fb_id}",
            "title": "2 Bed 1 Bath - Condo",
            "price_thb": price,
            "price_raw": f"{price} ฿/месяц",
            "district": "Phuket Town",
            "description": "Квартира в кондо.",
        },
        now="2026-08-25T10:00:00Z",
    )
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
                "area": {"v": area},
                "pets": {"v": pets},
            },
        },
    )


def _sources():
    return [
        {
            "url": "https://example.com/rules",
            "title": "The Base Central — House Rules",
            "quote": "Pets are not allowed in the building.",
        }
    ]


def _result(**overrides):
    entry = {
        "key": "the-base-central-phuket",
        "name": "The Base Central Phuket",
        "area": "Phuket Town",
        "policy": "not_allowed",
        "confidence": "high",
        "note": "УК: no pets of any kind.",
        "sources": _sources(),
    }
    entry.update(overrides)
    return {"model": "claude-opus-5 (claude-code)", "results": [entry]}


# --- batch assembly ---------------------------------------------------------


def test_batch_carries_the_queue_and_the_research_prompt(conn):
    # An ad claiming "pet friendly" is exactly the contradiction the queue is
    # meant to surface; one saying "not_allowed" is already auto-rejected and
    # never reaches it.
    _in_building(conn, "1", "The Base Central Phuket", pets="allowed")
    _in_building(conn, "2", "The Base Central Phuket")

    batch = buildings_batch.build_buildings_batch(conn, {})

    assert [b["key"] for b in batch["buildings"]] == ["the-base-central-phuket"]
    row = batch["buildings"][0]
    assert row["listings"] == 2
    assert row["pets"] == ["allowed", "unclear"]
    assert sorted(batch["policies"]) == [
        "allowed",
        "negotiable",
        "not_allowed",
        "unknown",
    ]
    for value in buildings.POLICIES:
        assert value in batch["research_prompt"]
    assert "цитат" in batch["research_prompt"]
    assert batch["generated_at"]


def test_batch_limit_keeps_the_biggest_buildings(conn):
    _in_building(conn, "1", "The Base Central Phuket")
    _in_building(conn, "2", "The Base Central Phuket")
    _in_building(conn, "3", "Zcape III")

    batch = buildings_batch.build_buildings_batch(conn, {}, limit=1)

    assert [b["key"] for b in batch["buildings"]] == ["the-base-central-phuket"]


def test_batch_limit_zero_is_an_empty_batch(conn):
    _in_building(conn, "1", "The Base Central Phuket")
    assert buildings_batch.build_buildings_batch(conn, {}, limit=0)["buildings"] == []


def test_batch_keys_narrow_the_queue_and_are_normalised(conn):
    _in_building(conn, "1", "The Base Central Phuket")
    _in_building(conn, "2", "Zcape III")

    batch = buildings_batch.build_buildings_batch(conn, {}, keys=["Zcape III"])

    assert [b["key"] for b in batch["buildings"]] == ["zcape-iii"]


def test_batch_of_an_empty_queue_is_empty_not_an_error(conn):
    batch = buildings_batch.build_buildings_batch(conn, {})
    assert batch["buildings"] == []
    assert batch["research_prompt"]


def test_batch_skips_what_is_already_researched_unless_refreshed(conn):
    _in_building(conn, "1", "The Base Central Phuket")
    store = {
        "the-base-central-phuket": {
            "name": "The Base Central Phuket",
            "policy": "unknown",
            "status": "proposed",
        }
    }

    assert buildings_batch.build_buildings_batch(conn, store)["buildings"] == []
    refreshed = buildings_batch.build_buildings_batch(conn, store, refresh=True)
    assert [b["key"] for b in refreshed["buildings"]] == ["the-base-central-phuket"]


def test_batch_reads_the_store_from_disk_when_not_given_one(conn, path, monkeypatch):
    monkeypatch.setattr(buildings.config, "BUILDINGS_PATH", path)
    _in_building(conn, "1", "The Base Central Phuket")
    buildings.save_building(
        path, "The Base Central Phuket", policy="unknown", status="proposed"
    )

    assert buildings_batch.build_buildings_batch(conn)["buildings"] == []


def test_write_batch_names_the_file_after_its_timestamp(conn, tmp_path: Path):
    _in_building(conn, "1", "The Base Central Phuket")
    batch = buildings_batch.build_buildings_batch(conn, {}, now="2026-08-27T10:00:00Z")

    written = buildings_batch.write_buildings_batch(batch, tmp_path)

    assert written.name == "buildings-batch-20260827-100000.json"
    assert json.loads(written.read_text(encoding="utf-8"))["buildings"][0]["key"] == (
        "the-base-central-phuket"
    )


def test_write_batch_does_not_overwrite_a_batch_from_the_same_second(
    conn, tmp_path: Path
):
    batch = buildings_batch.build_buildings_batch(conn, {}, now="2026-08-27T10:00:00Z")
    first = buildings_batch.write_buildings_batch(batch, tmp_path)
    second = buildings_batch.write_buildings_batch(batch, tmp_path)
    assert first != second
    assert first.exists() and second.exists()


# --- validation -------------------------------------------------------------


def test_validate_accepts_a_clean_file():
    buildings_batch.validate_buildings_results(_result()["results"])


def test_validate_rejects_a_non_list():
    with pytest.raises(buildings_batch.BatchError, match="results"):
        buildings_batch.validate_buildings_results({"key": "x"})


def test_validate_rejects_a_non_object_entry():
    with pytest.raises(buildings_batch.BatchError, match="JSON object"):
        buildings_batch.validate_buildings_results(["the-base"])


@pytest.mark.parametrize(
    "entry",
    [
        {"policy": "unknown"},
        {"key": "  ", "policy": "unknown"},
        {"key": "!!!", "policy": "unknown"},
    ],
)
def test_validate_rejects_a_missing_key(entry):
    with pytest.raises(buildings_batch.BatchError, match="missing key"):
        buildings_batch.validate_buildings_results([entry])


def test_validate_rejects_an_unknown_policy():
    with pytest.raises(buildings_batch.BatchError, match="unknown pets policy"):
        buildings_batch.validate_buildings_results(
            _result(policy="no pets")["results"]
        )


def test_validate_rejects_an_unknown_confidence():
    with pytest.raises(buildings_batch.BatchError, match="unknown confidence"):
        buildings_batch.validate_buildings_results(
            _result(confidence="certain")["results"]
        )


@pytest.mark.parametrize(
    "sources",
    [
        None,
        [],
        [{"url": "https://example.com/rules", "title": "House Rules"}],
        [{"quote": "Pets are not allowed."}],
        [{"url": "", "quote": ""}],
        ["https://example.com/rules"],
    ],
)
def test_validate_rejects_not_allowed_without_evidence(sources):
    with pytest.raises(buildings_batch.BatchError, match="not_allowed"):
        buildings_batch.validate_buildings_results(
            _result(sources=sources)["results"]
        )


def test_validate_allows_a_sourceless_unknown():
    buildings_batch.validate_buildings_results(
        _result(policy="unknown", sources=[])["results"]
    )


def test_validate_allows_a_sourceless_negotiable():
    buildings_batch.validate_buildings_results(
        _result(policy="negotiable", sources=[])["results"]
    )


# --- apply ------------------------------------------------------------------


def test_apply_writes_the_entry_as_a_proposal(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    store = buildings.load_buildings(path)

    report = buildings_batch.apply_buildings_results(
        conn, store, _result(), path=path, now="2026-08-27T10:00:00Z"
    )

    assert report.applied == 1
    assert report.skipped == []
    saved = buildings.load_buildings(path)["the-base-central-phuket"]
    assert saved["policy"] == "not_allowed"
    assert saved["status"] == "proposed"
    assert saved["confirmed_at"] is None
    assert saved["confidence"] == "high"
    assert saved["sources"] == _sources()
    assert saved["researched_by"] == "claude-opus-5 (claude-code)"
    assert saved["researched_at"] == "2026-08-27T10:00:00Z"


def test_apply_mirrors_the_proposal_into_the_table(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path
    )

    row = conn.execute("SELECT * FROM buildings").fetchone()
    assert row["key"] == "the-base-central-phuket"
    assert row["status"] == "proposed"
    # ...and stays out of the map that can act on a listing.
    assert storage.building_policies(conn) == {}


def test_apply_counts_the_listings_a_proposal_would_reject(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    _in_building(conn, "2", "The Base Central Phuket")
    _in_building(conn, "3", "Zcape III")

    report = buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path
    )

    assert report.affected == {"the-base-central-phuket": 2}


def test_apply_never_changes_a_listing(conn, path):
    _in_building(conn, "1", "The Base Central Phuket", pets="allowed")
    before = storage.fetch_enriched(conn)[0]

    buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path
    )

    after = storage.fetch_enriched(conn)[0]
    assert after["status"] == before["status"] == "active"
    assert after["reason"] == before["reason"]
    assert after["score"] == before["score"]
    # the ad's own claim is untouched by research
    assert after["fields"]["pets"]["v"] == "allowed"


def test_apply_skips_a_building_the_user_already_confirmed(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    buildings.save_building(
        path,
        "The Base Central Phuket",
        policy="negotiable",
        status="confirmed",
        note="Спросил агента: до 10 кг можно.",
    )
    store = buildings.load_buildings(path)

    report = buildings_batch.apply_buildings_results(
        conn, store, _result(), path=path
    )

    assert report.applied == 0
    assert report.skipped == ["the-base-central-phuket"]
    assert any("already confirmed" in w for w in report.warnings)
    saved = buildings.load_buildings(path)["the-base-central-phuket"]
    assert saved["policy"] == "negotiable"
    assert saved["status"] == "confirmed"
    assert saved["confirmed_at"]


def test_apply_re_proposes_over_an_earlier_proposal(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    buildings.save_building(
        path, "The Base Central Phuket", policy="unknown", status="proposed"
    )

    report = buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path
    )

    assert report.applied == 1
    assert buildings.load_buildings(path)["the-base-central-phuket"]["policy"] == (
        "not_allowed"
    )


def test_apply_keeps_hand_made_aliases(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    buildings.save_building(
        path,
        "The Base Central Phuket",
        policy="unknown",
        status="proposed",
        aliases=["Base Central"],
    )

    buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path
    )

    assert buildings.load_buildings(path)["the-base-central-phuket"]["aliases"] == [
        "Base Central"
    ]


def test_apply_dry_run_writes_nothing_but_still_counts(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    _in_building(conn, "2", "The Base Central Phuket")

    report = buildings_batch.apply_buildings_results(
        conn, buildings.load_buildings(path), _result(), path=path, dry_run=True
    )

    assert report.dry_run is True
    assert report.applied == 1
    assert report.affected == {"the-base-central-phuket": 2}
    assert not path.exists()
    assert conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0] == 0


def test_apply_rejects_the_whole_file_before_writing_anything(conn, path):
    _in_building(conn, "1", "The Base Central Phuket")
    result = _result()
    result["results"].append({"key": "zcape-iii", "policy": "нельзя"})

    with pytest.raises(buildings_batch.BatchError):
        buildings_batch.apply_buildings_results(
            conn, buildings.load_buildings(path), result, path=path
        )

    assert not path.exists()


def test_apply_rejects_a_non_object_result(conn, path):
    with pytest.raises(buildings_batch.BatchError, match="JSON object"):
        buildings_batch.apply_buildings_results(conn, {}, ["nope"], path=path)


def test_apply_of_an_empty_file_does_nothing(conn, path):
    report = buildings_batch.apply_buildings_results(
        conn, {}, {"results": []}, path=path
    )
    assert report.applied == 0
    assert report.affected == {}
