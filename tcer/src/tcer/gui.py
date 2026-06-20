"""Tkinter GUI for TCER — browse projects, view metrics, and see per-session CTEI.

Pure standard library (tkinter). Launch with ``tcer gui`` or ``python -m tcer.gui``.
Analysis runs on a background thread so the UI stays responsive on large projects.
All user-facing text is Chinese, with hover tooltips + a glossary explaining each
metric in plain language.
"""
from __future__ import annotations

import queue
import threading
import traceback
import datetime as _dt
from pathlib import Path

from . import analyze, metrics
from .paths import list_projects
from .report import fmt_float, fmt_int, fmt_money, fmt_pct, models_label


def _fmt_dt(ms: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if ms is None:
        return "-"
    try:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return "-"


def _axis_label(key: str, v: float) -> str:
    """Format a y-axis tick for the trend chart by metric."""
    if key == "chr":
        return f"{v * 100:.0f}%"
    if key in ("cost", "cpe"):
        return f"${v:.1f}"
    if key == "ctei":
        return f"{v:.2f}"
    return f"{v:.0f}"

# CTEI grade → fill color for the chart and table.
GRADE_HEX = {
    "优秀": "#2e7d32",
    "良好": "#0277bd",
    "中等": "#f9a825",
    "低效": "#d84315",
    "极端低效": "#b71c1c",
}
_BG = "#1e1e1e"
_FG = "#e0e0e0"
_PANEL = "#252526"
_MUTED = "#9e9e9e"

# Summary cards: (key, 中文名, 单位, 说明). `key` matches the StringVar used by _render.
METRIC_CARDS = [
    ("TCER", "效率 TCER", "行/百万Token",
     "Token转码效率比 = 净增代码行 ÷ 百万Token。每花 100 万 Token 产出多少行净代码，越高越省。框架基准中位数 76.6。"),
    ("CTEI", "综合指数 CTEI", "",
     "复合 Token 效率指数：把效率、产出密度、成本、缓存合成的单一评分。>2 优秀 · 1~2 良好 · 0.5~1 中等 · 0.1~0.5 低效 · <0.1 极端低效。"),
    ("评级", "评级", "",
     "根据 CTEI 给出的效率等级，颜色与下方条形图一致。"),
    ("净LOC", "净增代码", "行",
     "所有会话净写入的代码行数（写入−删除），来自 Write/Edit/MultiEdit 工具调用，不依赖 git。"),
    ("churn", "返工率", "",
     "被删掉的代码 ÷ 写出的代码。越低说明返工越少、越接近“一次写对”。"),
    ("成本", "成本", "美元",
     "按各模型 list 价分别估算并求和的总花费（非订阅实际扣费）。"),
    ("CHR", "缓存命中率", "",
     "缓存读取 ÷ 总输入。越高越省钱——缓存读取单价仅为普通输入的 1/10。"),
    ("tokens", "Token 量", "百万",
     "总 Token 消耗（输入 + 输出 + 缓存）。"),
]

# Per-session table: (key, 中文表头, 宽度, 提示). The model column stretches to
# absorb leftover width (see _build); the rest are fixed.
TABLE_COLS = [
    ("session", "会话", 140, "会话 ID（前 18 位）。"),
    ("time", "开始时间", 96, "该会话的开始时间（首条 assistant 回复的时间戳，与下方时间趋势图横轴一致）。"),
    ("sub", "子代理", 52, "并入该会话的 subagent（子代理）数量；其 Token 与代码已计入本行。"),
    ("turns", "回合", 46, "计入统计的 assistant 回复条数。"),
    ("tokens", "Token", 78, "该会话总 Token 消耗（百万）。"),
    ("CHR", "缓存命中", 66, "缓存命中率：缓存读取 ÷ 总输入，越高越省。"),
    ("cost", "成本", 72, "按各模型 list 价估算的花费（美元）。"),
    ("netLOC", "净增行", 58, "该会话净写入代码行（写入−删除）。"),
    ("TCER", "效率", 52, "TCER：净增行 ÷ 百万 Token。"),
    ("CTEI", "综合", 52, "CTEI 综合效率评分。"),
    ("评级", "评级", 70, "依 CTEI 的等级（优秀/良好/中等/低效/极端低效）。"),
    ("model", "模型", 160, "该会话用到的模型（友好名）。多模型混用时逗号分隔；成本据此分模型计价。"),
]

# Extra glossary entries shown in the 指标说明 window (beyond the cards above).
GLOSSARY_EXTRA = [
    ("I/O 比", "总输入 ÷ 输出 Token。高 I/O = 上下文密集型任务（如代码审查），天然 TCER 偏低，是结构现象而非低效。"),
    ("CPE", "有效千行代码成本 = 成本 ÷ 净增行 × 1000（美元/千行）。可跨项目、跨模型对比。"),
    ("NCPI", "净代码产出指数 = 净增行 ÷ 代码库累计行。衡量本次对代码库的“贡献密度”，项目越成熟越趋近 0。"),
    ("CAF", "缓存调整因子 = 总输入 ÷（普通输入 + 缓存写入）。≥1，用于消除缓存对效率比较的干扰。"),
    ("TTAF / TA-TCER", "任务类型调整系数：新功能=1.0、功能扩展=0.85、调试=0.4、重构=0.5、审查=0.2、测试=0.9。TA-TCER = TCER ÷ TTAF，让不同任务在同一刻度对比。"),
    ("PSAC", "项目阶段调整系数。代码库越大、每次改动需注入的上下文越多，TCER 结构性下降；PSAC 用来抵消这种“大代码库维护税”。"),
    ("LOC 来源", "本工具不依赖 git：净增代码来自会话里 Write/Edit/MultiEdit 工具调用的逐条统计，按会话精确归因；代码库累计行来自扫描工作目录。"),
]


def _short_name(project_hash: str) -> str:
    """A friendlier label for a project-hash folder.

    Project-hash folders encode a full cwd with separators replaced by '-', so
    there's no reliable "project name" delimiter (path segments may contain '-').
    We only strip a leading drive token (e.g. ``c--``) and keep the rest intact.
    """
    for i in range(1, len(project_hash) - 2):
        if project_hash[i:i + 2] == "--":
            return project_hash[i + 2:]
    return project_hash


class _Tooltip:
    """Lightweight hover tooltip for a Tk widget (stdlib only)."""

    def __init__(self, tk, widget, text: str) -> None:
        self.tk = tk
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = self.tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        lbl = self.tk.Label(self.tip, text=self.text, justify="left", bg="#fff8e1",
                            fg="#222222", relief="solid", borderwidth=1,
                            wraplength=360, font=("Microsoft YaHei", 9), padx=8, pady=5)
        lbl.pack()

    def _hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class TcerGui:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self._result_q: queue.Queue = queue.Queue()
        self._projects: list[Path] = []
        self._current: analyze.ProjectAnalysis | None = None

        root.title("TCER — Token 转码效率计量")
        root.geometry("1180x780")
        root.configure(bg=_BG)
        self._init_style()
        self._build()
        self.load_projects()
        self.root.after(100, self._poll_results)

    # ----------------------------------------------------------------- build
    def _init_style(self) -> None:
        style = self.ttk.Style()
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("Treeview", background=_PANEL, fieldbackground=_PANEL,
                        foreground=_FG, rowheight=22)
        style.configure("Treeview.Heading", background="#333333", foreground=_FG,
                        relief="flat", borderwidth=1)
        # clam draws a raised (white-ish) border on heading hover/press — keep it dark & flat.
        style.map("Treeview", background=[("selected", "#094771")])
        style.map("Treeview.Heading",
                  background=[("active", "#3d3d3d"), ("pressed", "#2b2b2b")],
                  foreground=[("active", _FG)],
                  relief=[("active", "flat"), ("pressed", "flat")])

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk

        # Top control bar
        bar = tk.Frame(self.root, bg=_BG)
        bar.pack(side="top", fill="x", padx=8, pady=6)
        tk.Label(bar, text="任务类型:", bg=_BG, fg=_FG).pack(side="left")
        self.task_var = tk.StringVar(value="feature")
        task_cb = ttk.Combobox(bar, textvariable=self.task_var, width=12,
                               values=sorted(metrics.TTAF), state="readonly")
        task_cb.pack(side="left", padx=(4, 4))
        task_cb.bind("<<ComboboxSelected>>", lambda e: self._reanalyze())
        _Tooltip(tk, task_cb, "任务类型影响 TTAF / TA-TCER：调试、重构、审查等任务天然产出更少代码，"
                              "选对类型才能公平比较效率。")

        # Time filter (since / until)
        tk.Label(bar, text="时间:", bg=_BG, fg=_FG).pack(side="left", padx=(12, 4))
        self.since_var = tk.StringVar(value="")
        since_entry = tk.Entry(bar, textvariable=self.since_var, width=10, bg=_PANEL, fg=_FG,
                               insertbackground=_FG, relief="flat", highlightthickness=1,
                               highlightbackground="#3e3e42", highlightcolor="#007acc")
        since_entry.pack(side="left", padx=2)
        since_entry.bind("<Return>", lambda e: self._reanalyze())
        since_entry.bind("<FocusOut>", lambda e: self._reanalyze())
        _Tooltip(tk, since_entry, "开始日期（YYYY-MM-DD，留空=全部）。按回车或失焦后生效。")

        tk.Label(bar, text="至", bg=_BG, fg=_FG).pack(side="left", padx=2)
        self.until_var = tk.StringVar(value="")
        until_entry = tk.Entry(bar, textvariable=self.until_var, width=10, bg=_PANEL, fg=_FG,
                               insertbackground=_FG, relief="flat", highlightthickness=1,
                               highlightbackground="#3e3e42", highlightcolor="#007acc")
        until_entry.pack(side="left", padx=2)
        until_entry.bind("<Return>", lambda e: self._reanalyze())
        until_entry.bind("<FocusOut>", lambda e: self._reanalyze())
        _Tooltip(tk, until_entry, "结束日期（YYYY-MM-DD，留空=全部）。按回车或失焦后生效。")

        # Quick time filter buttons
        tk.Button(bar, text="本周", command=lambda: self._set_time_range("week"),
                  bg=_PANEL, fg=_FG, relief="flat", padx=4, pady=2).pack(side="left", padx=2)
        tk.Button(bar, text="本月", command=lambda: self._set_time_range("month"),
                  bg=_PANEL, fg=_FG, relief="flat", padx=4, pady=2).pack(side="left", padx=2)
        tk.Button(bar, text="全部", command=lambda: self._set_time_range("all"),
                  bg=_PANEL, fg=_FG, relief="flat", padx=4, pady=2).pack(side="left", padx=2)

        self.no_sub_var = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(bar, text="排除子代理", variable=self.no_sub_var, bg=_BG, fg=_FG,
                             selectcolor=_PANEL, activebackground=_BG, activeforeground=_FG,
                             command=self._reanalyze)
        chk.pack(side="left", padx=8)
        _Tooltip(tk, chk, "勾选后只统计主会话，排除 subagent（子代理）会话。")
        tk.Button(bar, text="刷新项目", command=self.load_projects).pack(side="left", padx=4)
        tk.Button(bar, text="指标说明", command=self._show_glossary).pack(side="left", padx=4)
        self.status = tk.Label(bar, text="就绪", bg=_BG, fg="#9cdcfe", anchor="e")
        self.status.pack(side="right")

        # Main split: project list | report panel
        paned = tk.PanedWindow(self.root, orient="horizontal", bg=_BG, sashwidth=4)
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        left = tk.Frame(paned, bg=_PANEL)
        tk.Label(left, text="项目（会话数）", bg=_PANEL, fg=_FG, anchor="w").pack(fill="x", padx=6, pady=2)
        self.proj_list = tk.Listbox(left, bg=_PANEL, fg=_FG, selectbackground="#094771",
                                    highlightthickness=0, borderwidth=0, exportselection=False,
                                    width=34, activestyle="none")
        self.proj_list.pack(fill="both", expand=True, padx=6, pady=4)
        self.proj_list.bind("<<ListboxSelect>>", lambda e: self._reanalyze())
        paned.add(left)

        right = tk.Frame(paned, bg=_BG)
        paned.add(right)

        # Summary cards
        self.summary = tk.Frame(right, bg=_BG)
        self.summary.pack(fill="x", pady=(0, 6))
        self._summary_vars: dict = {}
        for i, (key, name, unit, tip) in enumerate(METRIC_CARDS):
            cell = tk.Frame(self.summary, bg=_PANEL, padx=10, pady=6)
            cell.grid(row=0, column=i, sticky="nsew", padx=3)
            self.summary.grid_columnconfigure(i, weight=1)
            title = tk.Label(cell, text=name, bg=_PANEL, fg=_MUTED, font=("Microsoft YaHei", 8))
            title.pack(anchor="w")
            var = tk.StringVar(value="-")
            self._summary_vars[key] = var
            val = tk.Label(cell, textvariable=var, bg=_PANEL, fg=_FG, font=("Segoe UI", 13, "bold"))
            val.pack(anchor="w")
            unit_lbl = tk.Label(cell, text=unit or " ", bg=_PANEL, fg=_MUTED, font=("Microsoft YaHei", 7))
            unit_lbl.pack(anchor="w")
            full_tip = f"{name}\n{tip}"
            for w in (cell, title, val, unit_lbl):
                _Tooltip(tk, w, full_tip)

        # Per-session table
        keys = [c[0] for c in TABLE_COLS]
        self.tree = ttk.Treeview(right, columns=keys, show="headings", height=10)
        self._sort_col = "CTEI"  # default sort column
        self._sort_reverse = True  # default descending (high CTEI first)
        for key, label, w, tip in TABLE_COLS:
            self.tree.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            # Only the model column stretches; the rest stay fixed so model gets
            # the leftover width instead of every column shrinking evenly.
            self.tree.column(key, width=w, minwidth=40, anchor="w",
                             stretch=(key == "model"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", self._show_session_detail)
        tk.Label(right, text="提示：把鼠标移到上方卡片或点“指标说明”查看每个指标的含义。",
                 bg=_BG, fg=_MUTED, anchor="w", font=("Microsoft YaHei", 8)).pack(fill="x")

        # Chart with a mode selector (time trends + CTEI ranking)
        chart_bar = tk.Frame(right, bg=_BG)
        chart_bar.pack(fill="x", pady=(8, 0))
        tk.Label(chart_bar, text="图表:", bg=_BG, fg=_FG).pack(side="left")
        self.chart_var = tk.StringVar(value="TCER 时间趋势")
        chart_modes = ["TCER 时间趋势", "CTEI 时间趋势", "CPE 时间趋势",
                       "缓存命中 时间趋势", "成本 时间趋势", "CTEI 评级排名", "代码产出分布"]
        chart_cb = ttk.Combobox(chart_bar, textvariable=self.chart_var, width=16,
                                values=chart_modes, state="readonly")
        chart_cb.pack(side="left", padx=6)
        chart_cb.bind("<<ComboboxSelected>>", lambda e: self._draw_chart())
        tk.Label(chart_bar, text="（横轴为会话开始时间，按时间排序）", bg=_BG, fg=_MUTED,
                 font=("Microsoft YaHei", 8)).pack(side="left")

        self.canvas = tk.Canvas(right, bg=_PANEL, height=240, highlightthickness=0)
        self.canvas.pack(fill="both", expand=False, pady=4)
        self.canvas.bind("<Configure>", lambda e: self._draw_chart())

        # Grade legend
        legend = tk.Frame(right, bg=_BG)
        legend.pack(fill="x")
        tk.Label(legend, text="评级:", bg=_BG, fg=_MUTED, font=("Microsoft YaHei", 8)).pack(side="left")
        ranges = {"优秀": ">2", "良好": "1~2", "中等": "0.5~1", "低效": "0.1~0.5", "极端低效": "<0.1"}
        for g, color in GRADE_HEX.items():
            tk.Label(legend, text=f"■ {g} ({ranges[g]})", bg=_BG, fg=color,
                     font=("Microsoft YaHei", 8)).pack(side="left", padx=6)

    # --------------------------------------------------------------- actions
    def load_projects(self) -> None:
        self._projects = list_projects()
        self.proj_list.delete(0, "end")
        for d in self._projects:
            files = list(d.rglob("*.jsonl"))
            self.proj_list.insert("end", f"{_short_name(d.name)}  ({len(files)})")
        if self._projects:
            self.proj_list.selection_set(0)
            self._reanalyze()

    def _selected_project(self) -> Path | None:
        sel = self.proj_list.curselection()
        if not sel:
            return None
        return self._projects[sel[0]]

    def _reanalyze(self) -> None:
        proj = self._selected_project()
        if proj is None:
            return
        self.status.config(text=f"分析中… {_short_name(proj.name)}")
        since = self.since_var.get().strip() or None
        until = self.until_var.get().strip() or None
        args = dict(project=proj.name, no_subagents=self.no_sub_var.get(),
                    task_type=self.task_var.get(), since=since, until=until)
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _set_time_range(self, preset: str) -> None:
        """Set since/until based on preset ('week' / 'month' / 'all')."""
        from datetime import datetime, timedelta
        today = datetime.now()
        if preset == "week":
            # Monday of this week
            monday = today - timedelta(days=today.weekday())
            self.since_var.set(monday.strftime("%Y-%m-%d"))
            self.until_var.set("")
        elif preset == "month":
            # First day of this month
            first = today.replace(day=1)
            self.since_var.set(first.strftime("%Y-%m-%d"))
            self.until_var.set("")
        elif preset == "all":
            self.since_var.set("")
            self.until_var.set("")
        self._reanalyze()

    def _sort_by(self, col: str) -> None:
        """Sort table by column (toggle ascending/descending)."""
        if col == self._sort_col:
            # Same column → toggle direction
            self._sort_reverse = not self._sort_reverse
        else:
            # New column → default to descending for numeric, ascending for text
            self._sort_col = col
            self._sort_reverse = col not in ("session", "time", "model", "评级")
        if self._current:
            self._render(self._current)

    def _worker(self, args: dict) -> None:
        try:
            result = analyze.analyze_project(**args)
            self._result_q.put(("ok", result))
        except Exception as e:  # noqa: BLE001 — surface any failure in the UI
            self._result_q.put(("err", f"{e}\n{traceback.format_exc()}"))

    def _poll_results(self) -> None:
        try:
            while True:
                kind, payload = self._result_q.get_nowait()
                if kind == "ok":
                    self._current = payload
                    self._render(payload)
                    self.status.config(text=f"完成 · 共 {payload.n_sessions} 个会话")
                else:
                    self.status.config(text="出错（见表格区）")
                    self._show_error(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_results)

    # --------------------------------------------------------------- render
    def _render(self, a: analyze.ProjectAnalysis) -> None:
        agg = a.aggregate
        sv = self._summary_vars
        sv["TCER"].set(fmt_float(agg.tcer, "0.0"))
        sv["CTEI"].set(fmt_float(agg.ctei, "0.00"))
        sv["评级"].set(agg.grade or "-")
        sv["净LOC"].set(fmt_int(agg.net_loc))
        sv["churn"].set(fmt_pct(agg.churn_ratio))
        sv["成本"].set(fmt_money(agg.cost))
        sv["CHR"].set(fmt_pct(agg.chr))
        sv["tokens"].set(f"{agg.usage.total / 1e6:.1f}")

        self.tree.delete(*self.tree.get_children())
        for tag, color in GRADE_HEX.items():
            self.tree.tag_configure(tag, foreground=color)

        # Sort by current column and direction
        def sort_key(r):
            col = self._sort_col
            if col == "session":
                return r.meta.session_id or ""
            if col == "time":
                return r.usage.started_at or 0
            if col == "sub":
                return r.subagent_count or 0
            if col == "turns":
                return r.usage.assistant_msgs
            if col == "tokens":
                return r.usage.total
            if col == "CHR":
                return r.chr or -1
            if col == "cost":
                return r.cost or -1
            if col == "netLOC":
                return r.net_loc or -1
            if col == "TCER":
                return r.tcer or -1
            if col == "CTEI":
                return r.ctei or -1
            if col == "评级":
                return r.grade or "zzz"  # sort None last
            if col == "model":
                return ", ".join(sorted(r.usage.models)) if r.usage.models else ""
            return 0

        sorted_reports = sorted(a.reports, key=sort_key, reverse=self._sort_reverse)

        for r in sorted_reports:
            sid = (r.meta.session_id or r.meta.path.stem)[:18]
            row = (
                sid,
                _fmt_dt(r.usage.started_at, "%m-%d %H:%M"),
                str(r.subagent_count) if r.subagent_count else "",
                fmt_int(r.usage.assistant_msgs),
                f"{r.usage.total/1e6:.2f}M",
                fmt_pct(r.chr),
                fmt_money(r.cost),
                fmt_int(r.net_loc),
                fmt_float(r.tcer, "0.0"),
                fmt_float(r.ctei, "0.00"),
                r.grade or "",
                models_label(r.usage),
            )
            self.tree.insert("", "end", values=row, tags=((r.grade,) if r.grade else ()))
        self._draw_chart()

    def _show_error(self, msg: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", values=(msg[:120],) + ("",) * (len(TABLE_COLS) - 1))

    def _draw_chart(self) -> None:
        cv = self.canvas
        cv.delete("all")
        if not self._current:
            return
        mode = self.chart_var.get()
        if mode == "CTEI 评级排名":
            self._draw_bars()
            return
        if mode == "代码产出分布":
            self._draw_loc_dist()
            return
        key = {"TCER 时间趋势": "tcer", "CTEI 时间趋势": "ctei", "CPE 时间趋势": "cpe",
               "缓存命中 时间趋势": "chr", "成本 时间趋势": "cost"}.get(mode, "tcer")
        self._draw_trend(key, mode)

    def _draw_bars(self) -> None:
        cv = self.canvas
        scored = [r for r in self._current.reports if r.ctei is not None]
        if not scored:
            cv.create_text(12, 12, anchor="nw", fill=_MUTED,
                           text="暂无单会话 CTEI（这些会话没有可测的净代码，或已关闭 LOC 统计）",
                           font=("Microsoft YaHei", 9))
            return
        scored.sort(key=lambda r: r.ctei, reverse=True)
        w = cv.winfo_width()
        h = cv.winfo_height()
        if w <= 1:
            w = 900
        if h <= 1:
            h = 240
        top = max(r.ctei for r in scored) or 1.0
        pad_l, pad_r, row_h = 150, 60, 22
        max_bar = max(40, w - pad_l - pad_r)
        for i, r in enumerate(scored):
            y = 8 + i * row_h
            if y + row_h > h:
                break
            sid = (r.meta.session_id or r.meta.path.stem)[:16]
            cv.create_text(8, y + row_h / 2, anchor="w", fill=_FG, text=sid, font=("Consolas", 8))
            bar = max(2, int(r.ctei / top * max_bar))
            color = GRADE_HEX.get(r.grade or "", "#666666")
            cv.create_rectangle(pad_l, y + 3, pad_l + bar, y + row_h - 3, fill=color, width=0)
            cv.create_text(pad_l + bar + 6, y + row_h / 2, anchor="w", fill=_FG,
                           text=f"{r.ctei:.3f} {r.grade or ''}", font=("Microsoft YaHei", 8))

    def _draw_loc_dist(self) -> None:
        """Draw code output distribution (stacked bars by session, sorted by time)."""
        cv = self.canvas
        reports = [r for r in self._current.reports
                   if r.net_loc and r.net_loc > 0 and r.usage.started_at is not None]
        if not reports:
            cv.create_text(12, 12, anchor="nw", fill=_MUTED,
                           text="无净代码产出（这些会话未写入代码，或已关闭 LOC 统计）",
                           font=("Microsoft YaHei", 9))
            return
        reports.sort(key=lambda r: r.usage.started_at)  # time order
        w = cv.winfo_width() if cv.winfo_width() > 1 else 900
        h = cv.winfo_height() if cv.winfo_height() > 1 else 240
        ml, mr, mt, mb = 60, 18, 22, 38
        x0, y0, x1, y1 = ml, mt, w - mr, h - mb

        total_loc = sum(r.net_loc for r in reports)
        # Draw stacked bars (one per session, width proportional to LOC)
        x = x0
        bar_width = x1 - x0
        for r in reports:
            frac = r.net_loc / total_loc
            seg_w = bar_width * frac
            color = GRADE_HEX.get(r.grade or "", "#666666")
            cv.create_rectangle(x, y0, x + seg_w, y1, fill=color, width=0)
            # Label if wide enough
            if seg_w > 30:
                sid = (r.meta.session_id or r.meta.path.stem)[:8]
                cv.create_text(x + seg_w / 2, (y0 + y1) / 2, text=sid, fill="#ffffff",
                               font=("Consolas", 8), angle=90)
            x += seg_w

        # Y-axis label
        cv.create_text(8, (y0 + y1) / 2, text="代码行", fill=_MUTED, angle=90,
                       font=("Microsoft YaHei", 9))
        # Total label
        cv.create_text((x0 + x1) / 2, y1 + 18, text=f"总净增：{total_loc:,} 行（{len(reports)} 个会话）",
                       fill=_FG, font=("Microsoft YaHei", 9))

    def _draw_trend(self, key: str, title: str) -> None:
        cv = self.canvas
        pts = [(r.usage.started_at, getattr(r, key), r.grade)
               for r in self._current.reports
               if r.usage.started_at is not None and getattr(r, key) is not None]
        pts.sort(key=lambda p: p[0])
        if not pts:
            cv.create_text(12, 12, anchor="nw", fill=_MUTED,
                           text="无足够的带时间数据点（该指标在这些会话上不可用）",
                           font=("Microsoft YaHei", 9))
            return
        w = cv.winfo_width() if cv.winfo_width() > 1 else 900
        h = cv.winfo_height() if cv.winfo_height() > 1 else 240
        ml, mr, mt, mb = 60, 18, 22, 38
        x0, y0, x1, y1 = ml, mt, w - mr, h - mb

        vals = [v for _, v, _ in pts]
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin:
            vmax = vmin + (abs(vmin) or 1)
        span = vmax - vmin
        vmin -= span * 0.12
        vmax += span * 0.12
        tmin = pts[0][0]
        tmax = pts[-1][0]
        tspan = (tmax - tmin) or 1

        def sx(t):
            return x0 + (x1 - x0) * ((t - tmin) / tspan) if len(pts) > 1 else (x0 + x1) / 2

        def sy(v):
            return y1 - (y1 - y0) * ((v - vmin) / (vmax - vmin))

        # axes
        cv.create_line(x0, y0, x0, y1, fill="#555")
        cv.create_line(x0, y1, x1, y1, fill="#555")
        cv.create_text(x0, y0 - 6, anchor="sw", fill=_FG, text=title, font=("Microsoft YaHei", 9))
        # y gridlines + labels
        for frac in (0.0, 0.5, 1.0):
            v = vmin + (vmax - vmin) * frac
            yy = sy(v)
            cv.create_line(x0, yy, x1, yy, fill="#3a3a3a")
            cv.create_text(x0 - 6, yy, anchor="e", fill=_MUTED, text=_axis_label(key, v),
                           font=("Segoe UI", 8))
        # TCER baseline reference
        if key == "tcer" and vmin < metrics.TCER_BASELINE < vmax:
            yb = sy(metrics.TCER_BASELINE)
            cv.create_line(x0, yb, x1, yb, fill="#888", dash=(4, 3))
            cv.create_text(x1, yb - 2, anchor="se", fill="#aaaaaa",
                           text=f"框架基准 {metrics.TCER_BASELINE}", font=("Microsoft YaHei", 7))
        # polyline
        if len(pts) > 1:
            coords = []
            for t, v, _ in pts:
                coords += [sx(t), sy(v)]
            cv.create_line(*coords, fill="#4fc3f7", width=2)
        # points (colored by grade) + sparse date labels
        n = len(pts)
        for i, (t, v, grade) in enumerate(pts):
            xx, yy = sx(t), sy(v)
            color = GRADE_HEX.get(grade or "", "#4fc3f7")
            cv.create_oval(xx - 4, yy - 4, xx + 4, yy + 4, fill=color, outline="#ffffff", width=1)
            if i == 0 or i == n - 1 or (n > 4 and i == n // 2):
                cv.create_text(xx, y1 + 4, anchor="n", fill=_MUTED,
                               text=_fmt_dt(t, "%m-%d"), font=("Segoe UI", 7))
        cv.create_text(x1, y1 + 4, anchor="ne", fill=_MUTED,
                       text=f"{_fmt_dt(tmin, '%Y-%m-%d')} → {_fmt_dt(tmax, '%Y-%m-%d')}",
                       font=("Segoe UI", 7))

    # --------------------------------------------------------------- glossary
    def _show_session_detail(self, event=None) -> None:
        """Show detailed metrics for the selected session in a popup."""
        sel = self.tree.selection()
        if not sel or not self._current:
            return
        item = sel[0]
        sid_short = self.tree.item(item, "values")[0]
        # Find the full report
        report = None
        for r in self._current.reports:
            if (r.meta.session_id or r.meta.path.stem).startswith(sid_short):
                report = r
                break
        if not report:
            return

        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(f"会话详情 · {sid_short}")
        win.geometry("580x680")
        win.configure(bg=_BG)

        txt = tk.Text(win, wrap="word", bg=_PANEL, fg=_FG, font=("Consolas", 10),
                      insertbackground=_FG, padx=12, pady=12, relief="flat",
                      highlightthickness=0)
        txt.pack(fill="both", expand=True, padx=8, pady=8)

        r = report
        u = r.usage
        lines = [
            f"会话 ID: {r.meta.session_id or '(无)'}",
            f"路径: {r.meta.path}",
            f"工作目录: {r.meta.cwd or '(未知)'}",
            f"标题: {r.meta.title or '(无标题)'}",
            "",
            "=== 时间 ===",
            f"开始: {_fmt_dt(u.started_at, '%Y-%m-%d %H:%M:%S')}",
            f"结束: {_fmt_dt(u.ended_at, '%Y-%m-%d %H:%M:%S')}",
            "",
            "=== Token 用量（L1 原始层）===",
            f"输入 (非缓存): {u.input_tokens:,}",
            f"缓存写入: {u.cache_creation_input_tokens:,}",
            f"缓存读取: {u.cache_read_input_tokens:,}",
            f"输出: {u.output_tokens:,}",
            f"总计: {u.total:,} ({u.total / 1e6:.2f} M)",
            f"助手回合数: {u.assistant_msgs}",
            f"零 usage 跳过: {u.empty_usage_skipped}",
            "",
            "=== 效率层（L2）===",
            f"TCER: {fmt_float(r.tcer, '0.00')} LOC/Mt",
            f"CHR (缓存命中率): {fmt_pct(r.chr)}",
            f"I/O Ratio: {fmt_float(r.io_ratio, '0.0')}",
            "",
            "=== 质量层（L3）===",
            f"净增行: {fmt_int(r.net_loc)}",
            f"  写入: {fmt_int(r.loc_stat.added) if r.loc_stat else 'N/A'}",
            f"  删除: {fmt_int(r.loc_stat.deleted) if r.loc_stat else 'N/A'}",
            f"Churn 率: {fmt_pct(r.churn_ratio)}",
            f"未见文件的 Write: {r.unseen_writes}",
            "",
            "=== 经济层（L4）===",
            f"成本 (list 价): {fmt_money(r.cost)}",
            f"$/Mt: {fmt_float(r.cost_per_mt, '0.00')}",
            f"CPE ($/千行): {fmt_money(r.cpe)}",
            "",
            "=== 综合层（L5）===",
            f"任务类型: {r.task_type or '(未设)'}",
            f"TTAF 系数: {fmt_float(r.ttaf, '0.00')}",
            f"TA-TCER: {fmt_float(r.ta_tcer, '0.00')}",
            f"NCPI: {fmt_float(r.ncpi, '0.000')}",
            f"CAF: {fmt_float(r.caf, '0.00')}",
            f"PSAC: {fmt_float(r.psac, '0.000')}",
            f"TCER (阶段调整后): {fmt_float(r.tcer_phase_adj, '0.00')}",
            f"CTEI: {fmt_float(r.ctei, '0.000')}",
            f"评级: {r.grade or '(无)'}",
            "",
            "=== 模型 ===",
        ]
        for m in sorted(u.models):
            lines.append(f"  {m}")
        if u.per_model:
            lines.append("")
            lines.append("逐模型成本:")
            from . import metrics
            for m, bucket_u in u.per_model.items():
                cost = metrics.cost_usd(bucket_u, model=m)
                lines.append(f"  {m or '(未记录)'}: {fmt_money(cost)}")

        txt.insert("end", "\n".join(lines))
        txt.config(state="disabled")

    def _show_glossary(self) -> None:
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("指标说明")
        win.geometry("560x620")
        win.configure(bg=_BG)
        txt = tk.Text(win, bg=_PANEL, fg=_FG, wrap="word", font=("Microsoft YaHei", 10),
                      padx=12, pady=10, borderwidth=0)
        txt.pack(fill="both", expand=True)
        txt.tag_configure("h", foreground="#9cdcfe", font=("Microsoft YaHei", 11, "bold"))
        txt.insert("end", "TCER 指标速查\n\n", "h")
        for _key, name, unit, tip in METRIC_CARDS:
            head = f"{name}" + (f"（{unit}）" if unit else "")
            txt.insert("end", head + "\n", "h")
            txt.insert("end", tip + "\n\n")
        txt.insert("end", "其他指标\n\n", "h")
        for name, tip in GLOSSARY_EXTRA:
            txt.insert("end", name + "\n", "h")
            txt.insert("end", tip + "\n\n")
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy).pack(pady=6)


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("error: tkinter is not available in this Python build.")
        return 1
    root = tk.Tk()
    TcerGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
