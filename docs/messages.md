# Messenger automation — how it works and what we learned

Read this first when you come back to the Messenger pass. It carries what the
code cannot: measurements on the live account, the decisions worth not
undoing, and the open gaps. See [pipeline.md](pipeline.md) for where this
pass sits in the flow and `CLAUDE.md` for the invariants a change must not
break.

Last checked against the live account: **22 September 2026**.

## What the pass is for

Iteration 1: sync existing Marketplace conversations into the DB, send a
first-contact message (viewing + pets) to a user-picked selection of
listings, and surface replies that need the user's attention. It does not
negotiate viewing times — that is iteration 2.

## The E2E-encryption PIN dialog — the first thing every fresh profile hits

The very first time the persistent browser profile (`BROWSER_PROFILE_DIR`,
shared with scraping) opens *any* `/messages/` URL, Facebook shows a modal:
"Введите PIN-код, чтобы восстановить чаты" (secret-conversation history
recovery). Closing it without the PIN surfaces a second, harsher dialog —
"Продолжить без восстановления?" — whose "Не восстанавливать сообщения"
button **permanently drops this device's access to the encrypted chat
history**. Pressing `Escape` on the first dialog opens the second one; it
does not dismiss anything.

This is not a one-off — a **headless** `launch_persistent_context` against
the same `user_data_dir` re-triggered the PIN prompt every run until the PIN
was entered once in a **headed** run.  After one headed run where the user
typed the PIN into the visible window, subsequent headless runs against the
same profile no longer show the dialog. **Never click through this dialog
programmatically; never guess an answer.** If a fresh profile (or a wiped
one) hits it again, stop and have the user enter the PIN in a headed
session — `launch_persistent(pw, headless=False)`, navigate to
`https://www.facebook.com/messages/t/`, wait for the human.

## The Marketplace folder

`https://www.facebook.com/messages/t/?folder=marketplace` does **not** load
the Marketplace folder — Messenger ignores the query param and just opens
whatever thread was last open (`page.url` after `goto` stays on
`/messages/t/<last-thread-id>/`). The folder only opens by **clicking the
"Marketplace" chip** in the left rail (the one showing "N новых сообщений ·
М мин." under "Чаты"). No stable `href`/`role`/`aria-label` was found on
that chip in the HTML; it is a bare nested `<div>` React click target. The
one selector that worked reliably: `page.get_by_text("Marketplace",
exact=True).first.click(force=True)` — `force=True` because the PIN dialog
(when present) intercepts pointer events on everything, so the dialog must
be dismissed first. "Marketplace" is not translated in the Russian-locale
UI, so the exact-text match should hold across the account's locale, but
this has only been measured on one account — re-verify if it ever silently
returns 0 threads.

After the click, `page.eval_on_selector_all('a[href*="/messages/t/"]', ...)`
picks up **both** the Marketplace threads and stale entries from whatever
folder was open before (same href, empty `innerText`, no `aria-label`) —
filter on **`aria-label` starting with `"Групповой чат: "`** (or `"Chat: "`
in English?), not on the href pattern alone, or the old folder's dead rows
come along.

## Thread list row anatomy (Marketplace folder, one row per thread)

```
aria-label = "Групповой чат: <peer_name> · <listing_title>"
innerText  = "<peer_name> · <listing_title>\n<last message preview>\n\xa0\n·\n<age>\xa0<unit>"
href       = "/messages/t/<thread_id>/"
```

`<age> <unit>` is a **relative, minute-granular** timestamp in this account's
Russian locale: `14 мин.`, `46 мин.`, `1 ч.`, `4 ч.`, `14 ч.`, `1 г.`
(≈ minutes/hours/years; days as `N д.` presumably, not observed yet). This is
precise enough to apply the 2026-09-20 cutoff without ambiguity — the
"skip if the age can't be dated" fallback in the plan turned out not to be
needed, at least on this account. **Still parse defensively** (English
"3d"/"2w" forms exist on other locales/rollouts) and treat an unparseable
age as "skip, don't open" per the plan's conservative rule — just note the
fallback path is currently unexercised.

`<peer_name>` is a stripped-down name (no href to the seller profile in the
row itself). `<listing_title>` is free text and is **not** the `fb_id` — it
must not be used for matching, only the item-card link inside the opened
thread gives the id (see below).

The same "Marketplace" folder mixes **buyer-side threads** (us asking
landlords about a rental — what this feature cares about) and **seller-side
threads** (people asking about goods the user has for sale on Marketplace,
e.g. furniture/TV listings) — 24 of the 37 rows measured were month/year-old
furniture-sale threads, not rentals. Nothing in the row text tells them
apart before opening the item card; the listing title is the only signal
("2 ห้องนอน" / "Beds" / "House" vs "TV" / "cabinet" / "Yoga Mat"), too weak to
trust. **Do not filter on title text** — resolve every thread's `fb_id`
against `listings`/`duplicates` first (Phase 1's `read_thread` + sync flow
already does this) and let "no match in our DB" naturally drop the
furniture threads into `needs_action=1, reason=no_listing_match` — or,
better, simply don't upsert threads whose item card `fb_id` isn't a known
listing at all, since those are not ours to act on.

## Thread page anatomy (`/messages/t/<thread_id>/`)

**Item card** (top of the middle column): the listing link appears **twice**
(banner strip + "See details"/"Rate Seller" button), same href:

```
a[href*="/marketplace/item/"] → https://www.facebook.com/marketplace/item/<fb_id>/?referralSurface=...
```

Take the first match, strip query params, extract `fb_id` via
`/marketplace/item/(\d+)/`.

**Messages**: each message is a `div[role="article"]` /
`[data-scope="messages_table"]` node with:

```
aria-label = "В <HH:MM>, от <sender>: <text>"          — sender == "Вы" for our own messages
data-message-id = "mid.$<opaque>"
aria-roledescription = "сообщение"
```

Direction is unambiguous: `от Вы:` = outbound, anything else = inbound
(peer's display name matches the thread's `peer_name`). A message with no
text body (e.g. an image) renders the aria-label **without** the trailing
`: <text>` — `"В 08:18, от Seller"` — handle that as `text=""`, not a
parse failure. The very first system row in a thread we started —
`"В <time>, от Вы: Вы создали этот чат."` ("You created this chat.") — is a
reliable "this is a thread we opened, not one the seller opened toward us"
marker; skip it when extracting message text.

**Composer** (bottom of the thread): a `contenteditable` box, not a plain
`<input>`:

```
div[role="textbox"][contenteditable="true"], aria-label="Написать в <peer> · <listing_title>"
```

No dedicated "send" button was found in the static DOM for typed text (only
`"Отправить голосовое сообщение"` — voice message — and a like/thumbs-up
button that occupies the send slot while the composer is empty). Messenger's
ordinary behavior is **type into the box, press `Enter` to send**
(`Shift+Enter` for a newline) — this was inferred from the DOM, not yet
exercised live; confirm during Phase 2's first supervised send
(`messages send --ids <one listing> --cap 1 --apply`) and update this note
with what actually happened, including how `send_first_message` verified
the message landed.

## First-contact composer (marketplace item detail page)

`https://www.facebook.com/marketplace/item/<fb_id>/` — for a listing that
**already has** an open thread, the page shows "Отправить сообщение" as a
button (top-right icon + a lower CTA), not an inline composer; no
`div[role="textbox"]` is present on the page itself (measured: 0 matches).
Clicking it should either open a popout composer on the same page or
navigate into `/messages/t/<new-or-existing-thread-id>/` — **not yet
confirmed live**, to avoid opening a compose box (and the "typing..."
indicator it can trigger for the seller) against a real listing outside a
supervised send. Resolve this in Phase 2's first live run: click the
button, capture where it lands, and if it's a popout rather than a
navigation, this note needs the popout's composer selector before
`send_first_message` can be written against it.

## Decisions locked from this recon

- **Cutoff dating works** off the Marketplace folder row's relative
  timestamp — no need for the "never open an undated thread" fallback in
  the common case, but keep it as the conservative default when a row's age
  string doesn't parse.
- **Folder identification is text-based** (`get_by_text("Marketplace",
  exact=True)`), not structural — there is no stable selector for the
  folder chip. Re-verify if the click ever silently yields the wrong
  (previous) folder's threads.
- **The E2E PIN dialog is a one-time, per-profile, human-only step.** Code
  must detect it (presence of the "Введите PIN-код" dialog / the
  "Закрыть"/"Не восстанавливать" buttons) and fail loudly asking the user to
  run a headed session — never dismiss it automatically.
- **Thread → listing matching goes through the item-card link inside the
  thread**, never through the folder row's title text, because the same
  folder mixes buyer- and seller-side Marketplace threads with no other
  distinguishing signal in the list view.
