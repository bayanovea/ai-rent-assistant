"""The contract between the database and the AI.

The "AI" here is a Claude Code session, not an API client, so the interface
is two files rather than a request: :func:`build_batch` writes out everything
the model needs to see (the registry, the requirements letter, the listings),
and :func:`apply_results` reads back what it extracted.

Two things are enforced on the way in. A field key the registry does not
declare is a hard error listing the offending keys — that is what keeps
`data_json` and `config/ai_fields.json` from drifting apart, and it pushes
new signals through `proposed_new_fields` instead. And status, reason and
score are recomputed here from :mod:`.rules`, never taken from the model:
the AI reports facts, the rules decide what they mean.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from . import ai_schema, config, rules, storage
from .extractors import description_from_text_lines


class BatchError(ValueError):
    """Raised when a result file is malformed or contradicts the registry."""


@dataclass
class ApplyReport:
    """What :func:`apply_results` did, in counters plus explanations."""

    inserted: int = 0
    updated: int = 0
    rejected: list[tuple[str, str]] = dataclass_field(default_factory=list)
    skipped: list[str] = dataclass_field(default_factory=list)
    proposed_new_fields: list[dict[str, Any]] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)
    dry_run: bool = False

    @property
    def applied(self) -> int:
        """Number of analyses written (or that would be, when dry-running)."""
        return self.inserted + self.updated


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_requirements(path: Path | str | None) -> str:
    """Return the requirements letter, or an empty string if there is none."""
    target = Path(path) if path is not None else config.REQUIREMENTS_PATH
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _clean_description(listing: dict[str, Any], text_lines: list[str]) -> str | None:
    """Return the listing text the AI should read.

    The stored ``description`` column is unreliable on older rows — the
    scraper used to grab the "Выбор дня" sidebar instead of the ad. When the
    raw text lines still carry the "Описание" marker, that slice wins; the
    column is only a fallback for rows scraped without it.
    """
    from_lines = description_from_text_lines(text_lines)
    if from_lines:
        return from_lines
    stored = (listing.get("description") or "").strip()
    return stored or None


def build_batch(
    conn: Connection,
    registry: ai_schema.Registry,
    requirements_md: str | None = None,
    limit: int | None = None,
    ids: list[str] | None = None,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble the JSON batch of listings awaiting analysis.

    Only pending listings are included (see :func:`.storage.fetch_pending`);
    ``ids`` narrows that queue further, ``limit`` caps it. ``requirements_md``
    defaults to the contents of ``config/requirements.md``.
    """
    pending, _no_raw = storage.fetch_pending(conn, registry)

    if ids is not None:
        wanted = {str(i) for i in ids}
        pending = [row for row in pending if str(row["fb_id"]) in wanted]

    if limit is not None and limit > 0:
        pending = pending[:limit]

    items: list[dict[str, Any]] = []
    for listing in pending:
        text_lines = storage.text_lines_of(listing.get("raw_json"))
        items.append(
            {
                "fb_id": listing["fb_id"],
                "url": listing.get("url"),
                "price_thb": listing.get("price_thb"),
                "price_raw": listing.get("price_raw"),
                "district": listing.get("district"),
                "listing_title": listing.get("title"),
                "description": _clean_description(listing, text_lines),
                "text_lines": text_lines,
                "source_hash": listing["source_hash"],
            }
        )

    return {
        "schema_version": registry.schema_version,
        "generated_at": now or _now_iso(),
        "requirements_md": (
            requirements_md if requirements_md is not None
            else _read_requirements(None)
        ),
        "fields": [dict(f) for f in registry.fields],
        "items": items,
    }


def write_batch(batch: dict[str, Any], out_dir: Path | str | None = None) -> Path:
    """Write ``batch`` to ``<out_dir>/batch-<ts>.json`` and return the path."""
    directory = Path(out_dir) if out_dir is not None else config.AI_DIR
    directory.mkdir(parents=True, exist_ok=True)

    digits = re.sub(r"\D", "", str(batch.get("generated_at") or _now_iso()))[:14]
    path = directory / f"batch-{digits[:8]}-{digits[8:]}.json"
    path.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _validate_envelope(result: Any, registry: ai_schema.Registry) -> list[str]:
    """Check the result's top level; return non-fatal warnings."""
    if not isinstance(result, dict):
        raise BatchError("result must be a JSON object")

    version = result.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise BatchError("result must carry an integer schema_version")
    if version > registry.schema_version:
        raise BatchError(
            f"result schema_version={version} is newer than the registry's "
            f"{registry.schema_version} — update config/ai_fields.json first"
        )

    warnings: list[str] = []
    if version < registry.schema_version:
        warnings.append(
            f"result was produced against schema_version={version}, registry is "
            f"at {registry.schema_version}; these rows stay pending"
        )

    if not isinstance(result.get("results"), list):
        raise BatchError("result must carry a 'results' list")

    return warnings


def _validate_results(
    results: list[Any], registry: ai_schema.Registry
) -> None:
    """Reject the whole file if any entry is unusable, before writing anything."""
    unknown: dict[str, list[str]] = {}
    for index, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise BatchError(f"results[{index}] must be a JSON object")
        if not entry.get("fb_id"):
            raise BatchError(f"results[{index}] is missing fb_id")
        if not str(entry.get("verdict") or "").strip():
            raise BatchError(f"results[{index}] ({entry['fb_id']}) is missing a verdict")

        fields = entry.get("fields", {})
        if not isinstance(fields, dict):
            raise BatchError(f"results[{index}] ({entry['fb_id']}) has non-object fields")
        for key in registry.unknown_keys(fields.keys()):
            unknown.setdefault(key, []).append(str(entry["fb_id"]))

    if unknown:
        listed = ", ".join(sorted(unknown))
        raise BatchError(
            f"unknown field keys not in the registry: {listed}. "
            "Add them via `fb-rent ai-field add` or move them to "
            "proposed_new_fields."
        )


def apply_results(
    conn: Connection,
    registry: ai_schema.Registry,
    result: dict[str, Any],
    *,
    dry_run: bool = False,
    now: str | None = None,
) -> ApplyReport:
    """Validate an AI result file and store its analyses.

    Every field key must exist in the registry — an unknown key aborts the
    whole file with the offending keys listed, so a partial write can never
    leave `data_json` half-migrated. Status and score are recomputed from
    :mod:`.rules` rather than trusted from the model.

    A ``source_hash`` that no longer matches the listing is a warning, not an
    error: the listing may have been re-scraped between batch and apply. The
    reported hash is stored as-is, which leaves such a row pending for the
    next round instead of silently marking stale analysis as fresh.
    """
    report = ApplyReport(dry_run=dry_run)
    report.warnings.extend(_validate_envelope(result, registry))

    results = result["results"]
    _validate_results(results, registry)

    for proposal in result.get("proposed_new_fields") or []:
        if not isinstance(proposal, dict):
            continue
        key = proposal.get("key")
        if key and registry.field(key) is not None:
            report.warnings.append(
                f"proposed field {key!r} already exists in the registry — ignored"
            )
            continue
        report.proposed_new_fields.append(proposal)

    model = result.get("model")
    timestamp = now or _now_iso()

    for entry in results:
        fb_id = str(entry["fb_id"])
        fields = entry.get("fields") or {}

        row = conn.execute(
            "SELECT * FROM listings WHERE fb_id = ?", (fb_id,)
        ).fetchone()
        if row is None:
            report.skipped.append(fb_id)
            report.warnings.append(f"{fb_id}: no such listing in the database")
            continue

        listing = dict(row)
        current_hash = ai_schema.source_hash(
            {**listing, "text_lines": storage.text_lines_of(listing.get("raw_json"))}
        )
        reported_hash = entry.get("source_hash")
        if reported_hash and reported_hash != current_hash:
            report.warnings.append(
                f"{fb_id}: source_hash changed since the batch was built — "
                "accepted, but the listing stays pending"
            )

        status, reason = rules.decide(fields, listing.get("price_thb"))
        if status == "rejected" and reason:
            report.rejected.append((fb_id, reason))

        if dry_run:
            report.inserted += 1
            continue

        outcome = storage.upsert_ai_analysis(
            conn,
            {
                "fb_id": fb_id,
                "schema_version": registry.schema_version,
                "analyzed_at": entry.get("analyzed_at") or timestamp,
                "source_hash": reported_hash or current_hash,
                "model": model,
                "verdict": entry["verdict"],
                "score": rules.score(fields),
                "data_quality": entry.get("data_quality"),
                "data_json": fields,
            },
        )
        if outcome == "inserted":
            report.inserted += 1
        else:
            report.updated += 1

    return report
