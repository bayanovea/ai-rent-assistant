"""Playwright layer for Messenger automation — the Marketplace folder only.

Mirrors ``scraper_detail.py``'s shape: one page per call, best-effort DOM
reads that degrade to empty/``None`` rather than raising, and rate limiting
left entirely to the orchestrator (``cli.py``). See ``docs/messages.md`` for
the live measurements every selector here is based on — re-verify there
before touching a selector, this account renders a Russian-locale UI and the
English strings below are anticipated, not measured.

Nothing here sends a message except :func:`send_first_message` and
:func:`send_in_thread`, and neither is called by anything except the
``messages send`` CLI command, behind an explicit ``--apply`` confirmation.
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING, Any

from .browser import LoginRequiredError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


class MessengerPinRequiredError(RuntimeError):
    """Facebook is asking for the Messenger end-to-end-encryption PIN.

    Measured on this account (see docs/messages.md): the very first
    ``/messages/`` visit on a fresh browser profile shows "Введите PIN-код,
    чтобы восстановить чаты", and dismissing it without the PIN opens a
    second dialog whose "Не восстанавливать сообщения" button permanently
    drops this device's access to the encrypted history. Never click
    through this — surface it and let the user enter the PIN in a headed
    session (``launch_persistent(pw, headless=False)``).
    """


class MessagingLimitReachedError(RuntimeError):
    """Facebook's own daily cap on new-conversation messages.

    Measured live 2026-09-22: after roughly ten first-contact sends in one
    session, the compose dialog's send button opens a second dialog —
    "You've reached the messaging limit ... You can continue tomorrow." —
    instead of delivering the message. This is server-side and has nothing
    to do with our jitter or selectors; retrying the remaining drafts only
    burns a full timeout on each one for nothing. The exact daily count
    this account gets is unmeasured beyond "at least 10, capped well before
    27" — do not hardcode a number anywhere as if it were known.
    """


def _timeout_ms(name: str, default: int) -> int:
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

MESSAGES_HOME_URL = "https://www.facebook.com/messages/t/"

# The folder chip has no stable selector — no href, no role, no aria-label
# of its own (measured 2026-09-22, see docs/messages.md). Exact text match
# is what worked; "Marketplace" was not translated in the Russian UI. The
# same text also appears as a category label inside an already-open
# conversation's item card, so the match must be scoped to the thread-list
# panel — an unscoped page-wide match landed on that label instead on one
# run and opened a random thread rather than the folder.
_MARKETPLACE_FOLDER_TEXT = "Marketplace"
_THREAD_LIST_ARIA = "Перечень переписок"

# Only a genuine Marketplace-folder thread row carries this aria-label
# prefix; stale rows left over from whatever folder was open before match
# the href selector too but come back with aria=None.
_CHAT_ARIA_PREFIX = "Групповой чат: "
_CHAT_ROW_SELECTOR = f'[aria-label^="{_CHAT_ARIA_PREFIX}"]'

# The thread list is virtualized like the marketplace feed
# (scraper_list.py): only the rows near the viewport are in the DOM, and
# scrolling renders more. Measured 2026-09-22: the folder opened with 4
# rows rendered against 24 real marketplace threads in the account.
SCROLL_PAUSE_SEC: float = 1.5
NO_PROGRESS_LIMIT: int = 3

_THREAD_HREF_RE = re.compile(r"/messages/t/(\d+)/?")
_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)/?")

# "В 09:55, от Seller: May I have a picture of your dog, please?" —
# text is absent (no trailing ": ...") for an attachment-only message, e.g.
# "В 08:18, от Seller".
_MSG_ARIA_RE = re.compile(
    r"^В (?P<time>\d{1,2}:\d{2}), от (?P<sender>[^:]+?)(?::\s(?P<text>.*))?$"
)
_SELF_SENDER_NAMES = {"Вы", "You"}
_CHAT_CREATED_TEXT = "Вы создали этот чат."

# Meta's automated first-message safety notice, not something the seller
# wrote — measured 2026-09-22, appears verbatim as the first inbound
# message on threads Marketplace flags for its fraud check.
_META_SAFETY_NOTICE = (
    "Meta может использовать технологии, чтобы проверять сообщения в "
    "Marketplace. Это помогает выявлять и сокращать случаи мошенничества "
    "и обмана."
)
_SYSTEM_TEXTS = {_CHAT_CREATED_TEXT, _META_SAFETY_NOTICE}

# The composer's aria-label is "Написать в <peer_name> · <listing_title>" —
# always present once a thread has rendered, unlike the header, so it is
# the peer-name source used here.
_COMPOSER_ARIA_RE = re.compile(r"^Написать в (?P<rest>.+)$")

_PIN_DIALOG_MARKERS = ("Введите PIN-код", "Enter your PIN")

# Measured live 2026-09-22, English regardless of the account's Russian UI
# locale — the daily first-contact cap (see MessagingLimitReachedError).
_MESSAGING_LIMIT_MARKER = "reached the messaging limit"


def _detect_login_redirect(page: "Page") -> bool:
    if "/login" in page.url or "checkpoint" in page.url:
        return True
    try:
        return page.locator('input[name="email"]').first.count() > 0
    except Exception:  # noqa: BLE001 — page may not be ready
        return False


def _detect_pin_dialog(page: "Page") -> bool:
    try:
        for marker in _PIN_DIALOG_MARKERS:
            if page.get_by_text(marker, exact=False).count() > 0:
                return True
    except Exception:  # noqa: BLE001 — page may not be ready
        return False
    return False


def _guard_page(page: "Page") -> None:
    """Raise if this page is not a normal, authenticated Messenger view."""
    if _detect_login_redirect(page):
        raise LoginRequiredError(
            "Facebook redirected to login. Run `fb-rent login` and retry."
        )
    if _detect_pin_dialog(page):
        raise MessengerPinRequiredError(
            "Facebook is asking for the Messenger end-to-end-encryption "
            "PIN. Run a headed session and enter it by hand once — see "
            "docs/messages.md."
        )


def _open_marketplace_folder(page: "Page") -> None:
    # The chip is rendered by an async inbox-summary fetch, not present at
    # domcontentloaded — clicking too early can hit a 0-match locator that
    # times out, or (worse) match a same-text "Marketplace" category label
    # inside whatever conversation happens to already be open. Waiting for
    # the chip itself is the fix for both.
    try:
        scope = page.locator(f'[aria-label="{_THREAD_LIST_ARIA}"]')
        if scope.count() == 0:
            # Fallback for an unmeasured locale where the panel's own
            # aria-label differs — best-effort, page-wide.
            scope = page
        chip = scope.get_by_text(_MARKETPLACE_FOLDER_TEXT, exact=True).first
        chip.wait_for(state="visible", timeout=10_000)
        chip.click(timeout=8_000)
    except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError et al.
        raise RuntimeError(
            "Could not open the Messenger Marketplace folder — the chip "
            "selector may have changed, see docs/messages.md"
        ) from exc

    try:
        page.wait_for_selector(_CHAT_ROW_SELECTOR, timeout=10_000)
    except Exception:  # noqa: BLE001 — Playwright's TimeoutError
        # A genuinely empty Marketplace folder looks the same as a failed
        # click from here; the caller's thread count is what surfaces the
        # difference to the user.
        pass


def list_marketplace_threads(
    context: "BrowserContext", *, max_threads: int = 100
) -> list[dict[str, Any]]:
    """List threads in the Marketplace folder, without opening any of them.

    Returns ``[{"thread_id", "peer_name", "last_activity_raw"}, ...]`` in
    DOM order. ``last_activity_raw`` is Facebook's own relative-age text
    ("14 мин.", "2 г.") — parse it with ``messages.parse_thread_age``.

    Raises
    ------
    LoginRequiredError
        If Facebook redirects to login.
    MessengerPinRequiredError
        If Facebook is asking for the E2E-encryption PIN — never dismiss
        this automatically, see the class docstring.
    """
    page = context.new_page()
    try:
        page.goto(
            MESSAGES_HOME_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
        )
        _guard_page(page)
        _open_marketplace_folder(page)
        _guard_page(page)

        # The row list is virtualized (see _CHAT_ROW_SELECTOR's comment) —
        # scroll the thread list until no new rows render, same shape as
        # scraper_list.scrape_list's feed scroll.
        no_progress = 0
        scroll_attempts = 0
        hard_cap = max(max_threads * 2, 8)
        seen_count = 0
        while scroll_attempts < hard_cap:
            try:
                seen_count = page.locator(_CHAT_ROW_SELECTOR).count()
            except Exception:  # noqa: BLE001 — DOM may be transient
                break
            if seen_count >= max_threads:
                break
            page.mouse.wheel(0, 2000)
            time.sleep(SCROLL_PAUSE_SEC)
            scroll_attempts += 1
            try:
                new_count = page.locator(_CHAT_ROW_SELECTOR).count()
            except Exception:  # noqa: BLE001 — DOM may be transient
                break
            if new_count <= seen_count:
                no_progress += 1
                if no_progress >= NO_PROGRESS_LIMIT:
                    break
            else:
                no_progress = 0

        try:
            raw = page.eval_on_selector_all(
                'a[href*="/messages/t/"]',
                "els => els.map(e => ({href: e.getAttribute('href'), "
                "aria: e.getAttribute('aria-label'), text: e.innerText}))",
            )
        except Exception:  # noqa: BLE001 — DOM may be transient
            return []

        threads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            aria = item.get("aria")
            if not aria or not aria.startswith(_CHAT_ARIA_PREFIX):
                continue
            match = _THREAD_HREF_RE.search(item.get("href") or "")
            if not match:
                continue
            thread_id = match.group(1)
            if thread_id in seen:
                continue
            seen.add(thread_id)

            peer_name = aria[len(_CHAT_ARIA_PREFIX) :].split(" · ", 1)[0].strip() or None
            text_lines = [
                line for line in (item.get("text") or "").split("\n") if line.strip()
            ]
            last_activity_raw = text_lines[-1] if text_lines else None

            threads.append(
                {
                    "thread_id": thread_id,
                    "peer_name": peer_name,
                    "last_activity_raw": last_activity_raw,
                }
            )
            if len(threads) >= max_threads:
                break
        return threads
    finally:
        page.close()


def _parse_message_aria(aria: str) -> dict[str, Any] | None:
    match = _MSG_ARIA_RE.match(aria)
    if not match:
        return None
    sender = match.group("sender").strip()
    text = (match.group("text") or "").strip()
    if text in _SYSTEM_TEXTS:
        return None
    direction = "out" if sender in _SELF_SENDER_NAMES else "in"
    return {"direction": direction, "text": text, "sent_at": None}


_SCROLL_UP_PAUSE_SEC = 1.0
_SCROLL_UP_NO_PROGRESS_LIMIT = 3
_SCROLL_UP_MAX_ATTEMPTS = 20


def _scroll_thread_history(page: "Page") -> None:
    """Scroll the message pane up until no older messages load.

    Measured 2026-09-22: the message list virtualizes like the thread list
    (see ``_CHAT_ROW_SELECTOR``'s comment) — a thread with ~13 real
    messages rendered only the newest 4 without this, which would have
    made a thread look unanswered when it was not.
    """
    try:
        container = page.evaluate_handle(
            """
            () => {
                const first = document.querySelector('div[data-scope="messages_table"]');
                if (!first) return null;
                let el = first;
                while (el && el !== document.body) {
                    if (el.scrollHeight > el.clientHeight + 10) return el;
                    el = el.parentElement;
                }
                return null;
            }
            """
        )
    except Exception:  # noqa: BLE001 — DOM may be transient
        return
    if container is None:
        return

    no_progress = 0
    prev_count = -1
    for _ in range(_SCROLL_UP_MAX_ATTEMPTS):
        try:
            count = page.locator(
                'div[data-scope="messages_table"]'
            ).count()
        except Exception:  # noqa: BLE001 — DOM may be transient
            break
        if count == prev_count:
            no_progress += 1
            if no_progress >= _SCROLL_UP_NO_PROGRESS_LIMIT:
                break
        else:
            no_progress = 0
        prev_count = count
        try:
            container.evaluate("el => { el.scrollTop = 0; }")
        except Exception:  # noqa: BLE001 — element may be stale
            break
        time.sleep(_SCROLL_UP_PAUSE_SEC)


def _peer_name_from_composer(page: "Page") -> str | None:
    try:
        aria = page.locator('div[role="textbox"][contenteditable="true"]').first.get_attribute(
            "aria-label"
        )
    except Exception:  # noqa: BLE001 — composer may not be present
        return None
    if not aria:
        return None
    match = _COMPOSER_ARIA_RE.match(aria)
    if not match:
        return None
    return match.group("rest").split(" · ", 1)[0].strip() or None


def read_thread(context: "BrowserContext", thread_id: str) -> dict[str, Any] | None:
    """Read one thread's item-card listing id, peer name and message list.

    Returns ``None`` if the thread page never rendered anything readable —
    degraded like a churned DOM elsewhere in this project, not raised.
    ``messages`` is in DOM (= chronological) order; a message with no text
    body (an image, an attachment) comes back with ``text=""``.

    Raises the same two exceptions as :func:`list_marketplace_threads`.
    """
    page = context.new_page()
    try:
        url = f"https://www.facebook.com/messages/t/{thread_id}/"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            if _detect_login_redirect(page):
                raise LoginRequiredError(
                    "Facebook redirected to login. Run `fb-rent login` and retry."
                ) from exc
            return None

        _guard_page(page)

        try:
            page.wait_for_selector(
                'div[data-scope="messages_table"]', timeout=CONTENT_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001 — Playwright's TimeoutError
            pass  # an empty or unreadable thread degrades below, not here
        else:
            _scroll_thread_history(page)

        try:
            item_hrefs = page.eval_on_selector_all(
                'a[href*="/marketplace/item/"]',
                "els => els.map(e => e.getAttribute('href'))",
            )
        except Exception:  # noqa: BLE001 — DOM may be transient
            item_hrefs = []
        listing_fb_id: str | None = None
        for href in item_hrefs:
            match = _ITEM_ID_RE.search(href or "")
            if match:
                listing_fb_id = match.group(1)
                break

        peer_name = _peer_name_from_composer(page)

        try:
            raw_arias = page.eval_on_selector_all(
                'div[data-scope="messages_table"]',
                "els => els.map(e => e.getAttribute('aria-label'))",
            )
        except Exception:  # noqa: BLE001 — DOM may be transient
            raw_arias = []

        messages: list[dict[str, Any]] = []
        for aria in raw_arias:
            if not aria:
                continue
            parsed = _parse_message_aria(aria)
            if parsed:
                messages.append(parsed)

        return {
            "thread_id": thread_id,
            "peer_name": peer_name,
            "listing_fb_id": listing_fb_id,
            "messages": messages,
        }
    finally:
        page.close()


def send_in_thread(context: "BrowserContext", thread_id: str, text: str) -> bool:
    """Type ``text`` into an existing thread's composer and press Enter.

    Written for iteration 2 (viewing-time follow-ups); not called by
    anything in iteration 1. Returns ``True`` only if the sent text is then
    found among the thread's own outbound messages — an unverified send is
    treated as a failure, never assumed.
    """
    page = context.new_page()
    try:
        url = f"https://www.facebook.com/messages/t/{thread_id}/"
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        _guard_page(page)

        box = page.locator('div[role="textbox"][contenteditable="true"]').first
        box.click(timeout=CONTENT_TIMEOUT_MS)
        box.type(text, delay=20)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2_000)

        try:
            raw_arias = page.eval_on_selector_all(
                'div[data-scope="messages_table"]',
                "els => els.map(e => e.getAttribute('aria-label'))",
            )
        except Exception:  # noqa: BLE001 — DOM may be transient
            return False
        for aria in raw_arias:
            parsed = _parse_message_aria(aria or "")
            if parsed and parsed["direction"] == "out" and parsed["text"] == text:
                return True
        return False
    finally:
        page.close()


_SEND_BUTTON_TEXT = "Отправить сообщение"


def send_first_message(context: "BrowserContext", listing_url: str, text: str) -> bool:
    """Open a listing's detail page and send the first message to its seller.

    Measured live 2026-09-22 (see docs/messages.md): the detail page's
    "Отправить сообщение" button opens a **modal dialog**
    (`div[role="dialog"]`) with its own plain `<textarea>` — not the
    thread page's `contenteditable` composer `send_in_thread` uses. The
    dialog's own send button starts `aria-disabled="true"` and only
    enables once the textarea has text.
    """
    page = context.new_page()
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        _guard_page(page)

        try:
            open_button = page.get_by_text(_SEND_BUTTON_TEXT, exact=False).first
            if open_button.count() == 0:
                open_button = page.get_by_text("Send message", exact=False).first
            open_button.click(timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError et al.
            raise RuntimeError(
                f"Could not find/click the message button on {listing_url}"
            ) from exc

        # `div[role="dialog"]` alone is ambiguous: a persistent "Messenger"
        # chat-widget dialog can already be in the DOM from earlier in the
        # session, alongside a transient "Загрузка…" (loading) dialog —
        # measured live 2026-09-22, a bare locator hit Playwright's strict
        # mode with 2-3 matches. The compose dialog is the one with the
        # textarea in it.
        dialog = page.locator('div[role="dialog"]:has(textarea)')
        try:
            dialog.wait_for(state="visible", timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError
            raise RuntimeError(
                f"First-contact dialog did not open on {listing_url}"
            ) from exc
        _guard_page(page)

        box = dialog.locator("textarea").first
        box.click(timeout=CONTENT_TIMEOUT_MS)
        box.fill("")  # drop any prefilled suggestion ("Ещё продаётся?")
        box.type(text, delay=20)

        try:
            send_button = dialog.locator(
                f'div[aria-label="{_SEND_BUTTON_TEXT}"][role="button"]'
            ).first
            send_button.click(timeout=CONTENT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — Playwright's TimeoutError et al.
            raise RuntimeError(
                f"Could not click the dialog's send button on {listing_url}"
            ) from exc

        # The dialog closes on a successful send; a rejection (daily limit,
        # blocked seller) opens a *second* dialog on top instead, leaving
        # the compose one and its typed text right where they were.
        try:
            dialog.wait_for(state="hidden", timeout=CONTENT_TIMEOUT_MS)
            return True
        except Exception:  # noqa: BLE001 — Playwright's TimeoutError
            if page.get_by_text(_MESSAGING_LIMIT_MARKER, exact=False).count() > 0:
                raise MessagingLimitReachedError(
                    "Facebook: daily messaging limit reached — "
                    '"You can continue tomorrow."'
                )
            return False
    finally:
        page.close()
