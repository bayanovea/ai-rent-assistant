"""Heuristic extractors for FB Marketplace listing text.

These are the pure functions used to normalise scraped strings into
structured fields (`price_thb`, `bedrooms`, `property_type`) before we
upsert into SQLite. They are intentionally simple regex/keyword parsers;
the semantic work is done by the AI enrichment loop (:mod:`.ai_batch`,
:mod:`.rules`), which reads the text these functions salvage.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

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


# --- Detail-page text salvage -------------------------------------------
#
# FB renders the whole detail page as one flat text blob; the scraper keeps
# the first 200 non-empty lines in ``raw_json["text_lines"]``. The real ad
# body sits between the "Описание"/"Description" heading and the seller
# block. Picking "the longest text node" instead (what iteration 1 did)
# reliably grabs the "Выбор дня" sidebar of unrelated listings, so we cut
# on the markers.

_DESCRIPTION_MARKERS = frozenset({"описание", "description"})

# Everything that can legitimately follow the ad body. "Выбор дня" is the
# sidebar carousel — if the seller block is missing for some locale/layout
# we still must not swallow it.
_DESCRIPTION_END_MARKERS = frozenset(
    {
        "информация о продавце",
        "seller information",
        "выбор дня",
        "daily pick",
    }
)

# Collapsed-description expander, left in the text when the click failed.
_SEE_MORE_MARKERS = frozenset({"ещё", "еще", "see more", "see more…", "ещё…"})

# FB page <title>: "(6) Marketplace — 1 Bed 1 Bath - Apartment | Facebook".
_TITLE_UNREAD_PREFIX_RE = re.compile(r"^\(\d+\)\s*")
_TITLE_MARKETPLACE_PREFIX_RE = re.compile(
    r"^marketplace\s*[—–\-|:]\s*", re.IGNORECASE
)
_TITLE_FACEBOOK_SUFFIX_RE = re.compile(r"\s*[|·]\s*facebook\s*$", re.IGNORECASE)

# Titles that carry no information about the property. FB uses these as
# section headings on list cards; iteration 1 stored them verbatim for
# 100% of rows.
_TITLE_JUNK = frozenset(
    {
        "недавно опубликовано",
        "recently posted",
        "recently listed",
        "marketplace",
        "facebook",
        "аренда недвижимости",
    }
)

# Relative timestamps, RU and EN in one pattern: an optional count, a unit
# stem, then "назад"/"ago". "an hour ago" / "час назад" carry no digits, so
# a missing count means 1.
_PUBLISHED_RE = re.compile(
    r"(?:(\d+)\s*)?"
    r"(день|дн|мин|час|сутк|недел|мес|minutes?|mins?|hours?|days?|weeks?|months?)"
    r"[а-яёa-z]*"
    r"\s*(?:назад|ago)",
    re.IGNORECASE,
)

_PUBLISHED_UNIT_DAYS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("мин", "min"), 1 / 1440),
    (("час", "hour"), 1 / 24),
    (("день", "дн", "сутк", "day"), 1.0),
    (("недел", "week"), 7.0),
    (("мес", "month"), 30.0),
)


def description_from_text_lines(text_lines: list[str] | None) -> str | None:
    """Cut the real listing body out of a detail page's flat text lines.

    Takes the slice between the "Описание"/"Description" heading and the
    seller block, drops a trailing "Ещё"/"See more" expander, and joins
    what is left with newlines. Returns ``None`` when the heading is
    absent or the slice is empty — the caller then falls back to its own
    heuristic rather than getting a confidently wrong sidebar.
    """
    if not text_lines:
        return None

    start: int | None = None
    for index, line in enumerate(text_lines):
        if _marker_key(line) in _DESCRIPTION_MARKERS:
            start = index + 1
            break
    if start is None:
        return None

    end = len(text_lines)
    for index in range(start, len(text_lines)):
        if _marker_key(text_lines[index]) in _DESCRIPTION_END_MARKERS:
            end = index
            break

    body = [line.strip() for line in text_lines[start:end] if line and line.strip()]
    while body and _marker_key(body[-1]) in _SEE_MORE_MARKERS:
        body.pop()

    if not body:
        return None
    return "\n".join(body)


def _marker_key(line: str | None) -> str:
    """Normalise a line for marker comparison: lowercase, no punctuation tail."""
    if not line:
        return ""
    return line.strip().strip(":：").casefold()


def clean_fb_title(raw_title: str | None) -> str | None:
    """Strip FB page-title chrome down to the seller's own headline.

    ``"(6) Marketplace — 1 Bed 1 Bath - Apartment | Facebook"`` becomes
    ``"1 Bed 1 Bath - Apartment"``. Returns ``None`` for empty input or
    for FB's own section headings (``"Недавно опубликовано"``), which
    say nothing about the property.
    """
    if not raw_title:
        return None

    title = raw_title.strip()
    title = _TITLE_UNREAD_PREFIX_RE.sub("", title).strip()
    title = _TITLE_FACEBOOK_SUFFIX_RE.sub("", title).strip()
    title = _TITLE_MARKETPLACE_PREFIX_RE.sub("", title).strip()
    title = title.strip("|—–-·").strip()

    if not title or title.casefold() in _TITLE_JUNK:
        return None
    return title


def parse_published_at(raw: str | None, now: "datetime | None" = None) -> str | None:
    """Turn a relative FB timestamp into an absolute ISO-8601 UTC string.

    Understands both locales the scraper meets in practice: English
    ``"Listed 3 days ago"`` and Russian ``"Опубликовано 12 часов назад"``.
    ``now`` defaults to the current UTC time; tests pass it explicitly.
    Returns ``None`` when no relative timestamp is present.
    """
    if not raw:
        return None

    match = _PUBLISHED_RE.search(raw)
    if not match:
        return None

    count = int(match.group(1)) if match.group(1) else 1
    stem = match.group(2).casefold()
    days: float | None = None
    for prefixes, unit_days in _PUBLISHED_UNIT_DAYS:
        if stem.startswith(prefixes):
            days = unit_days
            break
    if days is None:
        return None

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    published = moment.astimezone(timezone.utc) - timedelta(days=days * count)
    return published.strftime("%Y-%m-%dT%H:%M:%SZ")
