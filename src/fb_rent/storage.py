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
  .tab { border: 1px solid #ccd; background: #fff; border-radius: 999px; padding: 3px 12px; font-size: 12px; cursor: pointer; color: #333; }
  .tab:hover { background: #f2f5fb; }
  .tab.on { background: #1a73e8; border-color: #1a73e8; color: #fff; }
  #q { border: 1px solid #ccd; border-radius: 4px; padding: 4px 8px; font-size: 12px; min-width: 220px; }
  .legend { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; font-size: 11px; color: #555; margin-bottom: 0.6rem; }
  .legend span.sw { display: inline-block; width: 14px; height: 14px; border: 1px solid #bbb; border-radius: 3px; vertical-align: -3px; margin-right: 4px; }
  .legend .item { white-space: nowrap; }
  .wrap { overflow: auto; max-height: 78vh; border: 1px solid #dde; border-radius: 4px; }
  table.rt { border-collapse: separate; border-spacing: 0; white-space: nowrap; }
  .rt th, .rt td { border-bottom: 1px solid #eef0f4; border-right: 1px solid #f3f4f7; padding: 5px 8px; text-align: left; vertical-align: top; font-weight: normal; }
  .rt th { position: sticky; top: 0; z-index: 2; background: #f7f8fb; font-weight: 600; cursor: pointer; user-select: none; box-shadow: 0 1px 0 #dde; }
  .rt th:hover { background: #eef1f7; }
  .rt th .grp { display: block; font-weight: normal; color: #99a; font-size: 10px; }
  .rt td:first-child, .rt th:first-child { position: sticky; left: 0; z-index: 1; background: #fff; box-shadow: 1px 0 0 #dde; }
  .rt th:first-child { z-index: 3; background: #f7f8fb; }
  .rt tbody tr:hover td { background: #f9fbff; }
  .id-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: #1a73e8; text-decoration: none; }
  .id-cell:hover { text-decoration: underline; }
  .verdict, .title-cell { display: inline-block; max-width: 320px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; }
  .who { color: #888; font-size: 10px; }
  .num { font-variant-numeric: tabular-nums; }
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
<div class="meta">Сгенерировано __GENERATED_AT__ · реестр полей v__SCHEMA_VERSION__ · сортировка — клик по заголовку · ID открывает объявление · наведите на ячейку, чтобы увидеть цитату-основание</div>
<div class="bar">
  <button class="tab" data-tab="active"></button>
  <button class="tab" data-tab="shortlist"></button>
  <button class="tab" data-tab="rejected"></button>
  <button class="tab" data-tab="all"></button>
  <input id="q" type="search" placeholder="поиск по всем колонкам…">
</div>
<div class="legend">
  <span class="item"><span class="sw c-good"></span>подходит (pets allowed, цена в бюджете)</span>
  <span class="item"><span class="sw c-warn"></span>под вопросом (unclear, 30–35k ฿)</span>
  <span class="item"><span class="sw c-bad"></span>стоп-фактор (животные запрещены, вне бюджета)</span>
  <span class="item"><span class="sw c-stale"></span>поля не было на момент анализа — нужен ре-анализ</span>
  <span class="item"><span class="sw c-none"></span>AI смотрел, информации нет</span>
  <span class="item"><span class="sw" style="border-left:3px solid #d93025"></span>row-rejected — отклонено (AI или вами)</span>
</div>
<div class="wrap"><table class="rt"><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<div class="empty" id="empty" hidden>Ничего не найдено.</div>
<script>
const COLUMNS = __COLUMNS_JSON__;
const DATA = __DATA_JSON__;

const TAB_LABELS = { active: "Active", shortlist: "Shortlist", rejected: "Rejected", all: "All" };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function cellOf(row, key) {
  return (row.cells && row.cells[key]) || { t: "", c: "" };
}

function haystack(row) {
  if (row._hay == null) {
    row._hay = COLUMNS.map(c => cellOf(row, c.key).t).join(" ").toLowerCase();
  }
  return row._hay;
}

function inTab(row, tab) {
  return tab === "all" ? true : row.status === tab;
}

function tdHTML(row, col) {
  const cell = cellOf(row, col.key);
  const text = cell.t;
  const empty = text == null || text === "";
  const classes = ["cell"];
  if (cell.c) classes.push(cell.c);
  if (col.kind === "num") classes.push("num");
  const title = cell.ev ? ' title="' + esc(cell.ev) + '"' : "";

  let inner;
  if (col.kind === "id") {
    const label = esc(empty ? "—" : text);
    inner = row.url
      ? '<a class="id-cell" href="' + esc(row.url) + '" target="_blank" rel="noopener">' + label + '</a>'
      : '<span class="id-cell">' + label + '</span>';
  } else if (col.kind === "verdict" || col.kind === "title") {
    const cls = col.kind === "verdict" ? "verdict" : "title-cell";
    inner = empty ? '—' : '<span class="' + cls + '" title="' + esc(text) + '">' + esc(text) + '</span>';
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
  return '<td class="' + classes.join(" ") + '"' + title + '>' + inner + '</td>';
}

function rowHTML(row) {
  const cls = row.status === "rejected"
    ? "row-rejected"
    : (row.status === "shortlist" ? "row-shortlist" : "");
  return '<tr class="' + cls + '">' + COLUMNS.map(c => tdHTML(row, c)).join("") + '</tr>';
}

let tab = "active";
let sortKey = null;
let sortDir = 1;

function sortValue(row, key) {
  const t = cellOf(row, key).t;
  if (t == null || t === "") return null;
  const n = Number(String(t).replace(/[\s ]/g, ""));
  return Number.isFinite(n) ? n : String(t).toLowerCase();
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

  document.getElementById("body").innerHTML = rows.map(rowHTML).join("");
  document.getElementById("empty").hidden = rows.length > 0;

  document.querySelectorAll(".tab").forEach(btn => {
    const key = btn.dataset.tab;
    const n = DATA.filter(r => inTab(r, key)).length;
    btn.textContent = TAB_LABELS[key] + " (" + n + ")";
    btn.classList.toggle("on", key === tab);
  });
}

document.getElementById("head").innerHTML = COLUMNS.map(c => {
  const grp = c.group ? '<span class="grp">' + esc(c.group) + '</span>' : "";
  return '<th data-key="' + esc(c.key) + '">' + esc(c.label) + grp + '</th>';
}).join("");

document.querySelectorAll("#head th").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    sortDir = key === sortKey ? -sortDir : 1;
    sortKey = key;
    render();
  });
});

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => { tab = btn.dataset.tab; render(); });
});

document.getElementById("q").addEventListener("input", render);

render();
</script>
</body>
</html>
"""

# Columns that come from the listing row itself rather than from the AI field
# registry. ``kind`` is what the embedded JS switches on when rendering a cell.
_BASE_COLUMNS: tuple[dict[str, str], ...] = (
    {"key": "fb_id", "label": "ID", "kind": "id"},
    {"key": "title", "label": "Заголовок", "kind": "title"},
    {"key": "score", "label": "Score", "kind": "num"},
    {"key": "verdict", "label": "Вердикт", "kind": "verdict"},
    {"key": "reason", "label": "Reason", "kind": "reason"},
    {"key": "price_thb", "label": "Цена FB ฿", "kind": "num"},
    {"key": "district", "label": "Район FB", "kind": "text"},
    {"key": "data_quality", "label": "Качество данных", "kind": "text"},
    {"key": "first_seen_at", "label": "Первый показ", "kind": "text"},
)


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


def html_columns(registry: ai_schema.Registry) -> list[dict[str, Any]]:
    """Return every export column: the fixed ones plus the registry's fields."""
    columns: list[dict[str, Any]] = [dict(col) for col in _BASE_COLUMNS]
    for field in registry.fields:
        columns.append(
            {
                "key": field["key"],
                "label": field.get("label") or field["key"],
                "kind": "num" if field.get("type") == "int" else "field",
                "group": field.get("group") or "",
            }
        )
    return columns


def html_row(item: dict[str, Any], registry: ai_schema.Registry) -> dict[str, Any]:
    """Turn one :func:`fetch_enriched` item into a row for the HTML payload.

    Every cell carries its text, its colour class from :func:`rules.cell_class`
    and — where the AI supplied one — the quote backing the value, which the
    page shows as a tooltip so any claim can be traced to the source text.
    """
    analyzed = bool(item.get("analyzed"))
    fields = item.get("fields") or {}
    # An unanalysed row has no schema version of its own. Pretending it is
    # current keeps its blank cells plain grey: `c-stale` means "the field
    # arrived after this row was analysed", which says nothing here.
    version = (item.get("ai_schema_version") or 0) if analyzed else registry.schema_version

    status = item.get("status") or "active"
    reason = item.get("reason")
    price = item.get("price_thb")
    first_seen = item.get("first_seen_at") or ""

    cells: dict[str, dict[str, Any]] = {
        "fb_id": {"t": item.get("fb_id"), "c": ""},
        "title": {"t": item.get("title") or "", "c": ""},
        "score": {"t": _display(item.get("score")), "c": ""},
        "verdict": {
            "t": item.get("verdict") or "",
            "c": "" if analyzed else rules.CLASS_NONE,
        },
        "reason": {
            "t": reason or "",
            "c": rules.CLASS_BAD if status == "rejected" and reason else "",
        },
        "price_thb": {
            "t": _display(price),
            "c": rules.cell_class("price_month_thb", price, {"added_in": 1}, version),
        },
        "district": {"t": item.get("district") or "", "c": ""},
        "data_quality": {
            "t": item.get("data_quality") or "",
            "c": rules.CLASS_WARN
            if analyzed and item.get("data_quality") not in (None, "", "good")
            else "",
        },
        "first_seen_at": {"t": str(first_seen)[:10], "c": ""},
    }

    for field in registry.fields:
        key = field["key"]
        value = rules.value_of(fields, key)
        cell: dict[str, Any] = {
            "t": _display(value),
            "c": rules.cell_class(key, value, field, version),
        }
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
