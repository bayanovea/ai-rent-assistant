# Command reference

Every command is a subcommand of `fb-rent`. The two AI passes are not commands
— they are Claude Code skills (`/analyze`, `/photo-analyze`) that drive the
commands below; see [pipeline.md](pipeline.md).

Paths default to the sibling `data/` directory; override the base with
`FB_RENT_HOME` (see [data-layout.md](data-layout.md)).

## `fb-rent login`

Opens Chromium in headed mode and waits (up to 5 minutes) for you to sign in
manually — including 2FA if enabled. On success the cookies are stored in the
persistent profile so later `scrape` runs can reuse the session headlessly.

Re-run whenever Facebook invalidates the session (typically every 1–4 weeks, or
after a suspicious-activity check).

## `fb-rent scrape`

Runs the full collection pipeline: build the search URL → scrape the list →
walk each detail page with a 3–7 s random jitter → extract fields → hash the
first three gallery shots → download the gallery → upsert into SQLite →
regenerate CSV and HTML.

| Flag              | Default | Description                                              |
|-------------------|---------|----------------------------------------------------------|
| `--max`           | `40`    | Maximum number of list cards to collect.                 |
| `--days`          | `7`     | Filter to listings created in the last N days.           |
| `--dry-run`       | off     | Print what would be written without touching the DB.     |
| `--skip-details`  | off     | List cards only, skip per-item detail pages.             |
| `--refresh`       | off     | Re-scrape detail pages for listings already in the DB.   |
| `--no-photos`     | off     | Skip downloading listing photos (faster, no photo pass). |
| `--detail-cap`    | `60`    | Maximum detail pages to open in one run.                 |
| `--min-price`     | `15000` | Lower bound of the price window, in THB.                 |
| `--max-price`     | `30000` | Upper bound of the price window, in THB.                 |
| `--bands`         | `1`     | Split that window into N queries and sweep them all.     |
| `--category`      | Phuket  | Marketplace category/location id to search.              |
| `--ids`           | —       | Scrape these `fb_id`s directly, skipping the feed.       |

```sh
fb-rent scrape --max 10 --dry-run       # smoke check, no writes
fb-rent scrape --max 40                 # regular run
fb-rent scrape --max 60 --skip-details  # fast incremental sweep

# wide sweep: 4 price bands x 60 cards, 150 detail pages, photos later
fb-rent scrape --bands 4 --max 60 --days 30 --detail-cap 150 --no-photos

# repair path: open these listings whatever the DB already holds
fb-rent scrape --ids 1234567890 --ids 9876543210
```

Why the flags behave the way they do — bands, the detail budget, what `--ids`
repairs and what a re-sighting is allowed to overwrite — is in
[scraping.md](scraping.md).

Two timeouts can be raised from the environment for a slow night run:
`FB_RENT_PAGE_LOAD_TIMEOUT_MS` (default 30000) and
`FB_RENT_CONTENT_TIMEOUT_MS` (default 15000).

## `fb-rent dedupe`

Merges listings that describe the same flat, using the photo and fact signals
that need no matching text. `--exact-only` restricts it to byte-identical text
and matching photos; `--dry-run` reports without writing. Whatever is merged is
remembered in `duplicates`, so the next scrape skips it without opening its
page. See [dedupe.md](dedupe.md).

## `fb-rent export`

Regenerates the CSV and HTML from the current DB, and mirrors `decisions.json`
into it on the way.

```sh
fb-rent export                               # writes ../data/rentals.{csv,html}
fb-rent export --path /tmp/rentals.csv       # custom CSV destination
fb-rent export --html-path /tmp/rentals.html # custom HTML destination
```

## `fb-rent ai-status`

The dashboard for both passes. Prints the registry version and field count,
then the text queue — analysed, pending (with the reason: never analysed,
description changed, registry grew), stale, and how many listings have no raw
text at all — then the photo blocks: the photo queue, listings with no pictures
on disk, and the photo-hash backfill queue (`Фото-хэши: N из M`).

Listings with no raw text are printed by `fb_id`; they need
`fb-rent scrape --ids <fb_id>` before there is anything to analyse.

## `fb-rent ai-batch`

Writes the JSON batch the text pass reads: the requirements letter, the field
registry and the raw text of each pending listing.

| Flag       | Default | Description                                             |
|------------|---------|---------------------------------------------------------|
| `--limit`  | `25`    | Maximum number of listings in the batch.                |
| `--all`    | off     | Include every pending listing, ignoring `--limit`.      |
| `--ids`    | —       | Analyse only these `fb_id`s (repeatable).               |
| `--out`    | —       | Write elsewhere (default `../data/ai/batch-<ts>.json`). |

Listings already analysed against the current registry are not returned again —
the loop is idempotent, so re-running `ai-batch` right after `ai-apply` yields
an empty queue. A listing comes back only if its description changed or the
registry grew and you asked for a backfill.

## `fb-rent ai-apply`

Reads the result file, validates it against the registry, writes the fields
into `ai_analysis`, applies `rules.py` and reports what happened: applied,
auto-rejected and why, skipped, and any new fields the AI proposed.

```sh
fb-rent ai-apply ../data/ai/result-20260825-141500.json --dry-run
fb-rent ai-apply ../data/ai/result-20260825-141500.json
```

## `fb-rent ai-field`

Evolving the schema — the registry of columns the AI fills.

```sh
fb-rent ai-field list
fb-rent ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear
```

| Flag         | Description                                                        |
|--------------|--------------------------------------------------------------------|
| `--key`      | Key used inside the analysis `data_json`.                          |
| `--label`    | Column heading in the HTML export.                                 |
| `--type`     | `enum`, `int`, `text` or `date` (`--values` required for enum).     |
| `--group`    | Column group (default `core`).                                     |
| `--prompt`   | Extraction instruction handed to the AI.                           |
| `--after`    | Insert right after an existing key instead of at the end.          |
| `--backfill` | Re-queue already-analysed listings so the AI fills the new field.  |
| `--source`   | Which pass fills the field: `text` (default), `photo` or `both`.   |
| `--registry` | Registry file to read/modify (default `config/ai_fields.json`).    |

What `--backfill` actually moves, and how an unfilled new column is rendered
differently from an empty one, is in
[ai-enrichment.md](ai-enrichment.md#evolving-the-schema).

## `fb-rent photos-fetch`

Downloads the galleries the photo pass is waiting for, from the links the
detail page left behind in `raw_json`. `scrape` already does this inline; this
command exists for the wide-sweep order of work.

| Flag      | Default | Description                                          |
|-----------|---------|------------------------------------------------------|
| `--limit` | `25`    | Maximum number of listings to fetch galleries for.   |
| `--all`   | off     | Fetch the whole queue, ignoring `--limit`.           |
| `--ids`   | —       | Fetch these `fb_id`s instead of the queue.           |

The queue is what `ai-status` prints under "Фото / No photos": listings the
text pass has read, that neither the rules nor you have rejected, and that have
no pictures on disk. A directory that already holds photos is left alone —
re-downloading would change `photos_hash` and push an analysed listing back
into the queue for pictures that did not change.

A listing whose signed links have expired, or that was scraped before galleries
were collected at all, is named in the output together with the command that
fixes it: `fb-rent scrape --ids <fb_id>`.

## `fb-rent photo-batch` / `fb-rent photo-apply`

`photo-batch` mirrors `ai-batch` flag for flag (`--limit` 25, `--all`, `--ids`,
`--out`); `photo-apply` takes `--dry-run`. The batch ships only the fields
whose `source` is `photo` or `both`, along with what the text pass claimed for
them, so the model can see what it is contradicting.

`photo-apply` prints the discrepancies (`fb_id.key: old -> new`) — the actual
product of the pass — then writes into the photo columns, merges on read and
recomputes `decide` and `score`.

A result whose `photos_hash` no longer matches what is on disk is **skipped**,
not written. Text can be re-read; an analysis of photos that have since changed
describes a different flat.

## `fb-rent photo-hash`

Backfills the duplicate signal for listings scraped before the hash stage
existed. Fetches the first three gallery shots from the same signed links
`photos-fetch` uses, stores their MD5 and dHash in `photo_hashes`, and writes
nothing to disk.

| Flag      | Default | Description                                          |
|-----------|---------|------------------------------------------------------|
| `--limit` | `100`   | Maximum number of listings to hash.                  |
| `--all`   | off     | Hash the whole queue, ignoring `--limit`.            |
| `--ids`   | —       | Hash these `fb_id`s instead of the queue.            |

The queue is every listing with gallery links and no hashes, oldest first. New
scrapes need none of this: they hash inline, `--no-photos` included.

## Your own decisions

The AI's verdict is a suggestion. Yours wins, and unlike everything else it
survives a DB wipe — it lives in `../data/decisions.json`.

```sh
fb-rent reject 1234567890 --reason "агент не отвечает"
fb-rent unreject 1234567890 --reason "перезвонили, животные ок"
fb-rent shortlist 1234567890 --reason "поехать смотреть в четверг"
fb-rent rate 1234567890 4.5          # a 1-5 mark that sets the status itself
fb-rent export                       # refresh the table
```

`reject` takes a listing out (overriding an AI `active`), `unreject` puts it
back (overriding an AI rejection), `shortlist` marks it as worth contacting.

`rate` is the shorthand for reviewing a whole screen of listings: 4.5 or more
shortlists, 3 or less rejects, anything between leaves the listing active
carrying the mark. The mark itself is the record that the listing has been
looked at, so an unmarked row is one still waiting for you — sort on the
**Моя оценка** column to tell the two apart.
