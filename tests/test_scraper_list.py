"""Tests for the list-feed scraper that do not need a real browser.

Only the empty-feed decision lives here: everything else in
``scraper_list`` is DOM extraction against selectors that a fake page would
not exercise honestly. An empty feed is different — it is the one case where
the scraper has to choose between "the session is dead" and "this question
simply has no answer", and a price-band sweep depends on that choice.
"""

from __future__ import annotations

import pytest

from fb_rent.browser import LoginRequiredError
from fb_rent.scraper_list import scrape_list


class _Locator:
    @property
    def first(self) -> "_Locator":
        return self

    def count(self) -> int:
        return 0


class _Handle:
    """A marketplace card anchor, counting how often it is read in full."""

    def __init__(self, fb_id: str, reads: list[str]) -> None:
        self.fb_id = fb_id
        self._reads = reads

    def get_attribute(self, name: str) -> str | None:
        return f"/marketplace/item/{self.fb_id}/" if name == "href" else None

    def inner_text(self) -> str:
        self._reads.append(self.fb_id)
        return "22 000 ฿\n2 Bed 1 Bath - House\nKathu, Phuket"

    def query_selector(self, selector: str) -> None:
        return None


class _Mouse:
    def wheel(self, x: int, y: int) -> None:
        pass


class _FeedPage:
    """A feed that hands back the same cards however far it is scrolled."""

    url = "https://www.facebook.com/marketplace/110729412288885/propertyrentals"
    mouse = _Mouse()

    def __init__(self, handles: list[_Handle]) -> None:
        self._handles = handles

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        pass

    def locator(self, selector: str) -> "_Locator":
        return _Locator()

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        pass

    def query_selector_all(self, selector: str) -> list[_Handle]:
        return self._handles

    def close(self) -> None:
        pass


class _FeedContext:
    def __init__(self, handles: list[_Handle]) -> None:
        self._page = _FeedPage(handles)

    def new_page(self) -> _FeedPage:
        return self._page


class _Page:
    url = "https://www.facebook.com/marketplace/110729412288885/propertyrentals"

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        pass

    def locator(self, selector: str) -> _Locator:
        return _Locator()

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        raise TimeoutError("no cards rendered")

    def query_selector_all(self, selector: str) -> list:
        return []

    def close(self) -> None:
        pass


class _Context:
    def new_page(self) -> _Page:
        return _Page()


def test_an_empty_feed_reads_as_a_dead_session_by_default() -> None:
    with pytest.raises(LoginRequiredError):
        scrape_list(_Context(), "https://example.test/", max_items=10)


def test_allow_empty_returns_no_cards_instead() -> None:
    assert scrape_list(
        _Context(), "https://example.test/", max_items=10, allow_empty=True
    ) == []


def test_a_card_is_read_in_full_only_once(monkeypatch) -> None:
    """Scrolling re-offers every card, and reading them all again is the cost.

    The feed keeps everything it has rendered, so pass N sees N screens worth
    of anchors. Deciding on the href is one round trip; ``inner_text`` on a
    card already collected is the same work done twice, and on a long sweep
    it is most of the run.
    """
    monkeypatch.setattr("fb_rent.scraper_list.time.sleep", lambda _s: None)
    reads: list[str] = []
    handles = [_Handle("1", reads), _Handle("2", reads)]

    cards = scrape_list(_FeedContext(handles), "https://example.test/", max_items=10)

    assert [card["fb_id"] for card in cards] == ["1", "2"]
    assert reads == ["1", "2"]
