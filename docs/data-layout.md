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

`decisions.json` is the only file here you cannot rebuild. The DB can be
re-scraped and re-analysed; what you thought about a flat cannot. It is
mirrored into the DB on every `export`, so wiping `rentals.db` loses nothing.

Photo directories are disposable but not free: `photos_hash` is computed from
the file names and sizes and is what re-queues a listing for the photo pass, so
anything that rewrites a directory re-queues it. That is why `photos-fetch` and
`scrape` both leave a non-empty directory alone. Auto-rejecting a listing, or
merging it away in `dedupe`, purges its directory.

### The HTML export

One self-contained file: no server, no CDN, no internet. Click a header to
sort, type in the search box to filter across every column, switch sets with
the Active / Shortlist / Rejected / All tabs. Columns are banded by where the
value came from — parsed off the Facebook page, extracted by the text pass, or
seen in the photos — and the rarely-scanned fields fold away behind the
"Ещё N полей" toggle. Rejected rows carry their reason; shortlisted ones get a
yellow bar; a photo field that overrode a non-empty text value carries a badge
with the old value in its tooltip.

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

Three tables carry the work:

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
