"""AI field registry: load, validate, and detect staleness.

The registry (`config/ai_fields.json`) is the single source of truth for
which facts the AI extracts from a listing. Adding a field is a JSON edit,
not a migration — values live in `ai_analysis.data_json`, keyed by field
`key`.

`added_in` records the `schema_version` a field appeared in, which is what
lets the HTML export tell "this field did not exist when the row was
analysed" apart from "the AI looked and found nothing".

`source_hash` is the staleness detector: it hashes exactly what the AI saw,
so a re-scrape that improves a listing's text puts it back in the queue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

# Field types the registry may declare. `enum` additionally requires a
# non-empty `values` list; everything else is free-form.
KNOWN_TYPES: frozenset[str] = frozenset({"enum", "int", "text", "date"})

# Keys hashed into `source_hash`, in a fixed order so the digest does not
# depend on dict ordering.
_HASH_KEYS: tuple[str, ...] = ("title", "price_raw", "district", "description")


class RegistryError(ValueError):
    """Raised when `config/ai_fields.json` is malformed or inconsistent."""


@dataclass(frozen=True)
class Registry:
    """A validated AI field registry."""

    schema_version: int
    fields: tuple[dict[str, Any], ...]

    def field_keys(self) -> list[str]:
        """Return every field key, in registry order."""
        return [f["key"] for f in self.fields]

    def field(self, key: str) -> dict[str, Any] | None:
        """Return the field definition for ``key``, or ``None``."""
        for f in self.fields:
            if f["key"] == key:
                return f
        return None

    def unknown_keys(self, keys: Any) -> list[str]:
        """Return the subset of ``keys`` that the registry does not define."""
        known = set(self.field_keys())
        return [k for k in keys if k not in known]


def load_registry(path: Path | str | None = None) -> Registry:
    """Load and validate the AI field registry.

    Defaults to ``config.AI_FIELDS_PATH``. Raises :class:`RegistryError` on
    duplicate keys, unknown types, enums without values, or fields claiming
    an ``added_in`` newer than the registry's own ``schema_version``.
    """
    path = Path(path) if path is not None else config.AI_FIELDS_PATH
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"field registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"field registry is not valid JSON: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError("field registry must be a JSON object")

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise RegistryError("schema_version must be an integer")
    if schema_version < 1:
        raise RegistryError("schema_version must be >= 1")

    fields = raw.get("fields")
    if not isinstance(fields, list) or not fields:
        raise RegistryError("fields must be a non-empty list")

    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            raise RegistryError("every field must be a JSON object")

        key = field.get("key")
        if not isinstance(key, str) or not key:
            raise RegistryError("every field needs a non-empty string key")
        if key in seen:
            raise RegistryError(f"duplicate field key: {key}")
        seen.add(key)

        if not field.get("label"):
            raise RegistryError(f"field {key!r} needs a label")

        ftype = field.get("type")
        if ftype not in KNOWN_TYPES:
            raise RegistryError(
                f"field {key!r} has unknown type {ftype!r} "
                f"(known: {', '.join(sorted(KNOWN_TYPES))})"
            )

        if ftype == "enum":
            values = field.get("values")
            if not isinstance(values, list) or not values:
                raise RegistryError(f"enum field {key!r} needs a non-empty values list")

        added_in = field.get("added_in")
        if not isinstance(added_in, int) or isinstance(added_in, bool):
            raise RegistryError(f"field {key!r} needs an integer added_in")
        if added_in > schema_version:
            raise RegistryError(
                f"field {key!r} has added_in={added_in} > schema_version={schema_version}"
            )

    return Registry(schema_version=schema_version, fields=tuple(fields))


def _text_line_count(listing: dict[str, Any]) -> int:
    """Return the number of raw text lines the AI would see for a listing."""
    lines = listing.get("text_lines")
    if lines is None:
        raw_json = listing.get("raw_json")
        if isinstance(raw_json, str) and raw_json:
            try:
                lines = json.loads(raw_json).get("text_lines")
            except (json.JSONDecodeError, AttributeError):
                lines = None
        elif isinstance(raw_json, dict):
            lines = raw_json.get("text_lines")
    return len(lines) if isinstance(lines, list) else 0


def source_hash(listing: dict[str, Any]) -> str:
    """Hash the listing content the AI actually reads.

    Digest of ``title | price_raw | district | description | len(text_lines)``.
    Independent of key order and of any other column, so unrelated churn
    (``last_seen_at``, for instance) does not force a re-analysis.
    """
    parts = [str(listing.get(k) or "") for k in _HASH_KEYS]
    parts.append(str(_text_line_count(listing)))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
