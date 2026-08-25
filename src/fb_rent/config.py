"""Runtime configuration and default paths for fb-rent.

All local state (SQLite DB, CSV export, Playwright profile) lives in the
sibling ``data/`` directory next to the ``scrapper/`` package by default.
Override the root via the ``FB_RENT_HOME`` environment variable — useful
for tests and isolated runs.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo layout: <fb-rent-root>/scrapper/src/fb_rent/config.py — data lives at
# <fb-rent-root>/data.
_DEFAULT_HOME: Path = Path(__file__).resolve().parents[3] / "data"

# Editable, version-controlled configuration lives in the repo itself (not in
# FB_RENT_HOME): the AI field registry and the search requirements letter.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIG_DIR: Path = REPO_ROOT / "config"
AI_FIELDS_PATH: Path = CONFIG_DIR / "ai_fields.json"
REQUIREMENTS_PATH: Path = CONFIG_DIR / "requirements.md"

# Hard budget ceiling: anything above this is auto-rejected by rules.py.
# The soft target stays DEFAULT_MIN_PRICE..DEFAULT_MAX_PRICE.
BUDGET_HARD_MAX_THB: int = 35_000

# Hard floor on living area: anything smaller is auto-rejected by rules.py.
# An unstated size is not a rejection — only a number below this one is.
MIN_SIZE_SQM: int = 35

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
    return _DEFAULT_HOME


FB_RENT_HOME: Path = _home()
DB_PATH: Path = FB_RENT_HOME / "rentals.db"
CSV_PATH: Path = FB_RENT_HOME / "rentals.csv"
HTML_PATH: Path = FB_RENT_HOME / "rentals.html"
BROWSER_PROFILE_DIR: Path = FB_RENT_HOME / "browser-profile"
AI_DIR: Path = FB_RENT_HOME / "ai"
DECISIONS_PATH: Path = FB_RENT_HOME / "decisions.json"


def ensure_home() -> Path:
    """Create the fb-rent home directory if it does not exist yet."""
    FB_RENT_HOME.mkdir(parents=True, exist_ok=True)
    return FB_RENT_HOME
