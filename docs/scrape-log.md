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
rent-assistant scrape --bands 6 --max 150 --days 7 --detail-cap 150 --no-photos
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
   `rent-assistant scrape --ids <150 ids> --detail-cap 150 --no-photos`. Four batches,
   ~20 minutes each, an hour or so between them. Distribution across the bands
   as of 25 August: 95 / 115 / 116 / 36 / 116 / 116.
2. **The text pass** over the 205 listings that have text (`/analyze`, 9
   batches). It never touches Facebook, so it can run alongside the catch-up.
3. Galleries were not downloaded (`--no-photos`). The links in `gallery_urls`
   live about a week — `rent-assistant photos-fetch` had to happen before
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
`RENT_ASSISTANT_PAGE_LOAD_TIMEOUT_MS=90000`, `RENT_ASSISTANT_CONTENT_TIMEOUT_MS=45000`
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

### 2026-08-26, 15:43–18:08 — the sweep with no detail budget

```sh
BANDS=6 MAXITEMS=200 DAYS=7 bash data/sweep/run.sh
# → rent-assistant scrape --bands 6 --max 200 --days 7 --detail-cap 99999 --no-photos
#   RENT_ASSISTANT_PAGE_LOAD_TIMEOUT_MS=90000 RENT_ASSISTANT_CONTENT_TIMEOUT_MS=45000
```

| Band, THB | Cards | Hit `--max` | Same band on 25 Aug (`--max 150`) |
|---|---|---|---|
| 15000–17499 | 200 | yes | 150 — capped |
| 17500–19999 | 164 | no | 150 — capped |
| 20000–22499 | 170 | no | 150 — capped |
| 22500–24999 | 62 | no | 70 |
| 25000–27499 | 200 | yes | 150 — capped |
| 27500–30000 | 200 | yes | 150 — capped |

996 cards collected in seven minutes, then two hours of detail pages. Run
totals: `inserted: 875, updated: 6, duplicates: 115, skipped_known: 6, photo
hashes: 819`. State afterwards: **1378 rows, 0 of them card-only**, 472
duplicates. The catch-up driver was invoked and found nothing to do.

**`--detail-cap` is what made the 594 pustyshki of 25 August, not Facebook.**
The cap was 150 against a feed of 799, and the log said so plainly (`Detail
budget spent (150 pages)`). Here it was 99999 — larger than any plausible feed
— and every one of the 875 new listings got its detail page inside the same
run. The rate held at 6–7 pages per minute for 145 minutes with no browser
restart and no `LoginRequiredError`, at the same 3–7 s jitter: there is still
no evidence of Facebook throttling detail pages.

The driver (`data/sweep/run.sh`) re-queries the DB after the sweep and hands
whatever is left without a description to `data/catchup/run.sh`. On this run
the hand-off was a no-op, which is the point — the safety net is for a crash,
not for a budget we set too low ourselves.

#### How much of the seven-day window we now hold: ~90 %

Bands 2 and 3 were capped at 150 the day before and came back with 164 and 170
under a cap of 200 — **their true size exceeded the old cap by only 9–13 %**.
If the three bands still capped at 200 behave the same way, they hide 60–120
cards, putting the whole window at 1050–1120 against the 996 collected.

Two limits on that number: it is a share of what Facebook is willing to show
(a band capped at 200 may be its ceiling rather than ours — the two are
indistinguishable from here), and it covers 15 000–30 000 THB only.

Next run: the three capped bands one at a time with a larger cap, e.g.
`--min-price 15000 --max-price 17499 --bands 1 --max 400 --days 7
--detail-cap 99999 --no-photos`. A band that returns exactly 400 has hit
Facebook's own ceiling, and the honest answer is then to split it in two.

#### What is left after this run

1. **The text pass** over 881 pending (875 new + 6 changed) — `/analyze`,
   it never touches Facebook.
2. Galleries were not downloaded. The signed links live about a week, so
   `photos-fetch` for whatever survives the hand-rejection has to happen
   before ~2 September 2026.

### 2026-08-26, 18:30–20:52 — the top-up of the three capped bands

```sh
MAXITEMS=400 bash data/sweep/topup.sh   # one band per query, cap doubled
```

| Band, THB | Cards (cap 400) | New rows | Known among the cards |
|---|---|---|---|
| 15000–17499 | 344 | +292 | 35 |
| 25000–27499 | 268 | +162 | died mid-band, see below |
| 27500–30000 | 288 | +230 | 13 |

**2062 rows afterwards (+684), still 0 without description.**

#### A band is a sample, not a set — the exhaustion signal is weak

The 15000–17499 band had already given 200 cards that morning, and those rows
were in the DB. Asked again eight hours later it returned 344 cards of which
**only 35 were known**. Two queries against the same band, at the same
`sortBy=creation_time_descend`, overlap by about 15 %.

So `cards < --max` means *this query* ran dry, not that the band holds nothing
more. Every coverage estimate in this file that rests on the cap-hit signal is
weaker than it reads — the morning sweep's "~90 % of the window" was, in
hindsight, nearer 70 %. The only honest measurement is empirical: ask the same
band again and count what is new. As of this run the well is not dry.

#### The goto timeout that killed a band

`Page.goto: Timeout 90000ms exceeded` threw straight out of `scrape_detail`
and ended the process with roughly 106 of the band's 268 cards unread. Raising
the timeouts on 26 August had been only half the fix: the handler still caught
`LoginRequiredError` alone.

Fixed in `f94bdc8` — a timed-out `goto` now answers `None`, the same as a page
that never rendered, so the caller keeps the list-card row and the catch-up
driver repairs it by `--ids`. A login redirect *during* the timeout still
raises, because an expired session must stop the run rather than quietly write
hundreds of empty rows. Three regression tests in
`tests/test_scraper_detail.py`.

The band was re-run afterwards on the fixed code.

#### The re-run on the fixed code, 20:53–21:52

`BANDS_TO_REDO="25000:27499" bash data/sweep/topup.sh` — 262 cards, `+173`
rows, exit 0. No crash where the same band had died an hour earlier.

Two listings answered `detail unavailable, keeping list-card data`, which is
the new timeout path doing its job: the run kept going and left two card-only
rows behind. The drain that follows the sweep repaired **both** by `--ids`
(`2 -> 0 missing, fetched 2, gave up on 0`) — so they were slow pages, not
dead listings, and under the old code either one of them would have ended the
band.

Final state for the day: **2235 rows, 0 without description**, 599 duplicates.
The text pass has 1752 pending. Scraping stops here — the seven-day window is
as full as it is worth making it, and the bottleneck moved to how many
listings a person can actually look at.
