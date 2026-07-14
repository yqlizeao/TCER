"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``CteiRankingView`` consumes the shared
``export.ctei_ranking`` / ``export.ctei_decompose`` helpers.
"""
from __future__ import annotations

import math
import statistics
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

from tcer.core import metrics
from tcer.core.export import ctei_decompose, ctei_decompose_avg
from tcer.core.format import fmt_dt
from . import theme
from .metric_defs import (
    GROUPS, MODEL_GROUPS, Metric, METRIC_BY_KEY,
    CTEI_FACTORS, CTEI_FACTOR_GOOD_THRESHOLD, format_factor,
    report_values, raw_value, format_value, format_plot,
    model_display, model_raw, model_tip,
)
from .widgets import Card, MetricCell, ScrollFrame, Tooltip

_PER_ROW = 6  # metric tiles per grid row inside a group


def _short_name(project_hash: str) -> str:
    """Friendlier label for a project-hash folder: strip a leading drive token.

    Hash folders encode a full cwd with separators replaced by '-', so there is
    no reliable project-name delimiter.  Windows: drop a leading ``c--`` style
    drive token.  Unix: strip the leading ``-`` produced by the root ``/``.
    """
    for i in range(1, len(project_hash) - 2):
        if project_hash[i:i + 2] == "--":
            return project_hash[i + 2:]
    # Unix: "/" → "-", strip only the single leading dash for root
    if project_hash.startswith("-"):
        return project_hash[1:]
    return project_hash


def project_label(project) -> str:
    """Display label for a source-aware project ref or legacy Path."""
    source = getattr(project, "source", "claude")
    if source in ("codex", "opencode", "grok"):
        default = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok"}.get(source, source)
        return getattr(project, "display_name", None) or getattr(project, "key", default)
    name = getattr(project, "name", None) or getattr(project, "key", str(project))
    return _short_name(name)


def project_source_label(project) -> str:
    source = getattr(project, "source", "claude")
    if source == "codex":
        return "Codex"
    if source == "opencode":
        return "OpenCode"
    if source == "grok":
        return "Grok"
    return "Claude"


def project_open_path(project) -> str:
    source = getattr(project, "source", "claude")
    if source == "codex":
        from tcer.core.paths import codex_sessions_dir
        return str(codex_sessions_dir())
    if source == "grok":
        from tcer.core.paths import grok_sessions_dir
        return str(grok_sessions_dir())
    path = getattr(project, "path", None)
    cwd = getattr(project, "cwd", None)
    return str(path or cwd or project)


def _file_manager_label() -> str:
    """Platform-appropriate file manager name for menu labels."""
    from .platform import FILE_MANAGER_NAME
    return FILE_MANAGER_NAME


class FilterBar:
    """Top control bar: segmented view switch + filters + actions, single row."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(side="top", fill="x", padx=8, pady=6)

        # -- View switcher: segmented control --
        self.view_mode = controller.view_mode
        seg_bg = tk.Frame(bar, bg="#333333", padx=2, pady=2)
        seg_bg.pack(side="left", padx=(0, 12))
        self._view_btns: dict[str, tk.Label] = {}
        for label, val in [("项目汇总", "project"), ("会话详情", "session")]:
            btn = tk.Label(seg_bg, text=label, padx=8, pady=1, cursor="hand2",
                           font=theme.FONT_UI_SMALL)
            btn.pack(side="left", padx=1)
            btn.bind("<Button-1>", lambda e, v=val: self._set_view(v))
            self._view_btns[val] = btn
        self._update_view_btns()

        # -- Filters --
        tk.Label(bar, text="任务类型:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self.task_var = tk.StringVar(value="代码创作")
        self._task_display_names = {
            "code_creation": "代码创作",
            "code_maintenance": "代码维护",
            "non_coding": "非编码",
        }
        self._task_reverse_map = {v: k for k, v in self._task_display_names.items()}
        task_cb = ttk.Combobox(bar, textvariable=self.task_var, width=10,
                               values=list(self._task_display_names.values()), state="readonly")
        task_cb.pack(side="left", padx=(4, 12))
        task_cb.bind("<<ComboboxSelected>>", self._on_task_type_change)
        Tooltip(task_cb, self._generate_task_type_tooltip())

        tk.Label(bar, text="来源:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self.source_var = tk.StringVar(value="全部")
        self._source_display_names = {
            "all": "全部",
            "claude": "Claude",
            "codex": "Codex",
            "opencode": "OpenCode",
            "grok": "Grok",
        }
        self._source_reverse_map = {v: k for k, v in self._source_display_names.items()}
        source_cb = ttk.Combobox(bar, textvariable=self.source_var, width=8,
                                 values=list(self._source_display_names.values()), state="readonly")
        source_cb.pack(side="left", padx=(4, 12))
        source_cb.bind("<<ComboboxSelected>>", self._on_source_change)
        Tooltip(source_cb, "选择数据来源：全部 / Claude / Codex / OpenCode / Grok")

        tk.Label(bar, text="时间:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self.since_var = tk.StringVar(value="")
        self._date_entry(bar, self.since_var, "开始日期").pack(side="left", padx=2)
        tk.Label(bar, text="至", bg=theme.BG, fg=theme.FG).pack(side="left", padx=2)
        self.until_var = tk.StringVar(value="")
        self._date_entry(bar, self.until_var, "结束日期").pack(side="left", padx=2)

        for label, preset in (("本周", "week"), ("本月", "month"), ("全部", "all")):
            tk.Button(bar, text=label, command=lambda p=preset: self._set_preset(p),
                      bg=theme.PANEL, fg=theme.FG, relief="flat", padx=4, pady=1).pack(side="left", padx=2)

        # -- Actions (right side) --
        for factory in [
            lambda: self._make_tool_menu(bar),
            lambda: self._make_export_menu(bar),
        ]:
            factory().pack(side="right", padx=2)

        self.status = tk.Label(bar, text="就绪", bg=theme.BG, fg="#9cdcfe", anchor="e")
        self.status.pack(side="right", padx=(8, 4))

    def _set_view(self, mode: str) -> None:
        self.view_mode.set(mode)
        self._update_view_btns()
        self.controller._on_view_change()

    def _update_view_btns(self) -> None:
        current = self.view_mode.get()
        for val, btn in self._view_btns.items():
            if val == current:
                btn.config(bg=theme.ACCENT, fg="#ffffff")
            else:
                btn.config(bg="#333333", fg=theme.MUTED)

    def _make_tool_menu(self, parent) -> tk.Menubutton:
        tb = tk.Menubutton(parent, text="工具 ▾", relief="flat", bg=theme.PANEL, fg=theme.FG,
                           padx=6, activebackground=theme.BG, activeforeground=theme.FG)
        tmenu = tk.Menu(tb, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                        activebackground=theme.ACCENT, activeforeground=theme.FG)
        tmenu.add_command(label="LOC 校准", command=self.controller.run_calibration)
        tmenu.add_command(label="计算个人基准", command=self.controller.compute_baselines)
        tmenu.add_command(label="高级选项", command=self.controller.show_advanced)
        tb.config(menu=tmenu)
        Tooltip(tb, "LOC 校准 · 计算个人基准 · 高级选项")
        return tb

    def _make_export_menu(self, parent) -> tk.Menubutton:
        mb = tk.Menubutton(parent, text="导出 ▾", relief="flat", bg=theme.PANEL, fg=theme.FG,
                           padx=6, activebackground=theme.BG, activeforeground=theme.FG)
        menu = tk.Menu(mb, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)
        for label, fmt in (("JSON", "json"), ("CSV", "csv"), ("Markdown", "md")):
            menu.add_command(label=label, command=lambda f=fmt: self.controller.export(f))
        mb.config(menu=menu)
        Tooltip(mb, "导出为 JSON / CSV / Markdown")
        return mb

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

    def _on_task_type_change(self, event) -> None:
        """任务类型变化时的回调"""
        # task_var 存储的是中文名称，直接触发重新分析
        self.controller.reanalyze()

    def _on_source_change(self, event) -> None:
        self.controller.refresh_projects()

    def _generate_task_type_tooltip(self) -> str:
        """生成任务类型的简要说明"""
        lines = []
        for cat_key, cat_info in metrics.TASK_CATEGORIES.items():
            display_name = self._task_display_names.get(cat_key, cat_key)
            lines.append(f"【{display_name}】系数 {cat_info['ttaf']}，TCER {cat_info['typical_tcer_range']}")
        return "\n".join(lines)

    def get_params(self) -> dict:
        """Analysis params owned by the bar (task type / time)."""
        # 将中文名称转换回英文 key
        display_name = self.task_var.get()
        task_type_key = self._task_reverse_map.get(display_name, display_name)
        return {
            "task_type": task_type_key,
            "since": self.since_var.get().strip() or None,
            "until": self.until_var.get().strip() or None,
        }

    def get_source(self) -> str:
        return self._source_reverse_map.get(self.source_var.get(), "all")

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

    def update(self, projects, empty_projects: set | None = None) -> None:
        for card in self._cards:
            card.frame.destroy()
        self._cards.clear()
        self._selected = None
        self._projects = projects
        self._empty = empty_projects or set()
        for idx, d in enumerate(projects):
            card = self._make_card(d, idx, is_empty=(idx in self._empty))
            self._cards.append(card)
        self.count_label.config(text=f"项目（{len(projects)}）")
        self.scroll.update_scroll(reset=True)
        # 自动选中第一个有数据的项目
        if self._cards:
            first_valid = next(
                (i for i in range(len(self._cards)) if i not in self._empty), None
            )
            if first_valid is not None:
                self._select(self._cards[first_valid], first_valid)

    def _make_card(self, project_dir, idx, *, is_empty=False):
        card = Card(self.container,
                    on_click=lambda c, i=idx, e=is_empty: self._on_card_click(c, i, e),
                    on_right_click=lambda e, _i=idx, _d=project_dir: self._on_right_click(e, _i, _d),
                    padx=1, pady=1)
        name = project_label(project_dir)
        label = project_source_label(project_dir)
        if is_empty:
            name += " （无会话）"
        fg = theme.MUTED if is_empty else theme.FG
        lbl = tk.Label(card.frame, text=f"[{label}] {name}", bg=theme.PANEL_2, fg=fg,
                       font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        lbl.pack(fill="x", padx=4, pady=3)
        card.bind_to(lbl)
        return card

    def _on_card_click(self, card, idx, is_empty):
        if is_empty:
            return  # 空项目不响应点击
        self._select(card, idx)

    def _select(self, card, idx=None):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        card.set_selected(True)
        if idx is not None:
            self.controller.on_select_project(idx)

    def _on_right_click(self, event, idx, project_dir):
        """Right-click context menu on a project card."""
        name = project_label(project_dir)
        is_empty = idx in self._empty
        menu = tk.Menu(self.container, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)

        if is_empty:
            menu.add_command(
                label=f"📭 {name[:30]}（无会话数据）", state="disabled",
            )
        else:
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
            label=f"📂 在{_file_manager_label()}中打开",
            command=lambda: self._open_in_explorer(project_dir),
        )
        menu.add_command(
            label="📋 复制项目路径",
            command=lambda: self._copy_text(project_open_path(project_dir)),
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
        from .platform import open_in_file_manager
        open_in_file_manager(project_open_path(project_dir))

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
        self.scroll.update_scroll(reset=True)

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

    def _select(self, card, sid, *, notify=True):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        card.set_selected(True)
        if notify:
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
        has_user_msgs = bool(report.usage.user_message_texts) or (
            report.meta.source in ("codex", "opencode", "grok") and report.usage.user_msgs > 0
        )
        menu.add_command(
            label=f"💬 查看用户消息（{report.usage.user_msgs} 条）",
            command=lambda: self._show_user_msgs(report),
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

        # File location
        menu.add_command(
            label=f"📂 在{_file_manager_label()}中打开",
            command=lambda: self._open_session_file(report),
        )

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
        cost_str = format_value("cost", report.cost)
        tcer_str = format_value("tcer", report.tcer)
        ctei_str = format_value("ctei", report.ctei)
        menu.add_command(
            label=f"📋 复制摘要（TCER={tcer_str} · CTEI={ctei_str} · {cost_str}）",
            command=lambda: self._copy_text(
                f"会话: {sid}\n标题: {title}\n"
                f"TCER: {tcer_str} · CTEI: {ctei_str} · 成本: {cost_str}"),
        )

        menu.add_separator()

        # Destructive action — last item, gated behind a二次确认对话框.
        readonly = report.meta.source in ("codex", "opencode", "grok")
        delete_state = "disabled" if readonly else "normal"
        delete_label = "🗑 删除会话…" if not readonly else f"🗑 删除会话（{project_source_label(report.meta)} 只读）"
        menu.add_command(
            label=delete_label,
            command=lambda: self._confirm_delete(report, sid),
            state=delete_state,
        )

        menu.tk_popup(event.x_root, event.y_root)

    def _confirm_delete(self, report, sid):
        """弹出二次确认；确认后彻底删除该会话（含 subagent / tool-results）。"""
        from . import popups
        title = report.meta.title or "(无标题)"
        popups.ConfirmDeletePopup(
            self.controller.root,
            title=title, session_id=sid,
            on_confirm=lambda: self.controller.delete_session(report),
        )

    def _show_user_msgs(self, report):
        old = getattr(self.controller, "_rendered_report", None)
        self.controller._rendered_report = report
        self.controller.show_user_msgs()
        self.controller._rendered_report = old

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

    def _open_session_file(self, report):
        from .platform import open_in_file_manager
        open_in_file_manager(str(report.meta.path))

    def clear_selection(self) -> None:
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = None

    def select_first(self, *, notify=True) -> str | None:
        """Select the first session card (if any); return its sid."""
        if not self._cards:
            return None
        sid = self._reports[0].meta.session_id or self._reports[0].meta.path.stem
        self._select(self._cards[0], sid, notify=notify)
        return sid

    def select_by_sid(self, sid: str, *, notify=True) -> bool:
        """Select the card whose session id matches ``sid``; return True if found."""
        for card, r in zip(self._cards, self._reports):
            if (r.meta.session_id or r.meta.path.stem) == sid:
                self._select(card, sid, notify=notify)
                return True
        return False


@dataclass
class _MetricGrid:
    """Per-grid collapse state for MetricPanel: the cells, the expander label,
    and whether empty (「-」) cells are currently shown."""
    frame: tk.Frame
    cells: list
    expander: tk.Label
    expander_row: int
    expanded: bool = False


class MetricPanel:
    """Right-column tab 1: the G1–G6 metric grid, built from metric_defs."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cells: dict[str, MetricCell] = {}
        self._grids: list[_MetricGrid] = []

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self.container = sf.inner

        for group in GROUPS:
            self._build_group(group)

    def _build_group(self, group) -> None:
        header = tk.Frame(self.container, bg=theme.GROUP_COLORS[group.id], padx=6, pady=1)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text=f"▼ {group.id} {group.name}",
                 bg=theme.GROUP_COLORS[group.id], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        if group.subgroups:
            for sg in group.subgroups:
                self._build_metric_grid(sg.metrics, sub_label=sg.name)
        else:
            self._build_metric_grid(group.metrics)

    def _build_metric_grid(self, metrics, sub_label: str | None = None) -> None:
        if sub_label:
            sub = tk.Frame(self.container, bg=theme.PANEL, padx=8, pady=0)
            sub.pack(fill="x", pady=(1, 0))
            tk.Label(sub, text=f"· {sub_label}", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self.container, bg=theme.PANEL, padx=4, pady=1)
        grid.pack(fill="x", pady=(0, 0))
        cells: list[MetricCell] = []
        for i, metric in enumerate(metrics):
            if metric.key == "tools":
                on_click = self.controller.show_tool_calls
            elif metric.key == "models":
                on_click = self.controller.show_models
            elif metric.key == "user_msgs":
                on_click = self.controller.show_user_msgs
            elif metric.key == "files_touched":
                on_click = self.controller.show_files_touched
            elif metric.key == "memory_files":
                on_click = self.controller.show_memory_files
            elif metric.key == "cost":
                on_click = self.controller.show_cost_breakdown
            else:
                on_click = None
            cell = MetricCell(grid, metric, on_click=on_click)
            cell.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW, sticky="nsew", padx=2)
            self._cells[metric.key] = cell
            cells.append(cell)
        for c in range(_PER_ROW):
            grid.grid_columnconfigure(c, weight=1)
        # Collapse expander: lives INSIDE the grid so grid_remove/grid() preserves
        # its row — pack_forget + pack would reshuffle it past the next group
        # header. Row = len(metrics) is guaranteed below every cell row; empty
        # rows between collapse and expand are auto-collapsed by Tk's grid.
        exp_row = len(metrics)
        expander = tk.Label(grid, text="", bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor="hand2")
        expander.grid(row=exp_row, column=0, columnspan=_PER_ROW,
                      sticky="w", pady=(1, 0))
        expander.grid_remove()  # hidden until _apply_grid finds empty cells
        state = _MetricGrid(frame=grid, cells=cells, expander=expander,
                            expander_row=exp_row, expanded=False)
        expander.bind("<Button-1>", lambda e, s=state: self._toggle(s))
        self._grids.append(state)

    def update(self, report) -> None:
        vals = report_values(report)
        for key, cell in self._cells.items():
            cell.set_value(vals.get(key, "-"))
        for state in self._grids:
            self._apply_grid(state)

    def clear(self) -> None:
        for cell in self._cells.values():
            cell.set_value("-")
        for state in self._grids:
            self._apply_grid(state)

    def _toggle(self, state: _MetricGrid) -> None:
        state.expanded = not state.expanded
        self._apply_grid(state)

    def _apply_grid(self, state: _MetricGrid) -> None:
        """Reflow one grid: hide empty (「-」) cells when collapsed, repack the
        rest tightly, and show/hide the expander row. The empty set is recomputed
        every call so a session change that fills a previously-empty metric
        brings its cell back automatically; the user's expand/collapse choice
        persists on ``state.expanded`` across updates."""
        empty = [c for c in state.cells if c.var.get() == "-"]
        n_empty = len(empty)
        if state.expanded or n_empty == 0:
            shown = state.cells
        else:
            empty_ids = {id(c) for c in empty}
            shown = [c for c in state.cells if id(c) not in empty_ids]
        for i, c in enumerate(shown):
            c.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW,
                         sticky="nsew", padx=2)
        if not state.expanded and n_empty > 0:
            for c in empty:
                c.frame.grid_remove()
        if n_empty == 0:
            state.expander.grid_remove()
        else:
            arrow = "▼" if state.expanded else "▶"
            action = "收起" if state.expanded else "展开"
            state.expander.config(text=f"{arrow} {n_empty} 项无数据（点击{action}）")
            state.expander.grid()


# --------------------------------------------------------------------------- #
# Charts (Canvas)
# --------------------------------------------------------------------------- #
class CteiRankingView:
    """Tab 2: interactive CTEI ranking dashboard.

    Layout:
      [Grade summary bar — clickable filter chips]
      [Treeview table (left) | Decompose panel (right)]

    Treeview columns: #, 会话, CTEI, 等级. Click header to sort.
    Decompose panel: summary card + 4-factor waterfall bars + project avg comparison.
    """

    # CTEI factor metadata (names / formulas / 好坏阈值) comes from the metric SSOT
    # (metric_defs.CTEI_FACTORS); colours are the shared theme value colours.

    def __init__(self, parent, controller=None) -> None:
        self._controller = controller
        self._ranking: list[tuple] = []  # (label, ctei, grade, report)
        self._avg_factors: dict[str, float] | None = None
        self._current_report = None
        self._grade_filter: str | None = None
        self._sort_col: str = "ctei"
        self._sort_reverse: bool = True

        # -- Grade summary bar (top, wrapped in group header) --
        grade_header = tk.Frame(parent, bg=theme.GROUP_COLORS["G_NEUTRAL"], padx=6, pady=3)
        grade_header.pack(fill="x", pady=(1, 0))
        tk.Label(grade_header, text="▼ 评级分布", bg=theme.GROUP_COLORS["G_NEUTRAL"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        self._grade_canvas = tk.Canvas(parent, bg=theme.PANEL, height=38,
                                       highlightthickness=0)
        self._grade_canvas.pack(fill="x", padx=2, pady=(0, 1))
        self._grade_canvas.bind("<Configure>", lambda e: self._draw_grade_bar())
        self._grade_canvas.bind("<Button-1>", self._on_grade_click)
        self._grade_rects: list[tuple[int, int, int, int, str]] = []

        # -- Split: table (left) + decompose (right) --
        paned = tk.PanedWindow(parent, orient="horizontal", bg=theme.BG, sashwidth=3)
        paned.pack(fill="both", expand=True, padx=2, pady=2)

        table_frame = tk.Frame(paned, bg=theme.BG)
        paned.add(table_frame, minsize=280)

        decomp_frame = tk.Frame(paned, bg=theme.BG)
        paned.add(decomp_frame, minsize=340)

        # -- Treeview with group header --
        tree_header = tk.Frame(table_frame, bg=theme.GROUP_COLORS["G2"], padx=6, pady=3)
        tree_header.pack(fill="x", pady=(1, 0))
        tk.Label(tree_header, text="▼ 会话排名", bg=theme.GROUP_COLORS["G2"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        cols = ("rank", "session", "ctei_val", "grade")
        self._tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                                  selectmode="browse", height=20)
        self._tree.heading("rank",    text="#",    anchor="center",
                           command=lambda: self._sort_by("rank"))
        self._tree.heading("session", text="标题", anchor="w",
                           command=lambda: self._sort_by("session"))
        self._tree.heading("ctei_val", text="CTEI", anchor="e",
                           command=lambda: self._sort_by("ctei"))
        self._tree.heading("grade",   text="等级", anchor="center",
                           command=lambda: self._sort_by("grade"))
        self._tree.column("rank",     width=40,  minwidth=30,  stretch=False, anchor="center")
        self._tree.column("session",  width=140, minwidth=80,  stretch=True,  anchor="w")
        self._tree.column("ctei_val", width=70,  minwidth=50,  stretch=False, anchor="e")
        self._tree.column("grade",    width=70,  minwidth=50,  stretch=False, anchor="center")

        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(fill="both", expand=True)
        sb.pack_forget()  # hidden; mousewheel handles scrolling

        # Mousewheel on enter/leave (same pattern as project/session columns)
        self._unbind_wheel = None
        self._tree.bind("<Enter>", self._on_tree_enter)
        self._tree.bind("<Leave>", self._on_tree_leave)

        # Grade → tag color
        self._tree.tag_configure("grade_优秀",     foreground="#4ec9b0")
        self._tree.tag_configure("grade_良好",     foreground="#42a5f5")
        self._tree.tag_configure("grade_中等",     foreground="#f9a825")
        self._tree.tag_configure("grade_低效",     foreground="#ef6c00")
        self._tree.tag_configure("grade_极端低效", foreground="#e53935")

        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # -- Decompose panel (ScrollFrame with group headers) --
        decomp_sf = ScrollFrame(decomp_frame, bg=theme.BG)
        decomp_sf.canvas.pack(fill="both", expand=True)
        self._decomp_inner = decomp_sf.inner
        self._build_decompose_empty()

    # -- public API -----------------------------------------------------------

    def update(self, reports) -> None:
        scored = [r for r in reports if r.ctei is not None]
        scored.sort(key=lambda r: r.ctei, reverse=True)
        self._ranking = [(r.meta.title or r.meta.session_id or r.meta.path.stem, r.ctei, r.grade or "", r)
                         for r in scored]
        self._avg_factors = ctei_decompose_avg(reports)
        self._current_report = None
        self._grade_filter = None
        self._rebuild_tree()
        self._draw_grade_bar()
        self._draw_decompose()

    # -- grade bar ------------------------------------------------------------

    def _draw_grade_bar(self) -> None:
        c = self._grade_canvas
        c.delete("all")
        self._grade_rects.clear()
        w = c.winfo_width()
        if w < 10:
            return

        grades_in_order = [label for label, _ in metrics.GRADE_BANDS]
        counts = {g: 0 for g in grades_in_order}
        for _, _, g, _ in self._ranking:
            if g in counts:
                counts[g] += 1
        total = sum(counts.values()) or 1

        x = 2
        bar_h = 22
        y0 = 8
        for g in grades_in_order:
            n = counts[g]
            if n == 0 and self._grade_filter != g:
                continue
            seg_w = max(28, int((n / total) * (w - 10)))
            if x + seg_w > w - 2:
                seg_w = w - 2 - x
            fill = theme.GRADE_HEX.get(g, theme.MUTED)
            if self._grade_filter and self._grade_filter != g:
                fill = "#3a3a3a"
            c.create_rectangle(x, y0, x + seg_w, y0 + bar_h,
                               fill=fill, outline="#1e1e1e", width=1)
            if seg_w > 36:
                c.create_text(x + seg_w / 2, y0 + bar_h / 2,
                              text=f"{g} {n}", fill="#ffffff",
                              font=theme.FONT_UI_SMALL, anchor="center")
            self._grade_rects.append((x, y0, x + seg_w, y0 + bar_h, g))
            x += seg_w + 2

    def _on_grade_click(self, event) -> None:
        for x0, y0, x1, y1, g in self._grade_rects:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._grade_filter = None if self._grade_filter == g else g
                self._rebuild_tree()
                self._draw_grade_bar()
                self._draw_decompose()
                return

    # -- Treeview -------------------------------------------------------------

    def _rebuild_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        items = [(l, c, g, r) for l, c, g, r in self._ranking
                 if not self._grade_filter or g == self._grade_filter]
        # Apply sort. Index into (label, ctei, grade, report) tuple.
        col_map = {"rank": 1, "session": 0, "ctei": 1, "grade": 2}
        if self._sort_col in col_map:
            idx = col_map[self._sort_col]
            items.sort(key=lambda t: t[idx], reverse=self._sort_reverse)
        for rank, (label, ctei, grade, report) in enumerate(items, 1):
            tag = f"grade_{grade}" if grade else ""
            self._tree.insert("", "end",
                              values=(rank, label, format_value("ctei", ctei), grade),
                              tags=(tag,),
                              iid=str(id(report)))
        # Restore selection if report still visible
        if self._current_report:
            iid = str(id(self._current_report))
            if self._tree.exists(iid):
                self._tree.selection_set(iid)
                self._tree.see(iid)

    def _on_tree_select(self, _event=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        iid = int(sel[0])
        for label, ctei, grade, report in self._ranking:
            if id(report) == iid:
                self._current_report = report
                self._draw_decompose()
                if self._controller:
                    sid = report.meta.session_id or report.meta.path.stem
                    self._controller.on_select_session(sid)
                return

    def _on_tree_enter(self, _event=None) -> None:
        from .platform import bind_mousewheel
        self._unbind_wheel = bind_mousewheel(
            self._tree, lambda units: self._tree.yview_scroll(units, "units"))

    def _on_tree_leave(self, _event=None) -> None:
        if self._unbind_wheel:
            self._unbind_wheel()
            self._unbind_wheel = None

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = (col == "ctei")  # CTEI desc by default
        self._rebuild_tree()

    # -- Decompose panel (ScrollFrame with group headers) ----------------------

    def _build_decompose_empty(self) -> None:
        for w in self._decomp_inner.winfo_children():
            w.destroy()
        tk.Label(self._decomp_inner, text="← 点击左侧排名表中的会话\n查看 CTEI 因子分解",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                 justify="center", pady=40).pack()

    def _draw_decompose(self) -> None:
        for w in self._decomp_inner.winfo_children():
            w.destroy()

        report = self._current_report
        if report is None:
            self._build_decompose_empty()
            return

        factors = ctei_decompose(report)
        if factors is None:
            tk.Label(self._decomp_inner, text="该会话无 CTEI 数据",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                     pady=40).pack()
            return

        self._build_summary_card(report)
        self._build_factor_section(factors, report)
        self._build_avg_section(factors)

    def _build_summary_card(self, report) -> None:
        """Summary card: CTEI + grade + rank, matching group header style."""
        header = tk.Frame(self._decomp_inner, bg=theme.GROUP_COLORS["G6"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ CTEI 概览", bg=theme.GROUP_COLORS["G6"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        card = tk.Frame(self._decomp_inner, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x", pady=(0, 1))

        sid = report.meta.session_id or report.meta.path.stem
        tk.Label(card, text=sid[:40], bg=theme.PANEL, fg=theme.ACCENT,
                 font=theme.FONT_MONO, anchor="w").pack(anchor="w")

        # CTEI + grade + rank row
        row = tk.Frame(card, bg=theme.PANEL)
        row.pack(fill="x", pady=(4, 0))

        ctei_val = report.ctei
        grade = report.grade or ""
        tk.Label(row, text="CTEI", bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left")
        tk.Label(row, text=format_value("ctei", ctei_val), bg=theme.PANEL,
                 fg=theme.GRADE_HEX.get(grade, theme.FG),
                 font=("Consolas", 16, "bold")).pack(side="left", padx=(4, 8))

        if grade:
            badge = tk.Label(row, text=grade, bg=theme.GRADE_HEX.get(grade, theme.MUTED),
                             fg="#ffffff", font=theme.FONT_UI_SMALL_BOLD, padx=6, pady=1)
            badge.pack(side="left", padx=(0, 8))

        # Rank
        for i, (l, cv, g, r) in enumerate(self._ranking):
            if r is report:
                total = len(self._ranking)
                tk.Label(row, text=f"排名 {i + 1}/{total}", bg=theme.PANEL,
                         fg=theme.MUTED, font=theme.FONT_UI).pack(side="right")
                break

        # TCER
        if report.tcer is not None:
            tk.Label(card, text=f"TCER {report.tcer:.1f} 行/百万", bg=theme.PANEL,
                     fg=theme.FG, font=theme.FONT_UI_SMALL, anchor="e").pack(anchor="e")

    def _build_factor_section(self, factors, report) -> None:
        """Factor bars: 4 CTEI factors with visual bars."""
        header = tk.Frame(self._decomp_inner, bg=theme.GROUP_COLORS["G2"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ CTEI 因子分解", bg=theme.GROUP_COLORS["G2"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self._decomp_inner, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        # Factor rows
        for i, factor in enumerate(CTEI_FACTORS):
            val = factors.get(factor.key, 0.0)
            name, desc = factor.name, factor.formula

            row = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=4)
            row.pack(fill="x")

            # Label + value
            tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG,
                     font=theme.FONT_UI_SMALL, width=10, anchor="w").pack(side="left")
            color = theme.VALUE_GOOD if val >= CTEI_FACTOR_GOOD_THRESHOLD else theme.VALUE_BAD
            tk.Label(row, text=format_factor(val), bg=theme.PANEL, fg=color,
                     font=theme.FONT_VALUE, width=6, anchor="e").pack(side="left", padx=4)

            # Bar
            bar_bg = tk.Frame(row, bg="#333333", height=8)
            bar_bg.pack(side="left", fill="x", expand=True, padx=4)
            bar_w = min(1.0, val / 2.0)  # normalize to 0-1 (max ~2.0)
            if bar_w > 0:
                tk.Frame(bar_bg, bg=color, height=8).place(
                    relx=0, rely=0, relwidth=bar_w, relheight=1.0)
            # 1.0 reference line
            tk.Frame(bar_bg, bg="#555555", width=1, height=8).place(
                    relx=0.5, rely=0, relheight=1.0)

            # Description
            tk.Label(row, text=desc, bg=theme.PANEL, fg=theme.MUTED,
                     font=(theme.FONT_MONO_NAME, 7)).pack(side="left", padx=4)

        # Product line
        prod_frame = tk.Frame(self._decomp_inner, bg=theme.PANEL, padx=10, pady=6)
        prod_frame.pack(fill="x", pady=(0, 1))
        tk.Label(prod_frame, text="乘积 =", bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI).pack(side="left")
        tk.Label(prod_frame, text=f"CTEI  {format_value('ctei', report.ctei)}", bg=theme.PANEL,
                 fg=theme.GRADE_HEX.get(report.grade or "", theme.FG),
                 font=theme.FONT_VALUE).pack(side="left", padx=4)

    def _build_avg_section(self, factors) -> None:
        """Factor bars vs project average."""
        avg = self._avg_factors
        if avg is None:
            return

        header = tk.Frame(self._decomp_inner, bg=theme.GROUP_COLORS["G2"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ 与项目均值对比", bg=theme.GROUP_COLORS["G2"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self._decomp_inner, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        for i, factor in enumerate(CTEI_FACTORS):
            name = factor.name
            sel_val = factors.get(factor.key, 0.0)
            avg_val = avg.get(factor.key, 0.0)

            row = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=3)
            row.pack(fill="x")

            tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG,
                     font=theme.FONT_UI_SMALL, width=10, anchor="w").pack(side="left")

            # Selected value
            sel_color = theme.VALUE_GOOD if sel_val >= avg_val else theme.VALUE_BAD
            tk.Label(row, text=format_factor(sel_val), bg=theme.PANEL, fg=sel_color,
                     font=theme.FONT_VALUE, width=6, anchor="e").pack(side="left", padx=2)

            # Average value
            tk.Label(row, text=f"均值 {format_factor(avg_val)}", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(side="left", padx=4)


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

# CTEI grade background bands (lo, hi, fill_color, label) — names + thresholds
# derived from the metric SSOT (metrics.GRADE_BANDS); only the dark trend-fill
# colours are presentation-local here.
_BAND_FILL = {
    "优秀": "#142814", "良好": "#14202e", "中等": "#2e2a14",
    "低效": "#2e1e14", "极端低效": "#2e1414",
}


def _build_ctei_bands() -> list[tuple[float, float, str, str]]:
    gb = metrics.GRADE_BANDS  # [(label, lower_bound)] best→worst
    bands = []
    for i, (label, lo) in enumerate(gb):
        hi = gb[i - 1][1] if i > 0 else 999
        if i == 0:
            rng = f">{lo:g}"
        elif i == len(gb) - 1:
            rng = f"<{gb[i - 1][1]:g}"
        else:
            rng = f"{lo:g}–{hi:g}"
        bands.append((lo, hi, _BAND_FILL[label], f"{label} {rng}"))
    return bands


_CTEI_BANDS: list[tuple[float, float, str, str]] = _build_ctei_bands()


def _units_compatible(overlays: list[_OverlayLine]) -> bool:
    """True if all overlays share the same non-empty unit (same-scale OK)."""
    units = {ol.unit for ol in overlays if ol.unit}
    return len(units) <= 1


# Raw numeric extraction for charts now lives in the metric SSOT (metric_defs).
# Kept as a module-level alias so existing call sites (and popups importing it
# from here) keep working.
metric_raw_value = raw_value


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

        self._scroll.update_scroll(reset=True)

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
        self._sig: tuple | None = None  # content signature last rendered

    def _place(self, x: int, y: int) -> None:
        """Compute a screen position with edge detection and move the window."""
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

    def show(self, x: int, y: int, lines: list[str],
             colors: list[str] | None = None) -> None:
        # The tooltip tracks the cursor every pixel, but its CONTENT only changes
        # when the hovered data point changes. Rebuilding a Toplevel + N Labels
        # per mouse-motion event is expensive, so when the content is unchanged
        # we reuse the existing window and just reposition it.
        sig = (tuple(lines), tuple(colors or ()))
        if self._win is not None and self._sig == sig:
            self._place(x, y)
            return
        self.hide()
        self._win = tk.Toplevel(self._canvas)
        self._win.wm_overrideredirect(True)
        self._place(x, y)
        fr = tk.Frame(self._win, bg=theme.PANEL_2, relief="solid",
                      borderwidth=1, padx=8, pady=5)
        fr.pack()
        for i, line in enumerate(lines):
            color = (colors[i] if colors and i < len(colors) else theme.FG)
            tk.Label(fr, text=line, bg=theme.PANEL_2, fg=color,
                     font=theme.FONT_UI, anchor="w").pack(anchor="w")
        self._sig = sig

    def hide(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None
        self._sig = None


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

    def _add_mode_buttons(self, parent) -> tk.Frame:
        """Build the shared '趋势分析' group header with the 3 mode radio buttons.

        The three sub-modes (趋势图 / 散点图 / 仪表板) rebuild ``_content`` from
        scratch, so each needs its own header. Callers pack their own extras
        (legend / 重置缩放 / hint label) into the returned frame.
        """
        gc = theme.GROUP_COLORS["G_NEUTRAL"]
        header = tk.Frame(parent, bg=gc, padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ 趋势分析", bg=gc, fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")
        for label, val in (("趋势图", "trend"), ("散点图", "scatter"), ("仪表板", "dashboard")):
            tk.Radiobutton(header, text=label, variable=self._mode, value=val,
                           bg=gc, fg=theme.FG, selectcolor=gc,
                           activebackground=gc, activeforeground=theme.ACCENT,
                           font=theme.FONT_UI, command=self._switch_mode).pack(side="left", padx=4)
        return header

    def _build_trend_content(self) -> None:
        self._clear_content()
        # Left: metric selector
        left = tk.Frame(self._content, bg=theme.PANEL, width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._selector = MetricTrendSelector(left, on_change=self._on_selection_change)

        # Separator
        sep = tk.Frame(self._content, bg="#3e3e42", width=2)
        sep.pack(side="left", fill="y")

        # Right: header + canvas + stats
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True)

        # Mode buttons in group header
        mode_header = self._add_mode_buttons(right)
        self._legend_frame = tk.Frame(mode_header, bg=theme.GROUP_COLORS["G_NEUTRAL"])
        self._legend_frame.pack(side="right")
        self._zoom_reset_btn = tk.Button(
            mode_header, text="重置缩放", command=self._reset_zoom,
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

        # Stats in group header
        stats_header = tk.Frame(right, bg=theme.GROUP_COLORS["G6"], padx=6, pady=3)
        stats_header.pack(fill="x", pady=(1, 0))
        tk.Label(stats_header, text="▼ 统计", bg=theme.GROUP_COLORS["G6"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")
        self._stats_frame = tk.Frame(right, bg=theme.PANEL, padx=6, pady=3)
        self._stats_frame.pack(fill="x")
        self._stats_labels = []

    def _build_dashboard_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        # Mode buttons in group header (same as trend)
        mode_header = self._add_mode_buttons(right)
        tk.Label(mode_header, text="6 组代表指标总览", bg=theme.GROUP_COLORS["G_NEUTRAL"],
                 fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(side="left", padx=8)

        self._dashboard = DashboardChart(right)
        self._dashboard.update(self._reports)

    def _build_scatter_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        # Mode buttons in group header (same as trend)
        self._add_mode_buttons(right)

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
        """Update the chart with new reports, restoring the selected session by sid.

        Zoom is intentionally NOT preserved: the zoom window is a pair of
        indices into the previous report list, which is meaningless once the
        data changes. Only the selected data point is carried over.
        """
        # Save current state
        old_selected_sid = None
        if self._selected_idx is not None and self._selected_idx < len(self._reports):
            r = self._reports[self._selected_idx]
            old_selected_sid = r.meta.session_id or r.meta.path.stem

        # Update data
        self._all_reports = sorted(reports,
                                   key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._reports = list(self._all_reports)
        self._zoom_active = False
        self._zoom_offset = 0
        self._selected_idx = None
        self._tooltip.hide()

        # Restore selection if the session still exists
        if old_selected_sid:
            for i, r in enumerate(self._reports):
                if (r.meta.session_id or r.meta.path.stem) == old_selected_sid:
                    self._selected_idx = i
                    break

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
        """Public API: find and highlight a session by its ID without rebuilding
        the chart (preserves zoom); only the selection overlay is refreshed."""
        for i, r in enumerate(self._reports):
            if (r.meta.session_id or r.meta.path.stem) == sid:
                if self._selected_idx == i:
                    return  # already highlighted
                self._selected_idx = i
                self._refresh_selection()
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
        """Format a single metric value for tooltip display (SSOT: format_plot)."""
        return format_plot(key, raw, m)

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
            self._draw_selection(c, ol)

    def _draw_selection(self, c, ol) -> None:
        """Draw the crosshair + ring + label for the selected point on one
        overlay. Items carry the ``sel_overlay`` tag so they can be wiped and
        redrawn incrementally (see ``_refresh_selection``) without a full chart
        redraw."""
        if self._selected_idx is None:
            return
        for j, ri in enumerate(ol.report_indices):
            if ri == self._selected_idx:
                px, py = ol.screen_pts[j]
                # Vertical crosshair line (solid, visible)
                c.create_line(px, self._PAD_T, px,
                              c.winfo_height() - self._PAD_B,
                              fill=theme.ACCENT, dash=(4, 3), width=1,
                              tags="sel_overlay")
                # Selection ring (large, bright)
                c.create_oval(px - 10, py - 10, px + 10, py + 10,
                              outline=theme.ACCENT, width=2, tags="sel_overlay")
                # Label showing which session is selected
                sel_r = self._reports[ri] if ri < len(self._reports) else None
                if sel_r:
                    sel_sid = (sel_r.meta.session_id or sel_r.meta.path.stem)[:12]
                    c.create_text(px, py - 16, text=f"▸ {sel_sid}…",
                                  fill=theme.ACCENT, font=theme.FONT_UI_SMALL_BOLD,
                                  anchor="s", tags="sel_overlay")
                break

    def _refresh_selection(self) -> None:
        """Incrementally redraw just the selection overlay (crosshair + ring +
        label) without rebuilding the whole chart, so picking a session from the
        list doesn't re-walk every data point. Mirrors the tag-based drag
        rectangle; only drawn in single-metric mode (matching full ``_draw``)."""
        c = self.canvas
        c.delete("sel_overlay")
        if self._selected_idx is None or len(self._overlay) != 1:
            return
        self._draw_selection(c, self._overlay[0])

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


# ============================================================
# 模型对比 (Apple-style, matching MetricPanel layout)
# ============================================================

class ModelCompareView:
    """模型对比 — per-model stats in group/grid layout matching MetricPanel style."""

    _COL_COLORS = ["#569cd6", "#4ec9b0", "#dcdcaa", "#ce9178", "#9cdcfe", "#c586c0"]

    def __init__(self, parent, controller=None):
        self.parent = parent
        self._models: list = []

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self._container = sf.inner

    def update(self, reports) -> None:
        from tcer.core.metrics import compare_models
        self._models = compare_models(reports)
        # Rebuild entire grid
        for w in self._container.winfo_children():
            w.destroy()
        if not self._models:
            tk.Label(self._container, text="无模型数据", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
            return
        self._build_header()
        # Per-model metric groups now come from the SSOT (metric_defs.MODEL_GROUPS):
        # labels, formatting, tooltips and 好坏方向 all live there, shared with the
        # other tabs' metric metadata.
        for group in MODEL_GROUPS:
            self._build_group(group)

    def _build_header(self) -> None:
        """Cost distribution bar + model summary, matching group header style."""
        # Group header with title
        header = tk.Frame(self._container, bg=theme.GROUP_COLORS["G_NEUTRAL"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ 模型对比", bg=theme.GROUP_COLORS["G_NEUTRAL"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        # Cost distribution bar
        total_cost = sum(mc.cost for mc in self._models)
        if total_cost > 0:
            bar = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
            bar.pack(fill="x")
            canvas = tk.Canvas(bar, bg=theme.PANEL, height=20, highlightthickness=0)
            canvas.pack(fill="x")

            def draw_bar(_e=None):
                canvas.delete("all")
                w = canvas.winfo_width()
                if w < 10:
                    return
                # Draw colored segments
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    color = self._COL_COLORS[i % len(self._COL_COLORS)]
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    canvas.create_rectangle(x1, 0, x2, 20, fill=color, outline="")
                    rx += rw
                # Draw model names on top (always visible)
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    cx = (x1 + x2) / 2
                    if x2 - x1 > 20:
                        canvas.create_text(cx, 10, text=mc.display_name,
                                           fill="#1e1e1e",
                                           font=(theme.FONT_MONO_NAME, 7))
                    rx += rw

            canvas.bind("<Configure>", draw_bar)
            canvas.after(10, draw_bar)

        # Summary grid: model names + cost + sessions
        grid = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))
        for j, mc in enumerate(self._models):
            color = self._COL_COLORS[j % len(self._COL_COLORS)]
            cell = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=2)
            cell.grid(row=0, column=j, sticky="nsew", padx=2)
            name_lbl = tk.Label(cell, text=mc.display_name, bg=theme.PANEL, fg=color,
                                font=theme.FONT_VALUE, anchor="w")
            name_lbl.pack(anchor="w")
            cost_str = model_display(mc, "m_cost")
            sub_lbl = tk.Label(cell, text=f"{cost_str} · {mc.session_count} 会话",
                               bg=theme.PANEL, fg=theme.MUTED,
                               font=theme.FONT_UI_SMALL, anchor="w")
            sub_lbl.pack(anchor="w")
            price_tip = _model_price_tip(mc)
            for w in (cell, name_lbl, sub_lbl):
                Tooltip(w, price_tip)
        for j in range(len(self._models)):
            grid.grid_columnconfigure(j, weight=1)

    def _build_group(self, group) -> None:
        """Build one per-model metric group from a metric_defs.Group (SSOT)."""
        header = tk.Frame(self._container, bg=theme.GROUP_COLORS["G2"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text=f"▼ {group.name}", bg=theme.GROUP_COLORS["G2"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        # Column headers (model names)
        tk.Label(grid, text="", bg=theme.PANEL, width=14).grid(row=0, column=0)
        for j, mc in enumerate(self._models):
            color = self._COL_COLORS[j % len(self._COL_COLORS)]
            tk.Label(grid, text=mc.display_name, bg=theme.PANEL, fg=color,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="e").grid(
                         row=0, column=j + 1, sticky="e", padx=2)

        # Metric rows — name / value / tooltip / 好坏方向 all come from the SSOT.
        for i, metric in enumerate(group.metrics):
            key = metric.key
            tip_text = model_tip(key)

            name_lbl = tk.Label(grid, text=metric.name, bg=theme.PANEL, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, anchor="w")
            name_lbl.grid(row=i + 1, column=0, sticky="w")
            if tip_text:
                Tooltip(name_lbl, tip_text)

            # Gold-highlight the best value in this row. metric.sentiment follows
            # the metric's 词性: "up"=越大越好, "down"=越小越好. Skipped for metrics
            # with no good/bad direction, or when all models tie.
            row_colors: dict[int, str] = {}
            if metric.sentiment in ("up", "down"):
                valid = [(j, model_raw(mc, key)) for j, mc in enumerate(self._models)]
                valid = [(j, v) for j, v in valid if isinstance(v, (int, float))]
                distinct = {v for _, v in valid}
                if len(distinct) >= 2:
                    target = max(distinct) if metric.sentiment == "up" else min(distinct)
                    for j, v in valid:
                        if v == target:
                            row_colors[j] = theme.VALUE_BEST

            for j, mc in enumerate(self._models):
                val = model_display(mc, key)
                lbl = tk.Label(grid, text=val, bg=theme.PANEL,
                               fg=row_colors.get(j, theme.VALUE_NEUTRAL),
                               font=theme.FONT_VALUE, anchor="e")
                lbl.grid(row=i + 1, column=j + 1, sticky="e", padx=2)
                if tip_text:
                    Tooltip(lbl, tip_text)

        # Make columns expandable
        for j in range(len(self._models) + 1):
            grid.grid_columnconfigure(j, weight=1)


def _model_price_tip(mc) -> str:
    """Tooltip text: a model's full list price (the four $/MTok billing rates).

    Rates come from ``pricing.resolve`` — the same table used to cost the
    session — so the card shows exactly what each dimension was charged at.
    Unknown models fall back to the Anthropic default list price, which is
    called out in the header so the user doesn't mistake it for the model's
    own official price.
    """
    from tcer.core import pricing

    def _rate(x: float) -> str:
        return f"${f'{x:.4f}'.rstrip('0').rstrip('.')}/百万"

    r = pricing.resolve(mc.model_id)
    known = pricing.table_key(mc.model_id) is not None
    title = "官方标价" if known else "默认配置价（未在价表中）"
    note = "" if known else "\n⚠️ 该模型未在价表中，按 Anthropic 通用 list 价回退，非其厂商官方价。"
    return (
        f"{mc.display_name} · {title}（$/百万 Token）\n"
        f"输入　　　{_rate(r['input'])}\n"
        f"输出　　　{_rate(r['output'])}\n"
        f"缓存创建　{_rate(r['cache_write'])}\n"
        f"缓存命中　{_rate(r['cache_read'])}{note}"
    )


# Reverse lookup metric key → Metric object (overlay / tooltip / axis labels),
# sourced from the metric SSOT so there is a single registry of metric metadata.
_metric_by_key = METRIC_BY_KEY
