"""Tests for the FB Marketplace URL builder."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from rent_assistant.config import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_DAYS_SINCE_LISTED,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_PRICE,
)
from rent_assistant.url_builder import build_marketplace_url, price_bands


def _parse(url: str) -> tuple[urlparse, dict[str, list[str]]]:
    parsed = urlparse(url)
    return parsed, parse_qs(parsed.query)


def test_default_url_matches_phuket_defaults() -> None:
    url = build_marketplace_url()
    parsed, qs = _parse(url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.facebook.com"
    assert parsed.path == f"/marketplace/{DEFAULT_CATEGORY_ID}/propertyrentals"

    assert qs["minPrice"] == [str(DEFAULT_MIN_PRICE)]
    assert qs["maxPrice"] == [str(DEFAULT_MAX_PRICE)]
    assert qs["daysSinceListed"] == [str(DEFAULT_DAYS_SINCE_LISTED)]
    assert qs["sortBy"] == ["creation_time_descend"]
    assert qs["exact"] == ["false"]


def test_custom_price_range_and_days() -> None:
    url = build_marketplace_url(min_price=20_000, max_price=25_000, days_since_listed=1)
    _, qs = _parse(url)

    assert qs["minPrice"] == ["20000"]
    assert qs["maxPrice"] == ["25000"]
    assert qs["daysSinceListed"] == ["1"]


def test_custom_category_id() -> None:
    url = build_marketplace_url(category_id="999")
    parsed, _ = _parse(url)
    assert parsed.path == "/marketplace/999/propertyrentals"


def test_edge_zero_min_price_allowed() -> None:
    url = build_marketplace_url(min_price=0, max_price=1)
    _, qs = _parse(url)
    assert qs["minPrice"] == ["0"]
    assert qs["maxPrice"] == ["1"]


def test_edge_equal_min_and_max_allowed() -> None:
    url = build_marketplace_url(min_price=20_000, max_price=20_000)
    _, qs = _parse(url)
    assert qs["minPrice"] == ["20000"]
    assert qs["maxPrice"] == ["20000"]


def test_edge_large_days_since_listed() -> None:
    url = build_marketplace_url(days_since_listed=30)
    _, qs = _parse(url)
    assert qs["daysSinceListed"] == ["30"]


def test_min_greater_than_max_raises() -> None:
    with pytest.raises(ValueError, match="min_price"):
        build_marketplace_url(min_price=30_000, max_price=15_000)


def test_negative_prices_raise() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        build_marketplace_url(min_price=-1, max_price=100)
    with pytest.raises(ValueError, match="non-negative"):
        build_marketplace_url(min_price=0, max_price=-1)


def test_non_positive_days_raises() -> None:
    with pytest.raises(ValueError, match="days_since_listed"):
        build_marketplace_url(days_since_listed=0)
    with pytest.raises(ValueError, match="days_since_listed"):
        build_marketplace_url(days_since_listed=-3)


def test_empty_category_id_raises() -> None:
    with pytest.raises(ValueError, match="category_id"):
        build_marketplace_url(category_id="")


def test_query_param_order_is_stable() -> None:
    """Preserve insertion order — makes URLs deterministic for logging/tests."""
    url = build_marketplace_url()
    query = urlparse(url).query
    keys_in_order = [pair.split("=", 1)[0] for pair in query.split("&")]
    assert keys_in_order == [
        "minPrice",
        "maxPrice",
        "daysSinceListed",
        "sortBy",
        "exact",
    ]


def test_one_band_is_the_whole_window() -> None:
    assert price_bands(15_000, 30_000, 1) == [(15_000, 30_000)]


def test_bands_touch_without_overlapping_or_leaving_gaps() -> None:
    bands = price_bands(15_000, 30_000, 3)

    assert bands == [(15_000, 19_999), (20_000, 24_999), (25_000, 30_000)]
    assert bands[0][0] == 15_000 and bands[-1][1] == 30_000
    for lower, upper in zip(bands, bands[1:]):
        assert upper[0] == lower[1] + 1


def test_a_remainder_is_spread_over_the_bands() -> None:
    bands = price_bands(0, 9, 4)

    widths = [high - low + 1 for low, high in bands]
    assert sum(widths) == 10
    assert max(widths) - min(widths) <= 1


@pytest.mark.parametrize(
    "args",
    [
        (15_000, 30_000, 0),
        (15_000, 30_000, -1),
        (30_000, 15_000, 2),
        (-1, 30_000, 2),
        (20_000, 20_001, 5),
    ],
)
def test_impossible_splits_are_refused(args) -> None:
    with pytest.raises(ValueError):
        price_bands(*args)
