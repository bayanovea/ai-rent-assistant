"""Tests for the FB Marketplace text extractors."""

from __future__ import annotations

import pytest

from fb_rent.extractors import (
    extract_bedrooms,
    extract_property_type,
    parse_price,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("THB25,000", 25_000),
        ("฿15 000/month", 15_000),
        ("฿15,000", 15_000),
        ("20K THB", 20_000),
        ("Bt 18000", 18_000),
        ("baht 22,500", 22_500),
        ("25000", 25_000),  # unlabelled — search is already THB-scoped
        ("  ฿30,000  ", 30_000),
        ("THB1,250,000", 1_250_000),
        ("25.000 THB", 25_000),  # dot as thousands separator (EU style)
    ],
)
def test_parse_price_valid(raw: str, expected: int) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "PRICE ON REQUEST",
        "Contact for price",
        "Ask",
        "$500",       # foreign currency — rejected
        "€1,200",     # foreign currency — rejected
        "500 USD",    # foreign currency — rejected
    ],
)
def test_parse_price_none(raw: str | None) -> None:
    assert parse_price(raw) is None


def test_parse_price_rejects_absurdly_large_numbers() -> None:
    # Phone-number bleed-through: e.g. a merged "THB 0812345678" line
    assert parse_price("THB 99999999999") is None


def test_parse_price_zero_rejected() -> None:
    assert parse_price("THB 0") is None


@pytest.mark.parametrize(
    "raw",
    [
        "THB 25.5",         # decimal price, not thousands
        "25.5K THB",        # fractional-K should not silently truncate
        "12,5 THB",         # EU decimal form
        "฿ 3.75",           # single decimal
    ],
)
def test_parse_price_rejects_decimals(raw: str) -> None:
    # Historical bug: parse_price stripped '.' unconditionally, so "25.5"
    # became 255. Reject decimals rather than pick a wrong integer.
    assert parse_price(raw) is None


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("2BR condo in Rawai", "", 2),
        ("Cozy apartment", "3 bedrooms, sea view", 3),
        ("Studio for rent", "small but new", 0),
        ("1 bed near beach", "", 1),
        ("Beautiful 4-bedroom villa", "big garden", 4),
        ("Nice place", "Has 2 bed and 1 bath", 2),
        ("Condo", "1 BR, fully furnished", 1),
        ("STUDIO APARTMENT", "modern", 0),
    ],
)
def test_extract_bedrooms(
    title: str, description: str, expected: int
) -> None:
    assert extract_bedrooms(title, description) == expected


@pytest.mark.parametrize(
    "title,description",
    [
        ("Nice place", "sea view, quiet street"),
        ("", ""),
        (None, None),
        ("Fully furnished", "modern kitchen"),
    ],
)
def test_extract_bedrooms_none(
    title: str | None, description: str | None
) -> None:
    assert extract_bedrooms(title, description) is None


def test_extract_bedrooms_prefers_explicit_count_over_studio() -> None:
    # Some ads say "2BR studio-style layout" — take the explicit number.
    assert extract_bedrooms("2BR studio-style unit", "") == 2


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("Luxury villa in Rawai", "private pool", "villa"),
        ("2BR condo for rent", "Kata beach", "condo"),
        ("Modern apartment", "furnished", "apartment"),
        ("Studio near beach", "", "studio"),
        ("Townhouse in Chalong", "3 bed", "townhouse"),
        ("Town house — Rawai", "great location", "townhouse"),
        ("Family house with garden", "quiet", "house"),
        ("Great place", "well located", "unknown"),
        ("Beautiful apt near beach", "", "apartment"),
        ("Condominium unit", "6th floor", "condo"),
    ],
)
def test_extract_property_type(
    title: str, description: str, expected: str
) -> None:
    assert extract_property_type(title, description) == expected


def test_extract_property_type_title_wins_over_description() -> None:
    # If title says villa but description mentions a nearby condo, title wins.
    assert (
        extract_property_type(
            "Private villa with pool",
            "surrounded by condo buildings",
        )
        == "villa"
    )


def test_extract_property_type_townhouse_not_shadowed_by_house() -> None:
    assert extract_property_type("Modern townhouse", "") == "townhouse"


def test_extract_property_type_empty_inputs() -> None:
    assert extract_property_type("", "") == "unknown"
    assert extract_property_type(None, None) == "unknown"
