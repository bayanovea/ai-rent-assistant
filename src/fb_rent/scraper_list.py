"""Scrape the FB Marketplace search result list.

Opens the search URL in an authenticated persistent context, scrolls the
feed to accumulate listing cards, and pulls basic fields off each card:
``fb_id``, ``url``, ``title``, ``price_raw``, ``district``,
``cover_image_url``. Detail-page fields (description, published_at)
belong to ``scraper_detail`` and are fetched per-item afterwards.

Selectors here are best-effort. FB rewrites its CSS classes constantly,
so we lean on stable-ish anchors (``a[href*="/marketplace/item/"]``) and
semantic roles rather than ``.xrvj5dj``-style class names. Expect
occasional breakage on real data — that is what the MANUAL verification
step in the plan is for.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .browser import LoginRequiredError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ElementHandle, Page


ITEM_LINK_SELECTOR = 'a[href*="/marketplace/item/"]'
SCROLL_PAUSE_SEC: float = 1.5
INITIAL_LOAD_TIMEOUT_MS: int = 30_000
INITIAL_CARDS_TIMEOUT_MS: int = 20_000
NO_PROGRESS_LIMIT: int = 2

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")


def _extract_fb_id(href: str) -> str | None:
    match = _ITEM_ID_RE.search(href)
    return match.group(1) if match else None


def _normalise_url(href: str) -> str:
    """Return an absolute URL stripped of tracking/query params."""
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.facebook.com" + href
    parsed = urlparse(href)
    path = parsed.path
    match = _ITEM_ID_RE.search(path)
    if match:
        return f"https://www.facebook.com/marketplace/item/{match.group(1)}/"
    return href


def _card_text_lines(handle: "ElementHandle") -> list[str]:
    """Return non-empty text lines from a card, in visual order."""
    raw = handle.inner_text() or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _pick_price_line(lines: list[str]) -> str | None:
    """First line that looks like a price (has THB/฿ or a digit run)."""
    for line in lines:
        if re.search(r"฿|thb|baht|bt\.?", line, re.IGNORECASE):
            return line
        if re.search(r"\d{3}", line):
            return line
    return None


def _pick_title(lines: list[str], price_line: str | None) -> str | None:
    """First non-price line — FB usually renders title right below price."""
    for line in lines:
        if line == price_line:
            continue
        if re.fullmatch(r"[฿$€]?\s*\d[\d\s,.]*(?:k|K)?", line):
            continue
        return line
    return None


def _pick_district(lines: list[str], price_line: str | None, title: str | None) -> str | None:
    """Location line — FB shows it as the last text line on the card."""
    for line in reversed(lines):
        if line in {price_line, title}:
            continue
        if re.fullmatch(r"[฿$€]?\s*\d[\d\s,.]*(?:k|K)?", line):
            continue
        return line
    return None


def _extract_card(handle: "ElementHandle") -> dict[str, Any] | None:
    """Extract fields from a single marketplace card anchor."""
    href = handle.get_attribute("href") or ""
    fb_id = _extract_fb_id(href)
    if not fb_id:
        return None

    url = _normalise_url(href)
    lines = _card_text_lines(handle)
    price_raw = _pick_price_line(lines)
    title = _pick_title(lines, price_raw)
    district = _pick_district(lines, price_raw, title)

    cover_image_url: str | None = None
    img = handle.query_selector("img")
    if img is not None:
        src = img.get_attribute("src")
        if src:
            cover_image_url = src

    return {
        "fb_id": fb_id,
        "url": url,
        "title": title,
        "price_raw": price_raw,
        "district": district,
        "cover_image_url": cover_image_url,
    }


def _detect_login_redirect(page: "Page") -> bool:
    if "/login" in page.url or "checkpoint" in page.url:
        return True
    if page.locator('input[name="email"]').first.count() > 0:
        return True
    return False


def scrape_list(
    context: "BrowserContext",
    url: str,
    max_items: int,
) -> list[dict[str, Any]]:
    """Scrape up to ``max_items`` unique listing cards from ``url``.

    Scrolls the feed until either ``max_items`` unique cards are collected
    or two consecutive scroll attempts produce no new cards. As a hard
    safety net, aborts after ``max_items * 2`` scroll attempts regardless.

    Raises
    ------
    LoginRequiredError
        If FB redirects to the login/checkpoint page or the feed renders
        zero listing links (both symptoms of an expired session).
    """
    if max_items <= 0:
        return []

    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=INITIAL_LOAD_TIMEOUT_MS)

        if _detect_login_redirect(page):
            raise LoginRequiredError(
                "Facebook redirected to login. Run `fb-rent login` and retry."
            )

        try:
            page.wait_for_selector(
                ITEM_LINK_SELECTOR, timeout=INITIAL_CARDS_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            raise LoginRequiredError(
                "No marketplace cards rendered — session likely expired or "
                "the marketplace layout changed. Run `fb-rent login` and retry."
            ) from exc

        seen: dict[str, dict[str, Any]] = {}
        no_progress = 0
        scroll_attempts = 0
        hard_cap = max(max_items * 2, 4)

        while len(seen) < max_items and scroll_attempts < hard_cap:
            handles = page.query_selector_all(ITEM_LINK_SELECTOR)
            before = len(seen)
            for handle in handles:
                if len(seen) >= max_items:
                    break
                card = _extract_card(handle)
                if card is None:
                    continue
                fb_id = card["fb_id"]
                if fb_id in seen:
                    continue
                seen[fb_id] = card

            if len(seen) >= max_items:
                break

            if len(seen) == before:
                no_progress += 1
                if no_progress >= NO_PROGRESS_LIMIT:
                    break
            else:
                no_progress = 0

            page.mouse.wheel(0, 4000)
            time.sleep(SCROLL_PAUSE_SEC)
            scroll_attempts += 1

        if not seen:
            raise LoginRequiredError(
                "Marketplace returned zero listings — likely session expired. "
                "Run `fb-rent login` and retry."
            )

        return list(seen.values())
    finally:
        page.close()
