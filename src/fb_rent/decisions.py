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

# The only verdicts a user may record. `active` exists so an AI auto-reject
# can be overridden back into the working set.
VALID_STATUSES: frozenset[str] = frozenset({"rejected", "active", "shortlist"})

# The user's own 1-5 mark, and the two thresholds that turn it into a status.
# Deliberately blunt: a listing worth 4.5 goes straight to the shortlist, one
# worth 3 or less is out, and what falls between stays on the main sheet —
# with a mark on it, which is what says "looked at, no need to come back".
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
    return "active"


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
    entry = {
        "status": status,
        "reason": reason,
        "rating": rating,
        "decided_at": now or _now_iso(),
    }
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
        rows.append(
            (
                str(fb_id),
                status,
                entry.get("reason"),
                entry.get("rating"),
                entry.get("decided_at") or _now_iso(),
            )
        )

    conn.execute("DELETE FROM decisions")
    conn.executemany(
        "INSERT INTO decisions (fb_id, status, reason, rating, decided_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
