# FB Marketplace Phuket Rentals Scraper — MVP (iteration 1)

## Overview

The first iteration of a system for finding long-term housing in Phuket through FB Marketplace.

The task: a script that is started by hand, scrapes fresh listings from [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals) in the 15,000–30,000 THB range, and stores them in a local SQLite database with an automatic CSV export. The database becomes the "single point of synchronisation" for every future iteration: the Hermes scheduler, group scraping, AI enrichment, a web artifact for viewing.

The first iteration is deliberately narrow — one source only, manual start, no AI. The goal is a working pipeline `URL → data → table`, on top of which automation and analysis are built later.

## Context (from discovery)

- **Project**: an empty directory (the repository root), only `.claude/`. We start from scratch.
- **Target URL**: `https://www.facebook.com/marketplace/110729412288885/propertyrentals` — the ID 110729412288885 pins the location to Phuket.
- **FB URL parameters** (verified on marketplace): `minPrice`, `maxPrice`, `sortBy=creation_time_descend`, `daysSinceListed=1|7|30`, `exact=false`. We use them to avoid fighting pagination — the freshest listings are always on top.
- **Known FB limitations**: aggressive anti-bot measures, no public API for marketplace, the listing detail page requires a login.
- **Future iterations** (out of scope, but they influence decisions): the Hermes scheduler, scraping FB groups, AI scoring of listings, a web viewer.

## Development Approach

- **testing approach**: Regular (code first, tests after). Unit tests only, for pure functions — extractors, normalisation, the DB layer. No E2E against live FB (fragile).
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for pure functions (parsing, DB, URL builder). For the Playwright code we skip tests on purpose — there we test by hand through `--dry-run` and a visual check of the CSV.
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change

## Autonomous Execution Notes (for autonomous agent execution)

Some steps need a live, logged-in Facebook and **cannot be done by an autonomous agent**. The convention:

- Checkboxes prefixed `MANUAL:` — the agent **skips them and marks them** `[x] MANUAL: ... (skipped — requires live FB / human action)`. It does not try to do them and does not block progress on them.
- Checkboxes prefixed `DESIGN:` — these record a design decision (for example, "we deliberately write no unit tests for Playwright code"). The agent **marks** `[x] DESIGN: ...` with no action.
- Everything else is automated as usual: code + unit tests + `pytest`.
- The list of FB selectors in Task 6/7 is a best-effort guess; the agent writes code from the description, the real check on live data stays with a human.

All `MANUAL:` items are also listed in the Post-Completion section — that is where a human picks up the list of what must be done by hand after an autonomous run.

## Testing Strategy

- **unit tests**: mandatory for pure functions — `parse_price`, `extract_bedrooms`, `extract_property_type`, `build_marketplace_url`, DB insert/upsert/export.
- **integration tests**: NOT written for the Playwright scrapers (network + live FB = fragile). Validation is a manual run.
- **e2e**: not applicable (no UI in this iteration).
- **manual verification**: after Task 7 — a manual `rent-assistant scrape` and a look at `rentals.csv` in Numbers.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

The architecture is a thin Python CLI on top of Playwright with a three-layer design:

```
┌───────────────────────────────────────────────┐
│  CLI (click)                                  │
│    rent-assistant login    ─┐                 │
│    rent-assistant scrape   ─┼─→ orchestrator  │
│    rent-assistant export   ─┘                 │
└──────────────┬────────────────────────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌────────────┐    ┌─────────────┐
│  scraper   │    │   storage   │
│ (playwright)│──→│ (SQLite)    │──→ rentals.csv
│  list      │    │  listings   │
│  detail    │    │  upsert     │
└────────────┘    └─────────────┘
       │                ▲
       ▼                │
┌────────────┐          │
│ extractors │──────────┘
│ (regex)    │
│ bedrooms   │
│ type       │
│ price      │
└────────────┘
```

Key decisions:

1. **Playwright + persistent context**. You log in by hand once through `rent-assistant login`; cookies/localStorage are saved in `~/.rent-assistant/browser-profile/`. After that the scraper starts headless and reuses the session. FB cookies live 1–4 weeks — on a 401/redirect to the login page we show a clear error with instructions.
2. **URL parameters instead of pagination**. `sortBy=creation_time_descend` + `minPrice=15000&maxPrice=30000` + `daysSinceListed=7`. We scroll the list to ~N cards (40 by default) and do not chase every page. With regular runs there are < 10 new cards — everything fits on the first "page".
3. **SQLite as the source of truth**, CSV as a flat export for viewing. Later the AI agent works through `sqlite3 rentals.db "SELECT..."`, and the human looks at `rentals.csv` in Numbers/Excel.
4. **Deduplication by FB listing ID** (a URL slug like `/marketplace/item/1234567890`). Upsert on this key: if the ID already exists — update `last_seen_at`, `price` (it can change) and `description`; if not — insert with `first_seen_at`.
5. **Rate limiting**: a pause of 3–7 seconds (jitter) between detail-page requests; between scrape sessions — at the user's discretion (started by hand). Overall cap: no more than 60 detail requests per run.
6. **Heuristic extractors** for `bedrooms` and `property_type` — regex/keywords on title + description. AI enrichment is a future iteration (deliberately not in this one).

## Technical Details

### DB schema (SQLite)

```sql
CREATE TABLE listings (
  fb_id           TEXT PRIMARY KEY,       -- ID from the URL /marketplace/item/{id}
  url             TEXT NOT NULL,
  title           TEXT NOT NULL,
  price_thb       INTEGER,                -- normalised to THB, NULL if not parsed
  price_raw       TEXT,                   -- the original string from the site
  district        TEXT,                   -- raw location string from the card
  bedrooms        INTEGER,                -- from extract_bedrooms, NULL if not found
  property_type   TEXT,                   -- villa/condo/apartment/house/studio/unknown
  description     TEXT,                   -- full description (can be long)
  published_at    TEXT,                   -- ISO publication date (what FB gives, e.g. "3 days ago" → normalised)
  first_seen_at   TEXT NOT NULL,          -- ISO datetime of the first scrape
  last_seen_at    TEXT NOT NULL,          -- ISO datetime of the last scrape
  raw_json        TEXT                    -- raw dict from the detail page for debugging and future AI
);

CREATE INDEX idx_last_seen ON listings(last_seen_at DESC);
CREATE INDEX idx_price ON listings(price_thb);
```

### URL builder

```python
def build_marketplace_url(
    category_id: str = "110729412288885",  # Phuket Property Rentals
    min_price: int = 15000,
    max_price: int = 30000,
    days_since_listed: int = 7,
) -> str:
    ...
# → https://www.facebook.com/marketplace/{category_id}/propertyrentals?minPrice=15000&maxPrice=30000&daysSinceListed=7&sortBy=creation_time_descend&exact=false
```

### CLI commands

- `rent-assistant login` — opens a headed browser, waits for the user to log in, saves the session, exits.
- `rent-assistant scrape [--max 40] [--days 7] [--dry-run]` — the main command: scrapes the list + details, writes to the DB, regenerates the CSV.
- `rent-assistant export [--path rentals.csv]` — only regenerates the CSV from the DB without scraping (useful for checks).

### Project structure

```
rent-assistant/
├── pyproject.toml
├── README.md
├── .gitignore
├── src/rent_assistant/
│   ├── __init__.py
│   ├── cli.py              # click CLI
│   ├── config.py           # constants, paths, defaults
│   ├── storage.py          # SQLite layer
│   ├── extractors.py       # pure parsers (price, bedrooms, type)
│   ├── url_builder.py      # build_marketplace_url
│   ├── browser.py          # Playwright session helpers
│   ├── scraper_list.py     # list scraping
│   └── scraper_detail.py   # detail page scraping
├── tests/
│   ├── test_extractors.py
│   ├── test_storage.py
│   └── test_url_builder.py
└── docs/
    └── plans/
```

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): all scraper code, DB, CLI, tests — what we do in this repo in this iteration.
- **Post-Completion** (no checkboxes): the manual FB login, debugging the selectors on the live site, plans for iteration 2.

## Implementation Steps

### Task 1: Project skeleton + dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/rent_assistant/__init__.py`
- Create: `src/rent_assistant/config.py`
- Create: `README.md` (minimal)

- [x] create `pyproject.toml` with the dependencies: `playwright`, `click`, `python-dateutil`; dev: `pytest`
- [x] create `src/rent_assistant/config.py` with the constants `DB_PATH`, `CSV_PATH`, `BROWSER_PROFILE_DIR` (all under `~/.rent-assistant/` for local storage, overridable through the env var `RENT_ASSISTANT_HOME`)
- [x] create `.gitignore` (`.venv/`, `__pycache__/`, `*.db`, `*.csv`, `~/.rent-assistant/`, `.pytest_cache/`)
- [x] create a minimal `README.md` with the install command (`pip install -e . && playwright install chromium`)
- [x] run `python -c "import rent_assistant.config; print(rent_assistant.config.DB_PATH)"` — must work

### Task 2: URL builder

**Files:**
- Create: `src/rent_assistant/url_builder.py`
- Create: `tests/test_url_builder.py`

- [x] implement `build_marketplace_url(category_id, min_price, max_price, days_since_listed) -> str` in `src/rent_assistant/url_builder.py` with defaults from config
- [x] URL parameters: `minPrice`, `maxPrice`, `daysSinceListed`, `sortBy=creation_time_descend`, `exact=false`
- [x] write `tests/test_url_builder.py`: the default URL for Phuket 15k-30k, custom ranges, boundary values
- [x] a test for invalid arguments (min > max — must raise `ValueError`)
- [x] `pytest tests/test_url_builder.py -v` — all green

### Task 3: Text extractors (price, bedrooms, property type)

**Files:**
- Create: `src/rent_assistant/extractors.py`
- Create: `tests/test_extractors.py`

- [x] `parse_price(raw: str) -> int | None` — parses strings like `"THB25,000"`, `"฿15 000/month"`, `"20K THB"`, returns an integer in THB or None
- [x] `extract_bedrooms(title: str, description: str) -> int | None` — regex on `2BR`, `2 bedrooms`, `studio` (=0), `1BR` and so on
- [x] `extract_property_type(title: str, description: str) -> str` — keyword match: `villa|condo|apartment|house|studio|townhouse`, default `"unknown"`
- [x] write `tests/test_extractors.py`: table-driven over 8-10 real examples from FB (copy a couple of listings by hand beforehand for fixtures)
- [x] edge cases: empty string, digits only with no currency, "PRICE ON REQUEST", very large amounts, unicode currency symbols
- [x] `pytest tests/test_extractors.py -v` — all green

### Task 4: SQLite storage layer

**Files:**
- Create: `src/rent_assistant/storage.py`
- Create: `tests/test_storage.py`

- [x] `init_db(path: Path)` — creates the table and indexes from the schema above, idempotent
- [x] `upsert_listing(conn, listing: dict) -> str` — insert or update by `fb_id`; returns `"inserted"` / `"updated"`; on update: updates `last_seen_at`, `price_thb`, `description`, `raw_json`, keeps `first_seen_at`
- [x] `export_csv(conn, path: Path)` — regenerates the CSV from the DB, sorted by `last_seen_at DESC`; columns in this order: `price_thb`, `title`, `bedrooms`, `district`, `property_type`, `published_at`, `url`, `description`, `first_seen_at`, `last_seen_at`
- [x] write `tests/test_storage.py` with in-memory SQLite (`":memory:"`): init creates the table, insert of a new row, upsert of an existing row keeps `first_seen_at`, export returns the right column order
- [x] edge-case tests: NULL price, unicode in the description, duplicates
- [x] `pytest tests/test_storage.py -v` — all green

### Task 5: Browser/session helper (Playwright)

**Files:**
- Create: `src/rent_assistant/browser.py`

- [x] `launch_persistent(headless: bool) -> BrowserContext` — uses `chromium.launch_persistent_context(user_data_dir=BROWSER_PROFILE_DIR)`, a realistic `user_agent` (latest stable Chrome on macOS), `viewport 1280x900`, `locale='en-US'`
- [x] `is_logged_in(context) -> bool` — opens `https://www.facebook.com/`, checks for the profile selector / the absence of login forms
- [x] `login_flow(context)` — opens `facebook.com/login` in headed mode, waits for the manual login event (polling `is_logged_in` every 3 seconds for up to 5 minutes), exits
- [x] DESIGN: we deliberately write NO tests for this module — it is integration with a live browser; we check by hand through the CLI in Task 8
- [x] MANUAL: `rent-assistant login` opens the browser, log in by hand, the second command works headless (skipped — requires live FB / human action)

### Task 6: List scraper

**Files:**
- Create: `src/rent_assistant/scraper_list.py`

- [x] `scrape_list(context, url: str, max_items: int) -> list[dict]` — opens `url`, waits for the cards to load, scrolls down until it collects `max_items` unique cards or new ones stop appearing (2 attempts with no growth → stop)
- [x] for each card in the list extracts: `fb_id` (from the link `/marketplace/item/{id}`), `url`, `title`, `price_raw`, `district` (usually the second line on the card), `cover_image_url`
- [x] selectors: use `role`/`aria-label` where possible (FB changes CSS classes often), fall back to the semantic `a[href*="/marketplace/item/"]`
- [x] on 0 cards or a detected login redirect — raises `LoginRequiredError` with a clear message
- [x] DESIGN: deliberately without unit tests (needs live FB); checked by hand in Task 9
- [x] protection against endless scrolling: a hard limit of `max_items * 2` scroll attempts
- [x] MANUAL: confirm the selectors work on live FB (best-effort guess in the code — an iteration is almost guaranteed) (skipped — requires live FB / human action)

### Task 7: Detail scraper

**Files:**
- Create: `src/rent_assistant/scraper_detail.py`

- [x] `scrape_detail(context, url: str) -> dict` — opens the listing URL, waits for it to load, clicks "See more" on the description if present, extracts: `description` (full), `published_at` (raw FB text like "Listed 3 days ago" — stored as is in the first iteration), `price_raw` (can differ from the list), `raw_json` (everything we managed to get, as a dict)
- [x] rate limit: `random.uniform(3, 7)` seconds before each call (not inside — the orchestrator controls it)
- [x] handling of 404 / a removed listing: returns `None`, does not crash
- [x] DESIGN: deliberately without unit tests; manual check in Task 9
- [x] MANUAL: confirm the selectors work on a live FB card (best-effort guess in the code) (skipped — requires live FB / human action)

### Task 8: CLI orchestration

**Files:**
- Create: `src/rent_assistant/cli.py`
- Modify: `pyproject.toml` (add the entry point `rent-assistant = rent_assistant.cli:main`)

- [x] `rent-assistant login` — calls `browser.login_flow`, prints the status
- [x] `rent-assistant scrape [--max 40] [--days 7] [--dry-run] [--skip-details]` — orchestrator:
  - opens the persistent context (headless)
  - checks `is_logged_in`; when False — a clear error "run `rent-assistant login`"
  - builds the URL through `url_builder`
  - `scrape_list` → for each listing (limit 60 per run): `scrape_detail` with a delay, extract fields, upsert into the DB
  - `--dry-run`: prints what it would do, does not write to the DB
  - `--skip-details`: list only, without visiting the cards (a quick incremental check)
  - at the end: `export_csv`, prints the summary "inserted: X, updated: Y, csv: /path/to/rentals.csv"
- [x] `rent-assistant export [--path]` — only regenerates the CSV
- [x] `rent-assistant --help` prints all commands with descriptions
- [x] DESIGN: we write NO tests for the CLI (a thin wrapper); the logic is already covered by the unit tests of the lower layers
- [x] run `pip install -e .` and `rent-assistant --help` — the commands are visible

### Task 9: Verify acceptance criteria

**Automatable** (the agent does it):
- [x] `pytest tests/ -v` — all unit tests green (67 passed)
- [x] `rent-assistant --help` — the commands are visible, `rent-assistant scrape --help` shows all flags
- [x] `rent-assistant scrape --max 5 --dry-run` without a logged-in session — prints a clear error "run `rent-assistant login`", does not crash with a stack trace (exit=3, friendly message)
- [x] `rent-assistant export --path /tmp/empty.csv` on an empty DB — creates a CSV with a header and no rows, exit code 0

**Requires manual action** (the agent marks as skipped):
- [x] MANUAL: `rent-assistant login` — log in by hand, see the success message (skipped — requires live FB / human action)
- [x] MANUAL: `rent-assistant scrape --max 5 --dry-run` — prints a list of 5 listings without writing (skipped — requires live FB / human action)
- [x] MANUAL: `rent-assistant scrape --max 10` — a real run, ~10 rows in the DB, the CSV is generated (skipped — requires live FB / human action)
- [x] MANUAL: open `rentals.csv` in Numbers/Excel — the columns are readable, the fields are filled (NULL is acceptable for heuristic fields) (skipped — requires live FB / human action)
- [x] MANUAL: the prices of all rows are within 15000-30000 THB (or NULL if not parsed) (skipped — requires live FB / human action)
- [x] MANUAL: a repeated `rent-assistant scrape --max 10` — `updated > 0`, `inserted` may be 0, no duplicates (skipped — requires live FB / human action)
- [x] MANUAL: `rent-assistant scrape --max 60` — a run on a larger volume, measure the time; there must be no bans/captchas (skipped — requires live FB / human action)

### Task 10: [Final] Update documentation

**Files:**
- Modify: `README.md`

- [x] add to `README.md`: installation (venv, pip install, playwright install chromium), quickstart (login → scrape → csv), a description of all CLI commands with examples, where the DB lives (`~/.rent-assistant/rentals.db`), troubleshooting (session expired → re-login)
- [x] note in the README that this is iteration 1 and link to this plan file as context for future iterations
- [x] move this plan file: `mkdir -p docs/plans/completed && mv docs/plans/20260824-fb-marketplace-scraper-mvp.md docs/plans/completed/`

## Post-Completion

*Requires manual action or belongs to future iterations.*

**What has to be done by hand after the autonomous run** (collected from all `MANUAL:` items above):
1. `rent-assistant login` — log in in the browser that opens (2FA, if enabled, is entered there too); confirm that headless mode sees the session afterwards
2. `rent-assistant scrape --max 5 --dry-run` — smoke check that the card list is parsed
3. `rent-assistant scrape --max 10` — a real run, check that the DB and the CSV are filled
4. Open `rentals.csv` in Numbers/Excel — visually check the prices (15000-30000 THB), the fields, the locations (Rawai/Chalong/Kata/Patong/…)
5. A repeated `rent-assistant scrape --max 10` — confirm deduplication (updated > 0)
6. `rent-assistant scrape --max 60` — a run on a larger volume, measure the time to plan the Hermes interval
7. If the selectors in `scraper_list.py` / `scraper_detail.py` did not work — iterate on live data (an expected part of the work, not a "bug")

**Known limitations of this iteration** (fixed in the next ones):
- `published_at` is stored as the raw FB string like "Listed 3 days ago" — parsing into an ISO date is postponed
- `bedrooms` / `property_type` are heuristics, misses are possible; AI enrichment is planned as a separate iteration
- no account of the personal housing requirements — just collecting into the DB, filtering by eye in the CSV; scoring/filters are the next iteration
- no notifications about new listings; they come with the Hermes scheduler

**Next iterations** (out of scope, for context):
1. AI enrichment (bedrooms/type/quality/scoring against the personal requirements) through the Claude API
2. Scraping Phuket rental FB groups
3. The Hermes scheduler: a schedule + diff notifications about new listings
4. A web viewer (simple Flask/FastAPI or static HTML from the CSV) with cards, a truncated description, filters
5. Monitoring of session expiry + automatic retry with an alert
