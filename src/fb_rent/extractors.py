"""Heuristic extractors for FB Marketplace listing text.

These are the pure functions used to normalise scraped strings into
structured fields (`price_thb`, `bedrooms`, `property_type`) before we
upsert into SQLite. They are intentionally simple regex/keyword parsers —
AI-based enrichment is planned for a later iteration.
"""

from __future__ import annotations

import re

# Currency tokens we accept as "this string is a THB price". FB shows a mix
# of "THB", "฿", "Bt", "baht", or nothing at all when the marketplace UI
# already labels the column. We treat unlabelled numbers as THB too since
# the whole search is scoped to Phuket rentals in THB.
_CURRENCY_RE = re.compile(
    r"(?:฿|thb|bt\.?|baht)",
    re.IGNORECASE,
)

# Match a numeric run allowing thousands separators (comma, space, dot,
# thin/nbsp space) and an optional "k"/"K" magnitude suffix meaning ×1000.
_NUMBER_RE = re.compile(
    r"(\d[\d\s,.  ]*)\s*(k)?",
    re.IGNORECASE,
)

# Bedroom patterns, ordered from most to least specific. We stop at the
# first match, so "studio" wins over a stray "1" in the description.
_STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
_BEDROOM_RE = re.compile(
    r"\b(\d{1,2})\s*(?:br|bed(?:room)?s?|bdr?m?s?|-?\s*bedroom)\b",
    re.IGNORECASE,
)

# Property type keywords. Order matters: "townhouse" must be checked
# before "house" so it does not get shadowed.
_PROPERTY_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("townhouse", re.compile(r"\btown\s?house\b", re.IGNORECASE)),
    ("villa", re.compile(r"\bvillas?\b", re.IGNORECASE)),
    ("condo", re.compile(r"\bcondo(?:minium)?s?\b", re.IGNORECASE)),
    ("apartment", re.compile(r"\bapartments?\b|\bapt\b", re.IGNORECASE)),
    ("studio", re.compile(r"\bstudio\b", re.IGNORECASE)),
    ("house", re.compile(r"\bhouses?\b", re.IGNORECASE)),
)


def parse_price(raw: str | None) -> int | None:
    """Parse a FB price string into an integer THB amount.

    Returns ``None`` for empty input, "Price on request"-style strings, or
    anything without a recognisable number. A trailing ``k``/``K`` is
    treated as ×1000. Non-THB currencies (USD/EUR/₽/etc.) are rejected —
    we do not do FX conversion.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    # Reject other currencies we would otherwise silently accept.
    if re.search(r"[$€£₽¥]|\busd\b|\beur\b|\bgbp\b|\brub\b", text, re.IGNORECASE):
        return None

    match = _NUMBER_RE.search(text)
    if not match:
        return None

    number = match.group(1)
    # Reject decimal-looking values like "25.5" or "12,5" (a separator
    # followed by 1–2 trailing digits — i.e. not a thousands group).
    if re.search(r"[.,]\d{1,2}(?!\d)", number):
        return None

    digits = re.sub(r"[\s,.  ]", "", number)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None

    if match.group(2):  # "20K" → 20000
        value *= 1000

    # Sanity clamp: reject absurd numbers that likely came from a phone
    # number or listing id bleed-through. Phuket long-term rentals sit in
    # the low-thousands to low-millions THB per month range.
    if value <= 0 or value > 10_000_000:
        return None

    return value


def extract_bedrooms(title: str | None, description: str | None) -> int | None:
    """Extract bedroom count from title + description text.

    Returns ``0`` for studio units, an integer for explicit "NBR" /
    "N bedroom" mentions, and ``None`` if nothing matched.
    """
    haystack = " ".join(part for part in (title, description) if part)
    if not haystack:
        return None

    match = _BEDROOM_RE.search(haystack)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None

    if _STUDIO_RE.search(haystack):
        return 0

    return None


def extract_property_type(title: str | None, description: str | None) -> str:
    """Classify listing as villa/condo/apartment/house/studio/townhouse.

    Falls back to ``"unknown"`` when no keyword matches. Title matches
    take precedence over description matches — sellers usually put the
    property type in the headline.
    """
    for source in (title, description):
        if not source:
            continue
        for label, pattern in _PROPERTY_TYPES:
            if pattern.search(source):
                return label
    return "unknown"
