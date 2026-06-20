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

# Per-session table: simplified to just session ID (all metrics moved to right panel)
TABLE_COLS = [
    ("session", "会话", 280, "会话 ID（完整）。点击查看该会话的详细五层指标。"),
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

        # Main layout: three columns (project | session | metrics)
        paned = tk.PanedWindow(self.root, orient="horizontal", bg=_BG, sashwidth=4)
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        # Left column: Project list
        left = tk.Frame(paned, bg=_PANEL, width=240)
        tk.Label(left, text="项目（会话数）", bg=_PANEL, fg=_FG, anchor="w").pack(fill="x", padx=6, pady=2)
        self.proj_list = tk.Listbox(left, bg=_PANEL, fg=_FG, selectbackground="#094771",
                                    highlightthickness=0, borderwidth=0, exportselection=False,
                                    width=28, activestyle="none")
        self.proj_list.pack(fill="both", expand=True, padx=6, pady=4)
        self.proj_list.bind("<<ListboxSelect>>", lambda e: self._reanalyze())
        paned.add(left, minsize=200)

        # Middle column: Session table
        middle = tk.Frame(paned, bg=_BG, width=700)
        paned.add(middle, minsize=500)

        tk.Label(middle, text="会话列表（点击查看详情）", bg=_BG, fg=_FG, anchor="w",
                 font=("Microsoft YaHei", 9, "bold")).pack(fill="x", padx=6, pady=(0, 4))

        keys = [c[0] for c in TABLE_COLS]
        self.tree = ttk.Treeview(middle, columns=keys, show="headings", height=20)
        self._sort_col = "session"
        self._sort_reverse = False  # Alphabetical ascending by default
        for key, label, w, tip in TABLE_COLS:
            self.tree.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            # Session column stretches to fill width
            self.tree.column(key, width=w, minwidth=200, anchor="w", stretch=True)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_session_select)

        # Right column: Metrics panel
        right = tk.Frame(paned, bg=_BG, width=500)
        paned.add(right, minsize=400)

        # View switcher (Project view / Session view)
        view_bar = tk.Frame(right, bg=_BG)
        view_bar.pack(fill="x", pady=(0, 8))

        self.view_mode = tk.StringVar(value="project")  # "project" or "session"

        tk.Label(view_bar, text="视图:", bg=_BG, fg=_FG, font=("Microsoft YaHei", 9, "bold")).pack(side="left", padx=(0, 8))

        proj_btn = tk.Radiobutton(view_bar, text="● 项目汇总", variable=self.view_mode, value="project",
                                  bg=_BG, fg=_FG, selectcolor=_BG, activebackground=_BG,
                                  activeforeground="#007acc", font=("Microsoft YaHei", 9),
                                  command=self._switch_view)
        proj_btn.pack(side="left", padx=4)

        sess_btn = tk.Radiobutton(view_bar, text="○ Session 详情", variable=self.view_mode, value="session",
                                  bg=_BG, fg=_FG, selectcolor=_BG, activebackground=_BG,
                                  activeforeground="#007acc", font=("Microsoft YaHei", 9),
                                  command=self._switch_view)
        sess_btn.pack(side="left", padx=4)

        # Five-layer metrics
        self.summary = tk.Frame(right, bg=_BG)
        self.summary.pack(fill="both", expand=True, pady=(0, 6))
        self._summary_vars: dict = {}
        self._build_five_layers(self.summary)  # Pass self.summary, not right

    # --------------------------------------------------------------- five layers

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

    def _build_five_layers(self, parent) -> None:
        """Build five-layer metric visualization (L1-L5)."""
        tk = self.tk

        # Layer colors (progressive from data to insight)
        layer_colors = {
            "L1": "#2d2d30",  # dark gray - raw data
            "L2": "#1e3a5f",  # dark blue - efficiency
            "L3": "#1e4d2b",  # dark green - quality
            "L4": "#5f3a1e",  # dark brown - economics
            "L5": "#4a1e5f",  # dark purple - synthesis
        }

        layers = [
            ("L1", "原始层", "Token 用量明细", [
                ("total_tokens", "总消耗", "百万", "总 Token 消耗（输入 + 输出 + 缓存）"),
                ("input", "输入", "K", "非缓存输入 Token（千）"),
                ("output", "输出", "K", "输出 Token（千）"),
                ("cache_write", "缓存创建", "K", "本次写入缓存的 Token（千）"),
                ("cache_read", "缓存命中", "K", "从缓存读取的 Token（千）"),
            ]),
            ("L2", "效率层", "Token 转化效率", [
                ("TCER", "TCER", "行/Mt", "净增代码行 ÷ 百万 Token"),
                ("CHR", "缓存命中率", "%", "缓存读取 ÷ 总输入"),
                ("io_ratio", "I/O 比", "", "总输入 ÷ 输出"),
                ("CAF", "缓存调整", "", "缓存调整因子（消除缓存对效率比较的影响）"),
            ]),
            ("L3", "质量层", "代码产出质量", [
                ("net_loc", "净增行", "行", "写入 − 删除"),
                ("added", "写入行", "行", "总写入代码行"),
                ("deleted", "删除行", "行", "总删除代码行"),
                ("churn", "返工率", "%", "删除 ÷ 写入（越低越好）"),
            ]),
            ("L4", "经济层", "成本分析", [
                ("cost", "总成本", "$", "按各模型 list 价分别估算并求和"),
                ("cost_per_mt", "$/Mt", "$/百万", "每百万 Token 的实付成本"),
                ("cpe", "CPE", "$/千行", "有效千行代码成本（成本 ÷ 净增行 × 1000）"),
            ]),
            ("L5", "综合层", "最终评分", [
                ("CTEI", "CTEI", "", "复合 Token 效率指数"),
                ("grade", "评级", "", "优秀/良好/中等/低效/极端低效"),
                ("TTAF", "任务类型", "", "feature/debug/refactor 等"),
                ("PSAC", "阶段调整", "", "项目阶段调整系数"),
            ]),
        ]

        for layer_id, layer_name, layer_desc, metrics in layers:
            # Layer header
            header = tk.Frame(parent, bg=layer_colors[layer_id], padx=8, pady=4)
            header.pack(fill="x", pady=(0, 1))

            label_text = f"▼ {layer_id} {layer_name} — {layer_desc}"
            label = tk.Label(header, text=label_text, bg=layer_colors[layer_id], fg=_FG,
                           font=("Microsoft YaHei", 9, "bold"), anchor="w")
            label.pack(side="left")

            # Metrics row
            metrics_frame = tk.Frame(parent, bg=_PANEL, padx=8, pady=6)
            metrics_frame.pack(fill="x", pady=(0, 2))

            for i, (key, name, unit, tip) in enumerate(metrics):
                cell = tk.Frame(metrics_frame, bg=_PANEL, padx=8, pady=4)
                cell.grid(row=0, column=i, sticky="nsew", padx=2)
                metrics_frame.grid_columnconfigure(i, weight=1)

                title = tk.Label(cell, text=name, bg=_PANEL, fg=_MUTED, font=("Microsoft YaHei", 8))
                title.pack(anchor="w")

                var = tk.StringVar(value="-")
                self._summary_vars[key] = var

                val = tk.Label(cell, textvariable=var, bg=_PANEL, fg=_FG, font=("Segoe UI", 12, "bold"))
                val.pack(anchor="w")

                unit_lbl = tk.Label(cell, text=unit or " ", bg=_PANEL, fg=_MUTED, font=("Microsoft YaHei", 7))
                unit_lbl.pack(anchor="w")

                full_tip = f"{name}\n{tip}"
                for w in (cell, title, val, unit_lbl):
                    _Tooltip(tk, w, full_tip)

    def _switch_view(self) -> None:
        """Switch between project view and session view."""
        mode = self.view_mode.get()
        if mode == "project":
            # Show project aggregate
            if self._current:
                self._render(self._current)
        else:
            # Show selected session (if any)
            sel = self.tree.selection()
            if sel and self._current:
                self._render_selected_session()
            else:
                # No session selected, clear metrics
                for var in self._summary_vars.values():
                    var.set("-")

    def _on_session_select(self, event=None) -> None:
        """Handle session selection in the middle column."""
        if self.view_mode.get() == "session":
            # In session view, update metrics for selected session
            self._render_selected_session()

    def _render_selected_session(self) -> None:
        """Render five-layer metrics for the currently selected session."""
        sel = self.tree.selection()
        if not sel or not self._current:
            return

        # Get session ID from table (now full ID, not truncated)
        item = sel[0]
        sid = self.tree.item(item, "values")[0]

        # Find the report
        report = None
        for r in self._current.reports:
            if (r.meta.session_id or r.meta.path.stem) == sid:
                report = r
                break

        if not report:
            return

        # Render session metrics (same logic as project aggregate, but for single session)
        sv = self._summary_vars
        r = report
        u = r.usage

        # L1 原始层
        sv["total_tokens"].set(f"{u.total / 1e6:.2f}")
        sv["input"].set(f"{u.input_tokens / 1e3:.1f}")
        sv["output"].set(f"{u.output_tokens / 1e3:.1f}")
        sv["cache_write"].set(f"{u.cache_creation_input_tokens / 1e3:.1f}")
        sv["cache_read"].set(f"{u.cache_read_input_tokens / 1e3:.1f}")

        # L2 效率层
        sv["TCER"].set(fmt_float(r.tcer, "0.0"))
        sv["CHR"].set(fmt_pct(r.chr))
        sv["io_ratio"].set(fmt_float(r.io_ratio, "0.1"))
        sv["CAF"].set(fmt_float(r.caf, "0.00"))

        # L3 质量层
        sv["net_loc"].set(fmt_int(r.net_loc))
        if r.loc_stat:
            sv["added"].set(fmt_int(r.loc_stat.added))
            sv["deleted"].set(fmt_int(r.loc_stat.deleted))
        else:
            sv["added"].set("-")
            sv["deleted"].set("-")
        sv["churn"].set(fmt_pct(r.churn_ratio))

        # L4 经济层
        sv["cost"].set(fmt_money(r.cost))
        sv["cost_per_mt"].set(f"{r.cost_per_mt:.2f}")
        sv["cpe"].set(fmt_money(r.cpe))

        # L5 综合层
        sv["CTEI"].set(fmt_float(r.ctei, "0.00"))
        sv["grade"].set(r.grade or "-")
        sv["TTAF"].set(r.task_type or "-")
        sv["PSAC"].set(fmt_float(r.psac, "0.000"))

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

        # L1 原始层（5 个指标）
        sv["total_tokens"].set(f"{agg.usage.total / 1e6:.2f}")
        sv["input"].set(f"{agg.usage.input_tokens / 1e3:.1f}")
        sv["output"].set(f"{agg.usage.output_tokens / 1e3:.1f}")
        sv["cache_write"].set(f"{agg.usage.cache_creation_input_tokens / 1e3:.1f}")
        sv["cache_read"].set(f"{agg.usage.cache_read_input_tokens / 1e3:.1f}")

        # L2 效率层（4 个指标）
        sv["TCER"].set(fmt_float(agg.tcer, "0.0"))
        sv["CHR"].set(fmt_pct(agg.chr))
        sv["io_ratio"].set(fmt_float(agg.io_ratio, "0.1"))
        sv["CAF"].set(fmt_float(agg.caf, "0.00"))

        # L3 质量层（4 个指标）
        sv["net_loc"].set(fmt_int(agg.net_loc))
        sv["added"].set(fmt_int(agg.code_added))
        sv["deleted"].set(fmt_int(agg.code_deleted))
        sv["churn"].set(fmt_pct(agg.churn_ratio))

        # L4 经济层（3 个指标）
        sv["cost"].set(fmt_money(agg.cost))
        sv["cost_per_mt"].set(f"{agg.cost_per_mt:.2f}")
        sv["cpe"].set(fmt_money(agg.cpe))

        # L5 综合层（4 个指标）
        sv["CTEI"].set(fmt_float(agg.ctei, "0.00"))
        sv["grade"].set(agg.grade or "-")
        sv["TTAF"].set(agg.task_type or "-")  # 显示任务类型而非 TTAF（TTAF 是 per-session）
        sv["PSAC"].set(fmt_float(agg.psac, "0.000"))

        self.tree.delete(*self.tree.get_children())
        for tag, color in GRADE_HEX.items():
            self.tree.tag_configure(tag, foreground=color)

        # Sort by session ID (only column now)
        sorted_reports = sorted(a.reports,
                                key=lambda r: r.meta.session_id or r.meta.path.stem,
                                reverse=self._sort_reverse)

        for r in sorted_reports:
            sid = r.meta.session_id or r.meta.path.stem
            # Only insert session ID (all other data shown in right panel)
            self.tree.insert("", "end", values=(sid,), tags=((r.grade,) if r.grade else ()))

    def _show_error(self, msg: str) -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", values=(msg[:120],) + ("",) * (len(TABLE_COLS) - 1))

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
