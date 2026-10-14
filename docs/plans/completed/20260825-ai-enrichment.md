# AI enrichment of listings — iteration 2

## Overview

Iteration 1 built the pipeline `URL → SQLite → CSV/HTML`. Every field is extracted with regexes; `extractors.py` says in plain words that AI enrichment is the next iteration, and `raw_json` is kept "for the future AI".

This iteration does that enrichment: a tool that can be pointed at the listings not yet processed, so that the AI pulls the significant facts out of the description and the other fields, fills the table columns and writes a short verdict on each property. The goal is to run the housing search from one HTML page instead of 40 Facebook tabs.

Three things define the architecture:

1. **The AI here is Claude Code (Opus, high thinking effort)**, not an API client. So the "tool" is a contract "batch out / result in" plus a skill that drives the cycle. No new runtime dependencies, no `anthropic` SDK.
2. **The schema must evolve.** When the AI notices a recurring useful signal that has no column, it asks: does it matter, and should the old listings be re-analysed or left as `unknown` in a separate colour.
3. **The current text from the scraper is unusable for analysis.** In 10 of 21 rows `description` is NULL outright; in most of the rest the "Выбор дня" (Pick of the day) sidebar ended up there instead of the ad itself. `title` = `"Недавно опубликовано"` (Recently listed) in 100% of rows, `published_at` is NULL in 100%. Feed this to the AI and you get confident nonsense, so it is fixed first.

None of this is valuable in itself: scraping and analysis can be restarted from scratch at any moment. Only the user's own decisions (accepted/rejected) are irreplaceable — they live outside the DB.

## The search criteria that get encoded

Two adults and a small dog, moving on the date in the requirements letter, a one-year contract, 15,000–30,000 THB/month. A house or townhouse in a good gated village (like Pruksa Ville), fully furnished, ≥1 bedroom + a living room, not a tiny kitchen, a green area nearby for walks, parking and good internet. A tourist zone and closeness to the beach are not required.

**Permission for pets is the most important criterion.**

All of this lives in a new editable `config/requirements.md` (the text of the letter to the agent verbatim + a checklist of criteria), which is inserted whole into every batch. Changing the priorities later = editing one markdown file, not code.

## Context (from discovery)

- **Repository**: the repository root (git root); runtime data in a separate data directory, `$RENT_ASSISTANT_HOME` (gitignored).
- **Stack**: Python 3.11+, click CLI, Playwright, SQLite. Package `rent-assistant` v0.1.0, `src/` layout, entry point `rent-assistant = rent_assistant.cli:main`. Tests — pytest, 67+ of them, `pythonpath = ["src"]`.
- **Existing modules**: `config.py`, `url_builder.py`, `extractors.py` (pure functions + table-driven tests), `storage.py` (`init_db`/`upsert_listing`/`export_csv`/`export_html`), `browser.py`, `scraper_list.py`, `scraper_detail.py`, `cli.py`.
- **DB**: `$RENT_ASSISTANT_HOME/rentals.db`, one table `listings` (`fb_id` PK, `url`, `title`, `price_thb`, `price_raw`, `district`, `bedrooms`, `property_type`, `description`, `published_at`, `first_seen_at`, `last_seen_at`, `raw_json`). 21 rows.
- **`raw_json`** — `{"url", "final_url", "title", "text_lines": [...]}`, where `text_lines` = the first 200 non-empty lines of the detail page. This is the richest and the most reliable material for the AI, richer than the `description` column.
- **HTML export**: `storage.export_html` (`storage.py:207-341`) — a self-contained file, data inline in `const DATA`, rendered with Grid.js from the unpkg CDN, escaping of `</` and `<!--` plus the `esc()` helper.
- **No AI code at all**: `grep -riE "anthropic|openai|claude|llm"` over `src/` is empty. Dependencies — only playwright, click, python-dateutil.

## Development Approach

- **testing approach**: Regular (code first, tests after). Unit tests are mandatory for all pure functions: extractors, the field registry, decision rules, the DB layer, batch building/intake.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for pure functions. For Playwright code and for CLI wrappers we skip tests on purpose (marked `DESIGN:`).
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change
- backward compatibility: the existing 67 tests must stay green, `rent-assistant scrape/export/login` do not change their signatures

## Autonomous Execution Notes (for autonomous agent execution)

The convention is the same as in iteration 1:

- Checkboxes prefixed `MANUAL:` — the agent **skips them and marks them** `[x] MANUAL: ... (skipped — requires live FB / human action)`. It does not try to do them and does not block progress on them.
- Checkboxes prefixed `DESIGN:` — a recorded design decision (for example, "we deliberately write no unit tests for Playwright code"). The agent **marks** `[x] DESIGN: ...` with no action.
- Everything else is automated as usual: code + unit tests + `pytest`.
- One task per iteration; after the task — a full `pytest` run, ticking the checkboxes, a commit.

**What is specific to this iteration — what the agent cannot do:**

1. `rent-assistant scrape --refresh` needs a live logged-in FB session → `MANUAL:`.
2. **The AI analysis run itself cannot be done autonomously**: it needs an interactive Claude Code session (the `AskUserQuestion` step about new fields) and fresh data from FB. The agent builds the **tooling**, it does not run the analysis → `MANUAL:`.

So every task must be verifiable **without live Facebook and without a human**: instead of live data we use pinned fixtures (Task 1) and synthetic dicts in tests. No automatable checkbox in this plan may need the network.

All `MANUAL:` items are duplicated in Post-Completion — that is where a human picks up the list of manual actions after an autonomous run.

## Testing Strategy

- **unit tests**: mandatory for `description_from_text_lines`, `clean_fb_title`, RU date parsing, `ai_schema` (loading/validating the registry, `source_hash`), `rules.decide`, `storage` (new tables, `fetch_enriched`), `decisions`, `ai_batch` (selecting pending, accepting results, rejecting unknown keys).
- **fixture-based tests**: the real `text_lines` of two listings, pinned in `tests/fixtures/` in Task 1 — the extractors are checked against them. This replaces integration tests with live FB.
- **integration tests**: NOT written for the Playwright scrapers and the CLI wrappers (on purpose, marked `DESIGN:`).
- **e2e**: not applicable, there is no UI — the HTML export is checked by a test for the presence of the expected CSS classes in the generated string.
- **manual verification**: the full cycle `scrape --refresh → ai-batch → analysis in a session → ai-apply → export` after Task 13.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

```
rent-assistant scrape --refresh        →  rentals.db (listings)
rent-assistant ai-batch                →  $RENT_ASSISTANT_HOME/ai/batch-<ts>.json   ← pending + field registry + requirements
     Claude Code (Opus, high effort) reads, thinks, writes:
                                          $RENT_ASSISTANT_HOME/ai/result-<ts>.json
rent-assistant ai-apply <file>         →  rentals.db (ai_analysis)
rent-assistant export                  →  $RENT_ASSISTANT_HOME/rentals.html
```

The whole cycle is started with one command through the `/analyze` skill.

Key decisions:

1. **The AI extracts facts, the code applies policy.** The AI never writes `status` and `reason` — it writes field values with a confidence and a quote as evidence. `rules.py` derives active/rejected deterministically. So auto-rejects are reproducible, explainable and change globally when the rules change — without re-analysis.
2. **Separation of persistence.** AI results live in SQLite (regenerable — can be wiped without fear). The user's decisions live in `$RENT_ASSISTANT_HOME/decisions.json` (not regenerable — neither scrape nor analysis touches them) and are the source of truth, mirrored into the DB on export. A user's decision always overrides the AI verdict.
3. **A field registry instead of migrations.** A new field = an entry in `config/ai_fields.json`. No ALTER TABLE: the values live in `data_json`.
4. **`added_in` versioning** — exactly what lets us tell "the field did not exist at analysis time" (a separate colour) from "the field existed, but the AI found no information" (an ordinary grey dash).
5. **`source_hash`** — the staleness detector. A re-scrape that improved a listing's text automatically puts it back into the analysis queue.
6. **Fixing the extractors goes first**, because the quality of the analysis depends entirely on the quality of the input text.

## Technical Details

### Field registry — `config/ai_fields.json`

```json
{
  "schema_version": 1,
  "fields": [
    { "key": "pets", "label": "Pets", "group": "core", "type": "enum",
      "values": ["allowed", "not_allowed", "unclear"],
      "highlight": "pets", "added_in": 1,
      "prompt": "Are pets/dogs allowed? 'allowed' ONLY on explicit confirmation (pets allowed, pet friendly, dogs ok, สัตว์เลี้ยงได้). 'not_allowed' ONLY on an explicit ban. In every other case 'unclear'. A quote in ev is mandatory." }
  ]
}
```

The v1 fields (all four groups), `added_in: 1` on every one:

| group | key | type | comment |
|---|---|---|---|
| core | `pets` | enum allowed/not_allowed/unclear | the main criterion |
| core | `pets_evidence` | text | the supporting quote |
| core | `price_month_thb` | int | the price the AI considers real |
| core | `price_12m_thb` | int | the price on a yearly contract, if the ad gives a rate table |
| core | `deposit` | text | "2 months deposit + 1 in advance" |
| core | `bedrooms` | int | |
| core | `bathrooms` | int | |
| core | `extra_rooms` | text | a study, a second living room, etc. |
| core | `living_room` | enum yes/no/unclear | no mention = `unclear`, NOT a reason to reject |
| core | `furnished` | enum full/partial/none/unclear | |
| property | `property_type` | enum house/townhouse/villa/condo/apartment/studio/unknown | |
| property | `project_name` | text | village / project name |
| property | `area` | text | normalised district |
| property | `size_sqm` | int | |
| property | `floors` | int | |
| property | `condition` | enum new/good/worn/unclear | |
| amenity | `kitchen` | enum full/kitchenette/none/unclear | |
| amenity | `parking` | enum yes/no/unclear | |
| amenity | `internet` | text | speed/limit, if given |
| amenity | `aircon` | text | count |
| amenity | `pool_gym` | text | |
| amenity | `green_area` | enum yes/no/unclear | for walking the dog |
| amenity | `security` | enum yes/no/unclear | guards/gatehouse |
| meta | `contract_min_months` | int | |
| meta | `available_from` | date | needed by the move-in date in the requirements letter |
| meta | `posted_at` | date | |
| meta | `poster_type` | enum agent/owner/unclear | |
| meta | `contact` | text | often `[hidden information]` |

Plus the service field `data_quality` (`good` / `sidebar_noise` / `missing`) — stored as a separate column, not in the registry.

### New tables

```sql
CREATE TABLE IF NOT EXISTS ai_analysis (
    fb_id          TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    analyzed_at    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,   -- staleness detector
    model          TEXT,            -- "claude-opus-5 (claude-code)"
    verdict        TEXT NOT NULL,   -- short free-text verdict
    score          INTEGER,         -- 0-100 match against the criteria
    data_quality   TEXT,            -- good | sidebar_noise | missing
    data_json      TEXT NOT NULL    -- {key: {"v":..., "conf":"high|med|low", "ev":"quote"}}
);

CREATE TABLE IF NOT EXISTS decisions (   -- mirror of $RENT_ASSISTANT_HOME/decisions.json
    fb_id      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,        -- rejected | active | shortlist
    reason     TEXT,
    decided_at TEXT NOT NULL
);
```

`source_hash` = sha256 over what the AI actually saw: `title | price_raw | district | description_clean | len(text_lines)`.

A listing counts as **pending** if: there is no row in `ai_analysis`, OR its `source_hash` differs, OR its `schema_version` is below the registry version raised with `--backfill`.

### Batch format — `$RENT_ASSISTANT_HOME/ai/batch-<ts>.json`

```json
{ "schema_version": 1,
  "generated_at": "2026-08-25T10:00:00Z",
  "requirements_md": "<contents of config/requirements.md>",
  "fields": [ ...the whole registry... ],
  "items": [
    { "fb_id": "...", "url": "...", "price_thb": 16000, "price_raw": "16 000 ฿/месяц",
      "district": "Kathu, Phuket, Thailand", "listing_title": "1 Bed 1 Bath - Apartment",
      "description": "<cleaned text>", "text_lines": [ "..." ],
      "source_hash": "abc123..." }
  ] }
```

(`price_raw` is the price string as Facebook renders it in the Russian locale; `฿/месяц` means "฿/month".)

### Result format — `$RENT_ASSISTANT_HOME/ai/result-<ts>.json`

```json
{ "schema_version": 1,
  "model": "claude-opus-5 (claude-code)",
  "results": [
    { "fb_id": "...", "source_hash": "abc123...",
      "verdict": "New studio condo near Kathu, has everything, but not a word about pets and it is not a house in a village.",
      "score": 62, "data_quality": "good",
      "fields": { "pets": {"v": "unclear", "conf": "high", "ev": ""},
                  "price_12m_thb": {"v": 16000, "conf": "high", "ev": "12 months: 16,000 THB/month"} } } ],
  "proposed_new_fields": [
    { "key": "electricity_rate", "label": "Electricity ฿/kWh", "type": "text",
      "why": "appears in 3 of 8 listings, spread 4–8 ฿/kWh — that is ±2000 THB/month",
      "examples": ["Electricity 7 THB/unit", "ไฟฟ้า 5 บาท/หน่วย"] } ] }
```

`ai-apply` **rejects with an error** any key in `fields` that is not in the registry, listing those keys. `proposed_new_fields` are collected and printed — this is the trigger for the conversation about extending the schema.

### Decision rules (`rules.py`)

Exactly three auto-rejects:

| Condition | Reason |
|---|---|
| `pets == "not_allowed"` | Pets forbidden |
| `price > 35 000` | Far over budget |
| an explicit studio (`property_type == "studio"` or `bedrooms == 0`) | Studio |

Everything else is a flag, not a reject. Explicitly:

- **More rooms than asked for is never a minus.** Extra rooms raise the score.
- **An unmentioned living room is not a reason to reject.** Most ads simply do not write the words "living room". It is a yellow flag.
- Condo/apartment, short-term only, no parking, weak internet, no green area → a flag + reflected in the verdict, the listing stays active.
- 30,001–35,000 THB → active, the price cell is orange.
- `data_quality != "good"` → **never a reject**, marked as "needs a re-scrape".

Colour classes (returned by `rules.cell_class`):

| State | Class / colour |
|---|---|
| `pets = allowed` | `c-good` — green |
| `pets = unclear` / not mentioned | `c-warn` — yellow |
| `pets = not_allowed` | `c-bad` — red + the row is auto-rejected |
| price ≤30k / 30–35k / >35k | plain / `c-warn` / `c-bad` |
| field empty, added **after** this row's analysis (`added_in > row.schema_version`) | `c-stale` — **a separate fourth colour** (blue-grey hatching) |
| field empty, but it existed at analysis time | `c-none` — an ordinary grey dash |
| row rejected | `row-rejected` — dimmed, struck through, red left border |

### CLI

```sh
rent-assistant ai-status                    # analyzed / pending / stale / no-raw-data
rent-assistant ai-batch [--limit 25] [--all] [--ids ID...] [--out PATH]
rent-assistant ai-apply PATH [--dry-run]
rent-assistant ai-field list
rent-assistant ai-field add --key K --label L --type enum --values a,b,c [--group G] [--after K] [--backfill]
rent-assistant reject ID --reason "..."     # → $RENT_ASSISTANT_HOME/decisions.json, overrides the AI
rent-assistant unreject ID
rent-assistant shortlist ID [--reason "..."]
rent-assistant export                       # same name as before, renders the full table
```

`--backfill` raises `schema_version` and marks the existing rows as stale so that `ai-batch` picks them up; without the flag the old rows simply get the `c-stale` colour. This flag is the answer to the question "re-analyse or leave as unknown", built right into the CLI.

### HTML

`export_html()` (`storage.py:207-341`) is rewritten; the existing techniques are kept: a self-contained file, inline `const DATA`, Grid.js from unpkg, escaping of `</` / `<!--`, the `esc()` helper.

- **All columns visible, horizontal scroll**, a sticky header and a sticky first column so the row is not lost when scrolling right.
- Filter tabs: **Active** (default) / Shortlist / Rejected / All with counters.
- Rejected rows carry their reason in a separate **Reason** column and are set off visually (dimmed + struck through + red border), with a note of who rejected: `AI` or `you`.
- The verdict is a truncated cell, full text on hover/expand. The evidence quotes (`ev`) are shown as cell tooltips so that any claim by the AI can be traced back to the source text.
- A colour legend at the top of the page covering all states from the table above.

Note: Grid.js is loaded from a CDN, the page needs the internet. This is existing behaviour; vendor it if needed, not in this iteration.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): all enrichment code, the registry, the rules, CLI, HTML, the skill, tests — everything done in this repo without the network.
- **Post-Completion** (no checkboxes): re-scraping live FB, the first real AI analysis run, a visual check of the HTML, plans for iteration 3.

## Implementation Steps

### Task 1: Real-data fixtures for the extractor tests

**Files:**
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/text_lines_good.json`
- Create: `tests/fixtures/text_lines_sidebar.json`
- Create: `tests/fixtures/README.md`

- [x] pull the `raw_json` of two real listings out of `$RENT_ASSISTANT_HOME/rentals.db` through `sqlite3`/python and save them as fixtures: the "good" one `fb_id=9000000000000001` (The Origin Kathu, a real description after the `Описание` (Description) marker) and the "bad" one `fb_id=9000000000000002` (the "Выбор дня" (Pick of the day) sidebar ended up in `description`, the real text is only in `text_lines`)
- [x] if these `fb_id`s are not in the DB — take any two: one with a non-empty `description` whose length is NOT 1288 and NOT 962 characters, the other with exactly such a length (that is the signature of the sidebar junk); record in `tests/fixtures/README.md` which ones were taken
- [x] if `$RENT_ASSISTANT_HOME/rentals.db` is absent — build the fixtures by hand from the structure samples in Technical Details: `text_lines` contains `"Описание"`, then the lines of the ad, then `"Информация о продавце"` (Seller information)
- [x] in `tests/fixtures/README.md` describe where the fixtures come from and that the data directory is gitignored, which is why the fixtures are committed
- [x] write `tests/test_fixtures.py`: both fixtures parse as JSON, contain the keys `title` and `text_lines`, `text_lines` is a non-empty list of strings
- [x] run `pytest tests/ -q` — all green

### Task 2: Extractors for the description, the title and the publication date

**Files:**
- Modify: `src/rent_assistant/extractors.py`
- Modify: `tests/test_extractors.py`

- [x] `description_from_text_lines(text_lines: list[str]) -> str | None` — cuts the slice between the marker `Описание`/`Description` and the marker `Информация о продавце`/`Seller information`; drops a trailing `Ещё`/`See more`; returns `None` if the marker is absent
- [x] `clean_fb_title(raw_title: str) -> str | None` — turns `"(6) Marketplace — 1 Bed 1 Bath - Apartment | Facebook"` into `"1 Bed 1 Bath - Apartment"`: strips the prefix `(N) Marketplace — ` (and `Marketplace | `) and the suffix ` | Facebook`; returns `None` for junk like `"Недавно опубликовано"` (Recently listed)
- [x] extend `_PUBLISHED_RE` with the Russian patterns `Опубликовано N минут/часов/дней/недель/месяцев назад` (Listed N minutes/hours/days/weeks/months ago) next to the existing English `... ago`; return an ISO date relative to the `now` passed in
- [x] write tests for `description_from_text_lines` on both fixtures from Task 1: on the "good" one return The Origin Kathu text, on the "bad" one — the real ad text, and NOT the "Выбор дня" sidebar
- [x] write tests for `clean_fb_title` (a normal title, a Thai title, `"Недавно опубликовано"` → None, an empty string) and for RU/EN date parsing (hours, days, weeks, an unknown format → None)
- [x] run `pytest tests/ -q` — all green, the existing 67 tests are not broken

### Task 3: Wiring the fixed extractors into the scrapers

**Files:**
- Modify: `src/rent_assistant/scraper_detail.py`
- Modify: `src/rent_assistant/scraper_list.py`

- [x] in `scraper_detail._extract_description()` (`scraper_detail.py:118`) try `description_from_text_lines(raw_json["text_lines"])` first; fall back to the old "longest text block" heuristic only when the marker is not found
- [x] in `scraper_detail` parse `published_at` through the updated extractor and store an ISO date instead of NULL — `_extract_published_at()` now returns an ISO string through `parse_published_at`, the local EN-only `_PUBLISHED_RE` is removed
- [x] in `scraper_list._pick_title()` use `clean_fb_title(raw_json["title"])`; if it returns `None` — the old logic "the first non-price line"
  - ⚠️ deviation from the wording: a list card has no `raw_json["title"]` (that is a detail-page field), so `clean_fb_title` is applied to every non-price line of the card; the first one that survives the filter becomes the title, otherwise — the old fallback "the first non-price line". The effect is the same: `"Недавно опубликовано"` no longer lands in `title` when the card has a real title
- [x] DESIGN: we deliberately write no unit tests for the Playwright modules (network + live FB = fragile); the behaviour is covered by the extractor tests on the fixtures in Task 2
- [x] run `pytest tests/ -q` — all green (131 passed)
- [x] MANUAL: `rent-assistant scrape --refresh` — reload the 21 existing rows with correct text (skipped — requires live FB / human action)

### Task 4: The field registry and the requirements file

**Files:**
- Create: `config/ai_fields.json`
- Create: `config/requirements.md`
- Create: `src/rent_assistant/ai_schema.py`
- Create: `tests/test_ai_schema.py`
- Modify: `src/rent_assistant/config.py`

- [x] create `config/ai_fields.json` with `schema_version: 1` and all fields from the table in Technical Details; each field has `key`, `label`, `group`, `type`, `values` (for enum), `added_in: 1`, `prompt` — a short instruction for the AI on what exactly to extract
- [x] create `config/requirements.md`: the text of the letter to the agent verbatim (two adults and a small dog, the move-in date in the requirements letter, a one-year contract, 15–30k THB, house/townhouse in a village, furnishing, 1 bedroom + living room, kitchen, green area, parking, internet) plus a priority checklist where pets are marked as criterion #1
- [x] `src/rent_assistant/ai_schema.py`: `load_registry(path) -> Registry` with validation (unique `key`, known `type`, non-empty `values` for enum, `added_in <= schema_version`), `field_keys()`, `schema_version`
- [x] `ai_schema.source_hash(listing: dict) -> str` — sha256 over `title | price_raw | district | description | len(text_lines)`, stable against key order
- [x] add to `config.py`: `AI_DIR = RENT_ASSISTANT_HOME / "ai"`, `AI_FIELDS_PATH`, `REQUIREMENTS_PATH`, `BUDGET_HARD_MAX_THB = 35_000`
- [x] write `tests/test_ai_schema.py`: the real `config/ai_fields.json` loads and is valid, a duplicate `key` → error, an enum without `values` → error, `source_hash` is deterministic and changes when the description changes
- [x] run `pytest tests/ -q` — all green

### Task 5: Decision rules and colour classes

**Files:**
- Create: `src/rent_assistant/rules.py`
- Create: `tests/test_rules.py`

- [x] `decide(fields: dict, price_thb: int | None) -> tuple[str, str | None]` — returns `("active"|"rejected", reason)`; exactly three reject conditions: `pets == "not_allowed"`, `price > BUDGET_HARD_MAX_THB`, an explicit studio (`property_type == "studio"` or `bedrooms == 0`)
- [x] `cell_class(field_key, value, field_def, row_schema_version) -> str` — implement the colour table: `c-good`/`c-warn`/`c-bad` for `pets`, the price thresholds, `c-stale` when `field_def["added_in"] > row_schema_version`, otherwise `c-none` for empty
- [x] `score(fields) -> int` — 0-100 match against the criteria in `requirements.md`; extra rooms and the presence of a green area/parking/security raise it, missing information is not penalised heavily
- [x] write tests for each reject condition and, as separate explicit cases, for what is **NOT** rejected: 3 bedrooms (more than asked) → active; `living_room` = `unclear` → active; `property_type` = `condo` → active; price 33,000 → active with `c-warn`; `data_quality` = `sidebar_noise` → active
- [x] write tests for `cell_class`: `pets=allowed` → `c-good`, `pets=unclear` → `c-warn`, a field with `added_in=2` on a row with `schema_version=1` → `c-stale`, a field with `added_in=1` and no value → `c-none`
- [x] run `pytest tests/ -q` — all green

### Task 6: Storage layer for AI results

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] in `init_db()` add `CREATE TABLE IF NOT EXISTS ai_analysis` and `decisions` per the schema in Technical Details, idempotently; the existing `listings` table is not touched
- [x] `upsert_ai_analysis(conn, row: dict) -> str` — insert/update by `fb_id`, returns `"inserted"`/`"updated"`
- [x] `fetch_pending(conn, registry) -> PendingListings` (a NamedTuple `pending` / `no_raw`, unpacks as a tuple) — listings with no row in `ai_analysis`, or with a different `source_hash`, or with a `schema_version` below the current one; exclude listings with no `description` and no `raw_json` and return them as a separate "no raw data" list (no description AND no `text_lines` in `raw_json`); every pending item carries a fresh `source_hash` and `pending_reason` = `new`/`changed`/`schema`
- [x] `fetch_enriched(conn) -> list[dict]` — JOIN `listings` + `ai_analysis` + `decisions`, with `data_json` unpacked; the user's decision overrides the AI verdict
  - ⚠️ the `ai_analysis` schema in Technical Details has no columns for the auto-reject status/reason, so `ai_status`/`ai_reason` are computed on read through `rules.decide` — the rules change without re-analysis
- [x] write tests on in-memory SQLite: `init_db` creates three tables and a repeated call does not fail, upsert stores and updates, `fetch_pending` correctly classes a listing as pending under each of the three conditions and filters out those with no raw data, `fetch_enriched` returns the user's decision on top of the AI's
- [x] run `pytest tests/ -q` — all green (201 passed)

### Task 7: The user's decisions outside the DB

**Files:**
- Create: `src/rent_assistant/decisions.py`
- Create: `tests/test_decisions.py`

- [x] `load_decisions(path) -> dict[str, dict]` / `save_decision(path, fb_id, status, reason)` — work with `$RENT_ASSISTANT_HOME/decisions.json`, an atomic write through a temp file + rename, a missing file = an empty dict
- [x] `sync_to_db(conn, decisions)` — loads the file's contents into the `decisions` table (the file is the source of truth, the table is a mirror for the JOIN)
- [x] validation of `status` — only `rejected` / `active` / `shortlist`, otherwise `ValueError`
- [x] write tests: write and read round trip, overwriting an existing decision, an invalid status, a missing file, `sync_to_db` fills the table
- [x] write the "survives a DB wipe" test: save a decision, recreate the DB through `init_db`, call `sync_to_db` — the decision and its reason are in place
- [x] run `pytest tests/ -q` — all green

### Task 8: Batch building and result intake

**Files:**
- Create: `src/rent_assistant/ai_batch.py`
- Create: `tests/test_ai_batch.py`

- [x] `build_batch(conn, registry, requirements_md, limit, ids=None) -> dict` — builds the JSON batch in the format from Technical Details; puts `description` into each item (through `description_from_text_lines` when the column is corrupted) and `text_lines`, plus `source_hash`
- [x] `write_batch(batch, out_dir) -> Path` — writes `$RENT_ASSISTANT_HOME/ai/batch-<ts>.json`, creates the directory if needed
- [x] `apply_results(conn, registry, result: dict) -> ApplyReport` — validates `schema_version`, rejects with an error any key in `fields` not in the registry (listing them), computes `decide`/`score` through `rules`, calls `upsert_ai_analysis`; returns counters and the list of `proposed_new_fields`
- [x] when the `source_hash` in the result differs from the current one in the DB — a warning in the report, the record is still accepted (the data may have changed between batch and apply)
- [x] write tests: the batch contains only pending items and no more than `limit`, a corrupted `description` is replaced by text from `text_lines`, `apply_results` puts a row into `ai_analysis`, an unknown field key → an error with the list of keys, `proposed_new_fields` are passed through to the report, a `source_hash` mismatch gives a warning but not an error
- [x] run `pytest tests/ -q` — all green

### Task 9: CLI commands

**Files:**
- Modify: `src/rent_assistant/cli.py`
- Create: `tests/test_cli_ai_field.py`

- [x] `rent-assistant ai-status` — prints analyzed / pending / stale / "no raw data, needs scrape --refresh" with counters
- [x] `rent-assistant ai-batch [--limit 25] [--all] [--ids ...] [--out PATH]` — builds and writes the batch, prints the path and the count; with zero pending says so and exits with code 0
- [x] `rent-assistant ai-apply PATH [--dry-run]` — applies the result, prints a report: how many accepted, how many auto-rejected and for what reason, which new fields are proposed
- [x] `rent-assistant ai-field list` and `rent-assistant ai-field add --key --label --type [--values] [--group] [--after] [--backfill]` — adds a field to `config/ai_fields.json`; with `--backfill` raises `schema_version` and marks the existing rows as stale, without it the old rows simply get `c-stale`
  - ⚠️ a clarification of the mechanics: `schema_version` is raised on **every** field addition (otherwise the new field's `added_in` cannot exceed the row's version and `c-stale` is unreachable — registry validation requires `added_in <= schema_version`). The two cases are separated by a new optional registry key `analysis_version`: it catches up with `schema_version` only under `--backfill`, and it is what decides whether already analysed rows go back into the queue. Without `--backfill` they count as `stale` (a separate counter in `ai-status`, excluded from the batch), with `--backfill` — `pending` again. `ai_schema` ignores the extra key, no change to the storage layer was needed
  - ⚠️ `ai-field list/add` have a `--registry PATH` option (default `config/ai_fields.json`) — otherwise the tests would mutate the real registry
- [x] `rent-assistant reject ID --reason "..."`, `rent-assistant unreject ID`, `rent-assistant shortlist ID [--reason]` — write to `$RENT_ASSISTANT_HOME/decisions.json` through `decisions.py`
- [x] DESIGN: no tests for the thin CLI wrappers — the logic is covered by the unit tests of the lower layers; the exception is `ai-field add`, where there is a real mutation of the registry
- [x] write `tests/test_cli_ai_field.py`: `ai-field add` on a copy of the registry adds a field with the correct `added_in`, `--after` puts the field at the right position, a duplicate key → error, `--backfill` raises `schema_version`
- [x] run `pytest tests/ -q` and `rent-assistant --help` — the new commands are visible

### Task 10: The rewritten HTML export

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] rewrite `export_html()`: builds the rows through `fetch_enriched`, takes each cell's class from `rules.cell_class`, inlines the data into `const DATA` with the same escaping of `</` and `<!--` and the `esc()` helper
- [x] all columns visible with horizontal scroll, a sticky header and a sticky first column
- [x] filter tabs Active (default) / Shortlist / Rejected / All with counters; the **Reason** column is always present; rejected rows get `row-rejected` (dimmed, struck through, red left border) and a note of who rejected — `AI` or `you`
- [x] the verdict is a truncated cell with full text on hover; the `ev` quotes are shown as `title` tooltips on the cells
- [x] a colour legend at the top of the page covering `c-good` / `c-warn` / `c-bad` / `c-stale` / `c-none` / `row-rejected`
- [x] write a test on a synthetic set of three rows (pets allowed / unclear / not_allowed): the generated HTML contains `c-good`, `c-warn`, `c-bad`, `row-rejected`, the reject reason text and all column headers from the registry
- [x] run `pytest tests/ -q` — all green
  - ⚠️ Grid.js dropped: the markup is now our own (`<table class="rt">` + ~120 lines of JS for sorting/search/tabs). Reason: Grid.js gives no stable hook for a row class — `row-rejected` would have to be attached after the fact through the DOM, and a sticky first column over 37 columns means fighting its DOM. A side effect — the page no longer needs the internet (the CDN is gone). Kept: the self-contained file, inline `const DATA`, escaping of `</` / `<!--`, the `esc()` helper
  - ⚠️ a row with no analysis at all gets `c-none`, not `c-stale`: hatching means "the field appeared after this row's analysis", not "nobody has looked at the row"

### Task 11: The `/analyze` skill

**Files:**
- Create: `.claude/skills/analyze/SKILL.md`

- [x] frontmatter with `name: analyze` and a trigger description ("analyze the unprocessed listings")
- [x] steps 1–3: `rent-assistant ai-status`; if there are listings with no raw data — tell the user to run `rent-assistant scrape --refresh` and stop; otherwise `rent-assistant ai-batch --limit 25` and read the batch together with `config/requirements.md`
- [x] steps 4–6 (extraction rules): build the facts from `text_lines` (the slice after `Описание`), not from the possibly corrupted `description` column; set `data_quality`; invent nothing — absence of evidence means `unclear`; on pets — `allowed` only on explicit confirmation, `not_allowed` only on an explicit ban, always with a quote in `ev`; extra rooms are a plus, not a minus; an unmentioned living room is not a defect; write a short verdict on each listing taking into account everything known, including what has no column yet
- [x] step 7 (schema evolution): if the same useful signal appears in ≥2 listings and there is no field for it — add it to `proposed_new_fields`
- [x] steps 8–10: `rent-assistant ai-apply <result>`; if fields are proposed — ask the user two questions through `AskUserQuestion`: (a) does this really matter, (b) re-analyse the old listings or leave them as `unknown` in a separate colour; then `rent-assistant ai-field add [--backfill]` and return to step 2 on backfill; at the end `rent-assistant export` and a report: counters, auto-rejects with reasons, the top 5 by score
- [x] DESIGN: no automated tests for the prompt file — it is checked by a real run in Post-Completion
- [x] run `pytest tests/ -q` — all green

### Task 12: Verify acceptance criteria

**Automatable** (the agent does it):
- [x] `pytest tests/ -q` — the whole suite is green, including the 67 tests of iteration 1 (248 tests pass; the iteration-1 suite on `main` is 76 tests, not 67 — all still green)
- [x] `rent-assistant --help` shows `ai-status`, `ai-batch`, `ai-apply`, `ai-field`, `reject`, `unreject`, `shortlist`
- [x] `python -c "from rent_assistant.ai_schema import load_registry; r=load_registry(); print(r.schema_version, len(r.fields))"` — the registry loads, the fields are in place
- [x] idempotence: on a temporary DB build a batch of 3 listings, apply a synthetic result, call `fetch_pending` again — these 3 do not come back; change the `description` of one of them — exactly that one comes back
- [x] schema evolution: `ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear` without `--backfill` → in the HTML the old rows get `c-stale`, not `c-none`; repeat with `--backfill` → the rows are pending again
- [x] decisions survive a DB wipe: `reject` on temporary data, delete the DB file, `init_db` + `sync_to_db` — the decision and the reason are in place
- [x] `rent-assistant export` on a DB with not a single `ai_analysis` row — valid HTML without a crash

**Requires manual action** (the agent marks as skipped):
- [x] MANUAL: `rent-assistant scrape --refresh` — reload the listings with the fixed extractors (skipped — requires live FB / human action)
- [x] MANUAL: check in sqlite3 that no `description` equals the sidebar boilerplate of 1288/962 characters, `title` is nowhere `"Недавно опубликовано"` any more, `published_at` is not NULL where FB showed a date (skipped — requires live FB / human action)
- [x] MANUAL: the full `/analyze` cycle in an interactive Claude Code session (Opus, high effort) on 3–5 listings (skipped — requires interactive AI session)
- [x] MANUAL: open `$RENT_ASSISTANT_HOME/rentals.html` — the Pets column is green/yellow/red as designed, rejected rows are visually distinct and carry reasons, all columns are reachable by horizontal scroll, the legend matches reality (skipped — requires human review)
- [x] MANUAL: check on real data that no listing is rejected for extra rooms and none for an unmentioned living room (skipped — requires live data)

### Task 13: [Final] Update documentation

**Files:**
- Modify: `README.md`

- [x] add a section on AI enrichment to `README.md`: the cycle `ai-batch → analysis → ai-apply → export`, a description of all new commands with examples, where `config/ai_fields.json`, `config/requirements.md`, `$RENT_ASSISTANT_HOME/decisions.json` and `$RENT_ASSISTANT_HOME/ai/` live
- [x] describe how to extend the schema (`ai-field add`, the meaning of `--backfill`, what the `c-stale` colour means) and how to reject listings by hand
- [x] note that this is iteration 2 and link to this plan file
- [x] move the plan: `mkdir -p docs/plans/completed && mv docs/plans/20260825-ai-enrichment.md docs/plans/completed/`

## Post-Completion

*Requires manual action or belongs to future iterations.*

**What has to be done by hand after the autonomous run** (collected from all `MANUAL:` items above):

1. `rent-assistant scrape --refresh` — reload the 21 existing rows with the fixed extractors. Without this step there is nothing to analyse: 10 rows have no description at all, in most of the rest the sidebar ended up there.
2. Check the data quality in sqlite3: there must be no sidebar boilerplate (1288 / 962 characters), `title` must not be `"Недавно опубликовано"`, `published_at` must not be NULL everywhere.
3. Run `/analyze` in an interactive Claude Code session (Opus, high thinking effort) on 3–5 listings first — make sure the extraction is adequate and the `ev` quotes really support the values.
4. Open `$RENT_ASSISTANT_HOME/rentals.html`, check the colours, the tabs, the legend and the horizontal scroll.
5. Make sure on live data that the rules do not cut too much: no rejects for more rooms than asked and for an unmentioned living room.
6. Run `/analyze` on the full volume, answer the questions about new fields if the AI proposes any.

**Known limitations of this iteration** (fixed in the next ones):

- `--refresh` re-downloads the detail pages, which is slow (3–7 seconds per listing, cap 60 per run) — on large volumes it has to be run in several passes.
- The analysis is not automated end-to-end: it is an interactive cycle in a Claude Code session, not a cron job. An autonomous run is impossible while `AskUserQuestion` is in the loop.
- `score` is a heuristic over the criteria in `requirements.md`, not a calibrated model; it serves for sorting, not for decisions.

**Next iterations** (out of scope, for context):

1. Scraping Phuket rental FB groups — a second source into the same DB.
2. The Hermes scheduler: a schedule + diff notifications about new listings, with the analysis run only over the fresh ones.
3. A non-interactive mode for `/analyze` (new fields accumulate in a proposal queue instead of a question on the spot) — a prerequisite for scheduled automation.
4. Monitoring of FB session expiry + automatic retry with an alert.
