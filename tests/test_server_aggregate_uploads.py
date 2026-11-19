"""Aggregate-only snapshots are scoped to their uploader and counted once."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TCER_SERVER_DB", str(tmp_path / "aggregate.db"))
    mod = importlib.reload(importlib.import_module("db"))
    mod.init_db()
    return mod


def _upload(db, uploader="alice", tokens=1000, session=False, ts=1700000000):
    row = {"total_tokens": tokens}
    if session:
        row["session_id"] = "s1"
    db.insert_records(
        uploaded_by=uploader, person="person", project="project",
        aggregate=None if session else row,
        sessions=[row] if session else [], generated_at=ts,
    )


def _total(db, **filters):
    return db.overview(projects=["project"], **filters)["totals"]["total_tokens"]


def test_other_uploaders_session_keeps_aggregate(db):
    _upload(db)
    _upload(db, "bob", 200, session=True)
    assert _total(db) == 1200


def test_repeated_snapshot_counts_once(db, monkeypatch):
    monkeypatch.setattr(db.time, "time", lambda: 1800000000)
    _upload(db)
    _upload(db)
    assert _total(db) == 1000
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 2


def test_same_uploader_session_hides_later_aggregate(db):
    _upload(db, tokens=200, session=True)
    _upload(db)
    assert _total(db) == 200


def test_different_uploaders_aggregates_both_count(db):
    _upload(db)
    _upload(db, "bob", 200)
    assert _total(db) == 1200


def test_project_private_hides_superseded_public_snapshots(db):
    _upload(db)
    with db.connect() as conn:
        old_id = conn.execute("SELECT id FROM uploads").fetchone()[0]
    assert db.set_session_visibility(old_id, "alice", "public") == "ok"
    _upload(db)
    with db.connect() as conn:
        latest_id = conn.execute("SELECT MAX(id) FROM uploads").fetchone()[0]
    assert db.set_session_visibility(latest_id, "alice", "public") == "ok"
    assert isinstance(db.session_detail(old_id, viewer="bob"), dict)
    _upload(db, "bob", 200)
    assert db.set_project_visibility("alice", "project", "private") == 1
    assert db.session_detail(old_id, viewer="bob") == "forbidden"
    assert db.session_detail(latest_id, viewer="bob") == "forbidden"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT visibility FROM uploads WHERE uploaded_by='bob'"
        ).fetchone()[0] == "private"



def test_project_public_keeps_superseded_snapshots_private(db):
    _upload(db)
    _upload(db)
    with db.connect() as conn:
        old_id, latest_id = (r[0] for r in conn.execute(
            "SELECT id FROM uploads ORDER BY id"))
    assert db.set_project_visibility("alice", "project", "public") == 1
    assert isinstance(db.session_detail(latest_id, viewer="bob"), dict)
    assert db.session_detail(old_id, viewer="bob") == "forbidden"

@pytest.mark.parametrize("times", [(1800000000, 1800000001),
                                   (1800000000, 1800000000),
                                   (1800000001, 1800000000)])
def test_latest_snapshot_uses_upload_time_then_id(db, monkeypatch, times):
    monkeypatch.setattr(db.time, "time", lambda: times[0])
    _upload(db, tokens=1000)
    monkeypatch.setattr(db.time, "time", lambda: times[1])
    _upload(db, tokens=1500)
    assert _total(db) == (1000 if times[0] > times[1] else 1500)


def test_latest_snapshot_selected_before_time_filter(db, monkeypatch):
    monkeypatch.setattr(db.time, "time", lambda: 1800000000)
    _upload(db, ts=1700000000)
    monkeypatch.setattr(db.time, "time", lambda: 1800000001)
    _upload(db, tokens=1500, ts=1700000100)
    assert _total(db, end_ts=1700000050) == 0
    assert _total(db, start_ts=1700000050) == 1500
