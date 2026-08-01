from __future__ import annotations

import json
from pathlib import Path

from tcer.core import analyze, pi_reader
from tcer.core.models import ProjectRef

# Pi (upstream earendil-works/pi) uses ISO-8601 timestamps like omp.
_ISO = "2026-07-29T06:18:21.927Z"


# -- Pi JSONL builders -------------------------------------------------------
# Unlike omp, Pi has NO fixed-width ``type:"title"`` slot — the first line is
# the ``type:"session"`` header directly. Assistant messages carry no omp-fork
# additions (contextSnapshot / duration / ttft), but the usage block carries
# two Pi-only fields: ``cacheWrite1h`` and ``reasoning``.
def _write_pi(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in entries:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def _session(cwd=r"C:\repo\app", sid="019fa-1", title="t") -> dict:
    # Pi header shape: type/version/id/timestamp/cwd (+ parentSession). title is
    # absent upstream; reader falls back to the first user message.
    return {"type": "session", "version": 3, "id": sid, "timestamp": _ISO,
            "cwd": cwd, "parentSession": None}


def _model_change(model="claude-opus-4-8") -> dict:
    return {"type": "model_change", "id": "m1", "parentId": None,
            "timestamp": _ISO, "model": model}


def _user(text="hi", ts=_ISO, imgs=0) -> dict:
    content = [{"type": "text", "text": text}]
    for _ in range(imgs):
        content.append({"type": "image", "mimeType": "image/png", "data": "iVBORw0="})
    return {"type": "message", "id": "u1", "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": content,
                        "attribution": "user", "timestamp": ts}}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _thinking() -> dict:
    return {"type": "thinking", "thinking": "...", "thinkingSignature": "sig"}


def _thinking_level(level="high") -> dict:
    return {"type": "thinking_level_change", "id": "tl1", "parentId": None,
            "timestamp": _ISO, "thinkingLevel": level, "configured": None}


def _tool_call(name: str, cid: str, args: dict) -> dict:
    return {"type": "toolCall", "id": cid, "name": name, "arguments": args}


def _assistant(content, usage, *, model="claude-opus-4-8", provider="anthropic",
               ts=_ISO, stop="end_turn", err=None) -> dict:
    # No contextSnapshot / duration / ttft — those are omp-fork additions.
    msg = {"role": "assistant", "content": content, "api": "anthropic-messages",
           "provider": provider, "model": model, "usage": usage,
           "stopReason": stop, "timestamp": ts}
    if err is not None:
        msg["errorMessage"] = err
    return {"type": "message", "id": "a1", "parentId": "u1", "timestamp": ts,
            "message": msg}


def _tool_result(cid, tool_name, *, details=None, is_error=False,
                 content=None, ts=_ISO) -> dict:
    return {"type": "message", "id": "tr1", "parentId": "a1", "timestamp": ts,
            "message": {"role": "toolResult", "toolCallId": cid, "toolName": tool_name,
                        "content": content if content is not None else [{"type": "text", "text": "ok"}],
                        "details": details or {}, "isError": is_error, "timestamp": ts}}


def _usage(inp, out, cr=0, cw=0, cw1h=0, reasoning=0, cost=None) -> dict:
    u = {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
         "cacheWrite1h": cw1h, "reasoning": reasoning,
         "totalTokens": inp + cr + cw + out}
    if cost is not None:
        u["cost"] = {"total": cost}
    return u


# -- usage aggregation: Pi enhancements + omp-absent fields -----------------

def test_aggregate_usage_pi_fields(tmp_path):
    """Pi's cacheWrite1h + reasoning are captured; peak_input/ttft stay empty."""
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _model_change("claude-opus-4-8"),
        _user("实现 Pi 支持"),
        _assistant([_thinking(), _text("好的"), _tool_call("read", "toolu_1", {"path": "a.py"})],
                   _usage(100, 20, cr=40, cw=10, cw1h=4, reasoning=8, cost=0.01)),
    ])
    u = pi_reader.aggregate_usage(p)
    assert u.input_tokens == 100
    assert u.output_tokens == 20
    assert u.cache_read_input_tokens == 40
    assert u.cache_creation_input_tokens == 10
    assert u.cache_write_1h_tokens == 4            # Pi 1h-cache-write subset
    assert u.reasoning_output_tokens == 8          # Pi reasoning tokens
    assert u.peak_input_tokens == 0                # Pi has no contextSnapshot
    assert u.time_to_first_token_ms is None        # Pi has no ttft
    assert u.user_msgs == 1 and u.assistant_msgs == 1
    assert u.thinking_count == 1
    assert u.tool_calls == {"Read": 1}
    assert abs(u.reported_cost_usd - 0.01) < 1e-9
    assert "claude-opus-4-8" in u.models
    # The 1h subset flows into the per-model bucket (priced at premium downstream).
    assert sum(mu.cache_write_1h_tokens for mu in u.per_model.values()) == 4
    assert len(u.turn_stats) == 1


def test_first_line_session_header_no_title_slot(tmp_path):
    """Pi's line 1 is the ``session`` header (no omp title slot); meta reads it."""
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(cwd=r"C:\repo\app", sid="sid-xyz"),
        _user("first real message"),
        _thinking_level("high"),
        _assistant([_text("ok")], _usage(5, 2)),
    ])
    meta = pi_reader.read_session_meta(p)
    assert meta.session_id == "sid-xyz"
    assert meta.cwd == r"C:\repo\app"
    assert meta.source == "pi"
    assert meta.entrypoint == "pi"
    assert meta.cli_version == "session v3"
    # No title field upstream → falls back to the first user message.
    assert meta.title == "first real message"
    assert meta.reasoning_effort == "high"


def test_empty_usage_skipped(tmp_path):
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _user("x"),
        _assistant([_text("hi")], _usage(0, 0)),   # all-zero -> skipped
        _assistant([_text("ok")], _usage(50, 10, reasoning=3)),
    ])
    u = pi_reader.aggregate_usage(p)
    assert u.empty_usage_skipped == 1
    assert u.assistant_msgs == 1
    assert u.input_tokens == 50 and u.output_tokens == 10
    assert u.reasoning_output_tokens == 3


def test_tool_mapping_reuses_omp(tmp_path):
    """Pi tool names map through the same canonical table as omp."""
    calls = [
        ("read", "c1", {"path": "a.py"}),
        ("grep", "c2", {"path": "."}),
        ("bash", "c3", {"workdir": "."}),
        ("web_search", "c4", {"query": "x"}),
    ]
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _user("do things"),
        _assistant([_tool_call(n, cid, args) for n, cid, args in calls], _usage(10, 5)),
    ])
    u = pi_reader.aggregate_usage(p)
    assert u.tool_calls == {"Read": 1, "Grep": 1, "Bash": 1, "WebSearch": 1}
    assert u.web_search_count == 1


def test_image_inputs_reuses_omp(tmp_path):
    """Pi shares omp's _aggregate_single: inline image blocks count too."""
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _user("look", imgs=2),
        _assistant([_text("ok")], _usage(10, 5)),
    ])
    u = pi_reader.aggregate_usage(p)
    assert u.image_count == 2


def test_aborted_turns_reuses_omp(tmp_path):
    """Pi shares omp's _aggregate_single, so stopReason 'aborted' feeds
    aborted_task_count + abort_reasons the same way."""
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _user("do a thing"),
        _assistant([_text("working")], _usage(10, 5), stop="aborted",
                   err="Operation aborted"),
    ])
    u = pi_reader.aggregate_usage(p)
    assert u.aborted_task_count == 1
    assert u.abort_reasons == {"Operation aborted": 1}


def test_loc_reuses_omp_accumulator(tmp_path):
    p = _write_pi(tmp_path / "s.jsonl", [
        _session(),
        _user("create file"),
        _assistant([_tool_call("write", "c1", {"path": "new.py", "content": "a\nb\nc\n"})],
                   _usage(10, 5)),
        _tool_result("c1", "write", details={"resolvedPath": "new.py"}),
    ])
    sloc, has = pi_reader._loc_scan(p)
    assert has is True
    assert sloc.added == 3
    assert sloc.unseen_writes == 1


# -- project discovery + env -------------------------------------------------

_STEM = "2026-07-29T06-18-21.927Z_019fa-1"


def test_list_project_refs_source_pi(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    proj = tmp_path / "agent" / "sessions" / "C--repo-app"
    main = proj / f"{_STEM}.jsonl"
    _write_pi(main, [_session(cwd=r"C:\repo\app"), _user("hi"),
                     _assistant([_text("ok")], _usage(10, 5))])
    refs = pi_reader.list_project_refs()
    assert len(refs) == 1
    assert refs[0].source == "pi"
    assert len(refs[0].session_paths) == 1
    assert pi_reader.sessions_for_project(refs[0]) == list(refs[0].session_paths)


def test_pi_sessions_dir_env(tmp_path, monkeypatch):
    from tcer.core import paths
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    assert paths.pi_sessions_dir() == tmp_path / "agent" / "sessions"
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    # Default root is ~/.pi — Pi has no PI_CONFIG_DIR (unlike omp).
    assert paths.pi_sessions_dir() == Path.home() / ".pi" / "agent" / "sessions"


def test_analyze_project_pi(tmp_path, monkeypatch):
    from tcer.core import file_cache
    file_cache.clear()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    proj = tmp_path / "agent" / "sessions" / "C--repo-app"
    main = proj / f"{_STEM}.jsonl"
    _write_pi(main, [_session(cwd=r"C:\repo\app"), _user("hi"),
                     _assistant([_text("ok")], _usage(100, 20, cw1h=5, reasoning=6))])
    refs = pi_reader.list_project_refs()
    a = analyze.analyze_project(refs[0].key, source="pi", project_ref=refs[0])
    assert a.n_sessions == 1
    assert a.aggregate.usage.input_tokens == 100
    assert a.aggregate.usage.cache_write_1h_tokens == 5
    assert a.aggregate.usage.reasoning_output_tokens == 6
    file_cache.clear()
