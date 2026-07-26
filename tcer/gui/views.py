"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``CteiRankingView`` consumes the shared
``export.ctei_ranking`` / ``export.ctei_decompose`` helpers.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from tcer.core import metrics
from tcer.core.export import ctei_decompose, ctei_decompose_avg
from tcer.core.format import fmt_dt
from . import theme
from .metric_defs import (
    GROUPS, MODEL_GROUPS, UNSUPPORTED_LABEL,
    CTEI_FACTORS, CTEI_FACTOR_GOOD_THRESHOLD, format_factor,
    report_values, format_value,
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
        # Display names from config SSOT (+「自动」per-session inference).
        self._task_display_names = {
            metrics.AUTO_TASK_TYPE: "自动",
            **{k: (v.get("name") or k) for k, v in metrics.TASK_CATEGORIES.items()},
        }
        default_task = metrics.DEFAULT_TASK_TYPE
        default_label = self._task_display_names.get(
            default_task, next(iter(self._task_display_names.values()), "代码创作"))
        self.task_var = tk.StringVar(value=default_label)
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
            lambda: self._make_upload_button(bar),
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
        tmenu.add_command(label="项目总览", command=self.controller.show_project_overview)
        tmenu.add_command(label="工具序列", command=self.controller.show_tool_sequence)
        tmenu.add_command(label="会话时间线", command=self.controller.show_session_timeline)
        tmenu.add_command(label="会话对比", command=self.controller.show_session_compare)
        tmenu.add_command(label="LOC 校准", command=self.controller.run_calibration)
        tmenu.add_command(label="计算个人基准", command=self.controller.compute_baselines)
        tmenu.add_command(label="高级选项", command=self.controller.show_advanced)
        tb.config(menu=tmenu)
        Tooltip(tb, "会话对比 · LOC 校准 · 计算个人基准 · 高级选项")
        return tb

    def _make_export_menu(self, parent) -> tk.Menubutton:
        mb = tk.Menubutton(parent, text="导出 ▾", relief="flat", bg=theme.PANEL, fg=theme.FG,
                           padx=6, activebackground=theme.BG, activeforeground=theme.FG)
        menu = tk.Menu(mb, tearoff=False, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.ACCENT, activeforeground=theme.FG)
        for label, fmt in (("项目报告 (HTML)", "html"), ("项目报告 (Markdown)", "md"),
                           ("项目数据 (JSON)", "json"), ("项目数据 (CSV)", "csv")):
            menu.add_command(label=label, command=lambda f=fmt: self.controller.export(f))
        menu.add_separator()
        for label, fmt in (("当前会话报告 (HTML)", "html"), ("当前会话报告 (Markdown)", "md"),
                           ("当前会话数据 (JSON)", "json")):
            menu.add_command(label=label,
                             command=lambda f=fmt: self.controller.export(f, scope="session"))
        mb.config(menu=menu)
        Tooltip(mb, "项目级 / 会话级报告导出：HTML（自包含可分享）· Markdown · JSON · CSV")
        return mb

    def _make_upload_button(self, parent) -> tk.Button:
        btn = tk.Button(parent, text="上传…", relief="flat", bg=theme.PANEL, fg=theme.FG,
                        padx=6, activebackground=theme.BG, activeforeground=theme.FG,
                        command=self.controller.show_upload)
        Tooltip(btn, "上传当前项目的效率报告到 TCER Web")
        return btn

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
        lines = [
            "【自动】按会话工具/产出信号推断类型（创作/维护/非编码），NTCER 更公平。",
        ]
        for cat_key, cat_info in metrics.TASK_CATEGORIES.items():
            display_name = self._task_display_names.get(cat_key, cat_key)
            lines.append(
                f"【{display_name}】系数 {cat_info['ttaf']}，TCER {cat_info['typical_tcer_range']}"
            )
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
        # All sources keep a count; bodies are lazy-loaded on popup open.
        has_user_msgs = report.usage.user_msgs > 0
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
        empty = [c for c in state.cells
                 if c.var.get() in ("-", UNSUPPORTED_LABEL)]
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
            state.expander.config(text=f"{arrow} {n_empty} 项无数据或不适用（点击{action}）")
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


# 图表组件已拆分至 charts.py；从这里 re-export 保持既有 import 路径可用。
from .charts import (  # noqa: F401
    DashboardChart, HeatmapChart, MetricTrendSelector, ScatterChart, TrendChart,
)

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


