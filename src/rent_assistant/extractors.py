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
# already labels the column. "бат" is the Russian-locale spelling group posts
# use (Facebook/ad text, verified live — keep it byte-for-byte).
# We treat unlabelled numbers as THB too since the whole search is scoped to
# Phuket rentals in THB.
_CURRENCY_RE = re.compile(
    r"(?:฿|thb|bt\.?|baht|бат)",
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

    A marketplace ``price_raw`` is a short, price-only string, so its first
    number is the price. A group post's is a whole sentence — "Villa with
    3 bedrooms ... 100 000 бат per month" — where the first number is the
    bedroom count, not the price. When a currency token is present, the
    number closest to it wins; only an untagged string (a bare "25000") falls
    back to the first number in it.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    # Reject other currencies we would otherwise silently accept.
    if re.search(r"[$€£₽¥]|\busd\b|\beur\b|\bgbp\b|\brub\b", text, re.IGNORECASE):
        return None

    numbers = list(_NUMBER_RE.finditer(text))
    if not numbers:
        return None

    currencies = list(_CURRENCY_RE.finditer(text))
    if currencies:
        match = min(
            numbers,
            key=lambda m: min(abs(m.start() - c.start()) for c in currencies),
        )
    else:
        match = numbers[0]

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


# Facebook UI text in the Russian locale, verified live — keep byte-for-byte.
PETS_FACET_LINE = "Можно с собаками и кошками"
# Facebook's own "pets" facet on a rental listing — a fixed line straight
# under the price, not ad copy. It only ever renders when the poster ticked
# an allow box, so seeing any of these three settles "allowed" outright; it
# never appears to say no, so absence proves nothing.
_PETS_FACET_LINES = frozenset(
    {
        "можно с собаками и кошками",
        "можно с собаками",
        "можно с кошками",
    }
)


def extract_pets_facet(text_lines: list[str] | None) -> str | None:
    """``"allowed"`` when Facebook's own pets facet line is present, else ``None``.

    This is a platform field, not the seller's prose — it sits among ~150
    lines of chrome and "similar listings" noise in ``text_lines``, which is
    why the AI pass (reading the same dump) misses or contradicts it on
    about 1 in 12 listings that carry it. A plain string match doesn't.
    """
    for line in text_lines or []:
        if line.strip().lower() in _PETS_FACET_LINES:
            return "allowed"
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
# on the markers. The Russian strings in the tables below are Facebook's own
# UI text in the Russian locale, verified live — keep them byte-for-byte.

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
# The unread badge caps out as "20+" rather than a bare number once past
# some threshold, hence the optional "+".
_TITLE_UNREAD_PREFIX_RE = re.compile(r"^\(\d+\+?\)\s*")
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

# Relative timestamps, RU (Facebook's Russian-locale text) and EN in one
# pattern: an optional count, a unit stem, then "назад"/"ago". "an hour ago"
# / "час назад" carry no digits, so a missing count means 1.
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

# Group posts also carry absolute FB timestamps, which marketplace detail
# pages never show ("Listed N days ago" is always relative there). Genitive
# RU month names and EN month names (full or abbreviated), both matched
# with a `\b` boundary, so "sep" never eats into "sept".
_RU_MONTHS: dict[str, int] = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
    "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
    "ноября": 11, "декабря": 12,
}
_EN_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# "12 сентября в 14:30", "12 сентября 2025".
_ABSOLUTE_RU_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_RU_MONTHS) + r")\b"
    r"(?:\s+(\d{4}))?"
    r"(?:\s*в\s*(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)
# "September 12 at 2:30 PM", "September 12, 2025".
_ABSOLUTE_EN_RE = re.compile(
    r"\b(" + "|".join(_EN_MONTHS) + r")\.?\s+(\d{1,2})\b"
    r"(?:,?\s+(\d{4}))?"
    r"(?:\s*at\s*(\d{1,2}):(\d{2})\s*(am|pm)?)?",
    re.IGNORECASE,
)

# Compact feed timestamps with no "назад"/"ago": "5 ч.", "2 дн.", "1 нед.",
# "5h", "2d". Kept apart from `_PUBLISHED_RE` on purpose — loosening that
# one would also accept bare units on marketplace pages, where "5 ч."
# never legitimately appears without "назад".
_FEED_SHORT_RE = re.compile(
    r"\b(\d+)\s*(мин|ч|дн|нед|мес|min|h|d|w)\b\.?",
    re.IGNORECASE,
)
_FEED_SHORT_UNIT_DAYS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("мин", "min"), 1 / 1440),
    (("ч", "h"), 1 / 24),
    (("дн", "d"), 1.0),
    (("нед", "w"), 7.0),
    (("мес",), 30.0),
)
# "вчера в 10:11" / "сегодня в 09:05" — Russian-locale feed timestamps.
_YESTERDAY_RE = re.compile(r"\bвчера\b[^\d]*(\d{1,2}):(\d{2})", re.IGNORECASE)
_TODAY_RE = re.compile(r"\bсегодня\b[^\d]*(\d{1,2}):(\d{2})", re.IGNORECASE)


def _absolute_to_iso(
    day: str,
    month: int,
    year: str | None,
    hour: str | None,
    minute: str | None,
    meridiem: str | None,
    moment: datetime,
) -> str | None:
    try:
        day_i = int(day)
        hour_i = int(hour) if hour else 0
        minute_i = int(minute) if minute else 0
        if meridiem:
            meridiem = meridiem.lower()
            if meridiem == "pm" and hour_i != 12:
                hour_i += 12
            elif meridiem == "am" and hour_i == 12:
                hour_i = 0
        year_i = int(year) if year else moment.year
        published = datetime(year_i, month, day_i, hour_i, minute_i, tzinfo=timezone.utc)
    except ValueError:
        return None
    # No year in the source means "this year" — unless that reads as
    # tomorrow or later, which only happens when it was actually last year.
    if not year and published > moment + timedelta(days=1):
        published = published.replace(year=year_i - 1)
    return published.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_absolute_published_at(raw: str, moment: datetime) -> str | None:
    match = _ABSOLUTE_RU_RE.search(raw)
    if match:
        day, month_name, year, hour, minute = match.groups()
        return _absolute_to_iso(
            day, _RU_MONTHS[month_name.casefold()], year, hour, minute, None, moment
        )
    match = _ABSOLUTE_EN_RE.search(raw)
    if match:
        month_name, day, year, hour, minute, meridiem = match.groups()
        return _absolute_to_iso(
            day, _EN_MONTHS[month_name.casefold()], year, hour, minute, meridiem, moment
        )
    return None


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
    """Turn an FB timestamp into an absolute ISO-8601 UTC string.

    Understands the relative forms the scraper meets in practice: English
    ``"Listed 3 days ago"`` and Russian ``"Опубликовано 12 часов назад"``,
    and — group posts carry these instead — absolute dates: RU ``"12
    сентября в 14:30"`` / ``"12 сентября 2025"`` and EN ``"September 12 at
    2:30 PM"`` / ``"September 12, 2025"``. ``now`` defaults to the current
    UTC time; tests pass it explicitly. Returns ``None`` when neither form
    is present.
    """
    if not raw:
        return None

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)

    match = _PUBLISHED_RE.search(raw)
    if match:
        count = int(match.group(1)) if match.group(1) else 1
        stem = match.group(2).casefold()
        for prefixes, unit_days in _PUBLISHED_UNIT_DAYS:
            if stem.startswith(prefixes):
                published = moment - timedelta(days=unit_days * count)
                return published.strftime("%Y-%m-%dT%H:%M:%SZ")
        return None

    return _parse_absolute_published_at(raw, moment)


def parse_feed_timestamp(raw: str | None, now: "datetime | None" = None) -> str | None:
    """Parse a group feed header's compact timestamp.

    Delegates to :func:`parse_published_at` for every form it already
    covers (relative "N назад"/"ago" and the absolute date forms) and adds
    only what the feed shows and the detail page never does: bare units
    with no "назад" ("5 ч.", "2 дн.", "1w") and "вчера/сегодня в HH:MM".
    Kept separate so scraping the group feed — used only to decide when to
    stop scrolling — never loosens what a marketplace detail page accepts.
    """
    if not raw:
        return None

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)

    delegated = parse_published_at(raw, moment)
    if delegated:
        return delegated

    match = _FEED_SHORT_RE.search(raw)
    if match:
        count = int(match.group(1))
        stem = match.group(2).casefold()
        for prefixes, unit_days in _FEED_SHORT_UNIT_DAYS:
            if stem in prefixes:
                published = moment - timedelta(days=unit_days * count)
                return published.strftime("%Y-%m-%dT%H:%M:%SZ")

    match = _YESTERDAY_RE.search(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        day = (moment - timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return day.strftime("%Y-%m-%dT%H:%M:%SZ")

    match = _TODAY_RE.search(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        day = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return day.strftime("%Y-%m-%dT%H:%M:%SZ")

    return None


def _truncate_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def group_post_title(page_title: str | None, first_body_line: str | None) -> str | None:
    """Best-effort title for a group post — it has no title field at all.

    The page ``<title>`` reads ``"(N) <group name> | <first line of the
    post>"``; the group name must not become every row's title, so this
    keeps only what follows the first ``" | "``. Falls back to the post's
    own first line, truncated to a readable length, when the page title
    doesn't split that way. ``listings.title`` is ``NOT NULL`` downstream,
    so this needs to produce something whenever there is any text at all.
    """
    candidate: str | None = None
    if page_title:
        cleaned = _TITLE_UNREAD_PREFIX_RE.sub("", page_title.strip()).strip()
        cleaned = _TITLE_FACEBOOK_SUFFIX_RE.sub("", cleaned).strip()
        if " | " in cleaned:
            _, _, tail = cleaned.partition(" | ")
            tail = tail.strip()
            if tail and tail.casefold() not in _TITLE_JUNK:
                candidate = tail

    if not candidate and first_body_line:
        line = first_body_line.strip()
        if line:
            candidate = _truncate_at_word(line, 120)

    if not candidate:
        return None
    return candidate
