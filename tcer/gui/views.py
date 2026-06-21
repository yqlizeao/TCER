"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``CteiBarChart`` consumes the shared
``export.ctei_ranking`` helper.
"""
from __future__ import annotations

import math
import statistics
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import TYPE_CHECKING

from tcer.core import metrics
from tcer.core.export import ctei_ranking
from tcer.core.format import fmt_dt
from . import theme
from .metric_defs import GROUPS, report_values
from .widgets import Card, MetricCell, ScrollFrame, Tooltip

if TYPE_CHECKING:  # avoid circular import for type hints only
    pass

_PER_ROW = 6  # metric tiles per grid row inside a group


def _short_name(project_hash: str) -> str:
    """Friendlier label for a project-hash folder: strip a leading drive token.

    Hash folders encode a full cwd with separators replaced by '-', so there is
    no reliable project-name delimiter. We only drop a leading ``c--`` style
    drive token and keep the rest intact.
    """
    for i in range(1, len(project_hash) - 2):
        if project_hash[i:i + 2] == "--":
            return project_hash[i + 2:]
    return project_hash


class FilterBar:
    """Top control bar: task type, time filter, subagent toggle, action buttons."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(side="top", fill="x", padx=8, pady=6)

        tk.Label(bar, text="任务类型:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self.task_var = tk.StringVar(value="feature")
        task_cb = ttk.Combobox(bar, textvariable=self.task_var, width=12,
                               values=sorted(metrics.TTAF), state="readonly")
        task_cb.pack(side="left", padx=(4, 4))
        task_cb.bind("<<ComboboxSelected>>", lambda e: controller.reanalyze())
        Tooltip(task_cb, "任务类型影响任务类型系数：调试、重构、审查等任务天然产出更少代码，"
                         "选对类型才能公平比较效率。")

        tk.Label(bar, text="时间:", bg=theme.BG, fg=theme.FG).pack(side="left", padx=(12, 4))
        self.since_var = tk.StringVar(value="")
        self._date_entry(bar, self.since_var, "开始日期（YYYY-MM-DD，留空=全部）").pack(side="left", padx=2)
        tk.Label(bar, text="至", bg=theme.BG, fg=theme.FG).pack(side="left", padx=2)
        self.until_var = tk.StringVar(value="")
        self._date_entry(bar, self.until_var, "结束日期（YYYY-MM-DD，留空=全部）").pack(side="left", padx=2)

        for label, preset in (("本周", "week"), ("本月", "month"), ("全部", "all")):
            tk.Button(bar, text=label, command=lambda p=preset: self._set_preset(p),
                      bg=theme.PANEL, fg=theme.FG, relief="flat", padx=4, pady=2).pack(side="left", padx=2)

        # 视图切换（从右侧面板移入顶部栏）
        self.view_mode = controller.view_mode
        tk.Label(bar, text="视图:", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_UI_BOLD).pack(side="left", padx=(16, 4))
        tk.Radiobutton(bar, text="项目汇总", variable=self.view_mode, value="project",
                       bg=theme.BG, fg=theme.FG, selectcolor=theme.BG,
                       activebackground=theme.BG, activeforeground=theme.ACCENT,
                       font=theme.FONT_UI, command=controller._on_view_change).pack(side="left")
        tk.Radiobutton(bar, text="会话详情", variable=self.view_mode, value="session",
                       bg=theme.BG, fg=theme.FG, selectcolor=theme.BG,
                       activebackground=theme.BG, activeforeground=theme.ACCENT,
                       font=theme.FONT_UI, command=controller._on_view_change).pack(side="left")

        tk.Button(bar, text="刷新项目", command=controller.refresh_projects,
                  bg=theme.PANEL, fg=theme.FG, relief="flat", padx=6).pack(side="left", padx=4)

        # 导出 menu
        mb = tk.Menubutton(bar, text="导出 ▾", relief="flat", bg=theme.PANEL, fg=theme.FG,
                           padx=6, activebackground=theme.BG, activeforeground=theme.FG)
        menu = tk.Menu(mb, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)
        for label, fmt in (("JSON", "json"), ("CSV", "csv"), ("Markdown", "md")):
            menu.add_command(label=label, command=lambda f=fmt: controller.export(f))
        mb.config(menu=menu)
        mb.pack(side="left", padx=4)
        Tooltip(mb, "把当前项目的指标导出为文件（JSON / CSV / Markdown）。")

        # 工具 menu
        tb = tk.Menubutton(bar, text="工具 ▾", relief="flat", bg=theme.PANEL, fg=theme.FG,
                           padx=6, activebackground=theme.BG, activeforeground=theme.FG)
        tmenu = tk.Menu(tb, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                        activebackground=theme.ACCENT, activeforeground=theme.FG)
        tmenu.add_command(label="LOC 校准", command=controller.run_calibration)
        tmenu.add_command(label="计算个人基准", command=controller.compute_baselines)
        tmenu.add_command(label="高级选项", command=controller.show_advanced)
        tb.config(menu=tmenu)
        tb.pack(side="left", padx=4)
        Tooltip(tb, "LOC 校准（对照 git 验证精度）· 计算个人基准 · 高级选项。")

        self.status = tk.Label(bar, text="就绪", bg=theme.BG, fg="#9cdcfe", anchor="e")
        self.status.pack(side="right")

    def _date_entry(self, bar, var, tip):
        e = tk.Entry(bar, textvariable=var, width=10, bg=theme.PANEL, fg=theme.FG,
                     insertbackground=theme.FG, relief="flat", highlightthickness=1,
                     highlightbackground="#3e3e42", highlightcolor=theme.ACCENT)
        e.bind("<Return>", lambda ev: self._validate_and_reanalyze(var))
        e.bind("<FocusOut>", lambda ev: self._validate_and_reanalyze(var))
        Tooltip(e, tip + "（YYYY-MM-DD 格式）。按回车或失焦后生效。")
        return e

    @staticmethod
    def _validate_date(s: str) -> bool:
        if not s:
            return True
        from datetime import datetime
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _validate_and_reanalyze(self, var) -> None:
        v = var.get().strip()
        if self._validate_date(v):
            self.controller.reanalyze()
        else:
            var.set("")
            self.controller.reanalyze()

    def _set_preset(self, preset: str) -> None:
        from datetime import datetime, timedelta
        today = datetime.now()
        if preset == "week":
            monday = today - timedelta(days=today.weekday())
            self.since_var.set(monday.strftime("%Y-%m-%d"))
            self.until_var.set("")
        elif preset == "month":
            self.since_var.set(today.replace(day=1).strftime("%Y-%m-%d"))
            self.until_var.set("")
        else:  # all
            self.since_var.set("")
            self.until_var.set("")
        self.controller.reanalyze()

    def get_params(self) -> dict:
        """Analysis params owned by the bar (task type / time)."""
        return {
            "task_type": self.task_var.get(),
            "since": self.since_var.get().strip() or None,
            "until": self.until_var.get().strip() or None,
        }

    def set_status(self, text: str) -> None:
        self.status.config(text=text)


class ProjectColumn:
    """Left column: a scrollable list of selectable project cards."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cards: list[Card] = []
        self._selected = None

        col = tk.Frame(parent, bg=theme.PANEL)
        col.pack(side="left", fill="both", expand=True)

        header = tk.Frame(col, bg=theme.PANEL)
        header.pack(fill="x", padx=6, pady=4)
        self.count_label = tk.Label(header, text="项目", bg=theme.PANEL, fg=theme.FG,
                                    font=theme.FONT_HEADING, anchor="w")
        self.count_label.pack(side="left")

        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self.scroll = sf
        self.container = sf.inner

    def update(self, projects) -> None:
        for card in self._cards:
            card.frame.destroy()
        self._cards.clear()
        self._selected = None
        self._projects = projects
        for idx, d in enumerate(projects):
            card = self._make_card(d, idx)
            self._cards.append(card)
        self.count_label.config(text=f"项目（{len(projects)}）")
        self.scroll.update_scroll()
        if self._cards:
            self._select(self._cards[0])

    def _make_card(self, project_dir, idx):
        card = Card(self.container,
                    on_click=lambda c, i=idx: self._select(c, i),
                    on_right_click=lambda e, _i=idx, _d=project_dir: self._on_right_click(e, _i, _d),
                    padx=1, pady=1)
        name = _short_name(project_dir.name)
        lbl = tk.Label(card.frame, text=name, bg=theme.PANEL_2, fg=theme.FG,
                       font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        lbl.pack(fill="x", padx=4, pady=3)
        card.bind_to(lbl)
        return card

    def _select(self, card, idx=None):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        card.set_selected(True)
        if idx is not None:
            self.controller.on_select_project(idx)

    def _on_right_click(self, event, idx, project_dir):
        """Right-click context menu on a project card."""
        name = _short_name(project_dir.name)
        menu = tk.Menu(self.container, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)

        menu.add_command(
            label=f"🔄 刷新此项目 · {name[:30]}",
            command=lambda: self._select_and_refresh(idx),
        )

        menu.add_separator()

        menu.add_command(
            label="📊 查看项目概览（指标分类）",
            command=lambda: self._select_and_view(idx, "project"),
        )
        menu.add_command(
            label="📊 查看会话详情视图",
            command=lambda: self._select_and_view(idx, "session"),
        )

        menu.add_separator()

        menu.add_command(
            label="📂 在资源管理器中打开",
            command=lambda: self._open_in_explorer(project_dir),
        )
        menu.add_command(
            label="📋 复制项目路径",
            command=lambda: self._copy_text(str(project_dir)),
        )
        menu.add_command(
            label="📋 复制项目名称",
            command=lambda: self._copy_text(name),
        )

        menu.add_separator()

        menu.add_command(
            label="🔄 刷新全部项目列表",
            command=lambda: self.controller.refresh_projects(),
        )

        menu.tk_popup(event.x_root, event.y_root)

    def _select_and_refresh(self, idx):
        self._select(self._cards[idx], idx)

    def _select_and_view(self, idx, mode):
        already_selected = (self._selected is self._cards[idx])
        if already_selected and self.controller._current:
            # Data already loaded — just switch view mode and re-render
            self.controller.view_mode.set(mode)
            self.controller._on_view_change()
        else:
            # Need to load data first; switch mode, then select (triggers reanalyze)
            self.controller.view_mode.set(mode)
            self._select(self._cards[idx], idx)

    def _open_in_explorer(self, project_dir):
        import subprocess
        try:
            subprocess.Popen(["explorer", str(project_dir)])
        except Exception:
            pass

    def _copy_text(self, text):
        self.controller.root.clipboard_clear()
        self.controller.root.clipboard_append(text)


class SessionColumn:
    """Middle column: a scrollable list of selectable session cards."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cards: list[Card] = []
        self._selected = None

        col = tk.Frame(parent, bg=theme.PANEL)
        col.pack(side="left", fill="both", expand=True)

        header = tk.Frame(col, bg=theme.PANEL)
        header.pack(fill="x", padx=6, pady=4)
        self.count_label = tk.Label(header, text="会话", bg=theme.PANEL, fg=theme.FG,
                                    font=theme.FONT_HEADING, anchor="w")
        self.count_label.pack(side="left")

        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self.scroll = sf
        self.container = sf.inner

    def update(self, reports) -> None:
        for card in self._cards:
            card.frame.destroy()
        self._cards.clear()
        self._selected = None
        self._reports = sorted(reports,
                               key=lambda r: r.usage.ended_at or r.usage.started_at or 0,
                               reverse=True)
        for r in self._reports:
            self._cards.append(self._make_card(r))
        self.count_label.config(text=f"会话（{len(self._reports)}）")
        self.scroll.update_scroll()

    def _make_card(self, r):
        sid = r.meta.session_id or r.meta.path.stem
        title = r.meta.title or "(无标题)"
        card = Card(self.container,
                    on_click=lambda c, s=sid: self._select(c, s),
                    on_right_click=lambda e, _r=r, _s=sid: self._on_right_click(e, _r, _s))
        time_ms = r.usage.ended_at or r.usage.started_at
        t_lbl = tk.Label(card.frame, text=fmt_dt(time_ms, "%m-%d %H:%M") if time_ms else "-",
                         bg=theme.PANEL_2, fg="#888888", font=theme.FONT_MONO, anchor="w")
        t_lbl.pack(fill="x", padx=6, pady=(4, 1))
        title_disp = title[:35] + "..." if len(title) > 35 else title
        ti_lbl = tk.Label(card.frame, text=title_disp, bg=theme.PANEL_2, fg=theme.FG,
                          font=theme.FONT_UI_SMALL, anchor="w")
        ti_lbl.pack(fill="x", padx=6, pady=(1, 1))
        sid_disp = sid[:36] + "..." if len(sid) > 36 else sid
        sid_lbl = tk.Label(card.frame, text=sid_disp, bg=theme.PANEL_2, fg="#6B7077",
                           font=theme.FONT_MONO, cursor="hand2", anchor="w")
        sid_lbl.pack(fill="x", padx=6, pady=(1, 4))
        for w in (t_lbl, ti_lbl, sid_lbl):
            card.bind_to(w)
            w.bind("<Double-Button-1>", lambda e, s=sid: self.controller.show_session_detail(s))
        return card

    def _select(self, card, sid):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        card.set_selected(True)
        self.controller.on_select_session(sid)

    def _on_right_click(self, event, report, sid):
        """Right-click context menu on a session card."""
        from . import popups
        menu = tk.Menu(self.container, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)

        # Session info sub-items
        menu.add_command(
            label=f"📋 查看详情 · {sid[:20]}…",
            command=lambda: self.controller.show_session_detail(sid),
        )
        menu.add_command(
            label="🔧 查看工具调用",
            command=lambda: popups.ToolCallsPopup(
                self.controller.root, report.usage, f" · {sid[:16]}…"),
        )
        has_user_msgs = bool(report.usage.user_message_texts)
        menu.add_command(
            label=f"💬 查看用户消息（{len(report.usage.user_message_texts)} 条）",
            command=lambda: popups.UserMsgsPopup(
                self.controller.root, report.usage.user_message_texts),
            state="normal" if has_user_msgs else "disabled",
        )
        has_files = bool(report.files_touched_details)
        menu.add_command(
            label=f"📁 查看涉及文件（{report.files_touched} 个）",
            command=lambda: popups.FilesTouchedPopup(
                self.controller.root, report.files_touched_details),
            state="normal" if has_files else "disabled",
        )
        menu.add_command(
            label="🤖 查看模型使用",
            command=lambda: popups.ModelsPopup(
                self.controller.root, report.usage, f" · {sid[:16]}…"),
        )

        menu.add_separator()

        # Analysis sub-items
        has_ctei = report.ctei is not None
        menu.add_command(
            label="🎯 查看效率雷达",
            command=lambda: popups.RadarPopup(
                self.controller.root, report, self._reports),
            state="normal" if has_ctei else "disabled",
        )
        menu.add_command(
            label="📈 在趋势图中定位",
            command=lambda: self._navigate_to_trend(sid),
        )

        menu.add_separator()

        # Copy actions
        menu.add_command(
            label="📋 复制会话 ID",
            command=lambda: self._copy_text(sid),
        )
        title = report.meta.title or "(无标题)"
        menu.add_command(
            label="📋 复制会话标题",
            command=lambda: self._copy_text(title),
        )
        cost_str = f"${report.cost:.4f}" if report.cost else "$0"
        tcer_str = f"{report.tcer:.1f}" if report.tcer is not None else "—"
        ctei_str = f"{report.ctei:.2f}" if report.ctei is not None else "—"
        menu.add_command(
            label=f"📋 复制摘要（TCER={tcer_str} · CTEI={ctei_str} · {cost_str}）",
            command=lambda: self._copy_text(
                f"会话: {sid}\n标题: {title}\n"
                f"TCER: {tcer_str} · CTEI: {ctei_str} · 成本: {cost_str}"),
        )

        menu.tk_popup(event.x_root, event.y_root)

    def _navigate_to_trend(self, sid):
        """Switch to trend tab and highlight this session's data point."""
        from tkinter import messagebox
        if not self.controller._current:
            messagebox.showinfo("定位", "请先分析一个项目，趋势图才有数据。")
            return
        # Switch notebook to trend tab (3rd tab, 0-indexed)
        try:
            nb = self.controller._nb
            nb.select(2)
        except Exception:
            pass
        # Ensure trend chart has data (may not have been drawn yet)
        tc = self.controller.trend_chart
        if not tc._reports:
            tc.update(self.controller._current.reports)
        # Highlight the session in the trend chart
        tc.select_session_by_sid(sid)
        # Also select in the session column for consistency
        self.controller.on_select_session(sid)

    def _copy_text(self, text):
        self.controller.root.clipboard_clear()
        self.controller.root.clipboard_append(text)

    def clear_selection(self) -> None:
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = None


class MetricPanel:
    """Right-column tab 1: the G1–G6 metric grid, built from metric_defs."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cells: dict[str, MetricCell] = {}

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self.container = sf.inner

        for group in GROUPS:
            self._build_group(group)

    def _build_group(self, group) -> None:
        header = tk.Frame(self.container, bg=theme.GROUP_COLORS[group.id], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text=f"▼ {group.id} {group.name}",
                 bg=theme.GROUP_COLORS[group.id], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self.container, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))
        for i, metric in enumerate(group.metrics):
            if metric.key == "tools":
                on_click = self.controller.show_tool_calls
            elif metric.key == "models":
                on_click = self.controller.show_models
            elif metric.key == "user_msgs":
                on_click = self.controller.show_user_msgs
            elif metric.key == "files_touched":
                on_click = self.controller.show_files_touched
            else:
                on_click = None
            cell = MetricCell(grid, metric, on_click=on_click)
            cell.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW, sticky="nsew", padx=2)
            self._cells[metric.key] = cell
        for c in range(_PER_ROW):
            grid.grid_columnconfigure(c, weight=1)

    def update(self, report) -> None:
        vals = report_values(report)
        for key, cell in self._cells.items():
            cell.set_value(vals.get(key, "-"))


# --------------------------------------------------------------------------- #
# Charts (Canvas)
# --------------------------------------------------------------------------- #
class CteiBarChart:
    """Tab 2: horizontal CTEI bars per session, colored by grade."""

    def __init__(self, parent) -> None:
        self._ranking: list = []
        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())

    def update(self, reports) -> None:
        self._ranking = ctei_ranking(reports)
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 2 or h < 2:
            return
        if not self._ranking:
            c.create_text(w / 2, h / 2, text="暂无 CTEI 数据\n（会话无可测量的净代码，或已跳过 LOC）",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            return

        label_w, value_w, top_pad, row_gap = 130, 70, 16, 4
        n = len(self._ranking)
        bar_area_w = max(40, w - label_w - value_w - 20)
        row_h = max(18, (h - top_pad) / n - row_gap)
        top = max(r[1] for r in self._ranking)
        scale = top if top > 0 else 1.0
        bar_thick = min(row_h, 18)

        c.create_text(label_w + bar_area_w / 2, 4,
                      text="综合效率指数排名  (优秀>2 · 良好1–2 · 中等0.5–1 · 低效0.1–0.5 · 极端低效<0.1)",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")
        for i, (label, ctei, grade) in enumerate(self._ranking):
            y = top_pad + i * (row_h + row_gap)
            c.create_text(6, y + bar_thick / 2, text=label, anchor="w",
                          fill=theme.FG, font=theme.FONT_MONO)
            n_units = max(1, round(ctei / scale * bar_area_w))
            color = theme.GRADE_HEX.get(grade, theme.FG)
            c.create_rectangle(label_w, y, label_w + n_units, y + bar_thick,
                               fill=color, outline="")
            c.create_text(label_w + bar_area_w + 8, y + bar_thick / 2,
                          text=f"{ctei:.3f} {grade}", anchor="w",
                          fill=color, font=theme.FONT_MONO)


# --------------------------------------------------------------------------- #
# Trend analysis (Tab 3)
# --------------------------------------------------------------------------- #
# Metrics that cannot be plotted (metadata / categorical / constant).
_NON_PLOTTABLE = frozenset({
    "models", "tools", "started", "last_time", "entrypoint",
    "task_type", "grade", "bl_tcer", "bl_ncpi", "bl_cpe",
})

# Baseline reference lines (key → metrics module constant name).
_METRIC_BASELINE: dict[str, str] = {
    "tcer": "TCER_BASELINE",
    "ncpi": "NCPI_BASELINE",
    "cpe": "CPE_BASELINE",
}

# Fixed palette for multi-metric overlay (up to 4 lines).
_OVERLAY_COLORS = ["#007acc", "#4ec9b0", "#ce9178", "#c586c0"]

# CTEI grade background bands (lo, hi, fill_color, label).
_CTEI_BANDS: list[tuple[float, float, str, str]] = [
    (2.0, 999, "#142814", "优秀 >2"),
    (1.0, 2.0, "#14202e", "良好 1–2"),
    (0.5, 1.0, "#2e2a14", "中等 0.5–1"),
    (0.1, 0.5, "#2e1e14", "低效 0.1–0.5"),
    (0.0, 0.1, "#2e1414", "极端低效 <0.1"),
]


def _units_compatible(overlays: list[_OverlayLine]) -> bool:
    """True if all overlays share the same non-empty unit (same-scale OK)."""
    units = {ol.unit for ol in overlays if ol.unit}
    return len(units) <= 1


def metric_raw_value(report, key: str) -> float | None:
    """Extract the raw numeric value for *key* from a SessionReport.

    Returns None when the metric is unavailable or not numeric.
    Special-cases a few keys whose live on ``report.usage`` or need scaling.
    """
    u = report.usage
    try:
        if key == "chr":
            return report.chr * 100.0 if report.chr is not None else None
        if key == "duration":
            if u.started_at and u.ended_at:
                return (u.ended_at - u.started_at) / 3600_000
            return None
        if key == "latency":
            return report.avg_turn_latency_sec
        if key == "tools":
            return float(sum(u.tool_calls.values())) if u.tool_calls else None
        if key == "total_tokens":
            return float(u.total)
        if key == "input":
            return float(u.input_tokens)
        if key == "output":
            return float(u.output_tokens)
        if key == "cache_write":
            return float(u.cache_creation_input_tokens)
        if key == "cache_read":
            return float(u.cache_read_input_tokens)
        if key == "turns":
            return float(u.assistant_msgs)
        if key == "skipped":
            return float(u.empty_usage_skipped)
        if key == "subagent":
            return float(report.subagent_count)
        if key == "user_msgs":
            return float(u.user_msgs)
        if key == "thinking_count":
            return float(u.thinking_count)
        # Default: direct attribute on report
        v = getattr(report, key, None)
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _nice_ticks(v_min: float, v_max: float, n: int = 5) -> list[float]:
    """Compute *n* 'nice' (round-number) tick values between *v_min* and *v_max*."""
    span = v_max - v_min
    if span <= 0:
        return [v_min]
    raw_step = span / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / mag
    if residual <= 1.5:
        nice_step = 1 * mag
    elif residual <= 3.5:
        nice_step = 2 * mag
    elif residual <= 7.5:
        nice_step = 5 * mag
    else:
        nice_step = 10 * mag
    start = math.ceil(v_min / nice_step) * nice_step
    ticks = []
    v = start
    while v <= v_max + nice_step * 0.01:
        ticks.append(round(v, 10))
        v += nice_step
    # Always include v_min if no tick is close
    if ticks and ticks[0] > v_min + nice_step * 0.5:
        ticks.insert(0, round(v_min, 10))
    elif not ticks:
        ticks = [round(v_min, 10)]
    return ticks


class MetricTrendSelector:
    """Grouped metric picker with single/multi-select modes for the trend chart.

    Built from ``GROUPS`` (metric_defs), filtering out non-plottable keys.
    Each group gets a colored header; metrics are Checkbuttons.
    An "叠加模式" toggle switches between single-select (default) and
    multi-select (up to 4 metrics). Calls *on_change()* on every toggle.
    """

    MAX_OVERLAY = 4

    def __init__(self, parent, on_change) -> None:
        self._on_change = on_change
        self._overlay_mode = False
        self._vars: dict[str, tk.BooleanVar] = {}
        self._buttons: dict[str, tk.Checkbutton] = {}

        # Top controls
        ctrl = tk.Frame(parent, bg=theme.PANEL)
        ctrl.pack(fill="x", padx=2, pady=2)
        self._overlay_var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(
            ctrl, text="叠加模式", variable=self._overlay_var,
            bg=theme.PANEL, fg=theme.FG, selectcolor=theme.BG,
            activebackground=theme.PANEL, activeforeground=theme.ACCENT,
            font=theme.FONT_UI_SMALL, command=self._toggle_overlay,
        )
        cb.pack(side="left", padx=2)
        Tooltip(cb, "开启后可同时勾选最多 4 个指标叠加对比")

        sf = ScrollFrame(parent, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True)
        self._scroll = sf
        inner = sf.inner

        for group in GROUPS:
            hdr = tk.Frame(inner, bg=theme.GROUP_COLORS.get(group.id, theme.PANEL),
                           padx=4, pady=2)
            hdr.pack(fill="x", pady=(2, 0))
            tk.Label(hdr, text=f"▼ {group.id} {group.name}",
                     bg=hdr["bg"], fg=theme.FG,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(fill="x")

            for m in group.metrics:
                if m.key in _NON_PLOTTABLE:
                    continue
                var = tk.BooleanVar(value=(m.key == "tcer"))
                self._vars[m.key] = var
                label = m.name
                if m.unit:
                    label += f"（{m.unit}）"
                rb = tk.Checkbutton(
                    inner, text=label, variable=var,
                    bg=theme.PANEL, fg=theme.FG, selectcolor=theme.BG,
                    activebackground=theme.PANEL, activeforeground=theme.ACCENT,
                    font=theme.FONT_UI, anchor="w", padx=4,
                    command=lambda k=m.key: self._on_toggle(k),
                )
                rb.pack(fill="x", padx=2)
                Tooltip(rb, m.tip)
                self._buttons[m.key] = rb

        self._scroll.update_scroll()

    def _toggle_overlay(self) -> None:
        self._overlay_mode = self._overlay_var.get()
        if not self._overlay_mode:
            # Keep only the first selected metric
            selected = [k for k, v in self._vars.items() if v.get()]
            if len(selected) > 1:
                for k in selected[1:]:
                    self._vars[k].set(False)
        self._on_change()

    def _on_toggle(self, key: str) -> None:
        if not self._overlay_mode:
            # Single-select: uncheck all others
            for k, v in self._vars.items():
                if k != key:
                    v.set(False)
        else:
            # Multi-select: enforce MAX_OVERLAY limit
            selected = [k for k, v in self._vars.items() if v.get()]
            if len(selected) > self.MAX_OVERLAY:
                self._vars[key].set(False)
        # Ensure at least one is selected
        if not any(v.get() for v in self._vars.values()):
            self._vars["tcer"].set(True)
        self._on_change()

    def selected_keys(self) -> list[str]:
        return [k for k, v in self._vars.items() if v.get()]

    def select(self, key: str) -> None:
        for k, v in self._vars.items():
            v.set(k == key)

    @property
    def overlay_mode(self) -> bool:
        return self._overlay_mode


class _ChartTooltip:
    """Lightweight Toplevel tooltip that follows the mouse on a Canvas."""

    def __init__(self, canvas: tk.Canvas) -> None:
        self._canvas = canvas
        self._win: tk.Toplevel | None = None

    def show(self, x: int, y: int, lines: list[str],
             colors: list[str] | None = None) -> None:
        self.hide()
        self._win = tk.Toplevel(self._canvas)
        self._win.wm_overrideredirect(True)

        cx = self._canvas.winfo_rootx() + x + 16
        cy = self._canvas.winfo_rooty() + y - 10
        # Edge detection: flip if near screen edge
        sw = self._canvas.winfo_screenwidth()
        sh = self._canvas.winfo_screenheight()
        if cx + 260 > sw:
            cx = self._canvas.winfo_rootx() + x - 270
        if cy + 80 > sh:
            cy = self._canvas.winfo_rooty() + y - 80
        if cx < 0:
            cx = 4
        if cy < 0:
            cy = 4

        self._win.wm_geometry(f"+{cx}+{cy}")
        fr = tk.Frame(self._win, bg=theme.PANEL_2, relief="solid",
                      borderwidth=1, padx=8, pady=5)
        fr.pack()
        for i, line in enumerate(lines):
            color = (colors[i] if colors and i < len(colors) else theme.FG)
            tk.Label(fr, text=line, bg=theme.PANEL_2, fg=color,
                     font=theme.FONT_UI, anchor="w").pack(anchor="w")

    def hide(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None


@dataclass
class _OverlayLine:
    """Cached geometry for one metric's trend line."""
    key: str
    name: str
    unit: str
    color: str
    values: list[float | None] = field(default_factory=list)
    timestamps: list[int | None] = field(default_factory=list)
    screen_pts: list[tuple[float, float]] = field(default_factory=list)
    report_indices: list[int] = field(default_factory=list)
    y_min: float = 0.0
    y_max: float = 0.0


class TrendChart:
    """Tab 3: multi-metric interactive time-series chart with statistics.

    Sub-components:
    - ``MetricTrendSelector`` (left, 180px) for metric selection
    - ``tk.Canvas`` for the chart
    - ``_ChartTooltip`` for hover details
    - Statistics summary at the bottom

    Controller callbacks:
    - ``on_select_session(sid)``: fired when user clicks a data point
    """

    _PAD_L = 62
    _PAD_R = 20
    _PAD_T = 30
    _PAD_B = 36
    _HIT_RADIUS = 8

    def __init__(self, parent, controller=None) -> None:
        self._controller = controller
        self._reports: list = []
        self._all_reports: list = []
        self._overlay: list[_OverlayLine] = []
        self._selected_idx: int | None = None
        self._tooltip = None
        self._resize_after: str | None = None
        self._mode = tk.StringVar(value="trend")
        # Zoom state
        self._zoom_active = False
        self._zoom_sel_start: int | None = None
        self._zoom_offset = 0  # index offset into _all_reports when zoomed
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_moved = False

        self._build(parent)

    # -- layout -----------------------------------------------------------
    def _build(self, parent) -> None:
        self._body = tk.Frame(parent, bg=theme.BG)
        self._body.pack(fill="both", expand=True)

        # Dynamic content area (rebuilt on mode switch)
        self._content = tk.Frame(self._body, bg=theme.BG)
        self._content.pack(fill="both", expand=True)
        self._build_trend_content()

    def _clear_content(self) -> None:
        for w in self._content.winfo_children():
            w.destroy()

    def _add_mode_buttons(self, top) -> None:
        """Add mode radio buttons to a header frame."""
        for label, val in (("趋势图", "trend"), ("散点图", "scatter"), ("仪表板", "dashboard")):
            tk.Radiobutton(top, text=label, variable=self._mode, value=val,
                           bg=theme.BG, fg=theme.FG, selectcolor=theme.BG,
                           activebackground=theme.BG, activeforeground=theme.ACCENT,
                           font=theme.FONT_UI, command=self._switch_mode).pack(side="left")

    def _build_trend_content(self) -> None:
        self._clear_content()
        # Left: metric selector
        left = tk.Frame(self._content, bg=theme.PANEL, width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._selector = MetricTrendSelector(left, on_change=self._on_selection_change)

        # Right: header + canvas + stats
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True)

        top = tk.Frame(right, bg=theme.BG)
        top.pack(fill="x", padx=4, pady=2)
        self._add_mode_buttons(top)
        self._legend_frame = tk.Frame(top, bg=theme.BG)
        self._legend_frame.pack(side="right")
        self._zoom_reset_btn = tk.Button(
            top, text="重置缩放", command=self._reset_zoom,
            bg=theme.PANEL, fg=theme.WARNING, relief="flat",
            font=theme.FONT_UI_SMALL, padx=6,
        )
        # Hidden by default; shown when zoom is active

        self.canvas = tk.Canvas(right, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Destroy>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Left>", self._on_key_prev)
        self.canvas.bind("<Right>", self._on_key_next)
        self.canvas.focus_set()

        self._stats_frame = tk.Frame(right, bg=theme.PANEL_2, padx=6, pady=3)
        self._stats_frame.pack(fill="x")
        self._stats_labels = []

    def _build_dashboard_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        top = tk.Frame(right, bg=theme.BG)
        top.pack(fill="x", padx=4, pady=2)
        self._add_mode_buttons(top)
        tk.Label(top, text="  6 组代表指标总览", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=8)

        self._dashboard = DashboardChart(right)
        self._dashboard.update(self._reports)

    def _build_scatter_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        top = tk.Frame(right, bg=theme.BG)
        top.pack(fill="x", padx=4, pady=2)
        self._add_mode_buttons(top)

        self._scatter_chart = ScatterChart(right)
        self._scatter_chart.update(self._reports)

    def _switch_mode(self) -> None:
        # Cancel any pending resize redraw
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
            self._resize_after = None
        # Save selector state before teardown
        saved_keys = self._selector.selected_keys() if hasattr(self, '_selector') else ["tcer"]
        self._tooltip.hide()
        mode = self._mode.get()
        if mode == "scatter":
            self._build_scatter_content()
        elif mode == "dashboard":
            self._build_dashboard_content()
        else:
            self._build_trend_content()
            # Restore selector state
            if saved_keys and hasattr(self, '_selector'):
                for k in saved_keys:
                    if k in self._selector._vars:
                        self._selector._vars[k].set(True)
            self._draw()

    # -- public API -------------------------------------------------------
    def update(self, reports) -> None:
        self._all_reports = sorted(reports,
                                   key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._reports = list(self._all_reports)
        self._zoom_active = False
        self._zoom_offset = 0
        self._selected_idx = None
        self._tooltip.hide()
        self._draw()

    # -- event handlers ---------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def _on_selection_change(self) -> None:
        self._selected_idx = None
        self._draw()

    def _on_motion(self, event) -> None:
        idx = self._hit_test(event.x, event.y)
        if idx is None:
            self._tooltip.hide()
            return
        if idx < len(self._reports):
            self._show_tooltip(idx, event.x, event.y)

    def _on_press(self, event) -> None:
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._drag_moved = False

    def _on_drag(self, event) -> None:
        if abs(event.x - self._drag_start_x) > 8:
            self._drag_moved = True
            # Only redraw the selection rectangle (tag-based, no full redraw)
            self.canvas.delete("sel_rect")
            c = self.canvas
            c.create_rectangle(self._drag_start_x, self._PAD_T,
                               event.x, c.winfo_height() - self._PAD_B,
                               outline=theme.ACCENT, dash=(3, 3), width=1,
                               tags="sel_rect")

    def _on_release(self, event) -> None:
        self.canvas.delete("sel_rect")
        if self._drag_moved:
            # Zoom: find report indices at start and end X positions
            self._apply_zoom(self._drag_start_x, event.x)
        else:
            # Click: select data point
            idx = self._hit_test(event.x, event.y)
            if idx is not None and self._controller:
                self._selected_idx = idx
                self._draw()
                r = self._reports[idx]
                sid = r.meta.session_id or r.meta.path.stem
                self._controller.on_select_session(sid)
        self._drag_moved = False

    def _apply_zoom(self, x_start: int, x_end: int) -> None:
        """Zoom to the report index range between x_start and x_end."""
        x_lo, x_hi = min(x_start, x_end), max(x_start, x_end)
        # Find report indices closest to the X positions
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.screen_pts:
            return
        idx_lo, idx_hi = None, None
        for j, (px, _py) in enumerate(ol.screen_pts):
            ri = ol.report_indices[j]
            if px >= x_lo and idx_lo is None:
                idx_lo = ri
            if px <= x_hi:
                idx_hi = ri
        if idx_lo is None or idx_hi is None or idx_lo >= idx_hi:
            return
        # Map back to _all_reports using the current zoom offset
        abs_lo = self._zoom_offset + idx_lo
        abs_hi = self._zoom_offset + idx_hi
        self._reports = self._all_reports[abs_lo:abs_hi + 1]
        self._zoom_offset = abs_lo
        self._zoom_active = True
        self._selected_idx = None
        self._draw()

    def _reset_zoom(self) -> None:
        self._reports = list(self._all_reports)
        self._zoom_active = False
        self._zoom_offset = 0
        self._selected_idx = None
        self._draw()

    def _on_double_click(self, event) -> None:
        """Double-click: show radar popup for the nearest data point."""
        idx = self._hit_test(event.x, event.y)
        if idx is not None and idx < len(self._reports):
            from . import popups
            popups.RadarPopup(self.canvas, self._reports[idx], self._reports)

    def _on_right_click(self, event) -> None:
        """Right-click: context menu for the nearest data point."""
        if not self._controller:
            return
        idx = self._hit_test(event.x, event.y)
        if idx is None or idx >= len(self._reports):
            return
        r = self._reports[idx]
        sid = r.meta.session_id or r.meta.path.stem
        menu = tk.Menu(self.canvas, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)
        menu.add_command(
            label=f"查看会话详情 · {sid[:16]}…",
            command=lambda: self._controller.show_session_detail(sid)
                            if self._controller else None,
        )
        menu.add_command(
            label="查看雷达图",
            command=lambda: self._show_radar_for(idx),
        )
        menu.add_separator()
        menu.add_command(
            label=f"选中此会话（第 {idx + 1} 个）",
            command=lambda: self._select_point(idx),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _show_radar_for(self, idx: int) -> None:
        if idx < len(self._reports):
            from . import popups
            popups.RadarPopup(self.canvas, self._reports[idx], self._reports)

    def _select_point(self, idx: int) -> None:
        self._selected_idx = idx
        self._draw()
        if self._controller and idx < len(self._reports):
            r = self._reports[idx]
            sid = r.meta.session_id or r.meta.path.stem
            self._controller.on_select_session(sid)

    def select_session_by_sid(self, sid: str) -> None:
        """Public API: find and highlight a session by its ID."""
        for i, r in enumerate(self._reports):
            if (r.meta.session_id or r.meta.path.stem) == sid:
                self._selected_idx = i
                self._draw()
                return

    def _on_key_prev(self, _event=None) -> None:
        """Left arrow: select previous data point."""
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.report_indices:
            return
        if self._selected_idx is None:
            new_idx = ol.report_indices[-1]
        else:
            prev = [i for i in ol.report_indices if i < self._selected_idx]
            new_idx = prev[-1] if prev else ol.report_indices[0]
        self._select_point(new_idx)

    def _on_key_next(self, _event=None) -> None:
        """Right arrow: select next data point."""
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.report_indices:
            return
        if self._selected_idx is None:
            new_idx = ol.report_indices[0]
        else:
            nxt = [i for i in ol.report_indices if i > self._selected_idx]
            new_idx = nxt[0] if nxt else ol.report_indices[-1]
        self._select_point(new_idx)

    # -- hit testing ------------------------------------------------------
    def _hit_test(self, mx: int, my: int) -> int | None:
        """Return report index of the nearest data point within _HIT_RADIUS."""
        best_idx = None
        best_dist = self._HIT_RADIUS + 1.0
        for ol in self._overlay:
            for pt_i, (px, py) in enumerate(ol.screen_pts):
                d = math.hypot(mx - px, my - py)
                if d < best_dist:
                    best_dist = d
                    best_idx = ol.report_indices[pt_i]
        return best_idx

    # -- tooltip ----------------------------------------------------------
    @staticmethod
    def _fmt_metric(key: str, raw: float, m: 'Metric | None') -> str:
        """Format a single metric value for tooltip display (lightweight)."""
        if key == "chr":
            return f"{raw:.1f}%"
        if key in ("cost", "cpe", "cost_per_mt"):
            return f"${raw:.4f}"
        if m and m.unit in ("行", "个"):
            return f"{raw:,.0f}"
        return f"{raw:g}"

    def _show_tooltip(self, idx: int, mx: int, my: int) -> None:
        r = self._reports[idx]
        sid = r.meta.session_id or r.meta.path.stem
        title = (r.meta.title or "(无标题)")[:30]
        ts = fmt_dt(r.usage.started_at, "%m-%d %H:%M")
        lines = [f"会话: {sid[:16]}… · {title}", f"时间: {ts}"]
        colors = [theme.ACCENT, theme.MUTED]
        for ol in self._overlay:
            raw = metric_raw_value(r, ol.key)
            if raw is not None:
                m = _metric_by_key.get(ol.key)
                disp = self._fmt_metric(ol.key, raw, m)
                lines.append(f"{ol.name}: {disp}")
                colors.append(ol.color)
        self._tooltip.show(mx, my, lines, colors)

    # -- drawing ----------------------------------------------------------
    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return

        # Show/hide zoom reset button
        if hasattr(self, '_zoom_reset_btn'):
            if self._zoom_active:
                self._zoom_reset_btn.pack(side="right", padx=4)
            else:
                self._zoom_reset_btn.pack_forget()

        keys = self._selector.selected_keys()
        self._build_overlay(keys)
        self._update_legend()

        if not self._overlay:
            c.create_text(w / 2, h / 2, text="无有效数据",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            self._update_stats([])
            return

        pad_l, pad_r, pad_t, pad_b = self._PAD_L, self._PAD_R, self._PAD_T, self._PAD_B
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        n_overlays = len(self._overlay)
        use_multi = (n_overlays >= 2)

        if use_multi:
            # Multi-metric rendering
            if n_overlays == 2 and not _units_compatible(self._overlay):
                self._draw_multi_dual_axis(c, w, h, pad_l, pad_r, pad_t, pad_b,
                                           plot_w, plot_h)
            else:
                self._draw_multi_normalized(c, w, h, pad_l, pad_r, pad_t, pad_b,
                                            plot_w, plot_h)
        else:
            # Single-metric rendering (existing logic)
            ol = self._overlay[0]
            valid = [(i, v) for i, v in enumerate(ol.values) if v is not None]
            if len(valid) < 1:
                c.create_text(w / 2, h / 2, text="该指标在当前时间范围内无有效数据",
                              fill=theme.MUTED, font=theme.FONT_UI, justify="center")
                self._update_stats([])
                return

            lo, hi = ol.y_min, ol.y_max
            bl_val = self._baseline_value(ol.key)
            if bl_val is not None:
                lo, hi = min(lo, bl_val), max(hi, bl_val)
            if hi - lo < 1e-12:
                lo -= 1
                hi += 1

            def xv(i):
                n = len(ol.values)
                return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

            def yv(v):
                return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

            if ol.key == "ctei":
                self._draw_ctei_bands(c, yv, pad_l, plot_w, lo, hi)

            ticks = _nice_ticks(lo, hi, 5)
            for tv in ticks:
                ty = yv(tv)
                c.create_line(pad_l, ty, pad_l + plot_w, ty,
                              fill="#333333", dash=(2, 4))
                c.create_text(pad_l - 6, ty, text=f"{tv:g}", anchor="e",
                              fill=theme.MUTED, font=theme.FONT_UI_SMALL)

            if bl_val is not None and lo <= bl_val <= hi:
                by = yv(bl_val)
                c.create_line(pad_l, by, pad_l + plot_w, by,
                              fill=theme.WARNING, dash=(4, 3))
                c.create_text(pad_l + plot_w - 2, by, text="基准", anchor="e",
                              fill=theme.WARNING, font=theme.FONT_UI_SMALL)

            c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#3e3e42")
            c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                          fill="#3e3e42")

            self._draw_x_axis(c, ol.timestamps, pad_l, plot_w, pad_t, plot_h,
                              len(ol.values))

            self._draw_overlay_line(c, ol, xv, yv,
                                    draw_extrema=True, draw_selection=True)

            # Prediction line (linear extrapolation)
            if len(ol.screen_pts) >= 3:
                self._draw_prediction(c, ol, xv, yv, pad_l, plot_w)

            label = f"{ol.name}"
            if ol.unit:
                label += f"（{ol.unit}）"
            c.create_text(pad_l + plot_w / 2, 6, text=f"{label} · 趋势",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

            valid_vals = [v for _, v in valid]
            ts_list = [ol.timestamps[i] for i, _ in valid]
            self._update_stats([(ol.key, ol.name, ol.unit, ol.color,
                                 valid_vals, ts_list)])

    def _build_overlay(self, keys: list[str]) -> None:
        """Build _OverlayLine objects for the given metric keys."""
        self._overlay = []
        for ki, key in enumerate(keys):
            metric = _metric_by_key.get(key)
            if metric is None:
                continue
            values = [metric_raw_value(r, key) for r in self._reports]
            timestamps = [r.usage.started_at or r.usage.ended_at
                          for r in self._reports]
            valid_vals = [v for v in values if v is not None]
            if not valid_vals:
                continue
            self._overlay.append(_OverlayLine(
                key=key, name=metric.name,
                unit=metric.unit, color=_OVERLAY_COLORS[ki % len(_OVERLAY_COLORS)],
                values=values, timestamps=timestamps,
                y_min=min(valid_vals), y_max=max(valid_vals),
            ))

    def _baseline_value(self, key: str) -> float | None:
        bl_name = _METRIC_BASELINE.get(key)
        if bl_name:
            return getattr(metrics, bl_name, None)
        return None

    def _draw_x_axis(self, c, timestamps, pad_l, plot_w, pad_t, plot_h,
                     n_pts) -> None:
        """Draw X-axis date labels with smart density."""
        valid_ts = [t for t in timestamps if t is not None]
        if not valid_ts:
            return
        min_ts, max_ts = min(valid_ts), max(valid_ts)
        span_ms = max_ts - min_ts
        # Choose format based on span
        if span_ms <= 24 * 3600_000:
            dt_fmt = "%H:%M"
        else:
            dt_fmt = "%m-%d"

        max_labels = max(2, int(plot_w / 55))
        step = max(1, len(timestamps) // max_labels)

        for i in range(n_pts):
            ts = timestamps[i]
            if ts is None:
                continue
            if i % step != 0 and i != n_pts - 1:
                continue
            px = pad_l + (plot_w * i / (n_pts - 1)) if n_pts > 1 else pad_l + plot_w / 2
            # Tick mark
            c.create_line(px, pad_t + plot_h, px, pad_t + plot_h + 4,
                          fill="#3e3e42")
            label = fmt_dt(ts, dt_fmt)
            if label == "-":
                continue
            # Stagger alternating labels to reduce overlap
            y_off = 10 if (i // step) % 2 == 0 else 20
            c.create_text(px, pad_t + plot_h + y_off, text=label,
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

    def _draw_ctei_bands(self, c, yv, pad_l, plot_w, lo, hi) -> None:
        """Draw grade background bands when CTEI is selected."""
        for lo_b, hi_b, color_b, _label in _CTEI_BANDS:
            if hi_b < lo or lo_b > hi:
                continue
            y_top = yv(min(hi_b, hi))
            y_bot = yv(max(lo_b, lo))
            c.create_rectangle(pad_l, y_top, pad_l + plot_w, y_bot,
                               fill=color_b, outline="")
            # Right-edge label
            c.create_text(pad_l + plot_w - 4, (y_top + y_bot) / 2,
                          text=_label, anchor="e",
                          fill="#555555", font=theme.FONT_UI_SMALL)

    @staticmethod
    def _find_extrema(values: list[float]) -> tuple[list[int], list[int]]:
        """Return (peak_indices, valley_indices) for a numeric series."""
        peaks, valleys = [], []
        for i in range(1, len(values) - 1):
            if values[i] > values[i - 1] and values[i] > values[i + 1]:
                peaks.append(i)
            elif values[i] < values[i - 1] and values[i] < values[i + 1]:
                valleys.append(i)
        # Limit to top 3 each by value prominence
        if len(peaks) > 3:
            peaks = sorted(peaks, key=lambda i: values[i], reverse=True)[:3]
            peaks.sort()
        if len(valleys) > 3:
            valleys = sorted(valleys, key=lambda i: values[i])[:3]
            valleys.sort()
        return peaks, valleys

    @staticmethod
    def _draw_marker(c, px: float, py: float, is_peak: bool, color: str) -> None:
        """Draw a small triangle marker at an extremum."""
        s = 5
        if is_peak:  # upward triangle
            pts = [px, py - s - 2, px - s, py - 2, px + s, py - 2]
        else:  # downward triangle
            pts = [px, py + s + 2, px - s, py + 2, px + s, py + 2]
        c.create_polygon(pts, fill=color, outline=theme.FG)

    # -- multi-metric rendering -------------------------------------------
    def _draw_overlay_line(self, c, ol, xv_fn, yv_fn,
                           draw_extrema: bool = False,
                           draw_selection: bool = False) -> None:
        """Shared: build screen coords, draw polyline + dots, optionally extrema/selection."""
        ol.screen_pts = []
        ol.report_indices = []
        for i, v in enumerate(ol.values):
            if v is None:
                continue
            ol.screen_pts.append((xv_fn(i), yv_fn(v)))
            ol.report_indices.append(i)

        color = ol.color
        if len(ol.screen_pts) >= 2:
            c.create_line(ol.screen_pts, fill=color, width=2, smooth=True)
        for px, py in ol.screen_pts:
            c.create_oval(px - 3, py - 3, px + 3, py + 3,
                          fill=color, outline=theme.FG)

        if draw_extrema and len(ol.screen_pts) >= 5:
            raw_vals = [ol.values[ol.report_indices[i]]
                        for i in range(len(ol.report_indices))]
            peaks, valleys = self._find_extrema(raw_vals)
            for pi in peaks:
                if pi < len(ol.screen_pts):
                    px, py = ol.screen_pts[pi]
                    self._draw_marker(c, px, py, True, color)
            for vi in valleys:
                if vi < len(ol.screen_pts):
                    px, py = ol.screen_pts[vi]
                    self._draw_marker(c, px, py, False, color)

        if draw_selection and self._selected_idx is not None:
            for j, ri in enumerate(ol.report_indices):
                if ri == self._selected_idx:
                    px, py = ol.screen_pts[j]
                    # Vertical crosshair line (solid, visible)
                    c.create_line(px, self._PAD_T, px,
                                  c.winfo_height() - self._PAD_B,
                                  fill=theme.ACCENT, dash=(4, 3), width=1)
                    # Selection ring (large, bright)
                    c.create_oval(px - 10, py - 10, px + 10, py + 10,
                                  outline=theme.ACCENT, width=2)
                    # Label showing which session is selected
                    sel_r = self._reports[ri] if ri < len(self._reports) else None
                    if sel_r:
                        sel_sid = (sel_r.meta.session_id or sel_r.meta.path.stem)[:12]
                        c.create_text(px, py - 16, text=f"▸ {sel_sid}…",
                                      fill=theme.ACCENT, font=theme.FONT_UI_SMALL_BOLD,
                                      anchor="s")
                    break

    def _draw_prediction(self, c, ol, xv, yv, pad_l, plot_w) -> None:
        """Draw a 3-point linear extrapolation as a dashed line."""
        pts = [(i, v) for i, v in enumerate(ol.values) if v is not None]
        if len(pts) < 3:
            return
        n = len(pts)
        # Use last N points for regression (at most 10)
        window = pts[-min(10, n):]
        xs_w = [p[0] for p in window]
        ys_w = [p[1] for p in window]
        mx_ = sum(xs_w) / len(xs_w)
        my_ = sum(ys_w) / len(ys_w)
        ss = sum((x - mx_) ** 2 for x in xs_w)
        if ss == 0:
            return
        slope = sum((xs_w[i] - mx_) * (ys_w[i] - my_) for i in range(len(xs_w))) / ss
        intercept = my_ - slope * mx_
        # Clamp range: use data min/max as soft bounds
        all_vals = [v for _, v in pts]
        v_min, v_max = min(all_vals), max(all_vals)
        v_margin = (v_max - v_min) * 0.3 if v_max > v_min else abs(v_max) * 0.3 or 1.0
        clamp_lo, clamp_hi = v_min - v_margin, v_max + v_margin
        # Extrapolate 3 points beyond the last data point
        last_i = pts[-1][0]
        pred_pts = []
        for step in range(1, 4):
            pi = last_i + step
            pv = max(clamp_lo, min(clamp_hi, slope * pi + intercept))
            pred_pts.append((xv(pi), yv(pv)))
        # Connect last actual point to first prediction
        last_actual = (xv(last_i), yv(pts[-1][1]))
        all_pred = [last_actual] + pred_pts
        c.create_line(all_pred, fill=theme.WARNING, width=1, dash=(4, 4))
        # Mark prediction points with hollow circles
        for px, py in pred_pts:
            c.create_oval(px - 2, py - 2, px + 2, py + 2,
                          outline=theme.WARNING, fill="")
        # Label
        mid = pred_pts[1]
        c.create_text(mid[0], mid[1] - 10, text="预测", fill=theme.WARNING,
                      font=theme.FONT_UI_SMALL)

    def _draw_multi_normalized(self, c, w, h, pad_l, pad_r, pad_t, pad_b,
                               plot_w, plot_h) -> None:
        """Draw 2+ metrics on a normalized 0–1 Y scale."""
        n = len(self._overlay[0].values)

        def xv(i):
            return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

        # Draw each line (normalized per-overlay)
        for ol in self._overlay:
            span = ol.y_max - ol.y_min if ol.y_max != ol.y_min else 1.0
            lo_ = ol.y_min
            def yv(v, _s=span, _lo=lo_):
                return pad_t + plot_h * (1 - (v - _lo) / _s)
            self._draw_overlay_line(c, ol, xv, yv)

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#3e3e42")
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                      fill="#3e3e42")
        c.create_text(pad_l - 6, pad_t, text="1.0", anchor="e",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL)
        c.create_text(pad_l - 6, pad_t + plot_h, text="0.0", anchor="e",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL)
        c.create_text(pad_l - 6, pad_t + plot_h / 2, text="0.5", anchor="e",
                      fill="#444444", font=theme.FONT_UI_SMALL)
        c.create_line(pad_l, pad_t + plot_h / 2, pad_l + plot_w,
                      pad_t + plot_h / 2, fill="#2a2a2a", dash=(2, 4))

        # X-axis
        self._draw_x_axis(c, self._overlay[0].timestamps,
                          pad_l, plot_w, pad_t, plot_h, n)

        # Title
        c.create_text(pad_l + plot_w / 2, 6, text="多指标归一化对比（0–1）",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

        # Stats for all metrics
        stats_items = []
        for ol in self._overlay:
            valid = [v for v in ol.values if v is not None]
            if valid:
                stats_items.append((ol.key, ol.name, ol.unit, ol.color,
                                    valid, ol.timestamps))
        self._update_stats(stats_items)

    def _draw_multi_dual_axis(self, c, w, h, pad_l, pad_r, pad_t, pad_b,
                              plot_w, plot_h) -> None:
        """Draw 2 metrics with independent left/right Y axes."""
        ol_l, ol_r = self._overlay[0], self._overlay[1]
        n = len(ol_l.values)

        lo_l, hi_l = ol_l.y_min, ol_l.y_max
        lo_r, hi_r = ol_r.y_min, ol_r.y_max
        if hi_l - lo_l < 1e-12:
            lo_l -= 1; hi_l += 1
        if hi_r - lo_r < 1e-12:
            lo_r -= 1; hi_r += 1

        def xv(i):
            return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

        def y_l(v):
            return pad_t + plot_h * (1 - (v - lo_l) / (hi_l - lo_l))

        def y_r(v):
            return pad_t + plot_h * (1 - (v - lo_r) / (hi_r - lo_r))

        # Left grid lines
        for tv in _nice_ticks(lo_l, hi_l, 4):
            ty = y_l(tv)
            c.create_line(pad_l, ty, pad_l + plot_w, ty, fill="#2a2a2a", dash=(2, 4))
            c.create_text(pad_l - 6, ty, text=f"{tv:g}", anchor="e",
                          fill=ol_l.color, font=theme.FONT_UI_SMALL)

        # Right grid lines
        for tv in _nice_ticks(lo_r, hi_r, 4):
            ty = y_r(tv)
            c.create_text(pad_l + plot_w + 6, ty, text=f"{tv:g}", anchor="w",
                          fill=ol_r.color, font=theme.FONT_UI_SMALL)

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#3e3e42")
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                      fill="#3e3e42")
        c.create_line(pad_l + plot_w, pad_t, pad_l + plot_w, pad_t + plot_h,
                      fill=ol_r.color, dash=(3, 3))

        # Left line
        self._draw_overlay_line(c, ol_l, xv, y_l)

        # Right line
        self._draw_overlay_line(c, ol_r, xv, y_r)

        # X-axis
        self._draw_x_axis(c, ol_l.timestamps, pad_l, plot_w, pad_t, plot_h, n)

        # Axis labels
        c.create_text(pad_l, pad_t - 10,
                      text=f"← {ol_l.name}" + (f"（{ol_l.unit}）" if ol_l.unit else ""),
                      fill=ol_l.color, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w, pad_t - 10,
                      text=f"{ol_r.name}" + (f"（{ol_r.unit}）" if ol_r.unit else "") + " →",
                      fill=ol_r.color, font=theme.FONT_UI_SMALL, anchor="e")

        # Stats for both
        stats = []
        for ol in (ol_l, ol_r):
            valid = [v for v in ol.values if v is not None]
            if valid:
                stats.append((ol.key, ol.name, ol.unit, ol.color,
                              valid, ol.timestamps))
        self._update_stats(stats)

    # -- legend & stats ---------------------------------------------------
    def _update_legend(self) -> None:
        for w in self._legend_frame.winfo_children():
            w.destroy()
        for ol in self._overlay:
            dot = tk.Label(self._legend_frame, text="●", fg=ol.color,
                           bg=theme.BG, font=theme.FONT_UI)
            dot.pack(side="left", padx=(6, 1))
            lbl = tk.Label(self._legend_frame, text=ol.name, fg=theme.FG,
                           bg=theme.BG, font=theme.FONT_UI_SMALL)
            lbl.pack(side="left")

    def _update_stats(self, items: list[tuple]) -> None:
        """Update the statistics bar. Each item: (key, name, unit, color, values, timestamps)."""
        for w in self._stats_frame.winfo_children():
            w.destroy()
        if not items:
            tk.Label(self._stats_frame, text="暂无统计信息",
                     bg=theme.PANEL_2, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(anchor="w")
            return
        for key, name, unit, color, vals, _ts in items:
            if len(vals) < 1:
                continue
            mean_v = statistics.mean(vals)
            median_v = statistics.median(vals)
            lo_v, hi_v = min(vals), max(vals)
            # Trend direction: compare first-half mean to second-half mean
            mid = len(vals) // 2
            if mid > 0:
                first_half = statistics.mean(vals[:mid])
                second_half = statistics.mean(vals[mid:])
                ratio = (second_half - first_half) / (abs(first_half) or 1)
                if ratio > 0.1:
                    trend = "↑ 上升"
                elif ratio < -0.1:
                    trend = "↓ 下降"
                else:
                    trend = "→ 平稳"
            else:
                trend = "—"
            # Moving average (last 3)
            ma3 = statistics.mean(vals[-3:]) if len(vals) >= 3 else mean_v

            suffix = f" {unit}" if unit else ""
            text = (f"●{name}: "
                    f"均值{mean_v:g}{suffix} · 中位{median_v:g} · "
                    f"{trend} · 近3期{ma3:g} · "
                    f"极值 {lo_v:g}~{hi_v:g}")
            lbl = tk.Label(self._stats_frame, text=text, bg=theme.PANEL_2,
                           fg=color, font=theme.FONT_UI_SMALL, anchor="w")
            lbl.pack(fill="x", anchor="w")



def _pearson_r(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient between two equal-length series."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / (n - 1))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / (n - 1))
    if sx == 0 or sy == 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)
    try:
        return cov / (sx * sy)
    except (ZeroDivisionError, ValueError):
        return 0.0


class ScatterChart:
    """Dual-metric scatter plot for correlation analysis.

    X-axis = metric A, Y-axis = metric B. Each dot = one session.
    Displays Pearson r value and optional regression line.
    """

    _PAD = 60

    def __init__(self, parent) -> None:
        self._reports: list = []
        self._frame = tk.Frame(parent, bg=theme.BG)
        self._frame.pack(fill="both", expand=True)
        self._tooltip = None
        self._point_coords: list[tuple[int, int, int]] = []  # (px, py, report_idx)
        self._resize_after: str | None = None
        self._build(self._frame)

    def _build(self, parent) -> None:
        ctrl = tk.Frame(parent, bg=theme.BG)
        ctrl.pack(fill="x", padx=6, pady=4)
        # X metric
        tk.Label(ctrl, text="X轴:", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_UI_SMALL).pack(side="left")
        self._x_var = tk.StringVar(value="cost")
        plottable = [m.key for _g in GROUPS for m in _g.metrics
                     if m.key not in _NON_PLOTTABLE]
        ttk.Combobox(ctrl, textvariable=self._x_var, width=14, state="readonly",
                     values=plottable).pack(side="left", padx=4)
        self._x_var.trace_add("write", lambda *_: self._on_change())
        # Y metric
        tk.Label(ctrl, text="Y轴:", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(12, 0))
        self._y_var = tk.StringVar(value="tcer")
        ttk.Combobox(ctrl, textvariable=self._y_var, width=14, state="readonly",
                     values=plottable).pack(side="left", padx=4)
        self._y_var.trace_add("write", lambda *_: self._on_change())
        # Info
        self._info = tk.Label(ctrl, text="", bg=theme.BG, fg=theme.FG,
                              font=theme.FONT_UI)
        self._info.pack(side="right", padx=6)

        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())

    def _on_canvas_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def update(self, reports) -> None:
        self._reports = list(reports)
        self._draw()

    def _on_change(self) -> None:
        self._draw()

    def _on_motion(self, event) -> None:
        for px, py, ri in self._point_coords:
            if math.hypot(event.x - px, event.y - py) <= 8 and ri < len(self._reports):
                r = self._reports[ri]
                sid = r.meta.session_id or r.meta.path.stem
                xk, yk = self._x_var.get(), self._y_var.get()
                xm = _metric_by_key.get(xk)
                ym = _metric_by_key.get(yk)
                xn = xm.name if xm else xk
                yn = ym.name if ym else yk
                xv = metric_raw_value(r, xk)
                yv = metric_raw_value(r, yk)
                x_disp = TrendChart._fmt_metric(xk, xv, xm) if xv is not None else "?"
                y_disp = TrendChart._fmt_metric(yk, yv, ym) if yv is not None else "?"
                lines = [
                    f"会话: {sid[:20]}…",
                    f"{xn}: {x_disp}",
                    f"{yn}: {y_disp}",
                ]
                self._tooltip.show(event.x, event.y, lines,
                                   [theme.ACCENT, theme.FG, theme.FG])
                return
        self._tooltip.hide()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._point_coords = []
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return

        xk, yk = self._x_var.get(), self._y_var.get()
        xs, ys, ris = [], [], []
        for i, r in enumerate(self._reports):
            xv = metric_raw_value(r, xk)
            yv = metric_raw_value(r, yk)
            if xv is not None and yv is not None:
                xs.append(xv)
                ys.append(yv)
                ris.append(i)

        if len(xs) < 2:
            c.create_text(w / 2, h / 2, text="需要 ≥2 个有效数据点",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            self._info.config(text="")
            return

        pad = self._PAD
        plot_w = w - pad * 2
        plot_h = h - pad * 2
        lo_x, hi_x = min(xs), max(xs)
        lo_y, hi_y = min(ys), max(ys)
        if hi_x - lo_x < 1e-12:
            lo_x -= 1; hi_x += 1
        if hi_y - lo_y < 1e-12:
            lo_y -= 1; hi_y += 1

        def xv(v):
            return pad + plot_w * (v - lo_x) / (hi_x - lo_x)

        def yv(v):
            return pad + plot_h * (1 - (v - lo_y) / (hi_y - lo_y))

        # Grid lines (Y)
        for tv in _nice_ticks(lo_y, hi_y, 4):
            ty = yv(tv)
            c.create_line(pad, ty, pad + plot_w, ty, fill="#2a2a2a", dash=(2, 4))
            c.create_text(pad - 6, ty, text=f"{tv:g}", anchor="e",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # Grid lines (X)
        for tv in _nice_ticks(lo_x, hi_x, 4):
            tx = xv(tv)
            c.create_line(tx, pad, tx, pad + plot_h, fill="#2a2a2a", dash=(2, 4))
            c.create_text(tx, pad + plot_h + 6, text=f"{tv:g}", anchor="n",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # Axes
        c.create_line(pad, pad, pad, pad + plot_h, fill="#3e3e42")
        c.create_line(pad, pad + plot_h, pad + plot_w, pad + plot_h, fill="#3e3e42")

        # Dots
        for xi, yi, ri in zip(xs, ys, ris):
            px, py = xv(xi), yv(yi)
            color = theme.ACCENT
            grade = self._reports[ri].grade
            if grade:
                color = theme.GRADE_HEX.get(grade, theme.ACCENT)
            c.create_oval(px - 4, py - 4, px + 4, py + 4,
                          fill=color, outline=theme.FG)
            self._point_coords.append((int(px), int(py), ri))

        # Regression line
        r_val = _pearson_r(xs, ys)
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        ss_xx = sum((x - mx) ** 2 for x in xs)
        if ss_xx > 0:
            slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / ss_xx
            intercept = my - slope * mx
            x0, x1 = lo_x, hi_x
            y0, y1 = slope * x0 + intercept, slope * x1 + intercept
            c.create_line(xv(x0), yv(y0), xv(x1), yv(y1),
                          fill=theme.WARNING, width=1, dash=(4, 3))

        # Info
        if abs(r_val) >= 0.7:
            strength = "强"
        elif abs(r_val) >= 0.4:
            strength = "中等"
        elif abs(r_val) >= 0.2:
            strength = "弱"
        else:
            strength = "极弱/无"
        xn = _metric_by_key.get(xk, Metric(xk, xk, "", "", "basic")).name
        yn = _metric_by_key.get(yk, Metric(yk, yk, "", "", "basic")).name
        self._info.config(text=f"Pearson r = {r_val:.3f}（{strength}相关） · n={n}")

        # Axis labels
        c.create_text(pad + plot_w / 2, pad + plot_h + 24, text=xn,
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")
        c.create_text(8, pad + plot_h / 2, text=yn,
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w",
                      angle=90)

        # Title
        c.create_text(pad + plot_w / 2, 6,
                      text=f"{xn} vs {yn} 散点图 · r={r_val:.3f}",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")


class DashboardChart:
    """6-group sparkline dashboard — one representative metric per G-group.

    Each sparkline shows the trend of one metric across sessions, with
    min/max/mean annotations and trend direction arrows.
    """

    # One representative metric per G-group.
    _GROUP_METRICS = [
        ("G1", "turns"),
        ("G2", "total_tokens"),
        ("G3", "chr"),
        ("G4", "net_loc"),
        ("G5", "cost"),
        ("G6", "ctei"),
    ]

    def __init__(self, parent) -> None:
        self._reports: list = []
        self._resize_after: str | None = None
        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)

    def _on_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def update(self, reports) -> None:
        self._reports = sorted(reports,
                               key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        n_groups = len(self._GROUP_METRICS)
        cols = 3
        rows = (n_groups + cols - 1) // cols
        cell_w = w // cols
        cell_h = h // rows

        for gi, (gid, key) in enumerate(self._GROUP_METRICS):
            col = gi % cols
            row = gi // cols
            x0 = col * cell_w
            y0 = row * cell_h

            metric = _metric_by_key.get(key)
            name = metric.name if metric else key
            unit = metric.unit if metric else ""
            group_color = theme.GROUP_COLORS.get(gid, theme.PANEL)

            # Cell background
            c.create_rectangle(x0, y0, x0 + cell_w, y0 + cell_h,
                               fill=theme.PANEL_2, outline="#333333")

            # Header bar
            c.create_rectangle(x0, y0, x0 + cell_w, y0 + 20,
                               fill=group_color, outline="")
            label = f"{gid} {name}"
            if unit:
                label += f"（{unit}）"
            c.create_text(x0 + 6, y0 + 10, text=label, fill=theme.FG,
                          font=theme.FONT_UI_SMALL_BOLD, anchor="w")

            # Extract values
            vals = [metric_raw_value(r, key) for r in self._reports]
            valid = [(i, v) for i, v in enumerate(vals) if v is not None]
            if len(valid) < 1:
                c.create_text(x0 + cell_w / 2, y0 + cell_h / 2,
                              text="无数据", fill=theme.MUTED,
                              font=theme.FONT_UI_SMALL)
                continue

            indices = [i for i, _ in valid]
            values = [v for _, v in valid]
            lo_v, hi_v = min(values), max(values)
            mean_v = statistics.mean(values)
            pad = 10
            plot_x = x0 + pad
            plot_w = cell_w - pad * 2
            plot_y = y0 + 24
            plot_h = cell_h - 44

            if hi_v - lo_v < 1e-12:
                hi_v = lo_v + 1

            def xv(i, _indices=indices, _plot_x=plot_x, _plot_w=plot_w, _n=len(vals)):
                return _plot_x + _plot_w * i / max(_n - 1, 1)

            def yv(v, _lo=lo_v, _hi=hi_v, _plot_y=plot_y, _plot_h=plot_h):
                return _plot_y + _plot_h * (1 - (v - _lo) / (_hi - _lo))

            # Mean line
            ym = yv(mean_v)
            c.create_line(plot_x, ym, plot_x + plot_w, ym,
                          fill="#444444", dash=(2, 3))

            # Sparkline
            pts = [(xv(i), yv(v)) for i, v in valid]
            color = _OVERLAY_COLORS[0]
            if len(pts) >= 2:
                c.create_line(pts, fill=color, width=2, smooth=True)
            for px, py in pts:
                c.create_oval(px - 2, py - 2, px + 2, py + 2,
                              fill=color, outline="")

            # Min/Max markers
            max_idx = indices[values.index(hi_v)]
            min_idx = indices[values.index(lo_v)]
            mx_px, mx_py = xv(max_idx), yv(hi_v)
            mn_px, mn_py = xv(min_idx), yv(lo_v)
            c.create_text(mx_px, mx_py - 6, text="▲", fill=theme.SUCCESS,
                          font=theme.FONT_UI_SMALL)
            c.create_text(mn_px, mn_py + 8, text="▼", fill=theme.ERROR,
                          font=theme.FONT_UI_SMALL)

            # Current value + trend arrow
            current = values[-1]
            if len(values) >= 2:
                prev_mean = statistics.mean(values[:len(values) // 2]) if len(values) > 2 else values[0]
                if current > prev_mean * 1.1:
                    trend = "↑"
                    trend_color = theme.SUCCESS
                elif current < prev_mean * 0.9:
                    trend = "↓"
                    trend_color = theme.ERROR
                else:
                    trend = "→"
                    trend_color = theme.MUTED
            else:
                trend = "—"
                trend_color = theme.MUTED

            footer_y = y0 + cell_h - 14
            c.create_text(x0 + 6, footer_y,
                          text=f"当前:{current:g} {trend}",
                          fill=trend_color, font=theme.FONT_UI_SMALL, anchor="w")
            c.create_text(x0 + cell_w - 6, footer_y,
                          text=f"均值:{mean_v:g}",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")


# Build reverse lookup: metric key → Metric object (for _build_overlay / tooltip).
_metric_by_key: dict[str, 'Metric'] = {}
for _g in GROUPS:
    for _m in _g.metrics:
        if _m.key not in _NON_PLOTTABLE:
            _metric_by_key[_m.key] = _m
