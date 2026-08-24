"""Decision rules and HTML colour classes.

Two questions live here, both answered from the AI's extracted fields:

* :func:`decide` — is this listing still worth looking at? Exactly three
  conditions auto-reject (pets forbidden, far over budget, explicit studio).
  Everything else is a flag in the verdict, never a rejection: more rooms
  than asked for is a bonus, an unmentioned living room is silence rather
  than a defect, and poor `data_quality` means "re-scrape", not "drop".
* :func:`cell_class` — which colour a single table cell gets, including the
  fourth state `c-stale`: the field did not exist when this row was
  analysed, so its emptiness says nothing about the listing.

:func:`score` ranks the survivors 0-100 against `config/requirements.md`.
Missing information costs nothing much; only explicit mismatches subtract.
"""

from __future__ import annotations

from typing import Any

from .config import BUDGET_HARD_MAX_THB, DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE

# Reasons attached to auto-rejections, in the order they are checked.
REASON_PETS = "Животные запрещены"
REASON_PRICE = "Сильно вне бюджета"
REASON_STUDIO = "Студия"

# CSS classes the HTML export understands.
CLASS_GOOD = "c-good"
CLASS_WARN = "c-warn"
CLASS_BAD = "c-bad"
CLASS_STALE = "c-stale"
CLASS_NONE = "c-none"
CLASS_PLAIN = ""

# Fields carrying a monthly price in THB — coloured against the budget.
_PRICE_KEYS: frozenset[str] = frozenset({"price_month_thb", "price_12m_thb"})


def value_of(fields: dict[str, Any], key: str) -> Any:
    """Return the plain value of ``key``.

    Field values arrive wrapped as ``{"v": ..., "conf": ..., "ev": ...}``
    but plain values are accepted too, so callers can pass either shape.
    """
    raw = fields.get(key)
    if isinstance(raw, dict):
        return raw.get("v")
    return raw


def _as_int(value: Any) -> int | None:
    """Coerce a field value to ``int``, or ``None`` if it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = value.strip().replace(" ", "").replace(",", "")
        try:
            return int(float(digits))
        except ValueError:
            return None
    return None


def is_empty(value: Any) -> bool:
    """Whether a cell has nothing to show (``None`` or blank string)."""
    return value is None or (isinstance(value, str) and not value.strip())


def decide(
    fields: dict[str, Any],
    price_thb: int | None = None,
    data_quality: str | None = None,
) -> tuple[str, str | None]:
    """Return ``("active" | "rejected", reason)`` for one listing.

    ``price_thb`` is the listing's own price; it falls back to the AI's
    ``price_month_thb`` when the scraper never captured one. Anything not
    listed below stays active — condos, missing parking and weak internet
    are all flags for the verdict, not rejections.

    ``data_quality`` other than ``good`` suppresses every rejection: the
    text the AI read was the sidebar or was missing entirely, so its
    "findings" are not evidence about this listing. Such a row needs a
    ``scrape --refresh``, not a verdict. ``None`` means the analysis never
    reported one and is treated as usable.
    """
    if data_quality and data_quality != "good":
        return "active", None

    if value_of(fields, "pets") == "not_allowed":
        return "rejected", REASON_PETS

    price = price_thb if price_thb is not None else _as_int(
        value_of(fields, "price_month_thb")
    )
    if price is not None and price > BUDGET_HARD_MAX_THB:
        return "rejected", REASON_PRICE

    bedrooms = _as_int(value_of(fields, "bedrooms"))
    if value_of(fields, "property_type") == "studio" or bedrooms == 0:
        return "rejected", REASON_STUDIO

    return "active", None


def cell_class(
    field_key: str,
    value: Any,
    field_def: dict[str, Any] | None,
    row_schema_version: int,
) -> str:
    """Return the CSS class for one table cell.

    An empty cell is either `c-stale` — the field was added to the registry
    after this row was analysed, so the AI never looked — or `c-none`, the
    ordinary "looked, found nothing" dash. Non-empty cells are coloured only
    where colour means something: pets and price.
    """
    if is_empty(value):
        added_in = (field_def or {}).get("added_in", 1)
        if isinstance(added_in, int) and added_in > row_schema_version:
            return CLASS_STALE
        return CLASS_NONE

    if field_key == "pets":
        if value == "allowed":
            return CLASS_GOOD
        if value == "not_allowed":
            return CLASS_BAD
        return CLASS_WARN

    if field_key in _PRICE_KEYS:
        price = _as_int(value)
        if price is None:
            return CLASS_PLAIN
        if price > BUDGET_HARD_MAX_THB:
            return CLASS_BAD
        if price > DEFAULT_MAX_PRICE:
            return CLASS_WARN
        return CLASS_PLAIN

    return CLASS_PLAIN


# Weights per field, applied on top of a neutral baseline. Unknown values
# score 0 — silence in an ad is not evidence against the listing.
_ENUM_WEIGHTS: dict[str, dict[str, int]] = {
    "pets": {"allowed": 20, "not_allowed": -30},
    "property_type": {
        "house": 10,
        "townhouse": 10,
        "villa": 8,
        "condo": 2,
        "apartment": 2,
        "studio": -10,
    },
    "furnished": {"full": 6, "partial": 2, "none": -5},
    "living_room": {"yes": 5, "no": -3},
    "kitchen": {"full": 5, "kitchenette": -2, "none": -5},
    "green_area": {"yes": 6, "no": -2},
    "parking": {"yes": 5, "no": -3},
    "security": {"yes": 4, "no": -1},
    "condition": {"new": 3, "good": 2, "worn": -3},
}

# Baseline for a listing we know nothing about beyond it being active.
_BASELINE = 50


def score(fields: dict[str, Any]) -> int:
    """Rank a listing 0-100 against `config/requirements.md`.

    Extra rooms, a green area, parking and security push the score up;
    only explicit mismatches (no pets, kitchenette, over budget) push it
    down. Fields the ad never mentions leave the score untouched.
    """
    total = _BASELINE

    for key, weights in _ENUM_WEIGHTS.items():
        value = value_of(fields, key)
        if isinstance(value, str):
            total += weights.get(value, 0)

    price = _as_int(value_of(fields, "price_month_thb"))
    if price is not None:
        if DEFAULT_MIN_PRICE <= price <= DEFAULT_MAX_PRICE:
            total += 10
        elif price < DEFAULT_MIN_PRICE:
            total += 6
        elif price <= BUDGET_HARD_MAX_THB:
            total += 3
        else:
            total -= 15

    # One bedroom is the requirement; every extra one is a bonus, capped so
    # a six-bedroom villa does not outrank a matching townhouse on size alone.
    bedrooms = _as_int(value_of(fields, "bedrooms"))
    if bedrooms is not None and bedrooms >= 1:
        total += 4 + min(bedrooms - 1, 2) * 3

    if not is_empty(value_of(fields, "extra_rooms")):
        total += 3
    if not is_empty(value_of(fields, "internet")):
        total += 3
    if not is_empty(value_of(fields, "pool_gym")):
        total += 1

    months = _as_int(value_of(fields, "contract_min_months"))
    if months is not None:
        total += 3 if months <= 12 else -2

    return max(0, min(100, total))
