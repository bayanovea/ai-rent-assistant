"""Build FB Marketplace search URLs for Phuket property rentals.

The list URL is deterministic and driven entirely by query parameters —
`sortBy=creation_time_descend` keeps freshest listings on top so we do not
need to paginate. `daysSinceListed` further bounds the result set to the
last N days.

One query is also a ceiling, not just a filter: the feed stops handing out
new cards after a few hundred however far the scraper scrolls. :func:`price_bands`
is the way past it — several narrower questions reach listings the single
wide one never shows.
"""

from __future__ import annotations

from urllib.parse import urlencode

from rent_assistant.config import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_DAYS_SINCE_LISTED,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_PRICE,
)

_MARKETPLACE_BASE = "https://www.facebook.com/marketplace"
_GROUPS_BASE = "https://www.facebook.com/groups"


def group_feed_url(group_id: str) -> str:
    """The group's chronological feed — newest post first, no pagination."""
    return f"{_GROUPS_BASE}/{group_id}?sorting_setting=CHRONOLOGICAL"


def group_post_url(group_id: str, post_id: str) -> str:
    """The clean, query-free permalink for one post in one group.

    Verified live to survive navigation unchanged and to carry the full
    post text and gallery — no lightbox or membership wall in the way.
    """
    return f"{_GROUPS_BASE}/{group_id}/posts/{post_id}/"


def build_marketplace_url(
    category_id: str = DEFAULT_CATEGORY_ID,
    min_price: int = DEFAULT_MIN_PRICE,
    max_price: int = DEFAULT_MAX_PRICE,
    days_since_listed: int = DEFAULT_DAYS_SINCE_LISTED,
) -> str:
    """Return the FB Marketplace property rentals search URL.

    Parameters
    ----------
    category_id:
        Marketplace location/category id (Phuket Property Rentals by default).
    min_price, max_price:
        THB price range. ``min_price`` must be ``<= max_price`` and both must
        be non-negative.
    days_since_listed:
        Filter to listings created in the last N days. Must be positive.

    Raises
    ------
    ValueError
        If ``category_id`` is empty, prices are negative, ``min_price`` is
        greater than ``max_price``, or ``days_since_listed`` is not positive.
    """
    if not category_id:
        raise ValueError("category_id must be a non-empty string")
    if min_price < 0 or max_price < 0:
        raise ValueError("prices must be non-negative")
    if min_price > max_price:
        raise ValueError(
            f"min_price ({min_price}) must be <= max_price ({max_price})"
        )
    if days_since_listed <= 0:
        raise ValueError("days_since_listed must be positive")

    params = [
        ("minPrice", str(min_price)),
        ("maxPrice", str(max_price)),
        ("daysSinceListed", str(days_since_listed)),
        ("sortBy", "creation_time_descend"),
        ("exact", "false"),
    ]
    query = urlencode(params)
    return f"{_MARKETPLACE_BASE}/{category_id}/propertyrentals?{query}"


def price_bands(
    min_price: int, max_price: int, bands: int
) -> list[tuple[int, int]]:
    """Split a price window into ``bands`` non-overlapping ranges, low first.

    The bands touch but never overlap — one ends a baht below the next
    begins — so every listing falls in exactly one of them and the sweep
    does not pay twice for the same card. A remainder is spread over the
    bands rather than dumped on the last one.

    Raises
    ------
    ValueError
        If ``bands`` is not positive, the price window is inverted, or the
        window holds fewer baht than the requested number of bands.
    """
    if bands <= 0:
        raise ValueError("bands must be positive")
    if min_price < 0 or max_price < 0:
        raise ValueError("prices must be non-negative")
    if min_price > max_price:
        raise ValueError(
            f"min_price ({min_price}) must be <= max_price ({max_price})"
        )

    span = max_price - min_price + 1
    if bands > span:
        raise ValueError(
            f"cannot split {span} baht into {bands} bands"
        )

    edges = [min_price + (span * index) // bands for index in range(bands + 1)]
    return [(edges[index], edges[index + 1] - 1) for index in range(bands)]
