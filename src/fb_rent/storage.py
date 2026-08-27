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
``buildings`` is the same arrangement for ``data/buildings.json``: a condo's
pets rule is researched once per building and vetted by hand, so the file
leads and the table only exists to be JOINed.
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
from collections.abc import Iterable
from typing import Any, NamedTuple

from . import ai_schema, buildings, config, extractors, photos, rules


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
    rating     REAL,
    decided_at TEXT NOT NULL
);
"""

_BUILDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    key           TEXT PRIMARY KEY,
    name          TEXT,
    area          TEXT,
    policy        TEXT,
    status        TEXT,
    confidence    TEXT,
    note          TEXT,
    aliases_json  TEXT,
    sources_json  TEXT,
    researched_at TEXT,
    confirmed_at  TEXT
);
"""

_PHOTO_HASHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_hashes (
    fb_id      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    md5        TEXT NOT NULL,
    dhash      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (fb_id, position)
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_last_seen ON listings(last_seen_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_price ON listings(price_thb);",
    "CREATE INDEX IF NOT EXISTS idx_content_key ON listings(content_key);",
    "CREATE INDEX IF NOT EXISTS idx_dup_canonical ON duplicates(canonical_fb_id);",
    "CREATE INDEX IF NOT EXISTS idx_photo_md5 ON photo_hashes(md5);",
    "CREATE INDEX IF NOT EXISTS idx_photo_dhash ON photo_hashes(dhash);",
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
# Two agents pricing one house rarely agree to the baht — each adds his own
# margin. Inside a condo the opposite holds: a project holds hundreds of
# near-identical units, so a price gap there is the very thing that tells
# them apart. The tolerance is therefore houses only.
_HOUSE_TYPES = frozenset({"house", "townhouse", "villa"})
_HOUSE_PRICE_TOLERANCE_THB = 1000


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

    have = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "rating" not in have:
        # The mirror is rebuilt from decisions.json on every sync, so the
        # column starts empty and fills itself on the next `export`.
        conn.execute("ALTER TABLE decisions ADD COLUMN rating REAL")

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
    conn.execute(_PHOTO_HASHES_SCHEMA)
    conn.execute(_BUILDINGS_SCHEMA)
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
    ``first_seen_at`` is preserved and every field the new sighting actually
    carries is refreshed — but a field it does not carry is left alone. That
    asymmetry is the whole point: ``scrape`` skips the detail page of a
    listing already in the DB, so a re-sighting is a list card and nothing
    more, and writing its Nones would erase the ad text the AI pass reads,
    the ``published_at`` behind it and the gallery URLs the photos come from.
    A wide sweep re-sees most of the table, so this used to wipe the base
    once per run.

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

    fields = {column: value for column, value in fields.items() if value is not None}
    fields["fb_id"] = fb_id
    fields["last_seen_at"] = ts
    assignments = ", ".join(f"{k} = :{k}" for k in fields.keys() if k != "fb_id")
    conn.execute(
        f"UPDATE listings SET {assignments} WHERE fb_id = :fb_id", fields
    )
    conn.commit()
    return "updated"


def _listing_signature(
    listing: dict[str, Any],
    fields: dict[str, Any],
    data_quality: str | None = None,
) -> dict[str, Any]:
    """Everything the near-duplicate check needs about one listing.

    ``data_quality`` other than ``good`` empties the word set, the same way
    it suppresses a rejection in :func:`rules.decide`: what got stored was
    the Facebook sidebar, not the ad. That sidebar is identical across
    listings, so leaving it in would match every such row against every
    other at 100% and collapse unrelated flats into one.
    """
    text = _ad_text(listing)
    is_ad = not data_quality or data_quality == "good"
    return {
        "fb_id": str(listing["fb_id"]),
        "text_len": len(text) if is_ad else 0,
        "words": frozenset(_WORD_RE.findall(text.lower())) if is_ad else frozenset(),
        "project": _normalise_project(rules.value_of(fields, "project_name")),
        "price": rules.value_of(fields, "price_month_thb") or listing.get("price_thb"),
        "property_type": rules.value_of(fields, "property_type"),
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


def _same_project(a: str, b: str) -> bool:
    """Whether two project names denote one project.

    Names arrive misspelled and half transliterated, which the ratio covers,
    and they also arrive trimmed: one agent's "Pruksa Ville Thalang" is
    another's "Pruksa Ville Thalang Hillside". A trailing word like that costs
    enough ratio to miss the threshold, so one name's words being a subset of
    the other's counts as well.
    """
    if difflib.SequenceMatcher(None, a, b).ratio() >= _PROJECT_MATCH_RATIO:
        return True
    words_a, words_b = set(a.split()), set(b.split())
    if not (words_a and words_b):
        return False
    return words_a <= words_b or words_b <= words_a


def _price_matches(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether the two ads agree on the rent.

    Exact equality, except between houses — see ``_HOUSE_PRICE_TOLERANCE_THB``.
    """
    price_a, price_b = a["price"], b["price"]
    if price_a is None or price_b is None:
        return False
    if price_a == price_b:
        return True
    if not (
        a["property_type"] in _HOUSE_TYPES and b["property_type"] in _HOUSE_TYPES
    ):
        return False
    if not (isinstance(price_a, int) and isinstance(price_b, int)):
        return False
    return abs(price_a - price_b) <= _HOUSE_PRICE_TOLERANCE_THB


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
        Both the project name and the price are matched with the slack that
        such ads actually carry — see :func:`_same_project` and
        :func:`_price_matches`.
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
        and _price_matches(a, b)
        and a["bedrooms"] is not None
        and a["bedrooms"] == b["bedrooms"]
        and _same_project(a["project"], b["project"])
    ):
        return "facts"

    return None


class PhotoMatches(NamedTuple):
    """Pairs that share enough photos, plus how many hashes were too common."""

    pairs: list[tuple[str, str]]
    common_hashes: int


def _photo_matches(
    hashes_by_id: dict[str, list[dict[str, Any]]],
) -> PhotoMatches:
    """Pairs of listings whose first shots are the same pictures.

    Both hashes of a shot are exact keys in one inverted index: an ``md5``
    match means the very same file was reposted, a ``dhash`` match means the
    picture survived a resize or a recompression on the way. No Hamming
    distance — a distance threshold needs tuning and glues together the
    empty white interiors that all look alike.

    A hash carried by more than ``PHOTO_HASH_COMMON_MAX`` listings leaves
    the index first: that is the agency splash screen or the shared view of
    the complex, not the flat. What remains yields the candidate pairs, so
    nothing is compared against everything; each pair is then counted
    greedily, one shot of B per shot of A, and passes at
    ``PHOTO_MATCH_MIN`` matched shots.
    """
    index: dict[tuple[str, str], set[str]] = {}
    for fb_id, shots in hashes_by_id.items():
        for shot in shots:
            for kind, value in (("m", shot["md5"]), ("d", shot["dhash"])):
                if value:
                    index.setdefault((kind, str(value)), set()).add(fb_id)

    common = {
        key
        for key, owners in index.items()
        if len(owners) > config.PHOTO_HASH_COMMON_MAX
    }
    candidates: set[tuple[str, str]] = set()
    for key, owners in index.items():
        if key in common or len(owners) < 2:
            continue
        ordered = sorted(owners)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                candidates.add((left, right))

    def keys_of(shot: dict[str, Any]) -> set[tuple[str, str]]:
        pairs = {("m", str(shot["md5"])), ("d", str(shot["dhash"]))}
        return {key for key in pairs if key not in common}

    pairs: list[tuple[str, str]] = []
    for left, right in sorted(candidates):
        right_keys = [keys_of(shot) for shot in hashes_by_id[right]]
        taken = [False] * len(right_keys)
        matched = 0
        for shot in hashes_by_id[left]:
            wanted = keys_of(shot)
            for position, other in enumerate(right_keys):
                if taken[position] or not (wanted & other):
                    continue
                taken[position] = True
                matched += 1
                break
        if matched >= config.PHOTO_MATCH_MIN:
            pairs.append((left, right))

    return PhotoMatches(pairs, len(common))


def _load_for_dedupe(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every listing with its analysis fields, oldest first."""
    rows = conn.execute(
        """
        SELECT l.*, a.data_json AS ai_data, a.data_quality AS ai_data_quality
        FROM listings AS l
        LEFT JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        ORDER BY l.first_seen_at, l.fb_id
        """
    ).fetchall()

    hashes = photo_hashes_by_listing(conn)

    loaded: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        quality = item.pop("ai_data_quality", None)
        raw = item.pop("ai_data", None)
        fields: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                fields = parsed
        item["_signature"] = _listing_signature(item, fields, quality)
        item["_photo_hashes"] = hashes.get(str(item["fb_id"]), [])
        loaded.append(item)
    return loaded


def _group_duplicates(
    items: list[dict[str, Any]], *, fuzzy: bool
) -> list[tuple[list[dict[str, Any]], set[str]]]:
    """Union listings that describe one flat, with the reasons they matched.

    Every signal but ``exact`` is vetoed against the whole group, not just
    against the listing it paired with. A union is transitive and
    ``_contradicts`` is not: an ad that names neither floor nor building
    contradicts nobody, so in a new complex where dozens of identical
    one-bedrooms rent at the same price it matches every one of them on
    ``facts`` and chains building A to building C through itself. Checking
    the newcomer against every member keeps a group internally consistent,
    which is what "one flat" means. ``exact`` skips the veto: a byte-identical
    ad body is the same text, whatever the two copies claim about the floor.
    """
    parent = {item["fb_id"]: item["fb_id"] for item in items}
    reasons: dict[str, set[str]] = {}
    signatures = {item["fb_id"]: item["_signature"] for item in items}
    members: dict[str, list[str]] = {
        item["fb_id"]: [item["fb_id"]] for item in items
    }

    def find(fb_id: str) -> str:
        while parent[fb_id] != fb_id:
            parent[fb_id] = parent[parent[fb_id]]
            fb_id = parent[fb_id]
        return fb_id

    def union(left: str, right: str, reason: str, *, guarded: bool = True) -> None:
        a, b = find(left), find(right)
        if a == b:
            reasons.setdefault(a, set()).add(reason)
            return
        if guarded and any(
            _contradicts(signatures[x], signatures[y])
            for x in members[a]
            for y in members[b]
        ):
            return
        parent[b] = a
        members[a].extend(members.pop(b))
        merged = reasons.pop(b, set()) | reasons.get(a, set()) | {reason}
        reasons[a] = merged

    by_key: dict[str, str] = {}
    for item in items:
        key = item.get("content_key") or content_key(item)
        if not key:
            continue
        if key in by_key:
            union(by_key[key], item["fb_id"], "exact", guarded=False)
        else:
            by_key[key] = item["fb_id"]

    hashes_by_id = {
        item["fb_id"]: item.get("_photo_hashes") or []
        for item in items
        if item.get("_photo_hashes")
    }
    # Not behind ``fuzzy``: the photo signal reads no AI field, so it is as
    # hard a fact as ``content_key`` and belongs with the exact pass.
    for left_id, right_id in _photo_matches(hashes_by_id).pairs:
        if find(left_id) == find(right_id):
            continue
        union(left_id, right_id, "photos")

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
            delete_photo_hashes(conn, fb_id)
            conn.execute("DELETE FROM listings WHERE fb_id = ?", (fb_id,))
            # The keeper's own photos stay: it is the row that survives.
            photos.purge_photos(fb_id)
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


def gallery_urls_of(raw_json: Any) -> list[str]:
    """Return ``gallery_urls`` from a listing's ``raw_json``, or an empty list.

    The links are signed and expire in about a week, so a stored gallery is
    a short-lived promise: ``photos-fetch`` can still download it, and after
    that only the detail page can say what the pictures are.
    """
    if isinstance(raw_json, str) and raw_json:
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            return []
    if isinstance(raw_json, dict):
        urls = raw_json.get("gallery_urls")
        if isinstance(urls, list):
            return [str(url) for url in urls]
    return []


def save_photo_hashes(
    conn: sqlite3.Connection,
    fb_id: str,
    entries: Iterable[tuple[int, str, str]],
    *,
    now: str | None = None,
) -> int:
    """Store the ``(position, md5, dhash)`` triples of one listing.

    The listing's previous rows are dropped first, so an empty ``entries``
    erases what was there: a refetch that came back with nothing means the
    gallery is gone, not that the old hashes are still true.
    """
    ts = now or _now_iso()
    rows = [(fb_id, int(pos), str(md5), str(dh), ts) for pos, md5, dh in entries]
    conn.execute("DELETE FROM photo_hashes WHERE fb_id = ?", (fb_id,))
    if rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO photo_hashes
                (fb_id, position, md5, dhash, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def delete_photo_hashes(conn: sqlite3.Connection, fb_id: str) -> None:
    """Forget a listing's photo hashes (used when its row is collapsed away)."""
    conn.execute("DELETE FROM photo_hashes WHERE fb_id = ?", (fb_id,))


def photo_hashes_by_listing(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Return every stored hash grouped by ``fb_id``, ordered by position.

    One query for the whole table: the dedupe pass needs all of it at once
    to build its inverted index, and per-row lookups would be a join per
    candidate pair.
    """
    rows = conn.execute(
        """
        SELECT fb_id, position, md5, dhash
        FROM photo_hashes
        ORDER BY fb_id, position
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["fb_id"]), []).append({
            "position": row["position"],
            "md5": row["md5"],
            "dhash": row["dhash"],
        })
    return grouped


def listings_missing_photo_hashes(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return listings that have no photo hashes yet, oldest first.

    Only rows carrying a ``raw_json`` are candidates — without it there are
    no ``gallery_urls`` to fetch. Oldest first because the CDN signatures
    expire in about a week: whatever is closest to that edge is worth doing
    before it turns into a ``scrape --ids``.
    """
    rows = conn.execute(
        """
        SELECT l.*
        FROM listings AS l
        LEFT JOIN photo_hashes AS p ON p.fb_id = l.fb_id
        WHERE p.fb_id IS NULL AND l.raw_json IS NOT NULL
        ORDER BY l.last_seen_at ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


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


def upsert_photo_analysis(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    now: str | None = None,
) -> str:
    """Write one photo pass onto an existing ``ai_analysis`` row.

    Unlike :func:`upsert_ai_analysis` this never inserts: the photo pass runs
    on top of a text analysis, and ``ai_analysis`` cannot hold a row without
    the text pass's ``verdict`` and ``source_hash``. A listing the text pass
    never read raises :class:`ValueError`; the caller reports it as skipped.

    ``score`` is optional and only written when the key is present — it is a
    shared column, recomputed by whichever pass ran last.
    """
    fb_id = row.get("fb_id")
    if not fb_id:
        raise ValueError("photo analysis must include 'fb_id'")
    for required in ("photo_schema_version", "photos_hash"):
        if row.get(required) in (None, ""):
            raise ValueError(f"photo analysis must include {required!r}")

    data_json = row.get("photo_data_json")
    if data_json is None:
        data_json = {}
    if not isinstance(data_json, str):
        data_json = json.dumps(data_json, ensure_ascii=False, default=str)

    fields = {
        "fb_id": fb_id,
        "photo_data_json": data_json,
        "photo_comment": row.get("photo_comment"),
        "photos_hash": row["photos_hash"],
        "photo_schema_version": row["photo_schema_version"],
        "photos_analyzed_at": row.get("photos_analyzed_at") or now or _now_iso(),
        "photo_model": row.get("photo_model"),
    }
    if "score" in row:
        fields["score"] = row["score"]

    existing = conn.execute(
        "SELECT fb_id FROM ai_analysis WHERE fb_id = ?", (fb_id,)
    ).fetchone()
    if existing is None:
        raise ValueError(f"{fb_id} has no text analysis to attach photos to")

    assignments = ", ".join(f"{k} = :{k}" for k in fields if k != "fb_id")
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
    ``scrape --ids <fb_id>`` first.
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


def fields_of(raw: Any) -> dict[str, Any]:
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


def _photo_overrides(
    text_fields: dict[str, Any], photo_fields: dict[str, Any]
) -> dict[str, Any]:
    """Keys where the photos contradicted something the text already said.

    Mirrors :func:`.photo_batch._overrides`: only a disagreement counts, so a
    photo confirming the text or filling a gap it left empty is not flagged,
    and an empty photo answer ("could not see") never overrides anything.
    Maps the key to the text's old value — that is what the tooltip shows.
    """
    found: dict[str, Any] = {}
    for key in photo_fields:
        old = rules.value_of(text_fields, key)
        new = rules.value_of(photo_fields, key)
        if rules.is_empty(old) or rules.is_empty(new):
            continue
        if old != new:
            found[key] = old
    return found


def photo_fields_of(row: dict[str, Any]) -> dict[str, Any]:
    """Return a row's ``photo_data_json`` as a dict, or an empty one."""
    return fields_of(row.get("photo_data_json"))


def fetch_pending_photos(
    conn: sqlite3.Connection,
    registry: ai_schema.Registry,
    *,
    photo_analysis_version: int | None = None,
) -> PendingPhotos:
    """Split analysed listings into "needs a photo pass" and "has no photos".

    Only listings the text pass already read are candidates, and only those
    still worth looking at: an ad that has been rejected is not worth a
    single image, let alone a gallery.

    Who did the rejecting decides. A user verdict from ``decisions`` wins
    outright, the way it does in :func:`fetch_enriched` — a hand-rejected
    listing is dropped even when the rules like it, and a hand-kept one
    stays even when the rules do not. Only where the user has said nothing
    does :func:`rules.decide` get a vote. This is what makes a review-first
    workflow possible: reject by hand, then spend tokens on the survivors.

    A candidate is pending when it never had a photo pass, when the photo
    set on disk changed under it (``photos_hash``), or when it was read
    against an older registry than ``photo_analysis_version`` (which
    defaults to the registry's own ``schema_version``). Reasons mirror the
    text queue: ``new`` / ``changed`` / ``schema``.

    Candidates with an empty or missing photo directory go to ``no_photos``
    instead — they need a ``photos-fetch`` before the pass can run.
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
               a.photo_schema_version, a.photos_analyzed_at,
               d.status AS user_status
        FROM listings AS l
        JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        LEFT JOIN decisions AS d ON d.fb_id = l.fb_id
        ORDER BY l.last_seen_at DESC
        """
    ).fetchall()

    pending: list[dict[str, Any]] = []
    no_photos: list[dict[str, Any]] = []

    for row in rows:
        listing = dict(row)
        text_fields = fields_of(listing.pop("data_json", None))
        photo_data = photo_fields_of(listing)
        user_status = listing.pop("user_status", None)
        if user_status:
            if user_status == "rejected":
                continue
        else:
            merged = {**text_fields, **photo_data}
            status, _reason = rules.decide(
                merged,
                listing.get("price_thb"),
                listing.get("data_quality"),
                listing.get("district"),
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


_BUILDING_COLUMNS: tuple[str, ...] = (
    "key",
    "name",
    "area",
    "policy",
    "status",
    "confidence",
    "note",
    "aliases_json",
    "sources_json",
    "researched_at",
    "confirmed_at",
)

# How many listings a queue row names outright. The count is what matters when
# choosing what to research; the ids are only there to spot-check a building.
QUEUE_SAMPLE_IDS = 5


def _json_list(raw: Any) -> list[Any]:
    """Decode a JSON list column, forgiving a hand-edited mess."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _building_index(store: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map every known spelling of a building to its canonical key.

    Both the key itself and each alias, normalised the same way, so a listing
    naming the condo the way one agent spells it lands on the entry another
    agent's spelling created. Nothing fuzzy happens here: an alias is a
    hand-made statement that two names are one building.
    """
    index: dict[str, str] = {}
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        index.setdefault(str(key), str(key))
        for alias in entry.get("aliases") or []:
            alias_key = buildings.building_key(alias)
            if alias_key:
                index.setdefault(alias_key, str(key))
    return index


def sync_buildings_to_db(
    conn: sqlite3.Connection,
    store: dict[str, dict[str, Any]] | None = None,
    *,
    path: Path | str | None = None,
) -> int:
    """Mirror ``data/buildings.json`` into the ``buildings`` table.

    The file wins, exactly as it does for decisions: entries it no longer
    contains are deleted, so removing a building by hand propagates. Pass
    ``store`` to sync an already-loaded map, otherwise it is read from
    ``path`` (or ``config.BUILDINGS_PATH``). Returns the number of rows
    written.
    """
    if store is None:
        store = buildings.load_buildings(path)

    rows = []
    for key, entry in store.items():
        policy = entry.get("policy")
        status = entry.get("status")
        if policy not in buildings.POLICIES:
            raise buildings.BuildingError(
                f"building {key}: unknown pets policy {policy!r}"
            )
        if status not in buildings.STATUSES:
            raise buildings.BuildingError(
                f"building {key}: unknown status {status!r}"
            )
        rows.append(
            (
                str(key),
                entry.get("name") or str(key),
                entry.get("area"),
                policy,
                status,
                entry.get("confidence"),
                entry.get("note"),
                json.dumps(entry.get("aliases") or [], ensure_ascii=False),
                json.dumps(entry.get("sources") or [], ensure_ascii=False),
                entry.get("researched_at"),
                entry.get("confirmed_at"),
            )
        )

    conn.execute("DELETE FROM buildings")
    conn.executemany(
        f"INSERT INTO buildings ({', '.join(_BUILDING_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_BUILDING_COLUMNS))})",
        rows,
    )
    conn.commit()
    return len(rows)


def building_policies(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{spelling: policy}`` for every policy allowed to act.

    Only ``confirmed`` entries, and only a policy that says something: a
    ``proposed`` building is inert by design and a confirmed ``unknown`` is
    research that found nothing. Both are absent from the map, which is the
    same absence an unresearched building has — the caller cannot tell the
    three apart and no rule needs to. Mirrors :func:`.buildings.policy_for`,
    over the table instead of the file.

    Aliases get their own entry pointing at the same policy, so a lookup by
    :func:`.buildings.building_key` of whatever the ad wrote hits.
    """
    rows = conn.execute(
        "SELECT key, policy, aliases_json FROM buildings WHERE status = 'confirmed'"
    ).fetchall()

    out: dict[str, str] = {}
    for row in rows:
        policy = row["policy"]
        if policy not in buildings.POLICIES or policy == "unknown":
            continue
        out[str(row["key"])] = str(policy)
        for alias in _json_list(row["aliases_json"]):
            alias_key = buildings.building_key(alias)
            if alias_key:
                out.setdefault(alias_key, str(policy))
    return out


def fetch_pending_buildings(
    conn: sqlite3.Connection,
    store: dict[str, dict[str, Any]] | None = None,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return the research queue: one row per building still to be looked up.

    A building qualifies through its listings, and only listings still worth
    keeping count. Who did the rejecting decides, the way it does in
    :func:`fetch_pending_photos`: a user verdict wins outright, and only where
    the user has said nothing does :func:`.rules.decide` get a vote. A condo
    whose every ad is already out is not worth a web search.

    Listings with an empty ``project_name`` cannot be researched — there is
    nothing to search for — and are dropped. Buildings already in ``store``
    are skipped unless ``refresh``, which is what keeps a second run cheap;
    an ad spelling a stored building differently is recognised through its
    aliases and collapses into that building's row rather than opening a new
    one.

    Each row carries ``key``, the display ``name`` (the spelling most of its
    ads use), ``area``, the listing count, up to :data:`QUEUE_SAMPLE_IDS`
    sample ``fb_ids`` and the distinct ``pets`` values those ads claimed —
    the last one so a contradiction between the ad and the house rule can be
    seen coming. Ordered by listing count descending: the building covering
    five listings is the one worth researching first.
    """
    if store is None:
        store = buildings.load_buildings()
    index = _building_index(store)

    rows = conn.execute(
        """
        SELECT l.*, a.data_json, a.photo_data_json, a.data_quality,
               d.status AS user_status
        FROM listings AS l
        JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        LEFT JOIN decisions AS d ON d.fb_id = l.fb_id
        ORDER BY l.last_seen_at DESC
        """
    ).fetchall()

    queue: dict[str, dict[str, Any]] = {}
    for row in rows:
        listing = dict(row)
        fields = {
            **fields_of(listing.pop("data_json", None)),
            **fields_of(listing.pop("photo_data_json", None)),
        }
        user_status = listing.pop("user_status", None)
        if user_status:
            if user_status == "rejected":
                continue
        else:
            status, _reason = rules.decide(
                fields,
                listing.get("price_thb"),
                listing.get("data_quality"),
                listing.get("district"),
            )
            if status == "rejected":
                continue

        name = str(rules.value_of(fields, "project_name") or "").strip()
        raw_key = buildings.building_key(name)
        if not raw_key:
            continue
        key = index.get(raw_key, raw_key)
        if key in store and not refresh:
            continue

        entry = queue.setdefault(
            key,
            {
                "key": key,
                "name": name,
                "area": None,
                "listings": 0,
                "fb_ids": [],
                "pets": [],
                "_names": {},
                "_areas": {},
            },
        )
        entry["listings"] += 1
        entry["_names"][name] = entry["_names"].get(name, 0) + 1
        area = str(rules.value_of(fields, "area") or "").strip() or (
            str(listing.get("district") or "").strip()
        )
        if area:
            entry["_areas"][area] = entry["_areas"].get(area, 0) + 1
        if len(entry["fb_ids"]) < QUEUE_SAMPLE_IDS:
            entry["fb_ids"].append(str(listing["fb_id"]))
        pets = str(rules.value_of(fields, "pets") or "").strip()
        if pets and pets not in entry["pets"]:
            entry["pets"].append(pets)

    out: list[dict[str, Any]] = []
    for entry in queue.values():
        # Ties keep the spelling of the most recently seen ad: rows arrive
        # newest first and `max` is stable on the first maximum it meets.
        entry["name"] = max(entry.pop("_names").items(), key=lambda kv: kv[1])[0]
        areas = entry.pop("_areas")
        entry["area"] = max(areas.items(), key=lambda kv: kv[1])[0] if areas else None
        entry["pets"] = sorted(entry["pets"])
        out.append(entry)

    out.sort(key=lambda e: (-e["listings"], e["key"]))
    return out


def fetch_enriched(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every listing joined with its AI analysis and user decision.

    ``data_json`` is unpacked into ``fields`` with ``photo_data_json`` merged
    on top of it — the gallery pass looked at the flat itself, so where it
    answered at all its answer wins over the text's. ``photo_keys`` lists what
    it filled and ``photo_overrides`` maps the keys where it contradicted a
    non-empty text value to that old value, so the export can flag them.

    ``ai_status`` / ``ai_reason`` and ``score`` are recomputed from the merged
    fields, the row's ``data_quality`` and its district by :mod:`.rules`, so a
    rule change takes effect without re-running the AI — the stored ``score``
    column is only what the last pass computed. A user decision always wins:
    ``status``, ``reason`` and ``decided_by`` reflect the ``decisions`` table
    whenever it has a row for the listing.
    """
    rows = conn.execute(
        """
        SELECT l.*,
               a.schema_version AS ai_schema_version,
               a.analyzed_at, a.source_hash AS ai_source_hash, a.model,
               a.verdict, a.comment, a.score, a.data_quality, a.data_json,
               a.photo_data_json, a.photo_comment, a.photo_model,
               a.photo_schema_version, a.photos_analyzed_at, a.photos_hash,
               d.status AS user_status, d.reason AS user_reason,
               d.rating AS user_rating, d.decided_at
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
        text_fields = fields_of(item.pop("data_json", None))
        photo_fields = fields_of(item.pop("photo_data_json", None))
        fields: dict[str, Any] = {**text_fields, **photo_fields}
        item["fields"] = fields
        item["photo_keys"] = sorted(photo_fields)
        item["photo_overrides"] = _photo_overrides(text_fields, photo_fields)
        item["analyzed"] = item.get("analyzed_at") is not None
        item["photo_analyzed"] = item.get("photos_analyzed_at") is not None

        if item["analyzed"]:
            ai_status, ai_reason = rules.decide(
                fields,
                item.get("price_thb"),
                item.get("data_quality"),
                item.get("district"),
            )
            item["score"] = rules.score(fields, item.get("district"))
        else:
            ai_status, ai_reason = "active", None
        item["ai_status"] = ai_status
        item["ai_reason"] = ai_reason

        user_status = item.get("user_status")
        item["rating"] = item.get("user_rating")
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
  #flt { border: 1px solid #b9c2d6; background: #fff; border-radius: 4px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #33405c; white-space: nowrap; }
  #flt:hover { background: #eef2fa; }
  #flt.on { background: #33405c; border-color: #33405c; color: #fff; }
  #preset { border: 1px solid #b9c2d6; background: #fff; border-radius: 4px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #33405c; white-space: nowrap; }
  #preset:hover { background: #eef2fa; }
  #preset.on { background: #137333; border-color: #137333; color: #fff; }
  #dec { border: 1px solid #e0c48a; background: #fffdf5; border-radius: 4px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #7a5c14; white-space: nowrap; }
  #dec:hover { background: #fdf6e3; }
  #dec.on { background: #8a6d00; border-color: #8a6d00; color: #fff; }
  #decisions { display: none; border: 1px solid #e8dcc0; border-radius: 4px; background: #fffdf5; padding: 0.6rem 0.75rem; margin-bottom: 0.6rem; }
  #decisions.on { display: block; }
  #dec-cmd { width: 100%; box-sizing: border-box; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; border: 1px solid #e0d6bd; border-radius: 3px; padding: 6px 8px; background: #fff; color: #333; resize: vertical; }
  #dec-copy, #dec-clear { border: 1px solid #ccd; background: #fff; border-radius: 4px; padding: 2px 10px; font-size: 11px; cursor: pointer; color: #33405c; }
  #dec-copy:hover, #dec-clear:hover { background: #eef2fa; }
  /* Панель фильтров: по одному блоку на колонку, вид блока выбирает
     filterSpec — диапазон для чисел и дат, чипы для перечислений, поле
     подстроки для всего остального. */
  #filters { display: none; border: 1px solid #dde; border-radius: 4px; background: #fbfcfe; padding: 0.6rem 0.75rem; margin-bottom: 0.6rem; }
  #filters.on { display: block; }
  .flt-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 0.5rem 0.9rem; }
  .flt { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .flt .name { font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; color: #55607a; }
  .flt.set .name { color: #1a73e8; font-weight: 600; }
  .flt input { border: 1px solid #ccd; border-radius: 3px; padding: 2px 5px; font-size: 11px; min-width: 0; }
  .flt .range { display: flex; gap: 4px; align-items: center; }
  .flt .range input { flex: 1 1 0; }
  .flt .chips { display: flex; flex-wrap: wrap; gap: 3px; max-height: 92px; overflow-y: auto; }
  .chip { border: 1px solid #ccd; background: #fff; border-radius: 999px; padding: 1px 8px; font-size: 11px; cursor: pointer; color: #444; }
  .chip:hover { background: #f2f5fb; }
  .chip.on { background: #1a73e8; border-color: #1a73e8; color: #fff; }
  .flt-tools { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.6rem; font-size: 11px; color: #666; }
  #flt-reset { border: 1px solid #ccd; background: #fff; border-radius: 4px; padding: 2px 10px; font-size: 11px; cursor: pointer; color: #33405c; }
  #flt-reset:hover { background: #eef2fa; }
  .legend { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; font-size: 11px; color: #555; margin-bottom: 0.6rem; }
  .legend span.sw { display: inline-block; width: 14px; height: 14px; border: 1px solid #bbb; border-radius: 3px; vertical-align: -3px; margin-right: 4px; }
  .legend .item { white-space: nowrap; }
  .wrap { overflow: auto; max-height: 78vh; border: 1px solid #dde; border-radius: 4px; }
  table.rt { border-collapse: separate; border-spacing: 0; white-space: nowrap; }
  .rt th, .rt td { border-bottom: 1px solid #eef0f4; border-right: 1px solid #f3f4f7; padding: 5px 8px; text-align: left; vertical-align: top; font-weight: normal; }
  .rt th { background: #f7f8fb; font-weight: 600; user-select: none; }
  .rt thead th.lbl { position: sticky; top: 0; z-index: 2; cursor: pointer; box-shadow: 0 1px 0 #dde; }
  .rt thead th.lbl:hover { background: #eef1f7; }
  .rt thead th.lbl.sorted { color: #1a73e8; }
  .rt thead th .so { font-size: 9px; margin-left: 3px; color: #1a73e8; }
  /* The only source worth a colour: the AI looked at photographs rather than
     at text. FB / AI / decision were three shades of the same pale grey, and
     the bands they drew pinned the column order to "whoever filled it" — the
     layout cost more than the distinction was worth. Where a single value
     came from is still on the cell itself, as the `fb` and `≠ текст` badges. */
  .rt th.src-photo, .rt td.src-photo { background: #f3faf8; }
  .rt td:first-child, .rt th:first-child { position: sticky; left: 0; z-index: 1; background: #fff; box-shadow: 1px 0 0 #dde; }
  .rt thead th:first-child { z-index: 4; background: #eef0f4; }
  .rt tbody tr:hover td { background: #f9fbff; }
  .id-list { display: flex; flex-direction: column; align-items: flex-start; gap: 1px; }
  .id-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #1a73e8; text-decoration: none; }
  .id-cell:hover { text-decoration: underline; }
  .id-row { display: flex; align-items: center; }
  .id-cell.dup-id { color: #6b7a99; }
  .dup-count { font-size: 9px; color: #6b3fa0; border: 1px solid #d8cfe8; border-radius: 3px; padding: 0 3px; margin-left: 4px; }
  .dq { font-size: 9px; color: #a50e0e; background: #fce8e6; border: 1px solid #f2c6c2; border-radius: 3px; padding: 0 3px; margin-left: 4px; cursor: help; }
  /* Long values are clipped so the table stays scannable; a click unfolds
     the cell in place, a second click folds it back. */
  .clip { display: inline-block; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; cursor: zoom-in; }
  .clip.w-verdict { max-width: 240px; }
  .clip.w-comment { max-width: 340px; color: #555; }
  .rt td.open .clip { max-width: 460px; white-space: normal; overflow: visible; text-overflow: clip; cursor: zoom-out; }
  .rt tbody td.open, .rt tbody tr:hover td.open { background: #fffdf3; }
  /* «Кто решил» на вкладке отказов: своих 23 среди 1083 машинных надо
     видеть, не читая, поэтому «вы» — чип, а «AI» — серый шёпот. */
  .who-you { background: #e8f0fe; color: #1a4fa0; border-radius: 3px; padding: 0 5px; font-weight: 600; }
  .who-ai { color: #9aa0a8; }
  .num { font-variant-numeric: tabular-nums; text-align: right; }
  .badge { font-size: 9px; color: #8a7fa8; border: 1px solid #d8cfe8; border-radius: 3px; padding: 0 3px; margin-left: 4px; vertical-align: 1px; }
  .badge.fb { color: #4a7099; border-color: #cbd9e8; }
  .badge.was { color: #1f6f68; border-color: #bfdcd6; cursor: help; }
  .c-good { background: #e6f4ea; color: #137333; }
  .c-warn { background: #fef7e0; color: #8a6d00; }
  .c-bad  { background: #fce8e6; color: #a50e0e; }
  .c-stale { background: repeating-linear-gradient(45deg, #eef1f6, #eef1f6 4px, #dbe2ee 4px, #dbe2ee 8px); color: #55607a; }
  .c-none { color: #c2c6cf; }
  .pend { font-weight: 600; background: #fff3d6; color: #8a6d00; }
  .rt tbody tr.cursor td { background: #eaf1fd; }
  .rt tbody tr.cursor td:first-child { box-shadow: inset 3px 0 0 #1a73e8, 1px 0 0 #dde; }
  tr.row-rejected td { opacity: 0.55; text-decoration: line-through; }
  tr.row-rejected td:first-child { border-left: 3px solid #d93025; opacity: 1; text-decoration: none; }
  tr.row-shortlist td:first-child { border-left: 3px solid #f9ab00; }
  tr.row-longlist td:first-child { border-left: 3px solid #4285f4; }
  .empty { padding: 1rem; color: #888; }
</style>
</head>
<body>
<h1>fb-rent — __ROW_COUNT__ listings</h1>
<div class="meta">Сгенерировано __GENERATED_AT__ · реестр полей v__SCHEMA_VERSION__ · сортировка — клик по заголовку, Shift+клик добавляет второй ключ · ID открывает объявление · наведите на ячейку, чтобы увидеть цитату-основание · клик по обрезанной ячейке разворачивает её целиком</div>
<div class="meta">Решения с клавиатуры: <b>↓ ↑</b> или <b>j k</b> — по строкам, <b>1</b>–<b>5</b> — своя оценка (1–3 отклоняют, 4 — в longlist, 5 — в shortlist), <b>r</b> — отклонить, <b>a</b> — вернуть в активные, <b>0</b> — снять. Накопленное лежит в браузере и переживает пересборку файла; кнопка «Решения» отдаёт готовый блок команд.</div>
<div class="bar">
  <button class="tab" data-tab="active"></button>
  <button class="tab" data-tab="shortlist"></button>
  <button class="tab" data-tab="longlist"></button>
  <button class="tab" data-tab="rejected"></button>
  <button class="tab" data-tab="all"></button>
  <input id="q" type="search" placeholder="поиск по всем колонкам…">
  <button id="flt"></button>
  <button id="preset"></button>
  <button id="dec"></button>
  <span class="spacer"></span>
  <button id="more"></button>
</div>
<div id="filters"><div class="flt-grid" id="flt-grid"></div>
  <div class="flt-tools"><button id="flt-reset">Сбросить фильтры</button><span id="flt-note"></span></div>
</div>
<div id="decisions">
  <textarea id="dec-cmd" rows="6" spellcheck="false" readonly></textarea>
  <div class="flt-tools">
    <button id="dec-copy">Скопировать</button>
    <button id="dec-clear">Очистить очередь</button>
    <span id="dec-note"></span>
  </div>
</div>
<div class="legend">
  <span class="item"><span class="sw src-photo"></span>колонка от фото-прохода — AI смотрел снимки, а не текст</span>
  <span class="item"><span class="dq">sidebar_noise</span>анализ читал не тот текст — строку надо перечитать</span>
  <span class="item"><span class="badge was">≠ текст</span>фото не согласилось с текстом — в подсказке прежнее значение</span>
  <span class="item"><span class="sw c-good"></span>подходит (pets allowed, цена в бюджете)</span>
  <span class="item"><span class="sw c-warn"></span>под вопросом (unclear, 30–35k ฿)</span>
  <span class="item"><span class="sw c-bad"></span>стоп-фактор (животные запрещены, вне бюджета)</span>
  <span class="item"><span class="sw c-stale"></span>поля не было на момент анализа — нужен ре-анализ</span>
  <span class="item"><span class="sw c-none"></span>AI смотрел, информации нет</span>
  <span class="item"><span class="pend" style="padding:0 4px;border-radius:3px">4</span>решение принято, но ещё не записано</span>
  <span class="item"><span class="sw" style="border-left:3px solid #d93025"></span>row-rejected — отклонено (AI или вами)</span>
  <span class="item"><span class="sw" style="border-left:3px solid #4285f4"></span>row-longlist — посмотрели, средний вариант</span>
  <span class="item"><span class="who-you">вы</span>решение ваше, а не правил — на вкладке Rejected это ещё и фильтр</span>
</div>
<div class="wrap"><table class="rt"><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div class="empty" id="empty" hidden>Ничего не найдено.</div>
<script>
const COLUMNS = __COLUMNS_JSON__;
const DATA = __DATA_JSON__;

// Active — что ещё не разобрано, остальные три — что уже посмотрели.
const TAB_LABELS = {
  active: "Разобрать", shortlist: "Shortlist", longlist: "Longlist",
  rejected: "Rejected", all: "All"
};
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

/* ---- решения прямо из таблицы -------------------------------------------
   Записать решение — это `fb-rent rate <id> 4` в терминале, и переключение
   туда стоит достаточно, чтобы на тысячу активных строк пришлось три десятка
   решений. Клавиши копят решения здесь, в localStorage: он переживает
   `export`, который переписывает этот файл после каждой стадии, — а кнопка
   отдаёт накопленное одним блоком команд. Записывать прямо в decisions.json
   было бы удобнее, но за это пришлось бы отдать главное свойство файла: он
   открывается с диска, без сервера и без сети. */
const PENDING_KEY = "fb-rent:pending";
let PENDING = {};
// Строка под курсором и то, что сейчас на экране: клавиши ходят по видимому
// списку, а он зависит от вкладки, поиска и фильтров.
let cursorId = null;
let shownRows = [];

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* Колонка с `tabs` живёт только на перечисленных вкладках: `Причина` пишется
   отказом и ничем больше, `Кто решил` на вкладках, где решали только вы, был
   бы столбцом одинаковых слов. На остальных вкладках это столбец прочерков
   высотой во всю таблицу. */
function colInTab(col) {
  return !col.tabs || col.tabs.indexOf(tab) !== -1;
}

function visibleColumns() {
  return COLUMNS.filter(c => (showExtra || !c.hidden) && colInTab(c));
}

function cellOf(row, key) {
  return (row.cells && row.cells[key]) || { t: "", c: "" };
}

function haystack(row) {
  if (row._hay == null) {
    const id = cellOf(row, "fb_id");
    const dups = (id.dups || []).map(d => d.fb_id).join(" ");
    row._hay = (COLUMNS.map(c => cellOf(row, c.key).t).join(" ")
      + " " + dups + " " + (id.dq || "")).toLowerCase();
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
  if (cell.c) classes.push(cell.c);
  if (col.kind === "num" || col.kind === "score" || col.kind === "count" || col.kind === "rating") classes.push("num");
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
      // `data_quality` is a badge rather than a column: it was `good` on 99%
      // of the table, and a whole column to flag 27 rows is a bad trade.
      const dq = (i === 0 && cell.dq)
        ? '<span class="dq" title="качество данных: ' + esc(cell.dq)
          + ' — анализ читал не тот текст, строку надо перечитать">' + esc(cell.dq) + '</span>'
        : "";
      return '<span class="id-row">' + link + count + dq + '</span>';
    }).join("") + '</span>';
  } else if (col.kind === "score") {
    inner = empty ? '—' : esc(text);
    if (!empty && cell.n != null) style = scoreStyle(cell.n);
  } else if (col.kind === "rating") {
    // Ваша оценка 1–5: пусто значит «ещё не смотрели». Решение, принятое в
    // этой сессии, но ещё не записанное, показывается здесь же — жёлтым,
    // чтобы его нельзя было спутать с уже сохранённым.
    const p = PENDING[row.fb_id];
    if (p) {
      inner = '<span class="pend">' + esc(pendingLabel(p)) + '</span>';
      title = ' title="решение ещё не записано — нажмите «Решения» и выполните команды в терминале"';
    } else {
      inner = empty ? '—' : esc(text);
      if (!empty && cell.n != null) style = countStyle(cell.n, 5);
    }
  } else if (col.kind === "count") {
    inner = empty ? '—' : esc(text);
    if (!empty && cell.n != null) style = countStyle(cell.n, col.key === "bedrooms" ? 4 : 3);
  } else if (col.kind === "type") {
    inner = empty ? '—' : (TYPE_ICONS[text] || "🏚️") + " " + esc(text);
  } else if (col.kind === "who") {
    // Кто вынес вердикт. Раньше это был суффикс «(вы)» в хвосте «Причины» —
    // невидимый для поиска (haystack собирается из cell.t) и неотбираемый
    // фильтром, а отделить свои отказы от машинных нужно ровно на той
    // вкладке, где их больше тысячи.
    inner = empty ? '—'
      : '<span class="who-' + (text === "вы" ? "you" : "ai") + '">' + esc(text) + '</span>';
  } else {
    inner = empty ? '—' : esc(text);
  }

  // A merged column (price, district) shows whichever source had the better
  // value; the badge says which one that was.
  if (!empty && cell.src && cell.src !== col.src) {
    inner += '<span class="badge ' + esc(cell.src) + '">' + esc(cell.src) + '</span>';
  }

  // The photo pass overruled a non-empty text value; the badge keeps the
  // overruled reading one hover away instead of dropping it silently.
  if (!empty && cell.was) {
    inner += '<span class="badge was" title="' + esc(cell.was) + '">\u2260 текст</span>';
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
  const cls = [];
  if (row.status === "rejected") cls.push("row-rejected");
  else if (row.status === "shortlist") cls.push("row-shortlist");
  else if (row.status === "longlist") cls.push("row-longlist");
  if (row.fb_id === cursorId) cls.push("cursor");
  return '<tr class="' + cls.join(" ") + '" data-row="' + esc(row.fb_id) + '">'
    + visibleColumns().map(c => tdHTML(row, c)).join("") + '</tr>';
}

/* ---- пресет «Кандидаты» --------------------------------------------------
   requirements.md называет жёсткие условия словами, а в панели они ставятся
   по одному руками — и поэтому не ставятся: из 1123 активных строк набор,
   отвечающий письму агенту, приходится собирать четырьмя движениями. Кнопка
   ставит их разом и оставляет видимыми: это пресет, а не отдельный режим, так
   что любое условие снимается по одному, не унося остальные.

   `спросить` из набора не выкидывается намеренно — это строки, где про
   животных в объявлении просто не написано, а по требованиям молчание это
   вопрос агенту, а не отказ. Снять этот чип — одно нажатие.

   Одна оговорка, которую стоит знать: фильтр «от» отбрасывает пустые ячейки,
   так что строки, где район не назван вовсе, в набор не попадают — хотя
   правила их за это не наказывают. */
function presetFilters() {
  return {
    verdict: { kind: "enum", values: new Set(["смотреть", "спросить"]) },
    area_rating: { kind: "range", min: 4, max: null },
    price: { kind: "range", min: null, max: 30000 },
  };
}
let presetOn = false;

function togglePreset() {
  const turningOn = !presetOn;
  Object.keys(FILTERS).forEach(k => delete FILTERS[k]);
  if (turningOn) {
    Object.assign(FILTERS, presetFilters());
    document.getElementById("filters").classList.add("on");
  }
  presetOn = turningOn;
  renderFilters();
  render();
}

function loadPending() {
  try { PENDING = JSON.parse(localStorage.getItem(PENDING_KEY) || "{}"); }
  catch (e) { PENDING = {}; }
  // Решение, доехавшее до decisions.json, уходит из очереди само — иначе
  // список команд рос бы вечно и каждый прогон повторял бы прошлые.
  const byId = {};
  DATA.forEach(r => { byId[r.fb_id] = r; });
  Object.keys(PENDING).forEach(id => {
    const row = byId[id];
    if (!row || alreadyRecorded(row, PENDING[id])) delete PENDING[id];
  });
  savePending();
}

function alreadyRecorded(row, p) {
  if (row.decided_by !== "user") return false;
  if (p.kind === "rate") return cellOf(row, "rating").n === p.v;
  return row.status === p.kind;
}

function savePending() {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(PENDING)); }
  catch (e) { /* приватное окно — очередь живёт до перезагрузки */ }
}

function setDecision(id, p) {
  if (p) PENDING[id] = p; else delete PENDING[id];
  savePending();
  render();
}

function pendingLabel(p) {
  if (p.kind === "rate") return String(p.v);
  return p.kind === "rejected" ? "reject" : "active";
}

function commandText() {
  const ids = Object.keys(PENDING);
  if (!ids.length) {
    return "Очередь пуста. Встаньте на строку (↓ ↑ или клик) и нажмите 1–5, r или a.";
  }
  return ids.map(id => {
    const p = PENDING[id];
    if (p.kind === "rate") return "fb-rent rate " + id + " " + p.v;
    return (p.kind === "rejected" ? "fb-rent reject " : "fb-rent unreject ") + id;
  }).join("\n") + "\nfb-rent export";
}

function moveCursor(step) {
  if (!shownRows.length) return;
  let i = shownRows.findIndex(r => r.fb_id === cursorId);
  i = i === -1 ? (step > 0 ? 0 : shownRows.length - 1) : i + step;
  cursorId = shownRows[Math.max(0, Math.min(shownRows.length - 1, i))].fb_id;
  render();
  const tr = document.querySelector("tr.cursor");
  if (tr) tr.scrollIntoView({ block: "nearest" });
}

let tab = "active";
// Таблицу открывают, чтобы посмотреть лучшее, поэтому она сразу отсортирована
// по Score вниз. Строки без анализа уходят в конец: sortValue отдаёт для них
// null, а null в компараторе всегда последний, независимо от направления.
// Ключей может быть несколько (Shift+клик по заголовку): порядок в массиве —
// это и есть приоритет, первый ключ решает, остальные разбирают равенство.
let sorts = [{ key: "score", dir: -1 }];

function sortValue(row, key) {
  const cell = cellOf(row, key);
  if (cell.n != null) return cell.n;
  const t = cell.t;
  if (t == null || t === "") return null;
  const n = Number(String(t).replace(/[\s ]/g, ""));
  return Number.isFinite(n) ? n : String(t).toLowerCase();
}

function compareRows(a, b) {
  for (const s of sorts) {
    const x = sortValue(a, s.key), y = sortValue(b, s.key);
    if (x === null && y === null) continue;
    // Пустая ячейка всегда внизу — иначе разворот сортировки поднимал бы
    // непроанализированные строки на первый экран.
    if (x === null) return 1;
    if (y === null) return -1;
    const r = (typeof x === "number" && typeof y === "number")
      ? x - y
      : String(x).localeCompare(String(y), "ru");
    if (r) return r * s.dir;
  }
  return 0;
}

/* ---- фильтры по колонкам ------------------------------------------------
   FILTERS: ключ колонки → {kind, ...}. Пустой фильтр из объекта удаляется,
   поэтому Object.keys(FILTERS).length — это счётчик на кнопке. */
const FILTERS = {};
const NUM_KINDS = { num: 1, score: 1, count: 1, rating: 1 };
// Чипы даёт только колонка, значения которой и пересчитываемы, и коротки:
// 55 районов набором кнопок читаются, 34 варианта "чем укомплектована кухня"
// длиной в предложение — уже нет, там нужна подстрока.
const ENUM_MAX = 60;
const ENUM_MAX_LEN = 30;
// Даты приходят как обычный текст, но набором кнопок читаются только пока
// корпус собран за неделю: на трёх месяцах чипы бесполезны, а «от/до» нет.
const DATE_COLUMNS = { first_seen_at: 1, published_at: 1 };
const specCache = {};

function numOf(cell) {
  if (cell.n != null) return cell.n;
  const t = cell.t;
  if (t == null || t === "") return null;
  const n = Number(String(t).replace(/[\s ]/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

// Вид фильтра выводится из данных, а не задаётся в реестре: колонка с
// коротким набором значений (pets, тип жилья, район) получает чипы, дата —
// два календаря, число — «от/до», всё остальное — поиск подстроки.
function filterSpec(col) {
  if (specCache[col.key]) return specCache[col.key];
  let spec;
  if (NUM_KINDS[col.kind]) {
    spec = { kind: "range" };
  } else if (col.kind === "date" || DATE_COLUMNS[col.key]) {
    spec = { kind: "date" };
  } else if (col.kind === "id") {
    spec = { kind: "text" };
  } else {
    const values = new Set();
    let hasEmpty = false;
    let enumerable = true;
    for (const row of DATA) {
      const t = cellOf(row, col.key).t;
      if (t == null || t === "") { hasEmpty = true; continue; }
      const s = String(t);
      if (s.length > ENUM_MAX_LEN) { enumerable = false; break; }
      values.add(s);
      if (values.size > ENUM_MAX) { enumerable = false; break; }
    }
    spec = enumerable
      ? {
          kind: "enum",
          values: Array.from(values).sort((a, b) => a.localeCompare(b, "ru")),
          hasEmpty: hasEmpty,
        }
      : { kind: "text" };
  }
  specCache[col.key] = spec;
  return spec;
}

function cellText(row, key) {
  const cell = cellOf(row, key);
  const dups = (cell.dups || []).map(d => d.fb_id).join(" ");
  return (String(cell.t == null ? "" : cell.t) + " " + dups).toLowerCase();
}

function passesFilters(row) {
  for (const key in FILTERS) {
    const f = FILTERS[key];
    const cell = cellOf(row, key);
    if (f.kind === "range") {
      const n = numOf(cell);
      if (n === null) return false;
      if (f.min !== null && n < f.min) return false;
      if (f.max !== null && n > f.max) return false;
    } else if (f.kind === "date") {
      const t = cell.t ? String(cell.t) : "";
      if (!t) return false;
      if (f.min && t < f.min) return false;
      if (f.max && t > f.max) return false;
    } else if (f.kind === "enum") {
      if (!f.values.has(cell.t == null ? "" : String(cell.t))) return false;
    } else if (f.kind === "text") {
      if (cellText(row, key).indexOf(f.q) === -1) return false;
    }
  }
  return true;
}

function dropIfEmpty(key) {
  const f = FILTERS[key];
  if (!f) return;
  const empty = f.kind === "enum"
    ? f.values.size === 0
    : (f.kind === "text" ? !f.q : (f.min == null || f.min === "") && (f.max == null || f.max === ""));
  if (empty) delete FILTERS[key];
}

// Панель показывает фильтры видимых колонок плюс те, что уже выставлены на
// скрытых: иначе спрятанный фильтр молча резал бы таблицу.
function filterColumns() {
  return COLUMNS.filter(c =>
    (showExtra || !c.hidden || FILTERS[c.key])
    && (colInTab(c) || FILTERS[c.key]));
}

function renderFilters() {
  const grid = document.getElementById("flt-grid");
  grid.innerHTML = filterColumns().map(col => {
    const spec = filterSpec(col);
    const f = FILTERS[col.key];
    let body;
    if (spec.kind === "range" || spec.kind === "date") {
      const type = spec.kind === "date" ? "date" : "number";
      const mn = f && f.min != null ? f.min : "";
      const mx = f && f.max != null ? f.max : "";
      body = '<span class="range">'
        + '<input type="' + type + '" data-key="' + esc(col.key) + '" data-bound="min"'
        + ' placeholder="от" value="' + esc(mn) + '">'
        + '<input type="' + type + '" data-key="' + esc(col.key) + '" data-bound="max"'
        + ' placeholder="до" value="' + esc(mx) + '">'
        + '</span>';
    } else if (spec.kind === "enum") {
      const opts = spec.values.map(v => ({ v: v, label: v }));
      if (spec.hasEmpty) opts.push({ v: "", label: "—" });
      body = '<span class="chips">' + opts.map(o =>
        '<button class="chip' + (f && f.values.has(o.v) ? " on" : "") + '"'
        + ' data-key="' + esc(col.key) + '" data-value="' + esc(o.v) + '">'
        + esc(o.label) + '</button>'
      ).join("") + '</span>';
    } else {
      body = '<input type="search" data-key="' + esc(col.key) + '" data-bound="q"'
        + ' placeholder="подстрока…" value="' + esc(f ? f.raw : "") + '">';
    }
    return '<span class="flt' + (f ? " set" : "") + '" data-col="' + esc(col.key) + '">'
      + '<span class="name">' + esc(col.label) + '</span>' + body + '</span>';
  }).join("");
}

function renderHead() {
  const cols = visibleColumns();
  document.getElementById("head").innerHTML = cols.map(c => {
    const i = sorts.findIndex(s => s.key === c.key);
    // Номер рядом со стрелкой появляется только когда ключей больше одного —
    // тогда он и означает приоритет.
    const mark = i === -1 ? "" : '<span class="so">' + (sorts[i].dir > 0 ? "▲" : "▼")
      + (sorts.length > 1 ? (i + 1) : "") + '</span>';
    return '<th class="lbl src-' + c.src
      + (i === -1 ? '' : ' sorted') + '" data-key="' + esc(c.key) + '"'
      + ' title="клик — сортировать, Shift+клик — добавить ключ">'
      + esc(c.label) + mark + '</th>';
  }).join("");

  document.querySelectorAll("#head th").forEach(th => {
    th.addEventListener("click", ev => {
      const key = th.dataset.key;
      const i = sorts.findIndex(s => s.key === key);
      if (ev.shiftKey) {
        // Shift проводит ключ по кругу: вверх → вниз → выбыл из сортировки.
        if (i === -1) sorts.push({ key: key, dir: 1 });
        else if (sorts[i].dir === 1) sorts[i].dir = -1;
        else sorts.splice(i, 1);
        if (!sorts.length) sorts = [{ key: key, dir: 1 }];
      } else {
        sorts = [{ key: key, dir: i === 0 ? -sorts[0].dir : 1 }];
      }
      render();
    });
  });
}

function render() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  let rows = DATA.filter(r =>
    inTab(r, tab) && (!q || haystack(r).indexOf(q) !== -1) && passesFilters(r));

  if (sorts.length) rows = rows.slice().sort(compareRows);
  shownRows = rows;
  if (cursorId && !rows.some(r => r.fb_id === cursorId)) cursorId = null;

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

  const set = Object.keys(FILTERS).length;
  const flt = document.getElementById("flt");
  flt.textContent = set ? "Фильтры (" + set + ")" : "Фильтры";
  flt.classList.toggle("on", set > 0);
  const inTabCount = DATA.filter(r => inTab(r, tab)).length;
  document.getElementById("flt-note").textContent =
    "показано " + rows.length + " из " + inTabCount + " во вкладке";

  const preset = document.getElementById("preset");
  preset.textContent = presetOn ? "✓ Кандидаты" : "Кандидаты";
  preset.classList.toggle("on", presetOn);

  const queued = Object.keys(PENDING).length;
  const dec = document.getElementById("dec");
  dec.textContent = queued ? "Решения (" + queued + ")" : "Решения";
  dec.classList.toggle("on", queued > 0);
  document.getElementById("dec-cmd").value = commandText();
  document.getElementById("dec-note").textContent = queued
    ? "выполните в терминале — записанное уйдёт из очереди при следующем export"
    : "";
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    tab = btn.dataset.tab;
    renderFilters();
    render();
  });
});

document.getElementById("body").addEventListener("click", ev => {
  // The ID cell is a link to the listing — let it navigate.
  if (ev.target.closest("a")) return;
  const tr = ev.target.closest("tr");
  if (tr && tr.dataset.row !== cursorId) {
    cursorId = tr.dataset.row;
    document.querySelectorAll("#body tr.cursor").forEach(r => r.classList.remove("cursor"));
    tr.classList.add("cursor");
  }
  const td = ev.target.closest("td");
  if (!td || !td.querySelector(".clip")) return;
  const id = td.dataset.cell;
  if (openCells.has(id)) openCells.delete(id);
  else openCells.add(id);
  td.classList.toggle("open");
});

document.getElementById("more").addEventListener("click", () => {
  showExtra = !showExtra;
  renderFilters();
  render();
});

document.getElementById("q").addEventListener("input", render);

document.getElementById("flt").addEventListener("click", () => {
  document.getElementById("filters").classList.toggle("on");
});

// Чипы перечислений. Панель не перерисовывается целиком — иначе фокус и
// каретка в соседнем поле ввода терялись бы на каждом нажатии.
document.getElementById("flt-grid").addEventListener("click", ev => {
  const chip = ev.target.closest(".chip");
  if (!chip) return;
  const key = chip.dataset.key, value = chip.dataset.value;
  const f = FILTERS[key] || (FILTERS[key] = { kind: "enum", values: new Set() });
  if (f.values.has(value)) f.values.delete(value);
  else f.values.add(value);
  chip.classList.toggle("on", f.values.has(value));
  dropIfEmpty(key);
  markSet(key);
  presetOn = false;
  render();
});

document.getElementById("flt-grid").addEventListener("input", ev => {
  const input = ev.target.closest("input");
  if (!input) return;
  const key = input.dataset.key, bound = input.dataset.bound;
  const raw = input.value.trim();
  if (bound === "q") {
    if (raw) FILTERS[key] = { kind: "text", q: raw.toLowerCase(), raw: raw };
    else delete FILTERS[key];
  } else {
    const spec = specCache[key];
    const kind = spec && spec.kind === "date" ? "date" : "range";
    const f = FILTERS[key] && FILTERS[key].kind === kind
      ? FILTERS[key]
      : (FILTERS[key] = { kind: kind, min: null, max: null });
    let parsed = null;
    if (raw !== "") {
      parsed = kind === "date" ? raw : Number(raw.replace(",", "."));
      if (kind === "range" && !Number.isFinite(parsed)) parsed = null;
    }
    f[bound] = parsed;
    dropIfEmpty(key);
  }
  markSet(key);
  presetOn = false;
  render();
});

function markSet(key) {
  const block = document.querySelector('.flt[data-col="' + key + '"]');
  if (block) block.classList.toggle("set", !!FILTERS[key]);
}

document.getElementById("flt-reset").addEventListener("click", () => {
  Object.keys(FILTERS).forEach(k => delete FILTERS[k]);
  presetOn = false;
  renderFilters();
  render();
});

document.getElementById("preset").addEventListener("click", togglePreset);

document.getElementById("dec").addEventListener("click", () => {
  document.getElementById("decisions").classList.toggle("on");
});

document.getElementById("dec-copy").addEventListener("click", () => {
  const box = document.getElementById("dec-cmd");
  const note = document.getElementById("dec-note");
  // `navigator.clipboard` не везде работает на file://, поэтому выделение
  // текстового поля остаётся запасным путём — и заодно видимым: даже если
  // копирование запретят, блок уже выделен и его можно взять руками.
  box.select();
  const done = () => { note.textContent = "скопировано"; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(box.value).then(done, () => {
      note.textContent = document.execCommand("copy")
        ? "скопировано" : "выделено — скопируйте вручную";
    });
  } else {
    note.textContent = document.execCommand("copy")
      ? "скопировано" : "выделено — скопируйте вручную";
  }
});

document.getElementById("dec-clear").addEventListener("click", () => {
  PENDING = {};
  savePending();
  render();
});

/* Клавиши работают по строке под курсором. Поля ввода их не видят — иначе
   «r» в поиске отклоняло бы объявление. */
document.addEventListener("keydown", ev => {
  const target = ev.target;
  if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA"
      || target.isContentEditable)) return;
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

  const key = ev.key;
  if (key === "ArrowDown" || key === "j") { ev.preventDefault(); moveCursor(1); return; }
  if (key === "ArrowUp" || key === "k") { ev.preventDefault(); moveCursor(-1); return; }
  if (!cursorId) return;

  if (key >= "1" && key <= "5") setDecision(cursorId, { kind: "rate", v: Number(key) });
  else if (key === "r") setDecision(cursorId, { kind: "rejected" });
  else if (key === "a") setDecision(cursorId, { kind: "active" });
  else if (key === "0" || key === "Backspace") setDecision(cursorId, null);
  else return;

  ev.preventDefault();
  // Оценил — поехали дальше: без этого каждая строка стоит двух нажатий.
  moveCursor(1);
});

loadPending();
renderFilters();
render();
</script>
</body>
</html>
"""

# Columns that come from the listing row itself rather than from the AI field
# registry. ``kind`` is what the embedded JS switches on when rendering a cell.
# ``src`` no longer draws bands across the header — it only survives so the
# photo columns keep their green tint, which is the one source distinction
# worth a colour: the AI looked at pictures rather than at text. The FB / AI /
# sys tints were three shades of the same pale grey, and the bands they drew
# pinned the column order to "whoever filled it", which is what put 600px of
# prose between the price and the number of bedrooms.
_FIXED_COLUMNS: dict[str, dict[str, Any]] = {
    "fb_id": {"label": "ID", "kind": "id", "src": "fb"},
    "price": {"label": "Цена ฿/мес", "kind": "num", "src": "fb"},
    "area": {"label": "Район", "kind": "text", "src": "fb"},
    # When the ad itself was posted, not when a sweep first saw it. A corpus
    # collected over three days has three `first_seen_at` values and tells you
    # nothing; `published_at` is filled on 98% of rows and is what says whether
    # the flat is likely already gone.
    "published_at": {"label": "Опубликовано", "kind": "text", "src": "fb"},
    "first_seen_at": {"label": "Первый показ", "kind": "text", "src": "fb"},
    "score": {"label": "Score", "kind": "score", "src": "ai"},
    # The district on the 1-5 scale the score already uses. It is not a column
    # anyone reads row by row — it exists so "хороший район" is expressible as
    # a filter and a sort key, which free-text `Район` never was.
    "area_rating": {"label": "Оценка района", "kind": "num", "src": "ai"},
    # The AI's recommendation, reduced to its tag. The sentence it wrote is
    # the tooltip, so nothing is lost and the column costs one word.
    "verdict": {"label": "Рекомендация", "kind": "tag", "src": "ai"},
    "comment": {"label": "Комментарий AI", "kind": "comment", "src": "ai"},
    "photo_comment": {
        "label": "Комментарий по фото", "kind": "comment", "src": "photo",
    },
    "rating": {"label": "Моя оценка", "kind": "rating", "src": "sys"},
    # Обе колонки живут только там, где им есть что показать: `tabs` —
    # список вкладок, на остальных колонки нет вовсе. Причину пишет отказ и
    # ничто больше; «Кто решил» на Longlist и Shortlist был бы столбцом,
    # где во всех строках стоит «вы».
    "decided_by": {
        "label": "Кто решил", "kind": "who", "src": "sys",
        "tabs": ["rejected", "all"],
    },
    "reason": {
        "label": "Причина", "kind": "reason", "src": "sys",
        "tabs": ["rejected", "all"],
    },
}

# The default view, in reading order. Everything countable comes first, so a
# glance down "цена → район → тип → спальни → площадь" never steps over a
# clipped paragraph; the text columns follow, where their length costs nothing.
# `decided_by` and `reason` close the table and the page drops them on the
# tabs where they are empty by construction.
_VISIBLE_ORDER: tuple[str, ...] = (
    "fb_id",
    "score",
    "pets",
    "price",
    "area",
    "property_type",
    "bedrooms",
    "bathrooms",
    "size_sqm",
    "published_at",
    "rating",
    "verdict",
    "comment",
    "photo_comment",
    "decided_by",
    "reason",
)

# Fixed columns that ship folded away. `first_seen_at` is ours, not the ad's —
# useful when a sweep is being debugged, noise the rest of the time; the
# district rating is machinery for the "Кандидаты" preset rather than reading
# matter, but it stays a real column so the preset is an ordinary set of
# filters the user can see and adjust.
_HIDDEN_FIXED: tuple[str, ...] = ("area_rating", "first_seen_at")

# Registry fields the base columns already cover: `area` and the monthly price
# are merged with their parsed counterparts into a single column each.
_MERGED_FIELD_KEYS: frozenset[str] = frozenset({"price_month_thb", "area"})

# Order of the folded-away columns: the ones that most often decide a viewing
# come first, the rest keep the registry's own order.
_EXTRA_FIELD_ORDER: tuple[str, ...] = (
    "security",
    "deposit",
    "parking",
    "aircon",
    "kitchen",
    "furnished",
    "living_room",
)

def is_photo_field(field: dict[str, Any]) -> bool:
    """Whether the gallery pass is expected to fill this registry field."""
    return ai_schema.field_source(field) in ("photo", "both")


# `unclear` means the AI read the ad and the ad did not say — which is exactly
# what the grey dash already shows, and it fills five columns with a word that
# weighs as much on screen as a real answer. `pets` is the exception: not
# knowing whether the dog is welcome is itself the reason to call the agent.
_UNCLEAR_SPEAKS: frozenset[str] = frozenset({"pets"})

# Who a verdict came from, as the `Кто решил` cell reads it. The column is
# what makes "мои отказы" a filter chip: the AI auto-rejects outnumber the
# hand-made ones 47 to 1, and nothing else on the row separates them.
_DECIDER_LABELS: dict[str, str] = {"user": "вы", "ai": "AI"}

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
    known = set(_VISIBLE_ORDER) | _MERGED_FIELD_KEYS
    rest = [f["key"] for f in registry.fields if f["key"] not in known]
    priority = [k for k in _EXTRA_FIELD_ORDER if k in rest]
    return priority + [k for k in rest if k not in priority]


def html_columns(registry: ai_schema.Registry) -> list[dict[str, Any]]:
    """Return every export column: the default view first, the extras after.

    ``_VISIBLE_ORDER`` is the whole layout — it mixes fixed columns and
    registry fields freely, because the order that reads well and the order
    the pipeline filled things in are not the same order. Everything the
    registry holds that the visible list did not claim follows it with
    ``hidden`` set, so the page can fold it away, and the fixed columns in
    ``_HIDDEN_FIXED`` close the list.
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
            "src": "photo" if is_photo_field(field) else "ai",
            "group": field.get("group") or "",
            "hidden": hidden,
        }

    def fixed_column(key: str, *, hidden: bool) -> dict[str, Any]:
        return {"key": key, **_FIXED_COLUMNS[key], "hidden": hidden}

    columns: list[dict[str, Any]] = []
    for key in _VISIBLE_ORDER:
        if key in _FIXED_COLUMNS:
            columns.append(fixed_column(key, hidden=False))
        elif key in by_key:
            columns.append(field_column(key, hidden=False))
    columns += [field_column(key, hidden=True) for key in _extra_field_keys(registry)]
    columns += [fixed_column(key, hidden=True) for key in _HIDDEN_FIXED]
    return columns


# Tags that mean "something stands between this row and a phone call". The
# recommendation the AI gives most often is deliberately not among them: paint
# 64% of the tab and the colour stops pointing at anything.
_VERDICT_CLASSES: dict[str, str] = {
    "ask": rules.CLASS_WARN,
    "doubt": rules.CLASS_WARN,
    "reread": rules.CLASS_STALE,
    "no": rules.CLASS_BAD,
}


def _verdict_cell(item: dict[str, Any], analyzed: bool) -> dict[str, Any]:
    """The recommendation as a tag, with the AI's sentence in the tooltip."""
    sentence = item.get("verdict") or ""
    slug, label = rules.verdict_tag(sentence, item.get("data_quality"))
    cell: dict[str, Any] = {
        "t": label,
        "c": _VERDICT_CLASSES.get(slug, "") if analyzed else rules.CLASS_NONE,
        "tag": slug,
        # Sorting ascending puts what to look at first, which alphabetical
        # order of the labels would not.
        "n": rules.verdict_order(slug),
    }
    if sentence:
        cell["ev"] = sentence
    return cell


def _area_rating_cell(item: dict[str, Any]) -> dict[str, Any]:
    """The district on the 1-5 scale, or an empty cell if none was named."""
    rating = rules.area_rating(item.get("fields") or {}, item.get("district"))
    if rating is None:
        return {"t": "", "c": rules.CLASS_NONE}
    return {"t": f"{rating:g}", "c": rules.area_class(
        rules.value_of(item.get("fields") or {}, "area") or item.get("district")
    ), "n": float(rating)}


def html_row(item: dict[str, Any], registry: ai_schema.Registry) -> dict[str, Any]:
    """Turn one :func:`fetch_enriched` item into a row for the HTML payload.

    Every cell carries its text, its colour class from :func:`rules.cell_class`
    and — where the AI supplied one — the quote backing the value, which the
    page shows as a tooltip so any claim can be traced to the source text.
    Price and district exist twice in the pipeline (parsed off the card, then
    read out of the ad text); each is merged into one cell that prefers the
    AI's reading and records in ``src`` which one is on screen. A cell the
    photo pass overrode carries ``was`` — what the text had claimed.
    """
    analyzed = bool(item.get("analyzed"))
    fields = item.get("fields") or {}
    # An unanalysed row has no schema version of its own. Pretending it is
    # current keeps its blank cells plain grey: `c-stale` means "the field
    # arrived after this row was analysed", which says nothing here.
    version = (item.get("ai_schema_version") or 0) if analyzed else registry.schema_version
    # The photo band has its own clock. Until the gallery pass has run it
    # borrows the text pass's, so a `both` field added after the text analysis
    # still hatches — but once photos exist, only their version decides,
    # otherwise every field added by `ai-field add --source photo` would hatch
    # across the whole table.
    photo_analyzed = bool(item.get("photo_analyzed"))
    photo_version = (
        (item.get("photo_schema_version") or 0) if photo_analyzed else version
    )
    photo_overrides = item.get("photo_overrides") or {}

    status = item.get("status") or "active"
    decided_by = item.get("decided_by")
    reason = item.get("reason")
    rating = item.get("rating")
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
        # `data_quality` used to be a column of its own and was `good` on 99%
        # of the table — a whole column's width to flag 27 rows. It rides on
        # the ID instead, as a badge the page also folds into the search text.
        "fb_id": {
            "t": item.get("fb_id"),
            "c": "",
            "dups": item.get("duplicates") or [],
            "dq": (item.get("data_quality") or "")
            if analyzed and item.get("data_quality") not in (None, "", "good")
            else "",
        },
        "price": {
            "t": _display(price),
            "c": rules.cell_class("price_month_thb", price, {"added_in": 1}, version),
            "src": "ai" if not rules.is_empty(ai_price) else "fb",
        },
        "area": {
            "t": _display(area),
            "c": rules.area_class(area),
            "src": "ai" if not rules.is_empty(ai_area) else "fb",
        },
        "first_seen_at": {"t": str(first_seen)[:10], "c": ""},
        "published_at": {
            "t": str(item.get("published_at") or "")[:10],
            "c": "" if item.get("published_at") else rules.CLASS_NONE,
        },
        "score": {
            "t": _display(score),
            "c": "" if analyzed else rules.CLASS_NONE,
            "n": score if isinstance(score, int) else None,
        },
        "verdict": _verdict_cell(item, analyzed),
        "area_rating": _area_rating_cell(item),
        "comment": {
            "t": item.get("comment") or "",
            "c": "" if analyzed else rules.CLASS_NONE,
        },
        "photo_comment": {
            "t": item.get("photo_comment") or "",
            "c": "" if photo_analyzed else rules.CLASS_NONE,
        },
        "rating": {
            "t": f"{rating:g}" if isinstance(rating, (int, float)) else "",
            "c": "",
            "n": float(rating) if isinstance(rating, (int, float)) else None,
        },
        "decided_by": {"t": _DECIDER_LABELS.get(decided_by or "", ""), "c": ""},
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
        shown = None if value == "unclear" and key not in _UNCLEAR_SPEAKS else value
        cell: dict[str, Any] = {
            "t": _display(shown),
            "c": rules.cell_class(
                key,
                shown,
                field,
                photo_version if is_photo_field(field) else version,
            ),
        }
        if key in ("bedrooms", "bathrooms"):
            cell["n"] = value if isinstance(value, int) else None
        raw = fields.get(key)
        if isinstance(raw, dict) and raw.get("ev"):
            cell["ev"] = str(raw["ev"])
        if key in photo_overrides:
            # The photo disagreed with the text. Both readings stay visible:
            # the cell shows what the camera saw, the badge what was written.
            cell["was"] = "текст: " + (_display(photo_overrides[key]) or "—")
        cells[key] = cell

    return {
        "fb_id": item.get("fb_id"),
        "url": item.get("url"),
        "status": status,
        "decided_by": decided_by,
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
    reason and the `Кто решил` column, and the Разобрать / Shortlist /
    Longlist / Rejected / All tabs filter the set — the first holds what has
    not been looked at, the other three what has. Data is embedded inline, so the file opens straight
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
