"""Tests for TCER/core/weekly.py and weekly report integration.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tcer.core import metrics, weekly
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
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


def _item(sid, date, hm, prompts, *, project="TCER", source="claude",
          cwd="C:\\repo\\TCER", files=(), net=0, trivial=0, end="",
          outside=False, cost=1.0):
    return weekly.SessionItem(
        session_id=sid, source=source, project=project,
        datetime_str=f"{date} {hm}", date_str=date, net_loc=net, cost=cost,
        prompts=list(prompts), trivial_prompts=trivial,
        edited_files=list(files), active_dates=[date],
        end_str=end, outside_range=outside, cwd=cwd)


def test_trivial_prompt_and_one_line():
    for t in ("hello", "继续", "好的。", " OK ", "y", "继续推进！"):
        assert weekly.is_trivial_prompt(t), t
    for t in ("修复周报", "继续推进 Sprint 2", "1.暂缓删除；2.方案 A"):
        assert not weekly.is_trivial_prompt(t), t
    assert weekly.one_line("a\n\n  b\tc") == "a b c"
    assert weekly.one_line("x" * 300, 10) == "x" * 9 + "…"


def test_aggregate_groups_by_cwd_across_sources_and_folds_empty():
    s1 = _item("sess-001", "2026-09-22", "10:00", ["完成周报核心逻辑"],
               files=["C:\\repo\\TCER\\TCER\\core\\weekly.py"], net=350,
               end="2026-09-22 11:30")
    # 同一工作目录（大小写/斜杠不同）的另一数据源会话 → 合并成一个项目
    s2 = _item("sess-002", "2026-09-23", "15:30", ["修复 Windows 路径", "补测试"],
               source="antigravity", cwd="c:/Repo/TCER", trivial=2,
               end="2026-09-25 09:00", outside=True)
    s3 = _item("sess-003", "2026-09-23", "09:00", ["看看 CI 报错"],
               project="zeta", cwd="D:\\work\\zeta")
    empty = _item("sess-004", "2026-09-24", "09:00", [], project="tmp", cwd="D:\\tmp")
    assert not empty.is_substantive
    agg = weekly._aggregate("2026-09-21", "2026-09-27", [s1, s2, s3], [empty])

    assert [p.name for p in agg.project_summaries] == ["TCER", "zeta"]
    ps = agg.project_summaries[0]
    assert len(ps.sessions) == 2 and ps.total_prompts == 3
    assert set(ps.sources) == {"claude", "antigravity"}
    assert agg.total_prompts == 4 and agg.total_trivial_prompts == 2
    assert agg.total_sessions == 3 and agg.skipped_sessions == 1
    assert agg.skipped_examples == ["tmp"]
    assert agg.cross_range_sessions == 1
    assert agg.day_index["2026-09-23"] == ["TCER", "zeta"]
    assert agg.active_days == 2

    md = weekly.generate_offline_markdown(agg)
    assert md.startswith("# 工作回顾 2026-09-21 ~ 2026-09-27（周一至周日 · 7天）")
    assert "- **09-21 周一**：无记录" in md
    assert "- **09-22 周二**：TCER" in md
    # 每条提问都列出，不只首条
    for p in ("完成周报核心逻辑", "修复 Windows 路径", "补测试", "看看 CI 报错"):
        assert f"- {p}" in md
    assert "（另有 2 条简短回复" in md
    assert "`TCER/core/weekly.py`" in md          # 工作目录内显示相对路径
    assert "09-23 周三 15:30 → 09-25 周五 09:00" in md
    assert "跨周期" in md and "只列出区间内发出的提问" in md
    assert "1 个无实质内容的会话" in md
    # 不再有评价 / 套话 / 大盘
    for gone in ("核心大盘", "阻力点", "下周持续推进", "自返工率", "⚠"):
        assert gone not in md
    # 指标只剩一行脚注
    assert md.count("$") == 1


def test_duplicate_project_names_disambiguated():
    a = _item("a", "2026-09-22", "10:00", ["x"], project="app", cwd="C:\\w1\\app")
    b = _item("b", "2026-09-22", "11:00", ["y"], project="app", cwd="C:\\w2\\app")
    agg = weekly._aggregate("2026-09-21", "2026-09-27", [a, b], [])
    assert sorted(p.name for p in agg.project_summaries) == ["app（w1）", "app（w2）"]


def test_no_cwd_same_name_projects_separated_by_source():
    a = _item("a", "2026-09-22", "10:00", ["修复周报"],
              project="app", cwd=None, net=10)
    b = _item("b", "2026-09-22", "11:00", ["补测试", "更新文档"],
              project="app", source="codex", cwd=None, net=20)
    agg = weekly._aggregate("2026-09-21", "2026-09-27", [a, b], [])

    assert len(agg.project_summaries) == 2
    by_source = {p.source: p for p in agg.project_summaries}
    for source, item, name, net, prompts in (
        ("claude", a, "app（Claude Code）", 10, 1),
        ("codex", b, "app（Codex）", 20, 2),
    ):
        ps = by_source[source]
        assert ps.name == name and item.project == name
        assert ps.sources == [source] and ps.sessions == [item]
        assert ps.total_net_loc == net and ps.total_prompts == prompts
    assert agg.day_index == {"2026-09-22": ["app（Claude Code）", "app（Codex）"]}
    assert agg.total_sessions == 2 and agg.total_net_loc == 30
    assert agg.total_prompts == 3


def test_duplicate_project_suffixes_remain_unique():
    items = [
        _item("a", "2026-09-22", "10:00", ["修复周报"], project="app", cwd=None),
        _item("b", "2026-09-22", "11:00", ["补测试"], project="app",
              source="codex", cwd=None),
        _item("c", "2026-09-22", "12:00", ["更新文档"], project="app",
              cwd="/one/Codex/app"),
        _item("d", "2026-09-22", "13:00", ["修复路径"], project="app",
              cwd="/two/Codex/app"),
        _item("e", "2026-09-22", "14:00", ["检查结果"], project="app（Codex）",
              cwd=None),
    ]
    agg = weekly._aggregate("2026-09-21", "2026-09-27", items, [])
    names = [p.name for p in agg.project_summaries]
    assert len(set(names)) == len(items)
    assert set(agg.day_index["2026-09-22"]) == set(names)
    assert {s.project for s in items} == set(names)


def test_markdown_escapes_renderer_marks_in_prompts():
    s = _item("a", "2026-09-22", "10:00", ["把 `foo` 改成 **bar** 和 a*b"])
    md = weekly.generate_offline_markdown(weekly._aggregate("2026-09-21", "2026-09-27", [s], []))
    assert "- 把 'foo' 改成 bar 和 a×b" in md


def test_prompt_payload_and_compose_report():
    s1 = _item("sess-001", "2026-09-22", "10:00", ["完成周报核心逻辑"],
               files=["C:\\repo\\TCER\\a.py"], net=10)
    agg = weekly._aggregate("2026-09-21", "2026-09-27", [s1], [])
    sys_p, user_p = weekly.build_weekly_prompt_payload(agg)
    assert "回顾" in sys_p and "不要猜" in sys_p and "下周建议" in sys_p
    data = json.loads(user_p.split("```json\n")[1].split("\n```")[0])
    sess = data["projects"][0]["sessions"][0]
    assert sess["user_prompts"] == [{"time": None, "text": "完成周报核心逻辑"}]
    assert sess["edited_files"] == ["a.py"]
    assert "cost" not in user_p and "token" not in user_p.lower()

    text, model = weekly.compose_report(agg, use_llm=False)
    assert model == "离线规则聚合" and text.startswith("# 工作回顾")

    seen = {}

    def fake_chat(sp, up):
        seen["sys"] = sp
        return "# 本周做了什么\n- 周报"
    text, model = weekly.compose_report(agg, use_llm=True, chat=fake_chat, model_name="m1")
    assert model == "m1" and seen["sys"] == sys_p
    assert text.startswith("# 本周做了什么")
    assert "附：逐会话原始记录" in text and "- 完成周报核心逻辑" in text

    def boom(sp, up):
        raise RuntimeError("net down")
    text, model = weekly.compose_report(agg, use_llm=True, chat=boom, model_name="m1")
    assert model == "离线规则聚合" and "net down" in text and "# 工作回顾" in text


def _rep_at(start, end, *, turns=(), cwd="/repo"):
    meta = SessionMeta(session_id="test-sid-1", cwd=cwd, title="t",
                       path=Path("/repo/s1.jsonl"), is_subagent=False, source="claude")
    u = TokenUsage(input_tokens=10_000, output_tokens=2_000,
                   started_at=int(start.timestamp() * 1000),
                   ended_at=int(end.timestamp() * 1000),
                   session_duration_ms=int((end - start).total_seconds() * 1000))
    u.turn_stats = [TurnStat(turn=i, ts=int(t.timestamp() * 1000)) for i, t in enumerate(turns)]
    return metrics.compute(meta, u, net_loc=120)


def _ms(*a):
    return int(datetime(*a).timestamp() * 1000)


def test_collect_weekly_data_mocked():
    rep = _rep_at(datetime(2026, 9, 22, 10), datetime(2026, 9, 22, 10, 30))

    class MockAnalysis:
        reports = [rep]
    ref = ProjectRef(source="claude", key="test_key", display_name="TestProj", cwd="/repo")

    with patch("tcer.core.analyze.analyze_project", return_value=MockAnalysis()):
        with patch("tcer.core.weekly.read_session_user_prompts_timed",
                   return_value=[("编写周报模块", _ms(2026, 9, 22, 10, 1)),
                                 ("继续", _ms(2026, 9, 22, 10, 5))]):
            agg = weekly.collect_weekly_data(
                since="2026-09-21", until="2026-09-25", project_refs=[ref])
    assert agg.total_sessions == 1
    assert agg.total_net_loc == 120
    assert agg.total_prompts == 1 and agg.total_trivial_prompts == 1
    ps = agg.project_summaries[0]
    assert ps.name == "repo" and ps.cwd == "/repo"
    assert ps.sessions[0].prompts == ["编写周报模块"]
    assert "- 10:01 编写周报模块" in weekly.generate_offline_markdown(agg)


def test_collect_uses_turn_timestamps_for_range():
    """会话上周开始、本周仍有回合 → 纳入并标「跨周期」；只在区间前活动 → 不纳入。"""
    spanning = _rep_at(datetime(2026, 9, 18, 10), datetime(2026, 9, 23, 12),
                       turns=[datetime(2026, 9, 18, 10), datetime(2026, 9, 23, 12)])
    before = _rep_at(datetime(2026, 9, 10, 10), datetime(2026, 9, 10, 11),
                     turns=[datetime(2026, 9, 10, 10)])

    class MockAnalysis:
        reports = [spanning, before]
    ref = ProjectRef(source="claude", key="k", display_name="P", cwd="/repo")
    with patch("tcer.core.analyze.analyze_project", return_value=MockAnalysis()):
        with patch("tcer.core.weekly.read_session_user_prompts_timed",
                   return_value=[("做事", None)]):
            agg = weekly.collect_weekly_data("2026-09-21", "2026-09-27", project_refs=[ref])
    assert agg.total_sessions == 1
    s = agg.project_summaries[0].sessions[0]
    assert s.outside_range and s.active_dates == ["2026-09-23"]
    assert agg.day_index == {"2026-09-23": ["repo"]}
    # 时间缺失的提问照列，脚注说明
    assert agg.undated_prompts == 1
    assert "1 条提问数据源未记录时间" in weekly.generate_offline_markdown(agg)


def test_prompts_and_edits_split_by_their_own_time():
    """跨周会话：只列区间内发出的提问、区间内回合编辑的文件；多日会话提问带日期。"""
    turns = [datetime(2026, 9, 19, 10), datetime(2026, 9, 22, 9), datetime(2026, 9, 23, 15)]
    rep = _rep_at(turns[0], turns[-1], turns=turns)
    rep.usage.tool_ops = [
        ToolOp(0, "Edit", "/repo/old.py"),      # 上周回合
        ToolOp(1, "Write", "/repo/new.py"),     # 本周回合
        ToolOp(2, "Read", "/repo/read.py"),     # 读不算编辑
        ToolOp(2, "Edit", "/repo/new2.py"),
    ]

    class MockAnalysis:
        reports = [rep]
    ref = ProjectRef(source="claude", key="k", display_name="P", cwd="/repo")
    prompts = [("上周的需求", _ms(2026, 9, 19, 9, 59)),
               ("本周第一件事", _ms(2026, 9, 22, 8, 58)),
               ("好的", _ms(2026, 9, 22, 9, 30)),
               ("本周第二件事", _ms(2026, 9, 23, 14, 59))]
    with patch("tcer.core.analyze.analyze_project", return_value=MockAnalysis()):
        with patch("tcer.core.weekly.read_session_user_prompts_timed", return_value=prompts):
            agg = weekly.collect_weekly_data("2026-09-21", "2026-09-27", project_refs=[ref])
    s = agg.project_summaries[0].sessions[0]
    assert s.prompts == ["本周第一件事", "本周第二件事"]
    assert s.trivial_prompts == 1 and s.prompts_outside == 1
    assert s.edited_files == ["/repo/new.py", "/repo/new2.py"]
    assert s.active_dates == ["2026-09-22", "2026-09-23"] and s.outside_range
    md = weekly.generate_offline_markdown(agg)
    assert "- 09-22 08:58 本周第一件事" in md and "- 09-23 14:59 本周第二件事" in md
    assert "上周的需求" not in md
    assert "1 条提问在区间外，未列出" in md
    assert "净增 +120 行（整个会话）" in md
    assert "区间外 1 条提问未列出" in md


def test_session_with_only_out_of_range_turns_but_in_range_prompt():
    """周日 23:58 发问、AI 回合全在周一凌晨（下个区间）→ 提问仍归本区间。"""
    rep = _rep_at(datetime(2026, 9, 27, 23, 58), datetime(2026, 9, 28, 0, 30),
                  turns=[datetime(2026, 9, 28, 0, 1)])

    class MockAnalysis:
        reports = [rep]
    ref = ProjectRef(source="claude", key="k", display_name="P", cwd="/repo")
    with patch("tcer.core.analyze.analyze_project", return_value=MockAnalysis()):
        with patch("tcer.core.weekly.read_session_user_prompts_timed",
                   return_value=[("深夜的需求", _ms(2026, 9, 27, 23, 58))]):
            agg = weekly.collect_weekly_data("2026-09-21", "2026-09-27", project_refs=[ref])
    assert agg.total_prompts == 1
    assert agg.day_index == {"2026-09-27": ["repo"]}


def test_llm_reports_view_supports_weekly_kind():
    assert "weekly" in LlmReportsView.REPORT_KINDS
    w_kind = LlmReportsView.REPORT_KINDS["weekly"]
    assert w_kind["label"] == "周报"
    assert w_kind["icon"] == "calendar"
