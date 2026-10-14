"""Tests for the pure Messenger-outreach logic: templates, state, age
parsing, and the send log."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rent_assistant import messages as msg


# --- compose_first_message / pets_already_allowed -------------------------


def test_pets_unknown_asks_the_question():
    row = {"fields": {}}
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_UNKNOWN


def test_pets_unclear_still_asks_the_question():
    row = {"fields": {"pets": {"v": "unclear"}}}
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_UNKNOWN


def test_pets_allowed_in_ad_skips_the_question():
    row = {"fields": {"pets": {"v": "allowed"}}}
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_KNOWN


def test_pets_not_allowed_in_ad_still_asks():
    # Not our call to assume — the ad's "not_allowed" is not "allowed", so
    # the guard (mirrors rules.decide) still sends the question.
    row = {"fields": {"pets": {"v": "not_allowed"}}}
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_UNKNOWN


def test_building_confirmed_allowed_skips_the_question():
    row = {
        "fields": {},
        "building_status": "confirmed",
        "building_policy": "allowed",
    }
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_KNOWN


def test_building_proposed_allowed_does_not_count():
    # A merely proposed policy is inert everywhere else (CLAUDE.md) — must
    # stay inert here too.
    row = {
        "fields": {},
        "building_status": "proposed",
        "building_policy": "allowed",
    }
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_UNKNOWN


def test_ad_allowed_wins_even_without_building_data():
    row = {"fields": {"pets": {"v": "allowed"}}, "building_status": None}
    assert msg.compose_first_message(row) == msg.MESSAGE_PETS_KNOWN


# --- parse_thread_age / is_on_or_after_cutoff ------------------------------


_NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,expected_date",
    [
        ("14\xa0мин.", "2026-09-22"),
        ("46 мин.", "2026-09-22"),
        ("1 ч.", "2026-09-22"),
        ("4 ч.", "2026-09-22"),
        ("14 ч.", "2026-09-21"),
        ("1 г.", "2025-09-22"),
        ("2 г.", "2024-09-22"),
    ],
)
def test_parse_thread_age_russian(raw: str, expected_date: str):
    result = msg.parse_thread_age(raw, now=_NOW)
    assert result is not None
    assert result.startswith(expected_date)


def test_parse_thread_age_unrecognised_returns_none():
    assert msg.parse_thread_age("just now", now=_NOW) is None
    assert msg.parse_thread_age(None, now=_NOW) is None
    assert msg.parse_thread_age("", now=_NOW) is None


def test_is_on_or_after_cutoff_true_for_recent():
    age = msg.parse_thread_age("14 мин.", now=_NOW)
    assert msg.is_on_or_after_cutoff(age, "2026-09-20") is True


def test_is_on_or_after_cutoff_false_for_old():
    age = msg.parse_thread_age("2 г.", now=_NOW)
    assert msg.is_on_or_after_cutoff(age, "2026-09-20") is False


def test_is_on_or_after_cutoff_false_for_unparseable():
    # The conservative default: an undated thread is never "recent enough".
    assert msg.is_on_or_after_cutoff(None, "2026-09-20") is False


# --- derive_state -----------------------------------------------------------


def test_derive_state_no_reply_yet():
    assert msg.derive_state(True, False, None) == ("contacted", 0, None)


def test_derive_state_unclassified_reply():
    assert msg.derive_state(True, True, None) == ("replied", 0, None)


def test_derive_state_pets_declined():
    facts = {"pets_answer": "not_allowed"}
    assert msg.derive_state(True, True, facts) == (
        "pets_declined",
        1,
        "pets_declined",
    )


def test_derive_state_viewing_proposed():
    facts = {"viewing_proposed": True, "proposed_times": ["сегодня в 16:00"]}
    assert msg.derive_state(True, True, facts) == (
        "viewing_proposed",
        1,
        "viewing_time",
    )


def test_derive_state_unusual_reply():
    assert msg.derive_state(True, True, {"unusual": True}) == (
        "answered",
        1,
        "unusual_reply",
    )


def test_derive_state_listing_gone():
    assert msg.derive_state(True, True, {"listing_gone": True}) == (
        "answered",
        1,
        "unusual_reply",
    )


def test_derive_state_ordinary_answer_needs_nothing():
    facts = {"pets_answer": "allowed", "viewing_proposed": False}
    assert msg.derive_state(True, True, facts) == ("answered", 0, None)


def test_derive_state_unusual_wins_over_pets_and_viewing():
    facts = {
        "unusual": True,
        "pets_answer": "not_allowed",
        "viewing_proposed": True,
    }
    assert msg.derive_state(True, True, facts)[2] == "unusual_reply"


# --- reply_hash_of -----------------------------------------------------------


def test_reply_hash_stable_for_same_inbound_texts():
    a = [{"direction": "in", "text": "Hi"}, {"direction": "out", "text": "Hello"}]
    b = [{"direction": "in", "text": "Hi"}, {"direction": "out", "text": "Different"}]
    assert msg.reply_hash_of(a) == msg.reply_hash_of(b)


def test_reply_hash_changes_with_new_inbound():
    a = [{"direction": "in", "text": "Hi"}]
    b = [{"direction": "in", "text": "Hi"}, {"direction": "in", "text": "Hi again"}]
    assert msg.reply_hash_of(a) != msg.reply_hash_of(b)


def test_reply_hash_empty_for_no_inbound():
    only_outbound = [{"direction": "out", "text": "Hello"}]
    assert msg.reply_hash_of(only_outbound) == msg.reply_hash_of([])


# --- send log ----------------------------------------------------------------


@pytest.fixture()
def send_log_path(tmp_path: Path) -> Path:
    return tmp_path / "messages_sent.json"


def test_load_send_log_missing_file_is_empty(send_log_path: Path):
    assert msg.load_send_log(send_log_path) == {}


def test_append_and_load_round_trip(send_log_path: Path):
    entry = msg.append_send_log(
        "123", "123", "first_contact", "Hello!", path=send_log_path
    )
    assert entry["fb_id"] == "123"
    assert entry["sent_at"].endswith("Z")

    log = msg.load_send_log(send_log_path)
    assert set(log) == {"123:123:first_contact"}


def test_already_sent(send_log_path: Path):
    assert msg.already_sent("123", "123", "first_contact", path=send_log_path) is False
    msg.append_send_log("123", "123", "first_contact", "Hi", path=send_log_path)
    assert msg.already_sent("123", "123", "first_contact", path=send_log_path) is True


def test_append_send_log_keeps_other_entries(send_log_path: Path):
    msg.append_send_log("111", "111", "first_contact", "A", path=send_log_path)
    msg.append_send_log("222", "222", "first_contact", "B", path=send_log_path)
    log = msg.load_send_log(send_log_path)
    assert set(log) == {"111:111:first_contact", "222:222:first_contact"}


def test_send_log_survives_missing_parent_dir(tmp_path: Path):
    nested = tmp_path / "nested" / "dir" / "messages_sent.json"
    msg.append_send_log("1", "1", "first_contact", "Hi", path=nested)
    assert msg.load_send_log(nested) != {}


def test_load_send_log_rejects_non_object(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        msg.load_send_log(bad)
