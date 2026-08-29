# Duplicates

The same flat reaches the table many times over. Agencies repost a listing to
lift it back up the feed and Facebook mints a new id every time; the same flat
is also handed to several agents, who each write their own text and often quote
a different price. Deduplication therefore happens on several levels, and each
one catches something the others cannot.

| Signal | What it matches | When |
|---|---|---|
| `fb_id` | the same posting seen again | on write |
| `exact` | the same ad text at the same price (`content_key`) | on write |
| `photos` | two of the first three gallery shots are the same pictures | `fb-rent dedupe` |
| `text` | the ad body is all but identical — a repost with a line edited | `fb-rent dedupe` |
| `facts` | same project, same price, same bedroom count | `fb-rent dedupe` |
| `manual` | you pointed at the pair yourself | `fb-rent dedupe <id> --into <id>` |

A listing whose text and price already exist is recorded in `duplicates` at
write time instead of getting its own row. Whatever `dedupe` merges is
remembered the same way, so the next scrape skips it without opening its page.
The table shows one line per flat, with every posting's id and link in its ID
cell.

Ads that name **different floors, buildings or sizes** are never merged
(`_contradicts`), whatever else matches. The one exception is `exact`:
byte-identical text stays the same text no matter what the copies claim about
the floor.

## The row you recognised: `d` in the table

The signals above answer "which rows are repeats". Standing on a row in the
table and pressing `d` asks something else — *I have seen this flat before,
find it* — and the answer has to survive being wrong, because a merge deletes
a row.

So `fb-rent dedupe <fb_id>` merges only what the hard signals already tie to
that row, pairwise. Pairwise is the whole difference from the whole-base pass:
there the veto is checked against the entire group, and a third listing that
claims another floor pulls apart a pair that is one flat (see "the group of
52"). By the time you press `d`, the group is what you pointed at, and there is
no third member in it.

When nothing matches, nothing is merged. The command prints the nearest rows —
the ones you have already decided on first, since that is the memory the key
runs on — each with the `--into` that merges it:

```
Nothing merged: no signal ties 9000000000000006 to another listing.
Closest rows — run one of these if it is the same flat:
  fb-rent dedupe 9000000000000006 --into 9000000000000005
      1 bed 1 bathroom – Flat — 25000 THB [active] — тот же проект,
      текст совпадает на 62%, столько же спален, та же цена;
      но этаж, корпус или площадь расходятся
```

That shortlist stays silent unless the **project name or the ad text** match.
Measured on the live base, dropping that requirement filled the list with five
strangers that merely shared a price and a bedroom count — which describes
half of Phuket — and pushed out the row worth looking at. Price and bedrooms
still count, but only towards the order.

`--into` is the one path that merges without a signal, and the row you name is
the keeper whatever it carries: naming it is a statement that your verdict
lives there.

## The photo signal

The only signal that needs neither matching text nor a finished AI analysis, so
it cuts rows **before** the text pass runs.

`scrape` hashes the first three shots of every gallery right after the detail
page — including under `--no-photos`, because three shots are not a gallery —
and stores two hashes per shot in `photo_hashes`: the MD5 of the CDN bytes (a
literal repost) and a perceptual dHash (a re-upload through someone else's
phone, or a resize). Two shots matching out of three merges the pair.

Nothing lands on disk. A preview left in `data/photos/<fb_id>/` would convince
the photo pass forever that the gallery is already downloaded — see
[photo-analysis.md](photo-analysis.md).

One shared shot is usually the pool, the view of the complex or an agency logo,
so a hash appearing on more than `PHOTO_HASH_COMMON_MAX` listings is dropped
from the index. The thresholds live in `config.py`:
`PHOTO_FINGERPRINT_COUNT = 3`, `PHOTO_MATCH_MIN = 2`,
`PHOTO_HASH_COMMON_MAX = 4`.

Rows scraped before the hash stage existed are caught up with
`fb-rent photo-hash`; `ai-status` prints the queue.

### The 2026-08-26 backfill: the thresholds hold

`photo-hash --all` over a 779-row database: **690 rows had stored gallery
links, all 690 were hashed, not one expired signature** (the rows were fresh,
from 2026-08-25). 631 listings yielded three shots, 59 yielded two. The
remaining 89 rows have no links at all — their detail pages were read before
galleries were collected. 2011 rows in `photo_hashes`, about 20 minutes of
runtime, 33 too-common hashes discarded.

What the signal added was measured by running `_group_duplicates` twice, with
and without hashes:

| | groups | rows merged away |
|---|---|---|
| without photos | 109 | 318 |
| with photos | 111 | 324 |

Six groups changed, and all six were reviewed by hand:

- **four new pairs, exactly right** — precisely the case the signal was built
  for: Dcondo Reef Kathu, building C, 5th floor, 28 m² — 18 000 and 22 000 from
  different agents; The Base Rise, 25 m² studio, 4th floor — 16 000 and 15 000;
  The Base Bukit, building C, 7th floor, 34.28 m² — both at 25 000; a house in
  Thalang at 30 000. The text caught none of them: rewritten descriptions,
  different prices. Two of the pairs hold **on dHash alone** — the MD5s
  diverged, the pictures had been re-uploaded;
- **a group of four** (The Rich Chalong, 2 bedrooms, 23 000 and 25 000) — two
  pairs the text had already merged separately, joined to each other by photos;
- **a merge of 8 + 44 → 52** on The Base Bukit — which turned out to be the
  real find, see below.

One false merge, and it is not about the thresholds (see "the trap"). Raising
`PHOTO_MATCH_MIN` to 3 is not an option: it would kill two of the four correct
pairs (they matched on exactly two shots of three) and would not touch the
false one (all three of its shots matched). The thresholds stay.

### The group of 52: transitivity against `_contradicts`

Taking that mega-group apart taught more than the photo signal itself. Inside
it were 52 The Base Bukit listings: one bedroom, ~30 m², but **buildings A, B
and C and floors 2 through 8**. Of its 1326 internal pairs, **1039 directly
contradict each other** on floor, building or size. The group was held together
by 100 `facts` edges (project + price + bedrooms) resting on 8 listings that
name neither floor nor building. A listing like that, at 20 000, contradicts
nobody, matches everybody on `facts`, and stitches building A to building C
through itself. Only 14 of the 1326 edges were photo edges — without them 44
rows would still have merged.

The defect was not in the signals but in the fact that union is transitive
while `_contradicts` was not: it looked at a pair while the group kept growing.
The fix, in `_group_duplicates`: **the veto is checked against the whole
group**, not just the candidate pair. `exact` is exempt, as above.

| | groups | rows merged away | largest group |
|---|---|---|---|
| before | 111 | 324 | 52 |
| with the group veto | 138 | 267 | 11 |

The price is 57 rows that go on living: pairs whose agents disagreed about the
floor no longer merge. That is the right trade — before it, one row was hiding
half a hundred different flats.

**Run on 2026-08-26:** `dedupe` removed 274 rows (267 plus what repeated runs
picked up until the signal ran dry), 779 → 503, with 359 records in
`duplicates`.

### The trap: a dead page's gallery belongs to someone else

The single false pair: a two-storey three-bedroom townhouse merged with a
one-bedroom flat in Hill Myna Condo, all three MD5s matching. It turned out the
townhouse's gallery (7 links) **contained the flat's gallery whole** (5 links,
in the same order) plus two more shots. The downloaded pictures were the
studio and bedroom of the condo, nothing to do with the townhouse.

The cause is not the hashes. The townhouse's page no longer opens, and a
re-read with `scrape --ids` returned the "Выбор дня" sidebar instead of the
description and an empty gallery. So at the moment of the first scrape the page
was already serving someone else's content, and `extract_gallery_urls` honestly
took the largest `alt` group of what was on screen. The row was repaired by
hand: description restored from a backup, gallery cleared, hashes deleted.

Two consequences:

- **`scrape --ids` over a dead page damages the row.** Description extraction
  falls into its fallback and writes the sidebar over the real text — the same
  defect as in [scrape-log.md](scrape-log.md), except here it destroys data
  that was already collected. Worth fixing before re-reading the 89 rows that
  have no galleries, or some descriptions will be traded for junk.
- **The photo signal inherits a foreign gallery** if one made it into
  `raw_json`. There is no defence for that in dedupe and probably should not
  be: it is curable at the entrance.

### The 2026-08-27 run: chains, and why one flat had two rows

The user pointed at six ids that were one Pruksa Ville Kathu–Samkong
townhouse. They were already two groups of three — the pair of survivors,
`9000000000000010` and `9000000000000009`, differed by one line
(«Co-agents welcome»), which is a 0.95 word overlap and a `text` match.
Nothing was wrong with the signal: `dedupe` simply had not been run since
those rows were analysed, and the fuzzy rules read the AI's fields.

Taking them apart surfaced a real defect. `dedupe` recorded the copies it
dropped but never moved the reposts that already hung off them: three of the
six ids named a `canonical_fb_id` that the previous run had deleted, and
`duplicates_by_canonical` groups by that name — so they vanished from the ID
cell instead of joining the surviving row. 30 rows in the DB were orphaned
that way. The merge now carries dependents over to the keeper, and
`_flatten_duplicate_chains` walks the dead links in an old DB to the copy
that survived; every one of the 30 resolved.

**Run:** 2235 → 1401 rows over six passes to convergence, 834 merged away,
1433 records in `duplicates`, no orphans left. The cascade is expected — each
merge frees pairings the previous pass could not see — and it is why the loop
runs until the signal is dry.
