"""Pets policy per building, stored outside the database.

A condo's house rule about animals is a property of the *building*, not of any
one listing: the same five units in the same tower share one answer, and the
answer outlives every ad that mentions it. So the store is keyed by building
and, like :mod:`.decisions`, it lives in a plain JSON file
(``data/buildings.json``) that no ``init_db`` can wipe — a web search the user
then vetted is exactly as unreproducible as a user verdict. The ``buildings``
table is only a mirror kept in sync so :func:`.fetch_enriched` stays one query.

The file format is a flat map keyed by the normalised building key::

    {"the-base-central-phuket": {
        "name": "The Base Central Phuket",
        "aliases": ["base central"],
        "area": "Phuket Town",
        "policy": "not_allowed",
        "status": "confirmed",
        "confidence": "high",
        "note": "Управляющая компания: no pets of any kind.",
        "sources": [{"url": "...", "title": "...", "quote": "..."}],
        "researched_at": "2026-08-27T10:00:00Z",
        "researched_by": "claude-opus-5 (claude-code)",
        "confirmed_at": "2026-08-27T10:12:00Z"}}

Two rules shape the whole module:

**Research proposes, the user confirms.** A researched entry lands as
``proposed`` and is inert — :func:`policy_for` hands out nothing for it, so no
web search on its own can reject a single row. Only ``confirmed`` reaches
:func:`.rules.decide`.

**:func:`building_key` is deliberately dumb.** It will not merge "The Base" and
"Base Central" on its own; a second spelling is pointed at an existing building
by hand through ``aliases``. Guessing at fuzzy matches would silently apply one
building's rejection to another building's listings.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# The building's own verdict on animals, independent of what any ad claims.
# `negotiable` is the reason this cannot be the three-value `pets` enum the AI
# fills in: the case the user explicitly wants kept has nowhere to live there.
# `unknown` is what honest research with no source looks like.
POLICIES: frozenset[str] = frozenset(
    {"not_allowed", "negotiable", "allowed", "unknown"}
)

# `proposed` is what research writes and it is inert; `confirmed` is the user's
# word and the only status that can reject anything.
STATUSES: frozenset[str] = frozenset({"proposed", "confirmed"})

# Advisory only — shown to the user at confirmation time, never read by a rule.
CONFIDENCES: frozenset[str] = frozenset({"high", "med", "low"})

_KEY_SPLIT_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


class BuildingError(ValueError):
    """An invalid building entry: unknown policy, status or empty key."""


def building_key(name: str | None) -> str:
    """Return the normalised key for a building name.

    Lowercases, drops everything that is not alphanumeric, collapses the runs
    of separators and joins what is left with hyphens. Empty or ``None`` input
    yields ``""`` — the caller decides whether that is an error.
    """
    if not name:
        return ""
    parts = [p for p in _KEY_SPLIT_RE.split(str(name).casefold()) if p]
    return "-".join(parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else config.BUILDINGS_PATH


def load_buildings(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return every known building keyed by its normalised key.

    A missing file is an empty dict — the normal state before the first
    research run. Defaults to ``config.BUILDINGS_PATH``.
    """
    target = _path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"buildings file is not valid JSON: {target}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"buildings file must be a JSON object: {target}")
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _clean_aliases(aliases: Any) -> list[str]:
    if not aliases:
        return []
    if isinstance(aliases, str):
        aliases = [aliases]
    out: list[str] = []
    for alias in aliases:
        text = str(alias).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_sources(sources: Any) -> list[dict[str, Any]]:
    if not sources:
        return []
    out: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise BuildingError(f"source must be an object, got {source!r}")
        out.append(
            {
                "url": str(source.get("url") or "").strip(),
                "title": str(source.get("title") or "").strip(),
                "quote": str(source.get("quote") or "").strip(),
            }
        )
    return out


def save_building(
    path: Path | str | None,
    name: str,
    *,
    policy: str,
    status: str = "proposed",
    key: str | None = None,
    aliases: list[str] | None = None,
    area: str | None = None,
    confidence: str | None = None,
    note: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    researched_by: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Record one building, merging onto any entry already stored for its key.

    Fields left as ``None`` keep whatever the stored entry had, so confirming a
    proposal does not have to repeat its sources. ``aliases`` are added, never
    dropped: an alias is a hand-made statement that two spellings are the same
    building and re-researching must not undo it.

    Raises :class:`BuildingError` for an unknown ``policy`` or ``status``, or
    for a name that normalises to an empty key.
    """
    if policy not in POLICIES:
        raise BuildingError(
            f"unknown pets policy {policy!r} (known: {', '.join(sorted(POLICIES))})"
        )
    if status not in STATUSES:
        raise BuildingError(
            f"unknown building status {status!r} "
            f"(known: {', '.join(sorted(STATUSES))})"
        )
    if confidence is not None and confidence not in CONFIDENCES:
        raise BuildingError(
            f"unknown confidence {confidence!r} "
            f"(known: {', '.join(sorted(CONFIDENCES))})"
        )

    entry_key = key or building_key(name)
    if not entry_key:
        raise BuildingError(f"building name {name!r} normalises to an empty key")

    target = _path(path)
    store = load_buildings(target)
    previous = store.get(entry_key) or {}
    stamp = now or _now_iso()

    merged_aliases = _clean_aliases(previous.get("aliases"))
    for alias in _clean_aliases(aliases):
        if alias not in merged_aliases:
            merged_aliases.append(alias)

    entry: dict[str, Any] = {
        "name": name or previous.get("name") or entry_key,
        "aliases": merged_aliases,
        "area": area if area is not None else previous.get("area"),
        "policy": policy,
        "status": status,
        "confidence": (
            confidence if confidence is not None else previous.get("confidence")
        ),
        "note": note if note is not None else previous.get("note"),
        "sources": (
            _clean_sources(sources)
            if sources is not None
            else _clean_sources(previous.get("sources"))
        ),
        "researched_at": previous.get("researched_at") or stamp,
        "researched_by": (
            researched_by if researched_by is not None else previous.get("researched_by")
        ),
        # A confirmation is dated; a proposal has not been confirmed yet, and
        # re-proposing an entry retracts the old confirmation on purpose.
        "confirmed_at": stamp if status == "confirmed" else None,
    }
    if sources is not None:
        entry["researched_at"] = stamp

    store[entry_key] = entry
    _write_atomic(target, store)
    return entry


def delete_building(path: Path | str | None, key: str) -> bool:
    """Remove one building from the store. Returns whether it was there."""
    target = _path(path)
    store = load_buildings(target)
    if key not in store:
        return False
    del store[key]
    _write_atomic(target, store)
    return True


def _write_atomic(target: Path, store: dict[str, dict[str, Any]]) -> None:
    """Write the buildings map, replacing ``target`` only once fully written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".buildings-")
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


def resolve(
    store: dict[str, dict[str, Any]], project_name: str | None
) -> dict[str, Any] | None:
    """Return the stored entry for ``project_name``, or ``None``.

    Tries the normalised key first, then every entry's ``aliases`` — also
    normalised, so an alias may be written the way the ad spells it. No fuzzy
    matching: an unlisted spelling is a miss, and the fix is to add the alias.
    """
    key = building_key(project_name)
    if not key:
        return None
    entry = store.get(key)
    if isinstance(entry, dict):
        return entry
    for candidate in store.values():
        if not isinstance(candidate, dict):
            continue
        for alias in _clean_aliases(candidate.get("aliases")):
            if building_key(alias) == key:
                return candidate
    return None


def policy_for(
    store: dict[str, dict[str, Any]], project_name: str | None
) -> str | None:
    """Return the policy that may act on a listing in ``project_name``.

    ``None`` unless the building is in the store, ``confirmed``, and carries a
    policy that says something: a ``proposed`` entry is inert by design, and a
    confirmed ``unknown`` is research that found nothing. That is the same
    ``None`` :func:`.rules.decide` expects for an unresearched building — the
    caller cannot tell the three apart, and no rule needs to.
    """
    entry = resolve(store, project_name)
    if not entry or entry.get("status") != "confirmed":
        return None
    policy = entry.get("policy")
    if policy not in POLICIES or policy == "unknown":
        return None
    return str(policy)
