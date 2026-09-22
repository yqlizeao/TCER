"""Shared fixtures for TCER tests.

Provides common factory functions and fixtures to avoid duplication across
test files. All test files can import from here instead of defining their
own helper functions.

Usage:
    from conftest import _usage, _assistant

    def test_something(make_session):
        p = make_session([_assistant(_usage(10, 0, 0, 5))])
        result = reader.aggregate_usage(p)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _usage(i=0, cw=0, cr=0, o=0) -> dict:
    """Create a token usage dict for testing.

    Args:
        i: input_tokens
        cw: cache_creation_input_tokens
        cr: cache_read_input_tokens
        o: output_tokens

    Returns:
        Dict with four token fields
    """
    return {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }


def _assistant(usage, model="claude-opus-4-8", ts="2026-03-06T10:00:00Z", msg_id=None) -> dict:
    """Create an assistant message dict for testing.

    Args:
        usage: Token usage dict (from _usage())
        model: Model identifier string
        ts: ISO 8601 timestamp string
        msg_id: Message ID for deduplication
            - None: omit the "id" field entirely (no dedup)
            - "": empty string (edge case, should be treated as no id)
            - "msg_123": real message id (should dedup)

    Returns:
        Dict representing one JSONL line
    """
    msg = {
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "x"}],
        "usage": usage,
    }
    if msg_id is not None:
        msg["id"] = msg_id
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": msg,
    }


@pytest.fixture
def make_session(tmp_path):
    """Factory fixture to create test JSONL session files.

    Creates a temporary JSONL file with the given lines, one JSON object per line.
    File names are auto-incremented to avoid collisions.

    Args:
        tmp_path: pytest's temporary directory fixture

    Returns:
        Callable that takes a list of dicts and returns a Path to the created file

    Example:
        def test_reader(make_session):
            p = make_session([
                _assistant(_usage(10, 0, 0, 5)),
                _assistant(_usage(20, 0, 0, 10)),
            ])
            result = reader.aggregate_usage(p)
            assert result.input_tokens == 30
    """
    def _make(lines):
        # Auto-increment file name to avoid collisions
        existing = list(tmp_path.glob("session-*.jsonl"))
        p = tmp_path / f"session-{len(existing)}.jsonl"

        with p.open("w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return p
    return _make


@pytest.fixture
def sample_jsonl():
    """Path to the sample.jsonl fixture.

    Returns:
        Path to tests/fixtures/sample.jsonl

    Example:
        def test_fixture(sample_jsonl):
            result = reader.aggregate_usage(sample_jsonl)
            assert result.assistant_msgs == 3
    """
    return Path(__file__).parent / "fixtures" / "sample.jsonl"


@pytest.fixture
def write_session():
    """Helper to write a session JSONL file (for backward compatibility).

    This is an alternative to make_session that takes a Path directly.
    Useful when you need to specify the exact file name.

    Example:
        def test_something(tmp_path, write_session):
            p = tmp_path / "my-session.jsonl"
            write_session(p, [_assistant(_usage(10, 0, 0, 5))])
            result = reader.aggregate_usage(p)
    """
    def _write(path, lines):
        with path.open("w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return path
    return _write


@pytest.fixture(autouse=True)
def _isolate_llm_reports(tmp_path, monkeypatch):
    """全局兜底:LLM 报告库在本测试期间一律指向 tmp。

    根因(2026-09-22 抓的现行):GUI 测试 dispatch 的 daemon worker 线程
    生命周期可跨越测试 monkeypatch teardown——晚到的 ``llm_reports.append``
    在 patch 已恢复后执行,写入用户真实 ``~/.tcer/tcer_llm_reports.json``
    (实测每轮 pytest 后真实文件多出一份「标题-s1」假报告,进程内插桩因
    解释器退出阶段 stdout 已关而抓不到)。本 fixture 把默认 ``_path`` 钉到
    tmp:各测试自己的更窄 patch 照常生效(后设置者覆盖),忘 patch 或晚到
    的写入全部落 tmp,真实文件零污染。
    """
    from tcer.core import llm_reports
    monkeypatch.setattr(llm_reports, "_path", lambda: tmp_path / "tcer_llm_reports_isolated.json")


@pytest.fixture(scope="session")
def root():
    """全测试会话共享的单个 Tk root。

    Windows 上同进程「先建后销 root 再建新 root」会抛
    couldn't read init.tcl —— 此前 smoke 与 dynamics 两文件各自建销 root，
    pytest capture 模式下后建者整体 skip（覆盖被静默吞掉，-s 才恢复）。
    单 root 全程复用后消除顺序耦合；无显示环境（CI headless）照旧整体 skip。
    需要真实窗口尺寸的测试（heatmap）用 root_session 别名包装本 fixture
    并自行 deiconify/withdraw。
    """
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("无显示环境,跳过 GUI 冒烟")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(scope="session")
def root_session(root):
    """别名 fixture：供需要包装（deiconify/几何设定）而非遮蔽共享 root 的文件。"""
    return root
