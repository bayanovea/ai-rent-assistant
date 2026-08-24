"""Build FB Marketplace search URLs for Phuket property rentals.

The list URL is deterministic and driven entirely by query parameters —
`sortBy=creation_time_descend` keeps freshest listings on top so we do not
need to paginate. `daysSinceListed` further bounds the result set to the
last N days.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fb_rent.config import (
    DEFAULT_CATEGORY_ID,
    DEFAULT_DAYS_SINCE_LISTED,
    DEFAULT_MAX_PRICE,
    DEFAULT_MIN_PRICE,
)

_MARKETPLACE_BASE = "https://www.facebook.com/marketplace"


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
