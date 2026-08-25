"""SQLite storage layer for fb-rent listings.

The database is the source of truth; ``rentals.csv`` and ``rentals.html``
are flat exports regenerated from it. Deduplication is done on ``fb_id``
(the listing ID extracted from the marketplace URL).

Three tables live here. ``listings`` is what the scraper writes;
``ai_analysis`` is what the AI extracted from each listing, keyed by the
same ``fb_id`` and carrying the ``source_hash`` that says whether the
analysis still matches the text it was made from; ``decisions`` mirrors
``data/decisions.json`` so a user verdict can be JOINed in — the file stays
the source of truth, because only user decisions are irreplaceable.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from . import ai_schema, extractors, photos, rules


CSV_COLUMNS: tuple[str, ...] = (
    "price_thb",
    "title",
    "bedrooms",
    "district",
    "property_type",
    "published_at",
    "url",
    "description",
    "first_seen_at",
    "last_seen_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    fb_id           TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    price_thb       INTEGER,
    price_raw       TEXT,
    district        TEXT,
    content_key     TEXT,
    bedrooms        INTEGER,
    property_type   TEXT,
    description     TEXT,
    published_at    TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw_json        TEXT
);
"""

_AI_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_analysis (
    fb_id          TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    analyzed_at    TEXT NOT NULL,
    source_hash    TEXT NOT NULL,
    model          TEXT,
    verdict        TEXT NOT NULL,
    comment        TEXT,
    score          INTEGER,
    data_quality   TEXT,
    data_json      TEXT NOT NULL
);
"""

_DUPLICATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS duplicates (
    fb_id            TEXT PRIMARY KEY,
    canonical_fb_id  TEXT NOT NULL,
    content_key      TEXT NOT NULL,
    url              TEXT,
    noticed_at       TEXT NOT NULL
);
"""

_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    fb_id      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    reason     TEXT,
    decided_at TEXT NOT NULL
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_last_seen ON listings(last_seen_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_price ON listings(price_thb);",
    "CREATE INDEX IF NOT EXISTS idx_content_key ON listings(content_key);",
    "CREATE INDEX IF NOT EXISTS idx_dup_canonical ON duplicates(canonical_fb_id);",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# An ad shorter than this is not a fingerprint: "Выбор дня" or a bare price
# line would collapse unrelated listings into one.
_CONTENT_KEY_MIN_CHARS = 60


def marketplace_url(fb_id: str) -> str:
    """The canonical listing URL for an id, in the shape the scraper stores."""
    return f"https://www.facebook.com/marketplace/item/{fb_id}/"


def _ad_text(listing: dict[str, Any]) -> str:
    """The ad body: the `text_lines` slice, falling back to the column."""
    return extractors.description_from_text_lines(
        text_lines_of(listing.get("raw_json"))
    ) or (listing.get("description") or "")


# A listing is pinned to a unit by its floor and building letter. These are
# only ever used to *reject* a match: two ads that name different floors of
# the same project are two different flats, however alike they read.
_FLOOR_PATTERNS = (
    re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*floor", re.I),
    re.compile(r"floor\s*:?\s*(\d{1,2})", re.I),
    re.compile(r"ชั้น\s*(\d{1,2})"),
)
_BUILDING_PATTERNS = (
    re.compile(r"building\s+([a-z])\b", re.I),
    re.compile(r"(?:ตึก|อาคาร)\s*([a-zA-Z])"),
    re.compile(r"корпус\s+([a-zA-Z])", re.I),
)

_WORD_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ\u0e00-\u0e7f]+")

# Agencies write from the same template, so two ads for different flats in
# one project routinely read 80% alike. Only near-identical text counts.
_TEXT_MATCH_RATIO = 0.9
# Project names arrive misspelled ("Panason" / "Phanason") and half
# transliterated, so they are compared loosely.
_PROJECT_MATCH_RATIO = 0.85
# Two agents measuring the same flat disagree by a metre or two.
_SIZE_TOLERANCE_SQM = 3


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(1).upper()
    return None


def _normalise_project(name: Any) -> str | None:
    if not name:
        return None
    stripped = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    stripped = re.sub(
        r"\b(korpus|building|condo|condominium|phuket|the)\b", " ", stripped
    )
    return " ".join(stripped.split()) or None


def content_key(listing: dict[str, Any]) -> str | None:
    """Fingerprint a listing by the ad text itself, not by its FB id.

    Agencies repost the same ad to lift it back up the feed, and Facebook
    issues a fresh ``fb_id`` every time, so id-keyed dedup lets the same flat
    in six times. The fingerprint is the ad body — read out of ``text_lines``,
    the same slice the AI reads, because the ``description`` column sometimes
    holds the sidebar instead — plus the price, so a repost at a new price
    stays a separate listing. Returns ``None`` when the text is too thin to
    identify anything, and such a listing is never treated as a duplicate.
    """
    normalised = " ".join(_ad_text(listing).split()).lower()
    if len(normalised) < _CONTENT_KEY_MIN_CHARS:
        return None
    price = listing.get("price_thb")
    digest = hashlib.sha256(f"{price}\n{normalised}".encode("utf-8"))
    return digest.hexdigest()


def canonical_for(
    conn: sqlite3.Connection, fb_id: str, key: str | None
) -> str | None:
    """The fb_id already holding this ad text, if another row has it."""
    if not key:
        return None
    row = conn.execute(
        "SELECT fb_id FROM listings WHERE content_key = ? AND fb_id != ? "
        "ORDER BY first_seen_at LIMIT 1",
        (key, fb_id),
    ).fetchone()
    return row["fb_id"] if row else None


def duplicate_fb_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids already known to be reposts, so a rescrape can skip them early."""
    return {
        row["fb_id"] for row in conn.execute("SELECT fb_id FROM duplicates")
    }


def _record_duplicate(
    conn: sqlite3.Connection,
    fb_id: str,
    canonical_fb_id: str,
    key: str,
    ts: str,
    url: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO duplicates "
        "(fb_id, canonical_fb_id, content_key, url, noticed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fb_id, canonical_fb_id, key, url or marketplace_url(fb_id), ts),
    )


def duplicates_by_canonical(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, str]]]:
    """Reposts grouped under the listing they duplicate, oldest first."""
    grouped: dict[str, list[dict[str, str]]] = {}
    rows = conn.execute(
        "SELECT fb_id, canonical_fb_id, url FROM duplicates ORDER BY noticed_at, fb_id"
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["canonical_fb_id"], []).append(
            {"fb_id": row["fb_id"], "url": row["url"] or marketplace_url(row["fb_id"])}
        )
    return grouped


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that arrived after the first databases were created.

    ``verdict`` used to hold the whole write-up; it now holds the one-line
    recommendation and ``comment`` holds the reasoning behind it.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(ai_analysis)")}
    if "comment" not in have:
        conn.execute("ALTER TABLE ai_analysis ADD COLUMN comment TEXT")

    # The photo pass keeps its own columns next to the text pass: same shape
    # (fields, hash, version, timestamp, model), separate lifecycle, so a
    # re-analysed description never invalidates the picture reading.
    have = {row["name"] for row in conn.execute("PRAGMA table_info(ai_analysis)")}
    for column in (
        "photo_data_json",
        "photos_hash",
        "photo_schema_version",
        "photos_analyzed_at",
        "photo_model",
        "photo_comment",
    ):
        if column not in have:
            kind = "INTEGER" if column == "photo_schema_version" else "TEXT"
            conn.execute(f"ALTER TABLE ai_analysis ADD COLUMN {column} {kind}")

    have = {row["name"] for row in conn.execute("PRAGMA table_info(duplicates)")}
    if "url" not in have:
        conn.execute("ALTER TABLE duplicates ADD COLUMN url TEXT")
        # Reposts recorded before this column existed: their row is gone from
        # `listings`, but a marketplace id is enough to rebuild the link.
        for row in conn.execute("SELECT fb_id FROM duplicates").fetchall():
            conn.execute(
                "UPDATE duplicates SET url = ? WHERE fb_id = ?",
                (marketplace_url(row["fb_id"]), row["fb_id"]),
            )
        conn.commit()

    have = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    if "content_key" not in have:
        conn.execute("ALTER TABLE listings ADD COLUMN content_key TEXT")
        rows = conn.execute("SELECT * FROM listings").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE listings SET content_key = ? WHERE fb_id = ?",
                (content_key(dict(row)), row["fb_id"]),
            )
        conn.commit()


def init_db(path: Path | str) -> sqlite3.Connection:
    """Create the schema (idempotent) and return an open connection.

    Accepts a filesystem path or the sentinel ``":memory:"`` for tests.
    """
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.execute(_AI_SCHEMA)
    conn.execute(_DUPLICATES_SCHEMA)
    conn.execute(_DECISIONS_SCHEMA)
    _migrate(conn)
    for stmt in _INDEXES:
        conn.execute(stmt)
    conn.commit()
    return conn


def upsert_listing(
    conn: sqlite3.Connection,
    listing: dict[str, Any],
    *,
    now: str | None = None,
) -> str:
    """Insert a new listing or update an existing one keyed by ``fb_id``.

    Returns ``"inserted"``, ``"updated"`` or ``"duplicate"``. On update
    ``first_seen_at`` is preserved; ``last_seen_at``, ``price_*``,
    ``description``, ``raw_json`` and other mutable fields are refreshed.

    A new row whose ad text already exists under a different ``fb_id`` is a
    repost: it is recorded in ``duplicates`` and not inserted, so one flat
    occupies one line of the table however often the agency republishes it.
    Rows already in ``listings`` keep updating normally — an id that once
    earned its own row is never demoted behind the user's back.
    """
    fb_id = listing.get("fb_id")
    if not fb_id:
        raise ValueError("listing must include 'fb_id'")

    ts = now or _now_iso()
    existing = conn.execute(
        "SELECT first_seen_at FROM listings WHERE fb_id = ?", (fb_id,)
    ).fetchone()

    key = content_key(listing)
    if existing is None:
        # A listing collapsed by `dedupe` must not walk back in through a
        # later scrape: its text no longer matches anything (that is the
        # point of the near-duplicate rules), but the verdict stands.
        known = conn.execute(
            "SELECT canonical_fb_id FROM duplicates WHERE fb_id = ?", (fb_id,)
        ).fetchone()
        canonical = known["canonical_fb_id"] if known else canonical_for(conn, fb_id, key)
        if canonical is not None:
            _record_duplicate(
                conn, fb_id, canonical, key or "", ts, listing.get("url")
            )
            conn.commit()
            return "duplicate"

    raw_json = listing.get("raw_json")
    if raw_json is not None and not isinstance(raw_json, str):
        raw_json = json.dumps(raw_json, ensure_ascii=False, default=str)

    fields = {
        "fb_id": fb_id,
        "url": listing.get("url"),
        "title": listing.get("title"),
        "price_thb": listing.get("price_thb"),
        "price_raw": listing.get("price_raw"),
        "district": listing.get("district"),
        "content_key": key,
        "bedrooms": listing.get("bedrooms"),
        "property_type": listing.get("property_type"),
        "description": listing.get("description"),
        "published_at": listing.get("published_at"),
        "raw_json": raw_json,
    }

    if existing is None:
        fields["first_seen_at"] = ts
        fields["last_seen_at"] = ts
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(f":{k}" for k in fields.keys())
        conn.execute(
            f"INSERT INTO listings ({cols}) VALUES ({placeholders})", fields
        )
        conn.commit()
        return "inserted"

    fields["last_seen_at"] = ts
    assignments = ", ".join(f"{k} = :{k}" for k in fields.keys() if k != "fb_id")
    conn.execute(
        f"UPDATE listings SET {assignments} WHERE fb_id = :fb_id", fields
    )
    conn.commit()
    return "updated"


def _listing_signature(
    listing: dict[str, Any], fields: dict[str, Any]
) -> dict[str, Any]:
    """Everything the near-duplicate check needs about one listing."""
    text = _ad_text(listing)
    return {
        "fb_id": str(listing["fb_id"]),
        "text_len": len(text),
        "words": frozenset(_WORD_RE.findall(text.lower())),
        "project": _normalise_project(rules.value_of(fields, "project_name")),
        "price": rules.value_of(fields, "price_month_thb") or listing.get("price_thb"),
        "bedrooms": rules.value_of(fields, "bedrooms"),
        "size": rules.value_of(fields, "size_sqm"),
        "floor": _first_match(_FLOOR_PATTERNS, text),
        "building": _first_match(
            _BUILDING_PATTERNS,
            f"{rules.value_of(fields, 'project_name') or ''} {text}",
        ),
    }


def _contradicts(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when the two ads name a detail that cannot describe one flat."""
    for key in ("floor", "building"):
        if a[key] and b[key] and a[key] != b[key]:
            return True
    if (
        isinstance(a["size"], int)
        and isinstance(b["size"], int)
        and abs(a["size"] - b["size"]) > _SIZE_TOLERANCE_SQM
    ):
        return True
    return False


def same_property(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """Why two listings are the same flat, or ``None`` if they are not.

    Two independent signals, both vetoed by a contradicting floor, building
    or size:

    ``text``
        The ad body is all but identical — a repost with a line edited, which
        the exact fingerprint misses.
    ``facts``
        Same project, same price, same bedroom count. This is what catches
        one flat listed by several agents, who each write their own text.
    """
    if _contradicts(a, b):
        return None

    union = a["words"] | b["words"]
    if union:
        overlap = len(a["words"] & b["words"]) / len(union)
        if overlap >= _TEXT_MATCH_RATIO:
            return "text"

    if (
        a["project"]
        and b["project"]
        and a["price"] is not None
        and a["price"] == b["price"]
        and a["bedrooms"] is not None
        and a["bedrooms"] == b["bedrooms"]
        and difflib.SequenceMatcher(None, a["project"], b["project"]).ratio()
        >= _PROJECT_MATCH_RATIO
    ):
        return "facts"

    return None


def _load_for_dedupe(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every listing with its analysis fields, oldest first."""
    rows = conn.execute(
        """
        SELECT l.*, a.data_json AS ai_data
        FROM listings AS l
        LEFT JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        ORDER BY l.first_seen_at, l.fb_id
        """
    ).fetchall()

    loaded: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("ai_data", None)
        fields: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                fields = parsed
        item["_signature"] = _listing_signature(item, fields)
        loaded.append(item)
    return loaded


def _group_duplicates(
    items: list[dict[str, Any]], *, fuzzy: bool
) -> list[tuple[list[dict[str, Any]], set[str]]]:
    """Union listings that describe one flat, with the reasons they matched."""
    parent = {item["fb_id"]: item["fb_id"] for item in items}
    reasons: dict[str, set[str]] = {}

    def find(fb_id: str) -> str:
        while parent[fb_id] != fb_id:
            parent[fb_id] = parent[parent[fb_id]]
            fb_id = parent[fb_id]
        return fb_id

    def union(left: str, right: str, reason: str) -> None:
        a, b = find(left), find(right)
        if a == b:
            reasons.setdefault(a, set()).add(reason)
            return
        parent[b] = a
        merged = reasons.pop(b, set()) | reasons.get(a, set()) | {reason}
        reasons[a] = merged

    by_key: dict[str, str] = {}
    for item in items:
        key = item.get("content_key") or content_key(item)
        if not key:
            continue
        if key in by_key:
            union(by_key[key], item["fb_id"], "exact")
        else:
            by_key[key] = item["fb_id"]

    if fuzzy:
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                if find(left["fb_id"]) == find(right["fb_id"]):
                    continue
                reason = same_property(left["_signature"], right["_signature"])
                if reason:
                    union(left["fb_id"], right["fb_id"], reason)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(find(item["fb_id"]), []).append(item)
    return [
        (members, reasons.get(root, set()))
        for root, members in grouped.items()
        if len(members) > 1
    ]


def dedupe_listings(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    fuzzy: bool = True,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Collapse listings that describe the same flat, keeping one per group.

    Returns one entry per group it touched: ``{"canonical", "removed",
    "reason"}``. The keeper is whichever copy carries the most work — a
    decision of yours first, then an AI analysis, then the fullest ad text,
    then the one seen first. The rest leave ``listings`` together with their
    analyses and are remembered in ``duplicates``, which is what stops the
    next scrape from fetching them again.

    With ``fuzzy`` (the default) the near-duplicate rules in
    :func:`same_property` run too, which is what merges one flat advertised
    by several agents. They read the AI's extracted fields, so a listing
    only joins such a group once it has been analysed.
    """
    ts = now or _now_iso()
    items = _load_for_dedupe(conn)

    decided = {row["fb_id"] for row in conn.execute("SELECT fb_id FROM decisions")}
    analysed = {row["fb_id"] for row in conn.execute("SELECT fb_id FROM ai_analysis")}

    def rank(item: dict[str, Any]) -> tuple[int, int, int, str]:
        fb_id = item["fb_id"]
        return (
            0 if fb_id in decided else 1,
            0 if fb_id in analysed else 1,
            -item["_signature"]["text_len"],
            str(item.get("first_seen_at") or ""),
        )

    collapsed: list[dict[str, Any]] = []
    for members, why in _group_duplicates(items, fuzzy=fuzzy):
        keeper, *rest = sorted(members, key=rank)
        collapsed.append({
            "canonical": keeper["fb_id"],
            "removed": [item["fb_id"] for item in rest],
            "reason": ", ".join(sorted(why)),
        })
        if dry_run:
            continue
        key = keeper.get("content_key") or content_key(keeper) or ""
        for item in rest:
            fb_id = item["fb_id"]
            _record_duplicate(conn, fb_id, keeper["fb_id"], key, ts, item.get("url"))
            conn.execute("DELETE FROM ai_analysis WHERE fb_id = ?", (fb_id,))
            conn.execute("DELETE FROM listings WHERE fb_id = ?", (fb_id,))
    if not dry_run:
        conn.commit()
    return collapsed


def known_fb_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of ``fb_id`` values already present in the DB."""
    rows = conn.execute("SELECT fb_id FROM listings").fetchall()
    return {row["fb_id"] for row in rows}


class PendingListings(NamedTuple):
    """Split of the analysis queue: work to do vs. work that cannot be done."""

    pending: list[dict[str, Any]]
    no_raw: list[dict[str, Any]]


def text_lines_of(raw_json: Any) -> list[str]:
    """Return ``text_lines`` from a listing's ``raw_json``, or an empty list."""
    if isinstance(raw_json, str) and raw_json:
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            return []
    if isinstance(raw_json, dict):
        lines = raw_json.get("text_lines")
        if isinstance(lines, list):
            return [str(line) for line in lines]
    return []


def upsert_ai_analysis(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    now: str | None = None,
) -> str:
    """Insert or replace one AI analysis, keyed by ``fb_id``.

    Returns ``"inserted"`` or ``"updated"``. ``data_json`` may be passed as a
    dict and is serialised here; ``analyzed_at`` defaults to now.
    """
    fb_id = row.get("fb_id")
    if not fb_id:
        raise ValueError("ai analysis must include 'fb_id'")
    for required in ("schema_version", "source_hash", "verdict"):
        if row.get(required) in (None, ""):
            raise ValueError(f"ai analysis must include {required!r}")

    data_json = row.get("data_json")
    if data_json is None:
        data_json = {}
    if not isinstance(data_json, str):
        data_json = json.dumps(data_json, ensure_ascii=False, default=str)

    fields = {
        "fb_id": fb_id,
        "schema_version": row["schema_version"],
        "analyzed_at": row.get("analyzed_at") or now or _now_iso(),
        "source_hash": row["source_hash"],
        "model": row.get("model"),
        "verdict": row["verdict"],
        "comment": row.get("comment"),
        "score": row.get("score"),
        "data_quality": row.get("data_quality"),
        "data_json": data_json,
    }

    existing = conn.execute(
        "SELECT fb_id FROM ai_analysis WHERE fb_id = ?", (fb_id,)
    ).fetchone()

    if existing is None:
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(f":{k}" for k in fields.keys())
        conn.execute(
            f"INSERT INTO ai_analysis ({cols}) VALUES ({placeholders})", fields
        )
        conn.commit()
        return "inserted"

    assignments = ", ".join(f"{k} = :{k}" for k in fields.keys() if k != "fb_id")
    conn.execute(
        f"UPDATE ai_analysis SET {assignments} WHERE fb_id = :fb_id", fields
    )
    conn.commit()
    return "updated"


def fetch_pending(
    conn: sqlite3.Connection, registry: ai_schema.Registry
) -> PendingListings:
    """Split listings into "needs analysis" and "has nothing to analyse".

    A listing is pending when it has no ``ai_analysis`` row, when its
    ``source_hash`` no longer matches the stored one (the text changed under
    it), or when it was analysed against an older ``schema_version`` than the
    registry now declares. Each pending dict carries its freshly computed
    ``source_hash``, the ``analysed_version`` it was last analysed against
    (``0`` when never) and a ``pending_reason`` of ``new`` / ``changed`` /
    ``schema``.

    Pending listings with neither a description nor any ``text_lines`` are
    moved to ``no_raw`` instead: nothing to feed the AI, they need a
    ``scrape --refresh`` first.
    """
    rows = conn.execute(
        """
        SELECT l.*, a.source_hash AS ai_source_hash,
               a.schema_version AS ai_schema_version
        FROM listings AS l
        LEFT JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        ORDER BY l.last_seen_at DESC
        """
    ).fetchall()

    pending: list[dict[str, Any]] = []
    no_raw: list[dict[str, Any]] = []

    for row in rows:
        listing = dict(row)
        analysed_hash = listing.pop("ai_source_hash")
        analysed_version = listing.pop("ai_schema_version")
        current_hash = ai_schema.source_hash(
            {**listing, "text_lines": text_lines_of(listing.get("raw_json"))}
        )

        if analysed_hash is None:
            reason = "new"
        elif analysed_hash != current_hash:
            reason = "changed"
        elif (analysed_version or 0) < registry.schema_version:
            reason = "schema"
        else:
            continue

        listing["source_hash"] = current_hash
        listing["pending_reason"] = reason
        # Kept so the caller can tell a row analysed two field-additions ago
        # from one analysed at the previous version.
        listing["analysed_version"] = analysed_version or 0

        has_text = bool((listing.get("description") or "").strip()) or bool(
            text_lines_of(listing.get("raw_json"))
        )
        (pending if has_text else no_raw).append(listing)

    return PendingListings(pending=pending, no_raw=no_raw)


class PendingPhotos(NamedTuple):
    """Split of the photo queue: shots to look at vs. listings with none."""

    pending: list[dict[str, Any]]
    no_photos: list[dict[str, Any]]


def _fields_of(raw: Any) -> dict[str, Any]:
    """Return a stored ``*_data_json`` column as a dict, or an empty one."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def photo_fields_of(row: dict[str, Any]) -> dict[str, Any]:
    """Return a row's ``photo_data_json`` as a dict, or an empty one."""
    return _fields_of(row.get("photo_data_json"))


def fetch_pending_photos(
    conn: sqlite3.Connection,
    registry: ai_schema.Registry,
    *,
    photo_analysis_version: int | None = None,
) -> PendingPhotos:
    """Split analysed listings into "needs a photo pass" and "has no photos".

    Only listings the text pass already read are candidates, and only those
    the rules still call ``active``: an auto-rejected ad is not worth six
    images. A candidate is pending when it never had a photo pass, when the
    photo set on disk changed under it (``photos_hash``), or when it was
    read against an older registry than ``photo_analysis_version`` (which
    defaults to the registry's own ``schema_version``). Reasons mirror the
    text queue: ``new`` / ``changed`` / ``schema``.

    Candidates with an empty or missing photo directory go to ``no_photos``
    instead — they need a ``scrape --refresh`` before the pass can run.
    """
    wanted_version = (
        registry.schema_version
        if photo_analysis_version is None
        else photo_analysis_version
    )
    rows = conn.execute(
        """
        SELECT l.*, a.data_json, a.data_quality,
               a.photo_data_json, a.photos_hash AS analysed_photos_hash,
               a.photo_schema_version, a.photos_analyzed_at
        FROM listings AS l
        JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        ORDER BY l.last_seen_at DESC
        """
    ).fetchall()

    pending: list[dict[str, Any]] = []
    no_photos: list[dict[str, Any]] = []

    for row in rows:
        listing = dict(row)
        text_fields = _fields_of(listing.pop("data_json", None))
        photo_data = photo_fields_of(listing)
        merged = {**text_fields, **photo_data}
        status, _reason = rules.decide(
            merged, listing.get("price_thb"), listing.get("data_quality")
        )
        if status == "rejected":
            continue

        analysed_hash = listing.pop("analysed_photos_hash")
        analysed_version = listing.pop("photo_schema_version")
        analysed_at = listing.pop("photos_analyzed_at")
        current_hash = photos.photos_hash(str(listing["fb_id"]))

        if current_hash is None:
            no_photos.append(listing)
            continue

        if analysed_at is None:
            reason = "new"
        elif analysed_hash != current_hash:
            reason = "changed"
        elif (analysed_version or 0) < wanted_version:
            reason = "schema"
        else:
            continue

        listing["photos_hash"] = current_hash
        listing["pending_reason"] = reason
        listing["analysed_photo_version"] = analysed_version or 0
        pending.append(listing)

    return PendingPhotos(pending=pending, no_photos=no_photos)


def fetch_enriched(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every listing joined with its AI analysis and user decision.

    ``data_json`` is unpacked into ``fields``; ``ai_status`` / ``ai_reason``
    are recomputed from those fields and the row's ``data_quality`` by
    :mod:`.rules`, so a rule change takes effect without re-running the AI. A user decision always wins: ``status``,
    ``reason`` and ``decided_by`` reflect the file-backed ``decisions`` table
    whenever it has a row for the listing.
    """
    rows = conn.execute(
        """
        SELECT l.*,
               a.schema_version AS ai_schema_version,
               a.analyzed_at, a.source_hash AS ai_source_hash, a.model,
               a.verdict, a.comment, a.score, a.data_quality, a.data_json,
               d.status AS user_status, d.reason AS user_reason, d.decided_at
        FROM listings AS l
        LEFT JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        LEFT JOIN decisions AS d ON d.fb_id = l.fb_id
        ORDER BY l.first_seen_at DESC
        """
    ).fetchall()

    reposts = duplicates_by_canonical(conn)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["duplicates"] = reposts.get(str(item["fb_id"]), [])
        raw_fields = item.pop("data_json", None)
        fields: dict[str, Any] = {}
        if raw_fields:
            try:
                loaded = json.loads(raw_fields)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                fields = loaded
        item["fields"] = fields
        item["analyzed"] = item.get("analyzed_at") is not None

        if item["analyzed"]:
            ai_status, ai_reason = rules.decide(
                fields, item.get("price_thb"), item.get("data_quality")
            )
        else:
            ai_status, ai_reason = "active", None
        item["ai_status"] = ai_status
        item["ai_reason"] = ai_reason

        user_status = item.get("user_status")
        if user_status:
            item["status"] = user_status
            item["reason"] = item.get("user_reason")
            item["decided_by"] = "user"
        else:
            item["status"] = ai_status
            item["reason"] = ai_reason
            item["decided_by"] = "ai" if ai_reason else None

        enriched.append(item)

    return enriched


def export_csv(conn: sqlite3.Connection, path: Path | str) -> Path:
    """Regenerate the CSV export from the database.

    Rows are sorted by ``last_seen_at DESC``. Columns follow
    :data:`CSV_COLUMNS`. The file (including header) is always written even
    when there are no rows.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    query = f"SELECT {', '.join(CSV_COLUMNS)} FROM listings ORDER BY last_seen_at DESC"
    rows = conn.execute(query).fetchall()

    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([row[c] for c in CSV_COLUMNS])
    return out


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>fb-rent listings</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 1rem 1.25rem; color: #222; font-size: 12px; }
  h1 { margin: 0 0 0.2rem; font-size: 1rem; }
  .meta { color: #666; font-size: 0.75rem; margin-bottom: 0.6rem; }
  .bar { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-bottom: 0.5rem; }
  .bar .spacer { flex: 1 1 auto; }
  .tab { border: 1px solid #ccd; background: #fff; border-radius: 999px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #333; }
  .tab:hover { background: #f2f5fb; }
  .tab.on { background: #1a73e8; border-color: #1a73e8; color: #fff; }
  #more { border: 1px solid #b9c2d6; background: #fff; border-radius: 4px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #33405c; white-space: nowrap; }
  #more:hover { background: #eef2fa; }
  #more.on { background: #33405c; border-color: #33405c; color: #fff; }
  #q { border: 1px solid #ccd; border-radius: 4px; padding: 4px 8px; font-size: 12px; min-width: 220px; }
  .legend { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; font-size: 11px; color: #555; margin-bottom: 0.6rem; }
  .legend span.sw { display: inline-block; width: 14px; height: 14px; border: 1px solid #bbb; border-radius: 3px; vertical-align: -3px; margin-right: 4px; }
  .legend .item { white-space: nowrap; }
  .wrap { overflow: auto; max-height: 78vh; border: 1px solid #dde; border-radius: 4px; }
  table.rt { border-collapse: separate; border-spacing: 0; white-space: nowrap; }
  .rt th, .rt td { border-bottom: 1px solid #eef0f4; border-right: 1px solid #f3f4f7; padding: 5px 8px; text-align: left; vertical-align: top; font-weight: normal; }
  .rt th { background: #f7f8fb; font-weight: 600; user-select: none; }
  .rt thead th.lbl { position: sticky; top: 24px; z-index: 2; cursor: pointer; box-shadow: 0 1px 0 #dde; }
  .rt thead th.band { position: sticky; top: 0; z-index: 2; text-align: center; font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 8px; color: #55607a; }
  .rt thead th.lbl:hover { background: #eef1f7; }
  /* Where the data comes from: parsed off the FB page vs. extracted by the AI. */
  .rt .src-fb  { background: #f4f7fb; }
  .rt .src-ai  { background: #f8f5fb; }
  .rt .src-sys { background: #f7f8fb; }
  .rt .band.src-fb  { color: #2a5f9e; background: #e9f0f9; }
  .rt .band.src-ai  { color: #6b3fa0; background: #f1eaf9; }
  .rt .band.src-sys { color: #55607a; background: #eef0f4; }
  .rt td.src-ai { background: #fdfcfe; }
  .rt .edge { border-left: 2px solid #c8cfe0; }
  .rt td:first-child, .rt th:first-child { position: sticky; left: 0; z-index: 1; background: #fff; box-shadow: 1px 0 0 #dde; }
  .rt thead th:first-child { z-index: 4; background: #eef0f4; }
  .rt tbody tr:hover td { background: #f9fbff; }
  .id-list { display: flex; flex-direction: column; align-items: flex-start; gap: 1px; }
  .id-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #1a73e8; text-decoration: none; }
  .id-cell:hover { text-decoration: underline; }
  .id-row { display: flex; align-items: center; }
  .id-cell.dup-id { color: #6b7a99; }
  .dup-count { font-size: 9px; color: #6b3fa0; border: 1px solid #d8cfe8; border-radius: 3px; padding: 0 3px; margin-left: 4px; }
  /* Long values are clipped so the table stays scannable; a click unfolds
     the cell in place, a second click folds it back. */
  .clip { display: inline-block; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; cursor: zoom-in; }
  .clip.w-verdict { max-width: 240px; }
  .clip.w-comment { max-width: 340px; color: #555; }
  .rt td.open .clip { max-width: 460px; white-space: normal; overflow: visible; text-overflow: clip; cursor: zoom-out; }
  .rt tbody td.open, .rt tbody tr:hover td.open { background: #fffdf3; }
  .who { color: #888; font-size: 10px; }
  .num { font-variant-numeric: tabular-nums; text-align: right; }
  .badge { font-size: 9px; color: #8a7fa8; border: 1px solid #d8cfe8; border-radius: 3px; padding: 0 3px; margin-left: 4px; vertical-align: 1px; }
  .badge.fb { color: #4a7099; border-color: #cbd9e8; }
  .c-good { background: #e6f4ea; color: #137333; }
  .c-warn { background: #fef7e0; color: #8a6d00; }
  .c-bad  { background: #fce8e6; color: #a50e0e; }
  .c-stale { background: repeating-linear-gradient(45deg, #eef1f6, #eef1f6 4px, #dbe2ee 4px, #dbe2ee 8px); color: #55607a; }
  .c-none { color: #c2c6cf; }
  tr.row-rejected td { opacity: 0.55; text-decoration: line-through; }
  tr.row-rejected td:first-child { border-left: 3px solid #d93025; opacity: 1; text-decoration: none; }
  tr.row-shortlist td:first-child { border-left: 3px solid #f9ab00; }
  .empty { padding: 1rem; color: #888; }
</style>
</head>
<body>
<h1>fb-rent — __ROW_COUNT__ listings</h1>
<div class="meta">Сгенерировано __GENERATED_AT__ · реестр полей v__SCHEMA_VERSION__ · сортировка — клик по заголовку · ID открывает объявление · наведите на ячейку, чтобы увидеть цитату-основание · клик по обрезанной ячейке разворачивает её целиком</div>
<div class="bar">
  <button class="tab" data-tab="active"></button>
  <button class="tab" data-tab="shortlist"></button>
  <button class="tab" data-tab="rejected"></button>
  <button class="tab" data-tab="all"></button>
  <input id="q" type="search" placeholder="поиск по всем колонкам…">
  <span class="spacer"></span>
  <button id="more"></button>
</div>
<div class="legend">
  <span class="item"><span class="sw src-fb"></span>колонка из парсинга FB</span>
  <span class="item"><span class="sw src-ai"></span>колонка от AI</span>
  <span class="item"><span class="sw c-good"></span>подходит (pets allowed, цена в бюджете)</span>
  <span class="item"><span class="sw c-warn"></span>под вопросом (unclear, 30–35k ฿)</span>
  <span class="item"><span class="sw c-bad"></span>стоп-фактор (животные запрещены, вне бюджета)</span>
  <span class="item"><span class="sw c-stale"></span>поля не было на момент анализа — нужен ре-анализ</span>
  <span class="item"><span class="sw c-none"></span>AI смотрел, информации нет</span>
  <span class="item"><span class="sw" style="border-left:3px solid #d93025"></span>row-rejected — отклонено (AI или вами)</span>
</div>
<div class="wrap"><table class="rt"><thead><tr id="bands"></tr><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div class="empty" id="empty" hidden>Ничего не найдено.</div>
<script>
const COLUMNS = __COLUMNS_JSON__;
const DATA = __DATA_JSON__;

const TAB_LABELS = { active: "Active", shortlist: "Shortlist", rejected: "Rejected", all: "All" };
const SRC_LABELS = { fb: "FB — парсинг", ai: "AI — анализ", sys: "Решение" };
const CLIPPED_KINDS = { verdict: 1, comment: 1, field: 1, text: 1, reason: 1 };
const TYPE_ICONS = {
  house: "🏠", townhouse: "🏘️", villa: "🏡", condo: "🏢",
  apartment: "🏬", studio: "🚪", unknown: "❔"
};

let showExtra = false;

// Text longer than this gets clipped and becomes click-to-expand.
const CLIP_AT = 30;
// Cells the user unfolded, as "<fb_id>|<column>". Kept outside the DOM so
// sorting or filtering does not fold everything back up.
const openCells = new Set();

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function visibleColumns() {
  return COLUMNS.filter(c => showExtra || !c.hidden);
}

function cellOf(row, key) {
  return (row.cells && row.cells[key]) || { t: "", c: "" };
}

function haystack(row) {
  if (row._hay == null) {
    const dups = (cellOf(row, "fb_id").dups || []).map(d => d.fb_id).join(" ");
    row._hay = (COLUMNS.map(c => cellOf(row, c.key).t).join(" ") + " " + dups).toLowerCase();
  }
  return row._hay;
}

function inTab(row, tab) {
  return tab === "all" ? true : row.status === tab;
}

/* 0 → red, 50 → amber, 100 → green, in the same muted register as the
   c-good / c-warn / c-bad swatches so the scale reads as one palette. */
function scoreStyle(n) {
  const t = Math.max(0, Math.min(100, n)) / 100;
  const hue = Math.round(t * 125);
  return ' style="background: hsl(' + hue + ', 62%, 91%); color: hsl(' + hue + ', 55%, 26%); font-weight: 600"';
}

/* More rooms than we asked for is a plus, so the ramp only ever gets
   greener; 0 bedrooms is a studio, which the rules reject outright. */
function countStyle(n, cap) {
  if (n === 0) return ' style="background: #fce8e6; color: #a50e0e"';
  const t = Math.min(n, cap) / cap;
  const light = 96 - Math.round(t * 14);
  return ' style="background: hsl(136, ' + (30 + Math.round(t * 28)) + '%, ' + light + '%); color: #14532d"';
}

function tdHTML(row, col) {
  const cell = cellOf(row, col.key);
  const text = cell.t;
  const empty = text == null || text === "";
  const classes = ["cell", "src-" + col.src];
  if (col.edge) classes.push("edge");
  if (cell.c) classes.push(cell.c);
  if (col.kind === "num" || col.kind === "score" || col.kind === "count") classes.push("num");
  let title = cell.ev ? ' title="' + esc(cell.ev) + '"' : "";
  let style = "";

  let inner;
  if (col.kind === "id") {
    // One flat, one row — but every posting of it keeps its own link.
    const posts = [{ fb_id: text, url: row.url }].concat(cell.dups || []);
    inner = '<span class="id-list">' + posts.map((p, i) => {
      const label = esc(p.fb_id || "—");
      const cls = "id-cell" + (i ? " dup-id" : "");
      const link = p.url
        ? '<a class="' + cls + '" href="' + esc(p.url) + '" target="_blank" rel="noopener">' + label + '</a>'
        : '<span class="' + cls + '">' + label + '</span>';
      const count = (i === 0 && posts.length > 1)
        ? '<span class="dup-count" title="одно объявление, опубликованное ' + posts.length + ' раза">×' + posts.length + '</span>'
        : "";
      return '<span class="id-row">' + link + count + '</span>';
    }).join("") + '</span>';
  } else if (col.kind === "score") {
    inner = empty ? '—' : esc(text);
    if (!empty && cell.n != null) style = scoreStyle(cell.n);
  } else if (col.kind === "count") {
    inner = empty ? '—' : esc(text);
    if (!empty && cell.n != null) style = countStyle(cell.n, col.key === "bedrooms" ? 4 : 3);
  } else if (col.kind === "type") {
    inner = empty ? '—' : (TYPE_ICONS[text] || "🏚️") + " " + esc(text);
  } else if (col.kind === "reason") {
    if (empty) {
      inner = '—';
    } else {
      const who = row.decided_by === "user" ? "вы" : (row.decided_by === "ai" ? "AI" : "");
      inner = esc(text) + (who ? ' <span class="who">(' + esc(who) + ')</span>' : "");
    }
  } else {
    inner = empty ? '—' : esc(text);
  }

  // A merged column (price, district) shows whichever source had the better
  // value; the badge says which one that was.
  if (!empty && cell.src && cell.src !== col.src) {
    inner += '<span class="badge ' + esc(cell.src) + '">' + esc(cell.src) + '</span>';
  }

  const cellId = row.fb_id + "|" + col.key;
  if (!empty && CLIPPED_KINDS[col.kind] && String(text).length > CLIP_AT) {
    const width = col.kind === "verdict" || col.kind === "comment"
      ? " w-" + col.kind : "";
    inner = '<span class="clip' + width + '">' + inner + '</span>';
    // With no quote to show, the tooltip carries the full text instead.
    if (!cell.ev) title = ' title="' + esc(text) + '"';
    if (openCells.has(cellId)) classes.push("open");
  }
  return '<td class="' + classes.join(" ") + '" data-cell="' + esc(cellId) + '"'
    + title + style + '>' + inner + '</td>';
}

function rowHTML(row) {
  const cls = row.status === "rejected"
    ? "row-rejected"
    : (row.status === "shortlist" ? "row-shortlist" : "");
  return '<tr class="' + cls + '">' + visibleColumns().map(c => tdHTML(row, c)).join("") + '</tr>';
}

let tab = "active";
let sortKey = null;
let sortDir = 1;

function sortValue(row, key) {
  const cell = cellOf(row, key);
  if (cell.n != null) return cell.n;
  const t = cell.t;
  if (t == null || t === "") return null;
  const n = Number(String(t).replace(/[\s ]/g, ""));
  return Number.isFinite(n) ? n : String(t).toLowerCase();
}

function renderHead() {
  const cols = visibleColumns();
  const bands = [];
  cols.forEach(c => {
    const last = bands[bands.length - 1];
    if (last && last.src === c.src) last.n += 1;
    else bands.push({ src: c.src, n: 1 });
  });
  document.getElementById("bands").innerHTML = bands.map((b, i) =>
    '<th class="band src-' + b.src + (i ? ' edge' : '') + '" colspan="' + b.n + '">'
    + esc(SRC_LABELS[b.src] || b.src) + '</th>'
  ).join("");

  document.getElementById("head").innerHTML = cols.map(c =>
    '<th class="lbl src-' + c.src + (c.edge ? ' edge' : '') + '" data-key="' + esc(c.key) + '">'
    + esc(c.label) + '</th>'
  ).join("");

  document.querySelectorAll("#head th").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      sortDir = key === sortKey ? -sortDir : 1;
      sortKey = key;
      render();
    });
  });
}

function render() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  let rows = DATA.filter(r => inTab(r, tab) && (!q || haystack(r).indexOf(q) !== -1));

  if (sortKey) {
    rows = rows.slice().sort((a, b) => {
      const x = sortValue(a, sortKey), y = sortValue(b, sortKey);
      if (x === null && y === null) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      if (typeof x === "number" && typeof y === "number") return (x - y) * sortDir;
      return String(x).localeCompare(String(y), "ru") * sortDir;
    });
  }

  renderHead();
  document.getElementById("body").innerHTML = rows.map(rowHTML).join("");
  document.getElementById("empty").hidden = rows.length > 0;

  document.querySelectorAll(".tab").forEach(btn => {
    const key = btn.dataset.tab;
    const n = DATA.filter(r => inTab(r, key)).length;
    btn.textContent = TAB_LABELS[key] + " (" + n + ")";
    btn.classList.toggle("on", key === tab);
  });

  const extra = COLUMNS.filter(c => c.hidden).length;
  const more = document.getElementById("more");
  more.textContent = (showExtra ? "▲ Скрыть доп. поля" : "▼ Ещё " + extra + " полей");
  more.classList.toggle("on", showExtra);
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => { tab = btn.dataset.tab; render(); });
});

document.getElementById("body").addEventListener("click", ev => {
  // The ID cell is a link to the listing — let it navigate.
  if (ev.target.closest("a")) return;
  const td = ev.target.closest("td");
  if (!td || !td.querySelector(".clip")) return;
  const id = td.dataset.cell;
  if (openCells.has(id)) openCells.delete(id);
  else openCells.add(id);
  td.classList.toggle("open");
});

document.getElementById("more").addEventListener("click", () => {
  showExtra = !showExtra;
  render();
});

document.getElementById("q").addEventListener("input", render);

render();
</script>
</body>
</html>
"""

# Columns that come from the listing row itself rather than from the AI field
# registry. ``kind`` is what the embedded JS switches on when rendering a cell;
# ``src`` is which half of the pipeline filled it — the page groups columns
# into bands by it so a parsed value is never mistaken for an extracted one.
_BASE_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "fb_id", "label": "ID", "kind": "id", "src": "fb"},
    {"key": "price", "label": "Цена ฿/мес", "kind": "num", "src": "fb"},
    {"key": "area", "label": "Район", "kind": "text", "src": "fb"},
    {"key": "first_seen_at", "label": "Первый показ", "kind": "text", "src": "fb"},
    {"key": "score", "label": "Score", "kind": "score", "src": "ai"},
    {"key": "verdict", "label": "Рекомендация", "kind": "verdict", "src": "ai"},
    {"key": "comment", "label": "Комментарий AI", "kind": "comment", "src": "ai"},
    {"key": "data_quality", "label": "Качество данных", "kind": "text", "src": "ai"},
)

# The one column that is neither parsed nor extracted: why the listing is out,
# decided by `rules` or by the user. It closes the table.
_TAIL_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "reason", "label": "Причина", "kind": "reason", "src": "sys"},
)

# Registry fields the base columns already cover: `area` and the monthly price
# are merged with their parsed counterparts into a single column each.
_MERGED_FIELD_KEYS: frozenset[str] = frozenset({"price_month_thb", "area"})

# Registry fields shown up front. Everything else in the registry is an extra,
# folded away behind the "Ещё N полей" button.
_PRIMARY_FIELD_KEYS: tuple[str, ...] = (
    "pets",
    "property_type",
    "bedrooms",
    "bathrooms",
    "size_sqm",
    "extra_rooms",
    "pool_gym",
)

# Order of the folded-away columns: the five that most often decide a viewing
# come first, the rest keep the registry's own order.
_EXTRA_FIELD_ORDER: tuple[str, ...] = (
    "security",
    "deposit",
    "parking",
    "aircon",
    "kitchen",
)

# Values of the FB `district` field that name the island or the province
# rather than a district — nothing to show.
_DISTRICT_NOISE: frozenset[str] = frozenset(
    {"phuket", "пхукет", "ภูเก็ต", "changwat phuket", "thailand", "таиланд"}
)

_DISTRICT_PREFIXES: tuple[str, ...] = ("amphoe ", "changwat ", "tambon ", "mueang ")


def normalise_district(value: Any) -> str:
    """Reduce FB's location string to the district itself.

    ``Rawai, Phuket, Thailand`` → ``Rawai``, ``Amphoe Thalang`` → ``Thalang``.
    A string that only names the island or the province carries no location,
    so it collapses to an empty cell rather than repeating "Пхукет" 30 times.
    """
    if not value:
        return ""
    head = str(value).split(",")[0].strip()
    lowered = head.lower()
    for prefix in _DISTRICT_PREFIXES:
        if lowered.startswith(prefix):
            head = head[len(prefix):].strip()
            lowered = head.lower()
            break
    return "" if lowered in _DISTRICT_NOISE else head


def _format_number(value: Any) -> str:
    """Render an integer with thin spaces between thousands (``16 000``)."""
    return f"{value:,}".replace(",", " ")


def _display(value: Any) -> str:
    """Render one field value as the text shown in its cell."""
    if rules.is_empty(value):
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return _format_number(value)
    if isinstance(value, float):
        return _format_number(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _extra_field_keys(registry: ai_schema.Registry) -> list[str]:
    """Registry keys that live behind the "Ещё N полей" toggle, in order."""
    known = set(_PRIMARY_FIELD_KEYS) | _MERGED_FIELD_KEYS
    rest = [f["key"] for f in registry.fields if f["key"] not in known]
    priority = [k for k in _EXTRA_FIELD_ORDER if k in rest]
    return priority + [k for k in rest if k not in priority]


def html_columns(registry: ai_schema.Registry) -> list[dict[str, Any]]:
    """Return every export column: the fixed ones plus the registry's fields.

    Registry fields follow the base columns in the AI band — first the ones
    worth a glance in the default view, then the extras, which ship with
    ``hidden`` set so the page can fold them away. ``edge`` marks the column
    that opens a new source band, which is where the page draws its divider.
    """
    by_key = {field["key"]: field for field in registry.fields}

    def field_column(key: str, *, hidden: bool) -> dict[str, Any]:
        field = by_key[key]
        if key == "property_type":
            kind = "type"
        elif key in ("bedrooms", "bathrooms"):
            kind = "count"
        elif field.get("type") == "int":
            kind = "num"
        else:
            kind = "field"
        return {
            "key": key,
            "label": field.get("label") or key,
            "kind": kind,
            "src": "ai",
            "group": field.get("group") or "",
            "hidden": hidden,
        }

    columns: list[dict[str, Any]] = [dict(col) for col in _BASE_COLUMNS]
    columns += [
        field_column(key, hidden=False)
        for key in _PRIMARY_FIELD_KEYS
        if key in by_key
    ]
    columns += [
        field_column(key, hidden=True) for key in _extra_field_keys(registry)
    ]
    columns += [dict(col) for col in _TAIL_COLUMNS]

    previous = None
    for column in columns:
        column.setdefault("hidden", False)
        column["edge"] = previous is not None and column["src"] != previous
        previous = column["src"]
    return columns


def html_row(item: dict[str, Any], registry: ai_schema.Registry) -> dict[str, Any]:
    """Turn one :func:`fetch_enriched` item into a row for the HTML payload.

    Every cell carries its text, its colour class from :func:`rules.cell_class`
    and — where the AI supplied one — the quote backing the value, which the
    page shows as a tooltip so any claim can be traced to the source text.
    Price and district exist twice in the pipeline (parsed off the card, then
    read out of the ad text); each is merged into one cell that prefers the
    AI's reading and records in ``src`` which one is on screen.
    """
    analyzed = bool(item.get("analyzed"))
    fields = item.get("fields") or {}
    # An unanalysed row has no schema version of its own. Pretending it is
    # current keeps its blank cells plain grey: `c-stale` means "the field
    # arrived after this row was analysed", which says nothing here.
    version = (item.get("ai_schema_version") or 0) if analyzed else registry.schema_version

    status = item.get("status") or "active"
    reason = item.get("reason")
    first_seen = item.get("first_seen_at") or ""
    score = item.get("score")

    fb_price = item.get("price_thb")
    ai_price = rules.value_of(fields, "price_month_thb")
    price = ai_price if not rules.is_empty(ai_price) else fb_price
    price_ev = _price_tooltip(fields, fb_price, ai_price)

    fb_area = normalise_district(item.get("district"))
    ai_area = rules.value_of(fields, "area")
    area = ai_area if not rules.is_empty(ai_area) else fb_area

    cells: dict[str, dict[str, Any]] = {
        "fb_id": {"t": item.get("fb_id"), "c": "", "dups": item.get("duplicates") or []},
        "price": {
            "t": _display(price),
            "c": rules.cell_class("price_month_thb", price, {"added_in": 1}, version),
            "src": "ai" if not rules.is_empty(ai_price) else "fb",
        },
        "area": {
            "t": _display(area),
            "c": "",
            "src": "ai" if not rules.is_empty(ai_area) else "fb",
        },
        "first_seen_at": {"t": str(first_seen)[:10], "c": ""},
        "score": {
            "t": _display(score),
            "c": "" if analyzed else rules.CLASS_NONE,
            "n": score if isinstance(score, int) else None,
        },
        "verdict": {
            "t": item.get("verdict") or "",
            "c": "" if analyzed else rules.CLASS_NONE,
        },
        "comment": {
            "t": item.get("comment") or "",
            "c": "" if analyzed else rules.CLASS_NONE,
        },
        "data_quality": {
            "t": item.get("data_quality") or "",
            "c": rules.CLASS_WARN
            if analyzed and item.get("data_quality") not in (None, "", "good")
            else "",
        },
        "reason": {
            "t": reason or "",
            "c": rules.CLASS_BAD if status == "rejected" and reason else "",
        },
    }
    if price_ev:
        cells["price"]["ev"] = price_ev

    for field in registry.fields:
        key = field["key"]
        if key in _MERGED_FIELD_KEYS:
            continue
        value = rules.value_of(fields, key)
        cell: dict[str, Any] = {
            "t": _display(value),
            "c": rules.cell_class(key, value, field, version),
        }
        if key in ("bedrooms", "bathrooms"):
            cell["n"] = value if isinstance(value, int) else None
        raw = fields.get(key)
        if isinstance(raw, dict) and raw.get("ev"):
            cell["ev"] = str(raw["ev"])
        cells[key] = cell

    return {
        "fb_id": item.get("fb_id"),
        "url": item.get("url"),
        "status": status,
        "decided_by": item.get("decided_by"),
        "analyzed": analyzed,
        "cells": cells,
    }


def _price_tooltip(
    fields: dict[str, Any], fb_price: Any, ai_price: Any
) -> str:
    """Everything the merged price cell folds away, as tooltip lines.

    The card price and the price in the ad text disagree often enough that
    hiding one of them silently would be a lie; the yearly rate goes here too
    so the twelve-month tier survives the column merge.
    """
    parts: list[str] = []
    quote = fields.get("price_month_thb")
    if isinstance(quote, dict) and quote.get("ev"):
        parts.append(str(quote["ev"]))
    if (
        not rules.is_empty(ai_price)
        and not rules.is_empty(fb_price)
        and ai_price != fb_price
    ):
        parts.append(f"AI: {_display(ai_price)} · карточка FB: {_display(fb_price)}")
    yearly = rules.value_of(fields, "price_12m_thb")
    if not rules.is_empty(yearly) and yearly != ai_price:
        parts.append(f"12 месяцев: {_display(yearly)} ฿")
    return "\n".join(parts)


def _inline_json(payload: Any) -> str:
    """Serialise a payload for embedding inside the page's ``<script>`` block.

    ``</`` and ``<!--`` are neutralised so no listing text can close the
    script element or open an HTML comment.
    """
    return (
        json.dumps(payload, ensure_ascii=False, default=str)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


def export_html(
    conn: sqlite3.Connection,
    path: Path | str,
    registry: ai_schema.Registry | None = None,
) -> Path:
    """Regenerate the self-contained HTML table from the database.

    Every listing is rendered with every registry field, joined with its AI
    analysis and the user's own verdict: cells are coloured by
    :func:`rules.cell_class`, rejected rows are struck through and carry the
    reason plus who rejected them, and the Active / Shortlist / Rejected / All
    tabs filter the set. Data is embedded inline, so the file opens straight
    from disk with no server and no network.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if registry is None:
        registry = ai_schema.load_registry()

    columns = html_columns(registry)
    data = [html_row(item, registry) for item in fetch_enriched(conn)]

    html = (
        _HTML_TEMPLATE
        .replace("__ROW_COUNT__", str(len(data)))
        .replace("__GENERATED_AT__", _now_iso())
        .replace("__SCHEMA_VERSION__", str(registry.schema_version))
        .replace("__COLUMNS_JSON__", _inline_json(columns))
        .replace("__DATA_JSON__", _inline_json(data))
    )
    out.write_text(html, encoding="utf-8")
    return out
