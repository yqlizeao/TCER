"""Tests for tcer/core/weekly.py and weekly report integration.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tcer.core import metrics, weekly
from tcer.core.models import ProjectRef, SessionMeta, SessionReport, TokenUsage
from tcer.gui.views import LlmReportsView


def test_resolve_week_range():
    # 2026-09-28 is a Monday
    monday = datetime(2026, 9, 28, 14, 0, 0)
    s, u, title = weekly.resolve_week_range("this_week", today=monday)
    assert s == "2026-09-28"
    assert u == "2026-10-04"
    assert "本周" in title

    s_last, u_last, title_last = weekly.resolve_week_range("last_week", today=monday)
    assert s_last == "2026-09-21"
    assert u_last == "2026-09-27"
    assert "上周" in title_last
    s_work, u_work, title_work = weekly.resolve_week_range("last_week", today=monday, workday_only=True)
    assert s_work == "2026-09-21"
    assert u_work == "2026-09-25"
    assert "工作日" in title_work

    s_30, u_30, title_30 = weekly.resolve_week_range("last_30_days", today=monday)
    assert u_30 == "2026-09-28"
    assert "近30天" in title_30

    assert weekly.format_date_span("2026-09-21", "2026-09-27") == "周一至周日 · 7天"
    assert weekly.format_date_span("2026-09-21", "2026-09-25") == "周一至周五 · 5天"

def test_weekly_markdown_generation_and_prompt_payload():
    s1 = weekly.SessionItem(
        session_id="sess-001",
        source="claude",
        project="TCER",
        datetime_str="2026-09-22 10:00",
        date_str="2026-09-22",
        net_loc=350,
        added=400,
        deleted=50,
        rework=20,
        cost=5.20,
        tokens=120_000,
        models=["claude-opus-4-8"],
        duration_min=45.0,
        score=78.5,
        tier="良好",
        task_type="code_creation",
        prompts=["完成周报生成器核心逻辑与聚合管道"],
        top_files=["tcer/core/weekly.py", "tcer/gui/popups.py"],
        tool_errors=1,
        corrections=0,
    )
    s2 = weekly.SessionItem(
        session_id="sess-002",
        source="omp",
        project="TCER",
        datetime_str="2026-09-23 15:30",
        date_str="2026-09-23",
        net_loc=150,
        added=180,
        deleted=30,
        rework=10,
        cost=1.80,
        tokens=50_000,
        models=["glm-5.3"],
        duration_min=20.0,
        score=82.0,
        tier="良好",
        task_type="code_maintenance",
        prompts=["修复 Windows 下编码路径与时区偏差"],
        top_files=["tcer/core/paths.py"],
        tool_errors=0,
        corrections=0,
    )

    ps = weekly.ProjectSummary(
        name="TCER",
        source="claude",
        sessions=[s1, s2],
        total_net_loc=500,
        total_added=580,
        total_deleted=80,
        total_rework=30,
        total_cost=7.00,
        total_tokens=170_000,
        total_duration_hours=1.08,
        models={"claude-opus-4-8", "glm-5.3"},
        top_files=["tcer/core/weekly.py", "tcer/gui/popups.py", "tcer/core/paths.py"],
        key_tasks=["完成周报生成器核心逻辑与聚合管道", "修复 Windows 下编码路径与时区偏差"],
    )

    agg = weekly.WeeklyAggregate(
        since="2026-09-21",
        until="2026-09-27",
        title_label="周报 (2026-09-21 ~ 2026-09-27)",
        total_sessions=2,
        active_days=2,
        total_net_loc=500,
        total_added=580,
        total_deleted=80,
        total_rework=30,
        churn_ratio=30 / 580,
        total_cost=7.00,
        total_tokens=170_000,
        total_duration_hours=1.08,
        total_tool_errors=1,
        total_corrections=0,
        source_counts={"claude": 1, "omp": 1},
        model_counts={"claude-opus-4-8": 1, "glm-5.3": 1},
        project_summaries=[ps],
        friction_points=[],
    )

    # 1. 验证离线 Markdown
    md = weekly.generate_offline_markdown(agg)
    assert "# AI 研发周报 (2026-09-21 ~ 2026-09-27)" in md
    assert "核心大盘概览" in md
    assert "净增代码 **+500 行**" in md
    assert "$7.00" in md
    assert "完成周报生成器核心逻辑与聚合管道" in md
    assert "`weekly.py`" in md
    assert "自返工率低于 10%" in md

    # 2. 验证 Prompt Payload
    sys_p, user_p = weekly.build_weekly_prompt_payload(agg)
    assert "你是一个资深技术总监" in sys_p
    assert "```json" in user_p
    # 验证 JSON 有效性
    json_str = user_p.split("```json\n")[1].split("\n```")[0]
    data = json.loads(json_str)
    assert data["aggregate"]["sessions"] == 2
    assert data["aggregate"]["net_loc"] == 500
    assert data["projects"][0]["project_name"] == "TCER"


def test_collect_weekly_data_mocked():
    # 模拟一个会话报表
    meta = SessionMeta(session_id="test-sid-1", cwd="/repo", title="周报测试",
                       path=Path("/repo/s1.jsonl"), is_subagent=False, source="claude")
    u = TokenUsage(
        input_tokens=10_000,
        output_tokens=2_000,
        started_at=int(datetime(2026, 9, 22, 10, 0, 0).timestamp() * 1000),
        ended_at=int(datetime(2026, 9, 22, 10, 30, 0).timestamp() * 1000),
        session_duration_ms=1_800_000,
    )
    rep = metrics.compute(meta, u, net_loc=120)

    class MockAnalysis:
        reports = [rep]
    ref = ProjectRef(source="claude", key="test_key", display_name="TestProj", cwd="/repo")

    with patch("tcer.core.analyze.analyze_project", return_value=MockAnalysis()):
        with patch("tcer.core.weekly.read_session_user_prompts", return_value=["编写周报模块"]):
            agg = weekly.collect_weekly_data(
                since="2026-09-21",
                until="2026-09-25",
                project_refs=[ref],
            )
            assert agg.total_sessions == 1
            assert agg.total_net_loc == 120
            assert len(agg.project_summaries) == 1
            assert agg.project_summaries[0].name == "TestProj"
            assert agg.project_summaries[0].key_tasks == ["编写周报模块"]


def test_llm_reports_view_supports_weekly_kind():
    assert "weekly" in LlmReportsView.REPORT_KINDS
    w_kind = LlmReportsView.REPORT_KINDS["weekly"]
    assert w_kind["label"] == "周报"
    assert w_kind["icon"] == "calendar"
