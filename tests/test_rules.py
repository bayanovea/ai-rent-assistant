"""Tests for auto-rejection rules, cell colours, and scoring."""

from __future__ import annotations

from fb_rent.rules import (
    CLASS_BAD,
    CLASS_GOOD,
    CLASS_NONE,
    CLASS_PLAIN,
    CLASS_STALE,
    CLASS_WARN,
    REASON_PETS,
    REASON_PRICE,
    REASON_SIZE,
    REASON_STUDIO,
    cell_class,
    decide,
    score,
)


def wrap(**fields) -> dict:
    """Build a fields dict in the wire shape the AI returns."""
    return {k: {"v": v, "conf": "high", "ev": ""} for k, v in fields.items()}


# --- the four auto-rejections ------------------------------------------


def test_pets_not_allowed_is_rejected():
    assert decide(wrap(pets="not_allowed"), 20_000) == ("rejected", REASON_PETS)


def test_price_over_hard_budget_is_rejected():
    status, reason = decide(wrap(pets="allowed"), 35_001)
    assert (status, reason) == ("rejected", REASON_PRICE)


def test_price_at_hard_budget_is_active():
    assert decide(wrap(pets="allowed"), 35_000) == ("active", None)


def test_ai_price_is_used_when_listing_has_none():
    fields = wrap(pets="allowed", price_month_thb=40_000)
    assert decide(fields, None) == ("rejected", REASON_PRICE)


def test_explicit_studio_type_is_rejected():
    assert decide(wrap(property_type="studio"), 18_000) == ("rejected", REASON_STUDIO)


def test_zero_bedrooms_is_rejected():
    assert decide(wrap(bedrooms=0), 18_000) == ("rejected", REASON_STUDIO)


def test_size_below_the_floor_is_rejected():
    fields = wrap(pets="allowed", bedrooms=1, size_sqm=34)
    assert decide(fields, 20_000) == ("rejected", REASON_SIZE)


def test_size_at_the_floor_is_active():
    fields = wrap(pets="allowed", bedrooms=1, size_sqm=35)
    assert decide(fields, 20_000) == ("active", None)


def test_size_as_a_string_is_still_compared():
    assert decide(wrap(bedrooms=1, size_sqm="28"), 20_000) == ("rejected", REASON_SIZE)


def test_pets_wins_over_other_reasons():
    fields = wrap(pets="not_allowed", property_type="studio")
    assert decide(fields, 99_000) == ("rejected", REASON_PETS)


def test_plain_values_are_accepted_too():
    assert decide({"pets": "not_allowed"}, 20_000) == ("rejected", REASON_PETS)


# --- what must NOT be rejected -----------------------------------------


def test_more_bedrooms_than_asked_stays_active():
    assert decide(wrap(pets="unclear", bedrooms=3), 25_000) == ("active", None)


def test_unclear_living_room_stays_active():
    assert decide(wrap(living_room="unclear", bedrooms=1), 25_000) == ("active", None)


def test_condo_stays_active():
    assert decide(wrap(property_type="condo", bedrooms=1), 25_000) == ("active", None)


def test_price_33000_stays_active_but_warns():
    assert decide(wrap(pets="unclear"), 33_000) == ("active", None)
    assert cell_class("price_month_thb", 33_000, {"added_in": 1}, 1) == CLASS_WARN


def test_sidebar_noise_data_quality_stays_active():
    fields = wrap(pets="unclear")
    assert decide(fields, 22_000, "sidebar_noise") == ("active", None)


def test_bad_data_quality_suppresses_a_rejection():
    # The "no pets" line the AI read belonged to the sidebar, not the ad:
    # such a row needs a re-scrape, never a rejection.
    fields = wrap(pets="not_allowed")
    assert decide(fields, 22_000, "sidebar_noise") == ("active", None)
    assert decide(fields, 22_000, "missing") == ("active", None)
    assert decide(fields, 22_000, "good") == ("rejected", REASON_PETS)
    assert decide(fields, 22_000, None) == ("rejected", REASON_PETS)


def test_unknown_size_stays_active():
    # Silence about the area is not evidence that the flat is small.
    assert decide(wrap(pets="allowed", bedrooms=1), 20_000) == ("active", None)
    assert decide(wrap(bedrooms=1, size_sqm=None), 20_000) == ("active", None)
    assert decide(wrap(bedrooms=1, size_sqm=""), 20_000) == ("active", None)
    assert decide(wrap(bedrooms=1, size_sqm="около 40"), 20_000) == ("active", None)


def test_small_size_from_bad_data_quality_stays_active():
    fields = wrap(size_sqm=20)
    assert decide(fields, 20_000, "sidebar_noise") == ("active", None)


def test_empty_fields_stay_active():
    assert decide({}, None) == ("active", None)


def test_missing_amenities_stay_active():
    fields = wrap(parking="no", green_area="no", internet="", contract_min_months=1)
    assert decide(fields, 20_000) == ("active", None)


def test_photos_of_something_else_cost_score():
    # A render or a shot of another unit means the flat itself was never
    # shown — that is a defect, and it has to cost something.
    baseline = score({})
    assert score(wrap(photo_quality="render")) == baseline - 8
    assert score(wrap(photo_quality="other_unit")) == baseline - 8
    assert score(wrap(photo_quality="floorplan")) == baseline - 6


def test_genuine_photos_are_the_norm_not_a_bonus():
    # Most rows have had no photo pass yet. Paying a bonus for "the pictures
    # were real" would rank them below analysed ones for no reason about the
    # flat itself.
    baseline = score({})
    assert score(wrap(photo_quality="real")) == baseline
    assert score(wrap(photo_quality="unclear")) == baseline


# --- cell colours -------------------------------------------------------


def test_size_below_the_floor_is_red():
    assert cell_class("size_sqm", 30, {"added_in": 1}, 1) == CLASS_BAD


def test_size_at_or_above_the_floor_is_plain():
    assert cell_class("size_sqm", 35, {"added_in": 1}, 1) == CLASS_PLAIN
    assert cell_class("size_sqm", 80, {"added_in": 1}, 1) == CLASS_PLAIN


def test_unparsable_size_is_plain():
    assert cell_class("size_sqm", "около 40", {"added_in": 1}, 1) == CLASS_PLAIN


def test_pets_allowed_is_green():
    assert cell_class("pets", "allowed", {"added_in": 1}, 1) == CLASS_GOOD


def test_pets_unclear_is_yellow():
    assert cell_class("pets", "unclear", {"added_in": 1}, 1) == CLASS_WARN


def test_pets_not_allowed_is_red():
    assert cell_class("pets", "not_allowed", {"added_in": 1}, 1) == CLASS_BAD


def test_field_added_after_the_row_was_analysed_is_stale():
    assert cell_class("balcony", None, {"added_in": 2}, 1) == CLASS_STALE


def test_field_that_existed_but_stayed_empty_is_none():
    assert cell_class("parking", None, {"added_in": 1}, 1) == CLASS_NONE


def test_blank_string_counts_as_empty():
    assert cell_class("internet", "   ", {"added_in": 1}, 1) == CLASS_NONE


def test_missing_field_def_defaults_to_none_not_stale():
    assert cell_class("parking", None, None, 1) == CLASS_NONE


def test_price_within_budget_is_plain():
    assert cell_class("price_month_thb", 22_000, {"added_in": 1}, 1) == CLASS_PLAIN


def test_price_over_hard_budget_is_red():
    assert cell_class("price_12m_thb", 36_000, {"added_in": 1}, 1) == CLASS_BAD


def test_ordinary_field_gets_no_class():
    assert cell_class("project_name", "Pruksa Ville", {"added_in": 1}, 1) == CLASS_PLAIN


# --- scoring ------------------------------------------------------------


def _perfect() -> dict:
    return wrap(
        pets="allowed",
        property_type="townhouse",
        price_month_thb=24_000,
        furnished="full",
        living_room="yes",
        kitchen="full",
        green_area="yes",
        parking="yes",
        security="yes",
        condition="good",
        bedrooms=2,
        extra_rooms="кабинет",
        internet="AIS fibre 300 Mbps",
        contract_min_months=12,
    )


def test_score_is_bounded():
    assert score(_perfect()) == 100
    assert score({}) == 50  # the baseline, not merely "somewhere in range"
    assert score(wrap(pets="not_allowed", price_month_thb=90_000)) < score({})


def test_unknown_fields_score_near_the_baseline():
    unknown = wrap(pets="unclear", living_room="unclear", kitchen="unclear")
    assert 45 <= score(unknown) <= 55


def test_extra_rooms_raise_the_score():
    one_bed = wrap(pets="allowed", bedrooms=1)
    three_bed = wrap(pets="allowed", bedrooms=3)
    assert score(three_bed) > score(one_bed)


def test_amenities_raise_the_score():
    bare = wrap(pets="allowed", bedrooms=2)
    with_amenities = wrap(
        pets="allowed", bedrooms=2, green_area="yes", parking="yes", security="yes"
    )
    assert score(with_amenities) > score(bare)


def test_pets_allowed_outranks_pets_unclear():
    assert score(wrap(pets="allowed")) > score(wrap(pets="unclear"))


def test_missing_living_room_barely_costs_anything():
    mentioned = wrap(pets="allowed", living_room="yes", bedrooms=1)
    silent = wrap(pets="allowed", bedrooms=1)
    assert score(mentioned) - score(silent) <= 5
