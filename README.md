# fb-rent

FB Marketplace Phuket rentals scraper — **iteration 1 (MVP)**.

Scrapes long-term rental listings from the [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals)
category (15 000 – 30 000 THB) into a local SQLite database, with CSV and
self-contained HTML exports for browsing in Numbers/Excel or a browser.

Design rationale, decisions, and the roadmap for future iterations live in
[`docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md`](docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md).

## Install

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Quickstart

```sh
fb-rent login          # one-time headed login, session persisted to ../data/
fb-rent scrape         # headless scrape, writes ../data/rentals.{db,csv,html}
fb-rent export         # regenerate CSV+HTML from the DB without scraping
```

Open `../data/rentals.html` in a browser (sortable, searchable, paginated —
Grid.js loaded from a CDN) or `../data/rentals.csv` in Numbers/Excel.

## Local state

Everything lives in the sibling `data/` directory (i.e. `../data/` relative
to this `scrapper/` package):

- `browser-profile/` — persistent Chromium profile (cookies, localStorage)
- `rentals.db` — SQLite database (source of truth)
- `rentals.csv` — flat export, regenerated on every scrape
- `rentals.html` — self-contained HTML table (Grid.js via CDN, needs internet
  the first time), regenerated on every scrape

Override the base path with the `FB_RENT_HOME` environment variable, e.g.
`FB_RENT_HOME=/tmp/fb-rent-test fb-rent scrape`.

## Commands

### `fb-rent login`

Opens Chromium in headed mode and waits (up to 5 minutes) for you to sign in
manually — including 2FA if enabled. On success the cookies are stored in the
persistent profile so later `scrape` runs can reuse the session headlessly.

Re-run this command whenever Facebook invalidates the session (typically every
1–4 weeks, or after suspicious-activity checks).

### `fb-rent scrape`

Runs the full pipeline: build search URL → scrape list → walk each detail page
with a 3–7 s random jitter → extract fields → upsert into SQLite → regenerate
CSV and HTML.

| Flag              | Default | Description                                              |
|-------------------|---------|----------------------------------------------------------|
| `--max`           | `40`    | Maximum number of list cards to collect.                 |
| `--days`          | `7`     | Filter to listings created in the last N days.           |
| `--dry-run`       | off     | Print what would be written without touching the DB.     |
| `--skip-details`  | off     | List cards only, skip per-item detail pages.             |

Detail requests are hard-capped at 60 per run to stay polite.

Examples:

```sh
fb-rent scrape --max 10 --dry-run       # smoke check, no writes
fb-rent scrape --max 40                 # regular run
fb-rent scrape --max 60 --skip-details  # fast incremental sweep
```

### `fb-rent export`

Regenerates the CSV and HTML from the current DB. Handy for reformatting after
schema tweaks or verifying an empty DB produces a header-only CSV.

```sh
fb-rent export                              # writes ../data/rentals.{csv,html}
fb-rent export --path /tmp/rentals.csv      # custom CSV destination
fb-rent export --html-path /tmp/rentals.html # custom HTML destination
```

## Troubleshooting

**"No active Facebook session. Run `fb-rent login` first."**
Cookies expired or were cleared. Re-run `fb-rent login` and sign in again.

**Zero list cards / `LoginRequiredError` mid-scrape.**
Either the session died between commands, or Facebook shipped a marketplace
markup change and the selectors in `scraper_list.py` / `scraper_detail.py` need
adjusting. Selectors are best-effort and expected to drift.

**Empty `price_thb` / `bedrooms` / `property_type`.**
Heuristic extractors gave up on that listing — `price_raw` is still stored, so
you can eyeball it in the CSV or improve the extractor later.

**Prices outside 15 000 – 30 000 THB.**
Facebook occasionally leaks results outside the URL filters. Filter in the CSV;
the URL builder is doing what it can.
