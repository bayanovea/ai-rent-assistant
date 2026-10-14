# Hashes of the first three photos as a duplicate signal

## Overview

Right after scraping, a cheap stage appears: for every listing the
**first three gallery shots** are taken, two hashes are computed for each
(MD5 of the original CDN bytes and a perceptual dHash), and the hashes go
into the DB. No content recognition at all — only exact comparison of
hashes between listings.

Why: `dedupe_listings` today catches a duplicate by text (`content_key`,
`same_property`) and by facts (project + price + bedrooms), and both signals
need either matching text or a finished AI analysis. One flat that went out
through two agents with different text and a different price is caught by
nothing — while its photos match byte for byte. This has already been
measured: `docs/photo-analysis.md`, section "Side finding: photos as a
duplicate signal" — two different `fb_id`s had all four MD5s identical.

The gain: fewer rows in the table **before** the AI pass, because the photo
signal does not depend on the analysis. Fewer rows — fewer to look at.

Integration: a new signal `photos` inside `_group_duplicates`, next to
`exact` / `text` / `facts`, with the same vetoes on floor, block and size.
The merge goes through the existing `duplicates` table, so the next
`scrape` no longer opens a merged repost.

## Context (from discovery)

- `src/rent_assistant/cli.py:445` — the point inside `scrape` where the detail
  page has already been read and the jitter paid; `_download_photos` is
  called there too.
- `src/rent_assistant/photos.py` — the gallery on disk and `photos_hash`, the
  idempotency key of the **full** photo pass. The new stage does not touch it.
- `src/rent_assistant/storage.py:615` `dedupe_listings`, `:564` `_group_duplicates`,
  `:495` `same_property`, `:446` `_contradicts` — the signal plugs in here.
- `src/rent_assistant/storage.py:701` `gallery_urls_of` — pulls `gallery_urls` out
  of `raw_json`; `photos-fetch` (`cli.py:953`) is built on the same thing.
- `docs/photo-analysis.md` — the measurement the whole plan grows from.

**The trap this plan is careful about in the first place.** If three
previews are put into `$RENT_ASSISTANT_HOME/photos/<fb_id>/`, the full photo
pass will decide forever that the gallery is downloaded: `_download_photos`
skips a non-empty directory (`cli.py:202`), and `photos_hash` is computed
from file names and sizes. So the new stage **writes nothing to disk at
all**: the bytes are downloaded, hashed and thrown away.

## Development Approach

- **testing approach**: Regular (code first, then tests in the same task)
- every task is finished before the next one
- **CRITICAL: every task contains new/updated tests**
- **CRITICAL: all tests pass before the next task starts**
- **CRITICAL: the plan is updated if the scope changes along the way**
- backward compatibility: old rows without hashes simply do not take part
  in the photo signal, nothing breaks

## Testing Strategy

- **unit tests** are mandatory in every task
- the project has no e2e tests (a CLI tool, tests in `tests/` via pytest)
- the run command: `pytest` from the `scrapper/` directory
- network calls (`context.request.get`) are mocked, as already done in
  `tests/test_photos.py`

## Progress Tracking

- mark completed items `[x]` immediately
- new tasks get the prefix ➕
- blockers get the prefix ⚠️

## Solution Overview

**Two hashes per shot, exact comparison on both.**

- `md5` — of the original CDN bytes. Catches a literal repost: the same
  file, re-uploaded by the same or another agent.
- `dhash` — a 64-bit difference hash (8×9 in greyscale). Catches a
  re-upload: the same picture that went through somebody else's phone, a
  resize or a recompression.

The comparison stays exact: hash string equals hash string. No Hamming
distance — a distance threshold needs tuning and gives false merges on
look-alike empty interiors.

**The merge threshold is two matching shots out of three.** One shared shot
is most often a photo of the complex, the pool or an agency logo, shared
between different flats. A listing with a single shot does not take part in
the photo signal by construction.

**Frequent hashes are excluded.** A hash seen on more than
`PHOTO_HASH_COMMON_MAX` listings is an agency splash or a general view of
the complex, not a flat. Such hashes are dropped from the index before
matching, and `dedupe` prints how many there were.

**The vetoes stay.** A pair where `_contradicts` holds (different floor,
different block, size apart by more than the tolerance) is not merged even
with matching photos.

**The photo signal does not depend on the AI.** So it also works under
`dedupe --exact-only`: it is not a "near-duplicate rule" on top of extracted
fields but a hard signal of the same kind as `content_key`.

**Why the three shots are downloaded separately and not reused from the
gallery.** In a run without `--no-photos`, `download_and_resize` holds the
bytes in its hands, and it is tempting to compute the hashes right there. We
do not: the shot's position in the gallery would then depend on which URLs
dropped out, and `md5` on whether we computed it before or after the resize.
Three extra GETs inside the jitter already paid cost fractions of a second
and keep the new stage fully orthogonal to the photo pass.

## Technical Details

**New module `src/rent_assistant/fingerprint.py`** (not in `photos.py`: that one
is about files on disk for the AI pass, this one is about hashes in the DB):

```python
fingerprint(raw: bytes) -> tuple[str, str] | None   # (md5, dhash), None if it did not decode
dhash(image: Image.Image) -> str                     # 8x9 -> 64 bits -> 16 hex characters
fetch_fingerprints(context, urls, limit) -> list[tuple[int, str, str]]  # (position, md5, dhash)
```

`fetch_fingerprints` takes `urls[:limit]`, downloads through `context.request`
(the persistent profile's cookies — the same thing `photos.py` runs on),
skips failed URLs, but **keeps the shot's original position** in the gallery.

**New table:**

```sql
CREATE TABLE IF NOT EXISTS photo_hashes (
    fb_id      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    md5        TEXT NOT NULL,
    dhash      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (fb_id, position)
);
CREATE INDEX IF NOT EXISTS idx_photo_md5   ON photo_hashes(md5);
CREATE INDEX IF NOT EXISTS idx_photo_dhash ON photo_hashes(dhash);
```

**New constants in `config.py`:**

```python
PHOTO_FINGERPRINT_COUNT: int = 3   # how many of the first shots to hash
PHOTO_MATCH_MIN: int = 2           # how many matching shots = one property
PHOTO_HASH_COMMON_MAX: int = 4     # a hash more frequent than this is a splash, not a flat
```

**Matching** (`storage._photo_matches`): an inverted index
`("m", md5) | ("d", dhash) -> {fb_id}`, keys with more listings than
`PHOTO_HASH_COMMON_MAX` are dropped, the remaining ones yield candidate
pairs, and for each pair the number of shots of A that have a match in B is
counted (greedily, one shot of B per shot of A). A pair passes at
`matched >= PHOTO_MATCH_MIN`. No full pairwise scan is needed — the index
gives only those that share a hash.

**The flow in `scrape`:** detail page read → `fetch_fingerprints` (always
runs, including under `--no-photos`; does not run under `--dry-run`) →
`upsert_listing` → on a result other than `duplicate` write `save_photo_hashes`.

**Backfill `rent-assistant photo-hash`:** over rows without hashes that have
something in `raw_json.gallery_urls`. CDN signatures live ~a week — what did
not make it is repaired through `scrape --ids`, exactly as with `photos-fetch`.

## What Goes Where

- **Implementation Steps** — code, tests, documentation inside the repository.
- **Post-Completion** — a run on the live DB and tuning the thresholds on real data.

## Implementation Steps

### Task 1: The shot-hashing module

**Files:**
- Create: `src/rent_assistant/fingerprint.py`
- Modify: `src/rent_assistant/config.py`
- Create: `tests/test_fingerprint.py`

- [x] add the constants `PHOTO_FINGERPRINT_COUNT`, `PHOTO_MATCH_MIN`,
      `PHOTO_HASH_COMMON_MAX` to `config.py` with a comment on why the
      threshold is exactly two shots
- [x] create `src/rent_assistant/fingerprint.py` with a module docstring:
      why two hashes and why here rather than in `photos.py`
- [x] implement `dhash(image)` — greyscale, resize to 9×8, comparison of
      neighbouring pixels along the row, 64 bits into 16 hex characters
- [x] implement `fingerprint(raw)` — `(md5_hex, dhash_hex)` or `None`
      on undecodable bytes
- [x] implement `fetch_fingerprints(context, urls, limit=None)` —
      download through `context.request.get`, skip failures keeping the
      position, no writing to disk
- [x] write tests for `dhash`: the same picture → the same hash;
      the picture re-saved as JPEG at a different quality → the same hash;
      a clearly different picture → a different hash
- [x] write tests for `fingerprint`: success and garbage bytes (`None`)
- [x] write tests for `fetch_fingerprints` with a fake `context`: the limit,
      skipping a `not ok` response, keeping positions with a hole in the middle
- [x] run `pytest` — green before task 2

### Task 2: Storing the hashes in the DB

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] add `_PHOTO_HASHES_SCHEMA` and both indexes, wire them into
      `init_db` next to the other schemas (no migration needed:
      `CREATE TABLE IF NOT EXISTS` creates the table on an old DB too)
- [x] implement `save_photo_hashes(conn, fb_id, entries, now=None)` —
      `INSERT OR REPLACE`, an empty list erases the previous rows
- [x] implement `photo_hashes_by_listing(conn)` → `dict[fb_id, list]`
- [x] implement `listings_missing_photo_hashes(conn)` — rows without hashes,
      with `raw_json`, oldest first
- [x] implement `delete_photo_hashes(conn, fb_id)`
- [x] write tests for write/read/overwrite and for `init_db` over an
      existing DB without the table creating it
- [x] write a test for `listings_missing_photo_hashes`: a row with hashes is
      not returned, a row without is
- [x] run `pytest` — green before task 3

### Task 3: The `photos` signal in dedupe

**Files:**
- Modify: `src/rent_assistant/storage.py`
- Modify: `tests/test_storage.py`

- [x] implement `_photo_matches(hashes_by_id)` — the inverted index,
      dropping frequent hashes, candidate pairs, counting matching shots;
      return the pairs and the number of dropped hashes
- [x] wire the call into `_group_duplicates` after the exact pass,
      **independently of `fuzzy`**, with the `_contradicts` veto and the
      reason `photos`
- [x] pass the hashes into `_load_for_dedupe` (one query, not per row)
- [x] in `dedupe_listings` delete the `photo_hashes` of merged rows together
      with `ai_analysis` and the photo directory
- [x] write a test: two listings with two matching MD5s out of three →
      merged with the reason `photos`
- [x] write a test: one shot out of three matches → not merged
- [x] write a test: a `dhash` match with different MD5s → merged
- [x] write a test: a hash on five listings (a frequent one) → ignored,
      no merge
- [x] write a test: two matching shots but different floors → `_contradicts`
      wins, no merge
- [x] write a test: `fuzzy=False` — the photo signal still works
- [x] run `pytest` — green before task 4

### Task 4: The stage inside scrape

**Files:**
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_cli.py`

- [x] add `_fetch_photo_hashes(context, detail)` next to
      `_download_photos` — a thin wrapper over `fingerprint.fetch_fingerprints`
      with `gallery_urls` from `detail["raw_json"]`
- [x] call it right after a successful `scrape_detail`, **before** the
      `--no-photos` branch (three shots are not a gallery), skipping `--dry-run`
- [x] write the hashes through `save_photo_hashes` after a successful
      `upsert_listing`, except on the result `duplicate`
- [x] add the counter `photo hashes: N` to the run's summary line
- [x] write a test: a run with `--no-photos` still writes hashes
- [x] write a test: `--dry-run` does not fetch shots and does not write to the DB
- [x] write a test: a listing without `gallery_urls` passes without an error
- [x] run `pytest` — green before task 5

### Task 5: The `rent-assistant photo-hash` command (backfill)

**Files:**
- Modify: `src/rent_assistant/cli.py`
- Modify: `tests/test_cli.py`

- [x] add the `photo-hash` command with `--limit` (default 100),
      `--all` and `--ids`, following `photos-fetch` (`cli.py:883`)
- [x] take the targets from `listings_missing_photo_hashes`, the URLs through
      `gallery_urls_of`, the pause between listings by
      `PHOTO_FETCH_DELAY_MIN_SEC` / `MAX`
- [x] report separately the rows without `gallery_urls` and the expired
      signatures, with the hint `rent-assistant scrape --ids <fb_id>`
- [x] add the line "Photo hashes: N of M" to `ai-status` — so the backfill
      queue is visible
- [x] write a test: the command skips rows that already have hashes
- [x] write a test: `--ids` reaches past the queue
- [x] write a test: a row without `gallery_urls` lands in the report, not in
      a crash
- [x] run `pytest` — green before task 6

### Task 6: Verify acceptance criteria

- [x] check that the new stage writes nothing into `$RENT_ASSISTANT_HOME/photos/` —
      grep for `PHOTOS_DIR` and `photos_dir` in the new code
- [x] check that `photos_hash` and the photo-pass queue did not change:
      `pytest tests/test_photos.py tests/test_photo_batch.py`
- [x] check that a row merged by the photo signal does not come back on the
      next scrape (a row in `duplicates` + `duplicate_fb_ids`)
- [x] run the full `pytest`

### Task 7: [Final] Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/photo-analysis.md`

- [x] README: in the dedupe section add the fourth level — matching
      shots; describe `photo-hash` next to `photos-fetch`
- [x] CLAUDE.md: in the "Photos" section — why the hash stage does not write
      into `PHOTOS_DIR` and what happens if it does; in "Scrape" — why the
      hashes are computed from the original bytes, not from the resized files
- [x] `docs/photo-analysis.md`: rewrite the "Side finding" section as
      implemented, with a link to this plan
- [x] move the plan to `docs/plans/completed/`

## Post-Completion

*Needs a live DB and a manual check — no checkboxes*

**Tuning the thresholds on real data:**
- run `rent-assistant photo-hash --all`, then `rent-assistant dedupe --dry-run` and
  check by eye the first 10–15 groups with the reason `photos`
- if false merges appear — raise `PHOTO_MATCH_MIN` to 3 or
  lower `PHOTO_HASH_COMMON_MAX`
- if the photo signal finds nothing beyond the text one — write that into
  `docs/photo-analysis.md`: a negative result is a measurement too

**Time budget:**
- measure how much more expensive `scrape` became per listing (expected:
  fractions of a second inside the existing jitter); if noticeable, discuss
  moving the stage into a separate `photo-hash` run
