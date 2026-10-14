# Messenger automation — how it works and what we learned

Read this first when you come back to the Messenger pass. It carries what the
code cannot: measurements on the live account, the decisions worth not
undoing, and the open gaps. See [pipeline.md](pipeline.md) for where this
pass sits in the flow and `CLAUDE.md` for the invariants a change must not
break.

Last checked against the live account: **22 September 2026** (Phase 0 recon
and Phase 1 implementation, same day — `messages.py`, `messenger.py`,
`storage.py` conversations tables, `rent-assistant messages sync`).

## What the pass is for

Iteration 1: sync existing Marketplace conversations into the DB, send a
first-contact message (viewing + pets) to a user-picked selection of
listings, and surface replies that need the user's attention. It does not
negotiate viewing times — that is iteration 2.

## The E2E-encryption PIN dialog — the first thing every fresh profile hits

The very first time the persistent browser profile (`BROWSER_PROFILE_DIR`,
shared with scraping) opens *any* `/messages/` URL, Facebook shows a modal:
"Введите PIN-код, чтобы восстановить чаты" ("Enter your PIN to restore
your chats" — secret-conversation history recovery). Closing it without the
PIN surfaces a second, harsher dialog — "Продолжить без восстановления?"
("Continue without restoring?") — whose "Не восстанавливать сообщения"
("Don't restore messages") button **permanently drops this device's access to the encrypted chat
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
М мин." — "N new messages · M min" — under "Чаты", "Chats"). No stable `href`/`role`/`aria-label` was found on
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
filter on **`aria-label` starting with `"Групповой чат: "`** ("Group chat: ";
or `"Chat: "` in English?), not on the href pattern alone, or the old folder's dead rows
come along.

## Thread list row anatomy (Marketplace folder, one row per thread)

```
aria-label = "Групповой чат: <peer_name> · <listing_title>"
innerText  = "<peer_name> · <listing_title>\n<last message preview>\n\xa0\n·\n<age>\xa0<unit>"
href       = "/messages/t/<thread_id>/"
```

`<age> <unit>` is a **relative, minute-granular** timestamp in this account's
Russian locale: `14 мин.`, `46 мин.`, `1 ч.`, `4 ч.`, `14 ч.`, `1 г.`
(`мин.` = minutes, `ч.` = hours, `г.` = years; days as `N д.` presumably,
not observed yet). This is
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
aria-label = "В <HH:MM>, от <sender>: <text>"          — "At <HH:MM>, from <sender>: <text>";
                                                        sender == "Вы" ("You") for our own messages
data-message-id = "mid.$<opaque>"
aria-roledescription = "сообщение"                    — "message"
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
div[role="textbox"][contenteditable="true"], aria-label="Написать в <peer> · <listing_title>"   — "Write to <peer> · <listing_title>"
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

**Confirmed live 2026-09-22** with the first real send (fb_id
`9000000000000001`, delivered and verified in the actual thread).
`https://www.facebook.com/marketplace/item/<fb_id>/` — clicking "Отправить
сообщение" ("Send message" — the top-right icon or the lower CTA) opens a **modal dialog**, not
the thread page's composer:

```
div[role="dialog"]                         — the modal itself
  textarea                                 — plain textarea, no aria-label/placeholder attribute
                                              (only one is inside the dialog; two more stray
                                              <textarea> elements exist elsewhere on the page —
                                              scope to the dialog or the wrong one gets typed into)
  div[aria-label="Отправить сообщение"][role="button"]
                                            — starts aria-disabled="true", the attribute is
                                              removed entirely once the textarea has text
```

No `Enter`-to-send here — this is a plain `<textarea>`, and the dialog's
own send button must be clicked. The dialog closes on a successful send;
that closing (`state="hidden"`) is what `send_first_message` polls for as
its success signal — confirmed correct on the live run, no separate
"message appeared" check was needed or possible from this page.

**Gap found on the live run: a freshly created thread carries no
`/marketplace/item/` link at all**, not even after a couple of minutes —
only "Вы создали этот чат." and a link to the seller's Marketplace
*profile* (`/marketplace/profile/<id>/`), no item link. `read_thread`
therefore returns `listing_fb_id: None` for a thread this fresh, and
`messages sync` files it under `no_listing_match` even though `messages
send` knows perfectly well which listing it was for (it composed the
message from that row). This does not lose anything load-bearing — the
`written` stage and the send-log entry are recorded straight from `messages
send` itself, keyed by the fb_id it already had, never by a re-derived
match — but the conversation's own row stays unmatched until a later
`messages sync` catches the item-card link once Facebook backfills it
(timing unmeasured — plausibly after the seller's first reply). Not fixed
in iteration 1; if it turns out Facebook never backfills the link,
`messages sync` needs a second matching path (e.g. the send log's
`fb_id` cross-referenced against a same-peer, no-listing thread created
right after a logged send).

## What Phase 1 implementation found that Phase 0 recon missed

Three things only surfaced once `messages sync` ran repeatedly against the
live account, all fixed in `messenger.py`:

**The folder click is racy against an async chip render.** The
"Marketplace" chip is filled in by an inbox-summary fetch that runs after
`domcontentloaded` — clicking immediately after `goto` sometimes hit a
0-match locator that still somehow resolved (Playwright's `.click()`
auto-waits and re-queries), landing on the wrong same-text element instead
(see below) or on nothing at all. The fix: scope the search to
`[aria-label="Перечень переписок"]` (the thread-list panel's own
aria-label — "list of conversations") *and* explicitly
`chip.wait_for(state="visible")` before clicking, rather than a blind
`wait_for_timeout`.

**Without that scope, "Marketplace" can resolve to the wrong element.**
The same literal text also labels the Marketplace category on the
item-card of whatever conversation happens to already be open (visible in
the very first screenshot: "Marketplace" appears above the price, inside
the open thread's right-hand panel). An unscoped
`page.get_by_text("Marketplace", exact=True).first` picked that up on one
run and the click just re-opened some unrelated thread instead of
switching folders — the DOM order between the chip and this label is not
stable.

**Both the thread list and the message list inside a thread are
virtualized.** Only a handful of thread rows render at a time — scrolling
(`page.mouse.wheel`) is needed to load the rest, same shape as
`scraper_list.py`'s marketplace-feed scroll. Worse: a single thread's
*message history* is also virtualized — a thread with 18 real messages
rendered only the newest 4 on load. `_scroll_thread_history` scrolls the
message pane's own scroll container (found by walking up from the first
message element to the first ancestor whose `scrollHeight` exceeds its
`clientHeight`) to the top repeatedly until the rendered count stops
growing. Skipping this silently truncates a thread to its tail and can
make an answered conversation look unanswered — exactly the kind of bug a
human reading the real Messenger UI would never make, because scrolling up
is instinctive there.

**One inbound message is Facebook's own text, not the seller's**:
"Meta может использовать технологии, чтобы проверять сообщения в
Marketplace. Это помогает выявлять и сокращать случаи мошенничества и
обмана." ("Meta may use technology to review messages in Marketplace. This
helps detect and reduce fraud and scams.") — an automated fraud-notice that appears verbatim as an inbound
message on threads Marketplace's safety system flags. `messenger.py`
filters it out by exact text match (`_META_SAFETY_NOTICE`) alongside the
"Вы создали этот чат." system row, so it never reaches `reply_hash` or a
classification batch as if the seller had written it.

## Facebook's own daily limit on first-contact sends

Measured live 2026-09-22, sending to the Longlist plus the last unwritten
Shortlist listing (27 targets): the first **10** first-contact sends went
through cleanly across two runs; every send attempted after that opened a
second dialog on top of the compose one — "You've reached the messaging
limit ... To help build a better community for buyers and sellers, there
is a temporary limit on how much you can message. You can continue
tomorrow." — in English, regardless of the account's Russian UI locale.
This is Facebook's own anti-spam throttle on the account, not a bug in the
jitter or the selectors, and not something to work around: `messenger.py`
detects the dialog's marker text and raises
`MessagingLimitReachedError`, which `messages send`'s loop treats like
`LoginRequiredError` — stop the whole batch immediately, report how many
got through, and let the user re-run the same selection once the cap
resets. Already-sent listings are skipped on the re-run (stage `written` +
the send log), so nothing doubles up.

**Do not hardcode "10" as the limit anywhere.** It is one measurement on
one account on one day, not a documented Facebook policy — the real number
could depend on account age, history, or simply vary. Treat the error as
the only reliable signal; do not pre-emptively cap `--cap` at 10 to "avoid"
it.

## The "Could not click the message button" failures weren't the daily limit

Debugged live 2026-09-22 after a batch send reported three items FAILED
with `Could not find/click the message button` while others in the same
run hit `MessagingLimitReachedError` cleanly — these were two unrelated
causes, easy to conflate since both surface as "the send didn't happen."

`document.elementFromPoint` at the button's actual on-screen coordinates
(after a proper `scroll_into_view_if_needed`, not the page's initial
scroll position) returned a `data-visualcompletion="ignore"` div — a
lazy-load placeholder layer measured at **2286px tall** on one listing, not
a fleeting fade-in. Playwright's ordinary `.click()` never clears its
actionability check against that, and `force=True` alone did not help
either: it skips the interception check but still drives Playwright's
simulated mouse-event pipeline, which this particular button's handler
did not respond to in testing — the dialog never opened even though
`force=True` reported success. What worked reliably: `element.click()`
run as a real DOM method inside the page (`locator.evaluate("el =>
el.click()")`) — a genuine method call, not a simulated pointer event,
so it reaches React's delegated listener regardless of what is stacked
visually on top. `messenger._click_resilient` tries the ordinary click
first and only falls back to this once that has had its own full
timeout — the fallback skips every safety check Playwright's own click
provides, so it is worth the ~15s of wasted waiting to prefer the safe
path when it works.

## Two accounts

Hitting the daily send limit above, the user asked to message from a
second Facebook account — that of a second person in the household, a
genuine second person also house-hunting, not a fake/duplicate account meant to look like more people
than there are. That distinction matters: Meta's terms prohibit exactly
the latter (multiple accounts operated by one person to evade
enforcement), not two real people each using their own login.

What got built: `config.browser_profile_dir(name)` resolves a named
account to its own persistent-context directory
(`$RENT_ASSISTANT_HOME/browser-profile-<name>`, e.g. `browser-profile-partner`),
completely separate cookies/localStorage from the default profile — `login`,
`messages sync` and `messages send` all take `--profile NAME`. That person runs
`rent-assistant login --profile partner` once, headed, to sign into their own account,
exactly like the primary `rent-assistant login` flow.

**What deliberately was not built: automatic failover between accounts on
a rate limit.** A tool that catches `MessagingLimitReachedError` on
account A and silently keeps going through account B's login would be
automated evasion of Facebook's anti-spam control, regardless of whether
both accounts are real — the automation is the part their policy
prohibits, not the account's authenticity. Which account messages which
listing stays a decision made through `--ids`/`--status` on the command
line every time, by the person running it — the same shape as the
existing draft-and-confirm flow, just now optionally pointed at a second
profile. If both accounts hit the limit in the same session, that is the
day's actual ceiling; the answer is to wait, not to add a third profile.

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
  "Закрыть" ("Close") / "Не восстанавливать" ("Don't restore") buttons) and fail loudly asking the user to
  run a headed session — never dismiss it automatically.
- **Thread → listing matching goes through the item-card link inside the
  thread**, never through the folder row's title text, because the same
  folder mixes buyer- and seller-side Marketplace threads with no other
  distinguishing signal in the list view.
