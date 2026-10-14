"""Command-line entry point for rent-assistant.

Wires the browser, list scraper, detail scraper, extractors and SQLite
storage layers into three commands:

- ``rent-assistant login`` — opens a headed Chromium via a persistent profile so
  the user can log into Facebook once. Cookies live in the profile and
  are reused by later headless scrape runs.
- ``rent-assistant scrape`` — orchestrator: builds the marketplace search URL,
  scrapes the list, walks each item's detail page with a random 3–7 s
  jitter, extracts structured fields and upserts them into SQLite. Ends
  by regenerating the CSV and HTML exports.
- ``rent-assistant export`` — regenerates the CSV and HTML exports from the
  current DB without touching the network. Useful for verification.

The ``ai-*`` commands drive the enrichment loop: ``ai-status`` reports what
is waiting, ``ai-batch`` writes the file the AI reads, ``ai-apply`` reads
back what it extracted, and ``ai-field`` evolves the registry. ``reject`` /
``unreject`` / ``shortlist`` / ``rate`` record the user's own verdicts, which
live in ``data/decisions.json`` and outrank anything the AI decided.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from itertools import zip_longest
from pathlib import Path
from typing import Any

import click

from . import (
    ai_batch,
    ai_schema,
    buildings,
    buildings_batch,
    config,
    decisions,
    fingerprint,
    messages as messages_module,
    photo_batch,
    photos,
    rules,
)
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
from .messenger import (
    MessagingLimitReachedError,
    MessengerPinRequiredError,
    list_marketplace_threads,
    read_thread,
    send_first_message,
)
from .scraper_detail import check_listing_alive, scrape_detail
from .scraper_group import scrape_group_feed, scrape_group_post
from .scraper_list import scrape_list
from .storage import (
    apply_conversation_classification,
    conversations_by_fb_id,
    conversations_needing_classification,
    dedupe_listings,
    dedupe_one,
    duplicate_fb_ids,
    duplicates_by_canonical,
    export_csv,
    export_html,
    fetch_enriched,
    fetch_pending,
    fetch_pending_buildings,
    fetch_pending_photos,
    fetch_pets_conflicts,
    gallery_urls_of,
    init_db,
    known_fb_ids,
    listings_missing_photo_hashes,
    marketplace_url,
    needs_action_conversations,
    normalise_district,
    save_conversation,
    save_photo_hashes,
    sync_buildings_to_db,
    upsert_listing,
)
from .url_builder import build_marketplace_url, group_post_url, price_bands


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
        # the "recently listed" badge), the detail page's <title> is the only
        # real headline we have.
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
@click.version_option(package_name="rent-assistant")
def main() -> None:
    """FB Marketplace Phuket rentals scraper (MVP).

    Run ``rent-assistant login`` once to authenticate, then use
    ``rent-assistant scrape`` to pull fresh listings into the local SQLite DB.
    """


@main.command("login")
@click.option(
    "--profile",
    default=None,
    help=(
        "Name a second account's own browser profile (its own cookies, "
        "never mixed with the default one) — e.g. --profile partner for a "
        "second person in the household. Omit for the main account."
    ),
)
def login_cmd(profile: str | None) -> None:
    """Open a browser window and wait for a manual Facebook login."""
    profile_dir = config.browser_profile_dir(profile)
    click.echo("Opening Facebook login in a browser window...")
    click.echo("Sign in (including 2FA if enabled), then leave the tab open.")
    if profile:
        click.echo(f"Profile: {profile} ({profile_dir})")
    playwright = start_playwright()
    context = None
    try:
        try:
            context = launch_persistent(playwright, headless=False, profile_dir=profile_dir)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)
        ok = login_flow(context)
        if ok:
            click.echo("Login detected. Session saved to persistent profile.")
        else:
            _echo_err("Timed out waiting for login. Re-run `rent-assistant login`.")
            sys.exit(1)
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            playwright.stop()


def _collect_cards(
    context: Any,
    bands: list[tuple[int, int]],
    *,
    category: str,
    days: int,
    max_items: int,
) -> list[dict[str, Any]]:
    """Walk the feed once per price band and return the cards, deduplicated.

    Cards come back in round-robin order rather than band after band: the
    detail budget is shared by the whole run, and taken in order it would be
    spent entirely inside the cheapest band before the dearer ones are
    reached. Each band contributes its freshest card first.
    """
    sweeping = len(bands) > 1
    collected: list[list[dict[str, Any]]] = []
    for number, (low, high) in enumerate(bands, start=1):
        band_url = build_marketplace_url(
            category_id=category,
            min_price=low,
            max_price=high,
            days_since_listed=days,
        )
        label = (
            f"[{number}/{len(bands)}] {low}-{high} THB: " if sweeping else "URL: "
        )
        click.echo(f"{label}{band_url}")
        # A band with no listings in it is an answer, not a dead session —
        # the verdict on the session waits until every band has been asked.
        band_cards = scrape_list(
            context, band_url, max_items=max_items, allow_empty=sweeping
        )
        if sweeping:
            click.echo(f"    {len(band_cards)} cards.")
        collected.append(band_cards)

    cards_by_id: dict[str, dict[str, Any]] = {}
    for row in zip_longest(*collected):
        for card in row:
            if card is not None:
                cards_by_id.setdefault(card["fb_id"], card)
    return list(cards_by_id.values())


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


def _fetch_photo_hashes(context: Any, detail: dict[str, Any]) -> list[tuple[int, str, str]]:
    """Hash the first few gallery shots of a freshly read detail page.

    Three shots, not the gallery: this is the duplicate signal, and it runs
    even when the photo pass is switched off with ``--no-photos``. Nothing
    lands in ``data/photos/`` — a preview written there would tell the photo
    pass forever that the gallery is already downloaded.
    """
    urls = gallery_urls_of(detail.get("raw_json"))
    if not urls:
        return []
    return fingerprint.fetch_fingerprints(context, urls)


def _detail_fn(context: Any, card: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch a card's detail fetch by its own ``source`` — the one thing
    that lets a single processing loop and a single ``--ids`` repair path
    serve both marketplace items and group posts."""
    source = card.get("source") or "marketplace"
    if source.startswith("group:"):
        group_id = source.split(":", 1)[1]
        return scrape_group_post(context, card["url"], group_id, str(card["fb_id"]))
    return scrape_detail(context, card["url"])


def _process_cards(
    conn: sqlite3.Connection,
    context: Any,
    cards: list[dict[str, Any]],
    *,
    detail_fn: Callable[[Any, dict[str, Any]], dict[str, Any] | None] = _detail_fn,
    detail_cap: int,
    dry_run: bool,
    no_photos: bool,
    seen_ids: set[str],
    dupe_ids: set[str],
    require_card_title: bool,
) -> None:
    """Walk cards, fetch detail pages within budget, upsert, export.

    Shared by ``scrape`` and ``scrape-groups`` — the only difference
    between a marketplace sweep and a group sweep is how the cards were
    collected and what ``detail_fn`` does with a card's URL; everything
    from here down (budget, jitter, photo hashing, upsert, exports) is
    identical.
    """
    click.echo(f"Collected {len(cards)} list cards.")

    enriched: list[dict[str, Any]] = []
    skipped_known = 0
    skipped_dupes = 0
    details_fetched = 0
    inserted = 0
    updated = 0
    duplicates = 0
    photo_hashed = 0
    with click.progressbar(
        length=len(cards),
        label="Scraping listings",
        show_pos=True,
        show_eta=True,
        item_show_func=lambda c: str(c.get("fb_id")) if c else "",
    ) as bar:
        for idx, card in enumerate(cards):
            # A targeted/feed card carries no title until its detail page
            # answers — that is the whole point of naming it.
            if not card.get("url") or (require_card_title and not card.get("title")):
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
            # None means "not asked", which is not the same as "asked
            # and got nothing": only the second one may erase hashes.
            hash_entries: list[tuple[int, str, str]] | None = None
            already_known = card["fb_id"] in seen_ids
            if already_known:
                skipped_known += 1
            elif details_fetched < detail_cap:
                details_fetched += 1
                time.sleep(
                    random.uniform(
                        config.DETAIL_DELAY_MIN_SEC,
                        config.DETAIL_DELAY_MAX_SEC,
                    )
                )
                try:
                    detail = detail_fn(context, card)
                except LoginRequiredError as exc:
                    _echo_err(str(exc))
                    _echo_err(
                        f"Stopped after {inserted + updated + duplicates} "
                        "listing(s) — those are already in the DB. Run "
                        "`rent-assistant export` to refresh the exports, then "
                        "`rent-assistant login` and scrape again."
                    )
                    sys.exit(3)
                if detail is None:
                    click.echo(
                        f"  [{idx + 1}/{len(cards)}] detail unavailable, "
                        f"keeping list-card data: {card['url']}"
                    )
                elif not dry_run:
                    # Inside the jitter we already paid for — the
                    # CDN fetches add ~1–2 s and no extra pause.
                    hash_entries = _fetch_photo_hashes(context, detail)
                    if not no_photos:
                        _download_photos(context, detail, card["fb_id"])
            item = _enrich(_merge_listing(card, detail))
            if not item.get("title"):
                if card["fb_id"] not in seen_ids:
                    # Reachable through --ids or a feed card whose detail
                    # page is gone or not a listing at all. `title` is
                    # NOT NULL and there is nothing to insert.
                    click.echo(
                        f"  [{idx + 1}/{len(cards)}] no listing behind "
                        f"{card['fb_id']} — skipped."
                    )
                    bar.update(1, card)
                    continue
                # A known id re-sighted as a bare card (detail fetch
                # skipped): still worth an upsert to refresh
                # `last_seen_at` — `upsert_listing` drops the missing
                # title on update rather than clobbering the stored one.
            if dry_run:
                enriched.append(item)
            else:
                # Written one at a time on purpose: a wide run walks
                # detail pages for the better part of an hour, and a
                # checkpoint or an expired session near the end used
                # to throw away everything scraped before it.
                result = upsert_listing(conn, item)
                if result == "inserted":
                    inserted += 1
                elif result == "duplicate":
                    duplicates += 1
                else:
                    updated += 1
                if hash_entries is not None and result != "duplicate":
                    save_photo_hashes(conn, item["fb_id"], hash_entries)
                    if hash_entries:
                        photo_hashed += 1
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

    if detail_cap and details_fetched >= detail_cap:
        click.echo(
            f"Detail budget spent ({detail_cap} pages) — the rest kept "
            "list-card data only. Raise it with --detail-cap."
        )

    _sync_decisions(conn)
    _sync_buildings(conn)
    csv_path = export_csv(conn, config.CSV_PATH)
    html_path = export_html(conn, config.HTML_PATH)

    click.echo(
        f"Done. inserted: {inserted}, updated: {updated}, "
        f"duplicates: {duplicates + skipped_dupes}, "
        f"skipped_known: {skipped_known}, photo hashes: {photo_hashed}, "
        f"csv: {csv_path}, html: {html_path}"
    )


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
@click.option(
    "--detail-cap",
    "detail_cap_opt",
    type=int,
    default=config.DETAIL_REQUEST_CAP,
    show_default=True,
    help="Maximum detail pages to open in one run.",
)
@click.option(
    "--min-price",
    "min_price",
    type=int,
    default=config.DEFAULT_MIN_PRICE,
    show_default=True,
    help="Lower bound of the price window, in THB.",
)
@click.option(
    "--max-price",
    "max_price",
    type=int,
    default=config.DEFAULT_MAX_PRICE,
    show_default=True,
    help="Upper bound of the price window, in THB.",
)
@click.option(
    "--bands",
    "band_count",
    type=int,
    default=1,
    show_default=True,
    help="Split the price window into N narrower queries and sweep them all.",
)
@click.option(
    "--category",
    "category",
    default=config.DEFAULT_CATEGORY_ID,
    show_default=True,
    help="Marketplace category/location id to search.",
)
@click.option(
    "--ids",
    "ids",
    multiple=True,
    help="Scrape these fb_ids directly, skipping the feed (repeatable).",
)
def scrape_cmd(
    max_items: int,
    days: int,
    dry_run: bool,
    skip_details: bool,
    refresh: bool,
    no_photos: bool,
    detail_cap_opt: int,
    min_price: int,
    max_price: int,
    band_count: int,
    category: str,
    ids: tuple[str, ...],
) -> None:
    """Scrape the marketplace and upsert results into the local SQLite DB."""
    try:
        bands = price_bands(min_price, max_price, band_count)
    except ValueError as exc:
        _echo_err(str(exc))
        sys.exit(1)

    click.echo(
        f"Options: max={max_items} days={days} "
        f"price={min_price}-{max_price} bands={len(bands)} category={category} "
        f"dry_run={dry_run} skip_details={skip_details} refresh={refresh} "
        f"no_photos={no_photos} detail_cap={detail_cap_opt}"
    )

    config.ensure_home()
    conn = init_db(config.DB_PATH)
    try:
        # An id asked for by name is always fetched: the reason to name one
        # is that its stored copy is wrong — sidebar noise instead of the ad,
        # no text at all, a gallery that never made it to disk.
        targeted = bool(ids)
        seen_ids = set() if (refresh or targeted) else known_fb_ids(conn)
        # Reposts stay skipped even under --refresh: their ad text already has
        # a row, and fetching the detail page again would only re-prove it.
        dupe_ids = set() if targeted else duplicate_fb_ids(conn)
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
                    "No active Facebook session. Run `rent-assistant login` first."
                )
                sys.exit(3)

            if targeted:
                cards = []
                for fb_id in dict.fromkeys(ids):
                    # An id already in the DB carries its own url/source —
                    # rebuilding a marketplace url for it would open the
                    # wrong page entirely for a group post. Only a brand
                    # new id falls back to the marketplace guess, which is
                    # what `scrape --ids` has always been for.
                    row = conn.execute(
                        "SELECT url, source FROM listings WHERE fb_id = ?",
                        (fb_id,),
                    ).fetchone()
                    if row is not None:
                        cards.append(
                            {
                                "fb_id": fb_id,
                                "url": row["url"],
                                "title": None,
                                "source": row["source"],
                            }
                        )
                    else:
                        cards.append(
                            {"fb_id": fb_id, "url": marketplace_url(fb_id), "title": None}
                        )
                click.echo(f"Targeting {len(cards)} listing(s) by id.")
            else:
                try:
                    cards = _collect_cards(
                        context,
                        bands,
                        category=category,
                        days=days,
                        max_items=max_items,
                    )
                except LoginRequiredError as exc:
                    _echo_err(str(exc))
                    sys.exit(3)
                if not cards:
                    _echo_err(
                        "Every band came back empty — the session has likely "
                        "expired. Run `rent-assistant login` and retry."
                    )
                    sys.exit(3)

            # The cap counts detail pages actually opened, not positions in
            # the feed: known listings and reposts fill the first screens
            # once the DB has grown, and an index-based cap would spend the
            # whole budget on them before reaching a single new listing.
            detail_cap = 0 if skip_details else detail_cap_opt

            _process_cards(
                conn,
                context,
                cards,
                detail_cap=detail_cap,
                dry_run=dry_run,
                no_photos=no_photos,
                seen_ids=seen_ids,
                dupe_ids=dupe_ids,
                require_card_title=not targeted,
            )
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                playwright.stop()
    finally:
        conn.close()


def _collect_group_cards(
    context: Any,
    group_ids: list[str],
    *,
    max_items: int,
    days: int,
) -> list[dict[str, Any]]:
    """Walk each group's feed once and return the cards, round-robin merged.

    Mirrors ``_collect_cards``: the detail budget is shared across every
    group in the run, so cards are interleaved rather than taken group
    after group — otherwise the whole budget would go to the first group
    before a second one is even read.
    """
    sweeping = len(group_ids) > 1
    collected: list[list[dict[str, Any]]] = []
    for number, group_id in enumerate(group_ids, start=1):
        label = config.GROUPS.get(group_id, group_id)
        click.echo(f"[{number}/{len(group_ids)}] {label} ({group_id})")
        group_cards = scrape_group_feed(
            context, group_id, max_items=max_items, days=days, allow_empty=sweeping
        )
        if sweeping:
            click.echo(f"    {len(group_cards)} posts.")
        collected.append(group_cards)

    cards_by_id: dict[str, dict[str, Any]] = {}
    for row in zip_longest(*collected):
        for card in row:
            if card is not None:
                cards_by_id.setdefault(card["fb_id"], card)
    return list(cards_by_id.values())


@main.command("scrape-groups")
@click.option(
    "--group",
    "group_ids",
    multiple=True,
    help="Group id/slug to scrape (repeatable). Default: every group in "
    "config.GROUPS.",
)
@click.option(
    "--max",
    "max_items",
    type=int,
    default=config.GROUP_FEED_MAX_ITEMS,
    show_default=True,
    help="Maximum posts to collect per group.",
)
@click.option(
    "--days",
    "days",
    type=int,
    default=config.DEFAULT_DAYS_SINCE_LISTED,
    show_default=True,
    help="Stop scrolling a group's feed once posts read older than this.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be scraped/written without touching the DB.",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Re-scrape detail pages for posts already in the DB.",
)
@click.option(
    "--no-photos",
    "no_photos",
    is_flag=True,
    default=False,
    help="Skip downloading post photos (faster runs, no photo pass).",
)
@click.option(
    "--detail-cap",
    "detail_cap_opt",
    type=int,
    default=config.DETAIL_REQUEST_CAP,
    show_default=True,
    help="Maximum detail pages to open in one run.",
)
@click.option(
    "--ids",
    "ids",
    multiple=True,
    help="Scrape these post fb_ids directly, skipping the feed (repeatable). "
    "Requires a single --group.",
)
def scrape_groups_cmd(
    group_ids: tuple[str, ...],
    max_items: int,
    days: int,
    dry_run: bool,
    refresh: bool,
    no_photos: bool,
    detail_cap_opt: int,
    ids: tuple[str, ...],
) -> None:
    """Scrape Facebook group posts and upsert results into the local SQLite DB."""
    groups = list(group_ids) or list(config.GROUPS)
    unknown = [g for g in groups if g not in config.GROUPS]
    if unknown:
        _echo_err(f"Unknown group(s): {', '.join(unknown)}. Known: {', '.join(config.GROUPS)}")
        sys.exit(1)

    targeted = bool(ids)
    if targeted and len(groups) != 1:
        _echo_err("--ids requires exactly one --group to build permalinks from.")
        sys.exit(1)

    click.echo(
        f"Options: groups={groups} max={max_items} days={days} "
        f"dry_run={dry_run} refresh={refresh} no_photos={no_photos} "
        f"detail_cap={detail_cap_opt}"
    )

    config.ensure_home()
    conn = init_db(config.DB_PATH)
    try:
        seen_ids = set() if (refresh or targeted) else known_fb_ids(conn)
        dupe_ids = set() if targeted else duplicate_fb_ids(conn)
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
                _echo_err("No active Facebook session. Run `rent-assistant login` first.")
                sys.exit(3)

            if targeted:
                group_id = groups[0]
                cards = [
                    {
                        "fb_id": fb_id,
                        "url": group_post_url(group_id, fb_id),
                        "title": None,
                        "source": f"group:{group_id}",
                    }
                    for fb_id in dict.fromkeys(ids)
                ]
                click.echo(f"Targeting {len(cards)} post(s) by id.")
            else:
                try:
                    cards = _collect_group_cards(
                        context, groups, max_items=max_items, days=days
                    )
                except LoginRequiredError as exc:
                    _echo_err(str(exc))
                    sys.exit(3)
                if not cards:
                    _echo_err(
                        "Every group came back empty — the session has likely "
                        "expired. Run `rent-assistant login` and retry."
                    )
                    sys.exit(3)

            _process_cards(
                conn,
                context,
                cards,
                detail_cap=detail_cap_opt,
                dry_run=dry_run,
                no_photos=no_photos,
                seen_ids=seen_ids,
                dupe_ids=dupe_ids,
                require_card_title=False,
            )
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                playwright.stop()
    finally:
        conn.close()


def _dedupe_one_cmd(
    conn: sqlite3.Connection, fb_id: str, *, into: str | None, dry_run: bool
) -> None:
    """`dedupe <fb_id>`: merge one row, or print what it could be merged with.

    No export at the end, unlike the whole-base pass: this runs from the
    table's block of commands, which already ends in `rent-assistant export`, and
    a block with a dozen merges in it would otherwise rebuild the file a
    dozen times.
    """
    try:
        result = dedupe_one(conn, fb_id, into=into, dry_run=dry_run)
    except LookupError as missing:
        raise click.ClickException(f"No listing {missing.args[0]} in the DB.")
    except ValueError:
        raise click.ClickException("A listing cannot be merged into itself.")

    if result.canonical:
        prefix = "Dry-run: would merge" if dry_run else "Merged"
        click.echo(
            f"{prefix} {', '.join(result.removed)} into {result.canonical}"
            f"  [{result.reason}]"
        )
        if not dry_run:
            click.echo("Run `rent-assistant export` to refresh the table.")
        return

    click.echo(f"Nothing merged: no signal ties {fb_id} to another listing.")
    if not result.candidates:
        click.echo(
            "Nor is any other row close enough to suggest — find it in the "
            f"table and merge by hand: rent-assistant dedupe {fb_id} --into <fb_id>"
        )
        return
    click.echo("Closest rows — run one of these if it is the same flat:")
    for cand in result.candidates:
        mark = cand["status"]
        if cand["rating"] is not None:
            mark += f" {cand['rating']:g}"
        why = ", ".join(cand["why"])
        if cand["contradicts"]:
            why += "; but floor, block or size differ"
        price = f"{cand['price']} THB" if cand["price"] is not None else "no price"
        click.echo(f"  rent-assistant dedupe {fb_id} --into {cand['fb_id']}")
        click.echo(f"      {cand['title'][:60]} — {price} [{mark}] — {why}")


@main.command("dedupe")
@click.argument("fb_id", required=False)
@click.option(
    "--into",
    metavar="FB_ID",
    help="Merge FB_ID into this listing outright, without consulting a signal.",
)
@click.option(
    "--dry-run", is_flag=True, help="List the duplicates without deleting them."
)
@click.option(
    "--exact-only",
    is_flag=True,
    help="Match identical ad text only; skip the near-duplicate rules.",
)
def dedupe_cmd(
    fb_id: str | None, into: str | None, dry_run: bool, exact_only: bool
) -> None:
    """Collapse duplicates: one row per flat, however many ads it has.

    Facebook issues a new listing id every time an ad is republished, so the
    same flat ends up as several rows. This keeps the copy carrying the most
    work — your decision, then an AI analysis, then the fullest ad text —
    drops the rest and remembers them, so the next scrape skips them instead
    of adding them back.

    Only signals that identify a flat rather than describe it merge on their
    own: the same gallery shots, the same ad text verbatim, or an ad body
    all but identical to another. The same flat handed to two agents who
    each wrote their own text is *not* merged unasked — press `d` on the row
    and confirm the pair.

    The veto that keeps different floors apart reads the AI's extracted
    fields, so run this after `ai-apply`; `--exact-only` skips the
    near-identical text rule and matches verbatim only.

    With FB_ID it answers the other question — "I have already seen this
    flat, find it" — and merges that one row into the copy you decided on.
    This is what `d` in the table queues. When no signal ties the row to
    another, nothing is merged and the nearest rows are printed with the
    `--into` that merges them.
    """
    if into and not fb_id:
        raise click.UsageError("--into needs the listing to merge: dedupe <fb_id> --into <fb_id>")
    conn = _open_db()
    try:
        if fb_id:
            _dedupe_one_cmd(conn, fb_id, into=into, dry_run=dry_run)
            return
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
        _sync_buildings(conn)
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
# The photo pass has its own pair of the same question: `photo_analysis_version`
# is what a `--backfill` on a photo field moves, and it is what re-queues rows
# the camera already looked at.
_PHOTO_ANALYSIS_VERSION_KEY = "photo_analysis_version"


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


def _photo_analysis_version(raw: dict[str, Any]) -> int:
    """Version at which a photo re-analysis was last requested."""
    value = raw.get(_PHOTO_ANALYSIS_VERSION_KEY)
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
        # The photo queue honours user rejections, and decisions.json is the
        # source of truth for those — refresh the mirror before counting.
        _sync_decisions(conn)
        analyzed = conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()[0]
        pending, stale, no_raw = _queue(conn, registry)
        photo_analyzed = conn.execute(
            "SELECT COUNT(*) FROM ai_analysis WHERE photos_analyzed_at IS NOT NULL"
        ).fetchone()[0]
        photo_pending, no_photos = fetch_pending_photos(
            conn,
            registry,
            photo_analysis_version=_photo_analysis_version(_raw_registry()),
        )
        total_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        hashed_listings = conn.execute(
            "SELECT COUNT(DISTINCT fb_id) FROM photo_hashes"
        ).fetchone()[0]
        building_store = _load_buildings()
        unresearched = len(fetch_pending_buildings(conn, building_store))
        conflicts = fetch_pets_conflicts(conn, building_store)
    finally:
        conn.close()

    proposed = sum(
        1 for e in building_store.values() if e.get("status") == "proposed"
    )
    confirmed = sum(
        1 for e in building_store.values() if e.get("status") == "confirmed"
    )

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
            "re-read their pages with `rent-assistant scrape --ids <fb_id>` before "
            "analysing them:"
        )
        for row in no_raw[:10]:
            click.echo(f"    - {row['fb_id']} {row.get('url') or ''}")
        if len(no_raw) > 10:
            click.echo(f"    ... and {len(no_raw) - 10} more")

    photo_reasons: dict[str, int] = {}
    for row in photo_pending:
        reason = str(row.get("pending_reason"))
        photo_reasons[reason] = photo_reasons.get(reason, 0) + 1
    photo_detail = ", ".join(f"{k}: {v}" for k, v in sorted(photo_reasons.items()))

    click.echo("Photos:")
    click.echo(f"  Analyzed: {photo_analyzed}")
    click.echo(
        f"  Pending:  {len(photo_pending)}"
        + (f" ({photo_detail})" if photo_detail else "")
    )
    click.echo(f"  No photos: {len(no_photos)}")
    # The hash backfill has its own queue: it does not care about the photo
    # pass at all, so it gets its own line rather than a pending_reason.
    click.echo(f"  Photo hashes: {hashed_listings} of {total_listings}")
    if no_photos:
        click.echo(
            "  These listings have no photos on disk — "
            "run `rent-assistant photos-fetch` to download them:"
        )
        for row in no_photos[:10]:
            click.echo(f"    - {row['fb_id']} {row.get('url') or ''}")
        if len(no_photos) > 10:
            click.echo(f"    ... and {len(no_photos) - 10} more")

    # A proposal is inert until the user confirms it, so `Proposed` is a
    # queue of questions to answer, not of work already done.
    click.echo("Buildings:")
    click.echo(f"  Unresearched: {unresearched}")
    click.echo(f"  Proposed:       {proposed}")
    click.echo(f"  Confirmed:      {confirmed}")
    # Not a queue: these rows are already visible in the table and rejected by
    # nothing. The line is here so a building the research got wrong surfaces
    # without anyone thinking to look for it.
    if conflicts:
        rows = sum(len(c["listings"]) for c in conflicts)
        click.echo(
            f"  Conflicts:      {len(conflicts)} "
            f"(listings: {rows}) — `rent-assistant buildings conflicts`"
        )


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
                    "No raw text to analyse (run `rent-assistant scrape --ids` "
                    f"first): {', '.join(unusable)}"
                )
            if missing_ids:
                click.echo(f"Unknown listing(s): {', '.join(missing_ids)}")

        if no_raw and not ids:
            click.echo(
                f"{len(no_raw)} listing(s) have no raw text — "
                "run `rent-assistant scrape --ids <fb_id>` to make them analysable."
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


@main.command("photos-fetch")
@click.option(
    "--limit",
    default=25,
    show_default=True,
    help="Maximum number of listings to fetch galleries for.",
)
@click.option(
    "--all",
    "fetch_all",
    is_flag=True,
    default=False,
    help="Fetch every listing in the queue, ignoring --limit.",
)
@click.option(
    "--ids",
    "ids",
    multiple=True,
    help="Fetch these fb_ids instead of the queue (repeatable).",
)
def photos_fetch_cmd(limit: int, fetch_all: bool, ids: tuple[str, ...]) -> None:
    """Download the galleries the photo pass is waiting for.

    A wide sweep is cheaper with `--no-photos`, and the photo pass only ever
    looks at listings that survived the text pass and your own rejections —
    so the pictures are better fetched afterwards, for the survivors alone.
    What makes that possible is that the detail page's gallery links are
    stored with the listing. They are signed and expire in about a week; past
    that only the page itself can hand them out again
    (`rent-assistant scrape --ids <fb_id>`).
    """
    if limit <= 0 and not fetch_all:
        raise click.ClickException("--limit must be a positive number")

    registry = _load_registry()
    conn = _open_db()
    try:
        # A hand-rejected listing is not worth a download either.
        _sync_decisions(conn)
        rows: list[dict[str, Any]] = []
        if ids:
            unknown: list[str] = []
            for fb_id in dict.fromkeys(str(i) for i in ids):
                row = conn.execute(
                    "SELECT * FROM listings WHERE fb_id = ?", (fb_id,)
                ).fetchone()
                if row is None:
                    unknown.append(fb_id)
                else:
                    rows.append(dict(row))
            if unknown:
                click.echo(f"Unknown listing(s): {', '.join(unknown)}")
        else:
            _pending, rows = fetch_pending_photos(
                conn,
                registry,
                photo_analysis_version=_photo_analysis_version(_raw_registry()),
            )
            if not fetch_all:
                rows = rows[:limit]
    finally:
        conn.close()

    targets: list[tuple[str, list[str]]] = []
    on_disk = 0
    for row in rows:
        fb_id = str(row["fb_id"])
        if photos.photos_hash(fb_id) is not None:
            on_disk += 1
            continue
        targets.append((fb_id, gallery_urls_of(row.get("raw_json"))))

    if on_disk:
        click.echo(f"{on_disk} listing(s) already have their photos on disk.")
    if not targets:
        click.echo("Nothing to fetch.")
        return

    playwright = start_playwright()
    context = None
    fetched = 0
    no_urls: list[str] = []
    failed: list[str] = []
    try:
        try:
            context = launch_persistent(playwright, headless=True)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)

        # The CDN links are signed for this account: they resolve through the
        # logged-in context's request client and nowhere else.
        if not is_logged_in(context):
            _echo_err("No active Facebook session. Run `rent-assistant login` first.")
            sys.exit(3)

        with click.progressbar(
            targets,
            label="Fetching galleries",
            show_pos=True,
            item_show_func=lambda t: t[0] if t else "",
        ) as bar:
            for fb_id, urls in bar:
                if not urls:
                    no_urls.append(fb_id)
                    continue
                if photos.download_and_resize(context, urls, fb_id):
                    fetched += 1
                else:
                    failed.append(fb_id)
                time.sleep(
                    random.uniform(
                        config.PHOTO_FETCH_DELAY_MIN_SEC,
                        config.PHOTO_FETCH_DELAY_MAX_SEC,
                    )
                )
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            playwright.stop()

    click.echo(f"Fetched galleries for {fetched} listing(s).")
    if no_urls:
        click.echo(
            f"No stored gallery links for {len(no_urls)} listing(s) — their "
            "detail page was read before photos were collected. Re-read it: "
            f"rent-assistant scrape --ids {' --ids '.join(no_urls[:5])}"
            + (" ..." if len(no_urls) > 5 else "")
        )
    if failed:
        click.echo(
            f"Every link expired for {len(failed)} listing(s) — re-read the "
            f"page: rent-assistant scrape --ids {' --ids '.join(failed[:5])}"
            + (" ..." if len(failed) > 5 else "")
        )


@main.command("photo-hash")
@click.option(
    "--limit",
    default=100,
    show_default=True,
    help="Maximum number of listings to hash.",
)
@click.option(
    "--all",
    "hash_all",
    is_flag=True,
    default=False,
    help="Hash every listing in the queue, ignoring --limit.",
)
@click.option(
    "--ids",
    "ids",
    multiple=True,
    help="Hash these fb_ids instead of the queue (repeatable).",
)
def photo_hash_cmd(limit: int, hash_all: bool, ids: tuple[str, ...]) -> None:
    """Backfill the photo hashes of listings scraped before the hash stage.

    Only the first few shots are fetched, and nothing lands on disk: these
    hashes are the duplicate signal, not the photo pass. The stored gallery
    links are signed and expire in about a week, so a row whose every link
    is gone is past that edge and only its detail page can help
    (`rent-assistant scrape --ids <fb_id>`).
    """
    if limit <= 0 and not hash_all:
        raise click.ClickException("--limit must be a positive number")

    conn = _open_db()
    try:
        rows: list[dict[str, Any]] = []
        if ids:
            unknown: list[str] = []
            for fb_id in dict.fromkeys(str(i) for i in ids):
                row = conn.execute(
                    "SELECT * FROM listings WHERE fb_id = ?", (fb_id,)
                ).fetchone()
                if row is None:
                    unknown.append(fb_id)
                else:
                    rows.append(dict(row))
            if unknown:
                click.echo(f"Unknown listing(s): {', '.join(unknown)}")
        else:
            rows = listings_missing_photo_hashes(conn)
            if not hash_all:
                rows = rows[:limit]

        targets = [
            (str(row["fb_id"]), gallery_urls_of(row.get("raw_json"))) for row in rows
        ]
        if not targets:
            click.echo("Nothing to hash.")
            return

        playwright = start_playwright()
        context = None
        hashed = 0
        no_urls: list[str] = []
        failed: list[str] = []
        try:
            try:
                context = launch_persistent(playwright, headless=True)
            except Exception as exc:  # noqa: BLE001 — surface a friendly error
                _echo_err(f"Failed to launch browser: {exc}")
                sys.exit(2)

            # The CDN links are signed for this account: they resolve through
            # the logged-in context's request client and nowhere else.
            if not is_logged_in(context):
                _echo_err("No active Facebook session. Run `rent-assistant login` first.")
                sys.exit(3)

            with click.progressbar(
                targets,
                label="Hashing photos",
                show_pos=True,
                item_show_func=lambda t: t[0] if t else "",
            ) as bar:
                for fb_id, urls in bar:
                    if not urls:
                        no_urls.append(fb_id)
                        continue
                    entries = fingerprint.fetch_fingerprints(context, urls)
                    if entries:
                        save_photo_hashes(conn, fb_id, entries)
                        hashed += 1
                    else:
                        failed.append(fb_id)
                    time.sleep(
                        random.uniform(
                            config.PHOTO_FETCH_DELAY_MIN_SEC,
                            config.PHOTO_FETCH_DELAY_MAX_SEC,
                        )
                    )
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                playwright.stop()
    finally:
        conn.close()

    click.echo(f"Hashed photos for {hashed} listing(s).")
    if no_urls:
        click.echo(
            f"No stored gallery links for {len(no_urls)} listing(s) — their "
            "detail page was read before photos were collected. Re-read it: "
            f"rent-assistant scrape --ids {' --ids '.join(no_urls[:5])}"
            + (" ..." if len(no_urls) > 5 else "")
        )
    if failed:
        click.echo(
            f"Every link expired for {len(failed)} listing(s) — re-read the "
            f"page: rent-assistant scrape --ids {' --ids '.join(failed[:5])}"
            + (" ..." if len(failed) > 5 else "")
        )


@main.command("photo-batch")
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
    help="Include every listing awaiting a photo pass, ignoring --limit.",
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
    help=(
        f"Write the batch to this file (default: {config.AI_DIR}/"
        "photo-batch-<ts>.json)."
    ),
)
def photo_batch_cmd(
    limit: int, all_pending: bool, ids: tuple[str, ...], out_path: Path | None
) -> None:
    """Assemble the JSON batch of listings awaiting a photo pass."""
    if limit <= 0 and not all_pending:
        raise click.ClickException("--limit must be a positive number")
    registry = _load_registry()
    wanted_version = _photo_analysis_version(_raw_registry())
    conn = _open_db()
    try:
        # A listing rejected by hand must not reach the batch, and
        # decisions.json is the source of truth for that verdict.
        _sync_decisions(conn)
        pending, no_photos = fetch_pending_photos(
            conn, registry, photo_analysis_version=wanted_version
        )

        selected = list(pending)
        if ids:
            wanted = {str(i) for i in ids}
            # An explicit id reaches past the queue, so a listing whose photos
            # were already read can be looked at again on request.
            queued = {str(row["fb_id"]): row for row in pending}
            bare = {str(row["fb_id"]) for row in no_photos}
            selected = []
            unusable: list[str] = []
            missing_ids: list[str] = []
            for fb_id in sorted(wanted):
                if fb_id in queued:
                    selected.append(queued[fb_id])
                    continue
                if fb_id in bare or photos.photos_hash(fb_id) is None:
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
                    "No photos on disk (run `rent-assistant photos-fetch` first): "
                    f"{', '.join(unusable)}"
                )
            if missing_ids:
                click.echo(f"Unknown listing(s): {', '.join(missing_ids)}")

        if no_photos and not ids:
            click.echo(
                f"{len(no_photos)} listing(s) have no photos on disk — "
                "run `rent-assistant photos-fetch` to download them."
            )

        if not selected:
            click.echo(
                "Nothing to analyse for the given --ids."
                if ids
                else "Nothing pending — every listing's photos are up to date."
            )
            return

        batch = photo_batch.build_photo_batch(
            conn,
            registry,
            listings=selected,
            # An explicit --ids list is the user's own selection: capping it
            # at --limit would drop listings they named.
            limit=None if (all_pending or ids) else limit,
            photo_analysis_version=wanted_version,
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
        written = photo_batch.write_photo_batch(batch)

    click.echo(f"Photo batch written: {written} ({len(batch['items'])} listing(s))")


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
        click.echo("  Add one with `rent-assistant ai-field add --key ... [--backfill]`.")

    for warning in report.warnings:
        _echo_err(f"warning: {warning}")


@main.command("photo-apply")
@click.argument(
    "path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate and report without writing anything to the DB.",
)
def photo_apply_cmd(path: Path, dry_run: bool) -> None:
    """Apply a photo result file to the local database."""
    registry = _load_registry()
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc

    conn = _open_db()
    try:
        try:
            report = photo_batch.apply_photo_results(
                conn, registry, result, dry_run=dry_run
            )
        except photo_batch.BatchError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()

    prefix = "Dry-run: would apply" if dry_run else "Applied"
    click.echo(f"{prefix} {report.applied} photo analysis/analyses")

    # The disagreements are the point of the pass: a photo that merely
    # confirms the text changes nothing the user needs to look at.
    if report.overrides:
        click.echo(f"Photos disagreed with the text in {len(report.overrides)}:")
        for fb_id, key, old, new in report.overrides:
            click.echo(f"  - {fb_id}.{key}: {old} -> {new}")

    if report.rejected:
        click.echo(f"Newly auto-rejected {len(report.rejected)}:")
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
        click.echo(
            "  Add one with `rent-assistant ai-field add --key ... --source photo "
            "[--backfill]`."
        )

    for warning in report.warnings:
        _echo_err(f"warning: {warning}")


# --- pets policy per building -------------------------------------------


def _sync_buildings(
    conn: sqlite3.Connection, store: dict[str, Any] | None = None
) -> None:
    """Refresh the buildings mirror the exports JOIN against.

    ``data/buildings.json`` is hand-editable in the same way decisions are —
    an alias is added there by hand — so a bad policy or status in it is a
    user error to report, not a traceback.
    """
    try:
        sync_buildings_to_db(conn, store)
    except (buildings.BuildingError, ValueError) as exc:
        raise click.ClickException(f"{config.BUILDINGS_PATH}: {exc}") from exc


def _load_buildings() -> dict[str, Any]:
    try:
        return buildings.load_buildings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _find_building(
    store: dict[str, Any], key: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Look a building up by its key, by its name or by one of its aliases."""
    if key in store:
        return key, store[key]
    entry = buildings.resolve(store, key)
    if entry is None:
        return None, None
    for stored_key, stored in store.items():
        if stored is entry:
            return stored_key, stored
    return None, None


def _affected_listings(
    conn: sqlite3.Connection, store: dict[str, Any]
) -> dict[str, int]:
    """How many still-active listings each stored building covers."""
    return {
        row["key"]: row["listings"]
        for row in fetch_pending_buildings(conn, store, refresh=True)
    }


@main.command("buildings-batch")
@click.option(
    "--limit",
    type=int,
    default=25,
    show_default=True,
    help="Maximum number of buildings to put in the batch.",
)
@click.option(
    "--key",
    "keys",
    multiple=True,
    help="Research only these buildings (repeatable); a name or a key.",
)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Include buildings already in the store (they are skipped by default).",
)
def buildings_batch_cmd(limit: int, keys: tuple[str, ...], refresh: bool) -> None:
    """Assemble the JSON batch of buildings awaiting a pets-policy lookup."""
    if limit <= 0:
        raise click.ClickException("--limit must be a positive number")

    store = _load_buildings()
    conn = _open_db()
    try:
        # The queue drops the buildings whose every ad the user rejected, and
        # decisions.json is the source of truth for that verdict.
        _sync_decisions(conn)
        batch = buildings_batch.build_buildings_batch(
            conn,
            store,
            limit=limit,
            keys=list(keys) or None,
            refresh=refresh,
        )
    finally:
        conn.close()

    if not batch["buildings"]:
        click.echo(
            "No such building in the queue."
            if keys
            else "Nothing to research — every building in the active set is "
            "already in the store."
        )
        return

    written = buildings_batch.write_buildings_batch(batch)
    click.echo(
        f"Buildings batch written: {written} "
        f"({len(batch['buildings'])} building(s))"
    )


@main.command("buildings-apply")
@click.argument(
    "path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate and report without writing anything.",
)
def buildings_apply_cmd(path: Path, dry_run: bool) -> None:
    """Store a research result file as proposals awaiting confirmation.

    Nothing this command writes can reject a listing: every entry lands as
    `proposed`, and only `rent-assistant buildings set` makes a policy act.
    """
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc

    conn = _open_db()
    try:
        try:
            report = buildings_batch.apply_buildings_results(
                conn, None, result, dry_run=dry_run
            )
        except buildings_batch.BatchError as exc:
            raise click.ClickException(str(exc)) from exc
        except buildings.BuildingError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()

    prefix = "Dry-run: would propose" if dry_run else "Proposed"
    click.echo(f"{prefix} {report.applied} building(s)")
    for proposal in report.proposed:
        key = proposal["key"]
        confidence = proposal.get("confidence") or "?"
        click.echo(
            f"  - {proposal.get('name') or key} [{key}]: "
            f"{proposal['policy']} ({confidence}), "
            f"{report.affected.get(key, 0)} listing(s)"
        )

    if report.skipped:
        click.echo(f"Skipped {len(report.skipped)}: {', '.join(report.skipped)}")

    for warning in report.warnings:
        _echo_err(f"warning: {warning}")

    if report.proposed and not dry_run:
        click.echo(
            "These are proposals and change nothing yet — confirm one with "
            "`rent-assistant buildings set <key> --policy <policy>`."
        )


@main.group("buildings")
def buildings_group() -> None:
    """Review and confirm the pets policy researched per building."""


@buildings_group.command("list")
@click.option(
    "--status",
    type=click.Choice(sorted(buildings.STATUSES)),
    default=None,
    help="Show only proposals, or only what the user confirmed.",
)
@click.option(
    "--policy",
    type=click.Choice(sorted(buildings.POLICIES)),
    default=None,
    help="Show only buildings with this pets policy.",
)
def buildings_list_cmd(status: str | None, policy: str | None) -> None:
    """Print every researched building with its evidence and affected rows."""
    store = _load_buildings()
    conn = _open_db()
    try:
        _sync_decisions(conn)
        affected = _affected_listings(conn, store)
    finally:
        conn.close()

    selected = [
        (key, entry)
        for key, entry in sorted(store.items())
        if (status is None or entry.get("status") == status)
        and (policy is None or entry.get("policy") == policy)
    ]
    if not selected:
        click.echo(f"No buildings in {config.BUILDINGS_PATH} match.")
        return

    for key, entry in selected:
        click.echo(
            f"{entry.get('name') or key} [{key}]: {entry.get('policy')} "
            f"/ {entry.get('status')} "
            f"({entry.get('confidence') or '?'}), "
            f"{affected.get(key, 0)} listing(s)"
        )
        if entry.get("area"):
            click.echo(f"    District: {entry['area']}")
        if entry.get("aliases"):
            click.echo(f"    Aliases: {', '.join(entry['aliases'])}")
        if entry.get("note"):
            click.echo(f"    {entry['note']}")
        for source in entry.get("sources") or []:
            if not isinstance(source, dict):
                continue
            click.echo(f"    - {source.get('title') or ''} {source.get('url') or ''}")
            if source.get("quote"):
                click.echo(f"      «{source['quote']}»")
    click.echo(f"{len(selected)} building(s) in {config.BUILDINGS_PATH}")


@buildings_group.command("conflicts")
@click.option(
    "--min-signals",
    type=int,
    default=2,
    show_default=True,
    help="How many ads must claim 'pets allowed' before the building is listed.",
)
def buildings_conflicts_cmd(min_signals: int) -> None:
    """Print buildings whose ads keep contradicting a not_allowed house rule.

    These listings are not rejected — an ad saying "pets welcome" survives
    the building's rule on purpose. What the pile means is that the research
    may have read the wrong tower, so it is worth a look by hand before the
    policy is trusted.
    """
    store = _load_buildings()
    conn = _open_db()
    try:
        _sync_decisions(conn)
        conflicts = fetch_pets_conflicts(conn, store, min_signals=min_signals)
    finally:
        conn.close()

    if not conflicts:
        click.echo(
            f"No building where {min_signals}+ ads argue with the house rule."
        )
        return

    for conflict in conflicts:
        click.echo(
            f"{conflict['name']} [{conflict['key']}]: "
            f"{conflict['policy']} / {conflict['status']}, "
            f"{len(conflict['listings'])} ads promise pets"
        )
        for listing in conflict["listings"]:
            price = listing.get("price_thb")
            price_str = f"{price} THB" if price else "price not stated"
            click.echo(
                f"    - {listing['fb_id']} {price_str} "
                f"{listing.get('title') or ''}"
            )
            if listing.get("url"):
                click.echo(f"      {listing['url']}")
    click.echo(f"{len(conflicts)} building(s) to look at by hand")


@buildings_group.command("set")
@click.argument("key")
@click.option(
    "--policy",
    required=True,
    type=click.Choice(sorted(buildings.POLICIES)),
    help="The pets rule the user confirms for this building.",
)
@click.option("--note", default=None, help="A note to keep with the confirmation.")
@click.option(
    "--alias",
    "aliases",
    multiple=True,
    help="Another spelling naming this same building (repeatable).",
)
def buildings_set_cmd(
    key: str, policy: str, note: str | None, aliases: tuple[str, ...]
) -> None:
    """Confirm a building's pets policy; only a confirmed one can reject.

    KEY is the stored key, the building's name or any of its aliases.
    """
    store = _load_buildings()
    stored_key, entry = _find_building(store, key)
    if entry is None:
        raise click.ClickException(
            f"unknown building {key!r} — `rent-assistant buildings list` shows what "
            "research proposed"
        )

    try:
        saved = buildings.save_building(
            None,
            str(entry.get("name") or stored_key),
            key=stored_key,
            policy=policy,
            status="confirmed",
            aliases=list(aliases) or None,
            note=note,
        )
    except buildings.BuildingError as exc:
        raise click.ClickException(str(exc)) from exc

    store[stored_key] = saved
    conn = _open_db()
    try:
        _sync_decisions(conn)
        _sync_buildings(conn, store)
        affected = _affected_listings(conn, store).get(stored_key, 0)
        # An ad that says "pets allowed" outright survives the rule, so
        # counting the building's coverage as rejections would overstate what
        # this confirmation does — and this is the number the user decides on.
        survivors = [
            len(c["listings"])
            for c in fetch_pets_conflicts(conn, store, min_signals=1)
            if c["key"] == stored_key
        ]
    finally:
        conn.close()

    kept = survivors[0] if survivors else 0
    click.echo(f"{saved['name']} [{stored_key}]: {policy} (confirmed)")
    if policy == "not_allowed":
        click.echo(f"  Rejects listings: {max(affected - kept, 0)}")
        if kept:
            click.echo(
                f"  Survive: {kept} — the ad promises pets despite the house "
                "rule, see `rent-assistant buildings conflicts`"
            )
    click.echo(
        f"Saved to {config.BUILDINGS_PATH}. Run `rent-assistant export` to refresh "
        "the table."
    )

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
        f"(analysis_version: {_analysis_version(raw)}, "
        f"photo_analysis_version: {_photo_analysis_version(raw)})"
    )
    for field in registry.fields:
        values = field.get("values")
        suffix = f" [{', '.join(values)}]" if values else ""
        click.echo(
            f"  {field['key']:<20} {field.get('group', ''):<9} "
            f"{field['type']:<5} {ai_schema.field_source(field):<5} "
            f"v{field['added_in']}  {field['label']}{suffix}"
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
@click.option(
    "--source",
    "source",
    default=ai_schema.DEFAULT_SOURCE,
    show_default=True,
    type=click.Choice(sorted(ai_schema.KNOWN_SOURCES)),
    help="Which pass fills the field: the text one, the photo one, or both.",
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
    source: str,
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
    if source != ai_schema.DEFAULT_SOURCE:
        # `text` is the assumed default, so writing it out would only add noise
        # to a registry where 28 fields already omit the key.
        field["source"] = source
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
    previous_photo = _photo_analysis_version(raw)
    raw["schema_version"] = new_version
    # A backfill moves only the version of the pass that can actually fill the
    # field: a photo-only field must not re-queue every listing for the text
    # pass, and `both` needs each pass to look again.
    moves_text = backfill and source in ("text", "both")
    moves_photo = backfill and source in ("photo", "both")
    raw[_ANALYSIS_VERSION_KEY] = new_version if moves_text else previous_analysis
    raw[_PHOTO_ANALYSIS_VERSION_KEY] = new_version if moves_photo else previous_photo

    _write_registry(target, raw)

    click.echo(
        f"Added {key!r} to {target} (schema_version {new_version - 1} -> "
        f"{new_version})"
    )
    if backfill:
        commands = []
        if moves_text:
            commands.append("`rent-assistant ai-batch`")
        if moves_photo:
            commands.append("`rent-assistant photo-batch`")
        click.echo(
            "Backfill requested: already-analysed listings are pending again — "
            f"run {' and '.join(commands)}."
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


def _record_decision(
    fb_id: str, status: str, reason: str | None, rating: float | None = None
) -> None:
    """Write one decision to the JSON file and mirror it into the DB."""
    try:
        entry = decisions.save_decision(None, fb_id, status, reason, rating=rating)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    conn = _open_db()
    try:
        _sync_decisions(conn)
    finally:
        conn.close()

    mark = f" [{entry['rating']:g}]" if entry.get("rating") is not None else ""
    suffix = f" — {entry['reason']}" if entry.get("reason") else ""
    click.echo(f"{fb_id}: {status}{mark}{suffix}")
    click.echo(f"Saved to {config.DECISIONS_PATH}. Run `rent-assistant export` to refresh the table.")


@main.command("check-alive")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    default=("shortlist", "longlist"),
    show_default=True,
    help="Only check listings currently in these tabs (repeatable).",
)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Reject listings FB confirms are gone (default: report only).",
)
@click.option(
    "--reason",
    default="no longer listed",
    show_default=True,
    help="Reason recorded on the auto-reject.",
)
def check_alive_cmd(statuses: tuple[str, ...], do_apply: bool, reason: str) -> None:
    """Re-visit shortlist/longlist listings and flag ones FB no longer serves.

    This is a liveness probe, not a re-scrape: it never writes a stored
    field, only (with ``--apply``) a rejection decision — `scrape --ids`
    is the wrong tool here because a dead page's re-read can overwrite the
    description and gallery of a *different* listing. A page that times out
    or half-renders is left alone: only an actual "content isn't available"
    match from FB counts as discontinued.
    """
    wanted = set(statuses)
    conn = _open_db()
    try:
        rows = [r for r in fetch_enriched(conn) if r["status"] in wanted]
    finally:
        conn.close()

    if not rows:
        click.echo(f"Nothing to check in {', '.join(sorted(wanted))}.")
        return

    click.echo(f"Checking {len(rows)} listing(s) in {', '.join(sorted(wanted))}...")

    playwright = start_playwright()
    context = None
    dead: list[tuple[str, str]] = []
    inconclusive: list[str] = []
    try:
        try:
            context = launch_persistent(playwright, headless=True)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)

        if not is_logged_in(context):
            _echo_err("No active Facebook session. Run `rent-assistant login` first.")
            sys.exit(3)

        for idx, row in enumerate(rows):
            fb_id = str(row["fb_id"])
            url = row.get("url") or marketplace_url(fb_id)
            title = row.get("title") or fb_id
            if idx:
                time.sleep(
                    random.uniform(
                        config.DETAIL_DELAY_MIN_SEC, config.DETAIL_DELAY_MAX_SEC
                    )
                )
            try:
                alive = check_listing_alive(context, url)
            except LoginRequiredError as exc:
                _echo_err(str(exc))
                _echo_err(
                    f"Stopped after {idx} of {len(rows)} — run `rent-assistant login` "
                    "and retry for the rest."
                )
                break
            if alive is False:
                dead.append((fb_id, title))
                click.echo(f"  [{idx + 1}/{len(rows)}] gone: {title} ({fb_id})")
            elif alive is None:
                inconclusive.append(fb_id)
                click.echo(f"  [{idx + 1}/{len(rows)}] inconclusive: {title} ({fb_id})")
            else:
                click.echo(f"  [{idx + 1}/{len(rows)}] alive: {title} ({fb_id})")
    finally:
        if context is not None:
            context.close()
        playwright.stop()

    checked = len(dead) + len(inconclusive)
    click.echo(
        f"\n{len(dead)} confirmed gone, {len(inconclusive)} inconclusive "
        f"(left untouched), {len(rows) - checked} still alive."
    )

    if not dead:
        return

    if not do_apply:
        click.echo("Dry run — rerun with --apply to auto-reject these:")
        for fb_id, title in dead:
            click.echo(f"  - {fb_id}: {title}")
        return

    conn = _open_db()
    try:
        for fb_id, _title in dead:
            decisions.save_decision(None, fb_id, "rejected", reason)
        _sync_decisions(conn)
    finally:
        conn.close()
    click.echo(
        f"Auto-rejected {len(dead)} listing(s). Run `rent-assistant export` to refresh the table."
    )


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


@main.command("rate")
@click.argument("fb_id")
@click.argument("rating", type=float)
@click.option("--reason", default=None, help="A note to keep with the mark.")
def rate_cmd(fb_id: str, rating: float, reason: str | None) -> None:
    """Score a listing 1-5; the mark decides its status.

    4.5 and up go to the shortlist, 3 and below are rejected, and what falls
    between goes to the longlist — seen, kept to hand, and off the active set,
    which is the queue of what has not been looked at yet.
    """
    status = decisions.status_for_rating(rating)
    _record_decision(fb_id, status, reason, rating)


def _resolve_posting(
    conn: sqlite3.Connection, fb_id: str, posting_fb_id: str | None
) -> str:
    """Which specific ad(s) a stage is about, validated against this row's group.

    ``--posting`` may name more than one id, comma-separated — dedupe can
    fold many postings into one row, and you may have written to several of
    them without writing to the rest. Defaults to whatever a previous stage
    already recorded for ``fb_id`` (so ``schedule-viewing`` and
    ``mark-viewed`` do not need ``--posting`` repeated once ``contact`` set
    it), and beyond that to ``fb_id`` itself.

    Each id must be ``fb_id`` itself or one of the postings ``rent-assistant
    dedupe`` has already merged into it — the same ids the table's ID cell
    lists — so a typo cannot silently point the stage at some other listing
    entirely. A merged-away posting need not have its own row in ``listings``
    (an exact repost is recorded in ``duplicates`` at write time and never
    gets one), so membership is checked against the duplicates group first,
    and only a name that matches neither is reported as "no such listing".
    """
    raw = posting_fb_id
    if not raw:
        existing = decisions.load_decisions(None).get(str(fb_id)) or {}
        raw = existing.get("stage_fb_id") or fb_id

    known = {d["fb_id"] for d in duplicates_by_canonical(conn).get(fb_id, [])}
    resolved: list[str] = []
    for target in dict.fromkeys(p.strip() for p in raw.split(",") if p.strip()):
        if target == fb_id or target in known:
            resolved.append(target)
            continue
        if conn.execute(
            "SELECT 1 FROM listings WHERE fb_id = ?", (target,)
        ).fetchone():
            where = f" ({', '.join(sorted(known))})" if known else " — dedupe has not merged anything into this row"
            raise click.ClickException(
                f"{target} is not one of {fb_id}'s known postings{where}. "
                "Check the ID cell, or omit --posting to use the row itself."
            )
        raise click.ClickException(f"{target}: no such listing")

    if not resolved:
        raise click.ClickException("--posting named nothing")
    return ",".join(resolved)


def _record_stage(
    fb_id: str,
    stage: str,
    *,
    posting_fb_id: str | None,
    viewing_at: str | None,
    note: str | None,
) -> None:
    """Write one stage to the JSON file and mirror it into the DB."""
    conn = _open_db()
    try:
        target = _resolve_posting(conn, fb_id, posting_fb_id)
        try:
            decisions.save_stage(
                None,
                fb_id,
                stage,
                posting_fb_id=target,
                viewing_at=viewing_at,
                note=note,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _sync_decisions(conn)
    finally:
        conn.close()

    label = decisions.STAGE_LABELS[stage]
    when = f" — {viewing_at}" if viewing_at else ""
    note_suffix = f" ({note})" if note else ""
    postings_word = "posting" if "," not in target else "postings"
    click.echo(f"{fb_id}: {label}, {postings_word} {target}{when}{note_suffix}")
    click.echo(f"Saved to {config.DECISIONS_PATH}. Run `rent-assistant export` to refresh the table.")


@main.command("contact")
@click.argument("fb_id")
@click.option(
    "--posting",
    "posting_fb_id",
    default=None,
    help="Which merged posting(s) you actually wrote to, comma-separated (default: fb_id itself).",
)
@click.option("--note", default=None, help="A note to keep with the stage.")
def contact_cmd(fb_id: str, posting_fb_id: str | None, note: str | None) -> None:
    """Record that you wrote to the landlord/agent about this listing.

    Independent of status/shortlist: rejecting the listing later does not
    erase this. Moves the row to the "In progress" tab.
    """
    _record_stage(fb_id, "written", posting_fb_id=posting_fb_id, viewing_at=None, note=note)


@main.command("schedule-viewing")
@click.argument("fb_id")
@click.argument("at")
@click.option(
    "--posting",
    "posting_fb_id",
    default=None,
    help="Which merged posting(s) the viewing is about, comma-separated (default: whatever `contact` recorded, or fb_id itself).",
)
@click.option("--note", default=None, help="A note to keep with the stage.")
def schedule_viewing_cmd(
    fb_id: str, at: str, posting_fb_id: str | None, note: str | None
) -> None:
    """Record that a viewing is set up, and when (free text, e.g. "2026-09-25 14:00")."""
    _record_stage(fb_id, "viewing_scheduled", posting_fb_id=posting_fb_id, viewing_at=at, note=note)


@main.command("mark-viewed")
@click.argument("fb_id")
@click.option(
    "--posting",
    "posting_fb_id",
    default=None,
    help="Which merged posting(s) were viewed, comma-separated (default: whatever the prior stage recorded, or fb_id itself).",
)
@click.option("--note", default=None, help="A note to keep with the stage.")
def mark_viewed_cmd(fb_id: str, posting_fb_id: str | None, note: str | None) -> None:
    """Record that the viewing happened; not rejected, waiting to be compared."""
    _record_stage(fb_id, "viewed", posting_fb_id=posting_fb_id, viewing_at=None, note=note)


@main.command("clear-stage")
@click.argument("fb_id")
def clear_stage_cmd(fb_id: str) -> None:
    """Drop the written/viewing/viewed stage, leaving status untouched."""
    conn = _open_db()
    try:
        entry = decisions.clear_stage(None, fb_id)
        if entry is None:
            raise click.ClickException(f"{fb_id}: no decision recorded for it")
        _sync_decisions(conn)
    finally:
        conn.close()
    click.echo(f"{fb_id}: stage cleared")
    click.echo(f"Saved to {config.DECISIONS_PATH}. Run `rent-assistant export` to refresh the table.")


@main.group("messages")
def messages_group() -> None:
    """Messenger outreach: sync conversations, send first contact, check status."""


_PROFILE_HELP = (
    "Use a second account's own browser profile (own cookies, never mixed "
    "with the default one) — e.g. --profile partner. Omit for the main "
    "account. Which account messages which listing is always your own "
    "call, made through --ids/--status each time; nothing here picks it "
    "automatically."
)


@messages_group.command("sync")
@click.option(
    "--since",
    default=None,
    help=f"Skip threads older than this date (default: {config.MESSAGES_SINCE}).",
)
@click.option(
    "--limit", default=None, type=int, help="Stop after this many threads."
)
@click.option("--profile", default=None, help=_PROFILE_HELP)
def messages_sync_cmd(since: str | None, limit: int | None, profile: str | None) -> None:
    """Pull Marketplace Messenger threads into the DB.

    Never opens a thread whose relative age in the thread list cannot be
    positively dated on or after the cutoff, unless it is already known
    (synced before, or in the send log) — see docs/messages.md. A thread
    with a new, unclassified inbound reply is reported but not flagged
    ``needs_action`` here; that is ``messages classify-apply``'s call.
    """
    cutoff = since or config.MESSAGES_SINCE
    profile_dir = config.browser_profile_dir(profile)

    playwright = start_playwright()
    context = None
    synced = 0
    skipped_old = 0
    new_replies = 0
    unmatched = 0
    try:
        try:
            context = launch_persistent(playwright, headless=True, profile_dir=profile_dir)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)

        if not is_logged_in(context):
            login_hint = f"rent-assistant login --profile {profile}" if profile else "rent-assistant login"
            _echo_err(f"No active Facebook session. Run `{login_hint}` first.")
            sys.exit(3)

        conn = _open_db()
        try:
            known_thread_ids = {
                str(row["thread_id"])
                for row in conn.execute("SELECT thread_id FROM conversations").fetchall()
            }

            try:
                threads = list_marketplace_threads(context)
            except LoginRequiredError as exc:
                _echo_err(str(exc))
                sys.exit(3)
            except MessengerPinRequiredError as exc:
                _echo_err(str(exc))
                sys.exit(4)

            candidates = []
            for t in threads:
                age_iso = messages_module.parse_thread_age(t.get("last_activity_raw"))
                recent = messages_module.is_on_or_after_cutoff(age_iso, cutoff)
                if not recent and t["thread_id"] not in known_thread_ids:
                    skipped_old += 1
                    continue
                candidates.append(t)
            if limit is not None:
                candidates = candidates[:limit]

            reposts = duplicates_by_canonical(conn)
            canonical_by_posting: dict[str, str] = {}
            for canonical_id, postings in reposts.items():
                for posting in postings:
                    canonical_by_posting[str(posting["fb_id"])] = canonical_id

            for idx, t in enumerate(candidates):
                if idx:
                    time.sleep(
                        random.uniform(
                            config.DETAIL_DELAY_MIN_SEC, config.DETAIL_DELAY_MAX_SEC
                        )
                    )
                try:
                    detail = read_thread(context, t["thread_id"])
                except LoginRequiredError as exc:
                    _echo_err(str(exc))
                    _echo_err(
                        f"Stopped after {idx} of {len(candidates)} — run "
                        "`rent-assistant login` and retry for the rest."
                    )
                    break
                except MessengerPinRequiredError as exc:
                    _echo_err(str(exc))
                    break
                if detail is None:
                    continue

                posting_fb_id = detail.get("listing_fb_id")
                fb_id = (
                    canonical_by_posting.get(posting_fb_id, posting_fb_id)
                    if posting_fb_id
                    else None
                )

                thread_messages = detail["messages"]
                has_outbound = any(m["direction"] == "out" for m in thread_messages)
                has_inbound = any(m["direction"] == "in" for m in thread_messages)
                reply_hash = (
                    messages_module.reply_hash_of(thread_messages) if has_inbound else None
                )
                state, needs_action, reason = messages_module.derive_state(
                    has_outbound, has_inbound, None
                )

                if fb_id is None:
                    needs_action = True
                    reason = "no_listing_match"
                    unmatched += 1
                elif has_inbound:
                    new_replies += 1

                save_conversation(
                    conn,
                    thread_id=t["thread_id"],
                    fb_id=fb_id,
                    posting_fb_id=posting_fb_id,
                    peer_name=detail.get("peer_name") or t.get("peer_name"),
                    messages=thread_messages,
                    state=state,
                    needs_action=needs_action,
                    needs_action_reason=reason,
                    reply_hash=reply_hash,
                )
                synced += 1
                click.echo(f"  [{idx + 1}/{len(candidates)}] {t['thread_id']}: {state}")
        finally:
            conn.close()
    finally:
        if context is not None:
            context.close()
        playwright.stop()

    click.echo(
        f"\nSynced {synced} thread(s), skipped {skipped_old} too old, "
        f"{new_replies} with a new unclassified reply, "
        f"{unmatched} not matched to a listing."
    )
    if new_replies:
        click.echo("Run `rent-assistant messages classify-batch` to read the new replies.")


def _selection_rows(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...],
    district: str | None,
    ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows = fetch_enriched(conn)
    if ids:
        wanted = set(ids)
        return [r for r in rows if str(r["fb_id"]) in wanted]
    selected = [r for r in rows if r["status"] in set(statuses)]
    if district:
        needle = district.strip().lower()
        selected = [
            r
            for r in selected
            if needle in (normalise_district(r.get("district")) or "").lower()
            or needle in (rules.value_of(r.get("fields") or {}, "area") or "").lower()
        ]
    return selected


@messages_group.command("send")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    default=("shortlist",),
    show_default=True,
    help="Only listings currently in these tabs (repeatable).",
)
@click.option("--district", default=None, help="Filter by district/area substring.")
@click.option("--ids", "ids", multiple=True, help="Specific fb_id(s), bypassing --status.")
@click.option(
    "--cap",
    default=config.MESSAGES_SEND_CAP,
    show_default=True,
    help="Stop after sending this many messages.",
)
@click.option(
    "--apply", "do_apply", is_flag=True, default=False, help="Actually send (default: show drafts only)."
)
@click.option(
    "--yes", "skip_confirm", is_flag=True, default=False, help="Skip the batch confirmation prompt."
)
@click.option("--profile", default=None, help=_PROFILE_HELP)
def messages_send_cmd(
    statuses: tuple[str, ...],
    district: str | None,
    ids: tuple[str, ...],
    cap: int,
    do_apply: bool,
    skip_confirm: bool,
    profile: str | None,
) -> None:
    """Send the first-contact message to a selection of listings.

    Dry-run by default: prints every draft and sends nothing. With
    ``--apply``, asks for one confirmation covering the whole batch, then
    sends with jitter and a hard cap. A listing already written to (stage
    ``written``+, or a prior entry in the send log, or an existing outbound
    message in a synced conversation) is skipped, not re-sent.
    """
    profile_dir = config.browser_profile_dir(profile)
    conn = _open_db()
    try:
        rows = _selection_rows(conn, statuses=statuses, district=district, ids=ids)
        convo_by_fb_id = conversations_by_fb_id(conn)

        drafts: list[tuple[dict[str, Any], str]] = []
        skipped: list[tuple[str, str]] = []
        for row in rows:
            fb_id = str(row["fb_id"])
            stage = row.get("stage")
            if stage in ("written", "viewing_scheduled", "viewed"):
                skipped.append((fb_id, f"stage already {stage}"))
                continue
            if messages_module.already_sent(fb_id, fb_id, "first_contact"):
                skipped.append((fb_id, "already in send log"))
                continue
            convo = convo_by_fb_id.get(fb_id)
            if convo and convo.get("last_direction") is not None:
                skipped.append((fb_id, "conversation already exists"))
                continue
            drafts.append((row, messages_module.compose_first_message(row)))
            if len(drafts) >= cap:
                break
    finally:
        conn.close()

    if skipped:
        click.echo(f"Skipping {len(skipped)} already-contacted listing(s):")
        for fb_id, why in skipped:
            click.echo(f"  - {fb_id}: {why}")

    if not drafts:
        click.echo("Nothing to send.")
        return

    click.echo(f"\n{len(drafts)} draft(s):")
    for row, text in drafts:
        title = row.get("title") or row["fb_id"]
        price = row.get("price_thb")
        district_label = normalise_district(row.get("district")) or "?"
        click.echo(f"\n--- {row['fb_id']}: {title} ({price} THB, {district_label}) ---")
        click.echo(text)

    if not do_apply:
        click.echo("\nDry run — rerun with --apply to send these.")
        return

    account_label = f" as --profile {profile}" if profile else ""
    if not skip_confirm and not click.confirm(
        f"\nSend these {len(drafts)} message(s) now{account_label}?"
    ):
        click.echo("Cancelled.")
        return

    playwright = start_playwright()
    context = None
    sent = 0
    failed: list[str] = []
    try:
        try:
            context = launch_persistent(playwright, headless=True, profile_dir=profile_dir)
        except Exception as exc:  # noqa: BLE001 — surface a friendly error
            _echo_err(f"Failed to launch browser: {exc}")
            sys.exit(2)

        if not is_logged_in(context):
            login_hint = f"rent-assistant login --profile {profile}" if profile else "rent-assistant login"
            _echo_err(f"No active Facebook session. Run `{login_hint}` first.")
            sys.exit(3)

        for idx, (row, text) in enumerate(drafts):
            fb_id = str(row["fb_id"])
            url = row.get("url") or marketplace_url(fb_id)
            if idx:
                time.sleep(
                    random.uniform(
                        config.MESSAGE_SEND_DELAY_MIN_SEC,
                        config.MESSAGE_SEND_DELAY_MAX_SEC,
                    )
                )
            try:
                ok = send_first_message(context, url, text)
            except LoginRequiredError as exc:
                _echo_err(str(exc))
                _echo_err(f"Stopped after {sent} of {len(drafts)} sent.")
                break
            except MessengerPinRequiredError as exc:
                _echo_err(str(exc))
                break
            except MessagingLimitReachedError as exc:
                _echo_err(str(exc))
                _echo_err(
                    f"Stopped after {sent} of {len(drafts)} sent — Facebook's "
                    "own daily cap, not this tool's. Re-run the same "
                    "selection tomorrow; already-sent listings are skipped."
                )
                break
            except Exception as exc:  # noqa: BLE001 — one listing's DOM
                # hiccup (a stray overlay, a slow render) must not take the
                # rest of the batch down with it; report and move on.
                failed.append(fb_id)
                click.echo(f"  [{idx + 1}/{len(drafts)}] FAILED: {fb_id} ({exc})")
                continue
            if not ok:
                failed.append(fb_id)
                click.echo(f"  [{idx + 1}/{len(drafts)}] FAILED: {fb_id}")
                continue

            messages_module.append_send_log(
                fb_id, fb_id, "first_contact", text, profile=profile or "default"
            )
            db_conn = _open_db()
            try:
                target = _resolve_posting(db_conn, fb_id, fb_id)
                decisions.save_stage(
                    None,
                    fb_id,
                    "written",
                    posting_fb_id=target,
                    note="auto: first message",
                    profile=profile or "default",
                )
                _sync_decisions(db_conn)
            finally:
                db_conn.close()
            sent += 1
            click.echo(f"  [{idx + 1}/{len(drafts)}] sent: {fb_id}")
    finally:
        if context is not None:
            context.close()
        playwright.stop()

    click.echo(f"\nSent {sent} of {len(drafts)}, {len(failed)} failed.")
    if sent:
        click.echo("Run `rent-assistant export` to refresh the table.")


@messages_group.command("status")
def messages_status_cmd() -> None:
    """Report conversations that need your attention, without touching the network."""
    conn = _open_db()
    try:
        needs_action = needs_action_conversations(conn)
    finally:
        conn.close()

    if not needs_action:
        click.echo("Nothing needs your attention.")
        return

    click.echo(f"{len(needs_action)} conversation(s) need you:")
    for convo in needs_action:
        thread_url = f"https://www.facebook.com/messages/t/{convo['thread_id']}/"
        reason = convo.get("needs_action_reason") or "?"
        snippet = convo.get("last_snippet") or ""
        click.echo(
            f"  - {convo.get('fb_id') or '?'} ({convo.get('peer_name') or '?'}): "
            f"{reason} — \"{snippet}\" — {thread_url}"
        )


@messages_group.command("classify-batch")
@click.option("--limit", default=None, type=int, help="Cap how many threads to include.")
def messages_classify_batch_cmd(limit: int | None) -> None:
    """Write the batch file of threads with a new, unclassified reply."""
    conn = _open_db()
    try:
        pending = conversations_needing_classification(conn, limit=limit)
        if not pending:
            click.echo("Nothing to classify.")
            return
        items = []
        for convo in pending:
            msgs = conn.execute(
                "SELECT direction, text FROM conversation_messages WHERE thread_id = ? ORDER BY position",
                (convo["thread_id"],),
            ).fetchall()
            listing = conn.execute(
                "SELECT title FROM listings WHERE fb_id = ?", (convo.get("fb_id"),)
            ).fetchone()
            items.append(
                {
                    "thread_id": convo["thread_id"],
                    "reply_hash": convo["reply_hash"],
                    "fb_id": convo.get("fb_id"),
                    "listing_title": listing["title"] if listing else None,
                    "messages": [dict(m) for m in msgs],
                }
            )
    finally:
        conn.close()

    config.ensure_home()
    config.AI_DIR.mkdir(parents=True, exist_ok=True)
    path = ai_batch.timestamped_path(config.AI_DIR, "messages-batch", None)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(f"Wrote {len(items)} thread(s) to {path}")


@messages_group.command("classify-apply")
@click.argument("result_path", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, default=False, help="Report without writing.")
def messages_classify_apply_cmd(result_path: Path, dry_run: bool) -> None:
    """Apply classification facts written by the messages skill."""
    raw = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise click.ClickException("result file must be a JSON array")

    conn = _open_db()
    applied = 0
    stale = 0
    try:
        for item in raw:
            thread_id = item.get("thread_id")
            reply_hash = item.get("reply_hash")
            facts = item.get("facts") or {}
            if not thread_id or not reply_hash:
                continue
            if dry_run:
                click.echo(f"  would apply {thread_id}: {facts}")
                continue
            ok = apply_conversation_classification(conn, thread_id, reply_hash, facts)
            if ok:
                applied += 1
            else:
                stale += 1
                click.echo(f"  {thread_id}: stale reply_hash, skipped")
    finally:
        conn.close()

    if dry_run:
        return
    click.echo(f"Applied {applied}, skipped {stale} stale.")
    if applied:
        click.echo("Run `rent-assistant export` to refresh the table.")


if __name__ == "__main__":
    main()
