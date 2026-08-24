# AI enrichment layer for fb-rent

## Context

`scrapper/` already scrapes Facebook Marketplace rentals into `data/rentals.db` (21 rows today) and exports a Grid.js HTML table. Everything extracted so far comes from regex heuristics; `extractors.py` itself says AI enrichment is a later iteration, and `raw_json` was deliberately stored "for future AI".

That iteration is this one. We need a tool that can be pointed at listings the AI has not processed yet, pull the meaningful facts out of the description and the rest of the page text, fill in table columns, and write a short verdict per listing — so a house hunt for a Phuket move on 21 Sep 2026 can be run off one HTML page instead of 40 Facebook tabs.

Three things shape the design:

1. **The AI is Claude Code (Opus, high thinking effort)** — not an API client. So the "tool" is a batch-out / results-in contract plus a skill that drives the loop, not an SDK integration. No new runtime dependency.
2. **The schema must evolve.** When the AI notices a recurring useful signal with no column for it, it asks whether it matters, and whether to re-analyse older listings or leave them `unknown` in a distinct colour.
3. **The scraped text is currently unreliable.** 10 of 21 rows have no `description` at all; most of the remaining 11 captured Facebook's "Выбор дня" sidebar instead of the listing. `title` is `"Недавно опубликовано"` on 100% of rows and `published_at` is NULL on 100%. Feeding that to the AI would produce confident nonsense, so it gets fixed first.

Nothing here is precious: parsing and analysis can be re-run from scratch at any time. Only the user's own accept/reject decisions are irreplaceable, so those live outside the DB.

## Criteria being encoded

Couple + a small small dog, moving 21 Sep 2026, one-year contract, 15,000–30,000 THB/mo. House or townhouse in a secure village (Pruksa Ville-like), fully furnished, ≥1 bedroom + living room, kitchen that isn't tiny, green area nearby for walking, parking, good internet. Not tourist/beach dependent.

**Pets allowed is the single most important criterion.**

These live in a new editable `scrapper/config/requirements.md` (the estate-agent message verbatim + a criteria checklist), which is pasted into every batch. Changing priorities later means editing one markdown file, not the code.

## Architecture

```
fb-rent scrape --refresh        →  rentals.db (listings)
fb-rent ai-batch                →  data/ai/batch-<ts>.json   ← pending items + field registry + requirements
   Claude Code (Opus, high effort) reads it, thinks, writes:
                                   data/ai/result-<ts>.json
fb-rent ai-apply <file>         →  rentals.db (ai_analysis)
fb-rent export                  →  data/rentals.html
```

Driven end-to-end by a `/analyze` skill so the whole loop is one command in a session.

**Key split: the AI extracts facts, code applies policy.** The AI never writes `status` or `reason`. It writes field values with a confidence and an evidence quote; `rules.py` derives active/rejected deterministically. Auto-rejections are therefore reproducible, explainable, and change globally when the rules change — without re-running any analysis.

**Persistence split:** AI results live in SQLite (regenerable — wipe freely). User decisions live in `data/decisions.json` (not regenerable — never touched by scrape or analysis) and are the source of truth, mirrored into the DB at export time. A user decision always overrides the AI's.

## Data model

**`scrapper/config/ai_fields.json`** — git-tracked field registry, the thing that evolves:

```json
{ "schema_version": 1,
  "fields": [
    { "key": "pets", "label": "Pets", "group": "core", "type": "enum",
      "values": ["allowed", "not_allowed", "unclear"],
      "highlight": "pets", "added_in": 1,
      "prompt": "Are pets/dogs allowed? 'allowed' ONLY on explicit evidence..." }
  ] }
```

`added_in` is what makes the colour rule work: a blank cell on a row analysed *before* the field existed renders as stale-unknown (distinct colour), while a blank cell on a row analysed *after* renders as genuine-unknown. Adding a field is an edit to this file — no migration, no ALTER TABLE.

**Two new tables** in `storage.init_db()`, both `CREATE TABLE IF NOT EXISTS` alongside the existing `listings`:

```sql
CREATE TABLE ai_analysis (
    fb_id          TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    analyzed_at    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,   -- staleness detector
    model          TEXT,            -- "claude-opus-5 (claude-code)"
    verdict        TEXT NOT NULL,   -- short free-text verdict
    score          INTEGER,         -- 0-100 fit
    data_quality   TEXT,            -- good | sidebar_noise | missing
    data_json      TEXT NOT NULL    -- {key: {v, conf, ev}}
);
CREATE TABLE decisions (            -- mirror of data/decisions.json
    fb_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL
);
```

`source_hash` = sha256 over the inputs the AI actually saw (title, price_raw, district, cleaned description, text_lines length). A re-scrape that improves a listing changes the hash, which automatically marks it pending again.

An item is **pending** when: no `ai_analysis` row, or its `source_hash` differs, or its `schema_version` is below a registry version that was bumped with `backfill: true`.

## Registry v1 fields

All four groups the user selected. `pets` first because it decides everything.

- **Decision (structural, not in registry):** status, reason, verdict, score, price, link
- **Core:** `pets` (+`pets_evidence`), `price_month_thb`, `price_12m_thb` *(long-contract price — real listings quote tiers like "1–2 mo: 18,000 / 12 mo: 16,000"; the 12-month figure is the one that matters)*, `deposit`, `bedrooms`, `bathrooms`, `extra_rooms`, `living_room`, `furnished`
- **Property:** `property_type`, `project_name` (village/condo project), `area`, `size_sqm`, `floors`, `condition`
- **Amenities:** `kitchen`, `parking`, `internet`, `aircon`, `pool_gym`, `green_area`, `security`
- **Meta:** `contract_min_months`, `available_from`, `posted_at`, `poster_type`, `contact`

Plus `data_quality`, which flags rows whose text is sidebar noise or missing — these are *never* rejected, just marked "needs re-scrape".

## Decision rules (`rules.py`)

Auto-reject, exactly three:

| Condition | Reason |
|---|---|
| `pets == "not_allowed"` | Pets not allowed |
| `price > 35,000` | Over budget |
| explicit studio (`property_type == "studio"` or `bedrooms == 0`) | Studio |

Everything else is flagged, never rejected. Specifically:
- **More rooms than asked for is never a negative** — extra rooms raise the score.
- **A missing/unmentioned living room is not a reject** — most listings don't use the phrase. It's a yellow flag.
- Condo/apartment, short-term-only, no parking, weak internet, no green area → flagged and reflected in the verdict, still active.
- 30,001–35,000 THB → active, price cell flagged orange.

Cell colouring:

| State | Colour |
|---|---|
| `pets = allowed` | green |
| `pets = unclear` / not mentioned | yellow |
| `pets = not_allowed` | red + row auto-rejected |
| price ≤30k / 30–35k / >35k | normal / orange / red |
| field blank, added after this row was analysed | **stale-unknown — distinct 4th colour** (hatched blue-grey) |
| field blank, but the field existed at analysis time | plain grey "—" |
| rejected row | dimmed, struck-through, red left border |

## Files

**New**

| Path | Purpose |
|---|---|
| `scrapper/config/ai_fields.json` | field registry |
| `scrapper/config/requirements.md` | the search criteria + agent message |
| `src/fb_rent/ai_schema.py` | load/validate registry, `schema_version`, `source_hash()` |
| `src/fb_rent/ai_batch.py` | select pending, build batch, validate + apply results |
| `src/fb_rent/decisions.py` | read/write `data/decisions.json`, sync into DB |
| `src/fb_rent/rules.py` | auto-reject + highlight logic |
| `scrapper/.claude/skills/analyze/SKILL.md` | the `/analyze` orchestration prompt |
| `tests/test_ai_schema.py`, `test_ai_batch.py`, `test_rules.py`, `test_decisions.py` | |

**Modified**

- `src/fb_rent/extractors.py` — add `description_from_text_lines()` (slice between the `Описание`/`Description` marker and the `Информация о продавце`/seller marker — this is the reliable text and the fix for the sidebar bug), `clean_fb_title()` (strip `(N) Marketplace — ` / ` | Facebook`), and RU patterns in `_PUBLISHED_RE` (`Опубликовано N часов/дней/недель назад`) alongside the existing English ones. Reuse the existing pure-function + unit-test style.
- `src/fb_rent/scraper_detail.py:118` — `_extract_description()` calls the new extractor instead of the longest-text-block heuristic; fall back to the old behaviour only if no marker is found.
- `src/fb_rent/scraper_list.py` — `_pick_title()` uses `clean_fb_title(raw_json["title"])` instead of the first non-price line.
- `src/fb_rent/storage.py` — new tables in `init_db()`; a `fetch_enriched()` join over listings + ai_analysis + decisions; rewritten `export_html()`; CSV export gains the AI columns.
- `src/fb_rent/cli.py` — new commands, same click patterns as `scrape`/`export`.
- `src/fb_rent/config.py` — `AI_DIR`, `BUDGET_HARD_MAX_THB = 35_000`, `AI_FIELDS_PATH`, `REQUIREMENTS_PATH`.

## CLI

```sh
fb-rent ai-status                    # analyzed / pending / stale / missing-raw-data counts
fb-rent ai-batch [--limit 25] [--all] [--ids ...] [--out PATH]
fb-rent ai-apply PATH [--dry-run]    # validates against registry; unknown keys = error
fb-rent ai-field list
fb-rent ai-field add --key K --label L --type enum --values a,b,c [--group G] [--after K] [--backfill]
fb-rent reject ID --reason "..."     # → data/decisions.json, overrides the AI
fb-rent unreject ID
fb-rent shortlist ID [--reason "..."]
fb-rent export                       # unchanged name, now renders the full table
```

`--backfill` bumps `schema_version` and marks existing rows stale so `ai-batch` picks them up; without it, old rows keep the stale-unknown colour. That flag *is* the "re-analyse or leave unknown" answer, wired straight into the CLI.

## HTML output

Rewrite `export_html()` (`storage.py:207-341`). Keep the existing Grid.js-from-unpkg approach and the self-contained single-file + inlined `const DATA` pattern, including the current `</`/`<!--` escaping and `esc()` helper.

- **All columns visible, horizontal scroll** (as chosen), with sticky header and a sticky first column so a row stays identifiable while scrolling right.
- Filter tabs: **Active** (default) / Shortlist / Rejected / All, with counts.
- Rejected rows carry their **Reason** in a dedicated column and the dimmed + struck + red-border treatment, so they're unmistakable — including which side rejected them (`AI` vs `you`).
- Verdict shown as a truncated cell with the full text on hover/expand; evidence quotes (`ev`) surface as cell tooltips, so any AI claim is traceable to the source text.
- A colour legend at the top of the page covering all five states above.

Note: Grid.js loads from a CDN, so the page needs internet to render. That's the existing behaviour; worth vendoring later if it becomes annoying.

## The `/analyze` skill

`scrapper/.claude/skills/analyze/SKILL.md` — the "point the AI at unprocessed items" entry point:

1. `fb-rent ai-status`. If listings lack raw data, tell the user to run `fb-rent scrape --refresh` first and stop.
2. `fb-rent ai-batch --limit 25`; read the batch file and `config/requirements.md`.
3. Per item, think hard. Build facts from `text_lines` (post-`Описание` slice), not from the possibly-poisoned `description` column. Set `data_quality`. Never invent — absent evidence means `unclear`.
4. **Pets:** `allowed` only on explicit evidence (pets / dogs / pet friendly / สัตว์เลี้ยง); `not_allowed` only on explicit refusal; otherwise `unclear`. Always record the quote in `ev`.
5. Extra rooms are a plus, never a minus. A missing living-room mention is not a defect.
6. Write a short verdict per item weighing everything known, including things with no column yet.
7. When the same useful signal shows up in ≥2 listings with no field for it, add it to `proposed_new_fields` in the result file.
8. `fb-rent ai-apply <result>`.
9. If fields were proposed → **AskUserQuestion**: (a) is this actually important? (b) re-analyse older listings, or leave them `unknown` in the distinct colour? Then `fb-rent ai-field add [--backfill]`, and loop back to step 2 if backfilling.
10. `fb-rent export`; report counts, auto-rejections with reasons, and the top 5 by score.

## Order of work

1. Extractor fixes + tests (description / title / RU dates) — everything downstream depends on clean text.
2. `fb-rent scrape --refresh` to repopulate the 21 rows with correct text.
3. Registry, `requirements.md`, `ai_schema.py`, `rules.py` + tests.
4. Storage tables, `decisions.py`, CLI commands + tests.
5. `ai_batch.py` + tests.
6. HTML rewrite.
7. `SKILL.md`, then a real end-to-end run.

## Verification

```sh
cd ~/AI/fb-rent/scrapper && source .venv/bin/activate
python -m pytest tests -q                     # existing 67+ tests must stay green
```

- **Extractor fix:** re-run `fb-rent scrape --refresh`, then confirm in sqlite3 that no `description` is the 1288/962-char sidebar boilerplate, `title` is no longer `"Недавно опубликовано"` on any row, and `published_at` is non-NULL where FB showed a date.
- **Round trip:** `fb-rent ai-batch --limit 3` → analyse by hand in-session → `fb-rent ai-apply --dry-run` → apply → `fb-rent ai-status` shows 3 analyzed, 18 pending.
- **Idempotence:** re-run `ai-batch` — the 3 done items must not reappear. Touch a description in the DB, re-run — that one item alone comes back.
- **Rules:** unit tests for each auto-reject; explicitly assert that a 3-bedroom listing is *not* rejected for having more rooms, and that a listing with no living-room mention stays active.
- **Decisions survive a wipe:** `fb-rent reject <id> --reason test`, delete `rentals.db`, re-scrape, re-export — the rejection and its reason are still there.
- **Schema evolution:** `fb-rent ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear` (no `--backfill`) → export → old rows show the stale-unknown colour, not plain grey. Re-run with `--backfill` → they return to `ai-status` pending.
- **HTML:** open `data/rentals.html` — pets column green/yellow/red as specified, rejected rows visually distinct with reasons, all columns reachable by horizontal scroll, legend matches reality.
