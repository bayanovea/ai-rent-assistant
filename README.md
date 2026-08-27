# fb-rent

Finding a long-term rental on Phuket means reading a few hundred Facebook
Marketplace ads that all say "fully furnished, pets negotiable". This scrapes
them into a local database, has Claude read every ad and every photo against
one written list of requirements, and hands back a single sortable HTML page
where the forty tabs you would have opened are already sorted, scored and
mostly rejected.

Everything runs locally. There is no API key and no service: the AI is Claude
Code itself, reading files the CLI writes.

![The pipeline in five stages: collect, read the text, cut what is wrong, read the photos, shortlist](docs/diagrams/pipeline-flow.svg)

Each stage exists to spare the next one work: the scrape merges reposts before
the AI reads anything, the text pass auto-rejects on facts before you look, and
the photo pass — the expensive one — runs only on what *you* left alive.
[docs/pipeline.md](docs/pipeline.md) has the same picture with a stage table,
who writes what, and which stages can be run on their own.

## Install

Requires Python 3.11 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

Dependencies are Playwright (the scrape), Click (the CLI), python-dateutil (the
date parsing) and Pillow (the photo downscale).

The `-e` is required, not a convenience: `config/ai_fields.json` and
`config/requirements.md` are read from the checkout through a path relative to
the package source and are not packaged into a wheel. After a plain
`pip install .` every `ai-*` command and `fb-rent export` fail to find the
field registry.

## Quickstart

```sh
fb-rent login          # one-time headed login, session persisted to ../data/
fb-rent scrape         # headless scrape, writes ../data/rentals.{db,csv,html}
fb-rent dedupe         # merge reposts and the same flat listed by several agents
fb-rent export         # regenerate CSV + HTML from the DB without scraping
```

Then, in a Claude Code session:

```
/analyze               # ai-batch -> Claude reads the ads -> ai-apply -> export
/photo-analyze         # only after you have reviewed the table yourself
/pets-policy           # look up each condo's house rule about animals, on request
```

The pets pass is keyed by building, not by listing: one web search answers for
every flat in the tower, and nothing it finds rejects anything until you
confirm it by hand. [docs/pets-policy.md](docs/pets-policy.md) has the four
policy values and why a proposal is inert.

```sh
fb-rent buildings-batch --limit 5     # what to research
fb-rent buildings-apply <file>        # store the findings as proposals
fb-rent buildings list --status proposed
fb-rent buildings set <key> --policy not_allowed   # your confirmation
```

Open `../data/rentals.html` in a browser: one self-contained file, no server
and no internet needed. Sort by a header, filter across every column from the
search box, switch sets with the Разобрать / Shortlist / Longlist / Rejected /
All tabs — the first is the queue of what you have not looked at, the other
three are what you have.
Every AI-filled cell carries the quote it came from in its tooltip.

Record what you think as you go — this is the part no machine does for you:

```sh
fb-rent rate 1234567890 4.5              # 4.5+ shortlist, 4 longlist, 3- reject
fb-rent reject 1234567890 --reason "агент не отвечает"
```

## What it is looking for

`config/requirements.md` — a letter to the agent in plain prose, a priority
checklist and a 1–5 district scale. It is pasted verbatim into every batch, so
changing what "good" means is editing that markdown, not the code.

The *decisions* are not the AI's: `rules.py` computes the verdict and the score
from the extracted fields, so the same facts always produce the same outcome.
Six conditions auto-reject — pets explicitly forbidden by the ad, a condo whose
house rule you confirmed forbids them, Patong, above 35 000 THB, an explicit
studio, a stated size under 35 m². Everything else is a remark
in the verdict, never a rejection. Your own `reject` / `shortlist` outranks all
of it and lives in `../data/decisions.json` — which, with the confirmed pets
policies in `../data/buildings.json`, is what cannot be rebuilt by re-running
anything.

## Where things live

Everything the machine produced goes into the sibling `data/` directory
(`rentals.db`, `rentals.csv`, `rentals.html`, `photos/`, `decisions.json`,
`buildings.json`);
everything you edit is version-controlled in `config/`. Override the base path
with `FB_RENT_HOME`. Details: [docs/data-layout.md](docs/data-layout.md).

## Docs

- [pipeline.md](docs/pipeline.md) — how the whole thing hangs together
- [cli.md](docs/cli.md) — every command and flag
- [scraping.md](docs/scraping.md) — price bands, the detail budget, repairs, troubleshooting
- [ai-enrichment.md](docs/ai-enrichment.md) — the text pass and the field registry
- [photo-analysis.md](docs/photo-analysis.md) — the photo pass, measured
- [pets-policy.md](docs/pets-policy.md) — the pets pass: one house rule per building
- [dedupe.md](docs/dedupe.md) — five duplicate signals
- [data-layout.md](docs/data-layout.md) — files, config, database
- [scrape-log.md](docs/scrape-log.md) — what has been collected so far
- [docs/plans/completed/](docs/plans/completed) — the design rationale, iteration by iteration
