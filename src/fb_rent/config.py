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

# Facebook groups scraped alongside Marketplace, keyed by the slug/id in the
# group's URL, valued by the short label the export table shows.
GROUPS: dict[str, str] = {
    "phuketrentsale": "PhuketRentSale",
    "Villa.For.Rent.Phuket": "Villa For Rent",
    "170117323644873": "Phuket Rent",
}
GROUP_FEED_MAX_ITEMS: int = 40

# Photo pass budget: how many gallery shots per listing get downloaded, and
# how much of each one survives the resize. ``None`` means the whole gallery
# — the pass runs only on listings the user has already kept by hand, so
# completeness is worth more there than the tokens a capped sample saves.
# Set an int to sample instead: the cover shot plus an even spread.
PHOTO_MAX_COUNT: int | None = None
PHOTO_LONG_EDGE_PX: int = 1024
# Pause between listings in `photos-fetch`. The download itself is a handful
# of CDN requests, not a page load, so the gap is short — it only keeps a
# queue of fifty from arriving as one burst.
PHOTO_FETCH_DELAY_MIN_SEC: float = 0.5
PHOTO_FETCH_DELAY_MAX_SEC: float = 1.5
PHOTO_JPEG_QUALITY: int = 80

# Photo fingerprints: the cheap dedupe signal taken right after the detail
# page — hashes only, nothing on disk. Two matched shots out of three is the
# threshold because one shared shot is usually the pool, the building or an
# agency logo, pictures that belong to the complex and not to the flat;
# three out of three would miss the common repost that swaps the cover.
PHOTO_FINGERPRINT_COUNT: int = 3
PHOTO_MATCH_MIN: int = 2
# A hash seen on more listings than this is a splash screen or a shared view
# of the complex, so it leaves the index before any matching happens.
PHOTO_HASH_COMMON_MAX: int = 4


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
PHOTOS_DIR: Path = FB_RENT_HOME / "photos"
DECISIONS_PATH: Path = FB_RENT_HOME / "decisions.json"
# Researched pets policy per building — like decisions.json, a file no
# init_db can wipe, mirrored into a table only so fetch_enriched can JOIN.
BUILDINGS_PATH: Path = FB_RENT_HOME / "buildings.json"


def ensure_home() -> Path:
    """Create the fb-rent home directory if it does not exist yet."""
    FB_RENT_HOME.mkdir(parents=True, exist_ok=True)
    return FB_RENT_HOME
