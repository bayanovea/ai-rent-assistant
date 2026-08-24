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
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from . import ai_schema, rules


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
    score          INTEGER,
    data_quality   TEXT,
    data_json      TEXT NOT NULL
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
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    conn.execute(_DECISIONS_SCHEMA)
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

    Returns ``"inserted"`` or ``"updated"``. On update ``first_seen_at`` is
    preserved; ``last_seen_at``, ``price_*``, ``description``, ``raw_json``
    and other mutable fields are refreshed.
    """
    fb_id = listing.get("fb_id")
    if not fb_id:
        raise ValueError("listing must include 'fb_id'")

    ts = now or _now_iso()
    existing = conn.execute(
        "SELECT first_seen_at FROM listings WHERE fb_id = ?", (fb_id,)
    ).fetchone()

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
    ``source_hash`` and a ``pending_reason`` of ``new`` / ``changed`` /
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

        has_text = bool((listing.get("description") or "").strip()) or bool(
            text_lines_of(listing.get("raw_json"))
        )
        (pending if has_text else no_raw).append(listing)

    return PendingListings(pending=pending, no_raw=no_raw)


def fetch_enriched(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every listing joined with its AI analysis and user decision.

    ``data_json`` is unpacked into ``fields``; ``ai_status`` / ``ai_reason``
    are recomputed from those fields by :mod:`.rules`, so a rule change takes
    effect without re-running the AI. A user decision always wins: ``status``,
    ``reason`` and ``decided_by`` reflect the file-backed ``decisions`` table
    whenever it has a row for the listing.
    """
    rows = conn.execute(
        """
        SELECT l.*,
               a.schema_version AS ai_schema_version,
               a.analyzed_at, a.source_hash AS ai_source_hash, a.model,
               a.verdict, a.score, a.data_quality, a.data_json,
               d.status AS user_status, d.reason AS user_reason, d.decided_at
        FROM listings AS l
        LEFT JOIN ai_analysis AS a ON a.fb_id = l.fb_id
        LEFT JOIN decisions AS d ON d.fb_id = l.fb_id
        ORDER BY l.first_seen_at DESC
        """
    ).fetchall()

    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
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
            ai_status, ai_reason = rules.decide(fields, item.get("price_thb"))
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
<html lang="en">
<head>
<meta charset="utf-8">
<title>fb-rent listings</title>
<link href="https://unpkg.com/gridjs/dist/theme/mermaid.min.css" rel="stylesheet" />
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 1.5rem; color: #222; font-size: 12px; }
  h1 { margin: 0 0 0.25rem; font-size: 1rem; }
  .meta { color: #666; font-size: 0.75rem; margin-bottom: 1rem; }
  .gridjs-table, .gridjs-search-input { font-size: 12px; }
  .gridjs-td, .gridjs-th { padding: 6px 8px; }
  .id-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #1a73e8; text-decoration: none; }
  .id-cell:hover { text-decoration: underline; }
  .price-cell { font-variant-numeric: tabular-nums; padding: 2px 6px; border-radius: 3px; }
  .br-cell { padding: 2px 6px; border-radius: 3px; text-align: center; }
  .district-hot { background: #e6f4ea; color: #137333; padding: 2px 6px; border-radius: 3px; }
  th, td { vertical-align: top; }
</style>
</head>
<body>
<h1>fb-rent — __ROW_COUNT__ listings</h1>
<div class="meta">Generated __GENERATED_AT__ · sorted by first seen ↓ · click column headers to sort · click an ID to open the listing</div>
<div id="grid"></div>
<script src="https://unpkg.com/gridjs/dist/gridjs.umd.js"></script>
<script>
const DATA = __DATA_JSON__;

const RU_MONTHS = ["Января","Февраля","Марта","Апреля","Мая","Июня",
                   "Июля","Августа","Сентября","Октября","Ноября","Декабря"];

const TYPE_EMOJI = {
  villa:     "\u{1F3E1}",
  condo:     "\u{1F3E2}",
  townhouse: "\u{1F3D8}\u{FE0F}",
  house:     "\u{1F3E0}",
  apartment: "\u{1F3E2}",
  studio:    "\u{1F3E2}",
};

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function formatPrice(v) {
  if (v == null || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString("ru-RU").replace(/,/g, " ");
}

function formatDateRu(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.getDate() + " " + RU_MONTHS[d.getMonth()];
}

function formatType(t) {
  if (!t) return "";
  const key = String(t).toLowerCase();
  const emoji = TYPE_EMOJI[key];
  return emoji ? emoji + " " + t : t;
}

// Row layout: [fb_id, url, price_thb, bedrooms, district, property_type, first_seen_at].
const prices = DATA.map(r => Number(r[2])).filter(v => Number.isFinite(v) && v > 0);
const priceMin = prices.length ? Math.min(...prices) : 0;
const priceMax = prices.length ? Math.max(...prices) : 0;

const brs = DATA.map(r => Number(r[3])).filter(v => Number.isFinite(v) && v > 0);
const brMin = brs.length ? Math.min(...brs) : 0;
const brMax = brs.length ? Math.max(...brs) : 0;

function gradientBg(v, min, max) {
  if (v == null || !Number.isFinite(Number(v)) || max <= min) return "";
  const t = Math.max(0, Math.min(1, (Number(v) - min) / (max - min)));
  const hue = 120 * (1 - t);
  return "hsl(" + hue.toFixed(0) + ", 55%, 90%)";
}

function districtCell(d) {
  if (!d) return gridjs.html("");
  const isHot = /\b(bang|kathu)/i.test(d);
  const cls = isHot ? "district-hot" : "";
  return gridjs.html('<span class="' + cls + '">' + esc(d) + '</span>');
}

new gridjs.Grid({
  columns: [
    { name: "ID", width: "160px",
      formatter: (_c, row) => {
        const id = row.cells[0].data;
        const url = row.cells[1].data;
        const label = esc(id);
        if (!url) return gridjs.html('<span class="id-cell">' + label + '</span>');
        return gridjs.html(
          '<a class="id-cell" href="' + esc(url) + '" target="_blank" rel="noopener">' + label + '</a>'
        );
      } },
    { name: "URL", hidden: true },
    { name: "Price (THB)", sort: true, width: "110px",
      formatter: (c) => {
        const bg = gradientBg(c, priceMin, priceMax);
        const style = bg ? ' style="background:' + bg + '"' : "";
        return gridjs.html('<span class="price-cell"' + style + '>' + esc(formatPrice(c)) + '</span>');
      } },
    { name: "BR", sort: true, width: "60px",
      formatter: (c) => {
        if (c == null || c === "") return "";
        const bg = gradientBg(c, brMin, brMax);
        const style = bg ? ' style="background:' + bg + '"' : "";
        return gridjs.html('<span class="br-cell"' + style + '>' + esc(c) + '</span>');
      } },
    { name: "District", sort: true, width: "140px",
      formatter: districtCell },
    { name: "Type", sort: true, width: "130px",
      formatter: (c) => gridjs.html(esc(formatType(c))) },
    { name: "First seen", sort: true, width: "120px",
      formatter: (c) => esc(formatDateRu(c)) },
  ],
  data: DATA,
  search: true,
  sort: true,
  pagination: { limit: 50 },
  resizable: true,
}).render(document.getElementById("grid"));
</script>
</body>
</html>
"""

# Columns embedded in the HTML export (order matters — the JS template
# indexes into each row by position).
_HTML_COLUMNS: tuple[str, ...] = (
    "fb_id",
    "url",
    "price_thb",
    "bedrooms",
    "district",
    "property_type",
    "first_seen_at",
)


def export_html(conn: sqlite3.Connection, path: Path | str) -> Path:
    """Regenerate a self-contained HTML export from the database.

    Data is embedded as inline JSON; sorting/search/pagination are provided
    by Grid.js loaded from a CDN. Open the file directly in a browser — no
    local server required.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    query = (
        f"SELECT {', '.join(_HTML_COLUMNS)} FROM listings "
        "ORDER BY first_seen_at DESC"
    )
    rows = conn.execute(query).fetchall()
    data = [[row[c] for c in _HTML_COLUMNS] for row in rows]

    # Neutralise ``</script>`` and HTML comment starters inside string
    # values so a malicious listing cannot break out of the <script> block.
    data_json = (
        json.dumps(data, ensure_ascii=False, default=str)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )
    html = (
        _HTML_TEMPLATE
        .replace("__ROW_COUNT__", str(len(data)))
        .replace("__GENERATED_AT__", _now_iso())
        .replace("__DATA_JSON__", data_json)
    )
    out.write_text(html, encoding="utf-8")
    return out
