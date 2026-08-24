"""Tests for the AI field registry loader and the staleness hash."""

from __future__ import annotations

import json

import pytest

from fb_rent import config
from fb_rent.ai_schema import RegistryError, load_registry, source_hash


def _write(tmp_path, registry: dict):
    path = tmp_path / "ai_fields.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return path


def _valid() -> dict:
    return {
        "schema_version": 1,
        "fields": [
            {
                "key": "pets",
                "label": "Pets",
                "group": "core",
                "type": "enum",
                "values": ["allowed", "not_allowed", "unclear"],
                "added_in": 1,
                "prompt": "...",
            },
            {
                "key": "size_sqm",
                "label": "Площадь м²",
                "group": "property",
                "type": "int",
                "added_in": 1,
                "prompt": "...",
            },
        ],
    }


# --- the real registry shipped in config/ ------------------------------


def test_real_registry_loads_and_validates():
    registry = load_registry()
    assert registry.schema_version == 1
    assert len(registry.fields) == 28


def test_real_registry_covers_the_documented_fields():
    keys = load_registry().field_keys()
    assert keys[0] == "pets"  # criterion #1 comes first
    for key in ("pets_evidence", "price_month_thb", "property_type", "contact"):
        assert key in keys
    assert len(keys) == len(set(keys))


def test_real_registry_fields_all_carry_a_prompt():
    for field in load_registry().fields:
        assert field["prompt"].strip(), field["key"]
        assert field["group"] in {"core", "property", "amenity", "meta"}


def test_requirements_file_exists_and_flags_pets_first():
    text = config.REQUIREMENTS_PATH.read_text(encoding="utf-8")
    assert "критерий №1" in text
    assert "1 декабря 2026" in text


# --- lookups -----------------------------------------------------------


def test_field_lookup_and_unknown_keys():
    registry = load_registry()
    assert registry.field("pets")["type"] == "enum"
    assert registry.field("nope") is None
    assert registry.unknown_keys(["pets", "zzz", "qqq"]) == ["zzz", "qqq"]


# --- validation --------------------------------------------------------


def test_duplicate_key_rejected(tmp_path):
    bad = _valid()
    bad["fields"].append(dict(bad["fields"][0]))
    with pytest.raises(RegistryError, match="duplicate field key: pets"):
        load_registry(_write(tmp_path, bad))


def test_enum_without_values_rejected(tmp_path):
    bad = _valid()
    del bad["fields"][0]["values"]
    with pytest.raises(RegistryError, match="non-empty values"):
        load_registry(_write(tmp_path, bad))


def test_enum_with_empty_values_rejected(tmp_path):
    bad = _valid()
    bad["fields"][0]["values"] = []
    with pytest.raises(RegistryError, match="non-empty values"):
        load_registry(_write(tmp_path, bad))


def test_unknown_type_rejected(tmp_path):
    bad = _valid()
    bad["fields"][1]["type"] = "float"
    with pytest.raises(RegistryError, match="unknown type"):
        load_registry(_write(tmp_path, bad))


def test_added_in_beyond_schema_version_rejected(tmp_path):
    bad = _valid()
    bad["fields"][1]["added_in"] = 2
    with pytest.raises(RegistryError, match="added_in=2 > schema_version=1"):
        load_registry(_write(tmp_path, bad))


def test_missing_label_rejected(tmp_path):
    bad = _valid()
    del bad["fields"][0]["label"]
    with pytest.raises(RegistryError, match="needs a label"):
        load_registry(_write(tmp_path, bad))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "nope.json")


def test_broken_json_rejected(tmp_path):
    path = tmp_path / "ai_fields.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(path)


def test_empty_fields_rejected(tmp_path):
    with pytest.raises(RegistryError, match="non-empty list"):
        load_registry(_write(tmp_path, {"schema_version": 1, "fields": []}))


def test_non_integer_schema_version_rejected(tmp_path):
    with pytest.raises(RegistryError, match="schema_version must be an integer"):
        load_registry(_write(tmp_path, {"schema_version": "1", "fields": _valid()["fields"]}))


# --- source_hash -------------------------------------------------------


def _listing(**over) -> dict:
    base = {
        "title": "1 Bed 1 Bath - Apartment",
        "price_raw": "16 000 ฿/месяц",
        "district": "Kathu, Phuket, Thailand",
        "description": "Уютная квартира рядом с парком.",
        "text_lines": ["a", "b", "c"],
    }
    base.update(over)
    return base


def test_source_hash_is_deterministic():
    assert source_hash(_listing()) == source_hash(_listing())


def test_source_hash_ignores_key_order():
    listing = _listing()
    reordered = dict(reversed(list(listing.items())))
    assert source_hash(reordered) == source_hash(listing)


def test_source_hash_changes_with_description():
    assert source_hash(_listing(description="Совсем другой текст")) != source_hash(_listing())


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Другой заголовок"),
        ("price_raw", "18 000 ฿/месяц"),
        ("district", "Rawai, Phuket, Thailand"),
        ("text_lines", ["a", "b", "c", "d"]),
    ],
)
def test_source_hash_changes_with_each_hashed_field(field, value):
    assert source_hash(_listing(**{field: value})) != source_hash(_listing())


def test_source_hash_ignores_unhashed_columns():
    assert source_hash(_listing(last_seen_at="2026-08-25T10:00:00Z")) == source_hash(_listing())


def test_source_hash_reads_text_lines_from_raw_json():
    listing = _listing()
    del listing["text_lines"]
    listing["raw_json"] = json.dumps({"url": "u", "text_lines": ["a", "b", "c"]})
    assert source_hash(listing) == source_hash(_listing())


def test_source_hash_tolerates_nulls_and_broken_raw_json():
    assert source_hash({"title": None, "description": None, "raw_json": "{broken"})
