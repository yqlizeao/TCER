"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``CteiBarChart`` consumes the shared
``export.ctei_ranking`` helper.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tcer.core import metrics
from tcer.core.export import ctei_ranking
from tcer.core.format import fmt_dt
from . import theme
from .metric_defs import GROUPS, report_values
from .widgets import Card, MetricCell, ScrollFrame, Tooltip

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
        for idx, d in enumerate(projects):
            card = self._make_card(d, idx)
            self._cards.append(card)
        self.count_label.config(text=f"项目（{len(projects)}）")
        self.scroll.update_scroll()
        if self._cards:
            self._select(self._cards[0])

    def _make_card(self, project_dir, idx):
        card = Card(self.container, on_click=lambda c, i=idx: self._select(c, i),
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
        ordered = sorted(reports, key=lambda r: r.usage.ended_at or r.usage.started_at or 0,
                         reverse=True)
        for r in ordered:
            self._cards.append(self._make_card(r))
        self.count_label.config(text=f"会话（{len(ordered)}）")
        self.scroll.update_scroll()

    def _make_card(self, r):
        sid = r.meta.session_id or r.meta.path.stem
        title = r.meta.title or "(无标题)"
        card = Card(self.container, on_click=lambda c, s=sid: self._select(c, s))
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
            cell.var.set(vals.get(key, "-"))


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


# (label, SessionReport attr, baseline-or-None)
_TREND_METRICS = [
    ("TCER", "tcer", "TCER_BASELINE"),
    ("综合效率指数", "ctei", None),
    ("成本 (美元)", "cost", None),
    ("缓存命中率 (%)", "chr", None),
    ("千行代码成本", "cpe", None),
]


def _trend_value(report, attr):
    v = getattr(report, attr)
    if v is None:
        return None
    if attr == "chr":
        return v * 100.0
    return float(v)


class TrendChart:
    """Tab 3: a selected metric plotted across sessions over time."""

    def __init__(self, parent) -> None:
        self._reports: list = []
        self._idx = 0

        top = tk.Frame(parent, bg=theme.BG)
        top.pack(fill="x", padx=8, pady=4)
        tk.Label(top, text="趋势指标:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self._var = tk.StringVar()
        cb = ttk.Combobox(top, textvariable=self._var, width=16, state="readonly",
                          values=[name for name, _, _ in _TREND_METRICS])
        cb.current(0)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", self._on_metric_change)

        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())

    def _on_metric_change(self, _event=None) -> None:
        names = [n for n, _, _ in _TREND_METRICS]
        self._idx = names.index(self._var.get()) if self._var.get() in names else 0
        self._draw()

    def update(self, reports) -> None:
        self._reports = sorted(reports,
                               key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 2 or h < 2:
            return
        name, attr, baseline_key = _TREND_METRICS[self._idx]
        reports = [r for r in self._reports if _trend_value(r, attr) is not None]
        c.create_text(w / 2, 8, text=f"{name} 随会话时间变化",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")
        if len(reports) < 2:
            c.create_text(w / 2, h / 2, text="需要 ≥2 个有效会话才能绘制趋势",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            return

        vals = [_trend_value(r, attr) for r in reports]
        pad_l, pad_r, pad_t, pad_b = 56, 16, 28, 24
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        lo = min(vals)
        hi = max(vals)
        if baseline_key is not None:
            bl_raw = getattr(metrics, baseline_key, None)
            if bl_raw is not None:
                bl = bl_raw * (100.0 if attr == "chr" else 1.0)
                lo, hi = min(lo, bl), max(hi, bl)
        span = (hi - lo) or 1.0

        def x(i):
            return pad_l + (plot_w * i / (len(reports) - 1)) if len(reports) > 1 else pad_l + plot_w / 2

        def y(v):
            return pad_t + plot_h * (1 - (v - lo) / span)

        # baseline line
        if baseline_key is not None:
            yb = y(bl)
            c.create_line(pad_l, yb, pad_l + plot_w, yb, fill=theme.WARNING, dash=(4, 3))
            c.create_text(pad_l + plot_w, yb, text="基准", anchor="e",
                          fill=theme.WARNING, font=theme.FONT_UI_SMALL)

        # axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#3e3e42")
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill="#3e3e42")
        c.create_text(pad_l - 6, pad_t, text=f"{hi:g}", anchor="ne", fill=theme.MUTED,
                      font=theme.FONT_UI_SMALL)
        c.create_text(pad_l - 6, pad_t + plot_h, text=f"{lo:g}", anchor="ne",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # polyline + points
        pts = [(x(i), y(vals[i])) for i in range(len(reports))]
        if len(pts) >= 2:
            c.create_line(pts, fill=theme.ACCENT, width=2, smooth=True)
        for px, py in pts:
            c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=theme.ACCENT, outline=theme.FG)
