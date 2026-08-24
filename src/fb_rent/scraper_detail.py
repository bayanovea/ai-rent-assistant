"""Scrape a single FB Marketplace listing detail page.

Opens ``/marketplace/item/{id}/`` in the authenticated persistent context,
expands the collapsed description if the "See more" button is present, and
pulls the fields the storage layer cares about: ``description``,
``published_at`` (raw FB text like ``"Listed 3 days ago"`` — normalisation
is deferred to a later iteration), ``price_raw`` (may differ from the list
card), and a ``raw_json`` dict of everything the DOM gave us for debugging
and future AI enrichment.

Rate limiting between successive calls is the orchestrator's job — this
function makes exactly one page request and returns. Deleted or removed
listings (FB serves a 404-ish "content unavailable" page) return ``None``
instead of raising so the caller can skip them.

Selectors are best-effort. FB churns CSS classes constantly; we lean on
``aria-label``/text-based locators and only fall back to structural
guesses. Real-world tuning is the MANUAL step in the plan.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .browser import LoginRequiredError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


PAGE_LOAD_TIMEOUT_MS: int = 30_000
CONTENT_TIMEOUT_MS: int = 15_000
SEE_MORE_TIMEOUT_MS: int = 2_000

# Text FB shows when a listing has been removed or is not visible to the
# current viewer. Match loosely — the exact wording varies by locale and
# A/B test bucket.
_UNAVAILABLE_PATTERNS = (
    "content isn't available",
    "content is no longer available",
    "isn't available right now",
    "may have been removed",
    "the link you followed may be broken",
)

_PRICE_LINE_RE = re.compile(r"฿|thb|baht", re.IGNORECASE)
_PUBLISHED_RE = re.compile(
    r"(listed\s+.+?ago|posted\s+.+?ago|\d+\s+(?:minute|hour|day|week|month)s?\s+ago)",
    re.IGNORECASE,
)


def _detect_login_redirect(page: "Page") -> bool:
    if "/login" in page.url or "checkpoint" in page.url:
        return True
    if page.locator('input[name="email"]').first.count() > 0:
        return True
    return False


def _detect_unavailable(page: "Page") -> bool:
    """Return True if FB is showing a removed/unavailable listing page."""
    try:
        body_text = (page.locator("body").first.inner_text() or "").lower()
    except Exception:  # noqa: BLE001 — page may not be ready
        return False
    return any(pat in body_text for pat in _UNAVAILABLE_PATTERNS)


def _try_expand_description(page: "Page") -> None:
    """Click FB's "See more" button on the description, if present.

    Best-effort: a missing button just means the description was already
    short enough to render in full.
    """
    for selector in (
        'div[role="button"]:has-text("See more")',
        'span:has-text("See more")',
        'div[role="button"]:has-text("See More")',
    ):
        try:
            button = page.locator(selector).first
            if button.count() == 0:
                continue
            button.click(timeout=SEE_MORE_TIMEOUT_MS)
            return
        except Exception:  # noqa: BLE001 — button may be stale or non-clickable
            continue


def _extract_price_raw(lines: list[str]) -> str | None:
    """First line that carries an explicit THB currency marker.

    No "any 3-digit run" fallback — see scraper_list._pick_price_line for
    the rationale (phone numbers / sqm bleed-through).
    """
    for line in lines:
        if _PRICE_LINE_RE.search(line):
            return line
    return None


def _extract_published_at(lines: list[str]) -> str | None:
    for line in lines:
        match = _PUBLISHED_RE.search(line)
        if match:
            return match.group(0).strip()
    return None


def _extract_description(page: "Page", lines: list[str]) -> str | None:
    """Pick the longest text block on the page as the description.

    FB's detail page has one dominant multi-line text block for the
    listing body. We approximate by taking the longest ``<span>`` /
    ``<div>`` text node; fall back to the longest line from the body.
    """
    candidates: list[str] = []
    try:
        handles = page.query_selector_all(
            'div[role="main"] span, div[role="main"] div[dir="auto"]'
        )
        for handle in handles:
            text = (handle.inner_text() or "").strip()
            if len(text) >= 80:
                candidates.append(text)
    except Exception:  # noqa: BLE001 — DOM may be transient
        pass

    if candidates:
        return max(candidates, key=len)

    longest = ""
    for line in lines:
        if len(line) > len(longest):
            longest = line
    return longest or None


def _page_text_lines(page: "Page") -> list[str]:
    try:
        raw = page.locator("body").first.inner_text() or ""
    except Exception:  # noqa: BLE001
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def scrape_detail(
    context: "BrowserContext",
    url: str,
) -> dict[str, Any] | None:
    """Fetch and parse a single marketplace listing detail page.

    Returns a dict with ``url``, ``description``, ``published_at``,
    ``price_raw`` and ``raw_json`` on success. Returns ``None`` if the
    listing has been removed / is unavailable.

    Raises
    ------
    LoginRequiredError
        If FB redirects to the login/checkpoint page during the request.
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

        if _detect_login_redirect(page):
            raise LoginRequiredError(
                "Facebook redirected to login. Run `fb-rent login` and retry."
            )

        try:
            page.wait_for_selector('div[role="main"]', timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            if _detect_unavailable(page):
                return None
            # Main region never rendered — treat as unavailable rather
            # than crashing the whole scrape run.
            return None

        if _detect_unavailable(page):
            return None

        _try_expand_description(page)

        lines = _page_text_lines(page)
        description = _extract_description(page, lines)
        published_at = _extract_published_at(lines)
        price_raw = _extract_price_raw(lines)

        raw_json: dict[str, Any] = {
            "url": url,
            "final_url": page.url,
            "title": page.title() or None,
            "text_lines": lines[:200],
        }

        return {
            "url": url,
            "description": description,
            "published_at": published_at,
            "price_raw": price_raw,
            "raw_json": raw_json,
        }
    finally:
        page.close()
