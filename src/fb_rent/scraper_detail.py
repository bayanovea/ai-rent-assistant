"""Scrape a single FB Marketplace listing detail page.

Opens ``/marketplace/item/{id}/`` in the authenticated persistent context,
expands the collapsed description if the "See more" button is present, and
pulls the fields the storage layer cares about: ``description``,
``published_at`` (normalised to an ISO-8601 UTC timestamp from FB's
relative "3 days ago" / "3 дня назад" text), ``price_raw`` (may differ
from the list card), and a ``raw_json`` dict of everything the DOM gave
us for debugging and AI enrichment.

Rate limiting between successive calls is the orchestrator's job — this
function makes exactly one page request and returns. Deleted or removed
listings (FB serves a 404-ish "content unavailable" page) return ``None``
instead of raising so the caller can skip them.

Selectors are best-effort. FB churns CSS classes constantly; we lean on
``aria-label``/text-based locators and only fall back to structural
guesses. Real-world tuning is the MANUAL step in the plan.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from .browser import LoginRequiredError
from .extractors import (
    clean_fb_title,
    description_from_text_lines,
    parse_published_at,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


def _timeout_ms(name: str, default: int) -> int:
    """Read a page timeout from the environment, falling back to *default*.

    An unattended catch-up run walks hundreds of detail pages in a row, and a
    single slow load raises out of :func:`scrape_detail` and kills the whole
    process. Patience is cheap there and expensive during an interactive
    sweep, so the ceiling is a knob rather than a new default.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


PAGE_LOAD_TIMEOUT_MS: int = _timeout_ms("FB_RENT_PAGE_LOAD_TIMEOUT_MS", 30_000)
CONTENT_TIMEOUT_MS: int = _timeout_ms("FB_RENT_CONTENT_TIMEOUT_MS", 15_000)
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


def _detect_login_redirect(page: "Page") -> bool:
    if "/login" in page.url or "checkpoint" in page.url:
        return True
    if page.locator('input[name="email"]').first.count() > 0:
        return True
    return False


def _safe_login_redirect(page: "Page") -> bool:
    """``_detect_login_redirect`` for a page that may not have loaded.

    Called from the ``goto`` failure path, where querying the DOM can throw
    in its own right; a broken page is simply not a login redirect.
    """
    try:
        return _detect_login_redirect(page)
    except Exception:  # noqa: BLE001 — page may be in any state
        return False


def _detect_unavailable(page: "Page") -> bool:
    """Return True if FB is showing a removed/unavailable listing page."""
    try:
        body_text = (page.locator("body").first.inner_text() or "").lower()
    except Exception:  # noqa: BLE001 — page may not be ready
        return False
    return any(pat in body_text for pat in _UNAVAILABLE_PATTERNS)


# Measured 2026-09-13: two listings whose `/marketplace/item/<id>/` URL
# loaded a normal-looking page anyway — no "content isn't available" text —
# because FB had already bounced the navigation to the category browse page
# with this marker instead of rendering a removal wall on the item URL
# itself. `page.url` after `goto` is the *final* URL, so this is a plain
# substring check, no locale involved.
_UNAVAILABLE_REDIRECT_MARKER = "unavailable_product="


def _detect_redirected_away(page: "Page") -> bool:
    return _UNAVAILABLE_REDIRECT_MARKER in page.url


# The other gap from the same day: a listing the seller marked "rented" /
# "sold" without deleting it keeps rendering its own page normally, with a
# short status badge next to the title — none of `_UNAVAILABLE_PATTERNS`
# fire because the page isn't a removal wall. The wording is locale-bound
# like everything else FB renders (this account is Russian-language), so
# match loosely and extend as new locales turn up.
_MARKED_UNAVAILABLE_BADGES = ("сдано в аренду", "продано", "rented", "sold")


def _detect_marked_unavailable(page: "Page") -> bool:
    """A "rented"/"sold" badge on *this* listing, not on a sidebar card.

    Recommendation cards ("Выбор дня") carry the same badge for someone
    else's listing and are wrapped in `a[href*="/marketplace/item/"]` —
    the exact signal `extract_gallery_urls` already uses to tell this
    listing's own gallery from a suggestion's. A badge outside any such
    link is this listing's own.
    """
    try:
        for phrase in _MARKED_UNAVAILABLE_BADGES:
            matches = page.get_by_text(phrase, exact=False)
            for i in range(matches.count()):
                try:
                    inside_item_link = matches.nth(i).evaluate(
                        "e => { let n = e; while (n) { "
                        "if (n.tagName === 'A' && (n.getAttribute('href') || '')"
                        ".includes('/marketplace/item/')) return true; "
                        "n = n.parentElement; } return false; }"
                    )
                except Exception:  # noqa: BLE001 — element may be stale
                    continue
                if not inside_item_link:
                    return True
    except Exception:  # noqa: BLE001 — page may not be ready
        return False
    return False


def _is_gone(page: "Page") -> bool:
    """Every way FB tells us this listing is no longer up, combined."""
    return (
        _detect_redirected_away(page)
        or _detect_unavailable(page)
        or _detect_marked_unavailable(page)
    )


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
    """First line carrying a relative timestamp, as an ISO-8601 UTC string.

    Iteration 1 stored FB's raw wording ("Listed 3 days ago") and got NULL
    for every Russian-locale page; ``parse_published_at`` handles both
    locales and hands back an absolute date the storage layer can sort on.
    """
    for line in lines:
        published = parse_published_at(line)
        if published:
            return published
    return None


def _extract_description(page: "Page", lines: list[str]) -> str | None:
    """Cut the ad body out of the page text, longest-block as fallback.

    The reliable signal is the "Описание"/"Description" heading in the
    flat page text — everything between it and the seller block is the
    seller's own copy. Only when that marker is missing do we fall back
    to iteration 1's "longest text node" guess, which on real pages
    grabbed the unrelated "Выбор дня" sidebar for half the listings.
    """
    from_markers = description_from_text_lines(lines)
    if from_markers:
        return from_markers

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


# Gallery photos live under FB's ``t39.30808-6`` CDN prefix; avatars and
# icons sit under ``t39.30808-1`` / ``t1.30497-1`` and are filtered out by
# the prefix alone.
_GALLERY_IMG_SELECTOR = 'img[src*="/t39.30808-6/"]'

# A "similar listings" card is a link to *another* ad; a gallery shot is not
# inside a link at all. Measured on 28 August 2026 across three live pages:
# every suggestion card sat inside such an anchor, every one of the 17
# gallery images sat outside one.
_SUGGESTION_LINK_JS = (
    "(img) => Boolean(img.closest('a[href*=\"/marketplace/item/\"]'))"
)


def extract_gallery_urls(page: "Page") -> list[str]:
    """Return the listing's gallery image URLs, in DOM (= gallery) order.

    The spike found the whole gallery already in the DOM — no lightbox
    click needed. What has to be separated out are the "similar listings"
    cards, which live under the same CDN prefix.

    Two filters, in this order. First the structural one: a suggestion card
    is wrapped in an anchor to another marketplace item, and a gallery shot
    is in no anchor at all. Then the ``alt`` grouping: gallery shots share
    one caption while each card carries its own, so the modal group wins.

    The link filter is what the grouping alone could not do. On a page whose
    gallery is simply absent — it happens, and no amount of waiting or
    scrolling brings it back — the largest ``alt`` group was a *suggestion*
    group of five, and on 28 August six shortlisted listings were handed
    565x565 cards from other people's ads as if they were the flat. Grouping
    has no way to notice that: it ranks groups against each other and cannot
    tell that none of them belongs to this listing. The right answer for such
    a page is an empty list, and only the link test produces it.

    Best-effort like the rest of this module: a churned DOM yields an empty
    list, never an exception. An element that cannot be interrogated is
    dropped rather than kept — for a gallery, missing a shot is cheap and
    keeping a stranger's is not.
    """
    try:
        handles = page.query_selector_all(_GALLERY_IMG_SELECTOR)
    except Exception:  # noqa: BLE001 — DOM may be transient
        return []

    by_alt: dict[str, list[str]] = {}
    for handle in handles:
        try:
            src = handle.get_attribute("src")
            alt = handle.get_attribute("alt")
            in_suggestion = handle.evaluate(_SUGGESTION_LINK_JS)
        except Exception:  # noqa: BLE001 — element may be stale
            continue
        if in_suggestion:
            continue
        if not src or not alt or not alt.strip():
            continue
        by_alt.setdefault(alt.strip(), []).append(src)

    if not by_alt:
        return []

    # Ties go to the group seen first — dicts keep insertion order, and the
    # gallery renders above the suggestions.
    best = max(by_alt.values(), key=len)
    if len(best) < 2:
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for src in best:
        if src in seen:
            continue
        seen.add(src)
        urls.append(src)
    return urls


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

    Returns a dict with ``url``, ``title``, ``description``,
    ``published_at``, ``price_raw`` and ``raw_json`` on success. ``title``
    is the page ``<title>`` with FB's chrome stripped, or ``None`` when
    nothing survives that — the list card's own title is preferred anyway. Returns ``None`` if the
    listing has been removed / is unavailable.

    Raises
    ------
    LoginRequiredError
        If FB redirects to the login/checkpoint page during the request.
    """
    page = context.new_page()
    try:
        try:
            page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            # A single slow page must not end the run. Raising the timeout to
            # 90 s was only half the fix: on 26 August a `Page.goto: Timeout
            # 90000ms exceeded` still threw straight out of here and killed a
            # band with 106 of its cards unread. Treated like a page that
            # never rendered — the caller keeps the list-card row and the
            # catch-up driver repairs it by `--ids`.
            if _safe_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            return None

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
            if _is_gone(page):
                return None
            # Main region never rendered — treat as unavailable rather
            # than crashing the whole scrape run.
            return None

        if _is_gone(page):
            return None

        _try_expand_description(page)

        lines = _page_text_lines(page)
        description = _extract_description(page, lines)
        published_at = _extract_published_at(lines)
        price_raw = _extract_price_raw(lines)

        raw_page_title = page.title() or None
        raw_json: dict[str, Any] = {
            "url": url,
            "final_url": page.url,
            "title": raw_page_title,
            "text_lines": lines[:200],
            "gallery_urls": extract_gallery_urls(page),
        }

        return {
            "url": url,
            "title": clean_fb_title(raw_page_title),
            "description": description,
            "published_at": published_at,
            "price_raw": price_raw,
            "raw_json": raw_json,
        }
    finally:
        page.close()


def check_listing_alive(context: "BrowserContext", url: str) -> bool | None:
    """Tri-state liveness check: does not read or write a single field.

    ``scrape_detail`` collapses "FB says this ad is gone" and "the page just
    didn't load" into the same ``None`` — fine for a scrape, where the
    caller's fallback is the list-card row either way, but wrong for a
    liveness check: a timeout is not evidence the listing was pulled, and
    auto-rejecting on it would take down a live ad because of a slow load.
    This mirrors ``scrape_detail``'s navigation and only ever returns
    ``False`` where ``_is_gone`` fires: a "content isn't available" wall, a
    redirect to the category page with ``unavailable_product=1`` (the item
    page can render fine and still bounce there), or a "rented"/"sold" badge
    on the listing's own page — the seller closed the ad without FB pulling
    it. Everything else that isn't a confirmed "still there" comes back
    ``None``: inconclusive, try again later, never acted on.
    """
    page = context.new_page()
    try:
        try:
            page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _safe_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            return None

        if _detect_login_redirect(page):
            raise LoginRequiredError(
                "Facebook redirected to login. Run `fb-rent login` and retry."
            )

        if _is_gone(page):
            return False

        try:
            page.wait_for_selector('div[role="main"]', timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            return False if _is_gone(page) else None

        return False if _is_gone(page) else True
    finally:
        page.close()
