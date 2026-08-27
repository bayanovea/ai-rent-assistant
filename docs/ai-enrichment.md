# The text pass

The scraper only gets you raw text. The enrichment loop turns that text into
filled columns, a per-listing recommendation and a score, so the search happens
on one HTML page instead of forty Facebook tabs.

The "AI" here is Claude Code itself — Opus, high thinking effort. There is no
API client and no extra runtime dependency. The CLI's job is only the contract:
write a batch out, read a result back.

```sh
fb-rent ai-status                 # analysed / pending / stale / unusable
fb-rent ai-batch --limit 25       # -> ../data/ai/batch-<ts>.json
   # the AI reads the batch + config/requirements.md, writes a result file
fb-rent ai-apply <result.json>    # store fields, verdicts, auto-rejections
fb-rent export                    # regenerate CSV + HTML
```

In a Claude Code session `/analyze` drives that whole loop
(`.claude/skills/analyze/SKILL.md`).

## What the AI is allowed to produce

Two texts per listing and a bag of fields:

- **`verdict`** — one short recommendation line, what you see in the table
  without hovering: "Стоит посмотреть — дом с гостиной в бюджете", "Отказ:
  животные запрещены".
- **`comment`** — one to three sentences of reasoning, and the only place facts
  that have no column yet are visible at all ("депозит 3 месяца", "до школы
  10 минут").
- **fields**, each as `{"v": …, "conf": "high|med|low", "ev": "quote"}`. Every
  non-empty value carries a verbatim quote from the listing, which the export
  shows as a tooltip: any claim can be traced back to the ad.

Two rules do most of the work. **Nothing is invented** — no evidence in the
text means `unclear` for an enum or a missing key for the rest; a plausible
guess is worse than an honest `unclear`. And **silence is not a defect** — an
unmentioned living room is `unclear`, not `no`.

`score` and `status` are ignored if present. Extraction is the AI's job; the
decision is not.

## `data_quality`

Alongside the fields the AI records how usable the text was: `good` (it read
the ad itself), `sidebar_noise` (Facebook's "Выбор дня" block landed in the
text instead of the ad), or `missing` (no ad text at all).

Anything other than `good` is amber in the export, means *re-scrape this one*,
and **suppresses every auto-rejection** — a verdict drawn from the wrong text
is not evidence about this listing. See
[scraping.md](scraping.md#extraction-is-best-effort) for why some listings
never recover from this and have to be read out of `text_lines` instead.

## What `rules.py` decides

Exactly five conditions auto-reject:

| Condition | Note |
|---|---|
| pets explicitly forbidden | criterion #1; only an explicit ban counts, silence does not |
| Patong | checked before price and size — it is out whatever the flat looks like |
| price above 35 000 THB | the hard ceiling above the 15 000–30 000 target |
| an explicit studio | `property_type = studio` or `bedrooms = 0` |
| a stated size under 35 m² | an ad that never names its size is **not** rejected |

Everything else — a condo, no parking, an unmentioned living room, weak
internet — is a remark in the verdict, never a rejection.

The score starts from a baseline of 50 and moves on evidence only. Fields the
ad never mentions leave it untouched, which is the whole design: silence must
not rank a listing below one that spoke. The district is worth up to ±9 (the
1–5 scale in `config/requirements.md`, pivoting at 3.5, three points per half
step), pets `allowed` +14 against `not_allowed` −30, and the rest are the
smaller weights in `rules.py`.

The two sides are asymmetric on purpose: a stop-factor hits at full strength, a
nice-to-have is worth about two thirds of what a mirror-image penalty would
cost. **The failure mode to watch for is a tie at the top.** An earlier version
paid points for a merely non-empty `internet`, `pool_gym` or `extra_rooms` —
which ranks the wordy ad above the terse one describing the same flat — plus a
flat bonus for the first bedroom and for a price inside the budget, both of
which every survivor has by definition. Together they pinned **352 of 1123
active listings at exactly 100**, so the default sort had nothing left to order
the first screen by. Removing them and trimming the positive weights spread the
same corpus over 56 distinct values with a median of 69 and four rows at 100.
Anything added here has to be checked the same way: raise the ceiling and the
sort quietly stops working.

Two details about districts that are easy to get wrong:

- **The first named area wins.** `area` is free text and the AI often names two
  places at once ("Cherngtalay, Thalang", "Kathu, 10 мин до Patong"). The
  rating takes the *earliest* match by position in the string, not the best or
  the worst one: the flat is where the ad put it, everything after is a
  landmark. Taking the maximum would turn a Thalang townhouse into Bang Tao;
  taking the minimum would auto-reject a Kathu flat for mentioning Patong.
- **Proximity to the sea is already inside the district rating.** There is no
  separate bonus for beach distance and there should not be — a deliberate
  decision, not an omission.

**The score is recomputed on read**, not taken from the column.
`fetch_enriched` runs `rules.score` and `rules.decide` over the merged fields
every time, so changing a weight reaches rows analysed weeks ago without
re-running the AI. The `ai_analysis.score` column is written by both passes and
is only a trace of the last run.

## Evolving the schema

When the AI keeps seeing a useful signal that has no column, it proposes one in
`proposed_new_fields` (the bar is: seen in ≥2 listings, and it affects the
choice of a home or the money). `ai-apply` prints the proposal; you decide.

```sh
fb-rent ai-field list
fb-rent ai-field add --key balcony --label Balcony --type enum \
  --values yes,no,unclear --group amenity --after price_12m_thb \
  --prompt "Есть ли балкон" [--backfill]
```

Every `add` bumps `schema_version`. What `--backfill` changes is
`analysis_version` (or `photo_analysis_version` for `--source photo`;
`--source both` moves both):

- **without `--backfill`** — old rows keep their analysis and show a `c-stale`
  cell (hatched grey) for the new field, meaning *"the field did not exist when
  this listing was analysed"* — as opposed to `c-none` (plain grey dash), *"the
  AI looked and there was nothing to find"*. Nothing is re-queued.
- **with `--backfill`** — the pass's version catches up to `schema_version`,
  every analysed listing becomes pending again, and the next batch fills the
  new column across the whole table.

Staleness hatching in the export has to be computed against the field's own
version. Using `ai_schema_version` for a photo field hatches every row.

## Two passes, two versions

The photo pass ([photo-analysis.md](photo-analysis.md)) has its own version,
its own queue and its own columns. Text results live in `data_json`, photo
results in `photo_data_json`, and they are merged on read as
`{**data_json, **photo_data_json}` — so neither pass can clobber the other, and
dropping the photo columns leaves the text analysis intact.

A field declares which pass may fill it with `source`: `text` (the default),
`photo`, or `both`. The photo batch ships only the `photo`/`both` subset.
