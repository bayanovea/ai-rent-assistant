"""Tests for group-post extraction: the DOM logic unique to scraper_group.

The load-bearing trap this module protects against: a permalink page can
render a *second*, unrelated post above the one the URL names, and its
``img`` tags carry an empty ``alt`` — so marketplace's alt-grouping filter
cannot separate "this post's gallery" from "the other post's gallery" the
way it does for marketplace items. The article-scoping tests below are
what stands in for that filter.
"""

from __future__ import annotations

from fb_rent.scraper_group import (
    GROUP_POST_RE,
    _extract_body,
    _extract_price_raw,
    _extract_published_at,
    _find_own_article,
    _harvest_post_ids,
    _try_expand_description,
    extract_group_gallery_urls,
)

_GALLERY = "https://scontent.xx.fbcdn.net/v/t39.30808-6/{}.jpg"


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _FakeImg:
    def __init__(self, src: str | None) -> None:
        self._src = src

    def get_attribute(self, name: str) -> str | None:
        assert name == "src"
        return self._src


class _FakeButton:
    def __init__(self) -> None:
        self.clicked = False

    def click(self, timeout: int) -> None:  # noqa: ARG002
        self.clicked = True


class _FakeArticle:
    """Minimal stand-in for a `div[role="article"]` ElementHandle."""

    def __init__(
        self,
        *,
        hrefs: list[str] | None = None,
        messages: list[str] | None = None,
        dir_auto: list[str] | None = None,
        images: list[str | None] | None = None,
        text: str = "",
        button: _FakeButton | None = None,
    ) -> None:
        self._hrefs = hrefs or []
        self._messages = [_FakeMessage(t) for t in (messages or [])]
        self._dir_auto = [_FakeMessage(t) for t in (dir_auto or [])]
        self._images = [_FakeImg(s) for s in (images or [])]
        self._text = text
        self._button = button

    def eval_on_selector_all(self, selector: str, script: str):  # noqa: ARG002
        return self._hrefs

    def query_selector_all(self, selector: str):
        if "data-ad-preview" in selector or "data-ad-comet-preview" in selector:
            return self._messages
        if selector == 'div[dir="auto"]':
            return self._dir_auto
        if "t39.30808-6" in selector:
            return self._images
        return []

    def query_selector(self, selector: str):
        if self._button is not None and (
            "Ещё" in selector or "See more" in selector or "See More" in selector
        ):
            return self._button
        return None

    def inner_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(self, articles: list[_FakeArticle]) -> None:
        self._articles = articles

    def query_selector_all(self, selector: str):
        assert selector == 'div[role="article"]'
        return self._articles

    def eval_on_selector_all(self, selector: str, script: str):  # noqa: ARG002
        hrefs: list[str] = []
        for article in self._articles:
            hrefs.extend(article._hrefs)
        return hrefs


# --- GROUP_POST_RE / _harvest_post_ids --------------------------------------


def test_group_post_re_matches_posts_and_permalink_forms() -> None:
    assert GROUP_POST_RE.search(
        "https://www.facebook.com/groups/phuketrentsale/posts/9000000000000003/"
    ).group(2) == "9000000000000003"
    assert GROUP_POST_RE.search(
        "/groups/170117323644873/permalink/123456789/?ref=x"
    ).group(2) == "123456789"


def test_harvest_post_ids_collects_unique_ids_from_hrefs() -> None:
    page = _FakePage(
        [
            _FakeArticle(
                hrefs=[
                    "/groups/phuketrentsale/posts/111/?__cft__[0]=x",
                    "/groups/phuketrentsale/posts/111/?__cft__[0]=y",
                    "/groups/phuketrentsale/user/999/",
                ]
            ),
            _FakeArticle(hrefs=["/groups/phuketrentsale/posts/222/"]),
        ]
    )
    found = _harvest_post_ids(page, "phuketrentsale")
    assert set(found.keys()) == {"111", "222"}


# --- _find_own_article -------------------------------------------------


def test_find_own_article_matches_by_multi_permalinks_self_link() -> None:
    other = _FakeArticle(
        hrefs=["https://www.facebook.com/groups/999/?multi_permalinks=999999"],
        messages=["An unrelated recommended post."],
    )
    mine = _FakeArticle(
        hrefs=["https://www.facebook.com/groups/598821800173652/?multi_permalinks=123"],
        messages=["My actual post text."],
    )
    page = _FakePage([other, mine])
    assert _find_own_article(page, "123") is mine


def test_find_own_article_falls_back_to_longest_message_when_no_self_link() -> None:
    short = _FakeArticle(hrefs=[], messages=["Short."])
    long = _FakeArticle(hrefs=[], messages=["A much longer post body that wins."])
    page = _FakePage([short, long])
    assert _find_own_article(page, "unknown-id") is long


def test_find_own_article_returns_none_when_nothing_usable() -> None:
    page = _FakePage([_FakeArticle(hrefs=[], messages=[])])
    assert _find_own_article(page, "123") is None


# --- _extract_body -------------------------------------------------------


def test_extract_body_prefers_the_ad_preview_message() -> None:
    article = _FakeArticle(
        messages=["The real post body."],
        dir_auto=["Some unrelated short auto div."],
    )
    assert _extract_body(article) == "The real post body."


def test_extract_body_falls_back_to_longest_dir_auto_block() -> None:
    article = _FakeArticle(
        messages=[],
        dir_auto=["short", "x" * 80, "y" * 40],
    )
    assert _extract_body(article) == "x" * 80


def test_extract_body_none_when_nothing_matches() -> None:
    article = _FakeArticle(messages=[], dir_auto=["too short"])
    assert _extract_body(article) is None


# --- extract_group_gallery_urls -------------------------------------------


def test_extract_group_gallery_urls_keeps_every_image_in_the_article() -> None:
    """No alt-grouping: group-post images carry an empty alt, so the
    article boundary is the only filter — everything scoped to it belongs
    to this post."""
    article = _FakeArticle(images=[_GALLERY.format(i) for i in range(5)])
    assert extract_group_gallery_urls(article) == [_GALLERY.format(i) for i in range(5)]


def test_extract_group_gallery_urls_dedupes_and_drops_missing_src() -> None:
    article = _FakeArticle(
        images=[_GALLERY.format(1), _GALLERY.format(1), None, _GALLERY.format(2)]
    )
    assert extract_group_gallery_urls(article) == [_GALLERY.format(1), _GALLERY.format(2)]


def test_extract_group_gallery_urls_empty_when_no_images() -> None:
    assert extract_group_gallery_urls(_FakeArticle(images=[])) == []


# --- _try_expand_description ----------------------------------------------


def test_try_expand_description_clicks_the_see_more_button() -> None:
    button = _FakeButton()
    article = _FakeArticle(button=button)
    _try_expand_description(article)
    assert button.clicked


def test_try_expand_description_is_a_noop_without_a_button() -> None:
    article = _FakeArticle()
    _try_expand_description(article)  # must not raise


# --- price / published_at line scanning ------------------------------------


def test_extract_price_raw_matches_baht_variants() -> None:
    lines = ["Nice studio", "30,000 THB/month", "Deposit 1 month"]
    assert _extract_price_raw(lines) == "30,000 THB/month"


def test_extract_price_raw_matches_cyrillic_bat() -> None:
    lines = ["Вилла в Банг Тао", "100 000 бат в месяц"]
    assert _extract_price_raw(lines) == "100 000 бат в месяц"


def test_extract_price_raw_none_when_no_currency_line() -> None:
    assert _extract_price_raw(["Nice studio", "Deposit 1 month"]) is None


def test_extract_published_at_reads_the_compact_header_form() -> None:
    lines = ["Phuket Rent and Sale property", "A. Example", "·", "6 ч.", "·", "Body text"]
    result = _extract_published_at(lines)
    assert result is not None
    assert result.endswith("Z")


def test_extract_published_at_none_when_no_timestamp_line() -> None:
    assert _extract_published_at(["Body text only", "No timestamp here"]) is None
