# The pets pass — one answer per building

The third pass, and the only one that leaves the machine to read the open web.
It answers one question — *does this condo allow animals* — once per building,
and reuses the answer for every listing in that building, present and future.

Where the rest is:

- [pipeline.md](pipeline.md) — where this pass sits (it does not sit *in* the
  pipeline; it runs beside it);
- [cli.md](cli.md#rent-assistant-buildings-batch--rent-assistant-buildings-apply) —
  `buildings-batch`, `buildings-apply`, `buildings list`, `buildings set`;
- [data-layout.md](data-layout.md) — `buildings.json` in the data home and the mirror table;
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
| `not_allowed` | a verbatim quote says animals are forbidden | rejects the building's listings, reason "Building forbids pets" — except the ads that say `pets: allowed` outright |
| `negotiable` | animals do live there, and the limit is about which one | **nothing** — the listing stays, the badge says to ask |
| `allowed` | animals are permitted | nothing |
| `unknown` | research found no source | nothing |

`negotiable` is the whole reason this cannot reuse the three-value `pets` enum
the AI fills in: the case the user explicitly wants kept has nowhere to live
there. It never rejects, in any combination.

**A ban with an escape hatch is still a ban.** "No pets unless specifically
permitted by the juristic office" — the phrasing the agency feeds carry for
most Phuket condos — reads as `not_allowed`, not `negotiable`: walking into the
juristic office for one rented flat is not a path this user will take, so for
them the answer is no. `negotiable` is for the building where animals are
already kept and only the size, the breed or the deposit is in question.

Research is meant to be **decisive**: one source that plainly states the ban is
enough for `not_allowed`, with no second confirmation. The verbatim quote is
still required — decisiveness is about how the finding is read, not about doing
without evidence. Silence is `unknown`, and a source about a different building
in the same chain — "The Base Downtown" for "The Base Central" — is not
evidence at all.

## The ad that says yes anyway

A `not_allowed` building does not reject the listing whose own `pets` field is
`allowed`. The rule is about the tower; "pets are welcome" in the ad is about this flat,
and it is usually a landlord who already holds the permission the rule talks
about. Rejecting those rows would throw away the only listings in a forbidding
building worth opening. Only an outright yes counts — `unclear`, the value most
ads carry, is not consent, or the building rule would reject nothing.

Several such ads in one building are a different claim: that the research read
the wrong tower, or the wrong chain member. `storage.fetch_pets_conflicts`
collects those (two or more by default) and `rent-assistant buildings conflicts`
prints them with their URLs; `ai-status` carries the count on a `Conflicts:`
line. Nothing in that path changes a status — it is a queue for the user's own
eyes, and it keeps `proposed` buildings too, since a contradiction is exactly
what should be seen *before* a policy is confirmed.

## Research proposes, the user confirms

Nothing auto-rejects on the strength of a web search. A researched building
lands in the store as `proposed` and is **inert**: `policy_for` hands out
nothing for it, so `rules.decide` never sees it and not a single row changes
status. Only `rent-assistant buildings set` — the user's own word — makes a policy
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
rent-assistant ai-status                      # the "Buildings:" block — unresearched / proposed / confirmed
rent-assistant buildings-batch --limit 5      # writes $RENT_ASSISTANT_HOME/ai/buildings-batch-<ts>.json
# /pets-policy  — one subagent per building, WebSearch + WebFetch
rent-assistant buildings-apply ~/.rent-assistant/ai/buildings-result-<ts>.json
rent-assistant buildings list --status proposed
rent-assistant buildings set the-base-central --policy not_allowed
rent-assistant export
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

`$RENT_ASSISTANT_HOME/buildings.json` is the source of truth; the `buildings` table is a mirror
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

The policy rides as a badge on the existing "Project/village" cell, not as a
column of its own — the `data_quality` precedent, and for the same reason a
column would spend a full width on a word that is blank for every unresearched
building.

- `not_allowed` red, `negotiable` amber, `allowed` green, `unknown` neutral;
- a `proposed` entry gets its own class and reads "proposed, not
  confirmed", so an unvetted proposal never looks like a decision;
- the tooltip carries the verbatim quotes and their URLs, so a rejection can be
  **checked** from the table rather than trusted.

## Journal

Append after a real run. Do not rewrite what is here.

### 28 August 2026 — fifth run: where the sitemap trick stops

Pushed the remaining 70 as far as the feed allows. 14 more buildings were
matched by hand off the second and third candidates the scorer had ranked
below the cut, 13 of them yielded a rule (8 bans, 5 allowed).

**The strict number rule has a blind spot: roman numerals in the source.**
«NOON Village Tower 2» was refused against «Noon Village Tower I» — correctly —
but the right card was «NOON Village Tower II», ranked just below it. The rule
still earns its place; it just means the top candidate being refused is not the
end of the search. The same held for «Pruksa Ville Kathu-Samkong», whose real
card carries an internal number the ad does not («Pruksa Ville 822
Kathu-Samkong»).

Matches made by meaning rather than by the letter — «The Scene Kathu» ←
«The Scene», «Kathu Country Club» ← «Phuket Country Club», «Happy Place
Village» ← «The Happy Place» — are stored with `confidence: low` and a note
naming the card they came from, so the weaker half is visible as weaker.

**Two doors that turned out to be closed.** The mirror has no public search
endpoint (`/api/` and `/graphql` are disallowed in `robots.txt` and were not
touched). Hipflat — the one independent source known to carry a real per-project
pets field — sits behind a Cloudflare challenge; a subagent got through it once
by driving Playwright, but doing that at scale is evading anti-bot protection,
not fetching a public page, and it was not done.

A final area-restricted rematch (candidates limited to the ad's own
subdistrict) surfaced 13 more pairs and **not one was genuine** — all were the
phase-number and different-project cases already refused. That is the useful
negative result: **the 56 that remain are not in the feed's 1807 Phuket
projects under any recognisable name**, and no amount of better matching will
find them.

Final state of the pass: **143 buildings confirmed** — 62 `not_allowed`,
44 `allowed`, 32 `unknown`, 5 `negotiable`. 382 rows carry "Building forbids pets"; the active set is 530 of 1401. The conflicts pile is empty.

The 56 left are one listing each and mostly informal names («NBC», «Solin
Stay», «Moji Minimal House», «The Firsts Samkong»). They are queued, not
written off: a session with web-search budget is the only remaining route, and
`unknown` is the likely answer for most.

### 28 August 2026 — fourth run: the feed has a sitemap

Continued the tail with the search budget still at 200/200. It turned out not
to be needed for most of it.

**The evidence almost always lives at a predictable URL.** Every run so far has
found its rule in one place — the FazWaz feed — and its mirror
`kaibaanthai.com` serves that page to a plain `curl` with a browser
User-Agent, publishes a sitemap, and `robots.txt` allows it. 1807 Phuket
project URLs came out of four sitemap files in one fetch. That turns "search
the web for this building" into "match a name against a canonical slug list",
which needs no search tool at all.

The pipeline: normalise both names (transliteration pairs — `chaofa`/`chao
fah`, `dcondo`/`d condo`, `panason`/`phanason`, `chanakarn`/`chanakan`), score
by token-set F1 and sequence ratio, fetch the best match, pull the FAQ sentence
with a regex, and **verify the sentence names the building it was matched to**.
59 pages fetched, 41 rules extracted, 18 pages that existed but said nothing
about pets.

**The phase number is the trap, and it is mechanical.** «NOON Village Tower 2»
matched «Noon Village Tower I» at 0.90; «The Wish Paklok» matched «The Wish
Paklok 2»; «Villa Kathu» matched «Phuket Villa Kathu 3». All three are
different buildings. The rule that catches it: the digits and roman numerals in
the two names must be the same set, or the match is refused. It runs before the
fetch and needs no judgement.

**Name matching decides where the answer comes from; it must not decide the
answer.** Weak matches were not fetched at all, and a fetched page whose quote
named a different project was recorded as `unknown` rather than guessed at. The
store keeps the matched card's name in the note, so every one of these is
checkable later without re-running anything.

Result: 41 more buildings answered — 22 `allowed`, 19 `not_allowed` — and 12
recorded as `unknown` because their card exists and is silent. 130 buildings
confirmed in total: 54 `not_allowed`, 39 `allowed`, 32 `unknown`,
5 `negotiable`. 372 rows carry "Building forbids pets"; the active set is
539 of 1401. The conflicts pile is still empty.

**70 buildings remain, one listing each, and they are the genuinely hard ones**
— names that match nothing in the feed's 1807 Phuket projects («NBC», «Baan
Kueng Resort», «The Firsts Samkong»). For those the feed has no answer at any
URL, so a session with search budget is the only way forward, and it may well
come back `unknown` for most of them.

### 28 August 2026 — third run: the tail, and the search budget

Ran the rest of the queue autonomously. Two things changed the shape of the
pass, and one stopped it.

**Half the queue was the same buildings spelled differently.** 224 keys covered
far fewer real buildings: `d-condo-reef` and `d-condo-reef-kathu` next to the
confirmed `dcondo-reef`, `panason-green-place` next to `phanason-green-place`,
three spellings of Phyll, `ratchapon-place` / `ratchaporn-place` /
`ratchaporn-place-condominium`. `building_key` refuses to merge these on
purpose, and the sanctioned fix is `--alias`, not research: five aliases onto
already-confirmed buildings and a hand-reviewed merge map of 34 more clusters
took the queue from 224 to 175 before a single search ran. **Check for spelling
variants before spending a search** — a name-overlap cluster listing is a
minute's work and the merge decision stays human.

**Small buildings do not deserve a subagent each.** 186 of the 224 covered one
listing. Grouping five buildings per subagent kept the context isolation the
fan-out exists for while cutting the agent count from 175 to 35.

**Then the session's web-search budget ran out — 200 of 200, shared across
every subagent, not per agent.** It went in the first 12 agents (60 buildings).
The later ones felt it: one finished by driving the project's own Playwright
Chromium against Startpage and cost 75k tokens and 13 minutes for five
buildings; another spent its last third guessing feed URLs by slug. Both got
answers, neither is a way to do 134 more. The run stopped there, honestly,
rather than filling the store with hollow `unknown`s.

Of the 60 buildings researched: 22 `not_allowed`, 14 `allowed`, 4 `negotiable`,
**20 `unknown`** — the first time the pass has produced a real `unknown` rate,
and it is concentrated exactly where the design predicted: moo baan whose
regulations are not published anywhere, and names too generic to identify
(«Baan Don» is a village and a road, «Chanakan Village» is not a project).

Findings worth keeping:

- **The Rich, Chanakan and Chaofa Garden Home are name-collision minefields.**
  A dozen similarly named estates per developer, numbered phases, and the
  district in the name. Several subagents correctly refused to carry one
  phase's rule to another and returned `unknown`.
- **The feed contradicts itself at the unit level, and that is signal.** The
  Green Golf Residence: the project card says no pets, individual unit ads on
  the same site say pet-friendly. Kathu Golf: the feed bans, a dedicated site
  for a unit in the same building sells it as «Small Pets Allowed». Both were
  softened to `negotiable` — not because of the juristic-office clause, but
  because an independent source contradicted the ban.
- **OTA cards are a usable independent source.** Trip.com, Booking, Expedia and
  Agoda carry per-building pet rules that are not the FazWaz feed. They gave
  the only two `high` bans of the run (Diamond Resort, DLUX) and the
  independent confirmation for Supalai Park @ Downtown and Citygate Kamala.
  They are about short-let operators rather than the house rule, so they
  corroborate; they do not decide alone.
- 343 rows across the DB now carry "Building forbids pets"; the active set
  is 562 of 1401. 77 buildings confirmed: 35 `not_allowed`, 17 `allowed`,
  5 `negotiable`, 20 `unknown`. The `conflicts` pile is still empty.

**134 buildings remain, one listing each.** They need a session with search
budget: `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` raised, or simply a fresh
one. At the observed rate — five buildings per subagent, roughly seven searches
per subagent — the tail is about 27 subagents and 190 searches.

### 27 August 2026 — second run, twelve buildings

The next twelve off the queue, one subagent each, with the mirror warning from
the first run written into every prompt.

- **Again nothing came back `unknown`** — 0 of 12. Eight `not_allowed`, three
  `allowed`, one `negotiable`.
- **The FazWaz field is per-object, not boilerplate.** The open question from
  the first run has an answer: the same feed says «all kind of pets are
  allowed» for Phuket Villa Suanluang and prints the ban for The View Condo
  Suanluang, its neighbour. Read the feed; just do not count its four mirrors
  as four votes.
- **PropertyScout's version of the sentence *is* boilerplate.** «X does not
  allow pets unless permitted by the juristic office» turns up on pool-villa
  estates (The Lake House, Two Villas Tara) where a blanket ban is not
  credible. It had been taken as independent corroboration for Phyll Phuket;
  it is not. Both findings are now in the skill.
- **One building had genuinely independent evidence**: The Base Height —
  Hipflat (naming the manager, Plus Living Management), agency ads on
  Thailand-Property («Strictly: No Pets Allowed») and a Trip.com unit card.
  It is the only `high` among the eight bans.
- **The first confirmed `allowed`**: The Base Cherngtalay, on the developer's
  own site — «Pet-welcome», a Pet Park in the amenities and separate "Pet"
  unit variants in the floor plans. Caveat worth keeping: several agencies say
  only Building B, levels 2–3, are the pet-friendly ones.
- **Estates of houses are not condos**, and the pass now says so. Phuket Villa
  Suanluang, Supalai Bella Thalang and Siri Place Airport are moo baan; no
  village regulation exists online for any of them. Siri Place is the sharp
  case: both "rules" the search returned were boilerplate pointing opposite
  ways, while individual lots inside the estate are tagged both «เลี้ยงสัตว์ได้»
  and «pets prohibited» — the owner decides, which is what `negotiable` means.
- **Supalai Lagoon splits and `building_key` cannot see it.** The ban belongs
  to the condo towers; the houses at the same address are allowed by the same
  feed. Confirmed `not_allowed` because both listings on hand are condos — the
  note carries the warning for the day a house shows up.
- 41 active rows rejected. Across the DB 245 rows now carry "Building forbids pets"; the active set is 637 of 1401. 224 buildings left unresearched,
  17 confirmed.
- The `conflicts` pile is empty: not one ad claiming `pets: allowed` sits in a
  forbidding building right now.

**A dedupe ran mid-pass, from another session.** At 19:37–19:38 UTC
`rent-assistant dedupe` collapsed 834 duplicates (2235 listings → 1401), between the
batch being built and the results being applied. Nothing was lost — every
collapsed row is in the `duplicates` table with its canonical id — but the
batch file's counts, the apply report's counts and the confirmation's counts
were each measured on a different side of it (The Base Height read 16, then
11, then 8). **A listing count in a batch file is a snapshot, not a fact**:
re-measure before quoting one to the user.

### 27 August 2026 — first run, the five biggest buildings

One subagent per building over the head of the queue: DCondo Reef, The Base
Bukit, Phanason Green Place, The Base Central, The Base Uptown.

- **Nothing came back `unknown`** — 0 of 5. Every building had a source.
- **Every source was the same source.** All five hits trace to one FazWaz feed,
  reprinted verbatim by KaiBaanThai, LivePhuket, Thaiger Property and
  homes-thailand, carrying one sentence: «No pets are allowed at X unless
  specifically permitted by the juristic office.» No management-company
  document was found for any of the five, and the "same chain, different tower"
  failure the design expected did not appear — but the mirrors did, and they
  look like corroboration until you read the URLs. Treat four agency sites
  agreeing as **one** source.
- The pass first read that sentence as `negotiable` on all five. The user
  rejected the reading — walking into a juristic office for one rented flat is
  not a path they will take — and the classification rule changed; see the
  policy table above. All five are now confirmed `not_allowed`.
- **130 active rows left the table**: DCondo Reef 40, The Base Bukit 31, The
  Base Central 22, The Base Uptown 19, Phanason Green Place 18. Across the
  whole DB 327 rows now carry "Building forbids pets" as their reason — the
  building check runs early, so it also takes over rows that another rule had
  already rejected. The active set stands at 999 of 2235.
- Aliases earned their keep immediately: `base central` and `the base bukit
  phuket` pulled in 2 more rows each over what the proposal counted.
- **One contradiction, and it is real.** Phuket Insider lists THE BASE Central
  Phuket among Phuket's pet-friendly condos («no more than two pets per unit»),
  against the same feed's ban. The user confirmed the ban anyway.
- The ad-level disagreement barely exists in these five: **zero** ads claiming
  `pets: allowed` in the four Sansiri buildings, two in Phanason Green Place —
  which is the entire contents of `buildings conflicts` today (1 building, 2
  listings).
- 292 buildings left unresearched.

What the run leaves open: whether a source that is not the FazWaz feed exists
at all for a Phuket condo, and what `unknown` looks like when one does not.
Five buildings is not enough to say the queue is worth grinding through — but
it is enough to say the first five paid for themselves.

### 27 August 2026 — implemented, not yet run

The code and the skill are in; no research has happened, and
`buildings.json` does not exist yet in the data home. What the active set looks like
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
