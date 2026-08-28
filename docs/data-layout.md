# What lives where

Two directories matter: `data/` next to the checkout, which holds everything
the machine produced and can mostly reproduce, and `config/` inside the
package, which holds the things you edit and version-control.

Override the base path with `FB_RENT_HOME`, e.g.
`FB_RENT_HOME=/tmp/fb-rent-test fb-rent scrape`.

## `data/` — state, not source

Sibling of this `scrapper/` package, i.e. `../data/`.

| Path | What it is | If you delete it |
|------|------------|------------------|
| `browser-profile/` | persistent Chromium profile: cookies, localStorage | `fb-rent login` again |
| `rentals.db` | SQLite, the source of truth for everything scraped and analysed | re-scrape and re-analyse; your decisions survive |
| `rentals.csv` | flat export of the scraped `listings` columns only | `fb-rent export` |
| `rentals.html` | the full table — AI fields, verdict, score, decisions | `fb-rent export` |
| `photos/<fb_id>/` | the listing's whole gallery, `01.jpg`, `02.jpg`, … | downloaded again by `photos-fetch` while the links live |
| `ai/` | `batch-<ts>.json` / `result-<ts>.json` and the photo pass's pair | nothing — pure transport between CLI and AI |
| `decisions.json` | your verdicts and marks, with reasons | **irreplaceable** |
| `buildings.json` | the pets policy per condo, researched and then confirmed by you | **irreplaceable** |

`decisions.json` and `buildings.json` are the two files here you cannot
rebuild. The DB can be re-scraped and re-analysed; what you thought about a
flat cannot, and neither can a web search you then vetted. Both are mirrored
into the DB on every `export`, so wiping `rentals.db` loses nothing.

Photo directories are disposable but not free: `photos_hash` is computed from
the file names and sizes and is what re-queues a listing for the photo pass, so
anything that rewrites a directory re-queues it. That is why `photos-fetch` and
`scrape` both leave a non-empty directory alone. Auto-rejecting a listing, or
merging it away in `dedupe`, purges its directory.

### The HTML export

One self-contained file: no server, no CDN, no internet. Click a header to
sort, type in the search box to filter across every column, switch sets with
the Разобрать / Shortlist / Longlist / Rejected / All tabs.

**The header is two rows and nothing else.** Where you are — the tabs, with
their counts, and the build stamp in the corner — and how you narrow it down —
search, Фильтры, Кандидаты, Решения, «Ещё N полей». Everything that explains
rather than does sits behind two buttons on the right: «? Клавиши» (the keys
as `kbd` chips, plus what the mouse does — sorting, Shift for a second key,
click-to-unfold, hover for the quote) and «ⓘ Легенда», grouped into colour of
the cell, marks on the row and badges inside cells. Hover opens either one, a
click pins it, `?` opens the keys and `Esc` closes both. That used to be three
paragraphs and a fourteen-item strip of running text above the table — 160px
of the first screen spent on something read twice in the file's whole life.
The row count is gone with them: the Разобрать tab already carries the only
number worth a glance, and it is the one that shrinks as you work.

**The tabs are the four states of a listing, and only the first is a queue.**
Разобрать holds what nobody has looked at yet — 1122 rows of the 2235 today —
and it empties as you work: every mark moves the row out of it. 4.5 and up go
to Shortlist, 4 to Longlist (seen, middling, kept to hand), 3 and below to
Rejected. Nothing rated stays behind, which is what makes "what is left to do"
a tab rather than a filter you have to remember to set. `active` survives as a
status only for `unreject`, which puts an auto-rejected listing back into that
queue.

Both sorting and filtering take more than one column at a time. Shift-clicking
a second header appends it as a tie-breaker instead of replacing the current
key — the arrows are then numbered by priority, and a third shift-click drops
the key back out. The "Фильтры" button opens a panel with one control per
column, and the shape of the control is inferred from the data rather than
declared in the registry: numbers and dates get a from/to pair, a column whose
values are few and short (район, pets, тип жилья, рекомендация) gets a row of
chips including one for the empty cell, everything else gets a substring box.
Filters combine with each other, with the search box and with the tab; the
button counts how many are set and the panel says how many rows survived.
A filter set on a folded-away column keeps its control on screen, so nothing
can trim the table invisibly.

**"Кандидаты"** sets four of them at once — the hard part of the letter in
`config/requirements.md`, which nobody assembles by hand four controls at a
time: recommendation ∈ {смотреть, спросить}, district rating ≥ 4, price
≤ 30 000. On the current corpus that is 1122 active rows down to 310. It is a
preset, not a mode: the filters it sets stay visible and adjustable, and
dropping the `спросить` chip — the rows where the ad simply never mentioned the
dog — takes it to 201. Editing any of them turns the button off, because the
set on screen is then no longer the preset. One caveat worth knowing: a from/to
filter excludes empty cells, so rows whose district nobody named fall out of
the set even though the rules deliberately do not penalise them.

Columns run in reading order — ID, Score, Pets, price, district, type,
bedrooms, size, publication date, your own mark, and only then the prose. The
layout used to be banded by where the value came from (Facebook / text pass /
photo pass), which put three clipped paragraphs between the price and the
bedroom count; where a single value came from is still on the cell itself, as
the `fb` and `≠ текст` badges, and the photo columns keep a green tint. The
rarely-scanned fields fold away behind the "Ещё N полей" toggle.

**`Рекомендация` is a tag, not the sentence.** The AI writes one line per
listing; its first two words were "Стоит посмотреть" on 62% of the active set,
spelled four different ways, and the rest was 240px of clipped prose duplicating
`Комментарий AI`. The column now shows the normalised tag — `смотреть`,
`спросить`, `сомнительно`, `перечитать`, `отказ` — which fits a filter chip and
sorts by urgency rather than alphabetically; the sentence itself is the
tooltip. `rules.verdict_tag` derives it from what the AI wrote, and
`.claude/skills/analyze` fixes the vocabulary at the source so it keeps
deriving cleanly. A sentence matching none of the stems leaves the cell blank,
which shows the drift instead of filing it under a wrong heading.

`Оценка района` is the same district scale the score uses, as a folded-away
numeric column. Nobody reads it row by row — it exists so that "хороший район"
is expressible as a filter and a sort key, which free-text `Район` with its 306
distinct values never was.

Three things are deliberately not columns:

- **`Качество данных`** — it read `good` on 99% of the table. The 27 rows where
  it does not now carry a red badge on the ID (searchable like any other text),
  which is what "this row needs `scrape --ids`" was always worth.
- **`unclear`** — the AI looked and the ad did not say, which is exactly what
  the grey dash already means, and it was the most common value in five
  columns. `pets` is the exception: not knowing whether the dog is welcome is
  itself the reason to call the agent.
- **`Причина`** and **`Кто решил`** outside the Rejected and All tabs — a
  rejection writes the reason and nothing else does, and on Longlist and
  Shortlist every row was decided by you, so both were columns of one repeated
  value as tall as the table. A column carries `tabs` to say where it lives.

Rejected rows carry their reason; shortlisted ones get a yellow bar,
longlisted ones a blue one; a photo field that overrode a non-empty text value
carries a badge with the old value in its tooltip.

**`Кто решил` is how your own rejections stay findable.** The rules
auto-rejected 1083 listings and you rejected 23, and on the Rejected tab they
sat in one pile: the only thing separating them was a small `(вы)` after the
reason, which the search box could not see (the haystack is built from cell
text, and the suffix was added at render time) and no filter could select. It
is a column now — `вы` as a chip, `AI` as a grey word — so one click in the
filter panel takes 1106 rows down to your 23. That matters more than it looks:
the auto-rejections are recomputed on every export and will shuffle the moment
a weight changes, while your 23 are the one thing here that cannot be rebuilt.

**Decisions are taken from the keyboard.** `↓ ↑` (or `j k`) walk the visible
rows, `1`–`5` record your own mark (1–3 reject, 4 longlists, 5 shortlists),
`r` rejects, `a` puts a row back into the queue to be looked at, `0` takes the
decision back; every key that records something also steps to the next row, so
a row costs one keystroke. Since every mark moves the row off Разобрать, the
tab shrinks under the cursor as you go. The mark shows up in
`Моя оценка` in yellow — принято, но ещё не записано — and the "Решения (N)"
button opens the queue as a ready block of `fb-rent rate` / `reject` commands
to paste into a terminal.

`d` is the one key that is not about the row under the cursor: it queues
`fb-rent dedupe` — search the whole DB for repeats of what is already there and
collapse them into one row. It goes into the same block, after the marks and
before `fb-rent export`, because dedupe keeps the copy carrying the most work
and your decision is the first thing it looks at. Pressing `d` again takes it
back out. The flag is stored next to the export's own timestamp, so a rebuilt
file drops it — the same self-cleaning the marks get, since a whole-DB command
leaves no per-row trace to check against.

The queue lives in `localStorage`, not in the file, because `export` rewrites
`rentals.html` after every stage and the decisions have to survive that. It
cleans itself: on load, an entry whose decision the row already carries — the
same mark, recorded by you — is dropped, so running the commands and
re-exporting empties the queue without anyone clearing it. Writing straight
into `decisions.json` would be less work at the desk and would cost the file
the one property it is built around: it opens from disk, with no server and no
network.

`rentals.csv` opens in Numbers or Excel but carries the scraped columns only.
The AI fields, verdict, score and your decisions live in the HTML.

## `config/` — the parts you edit

Version-controlled inside the package, because they are inputs, not state.

- **`config/requirements.md`** — what we are looking for, in prose: the letter
  to the agent, the priority checklist, the district scale and the list of
  automatic rejections. It is pasted verbatim into every batch, so changing
  priorities means editing this markdown, not the code.
- **`config/ai_fields.json`** — the field registry: every column the AI fills,
  its type, allowed values, extraction prompt and `source` (`text`, `photo` or
  `both`), plus `schema_version`. `analysis_version` and
  `photo_analysis_version` appear once `ai-field add --backfill` has run; while
  a key is absent it reads as equal to `schema_version`.

Both are read from the checkout through a path relative to the package source,
which is why `pip install -e .` is required and a plain `pip install .` breaks
every `ai-*` command and `fb-rent export`.

## The database

The tables that carry the work:

- `listings` — one row per Facebook posting: what the card and the detail page
  said, `raw_json` with the gallery URLs and `text_lines`, and `content_key`,
  the hash of the ad text that decides whether the text pass has to look again.
- `ai_analysis` — the two passes, in separate columns: `data_json`,
  `source_hash` and `schema_version` from the text pass; `photo_data_json`,
  `photos_hash` and `photo_schema_version` from the photo one. The fields are
  merged on read. `score` is stored too, but only as a trace of the last run —
  the export recomputes it, which is how a change of weights reaches rows that
  were analysed earlier.
- `duplicates` and `photo_hashes` — the dedupe machinery, see
  [dedupe.md](dedupe.md).
- `decisions` — a mirror of `decisions.json`, refreshed on every `export`. The
  JSON file is the original; this table exists so the exporter can join.
- `buildings` — a mirror of `buildings.json`, refreshed on every `export` and
  every `scrape`, for the same reason. One row per condo: the pets `policy`,
  the `status` (`proposed` changes nothing, only `confirmed` can reject), the
  `aliases` that point other spellings at it and the `sources` behind it. See
  [pets-policy.md](pets-policy.md).

Whether a row is pending is a comparison between its stored `schema_version`
(or `photo_schema_version`) and the registry's `analysis_version` /
`photo_analysis_version`.

A quick look at the state of things:

```sh
sqlite3 ../data/rentals.db "
select 'total', count(*) from listings
union all select 'with text', count(*) from listings where description is not null
union all select 'card only', count(*) from listings where description is null
union all select 'duplicates', count(*) from duplicates;"
```
