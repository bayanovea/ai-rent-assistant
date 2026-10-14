# Photo analysis of listings — a second pass on top of the text one

## Context

Today the pipeline sees only the text of the ad. Because of that, `condition` is `unclear` on almost every row (nobody ever writes about the condition in the text), `furnished` is taken on trust, and `living_room` / `parking` / `green_area` stay empty where a picture would answer at once. The photos never reach the model: `cover_image_url` is extracted in `scraper_list.py:124-129` and dies there — `upsert_listing` does not write it (`storage.py:344-357`), and `scraper_detail.py` does not touch the detail-page gallery at all.

The main value of the photos is **checking the claims of the text, not new facts**: everybody writes "fully furnished", the photo shows what stands behind it. Plus one signal that no field carries: a 3D render instead of a real shot.

The naive variant (the whole gallery, native resolution) costs ×12 in tokens and ×4 in time. The plan is built around three limiters that bring it down to ×2.8: **6 photos, 1024 px, and only for the survivors of the text pass**. The target pace is 50 listings per evening; at the start the volume is larger, because 46 non-rejected listings in the DB need their photos fetched.

## Key decisions

| Decision | Value | Why |
|---|---|---|
| How many photos | 6 per listing | The gallery is redundant: 3 angles of the same bedroom. Take no. 1 (the cover) + 5 spread evenly over the rest — galleries usually walk through the rooms |
| Size | 1024 px on the long edge, JPEG q80 | ~1050 tokens/photo, ~6.3k per listing. Mould in a corner and wall texture are still visible |
| Who looks | A subagent on **Sonnet**, one per listing | Images never enter the orchestrator's context at all. Up to ~12 in parallel → 50 listings in minutes |
| When | After `ai-apply`, only for non-rejected | Auto-reject (4 conditions, `rules.py:101-121`) removes ~26% — no reason to spend tokens on them |
| Storage | Columns on `ai_analysis`, not a separate table | One row per flat — that is what dedupe relies on (`dedupe_listings`, `storage.py:529`) |

**Downloading ≠ tokens.** Downloading an extra photo costs seconds and megabytes; handing an extra one into the context is expensive. So the cut is between "files on disk" and "a subagent reads them", not inside the scraping.

## What already exists and is reused

- `rules.decide(fields, price_thb, data_quality)` (`rules.py:82`) — a pure function over a flat dict of fields. Photo fields go into the same dict, the signature does not change.
- `rules.cell_class(key, value, field_def, row_schema_version)` (`rules.py:126`) — the `c-stale` / `c-none` mechanics. For photo columns we pass a different version.
- Origin bands in the export: the `src` field (`fb` / `ai` / `sys`), merging of neighbours in `renderHead()` (`storage.py:1059-1068`), `edge` computed in `html_columns()` (`storage.py:1288-1292`). A fourth value is added.
- `ev` tooltips → the `title=` attribute (`storage.py:981`). The format is a string, nothing to change — the content changes.
- Migration pattern: `PRAGMA table_info` + `ALTER TABLE` in `_migrate()` (`storage.py:240-271`), the `content_key` branch at 262-270 is the model.
- `_analysis_version()` (`cli.py:443`) and its bump in `ai_field_add_cmd` (`cli.py:825-827`) — the model for `photo_analysis_version`.
- `_write_registry()` (`cli.py:849-867`) — atomic write via `mkstemp` + validation before `os.replace`. Reused as is.
- `write_batch()` (`ai_batch.py:146-165`) — name from `generated_at`, suffix on collision. The photo batch mirrors the schema under the name `photo-batch-<ts>.json`.
- `context.request.get(url)` in Playwright shares cookies with the persistent context — required for the signed `fbcdn` links.

**Why one registry and not a second file `config/photo_fields.json`.** Fields like `condition`, `furnished`, `kitchen` are filled by both passes. In two registries they would exist as two copies that immediately drift apart in `values` and `added_in`, and `unknown_keys` would start failing the results of one pass on the fields of the other. A `source` dimension inside a single registry solves the same thing with one key.

**Traps that would have cost debugging:**
- The `home` fixture (`test_cli.py:20-35`) patches every `config` constant separately — the paths are computed at import, substituting `RENT_ASSISTANT_HOME` alone is not enough. The new `PHOTOS_DIR` has to be added there too, otherwise the tests write photos into the real `$RENT_ASSISTANT_HOME`.
- Determinism in the tests is achieved by injecting `now=` / `requirements_md=` into signatures, not with `freezegun` and not with mocks. New functions must accept the same parameters.
- There is no `conftest.py` in `tests/`; fixtures live in their own files.

## Technical details

### The field registry

`config/ai_fields.json` gets an optional `source` key on a field: `"text"` (the default when absent — all 28 current fields stay valid), `"photo"`, `"both"`. The top-level `photo_analysis_version` works like `analysis_version`: it grows only on `ai-field add --source photo --backfill` and decides whether the photo pass is re-queued.

Fields with `source: "both"` (for example `condition`, `furnished`, `kitchen`) are filled by the text pass, and the photo pass overrides or confirms them.

### The `ev` format for photo fields

A value taken from a photo has no verbatim quote. `ev` stays a string but becomes a pointer with a description:

```
"ev": "photo 3: peeling paint above the headboard, a stain on the ceiling"
```

Verifiability is kept — the user opens the third file and looks for themselves. The tooltip rendering is not touched.

### DB schema

`ai_analysis` gets five columns in one `_migrate` branch:

| Column | Meaning |
|---|---|
| `photo_data_json` | fields taken from the photos, same format `{key: {"v","conf","ev"}}` |
| `photos_hash` | idempotency key: SHA-256 of the sorted `name+size` of the files in `$RENT_ASSISTANT_HOME/photos/<fb_id>/` |
| `photo_schema_version` | the registry version the photo pass saw |
| `photos_analyzed_at` | ISO UTC |
| `photo_model` | which model looked |

Plus `photo_comment` — 1-2 sentences about what is visible and what the text did not say (the counterpart of `comment`).

Photos on disk: `$RENT_ASSISTANT_HOME/photos/<fb_id>/01.jpg` … `06.jpg`. ~1.2 MB per listing, ~60 MB per 50.

### Merging

In `fetch_enriched` (`storage.py:732`): `fields = {**data_json, **photo_data_json}` — photo overrides text. From there the existing `rules.decide` works unchanged.

**An important asymmetry:** `decide` is recomputed on every read (`storage.py:774`), `score` is not — it is written only at apply (`ai_batch.py:355`). So `photo-apply` must recompute and write `score` over the merged fields, otherwise the photo signals never reach the sort order.

### Running the pass

`rent-assistant photo-batch` writes JSON, the skill launches one subagent per listing:

```
Agent(subagent_type: "general-purpose", model: "sonnet", prompt: <instruction + paths to 6 files>)
```

The subagent reads the photos via `Read` and returns a compact JSON for one listing. The orchestrator collects the results into `result-<ts>.json` and calls `photo-apply`. Images never enter its context.

## What goes where

**Implementation Steps** — everything inside the repository: code, tests, skills, README.
**Post-Completion** — manual check on live FB and the decision on added fields.

## Implementation Steps

### Task 1: Spike — how to get the gallery URLs

Defines the shape of the whole first step. If the links are in the page source, scraping barely slows down; if the lightbox is needed — +10-15 s per listing.

**Files:**
- Create: a one-off probe script in a scratch directory (`probe_gallery.py`, does not go into the repository)

- [x] write the script: open 2-3 real detail pages through `browser.launch_persistent`, dump `page.content()` and every `img[src]`
- [x] grep the dump for `scontent` / `fbcdn`, count how many unique URLs are visible without clicks and what sizes their names carry
- [x] if fewer than 6 without clicks — check what opening the lightbox gives (click on the cover + `ArrowRight`), time it for 10 photos
- [x] record the conclusion right in this plan file (⚠️ or ➕), choose the strategy for Task 2
- [x] no tests — the spike is one-off, delete afterwards (scripts deleted)

➕ **Spike conclusion (run on 3 live listings, 25.08.2026):** the gallery is visible
in the DOM **without a single click** — 11 / 10 / 28 unique URLs respectively,
that is, always ≥ 6. The lightbox is not needed, scraping does not slow down (the
page takes 6-7.5 s to load anyway, extracting the URLs takes ~0 s). An attempt to
click the cover fails outright — an overlay intercepts the pointer events, one
more argument against the lightbox.

**Strategy for Task 3 (`extract_gallery_urls`)**, locale-independent:

1. take every `img` whose `src` contains `/t39.30808-6/` and whose `alt` is non-empty;
2. group by `alt` and take the largest group (≥ 2 elements).
   Gallery photos share one `alt` — `"Фото <listing title>"` ("Photo <listing
   title>"); the "similar listings" cards each have their own
   (`"<name> в группе <group>"`, "<name> in the group <group>"), so the mode
   lands in the gallery unambiguously. Avatars and icons are filtered out
   already in step 1 (they live in `t39.30808-1` / `t1.30497-1`);
3. dedupe by `src` keeping DOM order — it matches the gallery order.

⚠️ Rejected variants: `aria-label` numbering (`"Смотреть фото 26"`, "View
photo 26") — locale-dependent and catches extra items (46 matches where there
are 28 photos); filtering on `cstp=mx` — drags in all the 565×565 cards of
similar listings.

➕ **Size of the served images** — 960×N on the long edge (`ctp=s960x717`),
so the planned 1024 px is effectively "do not upscale": Pillow only shrinks
what is larger, there must be no upscaling.

➕ **`context.request.get` works on signed CDN URLs** (verified):
HTTP 200, the body is a valid JPEG (`ff d8 ff`), 45-175 KB per file, 6 files
download in 1.1-2.3 s. A separate HTTP client with cookie plumbing is not needed.


### Task 2: The `photos.py` module — download, resize, paths, hash

**Files:**
- Create: `src/rent_assistant/photos.py`
- Create: `tests/test_photos.py`
- Modify: `pyproject.toml`
- Modify: `src/rent_assistant/config.py`
- Modify: `tests/test_cli.py`

- [x] add `pillow>=10` to `dependencies` in `pyproject.toml`
- [x] in `config.py` add `PHOTOS_DIR = RENT_ASSISTANT_HOME / "photos"` (next to `AI_DIR`, :52), `PHOTO_MAX_COUNT = 6`, `PHOTO_LONG_EDGE_PX = 1024`, `PHOTO_JPEG_QUALITY = 80`
- [x] **add `PHOTOS_DIR` to the `home` fixture** (`tests/test_cli.py:20-35`) — without this the tests write into the real `$RENT_ASSISTANT_HOME`
- [x] `photos_dir(fb_id) -> Path` — the path `$RENT_ASSISTANT_HOME/photos/<fb_id>/`
- [x] `pick_photo_urls(urls) -> list[str]` — takes the first URL plus 5 spread evenly over the rest, at most `PHOTO_MAX_COUNT`
- [x] `download_and_resize(context, urls, fb_id) -> list[Path]` — downloads through `context.request.get(url)` (shares cookies with the persistent context), resizes with Pillow to 1024 px on the long edge, writes `01.jpg`…`06.jpg`, converts to RGB; a broken/unavailable URL is skipped without failing the listing
- [x] `photos_hash(fb_id) -> str | None` — SHA-256 of the sorted `name+size`, `None` if the directory is missing or empty
- [x] `purge_photos(fb_id) -> None` — removes the listing's directory
- [x] write tests for `pick_photo_urls` (0/1/3/6/20 URLs), `photos_hash` (stability, sensitivity to the contents, `None` on empty), `purge_photos` — all on `tmp_path`
- [x] write a test for `download_and_resize` with a fake `context.request` and a synthetic PNG: check the long edge ≤ 1024, JPEG format, file names, skipping a broken URL
- [x] run the tests — must pass before Task 3

### Task 3: Gallery extraction in the scraper

**Files:**
- Modify: `src/rent_assistant/scraper_detail.py`
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_extractors.py`

- [x] in `scraper_detail.py` add `extract_gallery_urls(page) -> list[str]` following the strategy from Task 1; best-effort, like the other selectors in the file — on failure an empty list, not an exception
- [x] return the URLs in `raw_json` under the key `gallery_urls` (in `scrape_detail`, next to `text_lines`) — so they survive until the download and are visible when debugging
- [x] in the scrape loop `cli.py:264-269`, after `scrape_detail`, call `photos.download_and_resize` if the gallery is non-empty and the photo directory is not yet filled
- [x] add the `--no-photos` flag to `scrape` for fast runs without images
- [x] respect the existing delay budget: the download runs inside the jitter already counted, no separate pauses are added
- [x] write a test for `extract_gallery_urls` with a fake page (success, empty gallery, an exception from the selector)
- [x] write a test that `--no-photos` creates no directories
- [x] run the tests

### Task 4: The field registry — the `source` dimension

**Files:**
- Modify: `src/rent_assistant/ai_schema.py`
- Modify: `config/ai_fields.json`
- Modify: `tests/test_ai_schema.py`

- [x] in `ai_schema.py` add `KNOWN_SOURCES = {"text", "photo", "both"}` and validation of the `source` key in `load_registry` (absent = `"text"`)
- [x] add `Registry.fields_for(source) -> tuple[dict, ...]` — returns the fields the given pass must fill (`photo` matches `photo` and `both`)
- [x] in `config/ai_fields.json` set `"source": "both"` on the fields `condition`, `furnished`, `kitchen`, `living_room`, `parking`, `green_area`, `pool_gym`; leave the other 21 without the key
- [x] add the new field `photo_quality` (`enum`: `real` / `render` / `other_unit` / `floorplan` / `unclear`, `source: "photo"`) — the "3D render instead of a real photo" signal that exists nowhere today
- [x] raise `schema_version` to 2, add `photo_analysis_version: 2`
- [x] write tests: an invalid `source` fails the registry, the default is `"text"`, `fields_for("photo")` returns `both`+`photo` and does not return `text`
- [x] run the tests

### Task 5: The photo-pass queue

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_storage.py`

- [x] in `_migrate()` add a branch following the `content_key` model (`storage.py:262-270`): five columns in `ai_analysis` — `photo_data_json`, `photos_hash`, `photo_schema_version`, `photos_analyzed_at`, `photo_model` — plus `photo_comment`
- [x] `fetch_pending_photos(conn, registry) -> PendingPhotos` — a NamedTuple `(pending, no_photos)`. A candidate enters the queue if: the text analysis exists, `rules.decide` over the current fields gives **not** `rejected`, the photo directory is non-empty, and (`photos_hash` does not match OR `photo_schema_version < photo_analysis_version` OR there was no pass). Reasons: `new` / `changed` / `schema`
- [x] listings with no photos on disk go to `no_photos`; they need `scrape --refresh`
- [x] extend `ai-status`: after the current summary print a "Photos" block — how many analysed / queued with a breakdown by reason / without photos
- [x] write tests for `fetch_pending_photos`: rejected does not enter; without photos goes to `no_photos`; a change of `photos_hash` re-queues; a repeat after apply gives an empty queue (idempotency); a bump of `photo_analysis_version` brings everyone back
- [x] write a migration test: an old DB without the new columns opens and receives them
- [x] run the tests

### Task 6: `photo-batch` — building the batch

**Files:**
- Create: `src/rent_assistant/photo_batch.py`
- Create: `tests/test_photo_batch.py`
- Modify: `src/rent_assistant/cli.py`

- [x] `build_photo_batch(conn, registry, *, limit, ids, now=None) -> dict` — top level: `schema_version`, `photo_analysis_version`, `generated_at`, `requirements_md`, `fields` (only `fields_for("photo")`), `items`. The `now=` parameter is required for test determinism
- [x] item: `fb_id`, `url`, `photos_hash`, `photos` (list of paths), `current_fields` (the current values of the text pass **for the photo fields only** — the model has to see what the text already promised, to catch the discrepancy), `listing_title`
- [x] `write_photo_batch(batch, out_dir=None) -> Path` — following `write_batch` (`ai_batch.py:146`): the name `photo-batch-<ts>.json` from the digits of `generated_at`, suffix `-2` on collision
- [x] the command `rent-assistant photo-batch` with the flags `--limit` (default 25), `--all`, `--ids`, `--out` — a mirror of `ai-batch` (`cli.py:534`), including the branch "empty → echo + return with code 0"
- [x] `validate_photo_results(results, registry)` — `fb_id` and `photos_hash` are required; `fields` is an object; keys unknown to the registry and an enum outside `values` fail the whole file (like `_validate_results`, `ai_batch.py:222`); **`verdict` is not required** — the verdict stays with the text pass
- [x] write tests: batch structure, rejected cut off, `--ids`, only photo fields land in `fields`, every validation branch
- [x] run the tests

### Task 7: `photo-apply` — writing and recomputing

**Files:**
- Modify: `src/rent_assistant/photo_batch.py`
- Modify: `src/rent_assistant/storage.py`
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_photo_batch.py`

- [x] `upsert_photo_analysis(conn, row, *, now)` in `storage.py` following `upsert_ai_analysis` (`storage.py:613`)
- [x] `apply_photo_results(conn, registry, result, *, dry_run, now) -> PhotoApplyReport` — writes `photo_data_json`, `photo_comment`, `photos_hash`, `photo_schema_version` (the version **from the result file**, as in `ai_batch.py:347-349`), then over the merged `{**data_json, **photo_data_json}` recomputes `rules.decide` and **`rules.score`** and overwrites the `verdict`-independent `status` / `reason` / `score`
- [x] count the discrepancies separately in the report: fields where the photo overrode a non-empty text value — this is the main product of the pass and has to be shown to the user
- [x] a `photos_hash` in the result that does not match the current one on disk → **skip the write** with a warning. This is a deliberate difference from the text pass: `apply_results` on a hash mismatch still writes and stores the reported hash (`ai_batch.py:351`), because the text can be re-read. Here the analysis describes pictures that are no longer the ones on disk, and writing it would assign somebody else's observations to the listing
- [x] the command `rent-assistant photo-apply <path>` with `--dry-run`, prints: applied, discrepancies, new auto-rejects with reasons, skipped, `proposed_new_fields`
- [x] write tests: photo overrides text; `score` changes after apply; a photo drops a listing into `rejected` via `size_sqm`/`property_type`; `--dry-run` does not write; a hash mismatch is skipped; a repeated apply is idempotent
- [x] run the tests

### Task 8: Merging in the export and the fourth band

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] in `fetch_enriched` (`storage.py:732`) parse `photo_data_json` and put it into `fields` by merging `{**data_json, **photo_data_json}`; remember the set of overridden keys in `photo_keys`
- [x] in `html_columns` give the photo fields `src: "photo"`, add the `photo_comment` column to the same band
- [x] add `photo` to `SRC_LABELS` (`storage.py:915`, label "Photo analysis") and the CSS classes `.src-photo` following `.src-ai` (`storage.py:846-854`)
- [x] in `html_row` (`storage.py:1296`) pass **`photo_schema_version`** to `rules.cell_class` for the photo columns, not `ai_schema_version` — otherwise the new photo fields get hatched on every row
- [x] mark the cells where the photo overrode a non-empty text value — reuse the existing badge pattern (`storage.py:1020-1022`), the label "text: <old value>" in the tooltip
- [x] extend the legend (`storage.py:898-907`) with a line about the photo band and the discrepancy badge
- [x] write tests on the raw HTML (in the style of `test_export_html_stale_only_for_fields_added_after_analysis`, `test_storage.py:522`): the band renders, `edge` on the boundary, `c-stale` for a photo field is computed against its own version, the discrepancy badge appears
- [x] run the tests

### Task 9: Photo cleanup

**Files:**
- Modify: `src/rent_assistant/ai_batch.py`
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] in `apply_results` (`ai_batch.py:260`) after an auto-reject call `photos.purge_photos(fb_id)` — a rejected listing will not need its photos again, and bringing it back via `unreject` will require `--refresh` anyway
- [x] in `dedupe_listings` (`storage.py:529`) clean the photo directories of the non-canonical rows together with deleting their `ai_analysis` rows (`storage.py:579-580`)
- [x] write tests: an auto-reject removes the directory; dedupe removes the absorbed row's directory but not the canonical one's
- [x] run the tests

### Task 10: The photo-analysis skill

**Files:**
- Create: `.claude/skills/photo-analyze/SKILL.md`
- Modify: `.claude/skills/analyze/SKILL.md`

- [x] write `photo-analyze/SKILL.md` following the structure of `analyze/SKILL.md`: front matter with triggers, then a numbered procedure — check the queue → build the batch → fan out subagents → collect the result → apply → export → report
- [x] fix the fan-out: **one subagent per listing**, `Agent(subagent_type: "general-purpose", model: "sonnet")`, independent ones launched in a single message; the subagent prompt carries the paths to the 6 files, the registry subset with `prompt`, the current text values, and the requirement to return JSON only
- [x] carry the text skill's discipline into the subagent instruction: "invent nothing", `unclear` is better than a plausible guess, `ev` is required for every non-empty value — in the format `"photo N: what is visible"`
- [x] spell out `photo_quality`: `render` for 3D visualisations, `floorplan` for floor plans, `other_unit` if a clearly different unit is shown — and that under `render` the other fields must not be filled
- [x] spell out the boundary of authority as in the text skill: the subagent does not touch `verdict`, `score` and `status`, `rules.py` computes them
- [x] write the section about `proposed_new_fields` and the subsequent `ai-field add --source photo --backfill`
- [x] in `analyze/SKILL.md` step 10 add the hand-off: after the export, if the photo queue is non-empty — offer to run `/photo-analyze`
- [x] no tests — it is a document; verified in Task 12 on a live run

### Task 11: `ai-field add --source`

**Files:**
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_cli_ai_field.py`

- [x] add the `--source` flag to `ai-field add` via `click.Choice(sorted(ai_schema.KNOWN_SOURCES))`, default `text` — exactly like `--type` is done (`cli.py:734`)
- [x] under `--backfill` bump `photo_analysis_version` instead of `analysis_version` if the field has `source` `photo` or `both`; for `both` — both versions
- [x] keep writing the registry through the existing `_write_registry` (`cli.py:849`) — atomicity and validation before the swap are already there; keep the `--registry` option (the tests work on a copy of the real registry, `test_cli_ai_field.py:20-25`)
- [x] `ai-field list` prints a source column
- [x] write tests: `--source photo --backfill` moves only `photo_analysis_version`; `--source both --backfill` moves both; the default does not touch the photo version
- [x] run the tests

### Task 12: Verify acceptance criteria

- [x] `rent-assistant scrape --max 5` on live FB (skipped — not automatable: needs a live FB login; the limiters are covered by the `test_photos.py` tests)
- [x] full cycle on 5 listings (skipped — needs a live scrape and a subagent fan-out with real images)
- [x] inspect `rentals.html` in the browser (skipped — manual check; the band markup, tooltips and discrepancy badge are covered by the raw-HTML tests in `test_storage.py`)
- [x] rejected rows do not enter `photo-batch`, directories are cleaned — covered by tests (`test_storage.py::test_fetch_pending_photos_*`, `test_photo_cleanup.py`)
- [x] measure real token and time spend (skipped — needs a live run; the ×2.8 estimate in the plan stays a calculation)
- [x] run the whole suite: `pytest` — 385 passed
- [x] idempotency: a repeated `photo-batch` after `photo-apply` gives an empty queue — `test_storage.py::test_fetch_pending_photos_is_idempotent_after_a_pass`, `test_photo_batch.py::test_apply_photo_results_is_idempotent`

### Task 13: [Final] Documentation

- [x] README: the "Photo analysis" section — commands, limiters (6 / 1024 / survivors only), the model choice and why Sonnet, cost of a run
- [x] README: `$RENT_ASSISTANT_HOME/photos/` in the Local state section, `pillow` in Install
- [x] CLAUDE.md: if non-trivial patterns surfaced along the way (the behaviour of `context.request` with signed URLs, how the subagent fan-out is built) — write them down
- [x] move the plan to `docs/plans/completed/`

## Post-Completion

**Manual check:**
- Run the photo pass on the 46 non-rejected listings already in the DB. They need `rent-assistant scrape --refresh` (detail cap 60 per run, `config.py:37`), that is, ~10-15 minutes of scraping before the first photo pass.
- Assess Sonnet's quality on the contested cases from past sessions — first of all The Origin `9000000000000001` (kitchenette vs full kitchen). If it is systematically wrong — raise the subagents to Opus 5 with `effort: medium`, one line in the skill.

**Decisions after the first run:**
- Which new fields the model proposes from the photos — add through `ai-field add --source photo --backfill`.
- Whether 6 photos and 1024 px are enough, or `condition` needs a larger size. Both values are constants in `config.py`.
