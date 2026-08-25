"""The contract between the photo store and the AI's second pass.

Mirrors :mod:`.ai_batch`, with two deliberate differences. The batch carries
only the fields the registry marks ``photo`` or ``both`` — the pass is not
asked to re-read the description — and every item ships the text pass's own
answers for those fields under ``current_fields``, because the point of
looking at the pictures is to catch where the text oversold the flat.

The other difference is in validation: a photo result carries no ``verdict``.
Whether a listing is worth renting stays a text-pass judgement; the camera
only reports what is in frame.
"""

from __future__ import annotations

import json
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from . import ai_batch, ai_schema, config, photos, rules, storage

# Re-exported so callers can catch one error type across both passes.
BatchError = ai_batch.BatchError


def _text_fields(conn: Connection, fb_id: str) -> dict[str, Any]:
    """Return the text pass's ``data_json`` for a listing, or an empty dict."""
    row = conn.execute(
        "SELECT data_json FROM ai_analysis WHERE fb_id = ?", (str(fb_id),)
    ).fetchone()
    if row is None:
        return {}
    return storage.fields_of(row["data_json"])


def _current_fields(
    text_fields: dict[str, Any], photo_keys: list[str]
) -> dict[str, Any]:
    """Return what the text pass claimed, restricted to the photo fields.

    Plain values, not the ``{"v", "conf", "ev"}`` wrapper: the model is being
    shown a claim to check, not a record to edit.
    """
    current: dict[str, Any] = {}
    for key in photo_keys:
        value = rules.value_of(text_fields, key)
        if not rules.is_empty(value):
            current[key] = value
    return current


def _photo_paths(fb_id: str) -> list[str]:
    """Return the listing's photo files on disk, in name order."""
    directory = photos.photos_dir(str(fb_id))
    if not directory.is_dir():
        return []
    return [str(p) for p in sorted(directory.iterdir()) if p.is_file()]


def build_photo_batch(
    conn: Connection,
    registry: ai_schema.Registry,
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    listings: list[dict[str, Any]] | None = None,
    requirements_md: str | None = None,
    photo_analysis_version: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble the JSON batch of listings awaiting a photo pass.

    ``listings`` are rows already selected by the caller — that is how the CLI
    honours an explicit ``--ids`` for a listing the queue would otherwise skip.
    Without it the photo queue is read here (see
    :func:`.storage.fetch_pending_photos`), ``ids`` narrows it and ``limit``
    caps it.

    A listing with no photos on disk is dropped: there is nothing to look at,
    and ``fetch_pending_photos`` already reports those separately.
    """
    wanted_version = (
        registry.schema_version
        if photo_analysis_version is None
        else photo_analysis_version
    )

    if listings is not None:
        pending = list(listings)
    else:
        pending, _no_photos = storage.fetch_pending_photos(
            conn, registry, photo_analysis_version=wanted_version
        )
        if ids is not None:
            wanted = {str(i) for i in ids}
            pending = [row for row in pending if str(row["fb_id"]) in wanted]

    if limit is not None:
        pending = pending[: max(limit, 0)]

    photo_fields = [dict(f) for f in registry.fields_for("photo")]
    photo_keys = [f["key"] for f in photo_fields]

    items: list[dict[str, Any]] = []
    for listing in pending:
        fb_id = str(listing["fb_id"])
        paths = _photo_paths(fb_id)
        if not paths:
            continue
        items.append(
            {
                "fb_id": fb_id,
                "url": listing.get("url"),
                "listing_title": listing.get("title"),
                "photos_hash": listing.get("photos_hash") or photos.photos_hash(fb_id),
                "photos": paths,
                "current_fields": _current_fields(
                    _text_fields(conn, fb_id), photo_keys
                ),
            }
        )

    return {
        "schema_version": registry.schema_version,
        "photo_analysis_version": wanted_version,
        "generated_at": now or ai_batch._now_iso(),
        "requirements_md": (
            requirements_md
            if requirements_md is not None
            else ai_batch._read_requirements(None)
        ),
        "fields": photo_fields,
        "items": items,
    }


def write_photo_batch(
    batch: dict[str, Any], out_dir: Path | str | None = None
) -> Path:
    """Write ``batch`` to ``<out_dir>/photo-batch-<ts>.json`` and return the path."""
    directory = Path(out_dir) if out_dir is not None else config.AI_DIR
    directory.mkdir(parents=True, exist_ok=True)

    path = ai_batch.timestamped_path(
        directory, "photo-batch", batch.get("generated_at")
    )
    path.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def validate_photo_results(
    results: list[Any], registry: ai_schema.Registry
) -> None:
    """Reject the whole file if any entry is unusable, before writing anything.

    ``photos_hash`` is mandatory here where ``source_hash`` is optional in the
    text pass: without it there is no way to tell whether the analysis
    describes the pictures currently on disk. ``verdict`` is *not* required —
    it stays the text pass's business.
    """
    unknown: dict[str, list[str]] = {}
    bad_values: list[str] = []
    for index, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise BatchError(f"results[{index}] must be a JSON object")
        if not entry.get("fb_id"):
            raise BatchError(f"results[{index}] is missing fb_id")
        if not str(entry.get("photos_hash") or "").strip():
            raise BatchError(
                f"results[{index}] ({entry['fb_id']}) is missing photos_hash"
            )

        fields = entry.get("fields", {})
        if not isinstance(fields, dict):
            raise BatchError(
                f"results[{index}] ({entry['fb_id']}) has non-object fields"
            )
        for key in registry.unknown_keys(fields.keys()):
            unknown.setdefault(key, []).append(str(entry["fb_id"]))
        bad_values.extend(ai_batch._bad_enum_values(entry, fields, registry))

    if unknown:
        listed = ", ".join(sorted(unknown))
        raise BatchError(
            f"unknown field keys not in the registry: {listed}. "
            "Add them via `fb-rent ai-field add --source photo` or move them "
            "to proposed_new_fields."
        )

    if bad_values:
        raise BatchError(
            "values outside the registry's vocabulary: "
            + "; ".join(bad_values)
            + ". Use one of the declared values, or widen the field with "
            "`fb-rent ai-field add`."
        )
