"""A slow detail page must not end the run.

Raising the timeout to 90 s was only half the fix: the exception still threw
straight out of ``scrape_detail`` and killed the whole scrape (26 August, band
25000–27499, 106 cards left unread). What the caller needs is the same answer
a page that never rendered gives — ``None``.
"""

from __future__ import annotations

import pytest

from fb_rent.browser import LoginRequiredError
from fb_rent.scraper_detail import scrape_detail


class _FakePage:
    """The two attributes the ``goto`` failure path touches, and nothing else."""

    def __init__(self, url: str = "https://www.facebook.com/marketplace/item/1/"):
        self.url = url
        self.closed = False
        self.email_input_count = 0

    def goto(self, url, **kwargs):  # noqa: ARG002 — signature parity
        raise TimeoutError("Page.goto: Timeout 90000ms exceeded.")

    def locator(self, selector):  # noqa: ARG002 — only the login probe uses it
        page = self

        class _First:
            def count(self):
                return page.email_input_count

        class _Locator:
            first = _First()

        return _Locator()

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


def test_goto_timeout_returns_none_and_closes_the_page():
    page = _FakePage()
    result = scrape_detail(_FakeContext(page), "https://example.invalid/item/1/")
    assert result is None
    assert page.closed, "the tab must not leak when goto times out"


def test_goto_timeout_on_the_login_page_still_raises():
    """A timeout while being bounced to /login is an expired session, not a
    slow page — the run must stop rather than write hundreds of empty rows."""
    page = _FakePage(url="https://www.facebook.com/login/?next=%2Fmarketplace")
    with pytest.raises(LoginRequiredError):
        scrape_detail(_FakeContext(page), "https://example.invalid/item/1/")
    assert page.closed


def test_a_broken_page_in_the_login_probe_is_not_a_login_redirect():
    """``_detect_login_redirect`` queries the DOM, which can throw on a page
    that never loaded. That must read as "not a login", not as a crash."""

    class _ExplodingPage(_FakePage):
        def locator(self, selector):  # noqa: ARG002
            raise RuntimeError("Target page, context or browser has been closed")

    page = _ExplodingPage()
    assert scrape_detail(_FakeContext(page), "https://example.invalid/item/1/") is None
