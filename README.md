# fb-rent

FB Marketplace Phuket rentals scraper — **iteration 2 (AI enrichment)**.

Scrapes long-term rental listings from the [Phuket Property Rentals](https://www.facebook.com/marketplace/110729412288885/propertyrentals)
category (15 000 – 30 000 THB) into a local SQLite database, with CSV and
self-contained HTML exports for browsing in Numbers/Excel or a browser.

Iteration 2 adds AI enrichment on top: an analysis loop that reads each
listing's raw text, fills the table columns, writes a verdict per listing and
auto-rejects the obvious misses — see [AI enrichment](#ai-enrichment) below.

Iteration 3 adds a second pass over the listing *photos*, which checks what the
text actually promised — see [Photo analysis](#photo-analysis).

Design rationale, decisions, and the roadmap for future iterations live in the
plan files:

- iteration 1 (MVP): [`docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md`](docs/plans/completed/20260824-fb-marketplace-scraper-mvp.md)
- iteration 2 (AI enrichment): [`docs/plans/completed/20260825-ai-enrichment.md`](docs/plans/completed/20260825-ai-enrichment.md)
- iteration 3 (photo analysis): [`docs/plans/completed/20260825-photo-analysis.md`](docs/plans/completed/20260825-photo-analysis.md)

## Install

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

Dependencies are Playwright (the scrape), Click (the CLI), python-dateutil (the
date parsing) and Pillow (`pillow>=10`, the photo downscale — see
[Photo analysis](#photo-analysis)).

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
/photo-analyze         # second pass over the listing photos, see below
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
- `rentals.db` — SQLite database (source of truth). Deduplication happens on
  three levels: on `fb_id`, on the ad text, and on the flat itself. Agencies
  republish the same listing to lift it up the feed, and Facebook mints a new
  id every time; the same flat is also often handed to several agents, who
  each write their own text. A listing whose text and price already exist is
  recorded in `duplicates` at write time instead of getting its own row, and
  `fb-rent dedupe` additionally merges listings that describe one flat —
  matching on the project, price and bedroom count, and refusing to merge two
  ads that name different floors, buildings or sizes. Whatever is merged is
  remembered, so the next scrape skips it without opening its page. The table
  shows one line per flat, with every posting's id and link in its ID cell
- `rentals.csv` — flat export of the scraped `listings` columns only,
  regenerated on every scrape
- `rentals.html` — the full table: AI fields, recommendation, score and
  decisions. Columns are banded by where the value came from — parsed off the
  FB page or extracted by the AI — and the rarely-scanned fields fold away
  behind the "Ещё N полей" toggle. Self-contained, no CDN and no network;
  regenerated on every scrape
- `photos/<fb_id>/` — the listing's whole gallery, `01.jpg`, `02.jpg`, …,
  downscaled to 1024 px on the long edge. Written during `scrape` or by
  `fb-rent photos-fetch`, read by the photo pass,
  and disposable: a directory deleted (or a listing re-scraped with
  `--refresh`) is simply downloaded again. Auto-rejecting a listing or merging
  it away in `fb-rent dedupe` purges its directory
- `ai/` — batches written by `fb-rent ai-batch` and the result files you hand
  back to `fb-rent ai-apply` (`batch-<ts>.json` / `result-<ts>.json`), plus the
  photo pass's own pair (`photo-batch-<ts>.json` / its result file);
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
| `--no-photos`     | off     | Skip downloading listing photos (faster, no photo pass). |
| `--detail-cap`    | `60`    | Maximum detail pages to open in one run.                 |
| `--min-price`     | `15000` | Lower bound of the price window, in THB.                 |
| `--max-price`     | `30000` | Upper bound of the price window, in THB.                 |
| `--bands`         | `1`     | Split that window into N queries and sweep them all.     |
| `--category`      | Phuket  | Marketplace category/location id to search.              |
| `--ids`           | —       | Scrape these fb_ids directly, skipping the feed.          |

The detail budget counts pages actually opened, not cards walked: listings
already in the DB and known reposts are skipped without spending it, so a
sweep over a feed whose first screens are all familiar still reaches the new
listings underneath. Raise it with `--detail-cap` for a wide run — at 3–7 s of
jitter plus the page load, budget roughly 10 s per detail page.

Listings are written to the DB one at a time, as they are scraped. A run that
dies half way through (expired session, checkpoint, timeout) keeps everything
up to that point; only the CSV/HTML exports are left stale, and `fb-rent
export` refreshes them.

A listing already in the DB is re-seen as a bare list card — its detail page is
not opened again — and such a sighting only refreshes what it actually carries.
It never clears the ad text, `published_at` or the gallery URLs that the detail
page put there.

#### Repairing single listings

`--ids` skips the feed and opens the named listings directly, whatever the DB
already holds for them:

```sh
fb-rent scrape --ids 1234567890 --ids 9876543210
```

That is the repair path for the two things `ai-status` complains about: a
listing whose stored text is Facebook's sidebar instead of the ad
(`data_quality: sidebar_noise`), and one scraped before galleries were
collected, whose photos cannot be downloaded because the links were never
stored. `--refresh` re-walks the feed and only reaches such a listing if it is
still in it; `--ids` always does. An id with nothing behind it any more
(deleted listing) is reported and skipped.

#### Widening the search

One query is a ceiling, not just a filter. `sortBy=creation_time_descend` gives
the freshest listings first, but the feed stops handing out new cards after a
few hundred however far the scraper scrolls — `--max 500` does not collect five
times what `--max 100` does. The way past it is to ask several narrower
questions: `--bands N` splits the `--min-price` … `--max-price` window into N
adjacent ranges and runs the list scrape once per range, each with its own
`--max`. The bands touch but never overlap, so a listing belongs to exactly
one of them; a card that shows up in two anyway (Facebook leaks results past
its own filters) is scraped once.

Cards are then taken band by band in rotation — the freshest of the cheap
band, the freshest of the dear one, and so on. Order matters because the
detail budget is shared by the whole run: taken band after band, a `--bands 4`
sweep with a budget of 60 would spend all of it inside the cheapest quarter of
the price range.

An empty band is an answer, not a failure — the sweep continues. Only a run
where *every* band came back empty is reported as a dead session.

`--days` widens the other axis — the number goes straight into
`daysSinceListed` and Facebook decides what it honours — and `--category`
points the same machinery at another Marketplace category/location id.

Examples:

```sh
fb-rent scrape --max 10 --dry-run       # smoke check, no writes
fb-rent scrape --max 40                 # regular run
fb-rent scrape --max 60 --skip-details  # fast incremental sweep

# wide sweep: 4 price bands x 60 cards, 150 detail pages, photos later
fb-rent scrape --bands 4 --max 60 --days 30 --detail-cap 150 --no-photos
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
filled columns, a per-listing recommendation and a score, so the search happens
on one HTML page instead of 40 Facebook tabs. Each listing gets a one-line
`verdict` ("Стоит посмотреть — дом с гостиной в бюджете") for the table plus a
longer `comment` holding the reasoning and whatever facts have no column yet.

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
same facts always produce the same outcome. Exactly five conditions
auto-reject: pets explicitly forbidden, Patong, a price above 35 000 THB (the
hard ceiling above the 15 000–30 000 target), an explicit studio, and a stated
living area under 35 m². An ad that never names its size is *not* rejected —
only a number below the floor is. The district is also worth up to ±12 points
of the score on the 1–5 scale in `config/requirements.md`; an area the ad never
names costs nothing. Everything else — a condo, no parking, an
unmentioned living room — is a remark in the comment, never a rejection.

Alongside the fields the AI records `data_quality` per listing: `good` (it
read the ad itself), `sidebar_noise` (Facebook's "Выбор дня" block landed in
the text instead of the ad) or `missing` (no ad text at all). Anything other
than `good` is amber in the export and means *re-scrape this one*
(`fb-rent scrape --ids <fb_id>`); it also suppresses the auto-rejections, because
a verdict drawn from the wrong text is not evidence about this listing.

### `fb-rent ai-status`

Prints the registry version and field count, then the queue: how many listings
are analysed, pending (with the reason — never analysed, description changed,
registry grew), stale, and how many have no raw text at all. Listings in that
last bucket are listed by `fb_id` — they need `fb-rent scrape --ids <fb_id>`
before there is anything to analyse.

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
| `--source`   | Which pass fills the field: `text` (default), `photo` or `both`.   |
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
fb-rent rate 1234567890 4.5          # a 1-5 mark that sets the status itself
fb-rent export                       # refresh the table
```

`reject` takes a listing out (overriding an AI `active`), `unreject` puts it
back (overriding an AI rejection), `shortlist` marks it as worth contacting.

`rate` is the shorthand for reviewing a whole screen of listings: a mark of
4.5 or more shortlists the listing, 3 or less rejects it, and anything between
leaves it in the active set carrying the mark. The mark itself is the record
that the listing has been looked at, so an unmarked row is one still waiting
for you — sort on the **Моя оценка** column to tell the two apart.

The HTML export has an Active / Shortlist / Rejected / All tab strip, shows the
reason on rejected rows, and marks shortlisted ones with a yellow bar.

## Photo analysis

The text pass believes the ad. The photo pass checks it: "fully furnished" is
in every listing, and only the picture says what that means. Its main product
is therefore not new facts but **disagreements with the text** — a `condition`
that was `unclear` in prose and is visibly worn in the photo, a `kitchen`
promised as full and shown as a kitchenette, a listing whose gallery is a 3D
render rather than a photo of the actual flat (`photo_quality: render`).

Photos are downloaded during `fb-rent scrape` (skip with `--no-photos`), taken
straight from the detail page's DOM — the gallery is there without opening the
lightbox. What keeps the pass affordable is not sampling the pictures but
choosing which listings get looked at at all:

- **survivors only, and the user decides who survives.** The queue skips
  anything already rejected, and a hand-written verdict in `decisions.json`
  outranks the rules in both directions — a listing you reject never reaches
  the batch, one you keep does even when the rules would drop it. Review the
  table first, reject what you can see is wrong, and the pass runs on what is
  left
- **the whole gallery** is downloaded and read. Downloading is cheap and a
  missed room is not; `PHOTO_MAX_COUNT` in `config.py` can cap it (the cover
  plus an even spread over the rest) if a run ever needs rationing
- **1024 px on the long edge**, JPEG q80 — ~920 tokens per photo. Mould in a
  corner and wall texture are still legible. Facebook hands out 960 px anyway,
  so this is in practice "do not upscale"

The reader is a **Sonnet subagent, one per listing**: the images never enter
the orchestrator's context at all, and up to ~12 subagents run at once, so 50
listings finish in minutes. Sonnet is enough because the questions are
concrete and visual ("is there a sofa", "is this a render") rather than
judgement calls — the judgement stays in `rules.py`. Bumping the fan-out to
Opus is one line in the skill if a batch proves it necessary.

```
fb-rent ai-status                  # the "Фото" block: analysed / pending / no photos
fb-rent photos-fetch               # download the galleries the queue is missing
fb-rent photo-batch --limit 25     # -> ../data/ai/photo-batch-<ts>.json
   ... one subagent per listing reads its 6 files and returns JSON
fb-rent photo-apply <result.json>  # store photo fields, recompute rules
fb-rent export                     # regenerate CSV + HTML
```

### `fb-rent photos-fetch`

Downloads the galleries the photo pass is waiting for, from the links the
detail page left behind in the listing's `raw_json`. `scrape` already does
this inline, so this command is for the wide-sweep order of work: sweep with
`--no-photos` (a gallery costs a second or two per listing and hundreds of
megabytes over a big run), analyse the text, reject by hand, and only then
spend the downloads on the listings that are left.

| Flag      | Default | Description                                          |
|-----------|---------|------------------------------------------------------|
| `--limit` | `25`    | Maximum number of listings to fetch galleries for.   |
| `--all`   | off     | Fetch the whole queue, ignoring `--limit`.           |
| `--ids`   | —       | Fetch these `fb_id`s instead of the queue.           |

The queue is the same one `ai-status` prints under "Фото / No photos":
listings the text pass has read, that neither the rules nor you have rejected,
and that have no pictures on disk. A directory that already holds photos is
left alone — re-downloading would change `photos_hash` and push an analysed
listing back into the queue for pictures that did not change.

Those gallery links are signed and expire in about a week. A listing whose
links have gone stale, or that was scraped before galleries were collected at
all, is named in the output together with the command that fixes it —
`fb-rent scrape --ids <fb_id>` reads the page again and stores fresh links.

In a Claude Code session, `/photo-analyze` drives that loop (skill:
`.claude/skills/photo-analyze/SKILL.md`); `/analyze` offers to hand over to it
once the text pass leaves a non-empty photo queue.

`photo-batch` mirrors `ai-batch` flag for flag (`--limit` 25, `--all`, `--ids`,
`--out`) and `photo-apply` takes `--dry-run`. Which fields a pass may fill is
declared in the registry: each field carries `source` — `text` (the default),
`photo`, or `both` — and the photo batch ships only the `photo`/`both` subset,
along with what the text pass already claimed for those fields, so the model
can see what it is contradicting. Photo fields have their own
`photo_analysis_version`, so `ai-field add --source photo --backfill` re-queues
the photo pass without disturbing the text one.

`photo-apply` writes into separate columns and merges on read
(`{**text, **photo}`), so the two passes never overwrite each other: dropping
the photo columns leaves the text analysis intact. After writing it recomputes
`rules.decide` and `rules.score` over the merged fields, which means the photo
pass can auto-reject a listing (a photographed studio, a size that turns out
below the floor) and always shifts the score. In the HTML export photo fields
get their own band, and a cell where the photo overrode a non-empty text value
carries a badge with the old value in its tooltip.

A result whose `photos_hash` no longer matches what is on disk is **skipped**,
not written — unlike the text pass, which re-writes anyway. Text can be
re-read; an analysis of photos that have since changed describes a different
flat, and storing it would attribute someone else's observations to this
listing.

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
