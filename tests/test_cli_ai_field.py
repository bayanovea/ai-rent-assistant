"""Tests for `fb-rent ai-field` — the one CLI command that mutates state.

The other AI commands are thin wrappers over `ai_batch` / `storage` /
`decisions`, already covered by their own unit tests. `ai-field add` rewrites
`config/ai_fields.json` in place, so it is tested here against a copy.
"""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from fb_rent import ai_schema, config
from fb_rent.cli import main


@pytest.fixture()
def registry_file(tmp_path):
    """A writable copy of the real registry."""
    target = tmp_path / "ai_fields.json"
    shutil.copy(config.AI_FIELDS_PATH, target)
    return target


def _run(*args):
    return CliRunner().invoke(main, list(args))


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_add_appends_field_with_bumped_added_in(registry_file):
    before = _read(registry_file)["schema_version"]

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony",
        "--label", "Balcony",
        "--type", "enum",
        "--values", "yes,no,unclear",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    assert raw["schema_version"] == before + 1
    added = [f for f in raw["fields"] if f["key"] == "balcony"]
    assert len(added) == 1
    assert added[0]["added_in"] == before + 1
    assert added[0]["values"] == ["yes", "no", "unclear"]
    # The file stays loadable — added_in never exceeds schema_version.
    assert ai_schema.load_registry(registry_file).field("balcony") is not None


def test_add_without_backfill_leaves_analysis_version_behind(registry_file):
    raw_before = _read(registry_file)
    before = raw_before["schema_version"]
    analysis_before = raw_before.get("analysis_version", before)

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    # Analysed rows are not re-queued: they simply show a c-stale dash.
    assert raw["analysis_version"] == analysis_before
    assert raw["schema_version"] == before + 1
    assert "--backfill" in result.output


def test_backfill_marks_existing_rows_for_reanalysis(registry_file):
    before = _read(registry_file)["schema_version"]

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
        "--backfill",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    assert raw["schema_version"] == before + 1
    assert raw["analysis_version"] == before + 1
    assert "Backfill requested" in result.output


def test_after_places_the_field_at_the_requested_position(registry_file):
    keys_before = [f["key"] for f in _read(registry_file)["fields"]]
    anchor = keys_before[0]

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
        "--after", anchor,
    )

    assert result.exit_code == 0, result.output
    keys_after = [f["key"] for f in _read(registry_file)["fields"]]
    assert keys_after.index("balcony") == keys_after.index(anchor) + 1


def test_after_an_unknown_key_is_an_error(registry_file):
    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
        "--after", "no_such_field",
    )

    assert result.exit_code != 0
    assert "no_such_field" in result.output
    assert "balcony" not in registry_file.read_text(encoding="utf-8")


def test_duplicate_key_is_rejected_and_file_untouched(registry_file):
    existing = _read(registry_file)["fields"][0]["key"]
    before = registry_file.read_text(encoding="utf-8")

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", existing, "--label", "Duplicate", "--type", "text",
    )

    assert result.exit_code != 0
    assert existing in result.output
    assert registry_file.read_text(encoding="utf-8") == before


def test_enum_without_values_is_rejected(registry_file):
    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "enum",
    )

    assert result.exit_code != 0
    assert "--values" in result.output


def test_unknown_type_is_rejected(registry_file):
    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "boolean",
    )

    assert result.exit_code != 0


def test_list_prints_every_field_and_both_versions(registry_file):
    result = _run("ai-field", "list", "--registry", str(registry_file))

    assert result.exit_code == 0, result.output
    registry = ai_schema.load_registry(registry_file)
    assert f"schema_version: {registry.schema_version}" in result.output
    assert "analysis_version" in result.output
    for key in registry.field_keys():
        assert key in result.output


def test_add_keeps_the_registry_world_readable(registry_file):
    registry_file.chmod(0o644)

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony",
        "--label", "Balcony",
        "--type", "text",
    )

    assert result.exit_code == 0, result.output
    # The atomic write goes through mkstemp (0600); os.replace must not
    # narrow a version-controlled file down to owner-only.
    assert registry_file.stat().st_mode & 0o077 == 0o044


def test_photo_source_backfill_moves_only_the_photo_version(registry_file):
    raw_before = _read(registry_file)
    before = raw_before["schema_version"]
    analysis_before = raw_before.get("analysis_version", before)

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "mould", "--label", "Mould", "--type", "text",
        "--source", "photo", "--backfill",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    assert raw["fields"][-1]["source"] == "photo"
    # The camera has to look again; the text pass has nothing new to read.
    assert raw["photo_analysis_version"] == before + 1
    assert raw["analysis_version"] == analysis_before
    assert "photo-batch" in result.output
    assert "ai-batch" not in result.output


def test_both_source_backfill_moves_both_versions(registry_file):
    before = _read(registry_file)["schema_version"]

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "mould", "--label", "Mould", "--type", "text",
        "--source", "both", "--backfill",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    assert raw["fields"][-1]["source"] == "both"
    assert raw["analysis_version"] == before + 1
    assert raw["photo_analysis_version"] == before + 1
    assert "ai-batch" in result.output
    assert "photo-batch" in result.output


def test_default_source_backfill_leaves_the_photo_version_alone(registry_file):
    raw_before = _read(registry_file)
    before = raw_before["schema_version"]
    photo_before = raw_before.get("photo_analysis_version", before)

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
        "--backfill",
    )

    assert result.exit_code == 0, result.output
    raw = _read(registry_file)
    # `text` is the default, so the key is left off the field entirely.
    assert "source" not in raw["fields"][-1]
    assert raw["analysis_version"] == before + 1
    assert raw["photo_analysis_version"] == photo_before


def test_unknown_source_is_rejected(registry_file):
    before = registry_file.read_text(encoding="utf-8")

    result = _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "balcony", "--label", "Balcony", "--type", "text",
        "--source", "video",
    )

    assert result.exit_code != 0
    assert registry_file.read_text(encoding="utf-8") == before


def test_list_prints_the_source_column(registry_file):
    _run(
        "ai-field", "add",
        "--registry", str(registry_file),
        "--key", "mould", "--label", "Mould", "--type", "text",
        "--source", "photo",
    )

    result = _run("ai-field", "list", "--registry", str(registry_file))

    assert result.exit_code == 0, result.output
    assert "photo_analysis_version" in result.output
    mould_line = next(l for l in result.output.splitlines() if "mould" in l)
    assert "photo" in mould_line
    # Fields that omit `source` still print one — the assumed default.
    price_line = next(l for l in result.output.splitlines() if " price" in l)
    assert "text" in price_line
