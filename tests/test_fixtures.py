"""Sanity tests on the fixtures themselves: their shape must match what the scraper writes."""

from __future__ import annotations

import pytest

from tests.fixtures import GOOD, SIDEBAR, load

FIXTURE_NAMES = [GOOD, SIDEBAR]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_parses_as_json_with_expected_keys(name: str) -> None:
    data = load(name)
    assert set(data) >= {"fb_id", "url", "title", "text_lines"}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_text_lines_is_non_empty_list_of_strings(name: str) -> None:
    lines = load(name)["text_lines"]
    assert isinstance(lines, list)
    assert lines
    assert all(isinstance(line, str) and line.strip() for line in lines)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_has_description_and_seller_markers(name: str) -> None:
    """Both markers are present — otherwise the slicing tests have nothing to cut."""
    lines = load(name)["text_lines"]
    assert "Description" in lines
    assert "Seller information" in lines
    assert lines.index("Description") < lines.index("Seller information")


def test_good_fixture_carries_the_sample_townhouse_ad() -> None:
    lines = load(GOOD)["text_lines"]
    assert any("Sample Garden Village" in line for line in lines)


def test_sidebar_fixture_has_ad_text_before_the_sidebar_junk() -> None:
    """In the "bad" fixture the `Daily pick` sidebar comes AFTER the real description."""
    lines = load(SIDEBAR)["text_lines"]
    assert any("House for rent in Example Village" in line for line in lines)
    assert "Daily pick" in lines
    assert lines.index("Seller information") < lines.index("Daily pick")
