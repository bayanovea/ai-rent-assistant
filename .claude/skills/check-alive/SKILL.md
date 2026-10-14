---
name: check-alive
description: Check whether the shortlist/longlist listings are still live on Facebook, and drop what the author has already taken down — rent-assistant check-alive, then export. Triggers ONLY on an explicit request: "check the shortlist is still live", "what in the longlist is still alive", "check alive", "/check-alive". A non-empty shortlist/longlist is not a reason by itself — the pass costs one Facebook request per listing and runs when the user asks.
---

# Alive / gone — over the shortlist and longlist

This is a check of fact, not an AI opinion: a listing is either still up
on Facebook or the seller has taken it down. Only `shortlist` and
`longlist` are worth it — these are the rows the user has already picked
by hand and keeps at hand; nobody has looked at `active` yet, and
`rejected` is no longer needed.

**This is not a re-scrape.** `scrape --ids` re-reads the description and
the gallery, and on a dead page it damages the row with somebody else's
data (see `docs/dedupe.md`, "A dead page's `--ids` re-read damages the row"
in `CLAUDE.md`). The liveness check writes not a single listing field — it
either does nothing or moves the row to `rejected` through the ordinary
`reject`, the same place where the user's own decision goes.

**A timeout is not "gone".** Facebook returns recognizable text when a
listing is really deleted ("content isn't available" and variants); a page
that is merely slow to load or did not render proves nothing.
`check_listing_alive` returns three states: `True` (alive), `False`
(Facebook said outright that it is not) and `None` (unclear — leave as
is). Only `False` goes to `rejected`.

## 1. Run the check

```sh
rent-assistant check-alive
```

By default it checks `shortlist` and `longlist`. To narrow to one tab —
`--status shortlist`. Without `--apply` the command writes nothing: it only
prints `alive` / `gone` / `inconclusive` per listing and a summary at the
end — how many confirmed gone, how many unclear, how many still alive.

Between requests the command keeps the same 3–7 s pause that `scrape`
keeps between detail pages — this is somebody else's chat, not your own
enumeration script.

## 2. Look at what is proposed for rejection

Read the `gone` list from the output. If something looks off (for example,
suspiciously many in a row from one seller, or the session may have logged
out mid-run — then part of the "gone" is really "did not get through"),
re-check by hand: open `https://www.facebook.com/marketplace/item/<fb_id>/`
in a browser, or re-run `check-alive` without `--apply` once more for those
ids.

If the command stopped with a message about login, that is not the same as
"listing gone": the session expired, `rent-assistant login` and repeat the
run for the ones not covered.

## 3. Apply

```sh
rent-assistant check-alive --apply
```

Writes `rejected` (the default reason is "no longer listed", changed with
`--reason`) for every confirmed dead listing, through the same
`decisions.json` as a manual `rent-assistant reject`. `inconclusive`
touches nothing — those rows stay in their tab and enter the next run.

## 4. Export and report

```sh
rent-assistant export
```

Report briefly: how many checked, how many gone (with ids), how many
remain unclear and why (timeouts, logout), how many confirmed still alive.
The decision on a specific disputed id is the user's;
`rent-assistant unreject <fb_id>` returns it to the queue if the check was
wrong.
