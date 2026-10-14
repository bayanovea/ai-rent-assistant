"""Scrape rental posts from Facebook groups — a second listing source.

A group post has no title, no price facet and no "Описание" heading the
way a marketplace item does, but it does have a clean, stable permalink:
``/groups/<group_id>/posts/<post_id>/``. That permalink page is where all
the content comes from — the group's chronological feed
(:func:`scrape_group_feed`) is used only to *discover* post ids, not to
read them, because the feed is virtualized (scrolled-past posts leave the
DOM) and its post text is truncated behind a per-post "See more".

The one wrinkle a permalink page has that a marketplace item page does
not: FB sometimes renders a second, unrelated post above the one the URL
names (a cross-group recommendation). Both are top-level
``div[role="article"]`` elements. The one that is *this* post is found by
its own self-link — an anchor inside it carrying
``multi_permalinks=<post_id>`` — the same kind of structural signal
``scraper_detail`` uses to tell a marketplace listing's own gallery from a
suggestion card's. Scoping every extraction to that one article is also
what makes group-post galleries tractable: unlike marketplace images,
group-post ``img`` tags carry an empty ``alt``, so the alt-grouping trick
does not apply here — the article boundary is the only filter needed, and
it is a stronger one.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .browser import LoginRequiredError
from .extractors import group_post_title, parse_feed_timestamp
from .scraper_detail import (
    CONTENT_TIMEOUT_MS,
    PAGE_LOAD_TIMEOUT_MS,
    SEE_MORE_TIMEOUT_MS,
    _detect_login_redirect,
    _detect_unavailable,
    _safe_login_redirect,
)
from .url_builder import group_feed_url, group_post_url

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ElementHandle, Page

GROUP_POST_RE = re.compile(r"/groups/([^/?]+)/(?:posts|permalink)/(\d+)")

ARTICLE_SELECTOR = 'div[role="article"]'
_MESSAGE_SELECTOR = 'div[data-ad-preview="message"], div[data-ad-comet-preview="message"]'
_GALLERY_IMG_SELECTOR = 'img[src*="/t39.30808-6/"]'
_GROUP_PRICE_LINE_RE = re.compile(r"฿|thb|baht|бат", re.IGNORECASE)

SCROLL_PAUSE_SEC: float = 1.5
INITIAL_LOAD_TIMEOUT_MS: int = 30_000
INITIAL_ARTICLES_TIMEOUT_MS: int = 20_000
NO_PROGRESS_LIMIT: int = 4
# Pinned/announcement posts sit above the chronological order even under
# CHRONOLOGICAL sorting, so one over-age post is not enough to stop on.
STALE_STREAK_LIMIT: int = 3


def _find_own_article(page: "Page", post_id: str) -> "ElementHandle | None":
    """The top-level article that is *this* post, not a related one FB added.

    A permalink page can render a second, unrelated post above the one
    named in the URL (observed live: a cross-group recommendation card).
    Both are ``div[role="article"]``. This one's own self-link carries
    ``multi_permalinks=<post_id>`` in an anchor href — verified across two
    groups. Falls back to the article with the longest ad-body text when
    that signal is absent, since a churned DOM must still return something
    rather than silently reading the wrong post.
    """
    articles = page.query_selector_all(ARTICLE_SELECTOR)
    marker = f"multi_permalinks={post_id}"
    best_fallback: "ElementHandle | None" = None
    best_fallback_len = -1
    for article in articles:
        try:
            hrefs = article.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:  # noqa: BLE001 — DOM may be transient
            hrefs = []
        if any(href and marker in href for href in hrefs):
            return article
        try:
            messages = article.query_selector_all(_MESSAGE_SELECTOR)
            text_len = sum(len(m.inner_text() or "") for m in messages)
        except Exception:  # noqa: BLE001 — DOM may be transient
            text_len = 0
        if text_len > best_fallback_len:
            best_fallback_len = text_len
            best_fallback = article
    return best_fallback if best_fallback_len > 0 else None


def _try_expand_description(article: "ElementHandle") -> None:
    for selector in (
        'div[role="button"]:has-text("Ещё")',
        'div[role="button"]:has-text("See more")',
        'div[role="button"]:has-text("See More")',
    ):
        try:
            button = article.query_selector(selector)
            if button is None:
                continue
            button.click(timeout=SEE_MORE_TIMEOUT_MS)
            return
        except Exception:  # noqa: BLE001 — button may be stale or non-clickable
            continue


def _extract_body(article: "ElementHandle") -> str | None:
    """The post's own text, scoped to its article — never a comment's."""
    try:
        messages = article.query_selector_all(_MESSAGE_SELECTOR)
    except Exception:  # noqa: BLE001 — DOM may be transient
        messages = []
    for message in messages:
        try:
            text = (message.inner_text() or "").strip()
        except Exception:  # noqa: BLE001 — element may be stale
            continue
        if text:
            return text

    try:
        handles = article.query_selector_all('div[dir="auto"]')
    except Exception:  # noqa: BLE001 — DOM may be transient
        handles = []
    candidates: list[str] = []
    for handle in handles:
        try:
            text = (handle.inner_text() or "").strip()
        except Exception:  # noqa: BLE001 — element may be stale
            continue
        if len(text) >= 80:
            candidates.append(text)
    return max(candidates, key=len) if candidates else None


def extract_group_gallery_urls(article: "ElementHandle") -> list[str]:
    """Gallery images for one post, scoped to its own article.

    Group-post ``img`` tags carry an empty ``alt`` (measured live), so
    marketplace's alt-grouping filter does not apply — it would group
    everything into one bucket keyed by ``""``. The article boundary from
    :func:`_find_own_article` already excludes a neighbouring post's
    images, so no further filtering is needed here: every CDN image inside
    the right article is this post's own.
    """
    try:
        handles = article.query_selector_all(_GALLERY_IMG_SELECTOR)
    except Exception:  # noqa: BLE001 — DOM may be transient
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for handle in handles:
        try:
            src = handle.get_attribute("src")
        except Exception:  # noqa: BLE001 — element may be stale
            continue
        if not src or src in seen:
            continue
        seen.add(src)
        urls.append(src)
    return urls


def _extract_price_raw(lines: list[str]) -> str | None:
    for line in lines:
        if _GROUP_PRICE_LINE_RE.search(line):
            return line
    return None


def _extract_published_at(lines: list[str]) -> str | None:
    """First line carrying a timestamp — the post header, before any body text.

    Uses :func:`parse_feed_timestamp` rather than ``parse_published_at``
    directly: a post's own header shows the same compact form the feed
    does ("6 ч.", no "назад"), not the relative "N days ago" wording a
    marketplace detail page shows.
    """
    for line in lines:
        published = parse_feed_timestamp(line)
        if published:
            return published
    return None


def scrape_group_post(
    context: "BrowserContext",
    url: str,
    group_id: str,
    post_id: str,
) -> dict[str, Any] | None:
    """Fetch and parse one group post by its permalink.

    Mirrors :func:`scraper_detail.scrape_detail`'s contract: same return
    shape (``url``, ``title``, ``description``, ``published_at``,
    ``price_raw``, ``raw_json``), ``None`` when the post is gone, and
    :class:`LoginRequiredError` on a session bounce.
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
                    "Facebook redirected to login. Run `rent-assistant login` and retry."
                ) from exc
            return None

        if _detect_login_redirect(page):
            raise LoginRequiredError(
                "Facebook redirected to login. Run `rent-assistant login` and retry."
            )

        try:
            page.wait_for_selector(ARTICLE_SELECTOR, timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `rent-assistant login` and retry."
                ) from exc
            return None

        # Deliberately not `_detect_marked_unavailable`: that badge test
        # reads any "rented"/"sold" text not wrapped in a marketplace-item
        # link, and on a group post a comment saying "already rented?"
        # would false-positive. The removal wall text is locale-generic
        # and unambiguous either way.
        if _detect_unavailable(page):
            return None

        article = _find_own_article(page, post_id)
        if article is None:
            return None

        _try_expand_description(article)

        try:
            article_text = article.inner_text() or ""
        except Exception:  # noqa: BLE001 — element may be stale
            article_text = ""
        lines = [line.strip() for line in article_text.splitlines() if line.strip()]

        description = _extract_body(article)
        published_at = _extract_published_at(lines)
        price_raw = _extract_price_raw(lines)
        gallery_urls = extract_group_gallery_urls(article)

        raw_page_title = page.title() or None
        first_line = description.splitlines()[0] if description else None
        title = group_post_title(raw_page_title, first_line)

        raw_json: dict[str, Any] = {
            "url": url,
            "final_url": page.url,
            "title": raw_page_title,
            "text_lines": lines[:200],
            "gallery_urls": gallery_urls,
            "group": group_id,
        }

        return {
            "url": url,
            "title": title,
            "description": description,
            "published_at": published_at,
            "price_raw": price_raw,
            "raw_json": raw_json,
        }
    finally:
        page.close()


def _harvest_post_ids(page: "Page", group_id: str) -> dict[str, str]:
    """``{post_id: permalink}`` for every post link currently in the DOM."""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
    except Exception:  # noqa: BLE001 — DOM may be transient
        return {}
    found: dict[str, str] = {}
    for href in hrefs:
        if not href:
            continue
        match = GROUP_POST_RE.search(href)
        if match:
            found[match.group(2)] = href
    return found


def scrape_group_feed(
    context: "BrowserContext",
    group_id: str,
    *,
    max_items: int,
    days: int | None = None,
    allow_empty: bool = True,
) -> list[dict[str, Any]]:
    """Scroll a group's chronological feed, collecting card stubs.

    Unlike the marketplace feed, this one is virtualized — a post scrolled
    past leaves the DOM entirely, so ids are accumulated across every
    scroll step rather than re-read once at the end (measured live: a
    single read after six scrolls found fewer ids than the running total).
    Content is not read here at all: the returned cards carry only
    ``fb_id``, ``url`` and ``source`` — :func:`scrape_group_post` reads the
    permalink page for everything else, the same division marketplace
    keeps between its list cards and its detail pages.

    ``days``, when given, stops scrolling once ``STALE_STREAK_LIMIT``
    posts in a row read older than the window — a pinned post can still
    sit above that boundary, which is why one stale post does not stop it.

    ``allow_empty`` mirrors the marketplace sweep: one empty group says
    nothing about the session on its own.

    Raises
    ------
    LoginRequiredError
        If FB redirects to the login/checkpoint page or the feed renders
        no posts at all (both symptoms of an expired session).
    """
    if max_items <= 0:
        return []

    page = context.new_page()
    try:
        page.goto(
            group_feed_url(group_id),
            wait_until="domcontentloaded",
            timeout=INITIAL_LOAD_TIMEOUT_MS,
        )

        if _detect_login_redirect(page):
            raise LoginRequiredError(
                "Facebook redirected to login. Run `rent-assistant login` and retry."
            )

        try:
            page.wait_for_selector(
                ARTICLE_SELECTOR, timeout=INITIAL_ARTICLES_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `rent-assistant login` and retry."
                ) from exc
            if allow_empty:
                return []
            raise LoginRequiredError(
                "No group posts rendered — session likely expired or the "
                "group layout changed. Run `rent-assistant login` and retry."
            ) from exc

        seen: dict[str, str] = {}
        no_progress = 0
        scroll_attempts = 0
        stale_streak = 0
        hard_cap = max(max_items * 3, 6)

        while len(seen) < max_items and scroll_attempts < hard_cap:
            harvested = _harvest_post_ids(page, group_id)
            before = len(seen)
            for post_id, href in harvested.items():
                if len(seen) >= max_items:
                    break
                if post_id not in seen:
                    seen[post_id] = href

            if days is not None:
                for article in page.query_selector_all(ARTICLE_SELECTOR):
                    try:
                        header_text = article.inner_text()[:200]
                    except Exception:  # noqa: BLE001 — DOM may be transient
                        continue
                    stamp = parse_feed_timestamp(header_text)
                    if stamp is None:
                        continue
                    parsed = datetime.strptime(
                        stamp, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    age_limit = datetime.now(timezone.utc) - timedelta(days=days)
                    if parsed < age_limit:
                        stale_streak += 1
                        if stale_streak >= STALE_STREAK_LIMIT:
                            break
                    else:
                        stale_streak = 0
                if stale_streak >= STALE_STREAK_LIMIT:
                    break

            if len(seen) >= max_items:
                break

            if len(seen) == before:
                no_progress += 1
                if no_progress >= NO_PROGRESS_LIMIT:
                    break
            else:
                no_progress = 0

            page.mouse.wheel(0, 2500)
            time.sleep(SCROLL_PAUSE_SEC)
            scroll_attempts += 1

        if not seen:
            if allow_empty:
                return []
            raise LoginRequiredError(
                "Group feed returned zero posts — likely session expired. "
                "Run `rent-assistant login` and retry."
            )

        return [
            {
                "fb_id": post_id,
                "url": group_post_url(group_id, post_id),
                "title": None,
                "source": f"group:{group_id}",
            }
            for post_id in seen
        ]
    finally:
        page.close()
