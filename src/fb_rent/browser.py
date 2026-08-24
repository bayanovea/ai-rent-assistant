"""Playwright browser session helpers for fb-rent.

Uses a persistent Chromium context under ``BROWSER_PROFILE_DIR`` so the
user logs in once via ``fb-rent login`` and later headless scrapes reuse
the same cookies/localStorage. Facebook sessions typically live 1–4 weeks.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import sync_playwright

from .config import BROWSER_PROFILE_DIR, ensure_home

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright


# Latest stable Chrome on macOS at time of writing.
USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
VIEWPORT: dict[str, int] = {"width": 1280, "height": 900}
LOCALE: str = "en-US"

FB_HOME: str = "https://www.facebook.com/"
FB_LOGIN: str = "https://www.facebook.com/login"

LOGIN_POLL_INTERVAL_SEC: float = 3.0
LOGIN_POLL_TIMEOUT_SEC: float = 300.0


class LoginRequiredError(RuntimeError):
    """Raised when a scrape needs an authenticated FB session but has none."""


def start_playwright() -> "Playwright":
    """Start a Playwright driver. Caller is responsible for ``.stop()``."""
    return sync_playwright().start()


def launch_persistent(playwright: "Playwright", headless: bool) -> "BrowserContext":
    """Launch a persistent Chromium context under ``BROWSER_PROFILE_DIR``."""
    ensure_home()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=headless,
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        locale=LOCALE,
    )


def is_logged_in(context: "BrowserContext") -> bool:
    """Return True if the persistent context holds a live FB session.

    Opens facebook.com in a throwaway page and looks for the login form
    (email input). If the form is absent and the URL did not redirect to
    ``/login``, we treat the session as active.
    """
    page = context.new_page()
    try:
        page.goto(FB_HOME, wait_until="domcontentloaded", timeout=30_000)
        if "/login" in page.url:
            return False
        login_input = page.locator('input[name="email"]').first
        return login_input.count() == 0
    finally:
        page.close()


def login_flow(context: "BrowserContext") -> bool:
    """Open FB login in a visible page and poll until the user completes it.

    Returns True on success, False on timeout. The caller is expected to
    have launched the context with ``headless=False`` — this function only
    drives the login page, it does not re-launch the browser.
    """
    page = context.new_page()
    try:
        page.goto(FB_LOGIN, wait_until="domcontentloaded", timeout=30_000)
        deadline = time.monotonic() + LOGIN_POLL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if "/login" not in page.url and page.locator(
                'input[name="email"]'
            ).first.count() == 0:
                return True
            time.sleep(LOGIN_POLL_INTERVAL_SEC)
        return False
    finally:
        page.close()
