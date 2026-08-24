# fb-rent

FB Marketplace Phuket rentals scraper — iteration 1 (MVP).

Scrapes long-term rental listings from the [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals)
category (15 000 – 30 000 THB) into a local SQLite database, with a CSV export
for browsing in Numbers/Excel.

## Install

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Quickstart

```sh
fb-rent login          # one-time headed login, session persisted to ~/.fb-rent/
fb-rent scrape         # headless scrape, writes ~/.fb-rent/rentals.db + rentals.csv
fb-rent export         # regenerate CSV from the DB without scraping
```

Local state lives in `~/.fb-rent/` (override with `FB_RENT_HOME=/some/path`).

See `docs/plans/20260824-fb-marketplace-scraper-mvp.md` for the design rationale
and roadmap for future iterations.
