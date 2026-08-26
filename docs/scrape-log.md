# Scrape log

What has been collected and where to continue from. The database is the source
of truth; this file only retells it in human words and keeps what the DB does
not have: the flags a run used, and whether a band hit our own cap.

## A snapshot (ask the DB, do not trust this file)

```sh
sqlite3 ../data/rentals.db "
select 'total', count(*) from listings
union all select 'with text', count(*) from listings where description is not null
union all select 'card only', count(*) from listings where description is null
union all select 'duplicates', count(*) from duplicates;"
```

The catch-up queue is the same text-less rows, as a list of ids:

```sh
sqlite3 ../data/rentals.db \
  "select fb_id from listings where description is null limit 150;"
```

## The rule of entry

We scrape the **last 7 days** (`--days 7`). We move to 30 only once the
seven-day window stops yielding anything new — that is, when the bands start
returning fewer cards than `--max` and nearly everything in them is already
known.

The sign that the window is *not* exhausted: a band returned exactly `--max`
cards. The ceiling was our cap, not Facebook's, and there is more left in
there.

## Runs

### 2026-08-25, 15:26–15:48 UTC

```sh
fb-rent scrape --bands 6 --max 150 --days 7 --detail-cap 150 --no-photos
```

| Band, THB | Cards | Hit `--max` |
|---|---|---|
| 15000–17499 | 150 | yes |
| 17500–19999 | 150 | yes |
| 20000–22499 | 150 | yes |
| 22500–24999 | 70 | no — exhausted |
| 25000–27499 | 150 | yes |
| 27500–30000 | 150 | yes |

Run totals: `inserted: 709, updated: 57, duplicates: 54, skipped_known: 57`.
The 150-page detail budget ran out around the seventh minute; the rate without
photos was **7.6 detail pages per minute**.

State afterwards: 799 rows, of which **205 with text** and **594 card only**
(title, price, district and nothing else). 63 duplicates.

Five bands of six hit the cap → the seven-day window is **not exhausted**; the
next run is `--days 7` again, with a larger `--max`.

#### What was left to do after that run

1. **Catch up the 594 cards** in batches of 150:
   `fb-rent scrape --ids <150 ids> --detail-cap 150 --no-photos`. Four batches,
   ~20 minutes each, an hour or so between them. Distribution across the bands
   as of 25 August: 95 / 115 / 116 / 36 / 116 / 116.
2. **The text pass** over the 205 listings that have text (`/analyze`, 9
   batches). It never touches Facebook, so it can run alongside the catch-up.
3. Galleries were not downloaded (`--no-photos`). The links in `gallery_urls`
   live about a week — `fb-rent photos-fetch` had to happen before
   1 September 2026, after that only via `scrape --ids`.

### 2026-08-26 — the overnight catch-up of 594 cards

Driver script: `data/catchup/run.sh` (log in `data/catchup/catchup.log`). Not
batches of 150 but **chunks of 20**: after every chunk the list of missing rows
is re-queried from the DB, so a chunk that dies does not take the rest with it.
Every id carries an attempt counter (`data/catchup/attempts.tsv`); after three
failures it goes to a poison list and is not picked up again. `exit 3` (the
Facebook session expired) stops the whole run rather than spinning through
useless attempts.

Timeouts were raised through the new environment variables —
`FB_RENT_PAGE_LOAD_TIMEOUT_MS=90000`, `FB_RENT_CONTENT_TIMEOUT_MS=45000`
(`scraper_detail.py`; the 30 000 / 15 000 defaults are untouched). Before that,
a single slow `goto` threw out of `scrape_detail` and killed the whole process:
only `LoginRequiredError` was being caught.

**Result: 592 of 592 in 82 minutes, 0 given up, not one browser restart.** The
rate was 7.2 detail pages per minute — an overnight run goes at exactly the
speed of a daytime one, and the whole gain comes from nobody being there to
interrupt it. `No raw data` in `ai-status` dropped to 0.

The text pass followed: **779 analysed, pending 0, stale 0**; 309 active, 470
rejected (215 under 35 m², 191 pets forbidden, 25 studios, 6 Patong).

#### The sidebar instead of the description: 19 rows, a re-scrape does not fix it

`data/catchup/rescrape_noise.sh` re-read the 22 rows whose `description` held
the "Выбор дня" block. Success was defined as the description **changing**, not
merely existing — otherwise the script would have happily counted the very same
sidebar.

Nine were fixed, thirteen were not, and here is why: those are items in the
**general** Marketplace category, not the property one. Their detail page has
neither an "Информация о недвижимости" block nor an "Описание" heading — the ad
text starts right after "Подробнее". `_extract_description` does not know that
anchor, falls back to "the longest text block" and brings back the sidebar. A
repeated scrape is powerless here by construction: the page is the same every
time.

The ad itself is on the page and sits in `raw_json.text_lines`, so all 19 were
analysed from `text_lines` and carry real fields and verdicts. What should be
fixed is the extraction — adding "Подробнее" / "See details" to the anchor set
in `description_from_text_lines` — not the data.
