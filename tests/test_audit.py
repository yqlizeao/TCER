"""Closed-loop audit harness — synthetic project fixtures (no real ~/.claude required)."""
from __future__ import annotations

import json
from pathlib import Path

from tcer.core import audit, paths, reader
from tcer.core.models import ProjectRef


def _usage(i=10, cw=0, cr=0, o=5) -> dict:
    return {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }


def _assistant(usage, *, model="claude-opus-4-8", ts="2026-03-06T10:00:00Z",
               msg_id="m1", content=None) -> dict:
    msg = {
        "role": "assistant",
        "model": model,
        "id": msg_id,
        "content": content or [{"type": "text", "text": "x"}],
        "usage": usage,
    }
    return {"type": "assistant", "timestamp": ts, "message": msg}


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _seed_claude(tmp_path: Path, monkeypatch, hash_name: str = "c--tmp-audit"):
    root = tmp_path / ".claude"
    proj = root / "projects" / hash_name
    proj.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    paths.reset_claude_roots_cache()
    return proj, hash_name


def test_audit_claude_project_passes_on_synthetic(tmp_path, monkeypatch):
    proj, h = _seed_claude(tmp_path, monkeypatch)
    # Main + subagent: tokens must fold
    _write_jsonl(proj / "SID1.jsonl", [
        {"type": "user", "message": {"role": "user",
         "content": [{"type": "text", "text": "hi"}]},
         "sessionId": "SID1", "cwd": str(tmp_path / "code"),
         "timestamp": "2026-03-06T10:00:00Z"},
        _assistant(_usage(100, 0, 50, 20), msg_id="a1", ts="2026-03-06T10:00:01Z",
                   content=[{"type": "tool_use", "name": "Write", "id": "w1",
                             "input": {"file_path": "a.py", "content": "1\n2\n3"}}]),
        # Dup line same id — must not double-count tokens
        _assistant(_usage(100, 0, 50, 20), msg_id="a1", ts="2026-03-06T10:00:02Z",
                   content=[{"type": "tool_use", "name": "Edit", "id": "e1",
                             "input": {"file_path": "a.py", "old_string": "1",
                                       "new_string": "1a"}}]),
    ])
    _write_jsonl(proj / "SID1" / "subagents" / "agent-1.jsonl", [
        _assistant(_usage(30, 0, 0, 5), msg_id="s1", ts="2026-03-06T10:00:05Z",
                   content=[{"type": "tool_use", "name": "Read", "id": "r1",
                             "input": {"file_path": "a.py"}}]),
    ])

    result = audit.audit_project(h, source="claude", top=0, task_type="code_creation")
    assert result.error is None, result.error
    assert result.ok, audit.format_report([result], verbose=True)
    assert result.n_sessions == 1
    assert result.n_subagents == 1
    # 100+50+20 main + 30+5 sub = 205 total tokens (dup line ignored)
    assert result.sessions[0].ok
    tok_check = next(c for c in result.sessions[0].checks if c.name == "tokens_total")
    assert tok_check.ok and tok_check.actual == 205


def test_audit_detects_token_mismatch(tmp_path, monkeypatch):
    """If analyze output drifts from raw rescan, audit must FAIL."""
    proj, h = _seed_claude(tmp_path, monkeypatch)
    _write_jsonl(proj / "SID2.jsonl", [
        _assistant(_usage(10, 0, 0, 5), msg_id="a"),
    ])
    real_analyze = audit.analyze.analyze_project

    def _corrupt(project, **kwargs):
        result = real_analyze(project, **kwargs)
        for rep in result.reports:
            rep.usage.input_tokens += 999  # property total follows
        result.aggregate.usage.input_tokens += 999
        return result

    monkeypatch.setattr(audit.analyze, "analyze_project", _corrupt)
    result = audit.audit_project(h, source="claude", top=0, no_loc=True)
    assert not result.ok
    assert result.n_fail >= 1


def test_format_report_contains_pass_fail():
    pa = audit.ProjectAudit(
        source="claude", project_key="x", display_name="x",
        checks=[audit.Check("t", True)],
    )
    text = audit.format_report([pa])
    assert "PASS" in text
    pa2 = audit.ProjectAudit(
        source="claude", project_key="y", display_name="y",
        checks=[audit.Check("t", False, expected=1, actual=2)],
    )
    assert "FAIL" in audit.format_report([pa2])
    quiet = audit.format_report([pa], quiet=True)
    assert "→ PASS" in quiet
    quiet_f = audit.format_report([pa2], quiet=True)
    assert "→ FAIL" in quiet_f and "y" in quiet_f


def test_summarize_ci_payload():
    ok = audit.ProjectAudit(
        source="claude", project_key="a", display_name="a",
        checks=[audit.Check("t", True)], sessions=[],
    )
    bad = audit.ProjectAudit(
        source="claude", project_key="b", display_name="b",
        checks=[audit.Check("t", False, expected=1, actual=2)],
    )
    s = audit.summarize([ok, bad])
    assert s["ok"] is False
    assert s["n_ok"] == 1 and s["n_fail"] == 1
    assert s["failures"][0]["project_key"] == "b"


def test_cli_list_and_audit_help(tmp_path, monkeypatch):
    proj, h = _seed_claude(tmp_path, monkeypatch)
    _write_jsonl(proj / "S.jsonl", [_assistant(_usage(), msg_id="m")])
    # --list
    rc = audit.main(["--list", "--source", "claude"])
    assert rc == 0
    # audit synthetic project
    rc = audit.main(["--source", "claude", "--project", h, "--top", "0"])
    assert rc == 0


def test_audit_ref_accepts_project_ref(tmp_path, monkeypatch):
    proj, h = _seed_claude(tmp_path, monkeypatch)
    _write_jsonl(proj / "S.jsonl", [_assistant(_usage(5, 0, 0, 1), msg_id="m")])
    ref = ProjectRef(source="claude", key=h, display_name=h, cwd=None, path=proj)
    pa = audit.audit_ref(ref, top=0, no_loc=True)
    assert pa.ok


def test_audit_empty_claude_project_is_soft_pass(tmp_path, monkeypatch):
    """Listed project hash with zero jsonl must not FAIL the batch run."""
    proj, h = _seed_claude(tmp_path, monkeypatch)
    # project dir exists but no session files
    ref = ProjectRef(source="claude", key=h, display_name=h, cwd=None, path=proj)
    pa = audit.audit_ref(ref, top=0, no_loc=True)
    assert pa.ok
    assert pa.n_sessions == 0
    assert pa.error is None
