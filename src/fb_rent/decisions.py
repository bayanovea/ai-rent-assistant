"""User decisions, stored outside the database.

Everything else in this project is reproducible: listings can be re-scraped
and the AI analysis re-run at any time. A user verdict — "rejected, too far
from the school", "shortlist" — is not. So it lives in a plain JSON file
(``data/decisions.json``) that no ``init_db`` can wipe, and the ``decisions``
table is only a mirror kept in sync for JOINing in :func:`.fetch_enriched`.

The file format is a flat map keyed by ``fb_id``::

    {"9000000000000001": {"status": "rejected",
                           "reason": "Нет зелёной зоны",
                           "rating": 2.5,
                           "decided_at": "2026-08-25T10:00:00Z"}}

``rating`` is the user's own 1-5 mark for a listing and it is what decides
``status`` — see :func:`status_for_rating`. It is optional: ``reject`` and
``shortlist`` set a status without one.

The four statuses are the four drawers the table shows as tabs: ``active`` is
what still has to be looked at, ``longlist`` the middle variants worth keeping
to hand, ``shortlist`` the best, ``rejected`` what is out.

An entry may also carry a ``stage`` — what happened after the listing was
shortlisted: contacted the landlord, agreed on a viewing, or viewed and not
yet compared against the others. It is independent of ``status`` on purpose
(see :func:`save_stage`) and adds four more keys when present: ``stage``,
``stage_fb_id`` (which of the postings merged into this row the stage is
actually about — dedupe can fold several ads into one row, and a comma
joins more than one when you wrote to some of them and not the rest),
``stage_at`` (the viewing's date/time, only for ``viewing_scheduled``) and
``stage_note``.

Writes go through a temp file plus ``os.replace``, so an interrupted run
leaves the previous decisions intact rather than a truncated file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# The only verdicts a user may record. `active` means "not sorted out yet",
# which is also what an explicit `unreject` writes: it puts an AI auto-reject
# back into the queue of listings still to be looked at. The other three all
# say the listing has been seen — `longlist` is the middle drawer, kept to
# hand, `shortlist` the best of them.
VALID_STATUSES: frozenset[str] = frozenset(
    {"rejected", "active", "longlist", "shortlist"}
)

# The post-shortlist pipeline: contacted the landlord/agent, agreed on a
# viewing, or viewed and waiting to be compared against the others before a
# final call. Deliberately not folded into ``status`` — see :func:`save_stage`.
VALID_STAGES: frozenset[str] = frozenset(
    {"written", "viewing_scheduled", "viewed"}
)

STAGE_LABELS: dict[str, str] = {
    "written": "Написал",
    "viewing_scheduled": "Договорились о просмотре",
    "viewed": "Просмотрено, ждёт сравнения",
}

# The user's own 1-5 mark, and the two thresholds that turn it into a status.
# Deliberately blunt: a listing worth 4.5 goes straight to the shortlist, one
# worth 3 or less is out, and what falls between lands on the longlist.
#
# No mark ever leaves a row in `active`. The active tab is the queue of what
# has not been looked at, so a rated row sitting there would be indis-
# tinguishable from an untouched one at a glance — which is exactly the
# distinction the four statuses exist to draw.
RATING_MIN = 1.0
RATING_MAX = 5.0
RATING_SHORTLIST_MIN = 4.5
RATING_REJECT_MAX = 3.0


def status_for_rating(rating: float) -> str:
    """Return the status a 1-5 rating implies."""
    if rating >= RATING_SHORTLIST_MIN:
        return "shortlist"
    if rating <= RATING_REJECT_MAX:
        return "rejected"
    return "longlist"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else config.DECISIONS_PATH


def load_decisions(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return every recorded decision keyed by ``fb_id``.

    A missing file is an empty dict — the normal state before the first
    decision. Defaults to ``config.DECISIONS_PATH``.
    """
    target = _path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"decisions file is not valid JSON: {target}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"decisions file must be a JSON object: {target}")
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_decision(
    path: Path | str | None,
    fb_id: str,
    status: str,
    reason: str | None = None,
    *,
    rating: float | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Record one decision, overwriting any previous verdict for ``fb_id``.

    Leaves a ``stage`` already recorded by :func:`save_stage` alone — status
    and stage track different things (see the module docstring), so
    rejecting a listing you already wrote to must not erase that history.

    Returns the stored entry. Raises :class:`ValueError` for an unknown
    ``status`` or a ``rating`` outside 1-5.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown decision status {status!r} "
            f"(known: {', '.join(sorted(VALID_STATUSES))})"
        )
    if rating is not None and not RATING_MIN <= rating <= RATING_MAX:
        raise ValueError(
            f"rating {rating} is outside {RATING_MIN:g}-{RATING_MAX:g}"
        )

    target = _path(path)
    decisions = load_decisions(target)
    previous = decisions.get(str(fb_id)) or {}
    entry = {
        key: previous[key]
        for key in ("stage", "stage_fb_id", "stage_at", "stage_note", "stage_updated_at")
        if key in previous
    }
    entry.update(
        {
            "status": status,
            "reason": reason,
            "rating": rating,
            "decided_at": now or _now_iso(),
        }
    )
    decisions[str(fb_id)] = entry
    _write_atomic(target, decisions)
    return entry


def save_stage(
    path: Path | str | None,
    fb_id: str,
    stage: str,
    *,
    posting_fb_id: str | None = None,
    viewing_at: str | None = None,
    note: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Record the post-shortlist stage for a listing, alongside its status.

    A listing worth writing to has usually been shortlisted already, but the
    stage does not require it: an entry with no prior decision is created
    with ``status="active"`` rather than refused, since rejecting or
    shortlisting later must not lose the stage already recorded. The reverse
    also holds — :func:`save_decision` never touches these keys, so rejecting
    a listing you already wrote to keeps that history rather than erasing it.

    ``posting_fb_id`` is the specific ad(s), among however many dedupe has
    merged into this row, that the stage is actually about — comma-separated
    if more than one. This module has no DB access, so it cannot check those
    ids against the row's known postings — the caller (the CLI) must have
    already resolved and validated them before this is called.
    """
    if stage not in VALID_STAGES:
        raise ValueError(
            f"unknown stage {stage!r} (known: {', '.join(sorted(VALID_STAGES))})"
        )

    target = _path(path)
    decisions = load_decisions(target)
    entry = dict(
        decisions.get(str(fb_id))
        or {
            "status": "active",
            "reason": None,
            "rating": None,
            "decided_at": now or _now_iso(),
        }
    )
    entry["stage"] = stage
    entry["stage_fb_id"] = posting_fb_id
    entry["stage_at"] = viewing_at
    entry["stage_note"] = note
    entry["stage_updated_at"] = now or _now_iso()
    decisions[str(fb_id)] = entry
    _write_atomic(target, decisions)
    return entry


def clear_stage(
    path: Path | str | None, fb_id: str, *, now: str | None = None
) -> dict[str, Any] | None:
    """Drop the stage from a listing's entry, leaving its status untouched.

    Returns the entry (unchanged if there was no stage to clear), or ``None``
    if the listing has no decision at all.
    """
    target = _path(path)
    decisions = load_decisions(target)
    entry = decisions.get(str(fb_id))
    if entry is None or "stage" not in entry:
        return entry
    entry = dict(entry)
    for key in ("stage", "stage_fb_id", "stage_at", "stage_note", "stage_updated_at"):
        entry.pop(key, None)
    decisions[str(fb_id)] = entry
    _write_atomic(target, decisions)
    return entry


def _write_atomic(target: Path, decisions: dict[str, dict[str, Any]]) -> None:
    """Write the decisions map, replacing ``target`` only once fully written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(decisions, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".decisions-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        # mkstemp creates 0600; the file is meant to be readable and
        # hand-editable, so do not let os.replace narrow it.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def sync_to_db(
    conn: sqlite3.Connection,
    decisions: dict[str, dict[str, Any]] | None = None,
    *,
    path: Path | str | None = None,
) -> int:
    """Mirror the decisions file into the ``decisions`` table.

    The file wins: rows it no longer contains are deleted, so an undo in the
    file propagates. Pass ``decisions`` to sync an already-loaded map,
    otherwise it is read from ``path`` (or ``config.DECISIONS_PATH``).
    Returns the number of rows written.
    """
    if decisions is None:
        decisions = load_decisions(path)

    rows = []
    for fb_id, entry in decisions.items():
        status = entry.get("status")
        if status not in VALID_STATUSES:
            raise ValueError(f"listing {fb_id}: unknown decision status {status!r}")
        stage = entry.get("stage")
        if stage is not None and stage not in VALID_STAGES:
            raise ValueError(f"listing {fb_id}: unknown stage {stage!r}")
        rows.append(
            (
                str(fb_id),
                status,
                entry.get("reason"),
                entry.get("rating"),
                entry.get("decided_at") or _now_iso(),
                stage,
                entry.get("stage_fb_id"),
                entry.get("stage_at"),
                entry.get("stage_note"),
            )
        )

    conn.execute("DELETE FROM decisions")
    conn.executemany(
        "INSERT INTO decisions "
        "(fb_id, status, reason, rating, decided_at, "
        " stage, stage_fb_id, stage_at, stage_note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
