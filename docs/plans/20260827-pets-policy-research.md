# Pets policy research — a third pass keyed by building

## Overview

Facebook ads lie by omission about pets. The text pass can only report what the
ad says, so `pets` is `unclear` on most rows and the user has to ask every agent
the same question. But the answer usually is not the landlord's to give: a condo
has a house rule, published on the management's own site or repeated in agency
listings and reviews, and it applies to every unit in the building.

This adds a third, occasional pass that researches that rule **once per
building** and reuses it across every listing in that building — present and
future. It never runs as part of the pipeline; it is a skill the user invokes.

What it changes in the main flow:

- a confirmed `not_allowed` building auto-rejects its listings with a new
  reason, «Кондо запрещает животных», independent of the ad's own `pets` field;
- `negotiable` — the case the user explicitly wants kept — is a first-class
  value, never a rejection, and shows on the row so it can be asked about;
- the evidence (source URLs and verbatim quotes) is stored and reachable from
  the table, so the rejection can be checked rather than trusted.

Two decisions shape everything below:

1. **The store is keyed by building, not by listing.** A condo's rule is a
   property of the condo. Researching it per listing would pay for the same
   search five times and would leave a newly scraped unit in an
   already-researched building unanswered.
2. **Research proposes, the user confirms.** Nothing auto-rejects on the
   strength of a web search alone. A researched building lands as `proposed`
   and is inert; only a policy the user confirmed can reject. This is one
   approval covering N listings, so the cost is small — and it matches how
   `decisions.json` already treats the user's word as the thing the code may
   not invent.

## Context (from discovery)

Files and facts the plan builds on:

- `src/fb_rent/rules.py` — `decide()` already auto-rejects on
  `pets == "not_allowed"` (`REASON_PETS`), and both `decide` and `score` are
  recomputed **on read** in `fetch_enriched`. A new rejection reason therefore
  reaches every existing row without re-running any AI pass.
- `src/fb_rent/storage.py:1238` `fetch_enriched` — the single funnel every
  export and every skill reads through. This is where the building policy has
  to be joined in.
- `config/ai_fields.json` — already carries **`project_name`** ("Проект/посёлок")
  and `area`. Those two are the research key; no new AI field is needed.
- `src/fb_rent/decisions.py` — the pattern to copy for the store: a plain JSON
  file under `FB_RENT_HOME` that `init_db` cannot wipe, mirrored into a SQLite
  table only so `fetch_enriched` can JOIN it. Confirmed policies and the
  research behind them are exactly as unreproducible as a user verdict.
- `src/fb_rent/photo_batch.py` + `.claude/skills/photo-analyze/` — the shape of
  an occasional out-of-pipeline pass: `*-batch` writes a JSON file, the skill
  fans out over subagents, `*-apply` reads a result file back and validates it
  hard before writing anything.
- `src/fb_rent/cli.py` — Click group, `@main.command("name")`; 1676 lines, all
  commands in one module.
- Tests: pytest, `testpaths = ["tests"]`, one `test_<module>.py` per module.

## Development Approach

- **testing approach**: Regular (code first, then tests)
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in
  that task
  - write unit tests for new functions/methods
  - write unit tests for modified functions/methods
  - add new test cases for new code paths
  - tests cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change
- maintain backward compatibility: a DB with no `buildings` table, and a run
  with no `buildings.json`, must behave exactly as today

**Commit as you go.** Per `CLAUDE.md`: straight into `main`, one commit per
coherent block, Russian message with a conventional-commit prefix, tests green
first. Tasks 1–8 are each a commit.

## Testing Strategy

- **unit tests**: required for every task (see above)
- **e2e tests**: none — this project has no UI test harness. The export is
  covered by asserting on the generated HTML string, as `test_storage.py`
  already does.
- run with `pytest` from the repo root

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope

## Solution Overview

```
fb-rent buildings-batch          →  buildings-batch-<ts>.json   (what to research)
        ↓  skill: one subagent per building, WebSearch + WebFetch
        ↓  writes buildings-result-<ts>.json
fb-rent buildings-apply <file>   →  data/buildings.json, status: "proposed"   (inert)
        ↓  skill: AskUserQuestion per proposal, showing quotes + affected rows
fb-rent buildings set <key> --policy not_allowed
                                 →  data/buildings.json, status: "confirmed"  (live)
        ↓
fetch_enriched  →  rules.decide(..., building_policy=...)  →  «Кондо запрещает животных»
```

Key design decisions and why:

**A JSON file is the source of truth, the table is a mirror.** Same argument as
`decisions.py`: a scrape can be repeated and an AI pass re-run, but a web search
the user then vetted cannot be reconstructed by any command. `data/buildings.json`
survives `init_db`; `buildings` exists so `fetch_enriched` stays one query.

**The building key is normalised, with explicit aliases.** Ads spell the same
condo four ways. `building_key()` lowercases, keeps alphanumerics, collapses
whitespace and joins with hyphens. That is deliberately dumb — it will not merge
"The Base" and "Base Central" on its own. An entry carries an `aliases` list so a
second spelling can be pointed at an existing building by hand, which is the
honest fix; guessing at fuzzy matches would silently apply one building's
rejection to another building's listings.

**The policy is its own verdict, not a rewrite of `pets`.** Four values:
`not_allowed | negotiable | allowed | unknown`. The ad's own `pets` field is
never touched. Two independent sources, either of which can reject, and a
disagreement between them stays visible instead of being resolved silently — an
ad that says "pet friendly" in a building that forbids dogs is a fact worth
seeing, not a conflict to average away.

**`negotiable` keeps the listing, always.** The user's stated rule. It is also
why the vocabulary cannot be the existing three-value `pets` enum, which has
nowhere to put it.

**No score weight, in either direction.** `CLAUDE.md`: *"a tie at the top is how
`rules.score` fails"* — 352 of 1123 rows once sat at exactly 100 because small
positive weights stacked. A confirmed `allowed` is tempting to reward and must
not be: it would lift precisely the rows already easiest to rank, and it would
rank a researched building above an unresearched identical one for a reason that
is about the research, not the flat. `not_allowed` needs no weight either — it
rejects outright. This is a deliberate omission; record it in `CLAUDE.md`.

**`proposed` is inert.** `decide()` reads the policy only when
`status == "confirmed"`. A research run therefore cannot change a single row's
status, which is what makes it safe to run the skill unattended and confirm
later.

## Technical Details

### `data/buildings.json`

Flat map keyed by the normalised building key:

```json
{
  "the-base-central-phuket": {
    "name": "The Base Central Phuket",
    "aliases": ["base central", "the base condo phuket"],
    "area": "Phuket Town",
    "policy": "not_allowed",
    "status": "confirmed",
    "confidence": "high",
    "note": "Управляющая компания: no pets of any kind. Агентства подтверждают.",
    "sources": [
      {"url": "https://...", "title": "The Base Central — House Rules",
       "quote": "Pets are not allowed in the building."}
    ],
    "researched_at": "2026-08-27T10:00:00Z",
    "researched_by": "claude-opus-5 (claude-code)",
    "confirmed_at": "2026-08-27T10:12:00Z"
  }
}
```

- `policy` ∈ `not_allowed | negotiable | allowed | unknown`
- `status` ∈ `proposed | confirmed`; only `confirmed` reaches `decide()`
- `confidence` ∈ `high | med | low` — advisory, shown to the user at confirmation
- `sources` may be empty (that is what `unknown` looks like); a `not_allowed`
  proposal with no source is rejected by `buildings-apply` as unusable

### `buildings` table (mirror)

```sql
CREATE TABLE IF NOT EXISTS buildings (
    key           TEXT PRIMARY KEY,
    name          TEXT,
    area          TEXT,
    policy        TEXT,
    status        TEXT,
    confidence    TEXT,
    note          TEXT,
    aliases_json  TEXT,
    sources_json  TEXT,
    researched_at TEXT,
    confirmed_at  TEXT
);
```

Added in `_SCHEMA` (`CREATE TABLE IF NOT EXISTS`), so no `_migrate` step is
needed — an existing DB picks it up on the next `init_db`.

### `rules.decide` signature

```python
def decide(fields, price_thb=None, data_quality=None, district=None,
           building_policy=None) -> tuple[str, str | None]
```

`building_policy` is the already-resolved policy string for this listing's
building (`None` when unknown, unresearched, or merely `proposed`). Resolution —
key normalisation, alias lookup, the `confirmed` gate — happens in
`buildings.py`/`storage.py`, never here: `rules` stays a pure function of
values, as it is for every other rule.

Order inside `decide()`: after the ad's own `pets` check, before the district
check. The ad saying `not_allowed` outright is the more direct evidence and
should keep its own reason string when both apply.

### Queue

`fetch_pending_buildings(conn)` returns one row per distinct building key over
listings that are **active** (no user rejection, not AI-rejected) and have a
non-empty `project_name`, skipping keys already in the store unless
`--refresh`. Each row carries: key, display name (most common spelling), area,
listing count, sample `fb_id`s, and the distinct `pets` values the ads claimed —
the last one so the researcher can see a contradiction coming.

## What Goes Where

- **Implementation Steps** (`[ ]`): code, tests, docs inside this repo
- **Post-Completion** (no checkboxes): the first live research run and what to
  check in it

## Implementation Steps

### Task 1: Building key normalisation and the JSON store

**Files:**
- Create: `src/fb_rent/buildings.py`
- Create: `tests/test_buildings.py`

- [x] `building_key(name)` — lowercase, alphanumerics only, collapse whitespace,
      hyphen-join; returns `""` for empty/None input
- [x] `POLICIES` / `STATUSES` frozensets and a `BuildingError` for invalid values
- [x] `load_buildings(path=None)` / `save_building(...)` with the atomic
      temp-file + `os.replace` write copied from `decisions.py`
- [x] `resolve(store, project_name)` — exact key, then alias lookup; returns the
      entry or `None`
- [x] `policy_for(store, project_name)` — the confirmed policy string or `None`
      (`proposed` and unknown buildings both yield `None`)
- [x] write tests for `building_key` (spelling variants collapse, punctuation,
      Cyrillic, empty input)
- [x] write tests for load/save round-trip, atomic write leaving the old file
      intact on failure, and rejection of an invalid policy/status
- [x] write tests for `resolve` via alias and for `policy_for` returning `None`
      on a `proposed` entry
- [x] run tests — must pass before task 2

### Task 2: The `buildings` table and the research queue

**Files:**
- Modify: `src/fb_rent/storage.py`
- Modify: `tests/test_storage.py`

- [x] add the `buildings` table to `_SCHEMA` (`CREATE TABLE IF NOT EXISTS`) —
      as its own `_BUILDINGS_SCHEMA` constant executed by `init_db`, the way
      every other table in the module is declared (`conn.execute` takes one
      statement, so `_SCHEMA` could not have held it)
- [x] `sync_buildings_to_db(conn, store)` mirroring `decisions.sync_to_db`
- [x] `building_policies(conn)` → `{key: policy}` for confirmed entries only,
      alias keys included so a lookup by any spelling hits
- [x] `fetch_pending_buildings(conn, store, refresh=False)` → the queue rows
      described in Technical Details, ordered by listing count descending
- [x] write tests for the mirror (insert, update, entry removed from the file)
- [x] write tests for the queue: only active listings count, empty
      `project_name` excluded, already-researched keys skipped, `refresh=True`
      returns them, alias spellings collapse into one row
- [x] run tests — must pass before task 3

### Task 3: The rejection rule

**Files:**
- Modify: `src/fb_rent/rules.py`
- Modify: `tests/test_rules.py`

- [x] add `REASON_BUILDING_PETS = "Кондо запрещает животных"`
- [x] add the `building_policy` parameter to `decide()` and the check, placed
      after the ad's own `pets` check and before the district check
- [x] extend `decide`'s docstring: `negotiable`, `allowed` and `unknown` never
      reject; a policy only ever arrives here already confirmed
- [x] leave `score()` untouched and say why in a comment (the tie-at-100 trap)
- [x] write tests: `not_allowed` rejects with the new reason; `negotiable`,
      `allowed`, `unknown` and `None` do not
- [x] write tests: the ad's own `pets == "not_allowed"` still wins the reason
      string when both apply
- [x] write tests: bad `data_quality` suppresses the building rejection too,
      like every other rule
- [x] run tests — must pass before task 4

### Task 4: Wire the policy into `fetch_enriched`

**Files:**
- Modify: `src/fb_rent/storage.py`
- Modify: `tests/test_storage.py`

- [x] load the confirmed-policy map once per `fetch_enriched` call and resolve
      each row through its `project_name` field
- [x] pass `building_policy` into `rules.decide`
- [x] expose `building_policy`, `building_status`, `building_name` and
      `building_sources` on each item for the export
- [x] write tests: a confirmed `not_allowed` building flips its listings to
      `rejected` with the new reason, and a user decision still outranks it
- [x] write tests: a `proposed` building changes nothing; a DB with no rows in
      `buildings` behaves exactly as before
- [x] run tests — must pass before task 5

### Task 5: Batch and apply

**Files:**
- Create: `src/fb_rent/buildings_batch.py`
- Create: `tests/test_buildings_batch.py`

- [x] `build_buildings_batch(conn, store, limit=None, keys=None, refresh=False)`
      → the batch dict (queue rows + a `research_prompt` naming the four
      policy values and the source-evidence requirement)
- [x] `write_buildings_batch(batch, out_dir=None)` → `buildings-batch-<ts>.json`,
      reusing `ai_batch.timestamped_path`
- [x] `validate_buildings_results(results)` — unknown policy value, missing key,
      and a `not_allowed` carrying no source each raise `BatchError` before
      anything is written
- [x] `apply_buildings_results(conn, store, result, dry_run=False)` → writes
      entries as `status: "proposed"` and returns a report (applied, skipped,
      warnings, per-building affected-listing counts)
- [x] write tests for batch assembly (limit, `keys` narrowing, empty queue)
- [x] write tests for each validation failure and for a clean apply
- [x] write tests: apply never changes any listing's status (proposals are inert)
- [x] run tests — must pass before task 6

### Task 6: CLI commands

**Files:**
- Modify: `src/fb_rent/cli.py`
- Modify: `tests/test_cli.py`

- [ ] `fb-rent buildings-batch [--limit N] [--key K]... [--refresh]` — writes the
      batch file, prints its path and the queue size
- [ ] `fb-rent buildings-apply <file> [--dry-run]` — prints counters, each
      proposal with its policy, confidence and affected listing count
- [ ] `fb-rent buildings list [--status proposed|confirmed] [--policy P]` —
      the review view: name, policy, status, sources, affected rows
- [ ] `fb-rent buildings set <key> --policy P [--note TEXT] [--alias A]...` —
      records the user's confirmation (`status: "confirmed"`), syncs the mirror
      and re-exports nothing (the caller runs `export`)
- [ ] a `buildings` block in `ai-status`: how many buildings are unresearched,
      proposed and confirmed
- [ ] write tests for each command via Click's `CliRunner` (success path)
- [ ] write tests for error paths: unknown key on `set`, invalid policy value,
      missing batch file on `apply`
- [ ] run tests — must pass before task 7

### Task 7: Show the policy in the table

**Files:**
- Modify: `src/fb_rent/storage.py` (`export_html`)
- Modify: `tests/test_storage.py`

- [ ] render the policy as a **badge on the existing "Проект/посёлок" cell**, not
      as a new column — `docs/data-layout.md` and the `data_quality` precedent
- [ ] badge colours: `not_allowed` bad, `negotiable` warn, `allowed` good,
      `proposed` rendered distinctly from `confirmed` so an unvetted proposal
      never looks like a decision
- [ ] title/tooltip carries the source quotes and URLs, so a rejection can be
      checked from the table
- [ ] write tests asserting the badge appears for a confirmed building and that
      a `proposed` one renders in its own class
- [ ] write tests asserting no badge and unchanged markup when the store is empty
- [ ] run tests — must pass before task 8

### Task 8: The skill

**Files:**
- Create: `.claude/skills/pets-policy/SKILL.md`

- [ ] frontmatter `description` making it explicit the skill runs **only on
      request** — same wording discipline as `photo-analyze`, which says so to
      stop a non-empty queue from being read as an invitation
- [ ] the loop: `ai-status` → `buildings-batch` → subagent fan-out → result file
      → `buildings-apply` → `AskUserQuestion` per proposal → `buildings set` →
      `export` → report
- [ ] **one subagent per building**, launched in a single message so they run in
      parallel: search results and fetched pages never enter the orchestrator's
      context (the `photo-analyze` rule, same reason)
- [ ] the subagent contract: given name + area, search the management site,
      agency listing pages, Google/FB reviews and expat forums; return JSON only
      — `policy`, `confidence`, `note`, `sources[{url, title, quote}]`; it never
      sets status, never writes files, never touches listings
- [ ] research rules: `not_allowed` needs a verbatim quote saying so; a rule
      about *size or breed* is `negotiable`, not `not_allowed`; any hint that the
      management or owner decides case by case is `negotiable`; silence is
      `unknown`; a source about a different building in the same chain is not
      evidence
- [ ] the confirmation step: one `AskUserQuestion` per proposal showing policy,
      confidence, quotes, URLs and how many listings it would reject — with
      «оставить как есть» always an option
- [ ] the report: confirmed buildings, rows they rejected, proposals declined,
      buildings that came back `unknown` and are worth asking an agent about
- [ ] state plainly that the skill writes nothing except through the CLI

### Task 9: Verify acceptance criteria

- [ ] a confirmed `not_allowed` building rejects every one of its listings with
      «Кондо запрещает животных», including listings scraped after the research
- [ ] `negotiable` keeps the listing and is visible on the row
- [ ] a `proposed` building changes nothing anywhere
- [ ] the ad's own `pets` field is never overwritten by research
- [ ] `data/buildings.json` survives a DB wipe and re-populates the mirror on the
      next `init_db`
- [ ] no score changed for any reason other than a rejection
- [ ] run the full suite: `pytest`
- [ ] check the tie-at-100 spread on the live DB is unchanged (`fetch_enriched` →
      count rows at exactly 100, before and after)

### Task 10: [Final] Documentation

**Files:**
- Create: `docs/pets-policy.md`
- Modify: `docs/README.md`, `docs/pipeline.md`, `CLAUDE.md`, `README.md`

- [ ] `docs/pets-policy.md`: how the pass works, the four policy values, why
      research proposes and the user confirms, and a journal section for live
      runs (same convention as `docs/photo-analysis.md`)
- [ ] link it from `docs/README.md` and mention the pass in `docs/pipeline.md`
      as out-of-pipeline, alongside the photo pass
- [ ] `CLAUDE.md` traps — only what reading the code does not reveal:
      - a `proposed` building is inert by design; making it live removes the only
        thing standing between a bad web search and a wiped-out building
      - the building policy deliberately carries **no score weight** in either
        direction (the tie-at-100 failure)
      - `building_key` is deliberately dumb and does not fuzzy-match; aliases are
        added by hand because a wrong merge rejects another building's listings
      - `data/buildings.json` is the source of truth and the table is a mirror,
        like `decisions.json`
- [ ] `README.md`: the new commands in the command list
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

*No checkboxes — these need a live run and the user's own judgement.*

**First research run:**
- run `fb-rent buildings-batch --limit 5` first, not the whole queue: the point
  is to read five proposals closely and see whether the subagent's evidence bar
  is where it should be before trusting it at volume
- check at least one `not_allowed` proposal by opening its source URLs yourself
- watch for the failure this design expects: a source describing a different
  building in the same chain ("The Base Downtown" vs "The Base Central")

**Worth measuring and recording in `docs/pets-policy.md`:**
- how many distinct buildings the active set actually has, and how many listings
  the average building covers — the whole economic case for keying by building
- how often research returns `unknown`, which is the number that decides whether
  this pass is worth repeating
- whether any building's policy contradicted an ad claiming "pet friendly"

**Not in scope, decide later:**
- re-research cadence (a `researched_at` older than N months → re-queue). Left
  out until a live run shows whether policies actually change.
- fuzzy building matching. Only worth it if the alias list turns out to be a
  chore in practice.
