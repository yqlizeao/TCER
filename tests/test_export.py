"""Tests for export.py (JSON/CSV/Markdown + CTEI ranking) and format.py."""
from __future__ import annotations

from pathlib import Path

from tcer.core import export, format as fmt, metrics
from tcer.core.models import SessionMeta, TokenUsage


def _report(net_loc: int, sub: bool = False, sid: str = "sess") -> metrics.SessionReport:
    """Build a SessionReport with populated composite fields via compute()."""
    meta = SessionMeta(session_id=sid, cwd="/tmp", title=None,
                       path=Path(f"/tmp/{sid}.jsonl"), is_subagent=sub)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000)  # total 1Mt
    return metrics.compute(meta, u, net_loc=net_loc,
                           loc_accumulated=10_000, task_type="feature")


# --------------------------------------------------------------------------- #
# format.models_label
# --------------------------------------------------------------------------- #
def test_models_label_friendly_and_sorted():
    u = TokenUsage(models={"claude-opus-4-8[1m]", "gpt-5"})
    assert fmt.models_label(u) == "Claude Opus 4.8, GPT-5"


def test_models_label_empty():
    assert fmt.models_label(TokenUsage()) == "-"


# --------------------------------------------------------------------------- #
# export.ctei_ranking / text_ctei_chart
# --------------------------------------------------------------------------- #
def test_ctei_ranking_sorted_desc():
    lo, hi = _report(50, sid="low"), _report(5000, sid="high")
    ranking = export.ctei_ranking([lo, hi])
    assert [label for label, _, _ in ranking] == ["high", "low"]
    # grades carried through
    assert all(isinstance(grade, str) for _, _, grade in ranking)


def test_ctei_ranking_empty_when_no_ctei():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert export.ctei_ranking([r]) == []


def test_text_ctei_chart_renders_bars():
    out = export.text_ctei_chart([_report(50, sid="low"), _report(5000, sid="high")])
    lines = out.splitlines()
    assert "high" in lines[2] and "low" in lines[3]  # sorted desc
    assert "█" in out
    assert "\033[" not in out  # no ANSI in the text chart


def test_text_ctei_chart_empty_message():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert "no per-session score" in export.text_ctei_chart([r])


# --------------------------------------------------------------------------- #
# export JSON / CSV / Markdown
# --------------------------------------------------------------------------- #
def test_to_json_structure():
    r = _report(500)
    out = export.to_json([r], r, 1)
    import json
    payload = json.loads(out)
    assert set(payload) == {"aggregate", "sessions"}
    assert payload["aggregate"]["sessions_counted"] == 1
    assert payload["sessions"][0]["session_id"] == "sess"
    assert "cost_by_model" in payload["sessions"][0]


def test_to_csv_has_each_field_once():
    r = _report(500)
    csv_text = export.to_csv([r])
    header_fields = csv_text.splitlines()[0].split(",")
    # Header is written in fieldnames order with no duplicates.
    assert header_fields == export._CSV_FIELDS
    assert len(header_fields) == len(set(header_fields))


def test_to_markdown_contains_key_sections():
    r = _report(500, sid="abc12345")
    md = export.to_markdown([r], r, 1, code_dir=Path("/tmp"))
    assert "# TCER Report" in md
    assert "## Summary" in md
    assert "## Sessions" in md
    assert "## CTEI Distribution" in md
    assert "abc12345"[:12] in md
