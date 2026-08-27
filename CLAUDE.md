# fb-rent — notes for Claude

Traps only. Each entry below is something the code depends on that reading the
code does not reveal, and that a plausible-looking change would break. How the
project works, what the commands do and why the design is what it is live in
[`docs/`](docs/README.md) — start at [`docs/pipeline.md`](docs/pipeline.md).

**Coming back to the photo pass? Read `docs/photo-analysis.md` first.** It
carries the live measurements, the decisions worth not undoing, and the open
gaps.

## Working in this repo

**Commit a coherent block the moment it is done — do not wait to be asked.**
One commit is one change that stands on its own: the fix apart from the docs
that describe it, the diagram apart from the prose that embeds it. Get the
tests green first. Work left uncommitted at the end of a task is work the next
session has to reconstruct.

**Straight into `main`.** No branches, no PRs — one person works here and there
is no remote. Do not open a branch "to be safe": it strands the work where
nobody will look for it. Commit messages follow the log's existing style —
Russian, with a conventional-commit prefix, and a body that says *why* when the
why is not obvious.

## Photos

**The gallery is in the DOM without a click.** Every detail page ships 10–28
`img` elements from the gallery in `page.content()`; opening the lightbox is
neither necessary nor possible (an overlay eats the pointer events on the
cover). `extract_gallery_urls` picks them locale-independently: `img` with
`/t39.30808-6/` in `src` and a non-empty `alt`, **not inside an
`a[href*="/marketplace/item/"]`**, grouped by `alt`, largest group wins
(gallery photos share one `alt`, "similar listings" cards each have their own).
Do not filter on `aria-label` numbering (locale-dependent) or on `cstp=mx`
(drags in the 565×565 recommendation cards).

**The link test is what makes an absent gallery an empty list.** Some pages
render no gallery at all — waiting and scrolling do not bring it back — and on
those the largest `alt` group is a group of *suggestions*. Grouping ranks
groups against each other and cannot tell that none of them is this listing's,
so it hands back somebody else's flat with a straight face; it happened to six
rows on 28 August. A suggestion card is wrapped in a link to another ad and a
gallery shot is in no link at all, which is the only signal that separates
"this is not the gallery" from "this is a smaller gallery". Drop the test and
the bad rows come back silently — and worse than silently, because the cards
get downloaded, `photos_hash` goes non-empty and the listing counts as
photographed forever.

**`context.request.get` works on signed CDN URLs.** The persistent browser
context shares its cookies with its request client, so no separate HTTP client
and no cookie plumbing is needed — 6 files download in 1–2 s. Facebook serves
960 px on the long edge, so the 1024 px cap in `config.py` is effectively "do
not upscale".

**Galleries can be fetched later, but not much later.** The stored
`gallery_urls` are signed and expire in about a week, which is what makes
`photos-fetch` possible at all: sweep wide with `--no-photos`, analyse, reject
by hand, then download for the survivors. Past the expiry only the detail page
can hand out fresh links — `scrape --ids <fb_id>`, which is also the repair
path for `sidebar_noise` and for listings scraped before galleries were
collected.

**`photos_hash` is the photo pass's `content_key`.** It is what re-queues a
listing, so anything that rewrites the directory re-queues it. `_download_photos`
therefore leaves a non-empty directory alone; a genuine refetch means purging
first.

**The hash stage must never touch `PHOTOS_DIR`.** `fingerprint.py` downloads
the first three shots, hashes them and drops the bytes; nothing is written.
Save those three previews into `data/photos/<fb_id>/` and the photo pass is
broken forever: `_download_photos` skips a directory that already has photos (`cli.py`),
so the real gallery never arrives, and `photos_hash` — computed from file names
and sizes — happily marks the listing as ready with three pictures in it.

**Hashes come from the CDN bytes, not from the files on disk.** The fingerprint
stage re-downloads the three shots even in a run that is already downloading
the gallery. Reusing `download_and_resize`'s bytes looks free and is not: `md5`
would then depend on whether it was taken before or after the resize, and on a
shot's position among whichever URLs happened to survive. Three extra GETs fit
inside the jitter that is already paid for, and keep the stage orthogonal to
the photo pass — which is why it also runs under `--no-photos` (three shots are
not a gallery) and stops only under `--dry-run`.

## Scrape

**A re-sighting must not clobber.** `scrape` skips the detail page of a
listing already in the DB, so what comes back is a bare list card with no
description, no `published_at` and no `raw_json`. `upsert_listing` therefore
drops `None` values on update: writing them erased the ad text, the gallery
URLs and the `content_key` on every run that re-saw the table — which a wide
sweep does by definition.

**A price band is a question, not a filter.** One query hands out a few hundred
cards and then stops, so volume comes from `--bands N`. Two consequences the
code encodes: `scrape_list(allow_empty=True)` during a sweep, because an empty
band says nothing about the session (only *every* band empty does), and a
round-robin over the bands rather than concatenation, because the detail budget
is shared and band-after-band order spends all of it in the cheapest range.

**The detail budget counts pages, not cards.** `details_fetched < detail_cap`,
never `idx < detail_cap`: a sorted feed puts known listings and reposts on the
first screens, and an index-based cap spends the whole budget on cards whose
detail page is skipped anyway. Rows are upserted inside the loop, so a run that
dies at minute 40 keeps its work; the exports are the only stale part.

**A card-only row still counts as known.** `known_fb_ids` (`storage.py`)
returns every `fb_id` in the table, and does not care whether the row has a
description. When the detail budget runs out mid-run the loop keeps writing the
remaining cards as rows with a title and a price and nothing else — and those
rows then make the *next* run skip their detail pages forever. `--refresh` is
the wrong fix (it re-reads the rows that are already complete and burns the
budget on them); the right one is `--ids` over
`select fb_id from listings where description is null`.

**A dead page's `--ids` re-read damages the row.** Description extraction falls
back to "longest text block" and writes the sidebar over real text, and the
gallery it returns can belong to another listing entirely — this has happened,
see `docs/dedupe.md`. Fix the extraction before any mass re-read.

## The table

**A tie at the top is how `rules.score` fails.** Nothing in the code says so,
because each weight looks harmless on its own: the failure is the sum. Points
for a merely non-empty field (`internet`, `pool_gym`, `extra_rooms`), a flat
bonus for the first bedroom, a bonus for a price inside the budget — none of
those separate one survivor from another, and together they once pinned 352 of
1123 active rows at exactly 100, where the default sort could no longer order
the first screen. Before adding a positive weight, re-measure the spread on the
live DB (`fetch_enriched` → count the rows at 100); the ceiling is the budget.

**The verdict tag is derived, and the vocabulary lives in two places.**
`rules.verdict_tag` matches stems against the sentence the AI wrote, and
`.claude/skills/analyze` tells the AI which five words to start that sentence
with. Change one without the other and the column quietly goes blank on every
new row — blank is the deliberate outcome for an unmatched sentence, so nothing
raises. Adding a synonym to the stems is the fix for old rows; fixing the skill
is the fix for new ones. Neither touches `source_hash`, so neither re-queues
anything.

**`data_quality` is a badge, `unclear` is a dash, `Причина` and `Кто решил`
are tab-bound.** All of them look like missing columns and all of them are
deliberate: `good` on 99% of rows, "the ad did not say" on the majority of
five enum columns, and — for the last two — a column of one repeated value on
every tab but Rejected and All. A column says where it lives with `tabs` in
`_FIXED_COLUMNS`; dropping that key does not break anything visibly, it just
puts a full column's width behind information that is already on screen — see
`docs/data-layout.md`.

**A rating never leaves a listing in `active`.** `status_for_rating` sends
every mark somewhere else — 5 to the shortlist, 4 to the longlist, 3 and below
out — because the Разобрать tab is the queue of what has not been looked at,
and one rated row sitting in it is indistinguishable at a glance from an
untouched one. `active` remains a valid status for exactly one writer:
`unreject`, which puts an auto-rejected listing back into that queue. Widening
the middle band so that some mark maps back to `active` silently breaks the
queue — nothing raises, the tab just stops emptying.

**The pending-decision queue is cleared by `export`, not by a button.** The
page drops a queued decision when the row already carries it (same mark,
`decided_by == "user"`), which is why running the commands and re-exporting is
the whole loop. A "clear after copy" would throw away decisions whose commands
were copied but never run.

## Districts

**The first named district wins.** `area` is free text and the AI often names
two places at once (`Cherngtalay, Thalang`, `Kathu, 10 мин до Patong`).
`district_rating` takes the earliest match by position in the string, not the
best and not the worst: the flat is where the ad put it, everything else is a
landmark. Take the maximum and a Thalang townhouse becomes Bang Tao; take the
minimum and a Kathu house is auto-rejected for mentioning Patong.

**The district rating already includes proximity to the sea.** There is no
separate bonus for beach distance and there must not be — a deliberate
decision, not an omission: the sea is a minor plus for this user and it is
baked into the ratings themselves (Bang Tao 5, Chalong 3).

**Score is computed on read, not taken from the column.** `fetch_enriched`
recomputes it through `rules.score`, as it does the verdict — otherwise a
change of weights would never reach rows analysed earlier, and re-running the
AI for the sake of a district weight is pointless. The `ai_analysis.score`
column is written by both passes and remains only a trace of the last run.

## Pets policy per building

**A `proposed` building is inert, and that is the whole safety property.**
`policy_for` hands out nothing until `status == "confirmed"`, so research —
`/pets-policy`, a web search per building — cannot move a single row. Making a
proposal live removes the only thing standing between a bad search result and
a whole building's listings rejected at once. The user confirms with
`buildings set`; nothing else may write that status.

**The building policy carries no score weight, in either direction.** Not an
omission — see the tie-at-100 trap above. A confirmed `allowed` is the tempting
one and is exactly wrong: it would lift the rows already easiest to rank, and
rank a researched building over an identical unresearched one for a reason
about the research rather than the flat. `not_allowed` needs no weight because
it rejects outright. `score()` never reads the policy; keep it that way.

**An ad that says `pets: allowed` outright survives a `not_allowed`
building.** The check in `rules.decide` is guarded on the ad's own field, and
the guard looks like a bug that lets rejected rows through — it is the rule.
The house rule is about the tower, the ad is about the flat, and it is usually
a landlord who already has the permission the rule talks about; dropping the
guard rejects the only listings in a forbidding building worth opening. Only an
outright `allowed` counts: widen it to `unclear` — the value most ads carry —
and the building policy stops rejecting anything at all.

**A ban the juristic office can lift is `not_allowed`, not `negotiable`.**
Almost every Phuket condo carries the same agency-feed sentence («no pets
unless specifically permitted by the juristic office»), and reading it as
"negotiable" is both literally right and useless: the user will not go to the
management for one rented flat. `negotiable` is for the building where animals
already live and only the size, the breed or the deposit is in question. The
wording lives in `buildings_batch.RESEARCH_PROMPT` **and** in
`.claude/skills/pets-policy` — change one without the other and the next batch
argues with its own skill.

**`building_key` is deliberately dumb and must not learn to guess.** Lowercase,
alphanumerics, hyphen-join — it will not merge "The Base" and "Base Central",
and it is not supposed to. A second spelling is pointed at an existing building
by hand through `aliases`. A fuzzy match that got it wrong would silently
reject another building's listings, and nothing would raise.

**`data/buildings.json` is the source of truth; the `buildings` table is a
mirror.** The `decisions.json` argument exactly: a scrape can be repeated and
an AI pass re-run, a web search the user then vetted cannot. `init_db` only
creates the table — it does not fill it — so the file is synced back in
`export` and in `scrape` next to `_sync_decisions`. Drop that call and a wiped
DB exports with every confirmed policy silently inactive until the next
`buildings set`.

## Subagent fan-out

The photo pass reads images in subagents, one per listing
(`.claude/skills/photo-analyze/SKILL.md`) — images never enter the
orchestrator's context. The pets pass does the same with one subagent per
building (`.claude/skills/pets-policy/SKILL.md`), for the same reason: search
results and fetched pages stay out of the orchestrator. Launch the independent ones in a single message so they
run in parallel (~12 at a time). The subagent returns JSON only; it never sets
`verdict`, `score` or `status` — `rules.py` computes those from the fields, and
that boundary is the reason the same facts always produce the same outcome.

## Two passes, two versions

`analysis_version` re-queues the text pass, `photo_analysis_version` the photo
one; `ai-field add --source both --backfill` moves both. Text and photo results
live in separate columns on `ai_analysis` and are merged on read
(`{**data_json, **photo_data_json}`), so neither pass can clobber the other.
Staleness hatching in the export must be computed against the field's own
version — using `ai_schema_version` for a photo field hatches every row.

## Docs upkeep

`docs/scrape-log.md`, `docs/photo-analysis.md` and `docs/pets-policy.md` are
journals: they record what was measured on live data and when. Append to them after a real run, do
not rewrite history in them, and keep the "last checked" dates honest.

**Diagrams are SVGs, not fenced mermaid.** VS Code's markdown preview does not
render mermaid without an extension, so the committed `.svg` is what the docs
embed. Each has a source next to it, and neither is the SVG itself:

- `docs/diagrams/*.mmd` → `scripts/render-diagrams.sh` (borrows the Chromium
  Playwright already installed, adds nothing to the system);
- `pipeline-flow.svg` → `scripts/gen-pipeline-diagram.py`, whose `STAGES`
  table is the thing to edit. **A hand edit to that SVG is lost on the next
  run.** It is not mermaid on purpose: mermaid centres every line of a node at
  one weight, which is what made the earlier version an unreadable block of
  text. Do not "restore" it to mermaid.
