# fb-rent

FB Marketplace Phuket rentals scraper — **iteration 2 (AI enrichment)**.

Scrapes long-term rental listings from the [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals)
category (15 000 – 30 000 THB) into a local SQLite database, with CSV and
self-contained HTML exports for browsing in Numbers/Excel or a browser.

Iteration 2 adds AI enrichment on top: an analysis loop that reads each
listing's raw text, fills the table columns, writes a verdict per listing and
auto-rejects the obvious misses — see [AI enrichment](#ai-enrichment) below.

Design rationale, decisions, and the roadmap for future iterations live in the
plan files:

- iteration 1 (MVP): [`docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md`](docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md)
- iteration 2 (AI enrichment): [`docs/plans/completed/20260825-ai-enrichment.md`](docs/plans/completed/20260825-ai-enrichment.md)

## Install

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

The `-e` is required, not a convenience: `config/ai_fields.json` and
`config/requirements.md` are read from the checkout through a path relative to
the package source and are not packaged into a wheel. After a plain
`pip install .` every `ai-*` command and `fb-rent export` fail to find the
field registry.

## Quickstart

```sh
fb-rent login          # one-time headed login, session persisted to ../data/
fb-rent scrape         # headless scrape, writes ../data/rentals.{db,csv,html}
fb-rent export         # regenerate CSV+HTML from the DB without scraping
```

Then run the analysis loop from a Claude Code session (Opus, high thinking
effort):

```
/analyze               # ai-status -> ai-batch -> read -> ai-apply -> export
```

Open `../data/rentals.html` in a browser: one self-contained file, no server
and no internet needed. Click a header to sort, type in the search box to
filter across every column, switch sets with the Active / Shortlist /
Rejected / All tabs. `../data/rentals.csv` opens in Numbers/Excel, but it
carries the scraped columns only — the AI fields, verdict, score and your
decisions live in the HTML export.

## Local state

Everything lives in the sibling `data/` directory (i.e. `../data/` relative
to this `scrapper/` package):

- `browser-profile/` — persistent Chromium profile (cookies, localStorage)
- `rentals.db` — SQLite database (source of truth)
- `rentals.csv` — flat export of the scraped `listings` columns only,
  regenerated on every scrape
- `rentals.html` — the full table: AI fields, verdict, score and decisions.
  Self-contained, no CDN and no network; regenerated on every scrape
- `ai/` — batches written by `fb-rent ai-batch` and the result files you hand
  back to `fb-rent ai-apply` (`batch-<ts>.json` / `result-<ts>.json`);
  disposable, they are just the transport between the CLI and the AI
- `decisions.json` — your own verdicts (`reject` / `unreject` / `shortlist`)
  with reasons. **The only irreplaceable file here**: the DB can be rebuilt by
  re-scraping and re-analysing, your decisions cannot. They are mirrored into
  the DB on every `export`, so wiping `rentals.db` loses nothing.

Editable configuration is version-controlled inside this package instead:

- `config/ai_fields.json` — the field registry: every column the AI fills, its
  type, allowed values and extraction prompt, plus `schema_version`. An
  `analysis_version` key appears once `ai-field add` has run; while it is
  absent it is read as equal to `schema_version`
- `config/requirements.md` — what we are looking for, in prose. It is pasted
  verbatim into every batch, so changing priorities means editing this markdown,
  not the code.

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
| `--refresh`       | off     | Re-scrape detail pages for listings already in the DB.   |

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

## AI enrichment

The scraper only gets you raw text. The enrichment loop turns that text into
filled columns, a per-listing verdict and a score, so the search happens on one
HTML page instead of 40 Facebook tabs.

The "AI" here is Claude Code itself (Opus, high thinking effort) — there is no
API client and no extra runtime dependency. The CLI's job is the contract:
write a batch out, read a result back.

```
fb-rent ai-status                 # what is analysed / pending / stale / unusable
fb-rent ai-batch --limit 25       # -> ../data/ai/batch-<ts>.json
   ... the AI reads the batch + config/requirements.md and writes a result file
fb-rent ai-apply <result.json>    # store fields, verdicts, auto-rejections
fb-rent export                    # regenerate CSV + HTML
```

In a Claude Code session, `/analyze` drives that whole loop for you (skill:
`.claude/skills/analyze/SKILL.md`).

Extraction is the AI's job; the *decision* is not. `rules.py` computes the
`active` / `rejected` verdict and the score from the extracted fields, so the
same facts always produce the same outcome. Exactly three conditions
auto-reject: pets explicitly forbidden, a price above 35 000 THB (the hard
ceiling above the 15 000–30 000 target), and an explicit studio. Everything
else — a condo, no parking, an unmentioned living room — is a remark in the
verdict, never a rejection.

Alongside the fields the AI records `data_quality` per listing: `good` (it
read the ad itself), `sidebar_noise` (Facebook's "Выбор дня" block landed in
the text instead of the ad) or `missing` (no ad text at all). Anything other
than `good` is amber in the export and means *re-scrape this one*
(`fb-rent scrape --refresh`); it also suppresses the auto-rejections, because
a verdict drawn from the wrong text is not evidence about this listing.

### `fb-rent ai-status`

Prints the registry version and field count, then the queue: how many listings
are analysed, pending (with the reason — never analysed, description changed,
registry grew), stale, and how many have no raw text at all. Listings in that
last bucket are listed by `fb_id` — they need `fb-rent scrape --refresh` before
there is anything to analyse.

### `fb-rent ai-batch`

Writes the JSON batch the AI reads: the requirements letter, the field registry
and the raw text of each pending listing.

| Flag       | Default | Description                                             |
|------------|---------|---------------------------------------------------------|
| `--limit`  | `25`    | Maximum number of listings in the batch.                |
| `--all`    | off     | Include every pending listing, ignoring `--limit`.      |
| `--ids`    | —       | Analyse only these `fb_id`s (repeatable).               |
| `--out`    | —       | Write elsewhere (default `../data/ai/batch-<ts>.json`). |

```sh
fb-rent ai-batch --limit 25
fb-rent ai-batch --all
fb-rent ai-batch --ids 1234567890 --ids 9876543210   # re-analyse two by hand
```

Listings already analysed against the current registry are not returned again —
the loop is idempotent, so re-running `ai-batch` after `ai-apply` yields an
empty queue. A listing comes back only if its description changed or the
registry grew and you asked for a backfill.

### `fb-rent ai-apply`

Reads the AI's result file, validates it against the registry, writes the
fields into the `ai_analysis` table, applies `rules.py` and reports what
happened: how many were applied, which were auto-rejected and why, which were
skipped, and any new fields the AI proposed.

```sh
fb-rent ai-apply ../data/ai/result-20260825-141500.json --dry-run
fb-rent ai-apply ../data/ai/result-20260825-141500.json
```

`--dry-run` validates and reports without touching the DB.

### `fb-rent ai-field` — evolving the schema

When the AI keeps seeing a useful signal that has no column, it proposes one in
`proposed_new_fields`, and `ai-apply` prints the proposal. You add it:

```sh
fb-rent ai-field list
fb-rent ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear
fb-rent ai-field add --key balcony --label Balcony --type enum --values yes,no,unclear --backfill
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
| `--registry` | Registry file to read/modify (default `config/ai_fields.json`).    |

Every `add` bumps `schema_version`. What `--backfill` changes is
`analysis_version`:

- **without `--backfill`** — old rows keep their analysis and show a `c-stale`
  cell (hatched grey) for the new field. That means *"the field did not exist
  when this listing was analysed"* — as opposed to `c-none` (plain grey dash),
  which means *"the AI looked and there was nothing to find"*. Nothing is
  re-queued.
- **with `--backfill`** — `analysis_version` catches up to `schema_version`,
  every analysed listing becomes pending again, and the next `ai-batch` picks
  them up so the new column gets filled for the whole table.

### Your own decisions

The AI's verdict is a suggestion. Yours wins, and unlike everything else it
survives a DB wipe — it lives in `../data/decisions.json`, not in SQLite.

```sh
fb-rent reject 1234567890 --reason "агент не отвечает"
fb-rent unreject 1234567890 --reason "перезвонили, животные ок"
fb-rent shortlist 1234567890 --reason "поехать смотреть в четверг"
fb-rent export                       # refresh the table
```

`reject` takes a listing out (overriding an AI `active`), `unreject` puts it
back (overriding an AI rejection), `shortlist` marks it as worth contacting.
The HTML export has an Active / Shortlist / Rejected / All tab strip, shows the
reason on rejected rows, and marks shortlisted ones with a yellow bar.

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
