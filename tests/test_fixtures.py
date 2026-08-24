"""Санити-тесты на сами фикстуры: формат не разъехался с тем, что пишет скрапер."""

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
    """Оба маркера на месте — иначе тестам Task 2 нечего резать."""
    lines = load(name)["text_lines"]
    assert "Описание" in lines
    assert "Информация о продавце" in lines
    assert lines.index("Описание") < lines.index("Информация о продавце")


def test_good_fixture_carries_the_origin_kathu_ad() -> None:
    lines = load(GOOD)["text_lines"]
    assert any("The Origin Kathu" in line for line in lines)


def test_sidebar_fixture_has_ad_text_after_the_sidebar_junk() -> None:
    """У «плохой» фикстуры сайдбар `Выбор дня` идёт ПОСЛЕ настоящего описания."""
    lines = load(SIDEBAR)["text_lines"]
    assert any("House for rent in Wichit-Chalong" in line for line in lines)
    assert "Выбор дня" in lines
    assert lines.index("Информация о продавце") < lines.index("Выбор дня")
