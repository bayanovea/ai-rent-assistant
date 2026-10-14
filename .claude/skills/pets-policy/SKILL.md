---
name: pets-policy
description: Third pass over the rent-assistant listings — the building's house rule on pets, once per building: buildings-batch → a fan-out of subagents with web search → buildings-apply → confirmation by the user → buildings set → export. Triggers ONLY on an explicit request: "research the pets rules", "find out where a cat is allowed", "pets policy", "/pets-policy". A non-empty building queue in `ai-status` is not a reason by itself — the pass costs a web search per building and runs when the user asks for it.
---

# The house rule on pets — per building, not per listing

Ads lie by omission: `pets` in the table is `unclear` almost everywhere,
and that is honest — the landlord often really does not decide. The condo
decides: the building has a house rule, it is published by the management
company and repeated in agency listings and reviews, and it is one rule
for every flat in the tower.

The whole design of the pass follows from this:

- **The unit of work is a building, not a listing.** One search answers for
  every flat in the building — including the ones scraped tomorrow.
- **Research proposes, the user decides.** Everything this pass brings
  lands in `buildings.json` with the status `proposed` and changes not a
  single row. Only a policy the user has confirmed by hand
  (`rent-assistant buildings set`) rejects listings. That is one
  confirmation per N listings — it is cheap, and it is the only thing
  standing between a bad web search and a crossed-out building.

You are the orchestrator here: search results and web pages never enter
your context. The subagents read them — one per building. You collect
their JSON, apply it and bring the questions to the user.

The listings' verdict, `score` and `status` are neither your business nor
the subagents': `rules.py` computes them from the fields. This pass
**never touches** the listing's own `pets` field: they are two independent
sources, and a mismatch between them ("pet friendly" in a building where
pets are banned) is a finding, not a conflict to average out. The finding
has two consequences, and both are already in the code:

- a listing that says **outright** "pets allowed" survives the building's
  ban and stays in the table. The house rule is about the tower, the
  "allowed" is about this flat, and it is usually an owner who already has
  the management's permission;
- when there are **several** such listings in one building, that is no
  longer noise but a sign that the research read the wrong building.
  `rent-assistant buildings conflicts` collects them — the user sorts them
  out by hand.

You write nothing yourself except the results file: both the store and the
table change only through the CLI.

## 1. Check the queue

```sh
rent-assistant ai-status
```

Look at the "Buildings" block:

- `Unresearched: 0` — nothing to search, say so and stop (unless the user
  asks to re-check a specific building — then
  `rent-assistant buildings-batch --key "<name>" --refresh`);
- `Proposed: N` — N proposals are waiting for the user's answer. This is a
  queue of questions, not of finished work: if it is non-empty, start with
  step 6 — finish those first (`rent-assistant buildings list --status
  proposed`), then search for new ones;
- `Confirmed: N` — these policies are already in effect.

Only the active set counts: buildings whose every listing the user has
rejected (or `rules.py` has) do not enter the queue, and neither do
listings without a `project_name`.

## 2. Build the batch

```sh
rent-assistant buildings-batch --limit 5
```

**The first run is exactly `--limit 5`, not the whole queue.** The point is
to read five proposals carefully and understand where the subagent sets its
bar of evidence before trusting it at volume.

The command prints a path like `.../ai/buildings-batch-YYYYMMDD-HHMMSS.json`.
Flags: `--limit N`, `--key "<name or key>"` (repeatable, pulls a building
past the queue), `--refresh` (include buildings already researched —
otherwise they are skipped).

Read the whole batch, it is small. It contains:

- `research_prompt` — the contract with the subagent, verbatim; put it in
  the prompt as it is;
- `policies` — the four allowed values;
- `buildings[]` — per building: `key` (normalized, return the same one),
  `name` (the spelling most listings use), `area`, `listings` (how many
  listings it covers), `fb_ids` (examples), `pets` (what the listings
  themselves managed to say).

The queue is sorted by the number of listings, descending: `--limit` spends
the budget on buildings that cover more flats.

## 3. Fan out the subagents — one per building

For every element of `buildings` — its own subagent. Launch all of them
**in a single message** so they run in parallel:

```
Agent(subagent_type: "general-purpose", prompt: <instruction below>)
```

Put into each prompt:

- `research_prompt` from the batch, verbatim;
- `key`, `name`, `area`, `listings`, `pets` of this building;
- the requirement to return **JSON only**, with no explanation around it;
- the requirement to search with `WebSearch` and read what was found with
  `WebFetch`.

Where to search, by decreasing weight:

1. the management company's or the developer's site — the house rules /
   condo rules / regulations section;
2. agency pages for this condo (FazWaz, Hipflat, Thailand-Property,
   DDproperty and the like) — they usually have a line about pets;
3. Google Maps and Facebook reviews, expat forum threads about this
   building.

The research rules — copy them into the prompt verbatim, they are what
makes the pass worth running at all:

- **Grounds for a ban means `not_allowed`.** Decide boldly: one source that
  says outright that pets may not be kept in the building is enough; there
  is no need to wait for a second confirmation or to soften the finding.
- **The management-office caveat does not cancel the ban.** "No pets unless
  specifically permitted by the juristic office" is `not_allowed`: the user
  will not go to the condo's juristic office for one rented flat, so for
  them it means "no", not negotiation.
- **`not_allowed` still only with a verbatim quote** from the source. A
  retelling, an inference from silence and "condos usually do not allow
  it" do not qualify: apply rejects **the whole file**, not one entry.
  Boldness is about how to read what was found, not about doing without
  it.
- **`negotiable` is a building where animals already live,** and the only
  question is which: "dogs up to 10 kg", "small breeds only", "cats yes,
  dogs no", a separate fee or deposit per animal, "at the unit owner's
  discretion". A building whose base rule is a ban does not belong here.
- **Silence is `unknown`, not `allowed`.** The absence of a rule on the
  site does not mean pets are let in.
- **Sources saying the opposite do not change the policy, but are
  recorded.** A guide calls the building pet-friendly, residents write
  about their dogs in reviews — put such a source into `sources` and
  mention it in `note`: the user sorts out discrepancies by hand.

What counts as one source — measured on two runs; put this into the
subagent's prompt verbatim:

- **fazwaz.com, fazwaz.co.th, kaibaanthai.com, livephuket.com,
  thaigerproperty.com, homes-thailand.com, samuitimes.com, asia.villas are
  one FazWaz feed.** They reprint each other word for word. Four such sites
  look like four confirmations until you read the URLs; if the rule was
  found only there, `confidence` is no higher than `med`.
- **The PropertyScout sentence "X does not allow pets unless permitted by
  the juristic office" is their site-wide template;** it appears even on
  pool-villa estates. It does not qualify as an independent confirmation.
- The FazWaz field itself is **not** a template: neighbouring properties
  can carry opposite values (Phuket Villa Suanluang — "all kind of pets are
  allowed", The View Condo Suanluang — a ban). So the feed is worth
  reading, just not as four votes.
- Independent confirmation means: the developer's or management company's
  site, Hipflat, Thailand-Property, Thai portals (propertyhub, condonayoo,
  zmyhome), residents' reviews, this building's cards on Trip.com /
  Booking / TripAdvisor.

**A housing estate is not a condo.** Moo baan, townhouses, villas: condo
rules do not apply to them, there is usually no juristic regulation
online, and the house owner decides. The honest answer there is `unknown`
or `negotiable`, not a ban carried over from somewhere else. A separate
case is a complex with both towers and houses (Supalai Lagoon): the rule of
the condo part does not extend to the houses, and `building_key` does not
tell them apart — say so in `note`, it is the user's decision.
- **A source about another building of the same chain is not evidence.**
  The Base Downtown instead of The Base Central, "Laguna" in general — that
  is `unknown`. The name in the quote must match the building from the
  batch.
- `confidence`: `high` — the rule from the management company; `med` — two
  agencies agree, or an agency and reviews; `low` — a single retelling.
- The subagent **does not set** the status, **does not write** files,
  **does not touch** listings. At most a `note` of 1–2 sentences: who the
  source is and what the doubt is.

Each subagent returns one object:

```json
{ "key": "the-base-central-phuket",
  "name": "The Base Central Phuket",
  "policy": "not_allowed",
  "confidence": "high",
  "note": "House rules from the management company: no pets. Two agencies confirm.",
  "sources": [
    {"url": "https://...", "title": "The Base Central — House Rules",
     "quote": "Pets are not allowed in the building."}
  ] }
```

Copy `key` from the batch verbatim — the entry lands in the store under it.

## 4. Collect the result

Put the subagents' objects into a file next to the batch:
`<AI_DIR>/buildings-result-<same ts>.json`.

```json
{ "model": "claude-opus-5 (claude-code subagents)",
  "results": [ <objects from step 3> ] }
```

## 5. Apply

```sh
rent-assistant buildings-apply <path to buildings-result-*.json>
```

When in doubt, `--dry-run` first. The command prints per proposal: name,
key, policy, `confidence` and **how many listings it will cover** — that
number goes into the question to the user.

What it does and does not do:

- everything is written as `status: "proposed"` — **no listing changes its
  status**, however many `not_allowed` come in;
- a building the user has already confirmed **is skipped** with a warning:
  a proposal on top of a confirmation would silently cancel their word.
  Re-checking such a building is a deliberate action: delete the entry
  from `buildings.json` by hand, or confirm the new answer through
  `buildings set`;
- an unknown policy, an empty `key`, or `not_allowed` without a source
  carrying a URL **and** a quote fail the whole file. Do not "fix" this by
  weakening the check — go back to the subagent for the quote, or set
  `unknown`.

## 6. Confirmation — one question per proposal

```sh
rent-assistant buildings list --status proposed
```

For each proposal — its own `AskUserQuestion`. Show in it:

- the building's name and district;
- the policy and `confidence`;
- the sources' quotes and URLs — in full, this is what the user checks;
- how many active listings are affected and what exactly will happen
  (`not_allowed` — so many rows move to rejected);
- if the listings themselves claimed the opposite (`pets` from the batch
  argues with the finding) — say so plainly.

The option "leave as is" must always be there: an unaccepted proposal
simply stays `proposed` and keeps doing nothing.

Record what the user confirmed:

```sh
rent-assistant buildings set the-base-central-phuket --policy not_allowed \
  --note "management: no pets" --alias "base central"
```

`--policy` is one of the four (the user may confirm something other than
what was proposed). `--alias` is repeatable: add it when you see the same
building spelled differently. Keys **are not merged automatically** and
must not be: a wrong merge would reject another building's listings.

`buildings set` is the only command after which a policy starts to work.

## 7. Export and report

```sh
rent-assistant export
rent-assistant buildings conflicts
```

Report briefly:

- which buildings were confirmed and how many rows they rejected with the
  reason "Building forbids pets";
- `negotiable` — buildings where the question is open: these listings
  stay, and there is something to ask the agent about;
- discrepancies from `rent-assistant buildings conflicts` — buildings where
  several listings promise pets against the rule: list them with links,
  this is a queue for manual review, not finished work;
- which proposals the user declined or postponed (still `proposed`);
- buildings that came back `unknown` — the web search found nothing for
  them, only the agent has the answer; list them with the number of
  listings;
- how many buildings remain in the queue.

The decision on a specific listing is the user's, through
`rent-assistant reject / unreject / shortlist`. Do not call these commands
yourself unless asked.
