"""Tests for the file-backed user decisions store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rent_assistant import decisions as dec
from rent_assistant import storage


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "decisions.json"


def test_missing_file_is_empty(path: Path):
    assert dec.load_decisions(path) == {}


def test_save_and_load_round_trip(path: Path):
    dec.save_decision(path, "123", "rejected", "No green area")

    loaded = dec.load_decisions(path)
    assert set(loaded) == {"123"}
    assert loaded["123"]["status"] == "rejected"
    assert loaded["123"]["reason"] == "No green area"
    assert loaded["123"]["decided_at"].endswith("Z")


def test_save_keeps_other_decisions(path: Path):
    dec.save_decision(path, "123", "rejected", "too far")
    dec.save_decision(path, "456", "shortlist")

    loaded = dec.load_decisions(path)
    assert loaded["123"]["status"] == "rejected"
    assert loaded["456"]["status"] == "shortlist"
    assert loaded["456"]["reason"] is None


def test_save_overwrites_existing_decision(path: Path):
    dec.save_decision(path, "123", "rejected", "too hasty")
    dec.save_decision(path, "123", "active", "changed my mind")

    loaded = dec.load_decisions(path)
    assert len(loaded) == 1
    assert loaded["123"]["status"] == "active"
    assert loaded["123"]["reason"] == "changed my mind"


def test_invalid_status_rejected(path: Path):
    with pytest.raises(ValueError, match="unknown decision status"):
        dec.save_decision(path, "123", "maybe")
    assert not path.exists()


def test_file_is_readable_json(path: Path):
    dec.save_decision(path, "123", "shortlist", "cottage with a garden")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["123"]["reason"] == "cottage with a garden"
    # not escaped into \uXXXX — the file is meant to be hand-editable
    assert "cottage" in path.read_text(encoding="utf-8")


def test_load_rejects_non_object_file(path: Path):
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        dec.load_decisions(path)


def test_sync_to_db_fills_table(path: Path):
    conn = storage.init_db(":memory:")
    dec.save_decision(path, "123", "rejected", "noisy")
    dec.save_decision(path, "456", "shortlist")

    written = dec.sync_to_db(conn, path=path)
    assert written == 2

    rows = {r["fb_id"]: dict(r) for r in conn.execute("SELECT * FROM decisions")}
    assert rows["123"]["status"] == "rejected"
    assert rows["123"]["reason"] == "noisy"
    assert rows["456"]["status"] == "shortlist"
    conn.close()


def test_sync_to_db_drops_rows_removed_from_file(path: Path):
    conn = storage.init_db(":memory:")
    dec.save_decision(path, "123", "rejected", "noisy")
    dec.sync_to_db(conn, path=path)

    dec.sync_to_db(conn, {})
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    conn.close()


def test_sync_to_db_rejects_invalid_status(path: Path):
    conn = storage.init_db(":memory:")
    with pytest.raises(ValueError, match="unknown decision status"):
        dec.sync_to_db(conn, {"123": {"status": "maybe"}})
    conn.close()


def test_decision_survives_database_wipe(tmp_path: Path, path: Path):
    db_path = tmp_path / "rentals.db"
    conn = storage.init_db(db_path)
    dec.save_decision(path, "123", "rejected", "no dogs allowed")
    dec.sync_to_db(conn, path=path)
    conn.close()

    # the database is disposable — only the JSON file is not
    db_path.unlink()
    conn = storage.init_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0

    dec.sync_to_db(conn, path=path)
    row = conn.execute("SELECT * FROM decisions WHERE fb_id = '123'").fetchone()
    assert row["status"] == "rejected"
    assert row["reason"] == "no dogs allowed"
    conn.close()


def test_sync_to_db_feeds_fetch_enriched(path: Path):
    conn = storage.init_db(":memory:")
    conn.execute(
        """
        INSERT INTO listings (fb_id, url, title, first_seen_at, last_seen_at)
        VALUES ('123', 'https://example.com/123', 'Villa', '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z')
        """
    )
    conn.commit()

    dec.save_decision(path, "123", "shortlist", "viewing on Saturday")
    dec.sync_to_db(conn, path=path)

    row = storage.fetch_enriched(conn)[0]
    assert row["status"] == "shortlist"
    assert row["reason"] == "viewing on Saturday"
    assert row["decided_by"] == "user"
    conn.close()


def test_default_path_used_when_none(monkeypatch, tmp_path: Path):
    target = tmp_path / "nested" / "decisions.json"
    monkeypatch.setattr(dec.config, "DECISIONS_PATH", target)

    dec.save_decision(None, "123", "active")
    assert target.exists()
    assert dec.load_decisions(None)["123"]["status"] == "active"


# --- the 1-5 mark -------------------------------------------------------


def test_rating_decides_the_status():
    assert dec.status_for_rating(5) == "shortlist"
    assert dec.status_for_rating(4.5) == "shortlist"
    assert dec.status_for_rating(4) == "longlist"
    assert dec.status_for_rating(3.5) == "longlist"
    assert dec.status_for_rating(3) == "rejected"
    assert dec.status_for_rating(1) == "rejected"


def test_rating_is_stored_and_read_back(path: Path):
    dec.save_decision(path, "123", "longlist", "a bit cramped", rating=3.5)
    entry = dec.load_decisions(path)["123"]
    assert entry["rating"] == 3.5
    assert entry["status"] == "longlist"


def test_a_mark_never_leaves_a_listing_in_the_active_queue():
    """Active is the queue of what has not been looked at.

    A rated row sitting there would be indistinguishable from an untouched
    one, which is what the four statuses exist to prevent.
    """
    marks = [1, 2, 3, 3.5, 4, 4.5, 5]
    assert all(dec.status_for_rating(m) != "active" for m in marks)


def test_a_decision_without_a_rating_keeps_none(path: Path):
    dec.save_decision(path, "123", "rejected", "Patong")
    assert dec.load_decisions(path)["123"]["rating"] is None


def test_a_rating_outside_the_scale_is_refused(path: Path):
    with pytest.raises(ValueError):
        dec.save_decision(path, "123", "active", rating=0)
    with pytest.raises(ValueError):
        dec.save_decision(path, "123", "active", rating=5.5)


def test_rating_reaches_the_table(path: Path):
    conn = storage.init_db(":memory:")
    conn.execute(
        """
        INSERT INTO listings (fb_id, url, title, first_seen_at, last_seen_at)
        VALUES ('123', 'https://example.com/123', 'Villa', '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z')
        """
    )
    conn.commit()

    dec.save_decision(path, "123", "shortlist", None, rating=4.5)
    dec.sync_to_db(conn, path=path)

    row = storage.fetch_enriched(conn)[0]
    assert row["rating"] == 4.5
    assert row["status"] == "shortlist"

    registry = storage.ai_schema.load_registry()
    cells = storage.html_row(row, registry)["cells"]
    assert cells["rating"]["t"] == "4.5"
    assert cells["rating"]["n"] == 4.5
    conn.close()


def test_an_unrated_listing_shows_an_empty_cell(path: Path):
    conn = storage.init_db(":memory:")
    conn.execute(
        """
        INSERT INTO listings (fb_id, url, title, first_seen_at, last_seen_at)
        VALUES ('123', 'https://example.com/123', 'Villa', '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z')
        """
    )
    conn.commit()

    row = storage.fetch_enriched(conn)[0]
    registry = storage.ai_schema.load_registry()
    cells = storage.html_row(row, registry)["cells"]
    assert cells["rating"]["t"] == ""
    assert cells["rating"]["n"] is None
    conn.close()


# --- the post-shortlist stage --------------------------------------------


def test_save_stage_creates_an_active_entry_when_none_exists(path: Path):
    entry = dec.save_stage(path, "123", "written", posting_fb_id="123")

    assert entry["status"] == "active"
    assert entry["stage"] == "written"
    assert entry["stage_fb_id"] == "123"
    loaded = dec.load_decisions(path)["123"]
    assert loaded["stage"] == "written"


def test_save_stage_keeps_the_existing_status(path: Path):
    dec.save_decision(path, "123", "shortlist", "go and view")
    dec.save_stage(path, "123", "written", posting_fb_id="123")

    entry = dec.load_decisions(path)["123"]
    assert entry["status"] == "shortlist"
    assert entry["reason"] == "go and view"
    assert entry["stage"] == "written"


def test_save_decision_never_touches_an_existing_stage(path: Path):
    dec.save_stage(path, "123", "viewing_scheduled", posting_fb_id="123", viewing_at="2026-09-25 14:00")
    dec.save_decision(path, "123", "rejected", "plans changed")

    entry = dec.load_decisions(path)["123"]
    assert entry["status"] == "rejected"
    assert entry["stage"] == "viewing_scheduled"
    assert entry["stage_at"] == "2026-09-25 14:00"


def test_save_stage_overwrites_a_previous_stage(path: Path):
    dec.save_stage(path, "123", "written", posting_fb_id="123")
    dec.save_stage(path, "123", "viewed", posting_fb_id="123", note="taken again for renovation")

    entry = dec.load_decisions(path)["123"]
    assert entry["stage"] == "viewed"
    assert entry["stage_note"] == "taken again for renovation"


def test_invalid_stage_rejected(path: Path):
    with pytest.raises(ValueError, match="unknown stage"):
        dec.save_stage(path, "123", "maybe")
    assert not path.exists()


def test_clear_stage_drops_it_but_keeps_status(path: Path):
    dec.save_decision(path, "123", "shortlist")
    dec.save_stage(path, "123", "written", posting_fb_id="123")

    entry = dec.clear_stage(path, "123")

    assert entry["status"] == "shortlist"
    assert "stage" not in entry
    assert "stage_fb_id" not in entry
    loaded = dec.load_decisions(path)["123"]
    assert "stage" not in loaded


def test_clear_stage_on_unknown_listing_is_a_noop(path: Path):
    assert dec.clear_stage(path, "does-not-exist") is None


def test_clear_stage_without_a_stage_is_a_noop(path: Path):
    dec.save_decision(path, "123", "shortlist")
    entry = dec.clear_stage(path, "123")
    assert entry["status"] == "shortlist"
    assert "stage" not in entry


def test_sync_to_db_carries_the_stage(path: Path):
    conn = storage.init_db(":memory:")
    dec.save_stage(
        path, "123", "viewing_scheduled",
        posting_fb_id="999", viewing_at="2026-09-25 14:00", note="from the street side",
    )

    dec.sync_to_db(conn, path=path)

    row = dict(conn.execute("SELECT * FROM decisions WHERE fb_id = '123'").fetchone())
    assert row["stage"] == "viewing_scheduled"
    assert row["stage_fb_id"] == "999"
    assert row["stage_at"] == "2026-09-25 14:00"
    assert row["stage_note"] == "from the street side"
    conn.close()


def test_sync_to_db_rejects_invalid_stage(path: Path):
    conn = storage.init_db(":memory:")
    with pytest.raises(ValueError, match="unknown stage"):
        dec.sync_to_db(conn, {"123": {"status": "active", "stage": "maybe"}})
    conn.close()


def test_stage_reaches_the_html_row(path: Path):
    conn = storage.init_db(":memory:")
    conn.execute(
        """
        INSERT INTO listings (fb_id, url, title, first_seen_at, last_seen_at)
        VALUES ('123', 'https://example.com/123', 'Villa', '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z')
        """
    )
    conn.commit()

    dec.save_stage(
        path, "123", "viewing_scheduled",
        posting_fb_id="123", viewing_at="2026-09-25 14:00", note="second person meets them",
    )
    dec.sync_to_db(conn, path=path)

    row = storage.fetch_enriched(conn)[0]
    assert row["stage"] == "viewing_scheduled"

    registry = storage.ai_schema.load_registry()
    html_row = storage.html_row(row, registry)
    assert html_row["stage"] == "viewing_scheduled"
    cells = html_row["cells"]
    assert cells["stage"]["t"] == "Viewing scheduled"
    assert cells["stage_fb_id"]["t"] == "123"
    assert cells["stage_at"]["t"] == "2026-09-25 14:00"
    assert cells["stage_note"]["t"] == "second person meets them"
    conn.close()


# --- stage_profile (which Facebook account wrote) --------------------------


def test_save_stage_records_profile(path: Path):
    dec.save_stage(path, "123", "written", profile="default")
    entry = dec.load_decisions(path)["123"]
    assert entry["stage_profile"] == "default"


def test_save_stage_without_profile_leaves_it_untouched(path: Path):
    dec.save_stage(path, "123", "written", profile="partner")
    dec.save_stage(path, "123", "viewing_scheduled", viewing_at="2026-09-25 14:00")
    entry = dec.load_decisions(path)["123"]
    assert entry["stage_profile"] == "partner"


def test_save_decision_preserves_stage_profile(path: Path):
    dec.save_stage(path, "123", "written", profile="default")
    dec.save_decision(path, "123", "rejected", "too far")
    entry = dec.load_decisions(path)["123"]
    assert entry["stage_profile"] == "default"
    assert entry["status"] == "rejected"


def test_clear_stage_drops_profile(path: Path):
    dec.save_stage(path, "123", "written", profile="default")
    dec.clear_stage(path, "123")
    entry = dec.load_decisions(path)["123"]
    assert "stage_profile" not in entry


def test_sync_to_db_carries_stage_profile(path: Path):
    dec.save_stage(path, "123", "written", profile="partner")
    conn = storage.init_db(":memory:")
    dec.sync_to_db(conn, dec.load_decisions(path))
    row = conn.execute("SELECT stage_profile FROM decisions WHERE fb_id='123'").fetchone()
    assert row["stage_profile"] == "partner"
