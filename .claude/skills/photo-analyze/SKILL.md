---
name: photo-analyze
description: Second pass over the listing photos in rent-assistant — run the cycle photo-batch → a fan-out of subagents over the images → photo-apply → export. Triggers ONLY on an explicit request: "analyze the photos", "look at the listing pictures", "photo analyze", "/photo-analyze". A non-empty photo queue in `ai-status` is not a reason by itself: the pass runs after the user has reviewed the table and rejected the rest by hand.
---

# Analyzing listing photos

The text pass (`/analyze`) has already read the descriptions. This pass
looks at the pictures and checks what stands behind the words: everybody
writes "fully furnished", the photos show what it means. The main product
of the pass is not new facts but **discrepancies with the text**.

You are the orchestrator here: the pictures never enter your context. The
subagents read them — one per listing. You collect their JSON and apply it.

The verdict, `score` and `status` are neither your business nor the
subagents' — `rules.py` computes them from the fields.

The pass runs **on the user's explicit request**, after they have reviewed
the table and rejected the rest. Do not offer it yourself and do not run it
right after the text analysis: the point is to pay tokens only for what a
human has already kept. Rows rejected by hand do not enter the queue — the
verdict from `decisions.json` overrides `rules.py` in both directions.

## 1. Check the queue

```sh
rent-assistant ai-status
```

Look at the "Photos" block:

- `Pending: 0` — nothing to look at, say so and stop (unless the user asks
  to re-check specific ids — then `rent-assistant photo-batch --ids <ID>`);
- `No photos: N` — these listings have no pictures on disk. Tell the user:

  > N listings without photos. Run `rent-assistant photos-fetch`, then we
  > come back to the photo analysis.

  This is not a reason to stop if `Pending` is above zero: run those that
  have photos, and mention the rest in the report.

The reasons in `Pending` read like this: `new` — no pass yet, `changed` —
the photos on disk have changed, `schema` — a new photo field appeared.

## 2. Build the batch

```sh
rent-assistant photo-batch --limit 25
```

The command prints a path like `.../ai/photo-batch-YYYYMMDD-HHMMSS.json`.
The flags mirror `ai-batch`: `--all`, `--ids <ID>` (repeatable, pulls a
listing past the queue), `--out`.

Read the batch **without `items[].photos`** — you need `fields` (the
registry of photo columns with a `prompt` for each) and `requirements_md`.
The image paths go to the subagents; there is no reason to open them
yourself.

Listings rejected by the text pass do not enter the batch at all — no
tokens are spent on them.

## 3. Fan out the subagents — one per listing

For every `item` in the batch — its own subagent. Launch the independent
ones **in a single message** so they run in parallel (up to ~12 at a time):

```
Agent(subagent_type: "general-purpose", model: "sonnet", prompt: <instruction below>)
```

Put into each prompt:

- the paths to all files from `item.photos` (this is the listing's whole
  gallery, however many frames it has) and the requirement to open each one
  with `Read`, numbering them in the same order: photo 1 is the first path;
- `fields` from the batch in full — keys, types, `values`, `prompt`;
- `item.current_fields` — what the text said about the same fields. This
  is not a hint but the thing to check: a discrepancy here is worth more
  than a match;
- `item.fb_id`, `item.photos_hash`, `item.listing_title`;
- the requirement to return **JSON only**, with no explanation around it.

The discipline for the subagent is the same as in the text pass; copy it
into the prompt verbatim:

- **Invent nothing.** Not visible in the photo means `unclear` (for an
  enum) or an omitted key (for text). A plausible guess is worse than an
  honest `unclear`. "The kitchen is probably behind the door" is `unclear`.
- **Every non-empty value comes with `ev`** in the format `"photo N: what
  is visible"` ("photo 3: two-burner stove, extractor hood, worktop"). `ev`
  is what you justify the value with, not a retelling of the whole picture.
- **Text does not override photos and vice versa.** If the text promised
  `furnished: full` and the photos show empty rooms, set `none`/`partial`
  with a reference to the frame. If the photos do not show what the text
  wrote about (the parking was not photographed), that is `unclear`, not
  `no`: not photographed is not absent.
- `conf`: `high` — directly visible in the frame; `med` — follows from the
  angle or a neighbouring frame; `low` — a shaky inference (then `unclear`
  is more honest).
- Value format: `{"v": <value>, "conf": "high|med|low", "ev": "photo N: ..."}`.
- The subagent **does not fill in** `verdict`, `score`, `status` —
  `rules.py` computes them. At most a `comment` of 1–2 sentences about what
  the photos show and what they do not.

Separately about `photo_quality` — a signal the text does not have:

- `render` — a 3D visualization instead of photos (perfect light, a
  deserted interior, "plastic" textures, repeating furniture);
- `floorplan` — floor plans only, no interior;
- `other_unit` — clearly a different unit photographed: a different
  layout, a different floor, a different window view between frames;
- `real` — real shots of this property;
- `unclear` — cannot tell.

On `photo_quality: render` **the other photo fields must not be filled**: a
render shows the developer's intent, not the flat. Return only
`photo_quality` and `comment`.

Each subagent returns one object:

```json
{ "fb_id": "...", "photos_hash": "<from the batch, verbatim>",
  "comment": "Real shots, furniture everywhere except the second bedroom.",
  "fields": { "furnished": {"v": "partial", "conf": "high",
                            "ev": "photo 4: bare floor in the second bedroom, only an air conditioner"},
              "photo_quality": {"v": "real", "conf": "high",
                                "ev": "photo 1: a lived-in frame, the owners' things are visible"} } }
```

## 4. Collect the result

Put the subagents' objects into a file next to the batch:
`<AI_DIR>/photo-result-<same ts>.json`.

```json
{ "schema_version": <the same as in the batch>,
  "model": "claude-sonnet-5 (claude-code subagents)",
  "results": [ <objects from step 3> ],
  "proposed_new_fields": [] }
```

Copy `photos_hash` from the batch verbatim: the code uses it to tell
whether the pictures changed between the build and the apply. A mismatch
means the entry **is skipped**, and that is right: the analysis no longer
describes those photos.

`verdict` is not needed here — it stays with the text pass. A key that is
not in the registry fails **the whole file** — new signals go into
`proposed_new_fields`.

## 5. Apply

```sh
rent-assistant photo-apply <path to photo-result-*.json>
```

When in doubt, `--dry-run` first. The command prints:

- how many analyses were applied;
- **discrepancies** `fb_id.key: old -> new` — the main result of the pass;
- new auto-rejects with their reasons (a photo can bring a listing down if
  it showed a studio where the text promised a house);
- skipped entries (a mismatched `photos_hash` — they need a new
  `photo-batch`);
- proposed fields.

The photo fields are merged on top of the text fields, after which `decide`
and `score` are recomputed.

## 6. New fields

If the subagents proposed something in `proposed_new_fields` (a useful
signal visible **in 2 or more listings**), ask the user through
`AskUserQuestion` whether a column is needed and whether the old photos
should be re-checked. Then:

```sh
rent-assistant ai-field add --key balcony_view --label "Balcony view" \
  --type enum --values sea,city,yard,none,unclear --group amenity \
  --source photo --prompt "What the balcony looks onto in the photos" [--backfill]
```

`--source photo` — only the photo pass fills the field, `--source both` —
both passes do. `--backfill` moves `photo_analysis_version` and returns
every listing to the photo queue with the reason `schema`; then **go back
to step 2**. Without `--backfill` we move on, and the old listings get the
`c-stale` hatching.

## 7. Export and report

```sh
rent-assistant export
```

Report briefly:

- how many listings were looked at, how many remain in the queue;
- **the list of discrepancies** — where the photos argued with the text,
  one line each: what the text promised, what the photos show;
- `photo_quality: render` / `floorplan` / `other_unit` — these listings
  effectively have no photos; worth asking the agent about them;
- new auto-rejects with their reasons;
- listings without photos on disk — they need `rent-assistant photos-fetch`
  (and if it says there are no links or they have expired —
  `rent-assistant scrape --ids <fb_id>`).

The decision on a specific property is the user's, through
`rent-assistant reject / unreject / shortlist`. Do not call these commands
yourself unless asked.
