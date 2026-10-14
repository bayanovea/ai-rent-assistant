"""The contract between the research queue and the pets-policy pass.

Same shape as :mod:`.photo_batch`: a ``*-batch`` function writes a JSON file
describing what to look up, the skill fans out over subagents and writes a
result file, and an ``*-apply`` function validates that file hard before
anything is stored. Two things make this pass different from the other two.

**The unit of work is a building, not a listing.** One web search answers for
every flat in the tower, present and future, so the queue is
:func:`.storage.fetch_pending_buildings` and a result entry is keyed by
:func:`.buildings.building_key`.

**Apply proposes; it never decides.** Every entry lands as ``status:
"proposed"``, which :func:`.buildings.policy_for` refuses to hand out — so a
run of this pass cannot change one listing's status, however confident the
model was. The user confirms afterwards (``rent-assistant buildings set``), and that
is the only path to a policy that rejects anything. A building the user has
already confirmed is therefore skipped rather than overwritten: re-proposing
it would silently retract their word.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from . import ai_batch, buildings, config, storage

# Re-exported so a caller can catch one error type across all three passes.
BatchError = ai_batch.BatchError

# What the subagent is asked to come back with. It is part of the batch file
# rather than of the skill alone so that a batch stays readable on its own —
# and so the four policy values live next to the data they describe.
RESEARCH_PROMPT = """\
Find the house rule on pets for every building in `buildings`.

Look for the management company's site and the house rules, agency pages
for this condo, Google/Facebook reviews and expat forum threads. The
building's name and district are in `name` and `area`; `pets` shows what
the ads themselves have said about it (a contradiction with them is a
finding, not an error).

Return JSON: {"results": [{"key", "name", "policy", "confidence", "note",
"sources": [{"url", "title", "quote"}]}]}. `key` is exactly the one that
came in the batch.

`policy` is one of four:
- `not_allowed` — the house rule forbids animals. A ban with a caveat
  about the management's permission («no pets unless specifically
  permitted by the juristic office») goes here too — the user will not
  go to the juristic office for one rented flat, so for them it is "no",
  not a negotiation;
- `negotiable` — animals do live in the building, the only question is
  which: a size or breed limit, a separate fee or deposit for the animal,
  "at the owner's discretion". This is not about a building whose base
  rule is a ban;
- `allowed` — the building explicitly allows animals;
- `unknown` — no sources. Silence is `unknown`, not `allowed`.

Decide boldly: one source that states the ban outright is enough for
`not_allowed` — do not wait for a second confirmation and do not soften
the finding to `negotiable` because of caveats.

`not_allowed` without a verbatim quote from the source is not accepted:
apply rejects the whole file. A source about another building of the
same chain (The Base Downtown instead of The Base Central) does not count
as evidence.

If sources saying the opposite turn up along the way — a guide calls the
building pet-friendly, residents mention their dogs in reviews — the
policy does not change, but put them in `sources` and say so in `note`:
the user sorts out such discrepancies by hand.

`confidence` is `high` | `med` | `low`. Do not set a status, do not write
files, do not touch the listings: only the CLI reads and changes them.
"""


def build_buildings_batch(
    conn: Connection,
    store: dict[str, dict[str, Any]] | None = None,
    *,
    limit: int | None = None,
    keys: list[str] | None = None,
    refresh: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble the JSON batch of buildings awaiting research.

    The queue comes from :func:`.storage.fetch_pending_buildings`, which
    already drops the buildings whose every ad is out, the ads that name no
    building and the buildings already in ``store`` (unless ``refresh``).

    ``keys`` narrows it to the buildings named — normalised the same way the
    queue is, so a spelling off the command line finds its row — and ``limit``
    caps what is left. The order the queue arrived in is kept: it is by
    listing count descending, so a cap spends the budget on the buildings
    covering the most flats.
    """
    if store is None:
        store = buildings.load_buildings()

    queue = storage.fetch_pending_buildings(conn, store, refresh=refresh)

    if keys is not None:
        wanted = {buildings.building_key(k) for k in keys}
        wanted.discard("")
        queue = [row for row in queue if row["key"] in wanted]

    if limit is not None:
        queue = queue[: max(limit, 0)]

    return {
        "generated_at": now or ai_batch._now_iso(),
        "policies": sorted(buildings.POLICIES),
        "research_prompt": RESEARCH_PROMPT,
        "buildings": queue,
    }


def write_buildings_batch(
    batch: dict[str, Any], out_dir: Path | str | None = None
) -> Path:
    """Write ``batch`` to ``<out_dir>/buildings-batch-<ts>.json``, return the path."""
    directory = Path(out_dir) if out_dir is not None else config.AI_DIR
    directory.mkdir(parents=True, exist_ok=True)

    path = ai_batch.timestamped_path(
        directory, "buildings-batch", batch.get("generated_at")
    )
    path.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _has_evidence(sources: Any) -> bool:
    """Whether at least one source carries both a URL and a verbatim quote."""
    if not isinstance(sources, list):
        return False
    for source in sources:
        if not isinstance(source, dict):
            continue
        if str(source.get("url") or "").strip() and str(
            source.get("quote") or ""
        ).strip():
            return True
    return False


def validate_buildings_results(results: Any) -> None:
    """Reject the whole file if any entry is unusable, before writing anything.

    Three failures, all fatal for the file rather than for the entry: a
    ``key`` that normalises to nothing (there is no building to store it
    under), a ``policy`` outside :data:`.buildings.POLICIES` (a "no pets"
    that no rule matches would look researched and reject nobody), and a
    ``not_allowed`` with no source carrying both a URL and a quote — the one
    verdict that can wipe out a whole building's listings is the one that has
    to be checkable from the table.
    """
    if not isinstance(results, list):
        raise BatchError("result must carry a 'results' list")

    for index, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise BatchError(f"results[{index}] must be a JSON object")

        key = buildings.building_key(entry.get("key") or entry.get("name"))
        if not key:
            raise BatchError(f"results[{index}] is missing key")

        policy = entry.get("policy")
        if policy not in buildings.POLICIES:
            raise BatchError(
                f"results[{index}] ({key}) has unknown pets policy {policy!r} "
                f"(known: {', '.join(sorted(buildings.POLICIES))})"
            )

        confidence = entry.get("confidence")
        if confidence is not None and confidence not in buildings.CONFIDENCES:
            raise BatchError(
                f"results[{index}] ({key}) has unknown confidence "
                f"{confidence!r} (known: "
                f"{', '.join(sorted(buildings.CONFIDENCES))})"
            )

        if policy == "not_allowed" and not _has_evidence(entry.get("sources")):
            raise BatchError(
                f"results[{index}] ({key}) says not_allowed with no source "
                "carrying both a url and a quote — unusable, a rejection has "
                "to be checkable"
            )


@dataclass
class BuildingsApplyReport:
    """What :func:`apply_buildings_results` did, in counters plus explanations."""

    proposed: list[dict[str, Any]] = dataclass_field(default_factory=list)
    skipped: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)
    affected: dict[str, int] = dataclass_field(default_factory=dict)
    dry_run: bool = False

    @property
    def applied(self) -> int:
        """Number of proposals written (or that would be, when dry-running)."""
        return len(self.proposed)


def apply_buildings_results(
    conn: Connection,
    store: dict[str, dict[str, Any]] | None = None,
    result: dict[str, Any] | None = None,
    *,
    path: Path | str | None = None,
    dry_run: bool = False,
    now: str | None = None,
) -> BuildingsApplyReport:
    """Validate a research result file and store its entries as proposals.

    Everything is written with ``status: "proposed"``, so nothing this
    function does can change a listing's status — the report's ``affected``
    counts say how many listings each proposal *would* reject once confirmed,
    and that is the number the confirmation step shows the user.

    A building the user already confirmed is skipped with a warning: writing
    a proposal over it would blank its ``confirmed_at`` and quietly un-reject
    its listings. Re-researching one is a deliberate act — delete the entry,
    or confirm the new answer by hand.

    The mirror is re-synced afterwards so the table matches the file; the
    export shows a proposal, no rule reads one.
    """
    report = BuildingsApplyReport(dry_run=dry_run)

    if not isinstance(result, dict):
        raise BatchError("result must be a JSON object")
    results = result.get("results")
    validate_buildings_results(results)

    if store is None:
        store = buildings.load_buildings(path)
    model = result.get("model")

    for entry in results:
        key = buildings.building_key(entry.get("key") or entry.get("name"))
        previous = store.get(key) or {}
        if previous.get("status") == "confirmed":
            report.skipped.append(key)
            report.warnings.append(
                f"{key}: already confirmed by the user — proposal ignored, "
                "delete the entry to re-research it"
            )
            continue

        name = str(entry.get("name") or previous.get("name") or key).strip()
        stored = {
            "name": name,
            "aliases": entry.get("aliases"),
            "area": entry.get("area"),
            "policy": entry["policy"],
            "status": "proposed",
            "confidence": entry.get("confidence"),
            "note": entry.get("note"),
            "sources": entry.get("sources") or [],
            "researched_by": entry.get("researched_by") or model,
        }
        report.proposed.append({"key": key, **stored})

        if not dry_run:
            store[key] = buildings.save_building(
                path,
                name,
                key=key,
                policy=stored["policy"],
                status="proposed",
                aliases=stored["aliases"],
                area=stored["area"],
                confidence=stored["confidence"],
                note=stored["note"],
                sources=stored["sources"],
                researched_by=stored["researched_by"],
                now=now,
            )
        else:
            # Dry-running still has to count the listings, and the count is
            # read through the store — so mirror the write in memory only.
            store[key] = {**previous, **stored, "aliases": stored["aliases"] or []}

    report.affected = _affected_counts(conn, store, report.proposed)

    if not dry_run:
        storage.sync_buildings_to_db(conn, store)

    return report


def _affected_counts(
    conn: Connection,
    store: dict[str, dict[str, Any]],
    proposed: list[dict[str, Any]],
) -> dict[str, int]:
    """How many still-active listings each proposal covers.

    ``refresh=True`` because the buildings have just been written into the
    store and the queue would otherwise skip every one of them; what comes
    back is the same grouping the batch was built from, so an ad spelling the
    building differently counts through its aliases.
    """
    if not proposed:
        return {}
    counts = {
        row["key"]: row["listings"]
        for row in storage.fetch_pending_buildings(conn, store, refresh=True)
    }
    return {entry["key"]: counts.get(entry["key"], 0) for entry in proposed}
