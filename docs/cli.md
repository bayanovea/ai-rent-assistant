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
| `--max-price`     | `35000` | Upper bound of the price window, in THB.                 |
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

## `fb-rent scrape-groups`

Same pipeline as `scrape`, second source: reads rental posts out of
Facebook groups (`config.GROUPS`) into the same `listings` table, tagged
`source = "group:<id>"`. No price bands or category — a group has one
chronological feed, walked by scroll rather than by query.

| Flag              | Default       | Description                                              |
|-------------------|---------------|------------------------------------------------------------|
| `--group`         | every group in `config.GROUPS` | Group id/slug to scrape (repeatable). |
| `--max`           | `40`          | Maximum posts to collect per group.                       |
| `--days`          | `7`           | Stop scrolling a group's feed once posts read older than this. |
| `--dry-run`       | off           | Print what would be written without touching the DB.      |
| `--refresh`       | off           | Re-scrape detail pages for posts already in the DB.        |
| `--no-photos`     | off           | Skip downloading post photos (faster, no photo pass).     |
| `--detail-cap`    | `60`          | Maximum detail pages to open in one run.                   |
| `--ids`           | —             | Scrape these post `fb_id`s directly. Requires one `--group`. |

```sh
fb-rent scrape-groups --max 5 --detail-cap 2 --dry-run   # smoke check
fb-rent scrape-groups                                     # every group, regular run
fb-rent scrape-groups --group phuketrentsale --max 20

# repair path: open one post whatever the DB already holds
fb-rent scrape-groups --group phuketrentsale --ids 9000000000000003
```

`scrape --ids` on an id already in the DB also works for a group post — it
looks up the stored `url`/`source` first, so it never guesses a marketplace
URL for one. Why the feed has to be scrolled incrementally, how a post's own
article is told apart from an unrelated one FB renders next to it, and why
group-post galleries skip the alt-grouping trick entirely, is in
[scraping.md](scraping.md#facebook-groups--a-second-source-one-table).

## `fb-rent dedupe`

Merges listings that describe the same flat, using the photo and fact signals
that need no matching text. `--exact-only` restricts it to byte-identical text
and matching photos; `--dry-run` reports without writing. Whatever is merged is
remembered in `duplicates`, so the next scrape skips it without opening its
page. See [dedupe.md](dedupe.md).

```sh
fb-rent dedupe                          # the whole base — Shift+D in the table
fb-rent dedupe 9000000000000004         # one row — d in the table
fb-rent dedupe 9000000000000004 --into 9000000000000005
```

With an id it answers the other question — "I have seen this flat before,
find it" — and merges that row into the copy you already decided on. Nothing
is merged on a guess: either a hard signal ties the row to another one, or the
nearest rows are printed with the `--into` line that merges each, and the DB is
untouched. `--into` is the only way a merge happens without a signal; the row
you name is the keeper, whatever it carries.

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
on disk, and the photo-hash backfill queue (`Фото-хэши: N из M`). It ends with
the `Здания:` block — how many buildings the pets pass has never researched,
how many are proposed and how many you confirmed. A non-empty queue there is
not an invitation: that pass runs when you ask for it.

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

## `fb-rent buildings-batch` / `fb-rent buildings-apply`

The pets pass, keyed by building rather than by listing — one house rule
answers for every flat in the tower. Full story in
[pets-policy.md](pets-policy.md).

`buildings-batch` writes `../data/ai/buildings-batch-<ts>.json`: one entry per
distinct building over the **active** listings that name a `project_name`,
ordered by how many listings it covers, together with the `pets` values those
ads claimed so a contradiction is visible up front.

| Flag        | Default | Description                                            |
|-------------|---------|--------------------------------------------------------|
| `--limit`   | `25`    | Maximum number of buildings in the batch.               |
| `--key`     | —       | Research only these buildings (repeatable); a name or a key. |
| `--refresh` | off     | Include buildings already in the store.                 |

`buildings-apply <file>` reads the result back and stores every entry as
**`proposed`**, which changes nothing: no listing's status moves, and the
policy stays out of `rules.decide` until you confirm it. `--dry-run` validates
and reports without writing.

The whole file is refused before anything is written if a policy is not one of
`not_allowed | negotiable | allowed | unknown`, if an entry has no key, or if a
`not_allowed` arrives with no source. A ban with no verbatim quote is not a
finding.

A ban that lets the juristic office grant exceptions counts as `not_allowed`,
not `negotiable` — see [pets-policy.md](pets-policy.md).

## `fb-rent buildings`

The review side of the same pass.

```sh
fb-rent buildings list --status proposed      # policy, evidence, affected rows
fb-rent buildings list --policy not_allowed
fb-rent buildings set the-base-central --policy not_allowed \
    --note "УК: no pets" --alias "base central"
fb-rent buildings conflicts                   # ads that argue with the rule
fb-rent export
```

`list` prints each building with its policy, status, confidence, area,
aliases, note, every source URL with its quote, and how many still-active
listings it covers.

`set` is your confirmation and the only thing that makes a policy act: it
writes `status: "confirmed"`, syncs the mirror table and tells you how many
listings a `not_allowed` will reject. KEY is the stored key, the building's
name or any of its aliases. `--alias` is how a second spelling is pointed at an
existing building — there is no fuzzy matching, on purpose. Aliases are only
ever added, never dropped.

`set` re-exports nothing; run `fb-rent export` to refresh the table.

`conflicts` prints the buildings researched as `not_allowed` whose ads keep
saying the opposite — two or more of them claiming `pets: allowed`
(`--min-signals` moves the bar). Those listings are **not** rejected: an
outright yes in the ad survives the building's rule, because it is the more
specific claim about that flat. A pile of them in one tower is the signal that
the research read the wrong building, and this is where it surfaces —
`ai-status` shows the count on its «Расхождения» line. Nothing here changes a
status; it is a queue for your eyes.

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
shortlists, 3 or less rejects, anything between goes to the **longlist** — the
middle drawer, kept to hand. From the keyboard that reads 1–3 out, 4 to the
longlist, 5 to the shortlist.

No mark ever leaves a listing in the active set, and that is the point: the
Разобрать tab is the queue of what has not been looked at, so a row still on
it is a row still waiting for you. Nothing has to be sorted or filtered to
see what is left.
