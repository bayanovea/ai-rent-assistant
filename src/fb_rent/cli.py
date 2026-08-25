"""Command-line entry point for fb-rent.

Wires the browser, list scraper, detail scraper, extractors and SQLite
storage layers into three commands:

- ``fb-rent login`` — opens a headed Chromium via a persistent profile so
  the user can log into Facebook once. Cookies live in the profile and
  are reused by later headless scrape runs.
- ``fb-rent scrape`` — orchestrator: builds the marketplace search URL,
  scrapes the list, walks each item's detail page with a random 3–7 s
  jitter, extracts structured fields and upserts them into SQLite. Ends
  by regenerating the CSV and HTML exports.
- ``fb-rent export`` — regenerates the CSV and HTML exports from the
  current DB without touching the network. Useful for verification.

The ``ai-*`` commands drive the enrichment loop: ``ai-status`` reports what
is waiting, ``ai-batch`` writes the file the AI reads, ``ai-apply`` reads
back what it extracted, and ``ai-field`` evolves the registry. ``reject`` /
``unreject`` / ``shortlist`` record the user's own verdicts, which live in
``data/decisions.json`` and outrank anything the AI decided.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import click

from . import ai_batch, ai_schema, config, decisions, photos
from .browser import (
    LoginRequiredError,
    is_logged_in,
    launch_persistent,
    login_flow,
    start_playwright,
)
from .extractors import (
    clean_fb_title,
    extract_bedrooms,
    extract_property_type,
    parse_price,
)
from .scraper_detail import scrape_detail
from .scraper_list import scrape_list
from .storage import (
    dedupe_listings,
    duplicate_fb_ids,
    export_csv,
    export_html,
    fetch_pending,
    init_db,
    known_fb_ids,
    upsert_listing,
)
from .url_builder import build_marketplace_url


def _echo_err(msg: str) -> None:
    click.echo(msg, err=True)


def _merge_listing(list_card: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """Combine list-card fields with (optional) detail-page fields.

    Detail fields override list-card fields where both are present, since
    the detail page carries the fuller/fresher copy (e.g. full description
    vs. a truncated card snippet). The title is the exception: it is taken
    from the detail page only when the card gave us nothing usable.
    """
    merged: dict[str, Any] = dict(list_card)
    if detail:
        for key in ("description", "published_at", "raw_json"):
            if detail.get(key) is not None:
                merged[key] = detail[key]
        if detail.get("price_raw"):
            merged["price_raw"] = detail["price_raw"]
        # The card title wins when it survives `clean_fb_title` — it is the
        # seller's own wording. When it does not (a card carrying nothing but
        # "Недавно опубликовано"), the detail page's <title> is the only real
        # headline we have.
        if detail.get("title") and not clean_fb_title(merged.get("title")):
            merged["title"] = detail["title"]
    return merged


def _enrich(listing: dict[str, Any]) -> dict[str, Any]:
    """Run heuristic extractors and attach normalised fields."""
    listing["price_thb"] = parse_price(listing.get("price_raw"))
    listing["bedrooms"] = extract_bedrooms(
        listing.get("title"), listing.get("description")
    )
    listing["property_type"] = extract_property_type(
        listing.get("title"), listing.get("description")
    )
    return listing


@click.group()
@click.version_option(package_name="fb-rent")
def main() -> None:
    """FB Marketplace Phuket rentals scraper (MVP).

    Run ``fb-rent login`` once to authenticate, then use
    ``fb-rent scrape`` to pull fresh listings into the local SQLite DB.
    """


@main.command("login")
def login_cmd() -> None:
    """Open a browser window and wait for a manual Facebook login."""
    click.echo("Opening Facebook login in a browser window...")
    click.echo("Sign in (including 2FA if enabled), then leave the tab open.")
    playwright = start_playwright()
    context = None
    try:
        try:
            context = launch_persistent(playwright, headless=False)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)
        ok = login_flow(context)
        if ok:
            click.echo("Login detected. Session saved to persistent profile.")
        else:
            _echo_err("Timed out waiting for login. Re-run `fb-rent login`.")
            sys.exit(1)
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            playwright.stop()


def _download_photos(context: Any, detail: dict[str, Any], fb_id: str) -> None:
    """Pull the listing's gallery to disk, unless it is already there.

    A directory that already has photos is left alone: re-downloading would
    change ``photos_hash`` and push the listing back into the photo queue
    for pictures that did not actually change.
    """
    urls = (detail.get("raw_json") or {}).get("gallery_urls") or []
    if not urls:
        return
    if photos.photos_hash(fb_id) is not None:
        return
    photos.download_and_resize(context, urls, str(fb_id))


@main.command("scrape")
@click.option(
    "--max",
    "max_items",
    type=int,
    default=config.DEFAULT_MAX_ITEMS,
    show_default=True,
    help="Maximum number of list cards to collect.",
)
@click.option(
    "--days",
    "days",
    type=int,
    default=config.DEFAULT_DAYS_SINCE_LISTED,
    show_default=True,
    help="Filter to listings created in the last N days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be scraped/written without touching the DB.",
)
@click.option(
    "--skip-details",
    is_flag=True,
    default=False,
    help="Skip per-item detail pages (list-card fields only, no jitter).",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Re-scrape detail pages for listings already in the DB "
    "(by default, known fb_ids are skipped to save time).",
)
@click.option(
    "--no-photos",
    "no_photos",
    is_flag=True,
    default=False,
    help="Skip downloading listing photos (faster runs, no photo pass).",
)
def scrape_cmd(
    max_items: int,
    days: int,
    dry_run: bool,
    skip_details: bool,
    refresh: bool,
    no_photos: bool,
) -> None:
    """Scrape the marketplace and upsert results into the local SQLite DB."""
    url = build_marketplace_url(days_since_listed=days)
    click.echo(f"URL: {url}")
    click.echo(
        f"Options: max={max_items} days={days} "
        f"dry_run={dry_run} skip_details={skip_details} refresh={refresh} "
        f"no_photos={no_photos}"
    )

    config.ensure_home()
    conn = init_db(config.DB_PATH)
    try:
        seen_ids = set() if refresh else known_fb_ids(conn)
        # Reposts stay skipped even under --refresh: their ad text already has
        # a row, and fetching the detail page again would only re-prove it.
        dupe_ids = duplicate_fb_ids(conn)
        if seen_ids and not refresh:
            click.echo(
                f"Known listings in DB: {len(seen_ids)} "
                "(details will be skipped for these; use --refresh to override)."
            )

        playwright = start_playwright()
        context = None
        try:
            try:
                context = launch_persistent(playwright, headless=True)
            except Exception as exc:  # noqa: BLE001 — surface a friendly error
                _echo_err(f"Failed to launch browser: {exc}")
                sys.exit(2)

            if not is_logged_in(context):
                _echo_err(
                    "No active Facebook session. Run `fb-rent login` first."
                )
                sys.exit(3)

            try:
                cards = scrape_list(context, url, max_items=max_items)
            except LoginRequiredError as exc:
                _echo_err(str(exc))
                sys.exit(3)

            click.echo(f"Collected {len(cards)} list cards.")

            detail_cap = min(len(cards), config.DETAIL_REQUEST_CAP)
            if skip_details:
                detail_cap = 0

            enriched: list[dict[str, Any]] = []
            skipped_known = 0
            skipped_dupes = 0
            with click.progressbar(
                length=len(cards),
                label="Scraping listings",
                show_pos=True,
                show_eta=True,
                item_show_func=lambda c: str(c.get("fb_id")) if c else "",
            ) as bar:
                for idx, card in enumerate(cards):
                    if not card.get("url") or not card.get("title"):
                        click.echo(
                            f"  [{idx + 1}/{len(cards)}] skipping card missing url/title: "
                            f"fb_id={card.get('fb_id')}"
                        )
                        bar.update(1, card)
                        continue
                    if card["fb_id"] in dupe_ids:
                        skipped_dupes += 1
                        bar.update(1, card)
                        continue

                    detail: dict[str, Any] | None = None
                    already_known = card["fb_id"] in seen_ids
                    if already_known:
                        skipped_known += 1
                    elif idx < detail_cap:
                        time.sleep(
                            random.uniform(
                                config.DETAIL_DELAY_MIN_SEC,
                                config.DETAIL_DELAY_MAX_SEC,
                            )
                        )
                        try:
                            detail = scrape_detail(context, card["url"])
                        except LoginRequiredError as exc:
                            _echo_err(str(exc))
                            sys.exit(3)
                        if detail is None:
                            click.echo(
                                f"  [{idx + 1}/{len(cards)}] detail unavailable, "
                                f"keeping list-card data: {card['url']}"
                            )
                        elif not no_photos and not dry_run:
                            # Inside the jitter we already paid for — the
                            # CDN fetches add ~1–2 s and no extra pause.
                            _download_photos(context, detail, card["fb_id"])
                    merged = _merge_listing(card, detail)
                    enriched.append(_enrich(merged))
                    bar.update(1, card)

            if skipped_known:
                click.echo(
                    f"Skipped detail fetch for {skipped_known} listing(s) already in DB."
                )
            if skipped_dupes:
                click.echo(
                    f"Skipped {skipped_dupes} known repost(s) of listings already in DB."
                )

            if dry_run:
                click.echo("Dry-run: skipping DB writes and CSV export.")
                for item in enriched:
                    click.echo(
                        f"  - {item.get('fb_id')} | "
                        f"{item.get('price_thb')} THB | "
                        f"{item.get('title')!r}"
                    )
                click.echo(f"Would have processed {len(enriched)} listings.")
                return

            inserted = 0
            updated = 0
            duplicates = 0
            for item in enriched:
                result = upsert_listing(conn, item)
                if result == "inserted":
                    inserted += 1
                elif result == "duplicate":
                    duplicates += 1
                else:
                    updated += 1
            _sync_decisions(conn)
            csv_path = export_csv(conn, config.CSV_PATH)
            html_path = export_html(conn, config.HTML_PATH)

            click.echo(
                f"Done. inserted: {inserted}, updated: {updated}, "
                f"duplicates: {duplicates + skipped_dupes}, "
                f"skipped_known: {skipped_known}, csv: {csv_path}, html: {html_path}"
            )
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                playwright.stop()
    finally:
        conn.close()


@main.command("dedupe")
@click.option(
    "--dry-run", is_flag=True, help="List the duplicates without deleting them."
)
@click.option(
    "--exact-only",
    is_flag=True,
    help="Match identical ad text only; skip the near-duplicate rules.",
)
def dedupe_cmd(dry_run: bool, exact_only: bool) -> None:
    """Collapse duplicates: one row per flat, however many ads it has.

    Facebook issues a new listing id every time an ad is republished, and
    the same flat is often handed to several agents who each write their own
    text. Both cases end up as separate rows. This keeps the copy carrying
    the most work — your decision, then an AI analysis, then the fullest ad
    text — drops the rest and remembers them, so the next scrape skips them
    instead of adding them back.

    The near-duplicate rules read the AI's extracted fields, so run this
    after `ai-apply`; `--exact-only` falls back to matching ad text verbatim.
    """
    conn = _open_db()
    try:
        collapsed = dedupe_listings(conn, dry_run=dry_run, fuzzy=not exact_only)
        if not collapsed:
            click.echo("No duplicates found — every listing is its own flat.")
            return
        removed = sum(len(group["removed"]) for group in collapsed)
        prefix = "Dry-run: would remove" if dry_run else "Removed"
        click.echo(
            f"{prefix} {removed} duplicate(s) across {len(collapsed)} listing(s):"
        )
        for group in collapsed:
            click.echo(
                f"  - keeping {group['canonical']}, "
                f"dropping {', '.join(group['removed'])}"
                f"  [{group['reason']}]"
            )
        if not dry_run:
            csv_path = export_csv(conn, config.CSV_PATH)
            html_path = export_html(conn, config.HTML_PATH)
            click.echo(f"Exports refreshed: {csv_path}, {html_path}")
    finally:
        conn.close()


@main.command("export")
@click.option(
    "--path",
    "csv_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Destination CSV path (default: {config.CSV_PATH}).",
)
@click.option(
    "--html-path",
    "html_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Destination HTML path (default: {config.HTML_PATH}).",
)
def export_cmd(csv_path: Path | None, html_path: Path | None) -> None:
    """Regenerate the CSV and HTML exports from the local SQLite DB."""
    csv_out = csv_path if csv_path is not None else config.CSV_PATH
    html_out = html_path if html_path is not None else config.HTML_PATH
    conn = _open_db()
    try:
        _sync_decisions(conn)
        csv_written = export_csv(conn, csv_out)
        html_written = export_html(conn, html_out)
    finally:
        conn.close()
    click.echo(f"CSV written:  {csv_written}")
    click.echo(f"HTML written: {html_written}")



# --- AI enrichment -------------------------------------------------------

# Registry versions serve two different questions. `schema_version` grows on
# every `ai-field add`, which is what colours old rows' empty cells `c-stale`
# (the field did not exist when they were analysed). `analysis_version` only
# catches up when the user asks for `--backfill`, and that is what decides
# whether those rows are actually re-queued or left alone with their dash.
_ANALYSIS_VERSION_KEY = "analysis_version"


def _registry_path(path: Path | None) -> Path:
    return path if path is not None else config.AI_FIELDS_PATH


def _load_registry(path: Path | None = None) -> ai_schema.Registry:
    """Load the field registry, turning a bad file into a CLI error."""
    try:
        return ai_schema.load_registry(_registry_path(path))
    except ai_schema.RegistryError as exc:
        raise click.ClickException(str(exc)) from exc


def _raw_registry(path: Path | None = None) -> dict[str, Any]:
    """Return the registry file as plain JSON, comments of the format intact."""
    target = _registry_path(path)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise click.ClickException(f"field registry not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"field registry is not valid JSON: {exc}") from exc


def _analysis_version(raw: dict[str, Any]) -> int:
    """Version at which a re-analysis was last requested (defaults to current)."""
    value = raw.get(_ANALYSIS_VERSION_KEY)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(raw.get("schema_version") or 1)


def _open_db() -> sqlite3.Connection:
    config.ensure_home()
    return init_db(config.DB_PATH)


def _sync_decisions(conn: sqlite3.Connection) -> None:
    """Refresh the decisions mirror the exports JOIN against.

    ``data/decisions.json`` is documented as hand-editable, so a typo in it
    is a user error to report, not a traceback.
    """
    try:
        decisions.sync_to_db(conn)
    except ValueError as exc:
        raise click.ClickException(f"{config.DECISIONS_PATH}: {exc}") from exc


def _queue(
    conn: sqlite3.Connection,
    registry: ai_schema.Registry,
    registry_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the analysis queue into ``(pending, stale, no_raw)``.

    Listings pending only because the registry grew are "stale" — they get a
    `c-stale` cell in the export but are left out of the batch until the user
    asks for a re-analysis with `ai-field add --backfill`.
    """
    pending_rows, no_raw = fetch_pending(conn, registry)
    requested = _analysis_version(_raw_registry(registry_path))

    pending: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for row in pending_rows:
        # A row is re-queued only up to the version the user asked to
        # backfill to. Comparing per row (rather than the registry version
        # against `analysis_version`) keeps an earlier `--backfill` alive
        # when a later `ai-field add` runs without one.
        backfilling = int(row.get("analysed_version") or 0) < requested
        if row.get("pending_reason") == "schema" and not backfilling:
            stale.append(row)
        else:
            pending.append(row)
    return pending, stale, no_raw


@main.command("ai-status")
def ai_status_cmd() -> None:
    """Report how many listings are analysed, pending, stale or unusable."""
    registry = _load_registry()
    conn = _open_db()
    try:
        analyzed = conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()[0]
        pending, stale, no_raw = _queue(conn, registry)
    finally:
        conn.close()

    reasons: dict[str, int] = {}
    for row in pending:
        reason = str(row.get("pending_reason"))
        reasons[reason] = reasons.get(reason, 0) + 1
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items()))

    click.echo(f"Registry: schema_version={registry.schema_version}, "
               f"{len(registry.fields)} fields")
    click.echo(f"Analyzed: {analyzed}")
    click.echo(f"Pending:  {len(pending)}" + (f" ({detail})" if detail else ""))
    click.echo(
        f"Stale:    {len(stale)}"
        + (" (new fields added without --backfill)" if stale else "")
    )
    click.echo(f"No raw data: {len(no_raw)}")
    if no_raw:
        click.echo(
            "  These listings have neither a description nor text lines — "
            "run `fb-rent scrape --refresh` before analysing them:"
        )
        for row in no_raw[:10]:
            click.echo(f"    - {row['fb_id']} {row.get('url') or ''}")
        if len(no_raw) > 10:
            click.echo(f"    ... and {len(no_raw) - 10} more")


@main.command("ai-batch")
@click.option(
    "--limit",
    type=int,
    default=25,
    show_default=True,
    help="Maximum number of listings to put in the batch.",
)
@click.option(
    "--all",
    "all_pending",
    is_flag=True,
    default=False,
    help="Include every pending listing, ignoring --limit.",
)
@click.option(
    "--ids",
    "ids",
    multiple=True,
    help="Analyse only these fb_ids (repeatable); overrides the queue order.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Write the batch to this file (default: {config.AI_DIR}/batch-<ts>.json).",
)
def ai_batch_cmd(
    limit: int, all_pending: bool, ids: tuple[str, ...], out_path: Path | None
) -> None:
    """Assemble the JSON batch of listings awaiting AI analysis."""
    if limit <= 0 and not all_pending:
        raise click.ClickException("--limit must be a positive number")
    registry = _load_registry()
    missing_ids: list[str] = []
    conn = _open_db()
    try:
        pending, stale, no_raw = _queue(conn, registry)

        selected = list(pending)
        if ids:
            wanted = {str(i) for i in ids}
            # An explicit id wins over the stale/pending split: the user asked.
            # It also reaches past the queue entirely, so an already-analysed
            # listing can be re-analysed on request.
            queued = {str(row["fb_id"]): row for row in pending + stale}
            selected = []
            unusable: list[str] = []
            for fb_id in sorted(wanted):
                if fb_id in queued:
                    selected.append(queued[fb_id])
                    continue
                if any(str(row["fb_id"]) == fb_id for row in no_raw):
                    unusable.append(fb_id)
                    continue
                row = conn.execute(
                    "SELECT * FROM listings WHERE fb_id = ?", (fb_id,)
                ).fetchone()
                if row is None:
                    missing_ids.append(fb_id)
                else:
                    selected.append(dict(row))
            if unusable:
                click.echo(
                    "No raw text to analyse (run `fb-rent scrape --refresh` "
                    f"first): {', '.join(unusable)}"
                )
            if missing_ids:
                click.echo(f"Unknown listing(s): {', '.join(missing_ids)}")

        if no_raw and not ids:
            click.echo(
                f"{len(no_raw)} listing(s) have no raw text — "
                "run `fb-rent scrape --refresh` to make them analysable."
            )
        if stale and not ids:
            click.echo(
                f"{len(stale)} listing(s) are stale (registry grew since they were "
                "analysed). Re-add the field with --backfill to re-analyse them."
            )

        if not selected:
            click.echo(
                "Nothing to analyse for the given --ids."
                if ids
                else "Nothing pending — every listing is up to date."
            )
            return

        batch = ai_batch.build_batch(
            conn,
            registry,
            listings=selected,
            # An explicit --ids list is the user's own selection: capping it
            # at --limit would drop listings they named.
            limit=None if (all_pending or ids) else limit,
        )
    finally:
        conn.close()

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written = out_path
    else:
        written = ai_batch.write_batch(batch)

    click.echo(f"Batch written: {written} ({len(batch['items'])} listing(s))")


@main.command("ai-apply")
@click.argument(
    "path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate and report without writing anything to the DB.",
)
def ai_apply_cmd(path: Path, dry_run: bool) -> None:
    """Apply an AI result file to the local database."""
    registry = _load_registry()
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc

    conn = _open_db()
    try:
        try:
            report = ai_batch.apply_results(
                conn, registry, result, dry_run=dry_run
            )
        except ai_batch.BatchError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()

    prefix = "Dry-run: would apply" if dry_run else "Applied"
    click.echo(
        f"{prefix} {report.applied} analysis/analyses "
        f"(inserted: {report.inserted}, updated: {report.updated})"
    )

    if report.rejected:
        click.echo(f"Auto-rejected {len(report.rejected)}:")
        for fb_id, reason in report.rejected:
            click.echo(f"  - {fb_id}: {reason}")

    if report.skipped:
        click.echo(f"Skipped {len(report.skipped)}: {', '.join(report.skipped)}")

    if report.proposed_new_fields:
        click.echo(f"Proposed new fields ({len(report.proposed_new_fields)}):")
        for proposal in report.proposed_new_fields:
            why = proposal.get("why") or ""
            click.echo(
                f"  - {proposal.get('key')} ({proposal.get('type', 'text')}): "
                f"{proposal.get('label', '')}" + (f" — {why}" if why else "")
            )
        click.echo("  Add one with `fb-rent ai-field add --key ... [--backfill]`.")

    for warning in report.warnings:
        _echo_err(f"warning: {warning}")


@main.group("ai-field")
def ai_field_group() -> None:
    """Inspect and extend the AI field registry (config/ai_fields.json)."""


@ai_field_group.command("list")
@click.option(
    "--registry",
    "registry_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Registry file to read (default: {config.AI_FIELDS_PATH}).",
)
def ai_field_list_cmd(registry_path: Path | None) -> None:
    """Print every field in the registry."""
    registry = _load_registry(registry_path)
    raw = _raw_registry(registry_path)
    click.echo(
        f"schema_version: {registry.schema_version} "
        f"(analysis_version: {_analysis_version(raw)})"
    )
    for field in registry.fields:
        values = field.get("values")
        suffix = f" [{', '.join(values)}]" if values else ""
        click.echo(
            f"  {field['key']:<20} {field.get('group', ''):<9} "
            f"{field['type']:<5} v{field['added_in']}  {field['label']}{suffix}"
        )


@ai_field_group.command("add")
@click.option("--key", required=True, help="Field key used in data_json.")
@click.option("--label", required=True, help="Column heading in the HTML export.")
@click.option(
    "--type",
    "ftype",
    required=True,
    type=click.Choice(sorted(ai_schema.KNOWN_TYPES)),
    help="Field type.",
)
@click.option(
    "--values",
    default=None,
    help="Comma-separated allowed values (required for --type enum).",
)
@click.option("--group", default="core", show_default=True, help="Column group.")
@click.option("--prompt", default=None, help="Extraction instruction for the AI.")
@click.option(
    "--after",
    default=None,
    help="Insert the new field right after this existing key.",
)
@click.option(
    "--backfill",
    is_flag=True,
    default=False,
    help="Re-queue already-analysed listings so the AI fills the new field "
    "(without it they simply show a c-stale dash).",
)
@click.option(
    "--registry",
    "registry_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Registry file to modify (default: {config.AI_FIELDS_PATH}).",
)
def ai_field_add_cmd(
    key: str,
    label: str,
    ftype: str,
    values: str | None,
    group: str,
    prompt: str | None,
    after: str | None,
    backfill: bool,
    registry_path: Path | None,
) -> None:
    """Add a field to the registry, bumping its schema_version."""
    target = _registry_path(registry_path)
    raw = _raw_registry(registry_path)
    fields = raw.get("fields")
    if not isinstance(fields, list):
        raise click.ClickException("registry has no 'fields' list")

    if any(isinstance(f, dict) and f.get("key") == key for f in fields):
        raise click.ClickException(f"field {key!r} already exists in {target}")

    value_list = [v.strip() for v in (values or "").split(",") if v.strip()]
    if ftype == "enum" and not value_list:
        raise click.ClickException("--type enum needs a non-empty --values list")

    # Every addition bumps the version: that is what tells the export "this
    # field did not exist when the older rows were analysed".
    new_version = int(raw.get("schema_version") or 1) + 1
    field: dict[str, Any] = {
        "key": key,
        "label": label,
        "group": group,
        "type": ftype,
        "added_in": new_version,
    }
    if value_list:
        field["values"] = value_list
    if prompt:
        field["prompt"] = prompt

    if after is not None:
        position = next(
            (
                i + 1
                for i, f in enumerate(fields)
                if isinstance(f, dict) and f.get("key") == after
            ),
            None,
        )
        if position is None:
            raise click.ClickException(f"--after: no such field {after!r}")
        fields.insert(position, field)
    else:
        fields.append(field)

    # Read the old analysis version before the bump: without --backfill it
    # stays where it is, which is what keeps analysed rows out of the queue.
    previous_analysis = _analysis_version(raw)
    raw["schema_version"] = new_version
    raw[_ANALYSIS_VERSION_KEY] = new_version if backfill else previous_analysis

    _write_registry(target, raw)

    click.echo(
        f"Added {key!r} to {target} (schema_version {new_version - 1} -> "
        f"{new_version})"
    )
    if backfill:
        click.echo(
            "Backfill requested: already-analysed listings are pending again — "
            "run `fb-rent ai-batch`."
        )
    else:
        click.echo(
            "Existing rows keep their analysis and show a c-stale dash for this "
            "field. Re-run with --backfill to re-analyse them."
        )


def _write_registry(target: Path, raw: dict[str, Any]) -> None:
    """Validate ``raw`` as a registry, then replace ``target`` atomically."""
    payload = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".ai_fields-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        try:
            ai_schema.load_registry(tmp_name)
        except ai_schema.RegistryError as exc:
            raise click.ClickException(f"refusing to write an invalid registry: {exc}")
        # mkstemp creates 0600; os.replace would carry that onto a
        # version-controlled file that was 0644.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# --- User decisions ------------------------------------------------------


def _record_decision(fb_id: str, status: str, reason: str | None) -> None:
    """Write one decision to the JSON file and mirror it into the DB."""
    try:
        entry = decisions.save_decision(None, fb_id, status, reason)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    conn = _open_db()
    try:
        _sync_decisions(conn)
    finally:
        conn.close()

    suffix = f" — {entry['reason']}" if entry.get("reason") else ""
    click.echo(f"{fb_id}: {status}{suffix}")
    click.echo(f"Saved to {config.DECISIONS_PATH}. Run `fb-rent export` to refresh the table.")


@main.command("reject")
@click.argument("fb_id")
@click.option("--reason", default=None, help="Why this listing is out.")
def reject_cmd(fb_id: str, reason: str | None) -> None:
    """Reject a listing; survives DB wipes and overrides the AI verdict."""
    _record_decision(fb_id, "rejected", reason)


@main.command("unreject")
@click.argument("fb_id")
@click.option("--reason", default=None, help="Why it is back in the running.")
def unreject_cmd(fb_id: str, reason: str | None) -> None:
    """Put a listing back into the active set, overriding an AI rejection."""
    _record_decision(fb_id, "active", reason)


@main.command("shortlist")
@click.argument("fb_id")
@click.option("--reason", default=None, help="What makes it a candidate.")
def shortlist_cmd(fb_id: str, reason: str | None) -> None:
    """Mark a listing as a candidate worth contacting."""
    _record_decision(fb_id, "shortlist", reason)


if __name__ == "__main__":
    main()
