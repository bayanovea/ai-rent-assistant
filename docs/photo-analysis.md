# The photo pass — how it works and what we learned

Read this first when you come back to photos. It carries what the code cannot:
measurements on live data, the decisions worth not undoing, and the open gaps.

Where the rest is:

- [pipeline.md](pipeline.md) — where this pass sits in the flow;
- [cli.md](cli.md) — `photos-fetch`, `photo-batch`, `photo-apply`, `photo-hash`;
- [dedupe.md](dedupe.md) — photo hashes as a duplicate signal;
- `CLAUDE.md` — the technical traps (how the gallery is obtained, why
  `context.request` works on signed URLs, what overwriting a photo directory
  destroys);
- `.claude/skills/photo-analyze/SKILL.md` — the procedure itself;
- `docs/plans/completed/20260825-photo-analysis.md` — the implementation plan.

Last checked against live data: **28 August 2026**, commit `5ab065c`.

## What the pass is for

The text pass believes the ad. The photo pass checks it: "fully furnished" is
in every listing, and only the picture says what that means. Its main product
is therefore not new facts but **disagreements with the text** — a `condition`
that was `unclear` in prose and is visibly worn in the photo, a `kitchen`
promised as full and shown as a kitchenette, a gallery that turns out to be a
3D render rather than the flat (`photo_quality: render`).

`photo_quality` is the signal the text cannot have at all: `real`, `render`,
`floorplan`, `other_unit`, `unclear`. Under `render` the subagent fills nothing
else — a render shows the developer's intent, not the flat. That rule now
covers `light` and `appeal` too, and it has to: a render is perfectly lit and
tastefully furnished by construction, so those two are exactly the fields it
would lie about most convincingly.

`light`, `appeal` and `view` (added 28 August 2026, `schema_version` 6) are the
second kind of signal the text cannot have — not because ads never mention
light or a sea view, but because every ad claims them. See the entry below on
where their limit is.

The reader is a **Sonnet subagent, one per listing**: the images never enter the
orchestrator's context, and about 12 subagents run at once, so 50 listings
finish in minutes. Sonnet is enough because the questions are concrete and
visual ("is there a sofa", "is this a render") rather than judgement calls —
the judgement stays in `rules.py`.

## The status

The loop works end to end and has been verified on real listings: gallery
download → batch → subagent fan-out → write → export. Running `photo-batch`
straight after `photo-apply` yields an empty queue — idempotency confirmed on
live data, not only in tests.

## The order of work is not automatic

**The pass runs only when you ask for it, after you have looked through the
table and rejected what is obviously wrong.** This is not an interface
preference; it is what the economics rest on. The photo queue respects
`decisions.json`, so every manual rejection made before the pass is tokens not
spent.

Your verdict outranks `rules.py` in both directions, exactly as in
`fetch_enriched`: what you rejected never reaches the batch even when the rules
would keep it, and what you kept does reach it even when the rules would drop
it.

Neither `/analyze` nor `ai-status` offers the photo pass. An agent should not
start one on its own just because the queue is not empty.

```sh
rent-assistant scrape                  # the gallery is downloaded here, whole
rent-assistant ai-status               # the "Photos" block: analysed / pending / no photos
   # /analyze, then you read the table and cut what is wrong
rent-assistant photos-fetch            # galleries for the survivors
rent-assistant photo-batch --limit 25  # -> $RENT_ASSISTANT_HOME/ai/photo-batch-<ts>.json
   # /photo-analyze: subagent fan-out, result in photo-result-<ts>.json
rent-assistant photo-apply <result>    # merge fields, recompute decide and score
rent-assistant export
```

## Measured on live data

Not estimates — measurements from the 25 August run over 5 listings:

| What | Measurement |
|---|---|
| Scrape with gallery | **9.4 s** per listing (3–7 s jitter + page + pictures) |
| Shot size | Facebook serves **960 px** on the long edge, ~90 KB after resize |
| Tokens per shot | **~920** (the 1024 px cap is never reached; resize only shrinks) |
| Subagent per listing | **~31k tokens**, of which images are only ~4.6k |
| Five subagents in parallel | **62 s** wall clock |
| Discrepancies found | 16 across 4 listings |

The main conclusion from those numbers: **three quarters of the cost is not the
pictures but the `general-purpose` system prompt**, paid again for every
subagent. If the cost ever becomes a problem, that is what to fix — not the
resolution and not the number of shots.

What the photos gave on those five: `condition` was filled for all four
listings where the text was silent; a `furnished: full` from the text turned
out to be `partial` (one bedroom empty); `kitchen: full` twice turned out to be
`unclear` — the kitchen simply had not been photographed. Two listings got
`photo_quality: other_unit`: instead of the flat, a marketing collage of The
Origin Phuket-Patong.

## The 28 August run — shortlist and longlist

Ten listings, everything the user had marked `shortlist` or `longlist`, run on
the user's explicit request rather than off the queue. **Four of the ten
produced a gallery. Six produced nothing**, which is the run's main finding and
is written up as gap 1 below.

The four that worked cost ~52k tokens each (15-23 shots) and returned 12
disagreements with the text, none of them a rejection:

| What the text said | What the photos showed |
|---|---|
| `kitchen: full` | `kitchenette` — a two-ring hob and a microwave, no oven |
| `condition: unclear` ×2 | `new` (factory stickers still on the fridge), `good` |
| `living_room: unclear` ×2 | `yes` |
| `parking: unclear` ×2 | `yes` — a carport in the frame |
| `green_area: unclear` ×4 | `yes` |

The three new fields filled on all four: `light` one `bright` and three
`normal`; `appeal` two `attractive` and two `ok`; `view` one `green`, two
`city`, one `wall`. Nothing scored `dark` or `unattractive`, which is what a
hand-picked shortlist should look like — the sample cannot say whether the
scale discriminates on the wider set. Score changes were small and in both
directions: 81, 84, 90, 100.

Worth keeping from the subagents' prose, because no field carries it: on one
house the living rooms are finished to a different standard than the kitchen
block (exposed pipes, corrugated roof — probably a separate Thai wet kitchen),
and on another the third bedroom is empty while the ad says fully furnished.

## Decisions not to undo

**The whole gallery, no sampling.** `PHOTO_MAX_COUNT = None`. Downloading costs
megabytes, a missed room does not, and the pictures are only paid for on
hand-picked listings anyway. Setting the constant to an integer brings back the
old sampling (cover plus an even spread) if rationing is ever needed.

**One subagent per listing, not a batch of listings.** More overhead, but it
rules out cross-listing confusion — a subagent physically cannot attribute one
flat's frame to another. On a small hand-picked set, the saving from batching
is not worth that risk.

**`photo_quality: real` is worth zero, not a bonus.** Most rows have had no
photo pass yet. A bonus for authenticity would rank them below the analysed
ones for a reason that has nothing to do with the flat. Only the defect is
penalised: `render` and `other_unit` −8, `floorplan` −6.

**A gallery is what is not inside a link.** Six of the ten listings in the 28
August run came back with nothing or with two 565x565 images — on four of them
the same file byte for byte, somebody else's flat. `extract_gallery_urls` ranked
`alt` groups against each other, which cannot notice that none of the groups
belongs to this listing: on a page whose gallery is simply absent, the winner is
a group of suggestions. Measured on three live pages, every suggestion card sits
inside an `a[href*="/marketplace/item/"]` and every one of the 17 gallery images
sits outside any link, so the link test now runs before the grouping and an
absent gallery yields an empty list.

Waiting and scrolling were tried first and changed nothing — those pages have no
gallery in the DOM at any point, so there is nothing to wait for. The run's bad
directories are in `$RENT_ASSISTANT_HOME/photos-bogus-20260828/`; they were moved rather than
deleted because a non-empty photo directory is never refetched
(`_download_photos`), so leaving them in place would have frozen six listings as
"photographed" for good.

**`light` and `appeal` are facts about what is visible, not taste.** The
temptation is a "beauty 1-5" field, and it breaks the property the whole pass
rests on: subagents produce facts, `rules.py` produces the judgement, so the
same gallery always yields the same outcome. A rating nobody can cite has no
`ev` and drifts between runs. Both prompts therefore ask for the visible
grounds — window size, daylight, a view into a wall; freshness of the finish,
clutter, wear — and `appeal` says outright that it rates the flat and not the
photography, because a good agency photographer and a tired flat go together
often.

The weights are asymmetric on purpose, and by more than the usual house rule:
`bright` +3 against `dark` −4, `attractive` +2 against `unattractive` −5. Two
reasons. The pair correlates — a bright flat photographs as an attractive one —
so a full bonus on each would pay the same photogenic listing twice. And the
plus side is the weaker evidence: a wide-angle daylight shot with the lamps on
makes anything look bright, while small windows and a view into the next tower
cannot be staged away.

**A sea view is worth no more than a green one.** `view` (`sea`, `green`,
`city`, `wall`) pays +2 for either and charges −3 for a window into the next
tower. Equal weights are the point: the sea is a minor plus for this user and
is already baked into the district ratings, so a bonus for the view would
smuggle the same preference in a second time — the beach-distance rule in
`CLAUDE.md` in a new disguise. `wall` is the signal actually worth collecting,
and it is −3 rather than −4 because a window into a wall usually comes with a
`dark` flat, which the score has already charged for.

The prompt draws one line the pictures blur: a sea view from the shared pool or
the roof terrace is not a view from the flat. Agency galleries lead with
exactly that shot.

Measured before adding all three: 4 of 530 active rows sat at exactly 100, so
the ceiling had room for +7. Re-measure after the first real batch — the tie at
the top is the failure mode, see `CLAUDE.md`.

**`photo-apply` must rewrite `score`.** `decide` is recomputed on every read
(`storage.py`, `fetch_enriched`); `score` is only recomputed on apply. The
asymmetry predates the photo pass, but forgetting it would leave the photo
signals out of the sorting.

**The `ev` format for photo fields is `"photo N: what is visible"`.** A picture
has no verbatim quote, but verifiability survives: you open the Nth file and
look. The tooltip rendering did not change.

## Open gaps

In descending order of importance.

**1. No targeted photo catch-up.** `--refresh` only re-opens what came back
from the feed. 29 rows had already fallen out of the 7-day window and past the
60-card limit — they will never get pictures that way. The plan assumed
"`--refresh` will fill in what we already have"; in practice that holds only
for the fresh part of the database. `scrape --ids` is the path, with the caveat
in [dedupe.md](dedupe.md#the-trap-a-dead-pages-gallery-belongs-to-someone-else)
about dead pages.

**2. Subagent overhead.** ~26k of the 31k per listing is the `general-purpose`
system prompt. Curable either with a dedicated subagent type carrying a minimal
prompt, or by batching (5 listings per agent), which would cut the cost roughly
threefold. Against batching: the cross-listing confusion risk above.

**3. The `Stale` counter in `ai-status` lies.** `storage.py`, `fetch_pending`
compares against the whole `registry.schema_version` without looking at a
field's `source`. Add a photo field and every text row becomes "stale", and the
command advises a backfill that need not happen. No extra work actually occurs
(`analysis_version` is set explicitly, so the rows go to `stale`, not
`pending`) — only the number is wrong.

**4. Fifty directories hold six shots each.** They were downloaded under the
old limit, and `_download_photos` skips non-empty directories. The full gallery
for a given listing can be had like this — but only while it is still in the
feed:

```sh
rm -rf ~/.rent-assistant/photos/<fb_id>
rent-assistant scrape --refresh
```

## On plan limits

Anthropic does not publish Max capacity in tokens: the limit is stated relative
to Pro ("5x or 20x per 5-hour window") and depends on the model and the effort
level. There is nothing to match a token count against directly.

Measure it empirically: look at `/usage`, run one batch of five listings, look
again. The delta × N is the price of a run as a share of the window. As a rough
estimate an evening run of both passes over 50 listings is about 2M tokens —
a small share of the window for Max 20x, a noticeable part of one window for
Max 5x.
