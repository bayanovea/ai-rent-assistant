# Pipeline

The project is a funnel: a few hundred Facebook cards go in, a handful of flats
worth a phone call come out. Every stage exists to throw away work the next
stage would otherwise pay for — which is why the order matters and why two of
the stages are yours, not the machine's.

```mermaid
flowchart TD
    FB(["Facebook Marketplace<br/>Phuket Property Rentals"])

    subgraph collect["1 · Collect — Playwright, no AI"]
        direction TB
        scrape["fb-rent scrape<br/>feed cards → detail pages<br/>text · gallery URLs · photo hashes"]
        dedupe["fb-rent dedupe<br/>merge reposts and re-listings"]
        scrape --> dedupe
    end

    subgraph text["2 · Read the text — /analyze, Opus"]
        direction TB
        batch["ai-batch<br/>→ ai/batch-ts.json"]
        read["Claude reads the raw text<br/>+ config/requirements.md"]
        apply["ai-apply<br/>fields → rules.py → verdict"]
        batch --> read --> apply
    end

    subgraph mine["3 · Your pass — the table"]
        review["open rentals.html<br/>rate / reject / shortlist<br/>→ decisions.json"]
    end

    subgraph photo["4 · Read the photos — /photo-analyze, Sonnet fan-out"]
        direction TB
        fetch["photos-fetch<br/>galleries for the survivors"]
        pbatch["photo-batch<br/>→ ai/photo-batch-ts.json"]
        agents["one subagent per listing<br/>reads the gallery, returns JSON"]
        papply["photo-apply<br/>photo fields → rules.py again"]
        fetch --> pbatch --> agents --> papply
    end

    out(["shortlist → write to the agent"])

    FB --> collect
    collect --> text
    text --> mine
    mine -->|"what survived your rejections"| photo
    photo -->|"disagreements with the text"| mine
    mine --> out
```

The arrow back from stage 4 to stage 3 is the point of the photo pass: its
product is not new facts but **contradictions** — a `furnished: full` that the
pictures show as one empty bedroom, a gallery that turns out to be a 3D render.
Those land in the table and you look again.

## Stage by stage

| # | Step | Who runs it | Reads | Writes |
|---|------|-------------|-------|--------|
| 0 | `fb-rent login` | you, once every 1–4 weeks | — | `data/browser-profile/` |
| 1 | `fb-rent scrape` | you / cron | Facebook | `listings`, `photo_hashes`, `photos/<fb_id>/`, CSV + HTML |
| 1b | `fb-rent dedupe` | you | the DB | `duplicates`, purged photo dirs |
| 2 | `/analyze` skill | Claude Code (Opus) | `listings.description`, `config/requirements.md`, `config/ai_fields.json` | `ai_analysis.data_json`, auto-rejections |
| 3 | reading the table | **you** | `data/rentals.html` | `data/decisions.json` |
| 4a | `fb-rent photos-fetch` | you | stored gallery URLs | `photos/<fb_id>/` |
| 4b | `/photo-analyze` skill | Claude Code (Sonnet subagents) | the photo files | `ai_analysis.photo_data_json` |
| 5 | `fb-rent export` | anything above | the DB + `decisions.json` | `rentals.csv`, `rentals.html` |

Stage 3 is not a formality. The photo queue skips everything you rejected, so
every listing you throw out by hand before stage 4 is tokens not spent. Neither
`/analyze` nor `ai-status` will offer to start the photo pass — you ask for it.

## Who decides what

Extraction is the AI's job. The *decision* is not, and the final word is not
the AI's either.

```mermaid
flowchart LR
    txt["text pass<br/>ai_analysis.data_json"] --> merge["merged on read<br/>text fields + photo fields"]
    pho["photo pass<br/>ai_analysis.photo_data_json"] --> merge
    merge --> rules["rules.py<br/>decide + score"]
    rules --> status["status in the export"]
    you["decisions.json<br/>your reject / unreject / shortlist / rate"] -->|"outranks the rules<br/>in both directions"| status
```

- **The AI extracts.** Fields, each with a confidence and a quote it can be
  traced back to. It never writes `verdict`, `score` or `status`; values for
  those in a result file are ignored.
- **`rules.py` decides.** Five conditions auto-reject; the district is worth up
  to ±12 points of the score. The same facts therefore always produce the same
  outcome — see [ai-enrichment.md](ai-enrichment.md).
- **You override.** `decisions.json` beats the rules in both directions and is
  the only file here that a DB wipe would actually lose.

The two AI passes write into separate columns and are merged on read, so
neither can clobber the other: drop the photo columns and the text analysis is
untouched.

## The batch contract

Both passes talk to Claude Code through files, not an API. The CLI's whole job
is to write a batch out and read a result back.

```mermaid
sequenceDiagram
    participant CLI as fb-rent
    participant FS as data/ai/
    participant AI as Claude Code
    CLI->>FS: ai-batch → batch-ts.json<br/>(requirements + registry + raw text)
    FS->>AI: read
    AI->>FS: result-ts.json<br/>(fields + verdict + evidence)
    FS->>CLI: ai-apply
    CLI->>CLI: validate vs registry → rules.py → DB
```

A result file is rejected wholesale if it carries a key the registry does not
know — new signals go into `proposed_new_fields`, and you decide whether they
become columns ([`ai-field add`](cli.md#fb-rent-ai-field)). The photo pass
mirrors this with `photo-batch` / `photo-apply`, keyed on `photos_hash` instead
of the text hash.

## The order of work for a wide sweep

Downloading galleries costs a second or two per listing and hundreds of
megabytes over a big run, and most of those listings will be rejected on their
text alone. So a wide sweep splits collection from downloading:

```sh
fb-rent scrape --bands 4 --max 60 --days 7 --detail-cap 150 --no-photos
fb-rent dedupe
# /analyze  — the text pass, does not touch Facebook
# you read the table and cut what is obviously wrong
fb-rent photos-fetch --all      # only for what survived
# /photo-analyze
```

The stored gallery URLs are signed and expire in about a week, which is what
makes this order possible at all — and what bounds it. Past the expiry only the
detail page can hand out fresh links (`fb-rent scrape --ids <fb_id>`).
