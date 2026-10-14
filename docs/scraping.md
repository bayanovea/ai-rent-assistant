# Scraping

What `rent-assistant scrape` does is unremarkable — build a search URL, walk the feed,
open detail pages, write rows. What is worth reading is why the flags are
shaped the way they are, because each one encodes something Facebook does to
you.

## The session

`rent-assistant login` opens a headed Chromium and waits for you to sign in by hand.
The cookies land in `$RENT_ASSISTANT_HOME/browser-profile/`, and every later run reuses them
headlessly. Facebook invalidates the session every 1–4 weeks, or after a
suspicious-activity check; the symptom is `LoginRequiredError` or a run that
collects zero cards.

The persistent context shares its cookies with its request client, which is why
photos can be downloaded through `context.request.get` without a separate HTTP
client and without plumbing cookies anywhere.

## A price band is a question, not a filter

One query is a ceiling, not just a filter. `sortBy=creation_time_descend` gives
the freshest listings first, but the feed stops handing out new cards after a
few hundred however far the scraper scrolls — `--max 500` does not collect five
times what `--max 100` does.

The way past it is to ask several narrower questions. `--bands N` splits the
`--min-price` … `--max-price` window into N adjacent ranges and runs the list
scrape once per range, each with its own `--max`. The bands touch but never
overlap, so a listing belongs to exactly one of them; a card that shows up in
two anyway (Facebook leaks results past its own filters) is scraped once.

Two consequences the code encodes:

- **Cards are taken band by band in rotation** — the freshest of the cheap
  band, then the freshest of the dear one, and so on. Order matters because the
  detail budget is shared by the whole run: taken band after band, a
  `--bands 4` sweep with a budget of 60 would spend all of it inside the
  cheapest quarter of the price range.
- **An empty band is an answer, not a failure.** The sweep continues. Only a
  run where *every* band came back empty is reported as a dead session.

A band that returns exactly `--max` cards is the signal that the window is not
exhausted: the ceiling was ours, not Facebook's, and there is more down there.
That is the rule the [scrape log](scrape-log.md) is kept for.

`--days` widens the other axis — the number goes straight into `daysSinceListed`
and Facebook decides what it honours — and `--category` points the same
machinery at another Marketplace category/location id.

## The detail budget counts pages, not cards

`--detail-cap` bounds the number of detail pages actually opened, not the
number of cards walked. A sorted feed puts known listings and reposts on the
first screens, and an index-based cap would spend the whole budget on cards
whose detail page is skipped anyway.

At 3–7 s of jitter plus the page load, budget roughly 10 s per detail page —
measured, that is about 7.2–7.6 detail pages per minute, night or day.

Rows are upserted inside the loop, one at a time. A run that dies half way
through (expired session, checkpoint, timeout) keeps everything up to that
point; only the CSV and HTML exports are left stale, and `rent-assistant export`
refreshes them.

Two environment variables raise the per-page timeouts for a long unattended
run: `RENT_ASSISTANT_PAGE_LOAD_TIMEOUT_MS` (default 30000) and
`RENT_ASSISTANT_CONTENT_TIMEOUT_MS` (default 15000). Before they existed, a single
slow `goto` killed the whole process — only `LoginRequiredError` was caught.

## A re-sighting must not clobber

A listing already in the DB is re-seen as a bare list card — its detail page is
not opened again — so what comes back has no description, no `published_at` and
no `raw_json`. `upsert_listing` therefore drops `None` values on update.
Writing them erased the ad text, the gallery URLs and the `content_key` on
every run that re-saw the table, which a wide sweep does by definition.

## Card-only rows, and why `--refresh` is the wrong fix

When the detail budget runs out mid-run, the loop keeps writing the remaining
cards as rows with a title and a price and nothing else. Those rows then make
the *next* run skip their detail pages forever: `known_fb_ids` returns every
`fb_id` in the table and does not care whether the row has a description.

`--refresh` re-walks the feed and re-reads rows that are already complete,
burning the budget on them. The right fix is `--ids` over the rows that are
actually incomplete:

```sh
sqlite3 ~/.rent-assistant/rentals.db \
  "select fb_id from listings where description is null limit 150;"
rent-assistant scrape --ids <id> --ids <id> ... --detail-cap 150 --no-photos
```

The 2026-08-25 sweep left 594 such rows; how they were caught up — in chunks of
20, with a per-id attempt counter and a poison list — is in
[scrape-log.md](scrape-log.md).

## Repairing single listings

`--ids` skips the feed and opens the named listings directly, whatever the DB
already holds for them. It is the repair path for three things:

- a listing whose stored text is Facebook's sidebar instead of the ad
  (`data_quality: sidebar_noise`);
- a listing scraped before galleries were collected, whose photos cannot be
  downloaded because the links were never stored;
- a listing whose signed gallery links have expired — after about a week only
  the detail page can hand out fresh ones.

An id with nothing behind it any more (deleted listing) is reported and
skipped.

**A dead page can damage the row.** If the page no longer serves the listing,
description extraction falls back to "longest text block" and writes Facebook's
sidebar over the real text — and the gallery it returns may belong to a
different listing entirely. That has happened once and is written up in
[dedupe.md](dedupe.md#the-trap-a-dead-pages-gallery-belongs-to-someone-else).
Worth fixing before any mass re-read.

## Facebook groups — a second source, one table

`rent-assistant scrape-groups` reads rental posts from `config.GROUPS`
(`phuketrentsale`, `Villa.For.Rent.Phuket`, `170117323644873` by default) into
the same `listings` table, tagged `source = "group:<id>"`. Everything
downstream — AI pass, photo pass, dedupe, decisions, export — treats a group
row exactly like a marketplace one; the `Source` column in the export is
the only place the two are told apart, and it filters like any other chip.

**The feed only discovers post ids — it never reads a post's content.** A
group's chronological feed (`?sorting_setting=CHRONOLOGICAL`) is
*virtualized*: scroll past a post and it leaves the DOM entirely, unlike the
marketplace feed, which keeps every card it has ever rendered. `scrape_group_feed`
therefore accumulates permalink ids across every scroll step, not by reading
the DOM once at the end. Content — title, body text, gallery, published date —
comes from the post's own permalink page, opened the same way a marketplace
detail page is, and counted against the same shared detail budget.

**A permalink page can render a second, unrelated post above the one the URL
names** — observed live as a cross-group recommendation. Both sit in their own
top-level `div[role="article"]`. The one that is *this* post is found by its
own self-link, an anchor inside it carrying `multi_permalinks=<post_id>` —
verified across two different groups. Every extraction (body text, gallery,
"See more" expansion) is scoped to that one article.

**Group-post images carry an empty `alt`.** Marketplace's alt-grouping
gallery filter (see below) does not apply to them at all — grouping by `""`
would merge every image on the page into one bucket. The article-scoping
above is the filter instead, and it is the stronger one: it excludes a
neighbouring post's images structurally, not by a caption heuristic.

**A post has no title and no relative "N days ago" timestamp.** The title is
synthesized from the page `<title>` (`"(N) <group name> | <first line of the
post>"` — only the part after the `|` is kept) or, failing that, the post's
own first line. The timestamp is FB's compact feed form (`"6 ч."` — "6 h", `"вчера в
10:11"` — "yesterday at 10:11"), parsed by `extractors.parse_feed_timestamp` — a marketplace detail
page never shows this form, only groups do, which is why it is a separate
function from `parse_published_at` rather than a loosened version of it.

**`scrape --ids` repairs a group row through the group reader, not the
marketplace one.** A known id looks up its own stored `url`/`source` before
deciding how to re-fetch it, so naming a group post's id never opens a dead
`/marketplace/item/<id>/` guess.

## Extraction is best-effort

The heuristics are expected to drift with Facebook's markup.

- `description` is taken from `raw_json.text_lines`, sliced after the
  `Описание` ("Description") anchor. Listings posted to the *general* Marketplace category
  rather than the property one have no such anchor and no "Информация о
  недвижимости" ("Property information") block; the fallback picks the longest text block and brings
  back the sidebar. Re-scraping cannot help — the page is the same every time.
  The ad text is still in `text_lines`, so the analysis can be done from there.
- `price_thb`, `bedrooms`, `property_type` are heuristic. When they give up,
  `price_raw` is still stored and you can eyeball it.
- The gallery is picked out locale-independently: `img` elements with
  `/t39.30808-6/` in `src` and a non-empty `alt`, grouped by `alt`, largest
  group wins.

## Troubleshooting

**"No active Facebook session. Run `rent-assistant login` first."**
Cookies expired or were cleared. Re-run `rent-assistant login` and sign in again.

**Zero list cards, or `LoginRequiredError` mid-scrape.**
Either the session died between commands, or Facebook shipped a markup change
and the selectors in `scraper_list.py` / `scraper_detail.py` need adjusting.

**Empty `price_thb` / `bedrooms` / `property_type`.**
The heuristic extractors gave up on that listing; `price_raw` is still there.

**Prices outside 15 000 – 35 000 THB.**
Facebook occasionally leaks results outside its own URL filters. The URL
builder is doing what it can; filter in the table.

**`data_quality: sidebar_noise` in the export.**
Amber cell, means *re-scrape this one* — `rent-assistant scrape --ids <fb_id>` — and
read the general-category caveat above before assuming it will help.
