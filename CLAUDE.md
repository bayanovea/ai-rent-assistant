# fb-rent — notes for Claude

Project-specific things that are not obvious from the code. General layout,
commands and rationale live in `README.md` and `docs/plans/`.

**Coming back to the photo pass? Read `docs/photo-analysis.md` first.** It
carries what the code cannot: live measurements, the decisions worth not
undoing, and the open gaps. This file has only the technical traps.

## Photos

**The gallery is in the DOM without a click.** Every detail page ships 10–28
`img` elements from the gallery in `page.content()`; opening the lightbox is
neither necessary nor possible (an overlay eats the pointer events on the
cover). `extract_gallery_urls` picks them locale-independently: `img` with
`/t39.30808-6/` in `src` and a non-empty `alt`, grouped by `alt`, largest group
wins (gallery photos share one `alt`, "similar listings" cards each have their
own). Do not filter on `aria-label` numbering (locale-dependent) or on
`cstp=mx` (drags in the 565×565 recommendation cards).

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
broken forever: `_download_photos` skips a non-empty directory (`cli.py:202`),
so the real gallery never arrives, and `photos_hash` — computed from file names
and sizes — happily marks the listing as ready with three pictures in it.

## Scrape

**A re-sighting must not clobber.** `scrape` skips the detail page of a
listing already in the DB, so what comes back is a bare list card with no
description, no `published_at` and no `raw_json`. `upsert_listing` therefore
drops `None` values on update: writing them erased the ad text, the gallery
URLs and the `content_key` on every run that re-saw the table — which a wide
sweep does by definition.

**A price band is a question, not a filter.** One query hands out a few
hundred cards and then stops, so volume comes from `--bands N` — several
narrow queries instead of one wide one. Two consequences the code encodes:
`scrape_list(allow_empty=True)` during a sweep, because an empty band says
nothing about the session (only *every* band empty does), and a round-robin
over the bands rather than concatenation, because the detail budget is shared
and band-after-band order spends all of it in the cheapest range.

**The detail budget counts pages, not cards.** `details_fetched < detail_cap`,
never `idx < detail_cap`: a sorted feed puts known listings and reposts on the
first screens, and an index-based cap spends the whole budget on cards whose
detail page is skipped anyway. Rows are upserted inside the loop, so a run that
dies at minute 40 keeps its work; the exports are the only stale part.

**Hashes come from the CDN bytes, not from the files on disk.** The
fingerprint stage re-downloads the three shots even in a run that is already
downloading the gallery. Reusing `download_and_resize`'s bytes looks free and
is not: `md5` would then depend on whether it was taken before or after the
resize, and a shot's position on whichever URLs happened to survive. Three
extra GETs fit inside the jitter that is already paid for, and keep the stage
orthogonal to the photo pass — which is why it also runs under `--no-photos`
(three shots are not a gallery) and stops only under `--dry-run`.

**A card-only row still counts as known.** `known_fb_ids` (`storage.py:612`)
returns every `fb_id` in the table, and does not care whether the row has a
description. When the detail budget runs out mid-run the loop keeps writing the
remaining cards as rows with a title and a price and nothing else — and those
rows then make the *next* run skip their detail pages forever. `--refresh`
is the wrong fix (it re-reads the rows that are already complete and burns the
budget on them); the right one is `--ids` over
`select fb_id from listings where description is null`. The 2026-08-25 sweep
left 594 such rows — see `docs/scrape-log.md`.

## Районы

**Первый названный район выигрывает.** `area` — свободный текст, и AI часто
называет два места сразу (`Cherngtalay, Thalang`, `Kathu, 10 мин до Patong`).
`district_rating` берёт самое раннее совпадение по позиции в строке, а не
лучшее и не худшее: дом стоит там, где ад его поставил, всё остальное —
ориентиры. Взять максимум — и таунхаус в Thalang станет Bang Tao; взять
минимум — и дом в Kathu уедет в автоотказ из-за упоминания Patong.

**Оценка района уже включает близость к морю.** Отдельного бонуса за
расстояние до пляжа нет и быть не должно — это осознанное решение, а не
пропуск: море для пользователя неключевой плюс, и он вшит в сами оценки
(Bang Tao 5, Chalong 3).

**Score считается на чтении, а не берётся из колонки.** `fetch_enriched`
перевычисляет его через `rules.score`, как и вердикт, — иначе смена весов не
дошла бы до строк, проанализированных раньше, а перепрогонять AI ради веса
района бессмысленно. Колонка `ai_analysis.score` пишется обоими проходами и
остаётся только следом последнего прогона.

## Subagent fan-out

The photo pass reads images in subagents, one per listing
(`.claude/skills/photo-analyze/SKILL.md`) — images never enter the
orchestrator's context. Launch the independent ones in a single message so they
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
