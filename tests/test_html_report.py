"""Tests for gui/html_report.py — headless (no Tkinter needed).

Values in the HTML must be byte-identical to metric_defs.display (SSOT).
"""
from __future__ import annotations

from pathlib import Path

from tcer.core import metrics
from tcer.core.models import SessionMeta, TokenUsage
from tcer.gui import html_report, metric_defs


def _report(net_loc: int, sid: str = "sess", model: str = "claude-opus-4-8",
            unseen: int = 0) -> metrics.SessionReport:
    meta = SessionMeta(session_id=sid, cwd="/tmp", title=f"标题-{sid}",
                       path=Path(f"/tmp/{sid}.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000,
                   models={model}, started_at=1_770_000_000_000,
                   ended_at=1_770_003_600_000)
    u.per_model = {model: TokenUsage(input_tokens=500_000, output_tokens=500_000)}
    r = metrics.compute(meta, u, net_loc=net_loc,
                        loc_accumulated=10_000, task_type="feature")
    r.unseen_writes = unseen
    return r


def _agg(reports) -> metrics.SessionReport:
    meta = SessionMeta(session_id="(aggregate)", cwd="/tmp", title=None,
                       path=Path("/tmp"), is_subagent=False)
    u = TokenUsage()
    for r in reports:
        u = u.merge(r.usage)
    agg = metrics.compute(meta, u, net_loc=sum(r.net_loc or 0 for r in reports),
                          loc_accumulated=10_000, task_type="feature")
    agg.ncpi = None
    agg.ctei = None
    agg.grade = None
    return agg


def test_project_html_structure_and_ssot_values():
    reports = [_report(500, sid="s1"), _report(2000, sid="s2")]
    agg = _agg(reports)
    out = html_report.render_project_html(
        reports, agg, project_name="TCER", source_label="Claude",
        n_sessions=2, n_subagents=1)
    assert out.startswith("<!DOCTYPE html>")
    # 关键区块
    for section in ("聚合指标", "综合效率分排名", "模型对比", "会话明细", "每会话完整指标"):
        assert section in out
    # 数值与 SSOT 一致（聚合总 Token 显示串必须原样出现）
    assert metric_defs.display(agg, "total_tokens") in out
    assert metric_defs.display(reports[0], "tcer") in out
    # 六组全部出现
    for g in metric_defs.GROUPS:
        assert g.name in out
    # 会话标题被转义收录
    assert "标题-s1" in out and "标题-s2" in out
    # 子代理计入头部
    assert "含 1 个子代理" in out


def test_project_html_escapes_html():
    r = _report(100, sid="s1")
    r.meta = SessionMeta(session_id="s1", cwd="/tmp", title="<script>alert(1)</script>",
                         path=Path("/tmp/s1.jsonl"), is_subagent=False)
    out = html_report.render_project_html([r], _agg([r]), project_name="<x>&y")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;x&gt;&amp;y" in out


def test_project_html_unseen_writes_warning():
    r = _report(100, sid="s1", unseen=3)
    agg = _agg([r])
    agg.unseen_writes = 3
    out = html_report.render_project_html([r], agg, project_name="P")
    assert "盲写文件" in out and "3 次" in out


def test_session_html_structure():
    r = _report(800, sid="sess-x")
    out = html_report.render_session_html(r, project_name="TCER", source_label="Claude")
    assert out.startswith("<!DOCTYPE html>")
    assert "TCER 会话报告" in out
    assert "全部指标" in out
    assert metric_defs.display(r, "ctei") in out
    # 每个指标名都应出现
    for g in metric_defs.GROUPS:
        for m in g.metrics:
            assert m.name in out


def test_ctei_section_empty_note():
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    out = html_report.render_project_html([r], _agg([r]), project_name="P")
    assert "无可评分会话" in out


def test_session_to_json_single_row():
    import json

    from tcer.core import export
    r = _report(500, sid="only")
    payload = json.loads(export.session_to_json(r))
    assert payload["session_id"] == "only"
    assert "cost_by_model" in payload


def test_project_html_contains_mini_timeline():
    from tcer.core.models import TurnStat

    r = _report(300, sid="s1")
    r.usage.turn_stats = [TurnStat(0, ts=1_770_000_000_000, input_tokens=100,
                                   output_tokens=50),
                          TurnStat(1, input_tokens=200, output_tokens=80, errors=1)]
    out = html_report.render_project_html([r], _agg([r]), project_name="P")
    assert "vertical-align:bottom" in out  # 缩略条 span
    assert "回合 2" in out


def test_render_overview_html():
    rows = [{"source": "Claude", "name": "P1", "sessions": 3, "tokens": 1000,
             "cost": 1.5, "net": 200, "tcer": 60.0, "chr": 0.9, "churn": 0.05},
            {"source": "Grok", "name": "<x>", "sessions": 1, "tokens": 500,
             "cost": None, "net": None, "tcer": None, "chr": None, "churn": None}]
    rows[1]["cost"] = 0.0
    out = html_report.render_overview_html(rows)
    assert "TCER 项目总览" in out and "P1" in out
    assert "&lt;x&gt;" in out  # 转义
    assert "sortable" in out
