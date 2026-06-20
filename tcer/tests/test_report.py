"""Tests for report.py rendering — focus on the CTEI bar chart."""
from __future__ import annotations

from pathlib import Path

from tcer import metrics, report
from tcer.models import SessionMeta, TokenUsage


def _report(ctei_target_loc: int, sub: bool = False, sid: str = "sess"):
    """Build a SessionReport with a populated composite layer via compute()."""
    meta = SessionMeta(session_id=sid, cwd="/tmp", title=None,
                       path=Path(f"/tmp/{sid}.jsonl"), is_subagent=sub)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000)  # total 1Mt
    return metrics.compute(meta, u, net_loc=ctei_target_loc,
                           loc_accumulated=10_000, task_type="feature")


def test_chart_empty_when_no_ctei():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert r.ctei is None
    out = report.ctei_chart([r])
    assert "no per-session score" in out


def test_chart_renders_bars_sorted_desc():
    lo, hi = _report(50, sid="low"), _report(5000, sid="high")
    out = report.ctei_chart([lo, hi], color=False)
    lines = out.splitlines()
    # header + separator + 2 data rows
    assert len(lines) == 4
    # higher CTEI session sorts first
    assert "high" in lines[2]
    assert "low" in lines[3]
    assert "█" in out


def test_chart_color_toggle():
    r = _report(5000)
    colored = report.ctei_chart([r], color=True)
    plain = report.ctei_chart([r], color=False)
    assert "\033[" in colored  # ANSI escape present
    assert "\033[" not in plain


def test_chart_subagent_marker():
    r = _report(5000, sub=True, sid="agentX")
    out = report.ctei_chart([r], color=False)
    assert "↳" in out


def test_models_label_friendly_and_sorted():
    u = TokenUsage(models={"claude-opus-4-8[1m]", "gpt-5"})
    # friendly display names, comma-joined, sorted by raw id
    assert report.models_label(u) == "Claude Opus 4.8, GPT-5"


def test_models_label_empty():
    assert report.models_label(TokenUsage()) == "-"


def test_session_table_shows_model_column():
    r = _report(500, sid="s1")
    r.usage.models.add("claude-opus-4-8")
    out = report.session_table([r])
    assert "model" in out.splitlines()[0]      # header present
    assert "Claude Opus 4.8" in out            # friendly name rendered


def test_gui_row_matches_columns():
    """Guard: the GUI per-session row tuple must stay aligned with TABLE_COLS."""
    from tcer import gui
    r = _report(500)
    r.usage.models.add("claude-opus-4-8")
    # Re-create the row tuple exactly as _render builds it (now just session ID)
    row = (r.meta.session_id or r.meta.path.stem,)
    assert len(row) == len(gui.TABLE_COLS)
    assert len(row) == 1  # Only session ID now
