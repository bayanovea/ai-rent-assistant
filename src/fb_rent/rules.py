"""Decision rules and HTML colour classes.

Two questions live here, both answered from the AI's extracted fields:

* :func:`decide` — is this listing still worth looking at? Exactly six
  conditions auto-reject (pets forbidden by the ad, pets forbidden by the
  building's own confirmed rule, a district rated at or below
  `_AREA_REJECT_MAX`, far over budget, explicit studio, living area under
  `MIN_SIZE_SQM`).
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

from .config import (
    BUDGET_HARD_MAX_THB,
    DEFAULT_MAX_PRICE,
    MIN_SIZE_SQM,
)

# Reasons attached to auto-rejections, in the order they are checked.
REASON_PETS = "Животные запрещены"
REASON_BUILDING_PETS = "Кондо запрещает животных"
REASON_AREA = "Район в автоотказе"
REASON_PRICE = "Сильно вне бюджета"
REASON_STUDIO = "Студия"
REASON_SIZE = f"Меньше {MIN_SIZE_SQM} м²"

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


# Districts of Phuket on the user's own 1-5 scale. The numbers are personal,
# not geographic: 5 is Kathu and Bang Tao, 4 is a quiet
# place to actually live with a dog, 3.5 is "fine but touristy or far from
# everything", 3 is indifferent, 1 auto-rejects. Beach proximity is already
# baked into these numbers — it is a nice-to-have, never a criterion of its
# own, so there is no separate distance-to-the-sea bonus anywhere.
#
# Aliases are matched whole-word against a space-normalised string, which is
# why every spelling FB and the AI actually produce is listed separately
# (`Ka Rorn` is FB's Karon, `Ban Ket Ho` is a village inside Kathu).
_RATED_AREAS: tuple[tuple[float, tuple[str, ...]], ...] = (
    (5.0, ("kathu", "ket ho", "ketho", "loch palm", "lochpalm", "bang tao",
           "bangtao", "кату", "катху", "банг тао", "бангтао")),
    (4.5, ("cherngtalay", "cherng talay", "choeng thale", "choengthale",
           "laguna", "surin", "boat avenue", "boatavenue", "layan")),
    (4.0, ("rawai", "nai harn", "naiharn", "nai yang", "naiyang", "nai thon",
           "naithon", "mai khao", "maikhao", "sakhu", "airport", "аэропорт",
           "old town", "oldtown", "talat nuea", "talatnuea", "talat yai",
           "talatyai", "раваи", "равай")),
    (3.5, ("kata", "karon", "ka rorn", "karorn", "thalang", "si sunthon",
           "sisunthon", "sri soonthorn", "srisoonthorn", "bang jo", "bangjo",
           "koh kaew", "kohkaew", "ko kaeo", "pa khlok", "pakhlok", "paklok",
           "kamala", "panwa", "phuket town", "phukettown", "mueang phuket",
           "ката", "карон", "тхаланг", "камала")),
    (3.0, ("chalong", "wichit", "samkong", "sam kong", "ratsada", "rassada",
           "sapam", "ao po", "aopo", "koh siray", "ko sireh", "чалонг")),
    (1.0, ("patong", "патонг", "патонге")),
)

_AREA_RATINGS: dict[str, float] = {
    alias: rating for rating, aliases in _RATED_AREAS for alias in aliases
}

# A rating at or below this rejects outright; the pivot scores zero, and every
# half-step away from it is worth `_AREA_WEIGHT / 2` points.
_AREA_REJECT_MAX = 1.5
_AREA_PIVOT = 3.5
_AREA_WEIGHT = 6


def _area_words(text: Any) -> str:
    """Reduce a location string to ` word word ` for whole-word matching."""
    if not isinstance(text, str) or not text.strip():
        return ""
    chars = [ch.lower() if ch.isalnum() else " " for ch in text]
    return " " + " ".join("".join(chars).split()) + " "


def district_rating(text: Any) -> float | None:
    """Return the 1-5 rating of the district named in ``text``.

    The AI writes `area` as free text and often names two places at once
    (``Cherngtalay, Thalang``, ``Nai Harn, Rawai``, ``Kathu, 10 мин до
    Patong``). The **first** name wins, because that is where the ad puts the
    listing itself and everything after it is context — taking the best or the
    worst match instead would rate a Kathu house by its distance to Patong.
    ``None`` means no known district was mentioned, which costs nothing.
    """
    haystack = _area_words(text)
    if not haystack:
        return None

    best: tuple[int, int, float] | None = None
    for alias, rating in _AREA_RATINGS.items():
        found = haystack.find(f" {alias} ")
        if found < 0:
            continue
        # Earliest mention wins; on a shared start the longer name is the
        # more specific one (`bang tao` over a hypothetical `bang`).
        candidate = (found, -len(alias), rating)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else None


def area_rating(fields: dict[str, Any], district: str | None = None) -> float | None:
    """Rating of a listing's district: the AI's `area` first, FB's tag after.

    Same order the export uses to fill the district column, and for the same
    reason — the AI read the ad text, FB's own tag is where the poster
    dropped a pin and is often just ``Changwat Phuket``.
    """
    for text in (value_of(fields, "area"), district):
        rating = district_rating(text)
        if rating is not None:
            return rating
    return None


def decide(
    fields: dict[str, Any],
    price_thb: int | None = None,
    data_quality: str | None = None,
    district: str | None = None,
    building_policy: str | None = None,
) -> tuple[str, str | None]:
    """Return ``("active" | "rejected", reason)`` for one listing.

    ``price_thb`` is the listing's own price; it falls back to the AI's
    ``price_month_thb`` when the scraper never captured one. ``district`` is
    FB's own location tag, used only when the AI never named an area.
    Anything not listed below stays active — condos, missing parking and weak
    internet are all flags for the verdict, not rejections. The same holds for
    a missing ``size_sqm``: silence about the area is never a rejection, and a
    district nobody named is not a rejection either.

    ``building_policy`` is what the building's own house rule says about pets,
    already resolved by :mod:`fb_rent.buildings` — the key normalisation, the
    alias lookup and the ``confirmed`` gate all happen there, so a policy that
    reaches this function is one the user has vetted. Only ``not_allowed``
    rejects: ``negotiable`` is the case the user wants kept and asked about,
    ``allowed`` and ``unknown`` say nothing to act on, and ``None`` is the
    unresearched building.

    An ad that says ``pets: allowed`` outright is the one exception, and it
    survives a building that forbids them. The two remain separate sources —
    neither overwrites the other — but they are not equally specific: the
    house rule is about the tower, while "с животными можно" is about this
    flat, and it is usually a landlord who already has the permission the rule
    talks about. Rejecting it would throw away the only listings in a
    forbidding building worth looking at. Repeated such ads in one building
    are a signal the research got the tower wrong, which is what
    :func:`.storage.fetch_pets_conflicts` collects.

    ``data_quality`` other than ``good`` suppresses every rejection: the
    text the AI read was the sidebar or was missing entirely, so its
    "findings" are not evidence about this listing. Such a row needs a
    re-read (``scrape --ids``), not a verdict. ``None`` means the analysis never
    reported one and is treated as usable.
    """
    if data_quality and data_quality != "good":
        return "active", None

    if value_of(fields, "pets") == "not_allowed":
        return "rejected", REASON_PETS

    # Checked after the ad's own field so that a listing saying "no pets"
    # outright keeps the more direct reason when both apply — and skipped
    # entirely for the ad that says the opposite, see the docstring.
    if building_policy == "not_allowed" and value_of(fields, "pets") != "allowed":
        return "rejected", REASON_BUILDING_PETS

    # Patong is out whatever the flat looks like, so this is checked before
    # price and size rather than left to cost points in `score`.
    rating = area_rating(fields, district)
    if rating is not None and rating <= _AREA_REJECT_MAX:
        return "rejected", REASON_AREA

    price = price_thb if price_thb is not None else _as_int(
        value_of(fields, "price_month_thb")
    )
    if price is not None and price > BUDGET_HARD_MAX_THB:
        return "rejected", REASON_PRICE

    bedrooms = _as_int(value_of(fields, "bedrooms"))
    if value_of(fields, "property_type") == "studio" or bedrooms == 0:
        return "rejected", REASON_STUDIO

    # Only a stated number below the floor rejects. An ad that never names a
    # size — or whose size the AI could not read — stays active.
    size = _as_int(value_of(fields, "size_sqm"))
    if size is not None and size < MIN_SIZE_SQM:
        return "rejected", REASON_SIZE

    return "active", None


# The AI writes its recommendation as one sentence, and the sentence is what
# the table used to show: 240px of clipped prose whose first two words were
# "Стоит посмотреть" on 62% of the active set, spelled four different ways
# ("Стоит взглянуть", "Стоит присмотреться"). The tag below is those first two
# words, normalised — narrow enough for a column, short enough for a filter
# chip, ordered so that sorting ascending puts what to look at first.
#
# The vocabulary is also fixed at the source (`.claude/skills/analyze`), so the
# stems here are a net for what the AI has already written rather than a
# permanent parser. A sentence that matches nothing yields an empty tag — a
# blank cell that shows the drift instead of quietly filing it under a wrong
# heading, with the sentence itself still one hover away.
VERDICT_TAGS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("see", "смотреть", ("стоит посмотреть", "стоит взглянуть",
                         "стоит присмотреться", "стоит рассмотреть")),
    ("ask", "спросить", ("уточнить", "спросить")),
    ("doubt", "сомнительно", ("сомнительно", "под вопросом", "спорно")),
    ("reread", "перечитать", ("повторный скрейп", "повторного скрейпа",
                              "re-scrape", "рескрейп", "мало данных",
                              "текста объявления нет", "нет текста")),
    ("no", "отказ", ("отказ", "не жильё", "не жилье", "не аренда",
                     "не подходит", "не сдают")),
)

# Longest stems first, so "стоит уточнить" is a question rather than a
# recommendation: whichever stem appears earliest in the sentence wins, and on
# a tie the more specific one does.
_VERDICT_STEMS: tuple[tuple[str, str, str], ...] = tuple(
    sorted(
        ((stem, slug, label) for slug, label, stems in VERDICT_TAGS
         for stem in stems),
        key=lambda item: -len(item[0]),
    )
)


def verdict_tag(verdict: Any, data_quality: str | None = None) -> tuple[str, str]:
    """Return ``(slug, label)`` for the AI's one-line recommendation.

    ``data_quality`` outranks the sentence: if the analysis read the sidebar
    or read nothing, whatever it concluded is about the wrong text, and the
    only thing to do with the row is read it again.
    """
    if data_quality and data_quality != "good":
        return "reread", "перечитать"
    text = str(verdict or "").lower()
    if not text.strip():
        return "", ""
    best: tuple[int, int, str, str] | None = None
    for stem, slug, label in _VERDICT_STEMS:
        found = text.find(stem)
        if found < 0:
            continue
        candidate = (found, -len(stem), slug, label)
        if best is None or candidate < best:
            best = candidate
    return (best[2], best[3]) if best else ("", "")


def verdict_order(slug: str) -> int | None:
    """Rank of a tag for sorting: what to look at first sorts first."""
    for index, (name, _label, _stems) in enumerate(VERDICT_TAGS):
        if name == slug:
            return index
    return None


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
    where colour means something: pets, price and size.
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

    if field_key == "size_sqm":
        size = _as_int(value)
        if size is None:
            return CLASS_PLAIN
        return CLASS_BAD if size < MIN_SIZE_SQM else CLASS_PLAIN

    return CLASS_PLAIN


# Weights per field, applied on top of a neutral baseline. Unknown values
# score 0 — silence in an ad is not evidence against the listing.
#
# The two sides are deliberately asymmetric: a stop-factor hits at full
# strength, a nice-to-have is worth about two thirds of what it once was.
# The old, symmetric table let so many small pluses stack that a third of
# the active set sat at exactly 100 and the sort had nothing left to order
# them by — see docs/ai-enrichment.md. Anything that raises the ceiling here
# has to be checked against that: a tie at the top is the failure mode.
_ENUM_WEIGHTS: dict[str, dict[str, int]] = {
    "pets": {"allowed": 14, "not_allowed": -30},
    "property_type": {
        "house": 7,
        "townhouse": 7,
        "villa": 6,
        "condo": 1,
        "apartment": 1,
        "studio": -10,
    },
    "furnished": {"full": 4, "partial": 1, "none": -5},
    "living_room": {"yes": 4, "no": -3},
    "kitchen": {"full": 4, "kitchenette": -2, "none": -5},
    "green_area": {"yes": 4, "no": -2},
    "parking": {"yes": 4, "no": -3},
    "security": {"yes": 3, "no": -1},
    "condition": {"new": 2, "good": 1, "worn": -3},
    # Both are photo-only and both are about how the place feels rather than
    # what it has, so the plus side is deliberately thin: they correlate — a
    # bright flat photographs as an attractive one — and a full-strength bonus
    # on each would pay the same photogenic listing twice. The minus side is
    # not hedged, because a dark flat and a tired one are what this pass is
    # for. `bright` is worth less than `dark` costs for a second reason: a
    # wide-angle daylight shot makes anything look bright, while small windows
    # and a view into a wall cannot be staged away.
    "light": {"bright": 3, "dark": -4},
    "appeal": {"attractive": 2, "unattractive": -5},
    # `sea` is worth no more than `green` on purpose. The sea is a minor plus
    # for this user and is already inside the district ratings; paying extra
    # for the view would smuggle the same preference in a second time, and a
    # green view is worth as much to someone who walks a dog. `wall` is the
    # signal here — and it is softened to -3 rather than -4 because a window
    # into the next tower is usually also a `dark` flat, which has already
    # been charged for.
    "view": {"sea": 2, "green": 2, "wall": -3},
    # Photos that show something other than the flat on offer are a defect,
    # not an absence: you cannot judge the place at all. `real` scores zero
    # on purpose — most rows have had no photo pass yet, and paying a bonus
    # for "the pictures were genuine" would rank them below analysed ones
    # for no reason about the flat itself.
    "photo_quality": {"render": -8, "other_unit": -8, "floorplan": -6},
}

# Baseline for a listing we know nothing about beyond it being active.
_BASELINE = 50


def area_class(text: Any) -> str:
    """Colour for the district cell, from the same 1-5 scale as the score."""
    rating = district_rating(text)
    if rating is None:
        return CLASS_PLAIN
    if rating <= _AREA_REJECT_MAX:
        return CLASS_BAD
    if rating >= _AREA_PIVOT + 1:
        return CLASS_GOOD
    if rating < _AREA_PIVOT:
        return CLASS_WARN
    return CLASS_PLAIN


def score(fields: dict[str, Any], district: str | None = None) -> int:
    """Rank a listing 0-100 against `config/requirements.md`.

    A green area, parking, security and rooms beyond the first bedroom push
    the score up; only explicit mismatches (no pets, kitchenette, over the
    soft budget) push it down. Fields the ad never mentions leave the score
    untouched — including the district, which is why an unrecognised area is
    worth exactly zero rather than a penalty.

    The building's own pets policy is deliberately absent here, in both
    directions. A confirmed ``not_allowed`` needs no penalty — it rejects in
    `decide` and never reaches the ranking — and a confirmed ``allowed`` must
    not pay a bonus: it would lift exactly the rows already easiest to rank,
    and it would put a researched building above an identical unresearched one
    for a reason that is about the research rather than the flat. This is the
    tie-at-100 trap below, in its cheapest disguise.

    Nothing is scored for merely *saying* something. An earlier version paid
    points for a non-empty `internet`, `pool_gym` or `extra_rooms`, which
    ranked the wordy ad above the terse one describing the same flat, and
    paid a flat bonus for the first bedroom and for a price inside the
    budget — both of which every survivor has. Together they pinned a third
    of the active set at exactly 100, where the sort could no longer tell
    them apart.
    """
    total = _BASELINE

    rating = area_rating(fields, district)
    if rating is not None:
        # Linear around the pivot: Kathu and Bang Tao (5) are worth +12,
        # Kata and Karon (3.5) nothing, Chalong and Wichit (3) −4.
        total += round((rating - _AREA_PIVOT) * _AREA_WEIGHT)

    for key, weights in _ENUM_WEIGHTS.items():
        value = value_of(fields, key)
        if isinstance(value, str):
            total += weights.get(value, 0)

    # A price inside the budget is the norm, not an achievement: paying
    # points for it would hand the same bonus to almost every survivor, since
    # anything over BUDGET_HARD_MAX_THB is rejected before it ever gets here.
    # Only the stretch above the soft ceiling costs anything.
    price = _as_int(value_of(fields, "price_month_thb"))
    if price is not None and price > DEFAULT_MAX_PRICE:
        total += -8 if price <= BUDGET_HARD_MAX_THB else -15

    # One bedroom is the requirement, so it earns nothing — a studio is
    # rejected outright and every survivor has at least one. Extra bedrooms
    # are the bonus, capped so a six-bedroom villa does not outrank a
    # matching townhouse on room count alone.
    bedrooms = _as_int(value_of(fields, "bedrooms"))
    if bedrooms is not None and bedrooms >= 1:
        total += min(bedrooms - 1, 2) * 3

    months = _as_int(value_of(fields, "contract_min_months"))
    if months is not None:
        total += 2 if months <= 12 else -2

    return max(0, min(100, total))
