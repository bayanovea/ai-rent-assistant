"""Pure logic for the Messenger outreach pass.

Message templates, conversation state derivation, the relative-age parser,
and the irreversible send log. No Playwright and no click here — see
``messenger.py`` for the browser layer and ``storage.py`` for the
re-fetchable conversation data. The split mirrors the one ``rules.py``
already draws between "what was extracted" and "what the code decides":
the classification skill hands back facts only, and :func:`derive_state` is
the one place that turns facts into a state and a needs-action flag — see
CLAUDE.md "Subagent fan-out".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, rules

CUTOFF_DEFAULT: str = config.MESSAGES_SINCE

# contacted -> replied (unclassified) -> one of the terminal-ish states.
# `viewing_agreed` is reserved for iteration 2 (time negotiation); nothing
# in this iteration ever produces it.
VALID_CONVO_STATES: frozenset[str] = frozenset(
    {
        "contacted",
        "replied",
        "pets_declined",
        "viewing_proposed",
        "answered",
        "viewing_agreed",
    }
)

# Why a conversation needs the user's own eyes on it. `unclassified` is only
# for a thread whose classification actually failed (bad JSON, model error)
# — a thread merely awaiting its first classification pass is `replied`
# with needs_action=0, not this.
NEEDS_ACTION_REASONS: frozenset[str] = frozenset(
    {
        "viewing_time",
        "pets_declined",
        "unusual_reply",
        "no_listing_match",
        "unclassified",
    }
)

# Wording is the user's own, used as-is on real listings before this tool
# existed (see docs/messages.md) — do not rephrase without checking with
# them first.
MESSAGE_PETS_KNOWN: str = (
    "Hello! I want to rent your house for 1 year. Can I come to see it? "
    "When is a good time for you?"
)
MESSAGE_PETS_UNKNOWN: str = (
    MESSAGE_PETS_KNOWN
    + " Also, can we live there with a dog? It is a small, quiet small dog."
)


def pets_already_allowed(row: dict[str, Any]) -> bool:
    """True only when an outright "allowed" is already on record.

    Mirrors the guard in ``rules.decide`` (CLAUDE.md "Pets policy per
    building"): ``unclear`` is what most ads carry and must still trigger
    the question, and only a *confirmed* building policy counts — a merely
    ``proposed`` one is inert everywhere else and must stay inert here too.
    """
    fields = row.get("fields") or {}
    if rules.value_of(fields, "pets") == "allowed":
        return True
    return bool(
        row.get("building_status") == "confirmed"
        and row.get("building_policy") == "allowed"
    )


def compose_first_message(row: dict[str, Any]) -> str:
    """The English first-contact text: viewing ask, plus pets if unresolved."""
    if pets_already_allowed(row):
        return MESSAGE_PETS_KNOWN
    return MESSAGE_PETS_UNKNOWN


# --- relative-age parsing -------------------------------------------------
# Facebook's Marketplace-folder thread list gives age as free text, not a
# timestamp — see docs/messages.md. Measured live: Russian-locale
# "14 мин.", "1 ч.", "2 г."; English forms are anticipated but unmeasured.
_AGE_RE = re.compile(
    r"^(?P<num>\d+)\s*(?P<unit>[^\d.\s]+)\.?$",
    re.IGNORECASE,
)

_UNIT_MINUTES = {"мин", "min", "mins"}
_UNIT_HOURS = {"ч", "h", "hr", "hrs"}
_UNIT_DAYS = {"д", "d", "day", "days"}
_UNIT_WEEKS = {"нед", "w", "wk", "week", "weeks"}
_UNIT_YEARS = {"г", "y", "yr", "yrs"}


def _delta_for(unit: str, amount: int) -> timedelta | None:
    unit = unit.lower()
    if unit in _UNIT_MINUTES:
        return timedelta(minutes=amount)
    if unit in _UNIT_HOURS:
        return timedelta(hours=amount)
    if unit in _UNIT_DAYS:
        return timedelta(days=amount)
    if unit in _UNIT_WEEKS:
        return timedelta(weeks=amount)
    if unit in _UNIT_YEARS:
        return timedelta(days=365 * amount)
    return None


def parse_thread_age(raw: str | None, now: datetime | None = None) -> str | None:
    """Turn FB's relative age ("14 мин.", "2 г.") into an ISO-8601 UTC string.

    Returns ``None`` for anything unrecognised. The caller's rule (see
    ``messages sync`` and CLAUDE.md) is to never open a thread it could not
    positively date, so an unparseable age must not masquerade as recent —
    callers should treat ``None`` the same as "too old".
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("\xa0", " ")
    match = _AGE_RE.match(cleaned)
    if not match:
        return None
    try:
        amount = int(match.group("num"))
    except ValueError:
        return None
    delta = _delta_for(match.group("unit"), amount)
    if delta is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_on_or_after_cutoff(age_iso: str | None, cutoff: str = CUTOFF_DEFAULT) -> bool:
    """False for ``None`` — an undated thread is never "recent enough"."""
    if not age_iso:
        return False
    return age_iso[:10] >= cutoff


# --- conversation state ---------------------------------------------------


def derive_state(
    has_outbound: bool,
    has_inbound: bool,
    facts: dict[str, Any] | None,
) -> tuple[str, int, str | None]:
    """Deterministic facts -> ``(state, needs_action, needs_action_reason)``.

    The classification skill supplies ``facts`` only; it never sets state or
    needs_action directly — see the module docstring. ``has_outbound`` is
    unused today (kept for the iteration-2 ``viewing_agreed`` transition,
    which will need to know whether *we* proposed the time being agreed to)
    but every call site already has it from the message list, so accepting
    it now avoids a signature change later.
    """
    del has_outbound  # reserved for iteration 2
    if not has_inbound:
        return ("contacted", 0, None)
    if facts is None:
        return ("replied", 0, None)

    if facts.get("listing_gone") or facts.get("unusual"):
        return ("answered", 1, "unusual_reply")
    if facts.get("pets_answer") == "not_allowed":
        return ("pets_declined", 1, "pets_declined")
    if facts.get("viewing_proposed"):
        return ("viewing_proposed", 1, "viewing_time")
    return ("answered", 0, None)


def reply_hash_of(thread_messages: list[dict[str, Any]]) -> str:
    """Hash of the inbound message texts — the classification's source_hash.

    Changes only when a *new* inbound message arrives, so re-syncing a
    thread with nothing new does not re-flag it for classification, and a
    resend on our side never touches it.
    """
    inbound_texts = [
        str(m.get("text") or "")
        for m in thread_messages
        if m.get("direction") == "in"
    ]
    blob = "\n".join(inbound_texts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --- send log --------------------------------------------------------------
# Outbound sends are irreversible; the log is JSON for the reason
# decisions.json is JSON (see CLAUDE.md) — a send cannot be re-scraped back
# if the DB is wiped between sending and the next sync.


def _send_log_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else config.MESSAGES_SENT_PATH


def load_send_log(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return every logged send, keyed by ``"<fb_id>:<posting_fb_id>:<kind>"``.

    A missing file is an empty dict — the normal state before the first
    automated send.
    """
    target = _send_log_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"send log is not valid JSON: {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"send log must be a JSON object: {target}")
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def send_log_key(fb_id: str, posting_fb_id: str, kind: str) -> str:
    return f"{fb_id}:{posting_fb_id}:{kind}"


def already_sent(
    fb_id: str, posting_fb_id: str, kind: str, *, path: Path | str | None = None
) -> bool:
    return send_log_key(fb_id, posting_fb_id, kind) in load_send_log(path)


def append_send_log(
    fb_id: str,
    posting_fb_id: str,
    kind: str,
    text: str,
    *,
    thread_id: str | None = None,
    profile: str | None = None,
    path: Path | str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Record one successful send. Returns the stored entry.

    ``profile`` is which Facebook account sent it (``"default"`` for the
    first/primary account, a ``--profile`` name for any other — see
    docs/messages.md "Two accounts"); stored as given, not defaulted here,
    so an old caller that never passes it does not silently claim "default".

    Writes go through a temp file plus ``os.replace``, same as
    ``decisions._write_atomic``, so an interrupted run leaves the previous
    log intact rather than a truncated file.
    """
    target = _send_log_path(path)
    log = load_send_log(target)
    entry = {
        "fb_id": str(fb_id),
        "posting_fb_id": str(posting_fb_id),
        "kind": kind,
        "text": text,
        "thread_id": thread_id,
        "profile": profile,
        "sent_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    log[send_log_key(fb_id, posting_fb_id, kind)] = entry

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".messages-sent-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return entry
