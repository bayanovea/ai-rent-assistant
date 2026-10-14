"""Tests for the FB Marketplace text extractors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rent_assistant.extractors import (
    clean_fb_title,
    description_from_text_lines,
    extract_bedrooms,
    extract_property_type,
    group_post_title,
    parse_feed_timestamp,
    parse_price,
    parse_published_at,
)
from rent_assistant.scraper_detail import extract_gallery_urls
from tests.fixtures import GOOD, SIDEBAR, load


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
    "raw,expected",
    [
        # A group post's price_raw is a whole sentence, not a price-only
        # string — an unrelated number ahead of the currency token must not
        # win. Real bug: this parsed to 3 (the bedroom count).
        (
            "Вилла с 3 спальнями в Банг Тао доступна для ежегодного "
            "контракта по 100 000 бат в месяц",
            100_000,
        ),
        # Same shape in English: "1-year" precedes the actual price.
        ("• 1-year contract: 30,000 THB/month", 30_000),
    ],
)
def test_parse_price_picks_number_nearest_currency_token(
    raw: str, expected: int
) -> None:
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


# --- description_from_text_lines ----------------------------------------


def test_description_from_text_lines_good_fixture() -> None:
    """The "good" page: the slice after `Description` is the ad itself."""
    lines = load(GOOD)["text_lines"]
    body = description_from_text_lines(lines)

    assert body is not None
    assert body.startswith("🏡 Sample Garden Village")
    assert "Separate kitchen" in body
    assert body.endswith("[hidden information]")


def test_description_from_text_lines_sidebar_fixture_skips_junk() -> None:
    """The "bad" page: take the ad, NOT the `Daily pick` sidebar."""
    lines = load(SIDEBAR)["text_lines"]
    body = description_from_text_lines(lines)

    assert body is not None
    assert "House for rent in Example Village" in body
    assert "For rent 30,000/month" in body
    # The sidebar of other people's listings follows — none of it may leak in.
    assert "Daily pick" not in body
    assert "Example Property Co" not in body


@pytest.mark.parametrize("name", [GOOD, SIDEBAR])
def test_description_from_text_lines_drops_chrome(name: str) -> None:
    body = description_from_text_lines(load(name)["text_lines"])

    assert body is not None
    assert "Seller information" not in body
    assert not body.endswith("See more")
    assert "Description" not in body.splitlines()


def test_description_from_text_lines_english_markers() -> None:
    lines = [
        "Marketplace",
        "Description",
        "Cosy townhouse in a gated village.",
        "Pets welcome.",
        "See more",
        "Seller information",
        "John Doe",
    ]
    assert description_from_text_lines(lines) == (
        "Cosy townhouse in a gated village.\nPets welcome."
    )


def test_description_from_text_lines_without_seller_marker_runs_to_end() -> None:
    lines = ["Описание", "Дом с садом.", "Парковка на 2 машины."]
    assert description_from_text_lines(lines) == "Дом с садом.\nПарковка на 2 машины."


@pytest.mark.parametrize(
    "lines",
    [
        None,
        [],
        ["Marketplace", "16 000 ฿/месяц", "Отправить сообщение"],  # маркера нет
        ["Описание", "Информация о продавце", "A. Example"],  # empty slice
        ["Описание", "Ещё", "Информация о продавце"],  # только «Ещё»
    ],
)
def test_description_from_text_lines_none(lines: list[str] | None) -> None:
    assert description_from_text_lines(lines) is None


# --- clean_fb_title ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "(6) Marketplace — 1 Bed 1 Bath - Apartment | Facebook",
            "1 Bed 1 Bath - Apartment",
        ),
        (
            "(6) Marketplace — 2 ห้องนอน 2 ห้องน้ำ - บ้าน | Facebook",
            "2 ห้องนอน 2 ห้องน้ำ - บ้าน",
        ),
        ("Marketplace | House for rent in Chalong | Facebook", "House for rent in Chalong"),
        ("(12) Marketplace - Townhouse Pruksa Ville | Facebook", "Townhouse Pruksa Ville"),
        ("Villa with pool", "Villa with pool"),  # уже чистый
        ("  (3) Marketplace — Condo Kathu | Facebook  ", "Condo Kathu"),
    ],
)
def test_clean_fb_title(raw: str, expected: str) -> None:
    assert clean_fb_title(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Недавно опубликовано",
        "недавно опубликовано",
        "Recently posted",
        "Marketplace",
        "(6) Marketplace | Facebook",  # ничего кроме chrome не осталось
    ],
)
def test_clean_fb_title_junk(raw: str | None) -> None:
    assert clean_fb_title(raw) is None


def test_clean_fb_title_uses_fixture_titles() -> None:
    assert clean_fb_title(load(GOOD)["title"]) == "2 Bed 2 Bath - Townhouse"
    assert clean_fb_title(load(SIDEBAR)["title"]) == "2 Bed 1 Bath - House"


# --- parse_published_at --------------------------------------------------

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # RU — то, что реально отдаёт FB в русской локали (nbsp внутри).
        ("Опубликовано 12 часов назад", "2026-08-25T00:00:00Z"),
        ("Опубликовано 1 час назад", "2026-08-25T11:00:00Z"),
        ("Опубликовано 30 минут назад", "2026-08-25T11:30:00Z"),
        ("Опубликовано 3 дня назад", "2026-08-22T12:00:00Z"),
        ("Опубликовано 5 дней назад", "2026-08-20T12:00:00Z"),
        ("Опубликовано 2 недели назад", "2026-08-11T12:00:00Z"),
        ("Опубликовано 1 месяц назад", "2026-07-26T12:00:00Z"),
        # EN — прежние шаблоны не сломаны.
        ("Listed 3 days ago", "2026-08-22T12:00:00Z"),
        ("Posted 2 weeks ago", "2026-08-11T12:00:00Z"),
        ("5 minutes ago", "2026-08-25T11:55:00Z"),
        ("Listed an hour ago", "2026-08-25T11:00:00Z"),
        ("Listed 6 months ago", "2026-02-26T12:00:00Z"),
    ],
)
def test_parse_published_at(raw: str, expected: str) -> None:
    assert parse_published_at(raw, NOW) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Отправить сообщение",
        "Опубликовано вчера",  # без числа и без «назад» — формат неизвестен
    ],
)
def test_parse_published_at_unknown_format(raw: str | None) -> None:
    assert parse_published_at(raw, NOW) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # RU — группы показывают абсолютную дату, не относительную. NOW —
        # 2026-08-25, так что 12 сентября без года читается как будущее в
        # этом году, а значит на самом деле прошлогоднее.
        ("12 сентября в 14:30", "2025-09-12T14:30:00Z"),
        ("12 сентября 2025", "2025-09-12T00:00:00Z"),
        # EN — оба варианта.
        ("September 12 at 2:30 PM", "2025-09-12T14:30:00Z"),
        ("September 12, 2025", "2025-09-12T00:00:00Z"),
    ],
)
def test_parse_published_at_absolute_forms(raw: str, expected: str) -> None:
    assert parse_published_at(raw, NOW) == expected


def test_parse_published_at_absolute_without_year_rolls_back_when_future() -> None:
    # NOW is 2026-08-25. A bare "12 сентября" without a year, read on that
    # date, is in the future for this year — so it must mean last year.
    assert parse_published_at("12 сентября", NOW) == "2025-09-12T00:00:00Z"


# --- parse_feed_timestamp --------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5 ч.", "2026-08-25T07:00:00Z"),
        ("2 дн.", "2026-08-23T12:00:00Z"),
        ("1 нед.", "2026-08-18T12:00:00Z"),
        ("5h", "2026-08-25T07:00:00Z"),
        ("2d", "2026-08-23T12:00:00Z"),
        ("1w", "2026-08-18T12:00:00Z"),
        ("вчера в 10:11", "2026-08-24T10:11:00Z"),
        ("сегодня в 09:05", "2026-08-25T09:05:00Z"),
        # Формы, которые понимает parse_published_at, тоже проходят.
        ("Опубликовано 3 дня назад", "2026-08-22T12:00:00Z"),
    ],
)
def test_parse_feed_timestamp(raw: str, expected: str) -> None:
    assert parse_feed_timestamp(raw, NOW) == expected


def test_parse_feed_timestamp_unknown_format() -> None:
    assert parse_feed_timestamp("Отправить сообщение", NOW) is None
    assert parse_feed_timestamp(None, NOW) is None


# --- group_post_title -------------------------------------------------------


def test_group_post_title_splits_page_title_after_group_name() -> None:
    page_title = (
        "(20+) Phuket Rent and Sale property | 3-bedroom villa in Bang Tao "
        "available for a yearly contract at 100,000 baht per mo"
    )
    assert group_post_title(page_title, "irrelevant") == (
        "3-bedroom villa in Bang Tao available for a yearly contract at "
        "100,000 baht per mo"
    )


def test_group_post_title_falls_back_to_first_body_line() -> None:
    assert group_post_title("Phuket Rent and Sale property", "Nice villa for rent") == (
        "Nice villa for rent"
    )


def test_group_post_title_truncates_long_first_line() -> None:
    line = "word " * 40
    title = group_post_title(None, line)
    assert title is not None
    assert len(title) <= 121  # 120 + ellipsis
    assert title.endswith("…")


def test_group_post_title_none_when_nothing_usable() -> None:
    assert group_post_title(None, None) is None
    assert group_post_title("Phuket Rent and Sale property", "") is None


def test_parse_published_at_reads_fixture_line() -> None:
    lines = load(GOOD)["text_lines"]
    line = next(line for line in lines if line.startswith("Listed"))
    assert parse_published_at(line, NOW) == "2026-08-25T00:00:00Z"


def test_parse_published_at_naive_now_treated_as_utc() -> None:
    assert (
        parse_published_at("Listed 1 day ago", datetime(2026, 8, 25, 12, 0, 0))
        == "2026-08-24T12:00:00Z"
    )


def test_parse_published_at_defaults_to_now() -> None:
    """Без `now` берётся текущее UTC-время — результат близок к нему."""
    result = parse_published_at("Listed 1 day ago")
    assert result is not None
    parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    delta = datetime.now(timezone.utc) - parsed
    assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)


# --- extract_gallery_urls ------------------------------------------------

_GALLERY = "https://scontent.xx.fbcdn.net/v/t39.30808-6/{}.jpg"
_AVATAR = "https://scontent.xx.fbcdn.net/v/t39.30808-1/avatar.jpg"


class _FakeImg:
    def __init__(
        self, src: str, alt: str | None, in_suggestion: bool = False
    ) -> None:
        self._src = src
        self._alt = alt
        self._in_suggestion = in_suggestion

    def get_attribute(self, name: str) -> str | None:
        return {"src": self._src, "alt": self._alt}[name]

    def evaluate(self, script: str) -> bool:
        assert "marketplace/item" in script
        return self._in_suggestion


class _FakePage:
    """Minimal stand-in: only ``query_selector_all`` is ever called."""

    def __init__(self, imgs, raises: bool = False) -> None:
        self._imgs = imgs
        self._raises = raises

    def query_selector_all(self, selector: str):
        if self._raises:
            raise RuntimeError("DOM churned mid-query")
        # The fake page stores only elements the real selector would match.
        assert "t39.30808-6" in selector
        return list(self._imgs)


def test_extract_gallery_urls_picks_the_modal_alt_group() -> None:
    """Gallery shots share one alt; suggestion cards each carry their own."""
    page = _FakePage(
        [
            _FakeImg(_GALLERY.format(i), "Фото Дом в Катху") for i in range(3)
        ]
        + [
            _FakeImg(_GALLERY.format("s1"), "Вилла в группе Аренда Пхукет"),
            _FakeImg(_GALLERY.format("s2"), "Кондо в группе Аренда Пхукет"),
        ]
    )
    assert extract_gallery_urls(page) == [_GALLERY.format(i) for i in range(3)]


def test_extract_gallery_urls_dedupes_keeping_dom_order() -> None:
    page = _FakePage(
        [
            _FakeImg(_GALLERY.format("a"), "Фото Дом"),
            _FakeImg(_GALLERY.format("b"), "Фото Дом"),
            _FakeImg(_GALLERY.format("a"), "Фото Дом"),
        ]
    )
    assert extract_gallery_urls(page) == [_GALLERY.format("a"), _GALLERY.format("b")]


@pytest.mark.parametrize(
    "imgs",
    [
        [],
        # Single shot per alt — no group is a gallery.
        [_FakeImg(_GALLERY.format("s1"), "Вилла в группе Аренда")],
        # Blank alt (avatars, spacers) never counts.
        [_FakeImg(_AVATAR, ""), _FakeImg(_AVATAR, None)],
        # Missing src is unusable.
        [_FakeImg("", "Фото Дом"), _FakeImg("", "Фото Дом")],
    ],
)
def test_extract_gallery_urls_empty_gallery(imgs) -> None:
    assert extract_gallery_urls(_FakePage(imgs)) == []


def test_extract_gallery_urls_swallows_selector_failure() -> None:
    assert extract_gallery_urls(_FakePage([], raises=True)) == []


def test_extract_gallery_urls_ignores_images_inside_suggestion_links() -> None:
    """A card linking to another ad is never this listing's gallery."""
    page = _FakePage(
        [
            _FakeImg(_GALLERY.format(i), "Фото Дом в Катху") for i in range(3)
        ]
        + [
            _FakeImg(_GALLERY.format("s1"), "Похожее", in_suggestion=True),
            _FakeImg(_GALLERY.format("s2"), "Похожее", in_suggestion=True),
            _FakeImg(_GALLERY.format("s3"), "Похожее", in_suggestion=True),
            _FakeImg(_GALLERY.format("s4"), "Похожее", in_suggestion=True),
        ]
    )
    assert extract_gallery_urls(page) == [_GALLERY.format(i) for i in range(3)]


def test_extract_gallery_urls_empty_when_only_suggestions_rendered() -> None:
    """The 28 August failure: the page has no gallery, only other ads' cards.

    Grouping alone hands back the largest suggestion group. The answer has to
    be an empty list — a listing with no pictures, not one with somebody
    else's.
    """
    page = _FakePage(
        [
            _FakeImg(_GALLERY.format(f"s{i}"), "Похожее объявление",
                     in_suggestion=True)
            for i in range(5)
        ]
    )
    assert extract_gallery_urls(page) == []


def test_extract_gallery_urls_drops_an_element_it_cannot_interrogate() -> None:
    """A stale element is dropped, not kept: a stranger's shot costs more."""

    class _Stale(_FakeImg):
        def evaluate(self, script: str) -> bool:
            raise RuntimeError("element is detached")

    page = _FakePage(
        [
            _FakeImg(_GALLERY.format("a"), "Фото Дом"),
            _Stale(_GALLERY.format("b"), "Фото Дом"),
            _FakeImg(_GALLERY.format("c"), "Фото Дом"),
        ]
    )
    assert extract_gallery_urls(page) == [_GALLERY.format("a"), _GALLERY.format("c")]
