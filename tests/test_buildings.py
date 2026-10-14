"""Tests for the file-backed per-building pets policy store."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rent_assistant import buildings as bld


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "buildings.json"


def _sources() -> list[dict[str, str]]:
    return [
        {
            "url": "https://example.com/rules",
            "title": "The Base Central — House Rules",
            "quote": "Pets are not allowed in the building.",
        }
    ]


# --- building_key -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The Base Central Phuket", "the-base-central-phuket"),
        ("the base central phuket", "the-base-central-phuket"),
        ("  The   BASE  Central   Phuket ", "the-base-central-phuket"),
        ("The Base Central, Phuket", "the-base-central-phuket"),
        ("The Base Central (Phuket)", "the-base-central-phuket"),
        ("The Base — Central / Phuket", "the-base-central-phuket"),
        ("Zcape III", "zcape-iii"),
        ("D Condo Mine 2", "d-condo-mine-2"),
    ],
)
def test_building_key_collapses_spelling_variants(name: str, expected: str):
    assert bld.building_key(name) == expected


def test_building_key_keeps_cyrillic():
    assert bld.building_key("Кондо «Волна», Раваи") == "кондо-волна-раваи"
    assert bld.building_key("КОНДО Волна") == bld.building_key("кондо волна")


@pytest.mark.parametrize("name", ["", "   ", None, "!!!", "—"])
def test_building_key_empty_input(name):
    assert bld.building_key(name) == ""


def test_building_key_does_not_fuzzy_match():
    # Deliberately dumb: two spellings of the same condo stay two keys until
    # an alias says otherwise.
    assert bld.building_key("The Base") != bld.building_key("Base Central")


# --- load / save ------------------------------------------------------------


def test_missing_file_is_empty(path: Path):
    assert bld.load_buildings(path) == {}


def test_save_and_load_round_trip(path: Path):
    entry = bld.save_building(
        path,
        "The Base Central Phuket",
        policy="not_allowed",
        area="Phuket Town",
        confidence="high",
        note="Управляющая компания: no pets.",
        sources=_sources(),
        researched_by="claude-opus-5",
    )

    assert entry["status"] == "proposed"
    assert entry["confirmed_at"] is None
    assert entry["researched_at"].endswith("Z")

    loaded = bld.load_buildings(path)
    assert set(loaded) == {"the-base-central-phuket"}
    stored = loaded["the-base-central-phuket"]
    assert stored["name"] == "The Base Central Phuket"
    assert stored["policy"] == "not_allowed"
    assert stored["area"] == "Phuket Town"
    assert stored["sources"][0]["quote"] == "Pets are not allowed in the building."


def test_saved_file_is_readable_json(path: Path):
    bld.save_building(path, "Кондо Волна", policy="negotiable")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["кондо-волна"]["policy"] == "negotiable"
    # Written for a human to open and edit by hand.
    assert "Кондо Волна" in path.read_text(encoding="utf-8")
    assert oct(path.stat().st_mode)[-3:] == "644"


def test_save_keeps_other_buildings(path: Path):
    bld.save_building(path, "The Base", policy="not_allowed", sources=_sources())
    bld.save_building(path, "Zcape III", policy="allowed")

    loaded = bld.load_buildings(path)
    assert loaded["the-base"]["policy"] == "not_allowed"
    assert loaded["zcape-iii"]["policy"] == "allowed"


def test_confirming_keeps_the_research_behind_it(path: Path):
    bld.save_building(
        path,
        "The Base",
        policy="not_allowed",
        confidence="high",
        note="Из правил дома",
        sources=_sources(),
    )
    confirmed = bld.save_building(
        path, "The Base", policy="not_allowed", status="confirmed"
    )

    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_at"].endswith("Z")
    # Fields left as None keep what the proposal researched.
    assert confirmed["sources"] == _sources()
    assert confirmed["note"] == "Из правил дома"
    assert confirmed["confidence"] == "high"


def test_aliases_accumulate_and_never_drop(path: Path):
    bld.save_building(path, "The Base", policy="unknown", aliases=["base central"])
    entry = bld.save_building(
        path, "The Base", policy="unknown", aliases=["the base condo phuket"]
    )
    assert entry["aliases"] == ["base central", "the base condo phuket"]

    entry = bld.save_building(path, "The Base", policy="unknown")
    assert entry["aliases"] == ["base central", "the base condo phuket"]


def test_reproposing_retracts_the_confirmation(path: Path):
    bld.save_building(path, "The Base", policy="allowed", status="confirmed")
    entry = bld.save_building(path, "The Base", policy="not_allowed")
    assert entry["status"] == "proposed"
    assert entry["confirmed_at"] is None


def test_new_sources_restamp_researched_at(path: Path):
    bld.save_building(
        path, "The Base", policy="unknown", now="2026-01-01T00:00:00Z"
    )
    entry = bld.save_building(
        path,
        "The Base",
        policy="not_allowed",
        sources=_sources(),
        now="2026-02-02T00:00:00Z",
    )
    assert entry["researched_at"] == "2026-02-02T00:00:00Z"


def test_invalid_policy_rejected(path: Path):
    with pytest.raises(bld.BuildingError, match="unknown pets policy"):
        bld.save_building(path, "The Base", policy="maybe")
    assert not path.exists()


def test_invalid_status_rejected(path: Path):
    with pytest.raises(bld.BuildingError, match="unknown building status"):
        bld.save_building(path, "The Base", policy="allowed", status="vetted")
    assert not path.exists()


def test_invalid_confidence_rejected(path: Path):
    with pytest.raises(bld.BuildingError, match="unknown confidence"):
        bld.save_building(path, "The Base", policy="allowed", confidence="sure")
    assert not path.exists()


def test_empty_name_rejected(path: Path):
    with pytest.raises(bld.BuildingError, match="empty key"):
        bld.save_building(path, "   ", policy="allowed")
    assert not path.exists()


def test_malformed_source_rejected(path: Path):
    with pytest.raises(bld.BuildingError, match="source must be an object"):
        bld.save_building(
            path, "The Base", policy="not_allowed", sources=["https://example.com"]
        )
    assert not path.exists()


def test_failed_write_leaves_the_old_file_intact(path: Path, monkeypatch):
    bld.save_building(path, "The Base", policy="allowed", status="confirmed")
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        bld.save_building(path, "The Base", policy="not_allowed")

    assert path.read_text(encoding="utf-8") == before
    assert bld.load_buildings(path)["the-base"]["policy"] == "allowed"
    # And no half-written temp file left behind next to it.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_load_rejects_a_non_object_file(path: Path):
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        bld.load_buildings(path)


def test_load_rejects_broken_json(path: Path):
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        bld.load_buildings(path)


def test_load_skips_non_object_entries(path: Path):
    path.write_text('{"the-base": {"policy": "allowed"}, "junk": 5}', encoding="utf-8")
    assert set(bld.load_buildings(path)) == {"the-base"}


def test_default_path_comes_from_config(tmp_path: Path, monkeypatch):
    target = tmp_path / "nested" / "buildings.json"
    monkeypatch.setattr(bld.config, "BUILDINGS_PATH", target)
    bld.save_building(None, "The Base", policy="allowed")
    assert bld.load_buildings() == bld.load_buildings(target)
    assert target.exists()


def test_delete_building(path: Path):
    bld.save_building(path, "The Base", policy="allowed")
    assert bld.delete_building(path, "the-base") is True
    assert bld.load_buildings(path) == {}
    assert bld.delete_building(path, "the-base") is False


# --- resolve / policy_for ---------------------------------------------------


def test_resolve_by_exact_key(path: Path):
    bld.save_building(path, "The Base Central Phuket", policy="allowed")
    store = bld.load_buildings(path)
    assert resolve_name(store, "the base central   phuket") == "The Base Central Phuket"


def resolve_name(store, project_name):
    entry = bld.resolve(store, project_name)
    return entry and entry["name"]


def test_resolve_by_alias(path: Path):
    bld.save_building(
        path,
        "The Base Central Phuket",
        policy="not_allowed",
        aliases=["Base Central", "The Base Condo, Phuket"],
    )
    store = bld.load_buildings(path)

    assert resolve_name(store, "base central") == "The Base Central Phuket"
    assert resolve_name(store, "The Base Condo (Phuket)") == "The Base Central Phuket"
    assert bld.resolve(store, "The Base Downtown") is None


def test_resolve_misses_on_empty_or_unknown(path: Path):
    store = bld.load_buildings(path)
    assert bld.resolve(store, None) is None
    assert bld.resolve(store, "") is None
    assert bld.resolve({}, "The Base") is None


def test_policy_for_confirmed_building(path: Path):
    bld.save_building(
        path, "The Base", policy="not_allowed", status="confirmed", sources=_sources()
    )
    store = bld.load_buildings(path)
    assert bld.policy_for(store, "the  base") == "not_allowed"


def test_policy_for_is_none_on_a_proposal(path: Path):
    bld.save_building(
        path, "The Base", policy="not_allowed", sources=_sources()
    )
    store = bld.load_buildings(path)
    assert bld.resolve(store, "The Base")["policy"] == "not_allowed"
    # A proposal is inert: no web search on its own may reject a row.
    assert bld.policy_for(store, "The Base") is None


def test_policy_for_is_none_on_confirmed_unknown(path: Path):
    bld.save_building(path, "The Base", policy="unknown", status="confirmed")
    store = bld.load_buildings(path)
    assert bld.policy_for(store, "The Base") is None


def test_policy_for_negotiable_reaches_the_caller(path: Path):
    bld.save_building(path, "The Base", policy="negotiable", status="confirmed")
    store = bld.load_buildings(path)
    assert bld.policy_for(store, "The Base") == "negotiable"


def test_policy_for_unresearched_building(path: Path):
    bld.save_building(path, "The Base", policy="not_allowed", status="confirmed")
    store = bld.load_buildings(path)
    assert bld.policy_for(store, "Zcape III") is None
    assert bld.policy_for(store, None) is None
