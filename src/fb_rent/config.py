"""Runtime configuration and default paths for fb-rent.

All local state (SQLite DB, CSV export, Playwright profile) lives under
``~/.fb-rent/`` by default. Override the root via the ``FB_RENT_HOME``
environment variable — useful for tests and isolated runs.
"""

from __future__ import annotations

import os
from pathlib import Path

# Marketplace defaults for the Phuket Property Rentals category.
DEFAULT_CATEGORY_ID: str = "110729412288885"
DEFAULT_MIN_PRICE: int = 15_000
DEFAULT_MAX_PRICE: int = 30_000
DEFAULT_DAYS_SINCE_LISTED: int = 7

# Scrape run defaults.
DEFAULT_MAX_ITEMS: int = 40
DETAIL_REQUEST_CAP: int = 60
DETAIL_DELAY_MIN_SEC: float = 3.0
DETAIL_DELAY_MAX_SEC: float = 7.0


def _home() -> Path:
    """Return the fb-rent home directory, honouring ``FB_RENT_HOME`` env var."""
    override = os.environ.get("FB_RENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".fb-rent"


FB_RENT_HOME: Path = _home()
DB_PATH: Path = FB_RENT_HOME / "rentals.db"
CSV_PATH: Path = FB_RENT_HOME / "rentals.csv"
BROWSER_PROFILE_DIR: Path = FB_RENT_HOME / "browser-profile"


def ensure_home() -> Path:
    """Create the fb-rent home directory if it does not exist yet."""
    FB_RENT_HOME.mkdir(parents=True, exist_ok=True)
    return FB_RENT_HOME
