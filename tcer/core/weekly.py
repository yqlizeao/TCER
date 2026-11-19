"""周报：回顾「我这段时间做了什么」。

纯标准库（>=3.11）。定位是**个人工作回顾**，不是效能汇报：以真人提问（经
``user_input`` SSOT）为主线，按真实工作目录聚合项目、按会话列出全部提问与编辑过
的文件；指标只在文末留一行，不做评价、不写建议、不编套话。

产出：
1. ``collect_weekly_data`` → :class:`WeeklyAggregate`（结构化事实）。
2. ``generate_offline_markdown`` → 完全离线的确定性 Markdown。
3. ``build_weekly_prompt_payload`` → 可选 LLM 归纳的 system/user prompt。
4. ``compose_report`` → GUI 两处入口共用的「LLM 归纳 + 逐会话原始记录」合成。

日期口径：每条提问按**自身发出时间**（``user_input.read_user_messages_timed``）
筛进区间；编辑过的文件按回合时间戳（``turn_stats[].ts`` ↔ ``ToolOp.turn``）筛；
会话只要有区间内的提问或回合就纳入。跨区间会话打「跨周期」标记，区间外提问
只计数不列出；净增行数是会话级口径、无法按时间切分，标注「整个会话」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath, PurePosixPath
from typing import Any, Callable

from tcer.core import analyze, user_input
from tcer.core.models import ProjectRef, SessionReport
from tcer.core.parse_util import is_windows_path, normcase_path
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
    return [t for t, _ in read_session_user_prompts_timed(rep, source)]


def read_session_user_prompts_timed(rep: SessionReport,
                                    source: str) -> list[tuple[str, int | None]]:
    """真人提问 + 各自发出时间（epoch ms / None），经 ``user_input`` SSOT。"""
    try:
        msgs = user_input.read_user_messages_timed(source, rep.meta.path,
                                                   rep.meta.session_id)
        return [(m.strip(), ts) for m, ts in msgs if m and m.strip()]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# 提问 / 文件 清洗
# --------------------------------------------------------------------------- #
# 不承载工作内容的简短回复：回顾时折叠成计数，不逐条列出（不丢弃——计数照报）。
_TRIVIAL_PROMPTS = frozenset({
    "hello", "hi", "hey", "你好", "在吗", "继续", "继续吧", "继续推进", "好", "好的",
    "好吧", "行", "可以", "嗯", "嗯嗯", "是", "是的", "对", "对的", "ok", "okay",
    "yes", "y", "no", "n", "go", "开始", "确认", "同意", "收到", "谢谢", "thanks",
    "continue", "proceed", "下一步", "做吧", "改吧", "执行", "提交", "commit",
})
_TRIVIAL_STRIP = " \t\r\n。，,.!！?？~～…"
_WS_RE = re.compile(r"\s+")

# 编辑类工具（各源 ToolOp.tool 名）：回顾里「编辑过的文件」只看这些，读/搜不算。
# 记录的是编辑尝试（含失败调用），文案用「编辑过」而非「改动了」。
_EDIT_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "write", "edit", "ast_edit", "apply_patch",
})

# 逐条提问展示长度（离线 Markdown）与送 LLM 的长度。
PROMPT_CLIP = 160
LLM_PROMPT_CLIP = 300
FILES_SHOWN = 8

_SOURCE_LABELS = {
    "claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode",
    "omp": "Oh My Pi", "pi": "Pi", "antigravity": "Antigravity",
    "workbuddy": "WorkBuddy", "codebuddy": "CodeBuddy",
}


def source_label(source: str | None) -> str:
    return _SOURCE_LABELS.get((source or "claude").lower(), source or "claude")


def is_trivial_prompt(text: str) -> bool:
    """简短确认 / 寒暄类回复（「继续」「好的」「hello」）。"""
    t = text.strip().strip(_TRIVIAL_STRIP).lower()
    return len(t) <= 1 or t in _TRIVIAL_PROMPTS


def one_line(text: str, n: int = PROMPT_CLIP) -> str:
    """多行提问压成一行并截断（粘贴的日志/代码不撑爆版面）。"""
    t = _WS_RE.sub(" ", text).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _md_safe(text: str) -> str:
    """去掉会被周报渲染器误当格式的标记（反引号 / 星号）。"""
    return text.replace("`", "'").replace("**", "").replace("*", "×")


def _pure(p: str):
    return PureWindowsPath(p) if is_windows_path(p) else PurePosixPath(p)


def _rel_display(path: str, cwd: str | None) -> str:
    """文件展示名：在工作目录内显示相对路径，否则显示文件名。"""
    if cwd:
        try:
            return _pure(path).relative_to(_pure(cwd)).as_posix()
        except ValueError:
            pass
    return _pure(path).name or path


def _edited_files(rep: SessionReport, start_ms: int | None = None,
                  end_ms: int | None = None) -> list[str]:
    """编辑类工具碰过的文件（按首次出现顺序，去重）。

    给了区间时只收区间内回合的编辑：``ToolOp.turn`` 与 ``turn_stats`` 同号空间，
    回合时间戳未知的编辑照收（宁可多列不漏）。
    """
    u = rep.usage
    if u is None:
        return []
    turn_ts: dict[int, int] = {}
    if start_ms is not None:
        for t in u.turn_stats or ():
            if t.ts and t.turn not in turn_ts:
                turn_ts[t.turn] = t.ts
    seen: dict[str, None] = {}
    for op in u.tool_ops:
        if not op.path or op.tool not in _EDIT_TOOLS or op.path in seen:
            continue
        ts = turn_ts.get(op.turn)
        if ts is not None and not (start_ms <= ts <= end_ms):
            continue
        seen[op.path] = None
    return list(seen)


def _project_name(cwd: str | None, fallback: str) -> str:
    if not cwd:
        return fallback
    try:
        if normcase_path(cwd) == normcase_path(str(Path.home())):
            return "~（用户主目录）"
    except Exception:  # noqa: BLE001
        pass
    name = _pure(cwd).name
    return name or cwd


def _date_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _short_date(date_str: str) -> str:
    """'2026-11-10' -> '11-10 周二'。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.strftime('%m-%d')} {_WEEKDAY_NAMES[d.weekday()]}"
    except ValueError:
        return date_str


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class SessionItem:
    """一个会话在回顾里的事实条目。"""

    session_id: str
    source: str
    project: str
    datetime_str: str               # 会话开始 'YYYY-MM-DD HH:MM'
    date_str: str                   # 会话开始日期
    net_loc: int = 0
    added: int = 0
    deleted: int = 0
    rework: int = 0
    cost: float = 0.0
    tokens: int = 0
    models: list[str] = field(default_factory=list)
    duration_min: float = 0.0
    prompts: list[str] = field(default_factory=list)       # 区间内有内容的提问（全文 ≤ USER_TEXT_MAX）
    prompt_times: list[int | None] = field(default_factory=list)  # 与 prompts 平行（epoch ms / None）
    trivial_prompts: int = 0                                # 区间内折叠的简短回复数
    prompts_outside: int = 0                                # 区间外的提问数（未列出）
    edited_files: list[str] = field(default_factory=list)   # 区间内回合编辑过的文件（绝对路径）
    active_dates: list[str] = field(default_factory=list)   # 区间内有提问或回合的日期
    end_str: str = ""                                       # 会话最后活动 'YYYY-MM-DD HH:MM'
    outside_range: bool = False     # 区间外也有活动（净增行数按整个会话统计）
    cwd: str | None = None
    tool_errors: int = 0
    corrections: int = 0

    @property
    def is_substantive(self) -> bool:
        """有实质内容：至少一条非简短提问，或编辑过文件。"""
        return bool(self.prompts) or bool(self.edited_files)


@dataclass
class ProjectSummary:
    """区间内属于同一真实工作目录的会话。"""

    name: str
    source: str                      # 主要来源（会话数最多者）
    cwd: str | None = None
    sources: list[str] = field(default_factory=list)
    sessions: list[SessionItem] = field(default_factory=list)
    total_net_loc: int = 0
    total_added: int = 0
    total_deleted: int = 0
    total_rework: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    total_duration_hours: float = 0.0
    total_prompts: int = 0
    models: set[str] = field(default_factory=set)
    edited_files: list[str] = field(default_factory=list)
    active_dates: list[str] = field(default_factory=list)


@dataclass
class WeeklyAggregate:
    """结构化回顾数据（离线渲染与 LLM 归纳共用）。"""

    since: str
    until: str
    title_label: str
    total_sessions: int = 0          # 列出的（有实质内容的）会话数
    skipped_sessions: int = 0        # 折叠未列出的空会话数
    skipped_examples: list[str] = field(default_factory=list)
    total_prompts: int = 0
    total_trivial_prompts: int = 0
    total_prompts_outside: int = 0
    undated_prompts: int = 0
    active_days: int = 0
    total_net_loc: int = 0
    total_added: int = 0
    total_deleted: int = 0
    total_rework: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    total_tool_errors: int = 0
    total_corrections: int = 0
    cross_range_sessions: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    model_counts: dict[str, int] = field(default_factory=dict)
    project_summaries: list[ProjectSummary] = field(default_factory=list)
    day_index: dict[str, list[str]] = field(default_factory=dict)  # 日期 → 项目名（按当天会话数降序）


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
def _range_ms(since: str, until: str) -> tuple[int, int]:
    try:
        d = datetime.strptime(since, "%Y-%m-%d")
        start_ms = int(datetime(d.year, d.month, d.day).timestamp() * 1000)
    except ValueError:
        start_ms = 0
    try:
        d = datetime.strptime(until, "%Y-%m-%d")
        end_ms = int((datetime(d.year, d.month, d.day) + timedelta(days=1)).timestamp() * 1000) - 1
    except ValueError:
        end_ms = 9_999_999_999_999
    return start_ms, end_ms


def _activity(rep: SessionReport, start_ms: int, end_ms: int) -> tuple[list[str], bool, int | None]:
    """(区间内有活动的日期, 区间外是否也有活动, 最后活动时间)。

    优先逐回合时间戳；无回合时间戳时退回 started_at/ended_at 重叠判断。
    """
    u = rep.usage
    if u is None:
        return [], False, None
    stamps = [t.ts for t in (u.turn_stats or ()) if t.ts]
    if stamps:
        inside = {_date_of(t) for t in stamps if start_ms <= t <= end_ms}
        outside = any(t < start_ms or t > end_ms for t in stamps)
        return sorted(inside), outside, max(stamps)
    st, en = u.started_at, u.ended_at or u.started_at
    if not st:
        return [], False, None
    if en < start_ms or st > end_ms:
        return [], True, en
    lo, hi = max(st, start_ms), min(en, end_ms)
    dates = []
    d = datetime.fromtimestamp(lo / 1000).date()
    last = datetime.fromtimestamp(hi / 1000).date()
    while d <= last:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates, (st < start_ms or en > end_ms), en


def collect_weekly_data(
    since: str,
    until: str,
    *,
    project_refs: list[ProjectRef] | None = None,
    scope_source: str = "all",
    today: datetime | None = None,
    progress_callback: Any = None,
) -> WeeklyAggregate:
    """采集区间内全部会话事实，按真实工作目录聚合。"""
    start_ms, end_ms = _range_ms(since, until)
    projs = list(project_refs) if project_refs is not None else list_project_refs(scope_source)

    items: list[SessionItem] = []
    skipped: list[SessionItem] = []
    total_proj = len(projs)
    for i, p in enumerate(projs):
        pname = getattr(p, "display_name", None) or getattr(p, "name", str(p))
        if progress_callback:
            progress_callback(i + 1, total_proj, pname)
        src = getattr(p, "source", "claude")
        try:
            a = analyze.analyze_project(project=getattr(p, "key", str(p)),
                                        source=src, project_ref=p)
        except Exception:  # noqa: BLE001 — 单个项目读失败不拦整份回顾
            continue
        for r in a.reports:
            dates, outside, last_ms = _activity(r, start_ms, end_ms)
            u = r.usage
            if u is None:
                continue
            if not dates:
                # 回合全在区间外：只有会话起止跨过区间时才可能有区间内提问
                st0, en0 = u.started_at, u.ended_at or u.started_at
                if not st0 or st0 > end_ms or (en0 or st0) < start_ms:
                    continue
            st = u.started_at or (last_ms or start_ms)
            cwd = r.meta.cwd or getattr(p, "cwd", None)
            # 提问按各自发出时间筛进区间（时间缺失的照列，计入 undated）
            timed = read_session_user_prompts_timed(r, src)
            inside = [(m, ts) for m, ts in timed
                      if ts is None or start_ms <= ts <= end_ms]
            dates = sorted(set(dates) | {_date_of(ts) for _, ts in inside if ts})
            if not dates:
                continue
            keep = [(m, ts) for m, ts in inside if not is_trivial_prompt(m)]
            prompts = [m for m, _ in keep]
            item = SessionItem(
                session_id=r.meta.session_id or r.meta.path.stem,
                source=src,
                project=_project_name(cwd, pname),
                datetime_str=datetime.fromtimestamp(st / 1000).strftime("%Y-%m-%d %H:%M"),
                date_str=_date_of(st),
                net_loc=r.net_loc or 0,
                added=r.code_added or 0,
                deleted=r.code_deleted or 0,
                rework=r.code_reworked or 0,
                cost=r.cost or 0.0,
                tokens=u.total,
                models=list(u.per_model.keys()),
                duration_min=r.session_duration_minutes or 0.0,
                prompts=prompts,
                prompt_times=[ts for _, ts in keep],
                trivial_prompts=len(inside) - len(keep),
                prompts_outside=len(timed) - len(inside),
                edited_files=_edited_files(r, start_ms, end_ms),
                active_dates=dates,
                end_str=(datetime.fromtimestamp(last_ms / 1000).strftime("%Y-%m-%d %H:%M")
                         if last_ms else ""),
                outside_range=outside or len(timed) > len(inside),
                cwd=cwd,
                tool_errors=u.tool_errors,
                corrections=u.correction_msg_count,
            )
            (items if item.is_substantive else skipped).append(item)

    items.sort(key=lambda s: s.datetime_str)
    return _aggregate(since, until, items, skipped)


def _aggregate(since: str, until: str, items: list[SessionItem],
               skipped: list[SessionItem]) -> WeeklyAggregate:
    agg = WeeklyAggregate(since=since, until=until,
                          title_label=f"周报 ({since} ~ {until})")
    groups: dict[str, ProjectSummary] = {}
    src_by_group: dict[str, dict[str, int]] = {}
    day_proj: dict[str, dict[str, int]] = {}

    for s in items:
        key = normcase_path(s.cwd) if s.cwd else f"name:{s.source}:{s.project}"
        ps = groups.get(key)
        if ps is None:
            ps = groups[key] = ProjectSummary(name=s.project, source=s.source, cwd=s.cwd)
            src_by_group[key] = {}
        ps.sessions.append(s)
        src_by_group[key][s.source] = src_by_group[key].get(s.source, 0) + 1
        ps.total_net_loc += s.net_loc
        ps.total_added += s.added
        ps.total_deleted += s.deleted
        ps.total_rework += s.rework
        ps.total_cost += s.cost
        ps.total_tokens += s.tokens
        ps.total_duration_hours += s.duration_min / 60.0
        ps.total_prompts += len(s.prompts)
        ps.models.update(s.models)
        for f in s.edited_files:
            if f not in ps.edited_files:
                ps.edited_files.append(f)
        for d in s.active_dates:
            if d not in ps.active_dates:
                ps.active_dates.append(d)

        agg.total_prompts += len(s.prompts)
        agg.total_trivial_prompts += s.trivial_prompts
        agg.total_prompts_outside += s.prompts_outside
        agg.undated_prompts += sum(1 for t in s.prompt_times if t is None)
        agg.total_net_loc += s.net_loc
        agg.total_added += s.added
        agg.total_deleted += s.deleted
        agg.total_rework += s.rework
        agg.total_cost += s.cost
        agg.total_tokens += s.tokens
        agg.total_tool_errors += s.tool_errors
        agg.total_corrections += s.corrections
        agg.cross_range_sessions += int(s.outside_range)
        agg.source_counts[s.source] = agg.source_counts.get(s.source, 0) + 1
        for m in s.models:
            agg.model_counts[m] = agg.model_counts.get(m, 0) + 1

    # 同名项目：按上级目录或数据源区分
    by_name: dict[str, list[ProjectSummary]] = {}
    for ps in groups.values():
        by_name.setdefault(ps.name, []).append(ps)
    used_names = set(by_name)
    for same in by_name.values():
        if len(same) > 1:
            for ps in same:
                suffix = (_pure(ps.cwd).parent.name if ps.cwd else "") or source_label(ps.source)
                name = f"{ps.name}（{suffix}）"
                index = 2
                while name in used_names:
                    name = f"{ps.name}（{suffix} {index}）"
                    index += 1
                ps.name = name
                used_names.add(name)

    for key, ps in groups.items():
        counts = src_by_group[key]
        ps.sources = sorted(counts, key=lambda k: -counts[k])
        ps.source = ps.sources[0]
        ps.active_dates.sort()
        for s in ps.sessions:
            s.project = ps.name
            for d in s.active_dates:
                day_proj.setdefault(d, {})
                day_proj[d][ps.name] = day_proj[d].get(ps.name, 0) + 1

    agg.project_summaries = sorted(
        groups.values(),
        key=lambda p: (-p.total_prompts, -len(p.edited_files), p.name.lower()))
    agg.day_index = {d: sorted(c, key=lambda n: (-c[n], n.lower()))
                     for d, c in sorted(day_proj.items())}
    agg.active_days = len(agg.day_index)
    agg.total_sessions = len(items)
    agg.skipped_sessions = len(skipped)
    ex: list[str] = []
    for s in skipped:
        tag = s.project
        if tag not in ex:
            ex.append(tag)
    agg.skipped_examples = ex[:6]
    return agg


# --------------------------------------------------------------------------- #
# 离线 Markdown
# --------------------------------------------------------------------------- #
def _session_header(idx: int, s: SessionItem) -> str:
    start = datetime.strptime(s.datetime_str, "%Y-%m-%d %H:%M")
    head = f"{_short_date(s.date_str)} {start.strftime('%H:%M')}"
    if s.end_str:
        end = datetime.strptime(s.end_str, "%Y-%m-%d %H:%M")
        if end.date() == start.date():
            head += f"–{end.strftime('%H:%M')}"
        else:
            head += f" → {_short_date(s.end_str[:10])} {end.strftime('%H:%M')}"
    bits = [f"**{idx}. {head}**", source_label(s.source)]
    if s.edited_files:
        bits.append(f"编辑 {len(s.edited_files)} 个文件")
    if s.net_loc:
        bits.append(f"净增 {s.net_loc:+,} 行" + ("（整个会话）" if s.outside_range else ""))
    if s.outside_range:
        bits.append("跨周期")
    bits.append(f"会话 {s.session_id[:8]}")
    return " · ".join(bits)


def _prompt_time(ts: int | None, with_date: bool) -> str:
    """提问时间前缀：单日会话 'HH:MM '，多日会话 'MM-DD HH:MM '，未知为空。"""
    if ts is None:
        return ""
    d = datetime.fromtimestamp(ts / 1000)
    return d.strftime("%m-%d %H:%M ") if with_date else d.strftime("%H:%M ")


def _files_line(files: list[str], cwd: str | None, n: int = FILES_SHOWN) -> str:
    shown = [f"`{_rel_display(f, cwd)}`" for f in files[:n]]
    more = f" 等 {len(files)} 个" if len(files) > n else ""
    return "、".join(shown) + more


def render_session_details(agg: WeeklyAggregate) -> list[str]:
    """按项目 → 会话列出全部提问与编辑过的文件（离线报告主体 / LLM 报告附录）。"""
    lines: list[str] = []
    if not agg.project_summaries:
        lines.append("所选区间内没有有内容的会话。")
        return lines
    for ps in agg.project_summaries:
        lines.append(f"### {ps.name}")
        meta = [f"`{ps.cwd}`"] if ps.cwd else []
        meta.append(" + ".join(source_label(x) for x in ps.sources))
        meta.append(f"{len(ps.sessions)} 个会话")
        meta.append(f"{ps.total_prompts} 条提问")
        if ps.edited_files:
            meta.append(f"编辑 {len(ps.edited_files)} 个文件")
        lines.append(" · ".join(meta))
        lines.append("")
        for i, s in enumerate(ps.sessions, 1):
            lines.append(_session_header(i, s))
            multi_day = len(s.active_dates) > 1
            times = s.prompt_times or [None] * len(s.prompts)
            for p, ts in zip(s.prompts, times):
                lines.append(f"- {_prompt_time(ts, multi_day)}{_md_safe(one_line(p))}")
            extra = []
            if s.trivial_prompts:
                extra.append(f"另有 {s.trivial_prompts} 条简短回复，如「继续」「好的」")
            if s.prompts_outside:
                extra.append(f"{s.prompts_outside} 条提问在区间外，未列出")
            if extra:
                lines.append(f"- （{'；'.join(extra)}）")
            if s.edited_files:
                lines.append(f"> 编辑过：{_files_line(s.edited_files, s.cwd)}")
            lines.append("")
    return lines


def _overview_lines(agg: WeeklyAggregate) -> list[str]:
    lines = [
        f"> {agg.active_days} 天有活动 · {len(agg.project_summaries)} 个项目 · "
        f"{agg.total_sessions} 个会话 · 你发出 {agg.total_prompts} 条提问",
        "",
        "## 每日概览",
    ]
    try:
        d = datetime.strptime(agg.since, "%Y-%m-%d")
        last = datetime.strptime(agg.until, "%Y-%m-%d")
    except ValueError:
        d = last = None
    if d is not None and (last - d).days <= 31:
        while d <= last:
            key = d.strftime("%Y-%m-%d")
            projs = agg.day_index.get(key)
            lines.append(f"- **{_short_date(key)}**：" + ("、".join(projs) if projs else "无记录"))
            d += timedelta(days=1)
    else:
        for key, projs in agg.day_index.items():
            lines.append(f"- **{_short_date(key)}**：" + "、".join(projs))
    lines.append("")
    return lines


def _footer_lines(agg: WeeklyAggregate) -> list[str]:
    lines = [
        "---",
        f"本期数据：{agg.total_sessions} 个会话 · {agg.total_prompts} 条提问 · "
        f"净增 {agg.total_net_loc:+,} 行 · 成本 ${agg.total_cost:,.2f}"
        "（详细指标见「指标看板」）",
    ]
    if agg.skipped_sessions:
        ex = "、".join(agg.skipped_examples)
        lines.append(f"未列出：{agg.skipped_sessions} 个无实质内容的会话"
                     f"（无提问或只有简短回复，且没有编辑文件；涉及 {ex}）。")
    if agg.cross_range_sessions:
        note = (f"注：{agg.cross_range_sessions} 个会话标记「跨周期」——只列出区间内发出的提问"
                "与区间内回合编辑的文件")
        if agg.total_prompts_outside:
            note += f"（区间外 {agg.total_prompts_outside} 条提问未列出）"
        lines.append(note + "；净增行数按整个会话统计。")
    if agg.undated_prompts:
        lines.append(f"注：{agg.undated_prompts} 条提问数据源未记录时间，按所属会话列出。")
    lines.append("「编辑过」统计的是编辑类工具调用涉及的文件（含失败的尝试）。")
    return lines


def _title(agg: WeeklyAggregate) -> str:
    span = format_date_span(agg.since, agg.until)
    return f"# 工作回顾 {agg.since} ~ {agg.until}" + (f"（{span}）" if span else "")


def generate_offline_markdown(agg: WeeklyAggregate) -> str:
    """离线确定性回顾：每日概览 → 按项目逐会话列提问 → 一行数据脚注。"""
    lines = [_title(agg), ""]
    lines += _overview_lines(agg)
    lines.append("## 按项目")
    lines.append("")
    lines += render_session_details(agg)
    lines += _footer_lines(agg)
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# LLM 归纳
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = (
    "你在帮一位开发者回顾 TA 自己这段时间做了什么。TA 的工作分散在多个 AI 编程助手"
    "（Claude Code、Antigravity 等）的会话里，下面给出的是每个会话里 TA 亲自发出的"
    "提问原文（已截断）和 AI 编辑过的文件。\n\n"
    "【要求】\n"
    "1. 目标是让 TA 快速想起「这周做了哪些事」。按项目归纳；同一件事分散在多条提问或"
    "多个会话里的，合并成一条。\n"
    "2. 每条事情写清楚：做的是什么、涉及哪些日期；只有从提问或编辑的文件能看出结果时才写"
    "结果，看不出就写「进行中」或不写，**不要猜、不要编**。\n"
    "3. 不评价效率，不分析 Token/成本，不写下周建议，不写套话和管理层汇报腔。\n"
    "4. 若提问里明确出现了未解决的问题或 TA 说要稍后处理的事，单独列在「悬而未决」；"
    "没有就省略这一节。\n"
    "5. 输出 Markdown，只用 #、##、### 标题和「- 」列表，可用 **粗体**，不要用表格。\n"
    "【格式】\n"
    "# 本周做了什么\n"
    "（2~3 句总述）\n"
    "## <项目名>\n"
    "- **<一件事>**（11-10、11-12）：……\n"
    "## 悬而未决\n"
    "- ……"
)


def build_weekly_prompt_payload(agg: WeeklyAggregate) -> tuple[str, str]:
    """LLM 归纳用的 system / user prompt（只含提问、文件、日期，不含成本）。"""
    data_payload: dict[str, Any] = {
        "date_range": f"{agg.since} ~ {agg.until}",
        "projects": [
            {
                "project": ps.name,
                "path": ps.cwd,
                "sessions": [
                    {
                        "start": s.datetime_str,
                        "last_activity": s.end_str or None,
                        "active_dates_in_range": s.active_dates,
                        "tool": source_label(s.source),
                        "user_prompts": [
                            {"time": (datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
                                      if ts else None),
                             "text": one_line(p, LLM_PROMPT_CLIP)}
                            for p, ts in zip(s.prompts, s.prompt_times or [None] * len(s.prompts))
                        ],
                        "short_replies_omitted": s.trivial_prompts,
                        "prompts_outside_range_omitted": s.prompts_outside,
                        "edited_files": [_rel_display(f, s.cwd) for f in s.edited_files[:20]],
                        "edited_files_total": len(s.edited_files),
                        "net_loc": s.net_loc,
                        "spans_outside_range": s.outside_range,
                    }
                    for s in ps.sessions
                ],
            }
            for ps in agg.project_summaries
        ],
    }
    user_payload = (
        "以下是这段时间的会话事实，请按要求帮我回顾我做了什么：\n\n"
        f"```json\n{json.dumps(data_payload, ensure_ascii=False, indent=2)}\n```"
    )
    return _SYSTEM_PROMPT, user_payload


def compose_report(agg: WeeklyAggregate, *, use_llm: bool,
                   chat: Callable[[str, str], str] | None = None,
                   model_name: str = "") -> tuple[str, str]:
    """生成最终文本，返回 ``(text, model_label)``。

    LLM 模式：LLM 归纳在前，**逐会话原始记录**作为附录随后（可核对归纳有没有编）；
    LLM 失败回退离线版并在顶部注明原因。
    """
    offline = generate_offline_markdown(agg)
    if not use_llm or chat is None:
        return offline, "离线规则聚合"
    sys_p, user_p = build_weekly_prompt_payload(agg)
    try:
        llm_text = chat(sys_p, user_p).strip()
    except Exception as err:  # noqa: BLE001
        return (f"> 调用大模型失败（{err}），已回退为本地离线版本。\n\n{offline}",
                "离线规则聚合")
    appendix = ["", "---", "", "## 附：逐会话原始记录（离线生成，可核对上文）", ""]
    appendix += render_session_details(agg)
    appendix += _footer_lines(agg)
    return llm_text + "\n" + "\n".join(appendix).rstrip() + "\n", model_name or "LLM"
