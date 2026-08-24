"""SQLite storage layer for fb-rent listings.

The database is the source of truth; ``rentals.csv`` and ``rentals.html``
are flat exports regenerated from it. Deduplication is done on ``fb_id``
(the listing ID extracted from the marketplace URL).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>fb-rent listings</title>
<link href="https://unpkg.com/gridjs/dist/theme/mermaid.min.css" rel="stylesheet" />
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 1.5rem; color: #222; }
  h1 { margin: 0 0 0.25rem; font-size: 1.15rem; }
  .meta { color: #666; font-size: 0.85rem; margin-bottom: 1rem; }
  .desc { max-width: 42ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  th, td { vertical-align: top; }
</style>
</head>
<body>
<h1>fb-rent — __ROW_COUNT__ listings</h1>
<div class="meta">Generated __GENERATED_AT__ · sorted by last seen ↓ · click column headers to sort · search box top-right</div>
<div id="grid"></div>
<script src="https://unpkg.com/gridjs/dist/gridjs.umd.js"></script>
<script>
const DATA = __DATA_JSON__;
new gridjs.Grid({
  columns: [
    { name: "Price (THB)", sort: true, width: "110px" },
    { name: "Title" },
    { name: "BR", sort: true, width: "60px" },
    { name: "District", sort: true, width: "120px" },
    { name: "Type", sort: true, width: "100px" },
    { name: "Published", width: "130px" },
    { name: "Link", width: "70px",
      formatter: (c) => c ? gridjs.html('<a href="' + c + '" target="_blank" rel="noopener">open</a>') : "" },
    { name: "Description",
      formatter: (c) => {
        const s = c == null ? "" : String(c);
        const esc = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        return gridjs.html('<div class="desc" title="' + esc + '">' + esc + '</div>');
      } },
    { name: "First seen", sort: true, width: "170px" },
    { name: "Last seen",  sort: true, width: "170px" },
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


def export_html(conn: sqlite3.Connection, path: Path | str) -> Path:
    """Regenerate a self-contained HTML export from the database.

    Data is embedded as inline JSON; sorting/search/pagination are provided
    by Grid.js loaded from a CDN. Open the file directly in a browser — no
    local server required.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    query = f"SELECT {', '.join(CSV_COLUMNS)} FROM listings ORDER BY last_seen_at DESC"
    rows = conn.execute(query).fetchall()
    data = [[row[c] for c in CSV_COLUMNS] for row in rows]

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
