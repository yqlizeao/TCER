"""Weekly / date-range developer report aggregator and generator.

Pure Python standard library (>=3.11). Extracts multi-agent session facts
(prompts, files, net LOC, costs, rework, models) across all or single projects,
producing:
1. High-density structured aggregate metrics (WeeklyAggregate).
2. Deterministic, fully-offline Markdown weekly report.
3. System + User prompt payload for optional LLM semantic polishing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tcer.core import (
    analyze,
    antigravity_reader,
    codex_reader,
    grok_reader,
    omp_reader,
    opencode_reader,
    pi_reader,
    reader,
)
from tcer.core.models import ProjectRef, SessionReport
from tcer.core.paths import list_project_refs


def resolve_week_range(preset: str = "this_week",
                       today: datetime | None = None,
                       *,
                       workday_only: bool = False) -> tuple[str, str, str]:
    """Resolve start date, end date (YYYY-MM-DD), and human-friendly title.

    Args:
        preset: "this_week", "last_week", "last_30_days", etc.
        today: reference datetime (defaults to now)
        workday_only: if True, end date aligns to Friday (Mon-Fri) instead of Sunday (Mon-Sun).

    Returns:
        (since, until, title_label)
    """
    now = today or datetime.now()
    this_monday = now - timedelta(days=now.weekday())
    offset_days = 4 if workday_only else 6

    if preset == "this_week":
        s = this_monday.strftime("%Y-%m-%d")
        u = (this_monday + timedelta(days=offset_days)).strftime("%Y-%m-%d")
        label = "本周工作日" if workday_only else "本周"
        return s, u, f"{label} ({s} ~ {u})"
    elif preset == "last_week":
        last_monday = this_monday - timedelta(days=7)
        s = last_monday.strftime("%Y-%m-%d")
        u = (last_monday + timedelta(days=offset_days)).strftime("%Y-%m-%d")
        label = "上周工作日" if workday_only else "上周"
        return s, u, f"{label} ({s} ~ {u})"
    elif preset == "workdays":
        last_monday = this_monday - timedelta(days=7)
        s = last_monday.strftime("%Y-%m-%d")
        u = (last_monday + timedelta(days=4)).strftime("%Y-%m-%d")
        return s, u, f"工作日 ({s} ~ {u})"
    elif preset == "last_30_days":
        s = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        u = now.strftime("%Y-%m-%d")
        return s, u, f"近30天 ({s} ~ {u})"
    else:
        s = this_monday.strftime("%Y-%m-%d")
        u = (this_monday + timedelta(days=offset_days)).strftime("%Y-%m-%d")
        return s, u, f"周期 ({s} ~ {u})"


_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def format_date_span(since_str: str, until_str: str) -> str:
    """Format since and until dates into explicit weekday range and day count.

    Examples:
        "2026-09-21" ~ "2026-09-27" -> "周一至周日 · 7天"
        "2026-09-21" ~ "2026-09-25" -> "周一至周五 · 5天"
    """
    try:
        d1 = datetime.strptime(since_str.strip(), "%Y-%m-%d")
        d2 = datetime.strptime(until_str.strip(), "%Y-%m-%d")
        days = (d2 - d1).days + 1
        if days <= 0:
            return ""
        w1 = _WEEKDAY_NAMES[d1.weekday()]
        w2 = _WEEKDAY_NAMES[d2.weekday()]
        if days == 7 and d1.weekday() == 0 and d2.weekday() == 6:
            return "周一至周日 · 7天"
        elif days == 5 and d1.weekday() == 0 and d2.weekday() == 4:
            return "周一至周五 · 5天"
        elif days == 1:
            return f"{w1} · 1天"
        else:
            return f"{w1}至{w2} · {days}天"
    except Exception:
        return ""

def read_session_user_prompts(rep: SessionReport, source: str) -> list[str]:
    """Safely extract real user prompt strings from a session report."""
    try:
        p = rep.meta.path
        if source == "claude":
            msgs = reader.read_user_messages(p)
        elif source == "codex":
            msgs = codex_reader.read_user_messages(p)
        elif source == "opencode":
            # opencode takes (db_path, session_id)
            msgs = opencode_reader.read_user_messages(p, rep.meta.session_id or "")
        elif source == "grok":
            msgs = grok_reader.read_user_messages(p)
        elif source == "omp":
            msgs = omp_reader.read_user_messages(p)
        elif source == "pi":
            msgs = pi_reader.read_user_messages(p)
        elif source == "antigravity":
            msgs = antigravity_reader.read_user_messages(p)
        else:
            msgs = []
        return [m.strip() for m in msgs if m and m.strip()]
    except Exception:
        return []


@dataclass
class SessionItem:
    """Cleaned fact entry for one session in the weekly report."""

    session_id: str
    source: str
    project: str
    datetime_str: str
    date_str: str
    net_loc: int
    added: int
    deleted: int
    rework: int
    cost: float
    tokens: int
    models: list[str]
    duration_min: float
    score: float | None
    tier: str | None
    task_type: str | None
    prompts: list[str] = field(default_factory=list)
    top_files: list[str] = field(default_factory=list)
    tool_errors: int = 0
    corrections: int = 0


@dataclass
class ProjectSummary:
    """Summary of sessions belonging to one project within the timeframe."""

    name: str
    source: str
    sessions: list[SessionItem] = field(default_factory=list)
    total_net_loc: int = 0
    total_added: int = 0
    total_deleted: int = 0
    total_rework: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    total_duration_hours: float = 0.0
    models: set[str] = field(default_factory=set)
    top_files: list[str] = field(default_factory=list)
    key_tasks: list[str] = field(default_factory=list)


@dataclass
class WeeklyAggregate:
    """Structured aggregate data ready for formatting or LLM processing."""

    since: str
    until: str
    title_label: str
    total_sessions: int = 0
    active_days: int = 0
    total_net_loc: int = 0
    total_added: int = 0
    total_deleted: int = 0
    total_rework: int = 0
    churn_ratio: float = 0.0
    total_cost: float = 0.0
    total_tokens: int = 0
    total_duration_hours: float = 0.0
    total_tool_errors: int = 0
    total_corrections: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    model_counts: dict[str, int] = field(default_factory=dict)
    project_summaries: list[ProjectSummary] = field(default_factory=list)
    friction_points: list[str] = field(default_factory=list)


def collect_weekly_data(
    since: str,
    until: str,
    *,
    project_refs: list[ProjectRef] | None = None,
    scope_source: str = "all",
    today: datetime | None = None,
    progress_callback: Any = None,
) -> WeeklyAggregate:
    """Collect and aggregate all session data across projects for the date range."""
    # 1. Resolve date boundaries in epoch ms
    try:
        dt_start = datetime.strptime(since, "%Y-%m-%d")
        start_ms = int(datetime(dt_start.year, dt_start.month, dt_start.day, 0, 0, 0).timestamp() * 1000)
    except ValueError:
        start_ms = 0

    try:
        dt_end = datetime.strptime(until, "%Y-%m-%d")
        end_ms = int(datetime(dt_end.year, dt_end.month, dt_end.day, 23, 59, 59, 999000).timestamp() * 1000)
    except ValueError:
        end_ms = 9999999999999

    # 2. Select projects to scan
    if project_refs is not None:
        projs = list(project_refs)
    else:
        projs = list_project_refs(scope_source)

    all_sessions: list[SessionItem] = []
    total_proj = len(projs)

    for i, p in enumerate(projs):
        if progress_callback:
            progress_callback(i + 1, total_proj, getattr(p, "display_name", None) or getattr(p, "name", str(p)))
        src = getattr(p, "source", "claude")
        pname = getattr(p, "display_name", None) or getattr(p, "name", str(p))
        pkey = getattr(p, "key", str(p))

        try:
            a = analyze.analyze_project(
                project=pkey,
                source=src,
                project_ref=p,
            )
        except Exception:
            continue

        for r in a.reports:
            t = r.usage.started_at if r.usage else None
            if not t or not (start_ms <= t <= end_ms):
                continue

            dt = datetime.fromtimestamp(t / 1000)
            prompts = read_session_user_prompts(r, src)
            files = list((r.files_touched_details or {}).keys())[:6]

            item = SessionItem(
                session_id=r.meta.session_id or r.meta.path.stem,
                source=src,
                project=pname,
                datetime_str=dt.strftime("%Y-%m-%d %H:%M"),
                date_str=dt.strftime("%Y-%m-%d"),
                net_loc=r.net_loc or 0,
                added=r.code_added or 0,
                deleted=r.code_deleted or 0,
                rework=r.code_reworked or 0,
                cost=r.cost or 0.0,
                tokens=r.usage.total if r.usage else 0,
                models=list(r.usage.per_model.keys()) if r.usage else [],
                duration_min=r.session_duration_minutes or 0.0,
                score=r.score,
                tier=r.tier,
                task_type=r.task_type,
                prompts=prompts,
                top_files=files,
                tool_errors=r.usage.tool_errors if r.usage else 0,
                corrections=r.usage.correction_msg_count if r.usage else 0,
            )
            all_sessions.append(item)

    all_sessions.sort(key=lambda s: s.datetime_str)

    # 3. Group by Project
    proj_map: dict[str, ProjectSummary] = {}
    distinct_dates = set()
    source_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    total_added = 0
    total_deleted = 0
    total_rework = 0
    total_cost = 0.0
    total_tokens = 0
    total_duration_m = 0.0
    total_tool_errors = 0
    total_corrections = 0
    friction_points: list[str] = []

    for s in all_sessions:
        distinct_dates.add(s.date_str)
        source_counts[s.source] = source_counts.get(s.source, 0) + 1
        for m in s.models:
            model_counts[m] = model_counts.get(m, 0) + 1

        total_added += s.added
        total_deleted += s.deleted
        total_rework += s.rework
        total_cost += s.cost
        total_tokens += s.tokens
        total_duration_m += s.duration_min
        total_tool_errors += s.tool_errors
        total_corrections += s.corrections

        # Check friction
        if s.added > 50 and (s.rework / max(1, s.added)) > 0.35:
            friction_points.append(
                f"会话 {s.session_id[:8]} ({s.project}): 自返工率达 {s.rework/s.added*100:.1f}% (+{s.added}/-{s.rework} 重写)"
            )
        if s.tool_errors >= 5:
            friction_points.append(
                f"会话 {s.session_id[:8]} ({s.project}): 发生 {s.tool_errors} 次工具报错"
            )

        if s.project not in proj_map:
            proj_map[s.project] = ProjectSummary(
                name=s.project,
                source=s.source,
            )
        ps = proj_map[s.project]
        ps.sessions.append(s)
        ps.total_net_loc += s.net_loc
        ps.total_added += s.added
        ps.total_deleted += s.deleted
        ps.total_rework += s.rework
        ps.total_cost += s.cost
        ps.total_tokens += s.tokens
        ps.total_duration_hours += (s.duration_min / 60.0)
        ps.models.update(s.models)

        # Merge top files
        for f in s.top_files:
            if f not in ps.top_files and len(ps.top_files) < 8:
                ps.top_files.append(f)

        # Merge key prompts
        if s.prompts:
            first_p = s.prompts[0].replace("\n", " ").strip()
            if len(first_p) > 120:
                first_p = first_p[:117] + "…"
            if first_p and first_p not in ps.key_tasks and len(ps.key_tasks) < 6:
                ps.key_tasks.append(first_p)

    project_summaries = sorted(proj_map.values(), key=lambda p: p.total_cost, reverse=True)
    churn = (total_rework / max(1, total_added)) if total_added > 0 else 0.0

    return WeeklyAggregate(
        since=since,
        until=until,
        title_label=f"周报 ({since} ~ {until})",
        total_sessions=len(all_sessions),
        active_days=len(distinct_dates),
        total_net_loc=sum(s.net_loc for s in all_sessions),
        total_added=total_added,
        total_deleted=total_deleted,
        total_rework=total_rework,
        churn_ratio=churn,
        total_cost=total_cost,
        total_tokens=total_tokens,
        total_duration_hours=total_duration_m / 60.0,
        total_tool_errors=total_tool_errors,
        total_corrections=total_corrections,
        source_counts=source_counts,
        model_counts=model_counts,
        project_summaries=project_summaries,
        friction_points=friction_points[:8],
    )


def generate_offline_markdown(agg: WeeklyAggregate) -> str:
    """Format the aggregated data into a complete, clean, professional Markdown weekly report."""
    lines: list[str] = []
    lines.append(f"# AI 研发周报 ({agg.since} ~ {agg.until})")
    lines.append("")
    lines.append("## 1. 核心大盘概览 (Executive Summary)")
    lines.append(f"- **协同强度**：活跃 {agg.active_days} 天 | 会话 {agg.total_sessions} 次 | 有效协同计算工时 {agg.total_duration_hours:.1f} 小时")
    lines.append(f"- **代码交付**：净增代码 **{agg.total_net_loc:+d} 行** (新增 {agg.total_added} 行，删除 {agg.total_deleted} 行)")
    lines.append(f"- **代码质量**：自返工率 **{agg.churn_ratio * 100:.1f}%** (返工修改 {agg.total_rework} 行) | 工具调用错误 {agg.total_tool_errors} 次")
    lines.append(f"- **算力投入**：**${agg.total_cost:.2f}** | 累计 Token {agg.total_tokens:,}")
    if agg.source_counts:
        src_str = " · ".join(f"{s.upper()}: {c}次" for s, c in sorted(agg.source_counts.items(), key=lambda x: x[1], reverse=True))
        lines.append(f"- **协同环境**：{src_str}")
    if agg.model_counts:
        top_models = sorted(agg.model_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        m_str = " · ".join(f"{m or 'default'}: {c}次" for m, c in top_models)
        lines.append(f"- **主力模型**：{m_str}")
    lines.append("")

    lines.append("## 2. 重点项目与业务推进详情 (Key Deliverables)")
    if not agg.project_summaries:
        lines.append("*所选时间区间内无有效项目交付记录。*")
    else:
        for ps in agg.project_summaries:
            lines.append(f"### 📦 {ps.name}")
            lines.append(f"*协同 Agent: {ps.source.upper()} | 会话: {len(ps.sessions)} 次 | 净增: {ps.total_net_loc:+d} 行 | 投入: ${ps.total_cost:.2f}*")
            if ps.key_tasks:
                lines.append("- **核心任务与推进事实**：")
                for task in ps.key_tasks:
                    lines.append(f"  - {task}")
            if ps.top_files:
                files_fmt = ", ".join(f"`{Path(f).name}`" for f in ps.top_files[:5])
                lines.append(f"- **涉及核心模块/文件**：{files_fmt}")
            lines.append("")

    lines.append("## 3. 人机协作复盘与阻力点 (Collaboration & Friction)")
    if agg.friction_points:
        lines.append("- **卡点与异常诊断**：")
        for f in agg.friction_points:
            lines.append(f"  - ⚠️ {f}")
    else:
        lines.append("- **协同健康度**：全周无明显高频返工或工具严重卡死异常，代码一次性落地稳定。")
    if agg.churn_ratio < 0.10:
        lines.append("- **工程质量亮点**：全周自返工率低于 10%，需求理解与上下文边界控制良好。")
    lines.append("")

    lines.append("## 4. 下周持续推进项 (Next Steps)")
    lines.append("- 基于本周已完成的核心交付项，持续推进未完结模块的测试与线上回归；")
    lines.append("- 针对长耗时或高成本项目，优化 Prompt 任务拆分粒度，避免单一会话上下文过度膨胀。")
    lines.append("")
    return "\n".join(lines)


def build_weekly_prompt_payload(agg: WeeklyAggregate) -> tuple[str, str]:
    """Build system prompt and user context payload for LLM report polishing."""
    system_prompt = (
        "你是一个资深技术总监与研发效能专家。请根据提供的研发团队/个人在过去一周内与多个 AI 编程助手"
        "（如 Claude Code, Codex, OMP, Antigravity 等）的真实协作会话数据与事实，"
        "生成一份高水准、逻辑严密、面向技术管理层汇报的《AI 研发周报》。\n\n"
        "【编写要求】：\n"
        "1. 严格基于提供的事实，严禁虚构未发生的工作成果或编造文件名。\n"
        "2. 聚类归纳：将零散的会话与 Prompt 按业务/架构维度聚合成清晰的重点成果（如架构重构、功能落地、缺陷攻坚、工具链建设）。\n"
        "3. 数据融合：将代码净增、成本、模型配比、自返工率自然融入段落叙述中。\n"
        "4. 深度复盘：客观评价人机协作中的亮点（如一次写对率高、大模型分工互补）与阻力踩坑（如返工高、上下文过载、Token 消耗过大），并给出切实可行的下周建议。\n"
        "5. 输出标准格式的 Markdown，排版清晰美观，避免假大空套话。"
    )

    data_payload: dict[str, Any] = {
        "date_range": f"{agg.since} ~ {agg.until}",
        "aggregate": {
            "sessions": agg.total_sessions,
            "active_days": agg.active_days,
            "net_loc": agg.total_net_loc,
            "added_loc": agg.total_added,
            "deleted_loc": agg.total_deleted,
            "rework_loc": agg.total_rework,
            "churn_ratio_pct": round(agg.churn_ratio * 100, 1),
            "cost_usd": round(agg.total_cost, 2),
            "total_tokens": agg.total_tokens,
            "effective_hours": round(agg.total_duration_hours, 1),
            "tool_errors": agg.total_tool_errors,
            "source_distribution": agg.source_counts,
            "model_distribution": agg.model_counts,
        },
        "projects": [
            {
                "project_name": ps.name,
                "source": ps.source,
                "sessions_count": len(ps.sessions),
                "net_loc": ps.total_net_loc,
                "cost_usd": round(ps.total_cost, 2),
                "duration_hours": round(ps.total_duration_hours, 1),
                "key_prompts_and_tasks": ps.key_tasks,
                "main_files_touched": [Path(f).name for f in ps.top_files],
            }
            for ps in agg.project_summaries
        ],
        "friction_points": agg.friction_points,
    }

    user_payload = (
        f"请基于以下本周客观数据，生成一份结构清晰、语言精练的研发周报：\n\n"
        f"```json\n{json.dumps(data_payload, ensure_ascii=False, indent=2)}\n```"
    )
    return system_prompt, user_payload
