# Docs

Start with [pipeline.md](pipeline.md) — it shows how the pieces fit together
and points at the rest.

| File | What it covers |
|------|----------------|
| [pipeline.md](pipeline.md) | The flow from a Facebook card to a phone call: who runs each step, what it writes, and who decides what. |
| [cli.md](cli.md) | Every command and every flag. |
| [data-layout.md](data-layout.md) | What lives in `data/` and `config/`, what is disposable, what is irreplaceable, and the DB tables. |
| [scraping.md](scraping.md) | Price bands, the detail budget, repairing single listings, troubleshooting. |
| [ai-enrichment.md](ai-enrichment.md) | The text pass: the extraction contract, the five auto-rejections, the score, growing the field registry. |
| [photo-analysis.md](photo-analysis.md) | The photo pass: live measurements, decisions worth not undoing, open gaps. **Read before touching photos.** |
| [dedupe.md](dedupe.md) | Five duplicate signals, what the photo one caught, and the group-veto fix. |
| [scrape-log.md](scrape-log.md) | Journal: what has been collected, with which flags, and where to continue. |
| [plans/completed/](plans/completed) | The implementation plan per iteration, with the design rationale of the day. |

The diagrams are checked-in SVGs so that any markdown preview shows them
without a mermaid extension. Their sources live next to them in
[`diagrams/`](diagrams) as `.mmd` files — edit the source, then run
`scripts/render-diagrams.sh` to regenerate the SVG.

`CLAUDE.md` in the repository root is not documentation — it is the list of
traps that a coding agent must not walk into.
