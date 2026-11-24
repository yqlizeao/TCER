"""全局产物索引：分类规则 + Antigravity brain/teamwork + Claude plans/file-history。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tcer.core import antigravity_reader, artifacts


@pytest.fixture(autouse=True)
def _clear_caches():
    """antigravity 父指针/summaries 进程级缓存，测试间必须互不可见。"""
    antigravity_reader.reset_summaries_cache()
    artifacts._PLAN_LINKS.clear()
    yield
    antigravity_reader.reset_summaries_cache()
    artifacts._PLAN_LINKS.clear()


def _write_summaries(root: Path, rows) -> None:
    """rows: (cid, parent_cid, uri, title)；schema 含父指针与标题列。"""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "conversation_summaries.db")
    conn.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, "
        "workspace_uris TEXT, last_modified_time INTEGER, parent_conversation_id TEXT, "
        "title TEXT)"
    )
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
        [(cid, json.dumps([uri]), 1, par, title) for cid, par, uri, title in rows],
    )
    conn.commit()
    conn.close()


def _mk_session_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
                 "status INTEGER, metadata BLOB)")
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# 分类规则
# --------------------------------------------------------------------------- #

def test_classify_kinds():
    c = artifacts._classify
    assert c(("brain",), "implementation_plan.md") == "plan"
    assert c((), "walkthrough.md") == "plan"
    assert c((), "deep_research.md") == "report"
    assert c((), "final_report.md") == "report"
    assert c((), "handoff_next.md") == "report"
    assert c((), "notes.md") == "doc"
    assert c((), "shot.png") == "media"
    assert c((), "tool.py") == "code"
    assert c((), "rows.json") == "data"
    assert c((".user_uploaded",), "avatar.jpg") == "upload"
    assert c(("scratch",), "msg_1_worker.md") == "worker"
    assert c((".tempmediaStorage",), "x.jpg") == "internal"
    assert c((), "blob.bin") == "other"


# --------------------------------------------------------------------------- #
# Antigravity
# --------------------------------------------------------------------------- #

def _ag_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-none"))
    (tmp_path / "conversations").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_antigravity_brain_collects_and_folds_to_parent(tmp_path: Path, monkeypatch):
    """子会话 brain 目录归到根会话（与卡面对齐），项目/标题取根会话的。"""
    root = _ag_root(tmp_path, monkeypatch)
    _mk_session_db(root / "conversations" / "parent1.db")
    _mk_session_db(root / "conversations" / "sub1.db")
    _write_summaries(root, [
        ("parent1", "", "file:///C:/proj/demo", "主会话标题"),
        ("sub1", "parent1", "file:///C:/proj/demo", ""),
    ])
    brain = root / "brain"
    (brain / "sub1").mkdir(parents=True)
    (brain / "sub1" / "implementation_plan.md").write_text("plan", encoding="utf-8")
    (brain / "parent1").mkdir()
    (brain / "parent1" / "deep_research.md").write_text("r" * 100, encoding="utf-8")

    arts = {a.path.name: a for a in artifacts.collect() if a.source == "antigravity"}
    plan = arts["implementation_plan.md"]
    assert plan.kind == "plan"
    assert plan.session_id == "parent1"          # 子会话产物挂根会话
    assert plan.session_title == "主会话标题"
    assert plan.project == "demo"                # 工作区显示名
    research = arts["deep_research.md"]
    assert research.kind == "report"
    assert research.session_id == "parent1"
    assert research.size == 100


def test_antigravity_system_generated_aggregated(tmp_path: Path, monkeypatch):
    """.system_generated 逐 (cid, 二级目录) 聚合；logs/media 标 media。"""
    root = _ag_root(tmp_path, monkeypatch)
    _mk_session_db(root / "conversations" / "p1.db")
    _write_summaries(root, [("p1", "", "file:///C:/proj/x", "T")])
    sg = root / "brain" / "p1" / ".system_generated"
    (sg / "logs" / "chunks").mkdir(parents=True)
    (sg / "logs" / "media").mkdir(parents=True)
    (sg / "steps" / "2").mkdir(parents=True)
    for i in range(3):
        (sg / "logs" / "chunks" / f"chunk{i}.jsonl").write_text("x")
        (sg / "logs" / "media" / f"m{i}.jpg").write_text("x")
    (sg / "steps" / "2" / "a.txt").write_text("x")

    arts = [a for a in artifacts.collect() if a.source == "antigravity"]
    by_name = {a.path.name: a for a in arts}
    assert by_name["chunks"].kind == "internal"
    assert by_name["chunks"].count == 3
    assert by_name["media"].kind == "media"
    assert by_name["media"].count == 3
    # steps 二级目录聚合成一行 internal
    assert by_name["steps"].kind == "internal" and by_name["steps"].count == 1
    # 内部文件不逐条出列
    assert not any(a.path.name == "chunk0.jsonl" for a in arts)


def test_antigravity_zones_and_upload(tmp_path: Path, monkeypatch):
    root = _ag_root(tmp_path, monkeypatch)
    _mk_session_db(root / "conversations" / "p1.db")
    _write_summaries(root, [("p1", "", "file:///C:/proj/x", "T")])
    brain = root / "brain" / "p1"
    (brain / ".user_uploaded").mkdir(parents=True)
    (brain / ".user_uploaded" / "shot.png").write_text("x")
    (brain / "scratch").mkdir()
    (brain / "scratch" / "msg_1_w.md").write_text("x")
    tw = root / "scratch" / "teamwork" / "p1" / ".agents" / "teamwork" / "w1"
    tw.mkdir(parents=True)
    (tw / "BRIEFING.md").write_text("x")

    arts = artifacts.collect()
    by_name = {a.path.name: a for a in arts}
    assert by_name["shot.png"].kind == "upload"
    assert by_name["msg_1_w.md"].kind == "worker"
    assert by_name["BRIEFING.md"].zone == "teamwork"


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

def _claude_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path / "ag-none"))
    return root


def test_claude_plans_linked_via_planfilepath(tmp_path: Path, monkeypatch):
    root = _claude_root(tmp_path, monkeypatch)
    plans = root / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sunny-plan.md"
    plan.write_text("# plan", encoding="utf-8")
    sid = "abc12345-0000-0000-0000-000000000000"
    proj = root / "projects" / "c--GitHub-demo"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(json.dumps({
        "type": "attachment",
        "attachment": {"plan_mode": {"planFilePath": str(plan)}},
    }) + "\n", encoding="utf-8")

    arts = [a for a in artifacts.collect() if a.zone == "plans"]
    assert len(arts) == 1
    a = arts[0]
    assert a.session_id == sid
    assert a.project == "c--GitHub-demo"
    assert a.kind == "plan"


def test_claude_file_history_and_unlinked_plan(tmp_path: Path, monkeypatch):
    root = _claude_root(tmp_path, monkeypatch)
    sid = "ffffffff-0000-0000-0000-000000000000"
    proj = root / "projects" / "c--GitHub-x"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    fh = root / "file-history" / sid
    fh.mkdir(parents=True)
    (fh / "deadbeef@v1").write_text("old content")
    plans = root / "plans"
    plans.mkdir()
    (plans / "orphan.md").write_text("x")

    arts = artifacts.collect()
    backup = next(a for a in arts if a.kind == "backup")
    assert backup.session_id == sid
    assert backup.project == "c--GitHub-x"
    orphan = next(a for a in arts if a.path.name == "orphan.md")
    assert orphan.kind == "plan" and orphan.session_id == ""


def test_empty_when_no_roots(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path / "ag"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cl"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True)
    assert artifacts.collect() == []
