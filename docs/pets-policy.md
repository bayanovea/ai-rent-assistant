# The pets pass — one answer per building

The third pass, and the only one that leaves the machine to read the open web.
It answers one question — *does this condo allow animals* — once per building,
and reuses the answer for every listing in that building, present and future.

Where the rest is:

- [pipeline.md](pipeline.md) — where this pass sits (it does not sit *in* the
  pipeline; it runs beside it);
- [cli.md](cli.md#fb-rent-buildings-batch--fb-rent-buildings-apply) —
  `buildings-batch`, `buildings-apply`, `buildings list`, `buildings set`;
- [data-layout.md](data-layout.md) — `data/buildings.json` and the mirror table;
- `CLAUDE.md` — the traps (a proposal is inert, the policy carries no score
  weight, `building_key` does not fuzzy-match);
- `.claude/skills/pets-policy/SKILL.md` — the procedure itself;
- `docs/plans/completed/20260827-pets-policy-research.md` — the implementation
  plan and the design rationale.

Last checked against live data: **27 August 2026**, before the first research
run.

## What the pass is for

`pets` is `unclear` on 603 of the 726 active listings that name a project. That
is not the AI being cautious — it is the ad being honest by accident. The
landlord frequently does not know either: the rule belongs to the condo, is
published by the management company, repeated in agency listings and argued
about in reviews, and it applies to every unit in the tower.

So the unit of work is the **building**. Researching per listing would pay for
the same search five times over and would still leave tomorrow's scrape of the
same tower unanswered.

## The four values

`policy` is the building's own verdict, and it is not the ad's `pets` field
rewritten — the two are independent sources, and a disagreement between them
stays visible instead of being averaged away. An ad that says "pet friendly" in
a building that forbids dogs is a fact worth seeing.

| Policy | What it means | What it does |
|--------|---------------|--------------|
| `not_allowed` | a verbatim quote says animals are forbidden | rejects every listing in the building, reason «Кондо запрещает животных» |
| `negotiable` | a size or breed limit, or anyone deciding case by case | **nothing** — the listing stays, the badge says to ask |
| `allowed` | animals are permitted | nothing |
| `unknown` | research found no source | nothing |

`negotiable` is the whole reason this cannot reuse the three-value `pets` enum
the AI fills in: the case the user explicitly wants kept has nowhere to live
there. It never rejects, in any combination.

A size-or-breed rule is `negotiable`, not `not_allowed`. So is any hint that
the management or the owner decides case by case. Silence is `unknown`, and a
source about a different building in the same chain — "The Base Downtown" for
"The Base Central" — is not evidence at all.

## Research proposes, the user confirms

Nothing auto-rejects on the strength of a web search. A researched building
lands in the store as `proposed` and is **inert**: `policy_for` hands out
nothing for it, so `rules.decide` never sees it and not a single row changes
status. Only `fb-rent buildings set` — the user's own word — makes a policy
act.

That is the whole safety property of the pass, and it is why the skill can be
run unattended and confirmed later. It is also cheap: one approval covers every
listing in the building, so the confirmation step is a handful of questions,
not a queue.

The store treats a confirmed `unknown` the same as an unresearched building:
both yield `None`. No rule needs to tell them apart.

## No score weight, in either direction

A confirmed `allowed` is tempting to reward and must not be. `CLAUDE.md`'s
tie-at-100 trap is the reason: 352 of 1123 rows once sat at exactly 100 because
small positive weights stacked, and the default sort could no longer order the
first screen. A bonus here would lift precisely the rows already easiest to
rank, and would rank a researched building above an identical unresearched one
for a reason that is about the research, not the flat.

`not_allowed` needs no weight either — it rejects outright.

Measured on the live DB with the buildings table joined and again with it
dropped: 4 rows at exactly 100 either way, out of 1122 active. `score()` never
reads the policy, and that is deliberate.

## The loop

```sh
fb-rent ai-status                      # «Здания:» — unresearched / proposed / confirmed
fb-rent buildings-batch --limit 5      # writes ../data/ai/buildings-batch-<ts>.json
# /pets-policy  — one subagent per building, WebSearch + WebFetch
fb-rent buildings-apply ../data/ai/buildings-result-<ts>.json
fb-rent buildings list --status proposed
fb-rent buildings set the-base-central --policy not_allowed
fb-rent export
```

The skill fans out **one subagent per building**, all launched in a single
message. Same reason as the photo pass: search results and fetched pages never
enter the orchestrator's context. The subagent returns JSON only — `policy`,
`confidence`, `note`, `sources[{url, title, quote}]` — and never sets a status,
never writes a file and never touches a listing.

`buildings-apply` refuses the whole file before writing anything if a policy
value is unknown, a key is missing, or a `not_allowed` arrives without a
source. A `not_allowed` with no verbatim quote is not a finding; going back for
the quote is the fix, not loosening the check.

The queue is one row per distinct building key over **active** listings with a
non-empty `project_name`, ordered by listing count, skipping keys already in
the store unless `--refresh`. Each row carries the sample `fb_id`s and the
distinct `pets` values the ads claimed, so the researcher can see a
contradiction coming.

Start with `--limit 5`, not the whole queue: the point of the first run is to
read five proposals closely and find out whether the subagent's evidence bar is
where it should be.

## The store, and why it is a file

`data/buildings.json` is the source of truth; the `buildings` table is a mirror
refreshed on every `export` and every `scrape`. Exactly the `decisions.json`
argument: a scrape can be repeated and an AI pass re-run, but a web search the
user then vetted cannot be reconstructed by any command. The file survives
`init_db`; the table exists only so `fetch_enriched` stays one query.

Keys are normalised by `building_key` — lowercase, alphanumerics, hyphen-join —
and that is **deliberately dumb**. It will not merge "The Base" and "Base
Central" on its own. A second spelling is pointed at an existing building by
hand through `aliases`, which is the honest fix; a fuzzy match that guessed
wrong would silently apply one building's rejection to another building's
listings.

## On the table

The policy rides as a badge on the existing «Проект/посёлок» cell, not as a
column of its own — the `data_quality` precedent, and for the same reason a
column would spend a full width on a word that is blank for every unresearched
building.

- `not_allowed` red, `negotiable` amber, `allowed` green, `unknown` neutral;
- a `proposed` entry gets its own class and reads «предложение, не
  подтверждено», so an unvetted proposal never looks like a decision;
- the tooltip carries the verbatim quotes and their URLs, so a rejection can be
  **checked** from the table rather than trusted.

## Journal

Append after a real run. Do not rewrite what is here.

### 27 August 2026 — implemented, not yet run

The code and the skill are in; no research has happened, and
`../data/buildings.json` does not exist yet. What the active set looks like
going in, measured on the live DB (2235 rows, 1122 active):

- **299 distinct buildings** cover **729 active listings** — mean 2.4 listings
  per building, median 1.
- The head is worth the pass on its own: DCondo Reef 40 listings, The Base
  Bukit 29, Phanason Green Place 20, The Base Central 20, The Base Uptown 19,
  Phuket Villa Suanluang 17, The Base Height 16, Siri Place Airport 15. Eight
  searches cover 176 listings.
- The tail is long: 188 of the 299 buildings are a single listing each. The
  queue is ordered by listing count for this reason — `--limit` cuts from the
  cheap end.
- 396 of the 1122 active rows name no project at all and this pass can never
  reach them.
- On the 726 active rows that do name one: `pets` is `unclear` on 603,
  `allowed` on 123, `not_allowed` on none — the ads that said so outright were
  auto-rejected by the text pass already.

Still open, and only a live run answers them:

- how often research comes back `unknown` — the number that decides whether the
  pass is worth repeating;
- whether any building's confirmed policy contradicts an ad claiming "pet
  friendly";
- whether the "same chain, different tower" failure the design expects actually
  shows up in the sources.

Deliberately left out until there is evidence: a re-research cadence
(`researched_at` older than N months → re-queue), and fuzzy building matching —
worth it only if maintaining the alias list turns out to be a chore.
