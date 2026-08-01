"""Tests for analyze orchestration — Claude path contracts + cancel + task_type."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tcer.core import analyze, loc, metrics, paths, reader


def _usage(i=10, cw=0, cr=0, o=5) -> dict:
    return {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }


def _assistant(usage, *, model="claude-opus-4-8", ts="2026-03-06T10:00:00Z",
               msg_id=None, content=None) -> dict:
    msg = {
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": "x"}],
        "usage": usage,
    }
    if msg_id is not None:
        msg["id"] = msg_id
    return {"type": "assistant", "timestamp": ts, "message": msg}


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _seed_claude_project(tmp_path: Path, monkeypatch, hash_name: str = "c--tmp-proj"):
    """Point CLAUDE_CONFIG_DIR at tmp and create one project hash folder."""
    root = tmp_path / ".claude"
    proj = root / "projects" / hash_name
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    paths.reset_claude_roots_cache()
    return proj, hash_name


def test_default_task_type_is_code_creation():
    assert metrics.DEFAULT_TASK_TYPE == "code_creation"
    assert metrics.resolve_task_type(None) == "code_creation"
    assert metrics.resolve_task_type("feature") == "code_creation"
    assert metrics.resolve_task_type("unknown-xyz") == "code_creation"
    assert metrics.coerce_task_type("feature") == "code_creation"
    assert metrics.coerce_task_type("unknown-xyz") is None


def test_task_categories_ssot_matches_config():
    """TASK_CATEGORIES / TTAF come from composite_baselines.json, not a hard-coded twin."""
    cfg = metrics._load_composite_config()
    keys = {k for k, v in cfg["task_categories"].items()
            if isinstance(v, dict) and "ttaf" in v}
    assert set(metrics.TASK_CATEGORIES) == keys
    assert set(metrics.TTAF) == keys
    for k in keys:
        assert metrics.TTAF[k] == float(cfg["task_categories"][k]["ttaf"])
        assert metrics.TASK_CATEGORIES[k]["ttaf"] == cfg["task_categories"][k]["ttaf"]


def test_analyze_claude_folds_subagent_and_aggregate_ctei_valid(tmp_path, monkeypatch):
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    main = proj / "SID-MAIN.jsonl"
    sub = proj / "SID-MAIN" / "subagents" / "agent-1.jsonl"

    # Main: tokens + Write 3 lines
    _write_jsonl(main, [
        {"type": "user", "message": {"role": "user",
         "content": [{"type": "text", "text": "do work"}]},
         "sessionId": "SID-MAIN", "cwd": str(tmp_path / "code"),
         "timestamp": "2026-03-06T10:00:00Z"},
        _assistant(_usage(100, 0, 0, 50), msg_id="m1", ts="2026-03-06T10:00:01Z",
                   content=[{"type": "tool_use", "name": "Write", "id": "t1",
                             "input": {"file_path": "a.py", "content": "1\n2\n3"}}]),
    ])
    # Subagent: more tokens + Edit on same path (2 more edits → high churn if ≥3 total)
    _write_jsonl(sub, [
        _assistant(_usage(20, 0, 0, 10), msg_id="s1", ts="2026-03-06T10:00:05Z",
                   content=[
                       {"type": "tool_use", "name": "Edit", "id": "e1",
                        "input": {"file_path": "a.py", "old_string": "1", "new_string": "1a"}},
                       {"type": "tool_use", "name": "Edit", "id": "e2",
                        "input": {"file_path": "a.py", "old_string": "2", "new_string": "2b"}},
                   ]),
    ])

    result = analyze.analyze_project(h, source="claude")
    assert result.n_sessions == 1
    assert result.n_subagents == 1
    assert len(result.reports) == 1
    r = result.reports[0]
    assert r.usage.input_tokens == 120
    assert r.usage.output_tokens == 60
    assert r.net_loc is not None and r.net_loc > 0
    # Same path edited 3 times across main+sub → one high-churn file, not two.
    assert r.high_churn_file_count == 1
    assert r.high_churn_details and "a.py" in r.high_churn_details
    # CTEI 三因子化后聚合有效（不再抑制）。
    assert result.aggregate.ctei is not None
    assert result.aggregate.grade is not None
    # Default task_type yields NTCER when TCER is computable.
    assert r.task_type == "code_creation"
    assert r.ntcer is not None


def test_memory_files_on_every_report(tmp_path, monkeypatch):
    """项目级 memory/ 计数须挂到聚合与每个会话报告，两种视图一致——
    此前只挂聚合，会话视图显示「-」被折叠，但点击弹窗读聚合仍列文件（观感矛盾）。"""
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [_assistant(_usage(), msg_id="a1")])
    _write_jsonl(proj / "s2.jsonl", [_assistant(_usage(), msg_id="a2")])
    mem = proj / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("x", encoding="utf-8")
    (mem / "note.md").write_text("y", encoding="utf-8")

    result = analyze.analyze_project(project=h, no_loc=True)
    assert result.aggregate.memory_files is not None
    assert len(result.aggregate.memory_files) == 2
    assert result.aggregate.memory_dir == str(mem)
    # 关键回归：每个会话报告都带同一项目级计数（网格会话视图不再显示「-」）。
    assert len(result.reports) == 2
    for r in result.reports:
        assert r.memory_files == result.aggregate.memory_files
        assert r.memory_dir == str(mem)


def test_memory_files_absent_when_no_dir(tmp_path, monkeypatch):
    """无 memory/ 目录时聚合与会话报告都保持 None（网格显示「-」，不误报）。"""
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [_assistant(_usage(), msg_id="a1")])
    result = analyze.analyze_project(project=h, no_loc=True)
    assert result.aggregate.memory_files is None
    for r in result.reports:
        assert r.memory_files is None


def test_analyze_claude_date_filter(tmp_path, monkeypatch):
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    early = proj / "early.jsonl"
    late = proj / "late.jsonl"
    _write_jsonl(early, [
        _assistant(_usage(), msg_id="e", ts="2026-01-01T12:00:00Z"),
    ])
    _write_jsonl(late, [
        _assistant(_usage(), msg_id="l", ts="2026-03-15T12:00:00Z"),
    ])

    all_r = analyze.analyze_project(h)
    assert all_r.n_sessions == 2

    march = analyze.analyze_project(h, since="2026-03-01", until="2026-03-31")
    assert march.n_sessions == 1
    assert march.reports[0].meta.path.stem == "late"

    none = analyze.analyze_project(h, since="2025-01-01", until="2025-12-31")
    assert none.n_sessions == 0


def test_scan_session_matches_separate_usage_and_loc(tmp_path):
    """Single-pass scan_session must equal aggregate_usage + session_loc_full."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _assistant(_usage(5, 1, 2, 3), msg_id="m1",
                   content=[
                       {"type": "text", "text": "hi"},
                       {"type": "tool_use", "name": "Write", "id": "w1",
                        "input": {"file_path": "x.py", "content": "a\nb\nc"}},
                       {"type": "tool_use", "name": "Edit", "id": "e1",
                        "input": {"file_path": "x.py", "old_string": "a", "new_string": "A\nA2"}},
                   ]),
    ])
    u_only = reader.aggregate_usage(p)
    sl_only = loc.session_loc_full(p)
    u_scan, sl_scan = reader.scan_session(p, with_loc=True)
    assert u_scan.input_tokens == u_only.input_tokens
    assert u_scan.output_tokens == u_only.output_tokens
    assert u_scan.tool_calls == u_only.tool_calls
    assert sl_scan is not None
    assert (sl_scan.added, sl_scan.deleted, sl_scan.unseen_writes) == (
        sl_only.added, sl_only.deleted, sl_only.unseen_writes,
    )
    assert sl_scan.file_edit_counts == sl_only.file_edit_counts


def test_merge_session_locs_recomputes_high_churn():
    a = loc.SessionLoc(added=3, deleted=0, high_churn_files=1,
                       file_edit_counts={"a.py": 2, "b.py": 3})
    b = loc.SessionLoc(added=1, deleted=0, high_churn_files=1,
                       file_edit_counts={"a.py": 2})  # a.py total 4 → high churn once
    m = loc.merge_session_locs([a, b])
    assert m.added == 4
    assert m.file_edit_counts["a.py"] == 4
    # a.py (4) + b.py (3) → 2 high-churn files, not 1+1=2 from naive sum of flags
    # (naive sum of high_churn_files would also be 2 here; stronger case below)
    assert m.high_churn_files == 2

    # Stronger: each side thinks 1 file is high-churn, but it's the SAME path.
    c = loc.SessionLoc(added=1, deleted=0, high_churn_files=1,
                       file_edit_counts={"same.py": 3})
    d = loc.SessionLoc(added=1, deleted=0, high_churn_files=1,
                       file_edit_counts={"same.py": 1})
    m2 = loc.merge_session_locs([c, d])
    assert m2.file_edit_counts["same.py"] == 4
    assert m2.high_churn_files == 1  # not 2


def test_analyze_cancel_raises(tmp_path, monkeypatch):
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [
        _assistant(_usage(), msg_id="a", ts="2026-03-06T10:00:00Z"),
    ])
    ev = threading.Event()
    ev.set()
    with pytest.raises(analyze.AnalysisCancelled):
        analyze.analyze_project(h, cancel_event=ev)


def test_analyze_project_scopes_to_ref_root(tmp_path, monkeypatch):
    """ref.config_root 限定 analyze 只见该根会话（跨根同 hash 不串）。"""
    from tcer.core.models import ProjectRef
    h = "c--GitHub-Demo"
    root_a = tmp_path / ".claude"
    root_b = tmp_path / ".zclaude"
    _write_jsonl(root_a / "projects" / h / "SID-A.jsonl",
                 [_assistant(_usage(100, 0, 0, 50), msg_id="ma",
                             content=[{"type": "text", "text": "a"}])])
    _write_jsonl(root_b / "projects" / h / "SID-B.jsonl",
                 [_assistant(_usage(200, 0, 0, 50), msg_id="mb",
                             content=[{"type": "text", "text": "b"}])])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root_a))
    paths.reset_claude_roots_cache()
    ref_a = ProjectRef(source="claude", key=h, display_name=h, cwd=None,
                       path=root_a / "projects" / h, config_root=root_a)
    result = analyze.analyze_project(h, project_ref=ref_a, no_loc=True)
    assert len(result.reports) == 1
    assert result.reports[0].meta.path.stem == "SID-A"  # 只 root_a；root_b 的 SID-B 不串入


def test_file_cache_invalidates_on_mtime(tmp_path):
    from tcer.core import file_cache

    file_cache.clear()
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_assistant(_usage(1, 0, 0, 1), msg_id="a")])
    u1, _ = reader.scan_session(p, with_loc=False, include_user_texts=False)
    u2, _ = reader.scan_session(p, with_loc=False, include_user_texts=False)
    assert u1 is u2  # same cached object
    assert file_cache.stats()["entries"] >= 1

    # Mutate file → new signature → recompute
    import time
    time.sleep(0.02)
    _write_jsonl(p, [_assistant(_usage(99, 0, 0, 1), msg_id="b")])
    u3, _ = reader.scan_session(p, with_loc=False, include_user_texts=False)
    assert u3.input_tokens == 99
    assert u3 is not u1
    file_cache.clear()


def test_scan_session_caches_with_cancel_check(tmp_path):
    # GUI always passes cancel_check; completed scans must still be cached.
    from tcer.core import file_cache

    file_cache.clear()
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_assistant(_usage(1, 0, 0, 1), msg_id="a")])
    noop = lambda: None
    u1, _ = reader.scan_session(p, with_loc=False, include_user_texts=False,
                                cancel_check=noop)
    u2, _ = reader.scan_session(p, with_loc=False, include_user_texts=False,
                                cancel_check=noop)
    assert u1 is u2
    assert file_cache.stats()["entries"] >= 1
    file_cache.clear()


def test_cancelled_scan_not_cached(tmp_path):
    # A cancel raises inside the factory → the partial scan never enters the cache.
    from tcer.core import file_cache

    file_cache.clear()
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_assistant(_usage(1, 0, 0, 1), msg_id="a")])

    def _cancel():
        raise analyze.AnalysisCancelled()

    with pytest.raises(analyze.AnalysisCancelled):
        reader.scan_session(p, with_loc=False, include_user_texts=False,
                            cancel_check=_cancel)
    assert file_cache.stats()["entries"] == 0
    file_cache.clear()


def test_analyze_with_cancel_event_populates_cache(tmp_path, monkeypatch):
    # End-to-end: analyze_project with a (never-set) cancel_event — the GUI's
    # normal mode — must populate the mtime cache so reanalyze is cheap.
    from tcer.core import file_cache

    file_cache.clear()
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [
        _assistant(_usage(), msg_id="a", ts="2026-03-06T10:00:00Z"),
    ])
    analyze.analyze_project(h, cancel_event=threading.Event())
    assert file_cache.stats()["entries"] >= 1
    file_cache.clear()


def test_analyze_omits_user_texts_but_count_and_lazy_read(tmp_path, monkeypatch):
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [
        {"type": "user", "message": {"role": "user",
         "content": [{"type": "text", "text": "hello world"}]},
         "timestamp": "2026-03-06T10:00:00Z"},
        _assistant(_usage(), msg_id="a", ts="2026-03-06T10:00:01Z"),
    ])
    result = analyze.analyze_project(h)
    r = result.reports[0]
    assert r.usage.user_msgs == 1
    assert r.usage.user_message_texts == []  # lazy path
    assert reader.read_user_messages(proj / "s1.jsonl") == ["hello world"]


def test_analyze_legacy_feature_task_type(tmp_path, monkeypatch):
    """Legacy 'feature' alias resolves to code_creation so NTCER is populated."""
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    _write_jsonl(proj / "s1.jsonl", [
        _assistant(_usage(1_000_000, 0, 0, 0), msg_id="a", ts="2026-03-06T10:00:00Z",
                   content=[{"type": "tool_use", "name": "Write", "id": "w",
                             "input": {"file_path": "a.py", "content": "line1\nline2"}}]),
    ])
    result = analyze.analyze_project(h, task_type="feature")
    r = result.reports[0]
    assert r.task_type == "code_creation"
    assert r.tcer is not None
    assert r.ntcer == pytest.approx(r.tcer)


def test_analyze_auto_task_type_infers_per_session(tmp_path, monkeypatch):
    """task_type=auto uses LOC/tool signals instead of a fixed category."""
    proj, h = _seed_claude_project(tmp_path, monkeypatch)
    # High Write output, no Grep → should land on code_creation
    _write_jsonl(proj / "create.jsonl", [
        _assistant(_usage(100_000, 0, 0, 10_000), msg_id="c", ts="2026-03-06T10:00:00Z",
                   content=[{"type": "tool_use", "name": "Write", "id": "w",
                             "input": {"file_path": "a.py",
                                       "content": "\n".join(f"line{i}" for i in range(80))}}]),
    ])
    # Only Grep, no LOC → non_coding
    _write_jsonl(proj / "research.jsonl", [
        _assistant(_usage(200_000, 0, 0, 5_000), msg_id="r", ts="2026-03-06T11:00:00Z",
                   content=[{"type": "tool_use", "name": "Grep", "id": "g",
                             "input": {"pattern": "foo", "path": "."}}]),
    ])
    result = analyze.analyze_project(h, task_type="auto")
    by_stem = {r.meta.path.stem: r for r in result.reports}
    assert by_stem["create"].task_type == "code_creation"
    assert by_stem["research"].task_type == "non_coding"
    # Aggregate uses majority (tie → creation first in majority_task_type order,
    # here 1+1 so creation wins by taxonomy order).
    assert result.aggregate.task_type in ("code_creation", "non_coding")


def test_filter_activity_window_overlaps():
    """since 过滤按活跃区间 [started, ended]：跨天会话在窗口内仍活跃则命中。

    与左栏 mtime（最后写入≈ended）语义一致，避免「左栏显示、点进去 0 会话」。
    """
    from datetime import datetime
    from tcer.core.analyze import _filter_by_started_at
    from tcer.core.models import TokenUsage

    def ms(s: str) -> int:
        return int(datetime.strptime(s, "%Y-%m-%d").timestamp() * 1000)  # naive → 本地

    def usage(started: str, ended: str | None) -> TokenUsage:
        u = TokenUsage()
        u.started_at = ms(started)
        u.ended_at = ms(ended) if ended else None
        return u

    files = [Path("a"), Path("b"), Path("c")]
    table = {
        Path("a"): usage("2026-07-29", "2026-07-30"),   # 跨天：昨天起、今天还活跃
        Path("b"): usage("2026-07-09", "2026-07-09"),   # 单天旧
        Path("c"): usage("2026-07-30", "2026-07-30"),   # 单天新
    }
    kept = _filter_by_started_at(list(files), lambda f: table[f], "2026-07-30", None)
    assert set(kept) == {Path("a"), Path("c")}          # 跨天 + 新命中；旧排除

    # ended_at 缺失 → 降级 started_at（退回单天严格语义）
    table[Path("a")] = usage("2026-07-29", None)
    kept2 = _filter_by_started_at(list(files), lambda f: table[f], "2026-07-30", None)
    assert set(kept2) == {Path("c")}
