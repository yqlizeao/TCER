"""Tests for export.py (JSON/CSV/Markdown + 综合效率分 ranking) and format.py."""
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
                           task_type="feature")


# --------------------------------------------------------------------------- #
# format.models_label
# --------------------------------------------------------------------------- #
def test_models_label_friendly_and_sorted():
    u = TokenUsage(models={"claude-opus-4-8[1m]", "gpt-5"})
    assert fmt.models_label(u) == "Claude Opus 4.8, GPT-5"


def test_models_label_empty():
    assert fmt.models_label(TokenUsage()) == "-"


def test_fmt_dt_none_and_non_positive():
    assert fmt.fmt_dt(None) == "-"
    assert fmt.fmt_dt(0) == "-"
    assert fmt.fmt_dt(-1) == "-"
    # A plausible 2026-ish epoch ms should format without error
    s = fmt.fmt_dt(1_735_689_600_000)  # ~2025-01-01 UTC area
    assert s != "-" and len(s) >= 10


# --------------------------------------------------------------------------- #
# export.score_ranking / text_score_chart
# --------------------------------------------------------------------------- #
def test_score_ranking_sorted_desc():
    lo, hi = _report(50, sid="low"), _report(5000, sid="high")
    ranking = export.score_ranking([lo, hi])
    labels = [label for label, _, _ in ranking]
    assert labels[0] == "high"          # higher net_loc → higher score
    # tiers carried through as strings
    assert all(isinstance(tier, str) for _, _, tier in ranking)
    # scores bounded 0–100
    assert all(0.0 <= s <= 100.0 for _, s, _ in ranking)


def test_score_ranking_empty_when_no_score():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert export.score_ranking([r]) == []


def test_text_score_chart_renders_bars():
    out = export.text_score_chart([_report(50, sid="low"), _report(5000, sid="high")])
    assert "█" in out
    assert "\033[" not in out  # no ANSI in the text chart


def test_text_score_chart_empty_message():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert "no per-session score" in export.text_score_chart([r])


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
    md = export.to_markdown([r], r, 1)
    assert "# TCER Report" in md
    assert "## Summary" in md
    assert "## Sessions" in md
    assert "## 综合效率分 Distribution" in md
    assert "abc12345"[:12] in md


def test_csv_fields_cover_all_row_keys():
    """漂移护栏:report_row_dict 的每个键必须进 _CSV_FIELDS 或 _CSV_EXCLUDED。"""
    r = _report(500)
    row = export.report_row_dict(r)
    unaccounted = [k for k in row
                   if k not in export._CSV_FIELDS and k not in export._CSV_EXCLUDED]
    assert not unaccounted, (
        f"新导出字段未归类(进 _CSV_FIELDS 或 _CSV_EXCLUDED): {unaccounted}")
    # 反向:清单里不能有已消失的键
    stale = [k for k in export._CSV_FIELDS if k not in row]
    assert not stale, f"_CSV_FIELDS 含已不存在的键: {stale}"


def test_ui_prefs_roundtrip(tmp_path, monkeypatch):
    from tcer.core import ui_prefs
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: tmp_path / "tcer_ui.json")
    assert ui_prefs.load() == {}
    ui_prefs.save({"geometry": "1600x900+10+10", "sashes": [190, 420]})
    assert ui_prefs.load()["sashes"] == [190, 420]
    # 损坏文件容错
    (tmp_path / "tcer_ui.json").write_text("{broken", encoding="utf-8")
    assert ui_prefs.load() == {}
    # 几何串校验
    assert ui_prefs.valid_geometry("1600x900+160+40")
    assert ui_prefs.valid_geometry("1600x900+-160-40")
    assert not ui_prefs.valid_geometry("garbage")
    assert not ui_prefs.valid_geometry(None)


def test_score_decompose_missing_cost_axis_neutral():
    """成本轴缺失（如 net_loc=0）以 0.5 中性填充，不当最差档拉偏均值。"""
    from types import SimpleNamespace

    from tcer.core.export import score_decompose, score_decompose_avg

    # 正常会话：成本轴 0.75
    r1 = SimpleNamespace(score=50.0, score_output_axis=0.5,
                         score_cost_axis=0.75, score_quality_axis=0.5)
    # net_loc=0 会话：成本轴 None
    r2 = SimpleNamespace(score=50.0, score_output_axis=0.5,
                         score_cost_axis=None, score_quality_axis=0.5)
    assert score_decompose(r2)["cost"] == 0.5
    avg = score_decompose_avg([r1, r2])
    assert avg["cost"] == (0.75 + 0.5) / 2   # 不再被腰斩到 0.375
