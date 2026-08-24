"""Command-line entry point for fb-rent.

Wires the browser, list scraper, detail scraper, extractors and SQLite
storage layers into three commands:

- ``fb-rent login`` — opens a headed Chromium via a persistent profile so
  the user can log into Facebook once. Cookies live in the profile and
  are reused by later headless scrape runs.
- ``fb-rent scrape`` — orchestrator: builds the marketplace search URL,
  scrapes the list, walks each item's detail page with a random 3–7 s
  jitter, extracts structured fields and upserts them into SQLite. Ends
  by regenerating the CSV export.
- ``fb-rent export`` — regenerates the CSV export from the current DB
  without touching the network. Useful for verification.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Any

import click

from . import config
from .browser import (
    LoginRequiredError,
    is_logged_in,
    launch_persistent,
    login_flow,
    start_playwright,
)
from .extractors import extract_bedrooms, extract_property_type, parse_price
from .scraper_detail import scrape_detail
from .scraper_list import scrape_list
from .storage import export_csv, init_db, upsert_listing
from .url_builder import build_marketplace_url


def _echo_err(msg: str) -> None:
    click.echo(msg, err=True)


def _merge_listing(list_card: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """Combine list-card fields with (optional) detail-page fields.

    Detail fields override list-card fields where both are present, since
    the detail page carries the fuller/fresher copy (e.g. full description
    vs. a truncated card snippet).
    """
    merged: dict[str, Any] = dict(list_card)
    if detail:
        for key in ("description", "published_at", "raw_json"):
            if detail.get(key) is not None:
                merged[key] = detail[key]
        if detail.get("price_raw"):
            merged["price_raw"] = detail["price_raw"]
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
def scrape_cmd(max_items: int, days: int, dry_run: bool, skip_details: bool) -> None:
    """Scrape the marketplace and upsert results into the local SQLite DB."""
    url = build_marketplace_url(days_since_listed=days)
    click.echo(f"URL: {url}")
    click.echo(
        f"Options: max={max_items} days={days} "
        f"dry_run={dry_run} skip_details={skip_details}"
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
        for idx, card in enumerate(cards):
            if not card.get("url") or not card.get("title"):
                click.echo(
                    f"  [{idx + 1}/{len(cards)}] skipping card missing url/title: "
                    f"fb_id={card.get('fb_id')}"
                )
                continue
            detail: dict[str, Any] | None = None
            if idx < detail_cap:
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
            merged = _merge_listing(card, detail)
            enriched.append(_enrich(merged))

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

        config.ensure_home()
        conn = init_db(config.DB_PATH)
        try:
            inserted = 0
            updated = 0
            for item in enriched:
                result = upsert_listing(conn, item)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
            csv_path = export_csv(conn, config.CSV_PATH)
        finally:
            conn.close()

        click.echo(
            f"Done. inserted: {inserted}, updated: {updated}, csv: {csv_path}"
        )
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            playwright.stop()


@main.command("export")
@click.option(
    "--path",
    "csv_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Destination CSV path (default: {config.CSV_PATH}).",
)
def export_cmd(csv_path: Path | None) -> None:
    """Regenerate the CSV export from the local SQLite DB."""
    out = csv_path if csv_path is not None else config.CSV_PATH
    config.ensure_home()
    conn = init_db(config.DB_PATH)
    try:
        written = export_csv(conn, out)
    finally:
        conn.close()
    click.echo(f"CSV written: {written}")


if __name__ == "__main__":
    main()
