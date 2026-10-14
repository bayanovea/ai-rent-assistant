---
name: analyze
description: Analyze the unprocessed rent-assistant listings — run the cycle ai-batch → read the descriptions → ai-apply → export. Triggers on "analyze listings", "go through the new ads", "/analyze", and when the user asks what new rentals have turned up in Phuket.
---

# Analyzing rental listings

You are an analyst who reads the raw text of FB Marketplace ads and fills
in a table from it. Extraction is your job alone; the decision "reject or
keep" and the score are computed by the code (`rules.py`) — do not try to
replace them.

Model: Opus, high thinking effort. Work slowly and literally.

## 1. Check the queue

```sh
rent-assistant ai-status
```

Look at the line `No raw data`. If it is above zero, those listings have
neither a description nor `text_lines`, so there is nothing to analyze.
Tell the user:

> N listings without raw data. Run `rent-assistant scrape --ids <fb_id>`, then
> we come back to the analysis.

and **stop**. Do not invent facts from a title alone.

If `Pending: 0` and `No raw data: 0`, everything is already analyzed; say
so and stop (unless the user asks to re-analyze specific ids — then
`rent-assistant ai-batch --ids <ID> --ids <ID>`).

## 2. Build the batch

```sh
rent-assistant ai-batch --limit 25
```

The command prints a path like `.../ai/batch-YYYYMMDD-HHMMSS.json`.

## 3. Read

Read the whole batch and the requirements letter (it sits inside the batch
as `requirements_md` — it is the letter to the agent, the search criteria,
and it sets the priorities). Read `fields` from the batch — the registry of
columns you must fill, with a `prompt` for each.

## 4. Extract facts — from text_lines, not from description

The source of truth is `items[].text_lines`, specifically **the slice after
the line `Описание`** (the "Description" marker in the Russian-locale page;
for some listings the `description` column contains the "Выбор дня" sidebar
instead of the ad itself). `items[].description` is an already cleaned
attempt at the same slice; compare it with `text_lines` and, where they
differ, trust `text_lines`.

For a listing whose `items[].source` starts with `group:` this slice does
not work — a group post has no "Описание" line at all, and `text_lines`
picks up the post header (author, time) and part of the comments. For such
listings `description` is the structural text of the post itself (cut by
the scraper on a DOM boundary, not on a marker), and it is the source of
truth: compare it with `text_lines`, but on a mismatch trust `description`,
not the other way round. A missing "Описание" line or pets facet in
`text_lines` for `group:*` is normal, not `sidebar_noise`.

Fill in `data_quality` for every listing:

- `good` — the ad text was found and is readable (for `group:*` this means:
  `description` looks like a rental ad, not a fragment of the feed or
  somebody else's comment);
- `sidebar_noise` — an unrelated FB block instead of the ad;
- `missing` — there is no ad text at all.

A bad `data_quality` is **never a reason to reject a listing** — it is a
"needs re-scrape" mark.

Extraction rules:

- **Invent nothing.** No evidence in the text means `unclear` (for an enum)
  or an omitted key (for text/int). A plausible guess is worse than an
  honest `unclear`.
- **Every non-empty value comes with a quote in `ev`**, a verbatim piece of
  `text_lines`. The quotes are shown to the user as tooltips: every claim
  you make must trace back to the source text.
- **Pets**: `allowed` only on an explicit confirmation (pets allowed, pet
  friendly, dogs ok, สัตว์เลี้ยงได้). `not_allowed` only on an explicit ban.
  Everything else, including complete silence, is `unclear`. This is
  criterion #1 and the only fact-based auto-reject, so `not_allowed` must
  never be set without a quote. The Marketplace facet ("Можно с собаками и
  кошками", the pets line in the Russian-locale page) does not exist on
  group posts — the code fills in `allowed` from the facet separately when
  it is there; for `group:*` the only signal is the text of the post.
- **More rooms than asked for is a plus, not a minus.** Three bedrooms
  instead of one is good; do not lower the score or list it as a minus in
  the verdict.
- **An unmentioned living room is not a defect.** `living_room: unclear`,
  nothing more. Most ads simply do not write "living room".
- Condo/apartment, no parking, short-term rental, weak internet, no green
  area — flags for the verdict, not a rejection.
- `conf`: `high` — stated in plain words; `med` — follows from context;
  `low` — a shaky inference (then consider whether `unclear` is more
  honest).

Value format: `{"v": <value>, "conf": "high|med|low", "ev": "quote"}`.
Respect the types: `int` is a number, not the string "16,000".

## 5. Write the verdict and the comment

For every listing — two texts, written in English.

`verdict` — **one short recommendation sentence**. Open it with one of five
words, then a dash and free text:

| Opening | When |
|---|---|
| `View —` | suitable, nothing stands in the way of calling |
| `Ask —` | would suit, but the ad does not say the main thing (usually about pets) |
| `Doubtful —` | there is a real minus, but not a rejection |
| `Reject —` | not suitable at all: not housing, not a rental, a stop factor |
| `Re-read —` | the ad text is missing or is the wrong text |

The first word is what goes into the "Verdict" column and what the filter
chips work on: `rules.verdict_tag` pulls it out of the sentence, and the
sentence itself remains the hover hint. Synonyms ("Worth a glance", "Too
little data") break the tag — write exactly as in the table above.

Full examples: "View — house with a living room within budget",
"Doubtful — condo 34 m², no living room", "Reject — pets forbidden",
"Ask — the text says house, the FB card says apartment".

`comment` — 1–3 sentences of reasoning: what it is, what is good about it,
what is missing. Take **everything known** into account, including things
with no column yet ("the owner asks for a 3-month deposit", "10 minutes to
the school"). The comment is the only place where such facts are visible
at all until a column appears for them.

## 6. Save the result

Write the file next to the batch: `<AI_DIR>/result-<same ts>.json`.

```json
{ "schema_version": <the same as in the batch>,
  "model": "claude-opus-5 (claude-code)",
  "results": [
    { "fb_id": "...", "source_hash": "<from the batch, verbatim>",
      "verdict": "View — house with a living room within budget",
      "comment": "2-bedroom house in Rawai, internet and garbage included. Nothing said about pets or parking.",
      "data_quality": "good",
      "fields": { "pets": {"v": "unclear", "conf": "high", "ev": ""},
                  "price_12m_thb": {"v": 16000, "conf": "high",
                                    "ev": "12 months: 16,000 THB/month"} } }
  ],
  "proposed_new_fields": [] }
```

Do not set `score` and `status` — `rules.py` computes them, and values from
the file are ignored anyway. Copy `source_hash` from the batch unchanged:
the code uses it to tell whether the listing was re-scraped between the
build and the apply.

A key that is not in the registry fails **the whole file** with an error —
new signals go into `proposed_new_fields`, not into `fields`.

## 7. Schema evolution

If the same useful signal appeared **in 2 or more listings** and there is no
column for it, add it to `proposed_new_fields`:

```json
{ "key": "electricity_rate", "label": "Electricity ฿/kWh", "type": "text",
  "why": "in 3 of 8 listings, spread 4–8 ฿/kWh — that is ±2000 THB/month",
  "examples": ["Electricity 7 THB/unit", "ไฟฟ้า 5 บาท/หน่วย"] }
```

Useful means it affects the choice of home or the money. A one-off oddity
from a single ad does not go into the registry; its place is the comment.

## 8. Apply

```sh
rent-assistant ai-apply <path to result-*.json>
```

The command prints the counters, the auto-rejects with their reasons and
the list of proposed fields. If it complains about unknown keys or a
missing verdict, fix the file and repeat (when in doubt, `--dry-run`
first).

## 9. Ask about new fields

Only if `proposed_new_fields` is non-empty. For each proposal ask the user
two questions through `AskUserQuestion`:

1. **Is this field important?** (add to the table / not needed) — give the
   `why` and the examples from the ads.
2. **What to do with the old listings?** — re-analyze them (`--backfill`)
   or leave them an empty cell in a distinct colour (`c-stale`, hatching
   that says "the field did not exist when I looked at this").

Then:

```sh
rent-assistant ai-field add --key electricity_rate --label "Electricity ฿/kWh" \
  --type text --group amenity --after price_12m_thb \
  --prompt "Electricity rate, if given" [--backfill]
```

For `--type enum`, `--values a,b,c` is required.

If the user chose `--backfill`, **go back to step 2**: the old listings are
back in the queue and must be run again (now with the new field). Without
`--backfill` we move on.

## 10. Export and report

```sh
rent-assistant export
```

Report to the user briefly:

- counters: analyzed / rejected / still pending / stale;
- every auto-reject with its reason (`Pets forbidden`, `Far over budget`,
  `Studio`);
- **top 5 by score** — id, price, type, score and one sentence why;
- listings with `data_quality != good` — they need
  `rent-assistant scrape --ids <fb_id>`;
- what is worth asking the agent about (usually pets where `unclear`).

The top 5 is easy to get like this:

```sh
python -c "
from rent_assistant import config, storage
conn = storage.init_db(config.DB_PATH)
rows = [i for i in storage.fetch_enriched(conn) if i['analyzed'] and i['status'] != 'rejected']
for r in sorted(rows, key=lambda i: i['score'] or 0, reverse=True)[:5]:
    print(r['fb_id'], r['price_thb'], r['score'], (r['verdict'] or '')[:80])
"
```

Look at the "Photos" block in `rent-assistant ai-status` and just name the
number:

> N listings whose photos have not been looked at yet.

**Do not offer to run `/photo-analyze`.** The order is: the user reviews the
table, rejects what is not needed through `rent-assistant reject`, and only
then asks to look at the photos of what is left. Rows rejected by hand do
not enter the photo queue, so every reject before the pass is tokens saved.

`fetch_enriched` is used for a reason, not for convenience: AI auto-rejects
are stored nowhere, they are recomputed from the fields on read. Direct SQL
over `decisions` would show in the top 5 a listing rejected for "Pets
forbidden".

The decision on a specific property is the user's — they record it through
`rent-assistant reject <ID> --reason "..."`, `unreject`, `shortlist`. Do not
call these commands yourself unless asked: `decisions.json` in
`$RENT_ASSISTANT_HOME` is their word, not yours, and it survives a wipe of
the DB.
