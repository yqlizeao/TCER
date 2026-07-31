"""分析类弹窗：会话对比 / 会话时间线 / 工具序列 / 项目总览。

从 popups.py 拆出（popups 通过 re-export 保持旧 import 路径）。与 popups 一样
从 metric_defs SSOT 取值渲染。
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from tcer.core import format as fmt
from . import theme
from .metric_defs import GROUPS
from .widgets import ScrollFrame, Tooltip, flat_button, new_window as _new_window


class SessionComparePopup:
    """会话对比 — 选 2~3 个会话并排对比全部指标（六组），金色 = 该行最优。

    行/值/提示/好坏方向全部来自 metric_defs SSOT，与指标分类页逐字节一致；
    全为「-」或「不适用」的行自动隐藏。
    """

    _COL_COLORS = ["#569cd6", "#4ec9b0", "#dcdcaa"]
    _NONE = "（不选）"

    def __init__(self, parent, reports, preselect_sid: str | None = None) -> None:
        from .metric_defs import UNSUPPORTED_LABEL, display, raw_value
        self._display = display
        self._raw = raw_value
        self._unsupported = UNSUPPORTED_LABEL
        self._reports = list(reports)

        # 唯一化的会话下拉标签：时间 · 标题（重名追加序号）
        self._labels: list[str] = []
        seen: dict[str, int] = {}
        for r in self._reports:
            sid = r.meta.session_id or r.meta.path.stem
            base = (f"{fmt.fmt_dt(r.usage.started_at, fmt.FMT_SHORT_MINUTE)}"
                    f" · {(r.meta.title or sid)[:26]}")
            n = seen.get(base, 0) + 1
            seen[base] = n
            self._labels.append(base if n == 1 else f"{base} ({n})")
        self._by_label = dict(zip(self._labels, self._reports))

        win = _new_window(parent, "会话对比", "980x720")
        bar = tk.Frame(win, bg=theme.BG, padx=10, pady=8)
        bar.pack(fill="x")
        tk.Label(bar, text="会话对比", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(side="left", padx=(0, 12))

        # 默认选择：优先当前选中会话，其余按列表顺序补齐
        order = list(range(len(self._reports)))
        if preselect_sid:
            for i, r in enumerate(self._reports):
                if (r.meta.session_id or r.meta.path.stem) == preselect_sid:
                    order.remove(i)
                    order.insert(0, i)
                    break
        self._vars: list[tk.StringVar] = []
        for slot in range(3):
            default = (self._labels[order[slot]]
                       if slot < min(2, len(order)) else self._NONE)
            if slot == 2 and len(order) > 2:
                default = self._NONE  # 第三列默认留空，按需加选
            var = tk.StringVar(value=default)
            values = ([self._NONE] + self._labels) if slot == 2 else self._labels
            cb = ttk.Combobox(bar, textvariable=var, values=values,
                              state="readonly", width=32)
            cb.pack(side="left", padx=4)
            cb.bind("<<ComboboxSelected>>", lambda _e: self._render())
            self._vars.append(var)

        sf = ScrollFrame(win, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._container = sf.inner
        self._render()

    def _selected(self):
        picked = []
        for var in self._vars:
            r = self._by_label.get(var.get())
            if r is not None and all(r is not p for p in picked):
                picked.append(r)
        return picked

    def _render(self) -> None:
        for w in self._container.winfo_children():
            w.destroy()
        sel = self._selected()
        if len(sel) < 2:
            tk.Label(self._container, text="请选择至少两个不同的会话",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                     pady=30).pack()
            return
        for group in GROUPS:
            self._build_group(group, sel)

    def _build_group(self, group, sel) -> None:
        # 先算出该组有内容的行；整组皆空则不渲染
        rows = []
        for metric in group.metrics:
            vals = [self._display(r, metric.key) for r in sel]
            if all(v in ("-", self._unsupported) for v in vals):
                continue
            rows.append((metric, vals))
        if not rows:
            return

        header = tk.Frame(self._container, bg=theme.GROUP_COLORS[group.id],
                          padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text=f"▼ {group.name}",
                 bg=theme.GROUP_COLORS[group.id], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))
        tk.Label(grid, text="", bg=theme.PANEL, width=16).grid(row=0, column=0)
        for j, r in enumerate(sel):
            color = self._COL_COLORS[j % len(self._COL_COLORS)]
            title = (r.meta.title or r.meta.session_id or r.meta.path.stem)[:22]
            tk.Label(grid, text=title, bg=theme.PANEL, fg=color,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="e").grid(
                         row=0, column=j + 1, sticky="e", padx=2)

        for i, (metric, vals) in enumerate(rows):
            name_lbl = tk.Label(grid, text=metric.name, bg=theme.PANEL,
                                fg=theme.LEVEL_COLORS.get(metric.level, theme.FG),
                                font=theme.FONT_UI_SMALL, anchor="w")
            name_lbl.grid(row=i + 1, column=0, sticky="w")
            if metric.tip:
                Tooltip(name_lbl, f"{metric.name}\n{metric.tip}")

            # 金色最优（与模型对比页同一规则）：按好坏方向取最值，全并列不标
            row_colors: dict[int, str] = {}
            if metric.sentiment in ("up", "down"):
                valid = [(j, self._raw(r, metric.key)) for j, r in enumerate(sel)]
                valid = [(j, v) for j, v in valid if isinstance(v, (int, float))]
                distinct = {v for _, v in valid}
                if len(distinct) >= 2:
                    target = (max(distinct) if metric.sentiment == "up"
                              else min(distinct))
                    for j, v in valid:
                        if v == target:
                            row_colors[j] = theme.VALUE_BEST

            for j, val in enumerate(vals):
                fg = row_colors.get(j, theme.VALUE_NEUTRAL)
                if val == self._unsupported:
                    fg = theme.MUTED
                tk.Label(grid, text=val, bg=theme.PANEL, fg=fg,
                         font=theme.FONT_VALUE, anchor="e").grid(
                             row=i + 1, column=j + 1, sticky="e", padx=2)

        for j in range(len(sel) + 1):
            grid.grid_columnconfigure(j, weight=1)


class SessionTimelinePopup:
    """会话时间线 — 逐回合 token 堆叠条 + 权威耗时 + 错误标记。

    数据来自 TokenUsage.turn_stats（四源统一：Claude 逐响应、Codex 逐 token 步、
    Grok 逐 turn_completed、OpenCode 逐 step-finish）。耗时仅在源提供权威值时显示
    （Claude turn_duration / Codex task_complete / Grok apiDurationMs）。
    """

    _COLORS = {
        "input": "#569cd6", "cache_write": "#ce9178",
        "cache_read": "#4ec9b0", "output": "#dcdcaa",
    }
    _PAD_L = 56
    _PAD_R = 16
    _PAD_T = 34
    _PAD_B = 46

    def __init__(self, parent, report) -> None:
        from .charts import _ChartTooltip
        self._stats = list(report.usage.turn_stats)
        sid = (report.meta.session_id or report.meta.path.stem)[:16]
        win = _new_window(parent, f"会话时间线 · {sid}…", "900x520")

        head = tk.Frame(win, bg=theme.BG, padx=10, pady=6)
        head.pack(fill="x")
        tk.Label(head, text="会话时间线", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(side="left")
        n_dur = sum(1 for t in self._stats if t.duration_ms is not None)
        tk.Label(head,
                 text=f"{len(self._stats)} 回合 · {n_dur} 个有权威耗时 · "
                      "悬停查看明细",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
                     side="left", padx=10)
        # 图例
        legend = tk.Frame(head, bg=theme.BG)
        legend.pack(side="right")
        for label, key in (("输入", "input"), ("缓存写", "cache_write"),
                           ("缓存读", "cache_read"), ("输出", "output")):
            tk.Label(legend, text="■", bg=theme.BG, fg=self._COLORS[key],
                     font=theme.FONT_UI_SMALL).pack(side="left")
            tk.Label(legend, text=label, bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 6))

        self.canvas = tk.Canvas(win, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._tooltip = _ChartTooltip(self.canvas)
        self._bar_x: list[tuple[float, float, int]] = []  # (x0, x1, idx)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Destroy>", lambda e: self._tooltip.hide())

    def _draw(self) -> None:
        c = self.canvas
        if not c.winfo_exists():
            return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40 or not self._stats:
            return
        stats = self._stats
        plot_w = w - self._PAD_L - self._PAD_R
        plot_h = h - self._PAD_T - self._PAD_B
        n = len(stats)
        bw = max(2.0, min(28.0, plot_w / n * 0.8))
        step = plot_w / n
        max_tok = max((t.input_tokens + t.cache_write + t.cache_read
                       + t.output_tokens) for t in stats) or 1
        durs = [t.duration_ms for t in stats if t.duration_ms is not None]
        max_dur = max(durs) if durs else 0

        # Y 轴（token）
        c.create_text(self._PAD_L - 6, self._PAD_T, text=f"{max_tok:,}",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")
        c.create_text(self._PAD_L - 6, self._PAD_T + plot_h, text="0",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")
        c.create_line(self._PAD_L, self._PAD_T + plot_h,
                      w - self._PAD_R, self._PAD_T + plot_h, fill=theme.BORDER)

        self._bar_x = []
        base_y = self._PAD_T + plot_h
        for idx, t in enumerate(stats):
            x0 = self._PAD_L + idx * step + (step - bw) / 2
            x1 = x0 + bw
            self._bar_x.append((x0, x1, idx))
            y = base_y
            for key, val in (("input", t.input_tokens),
                             ("cache_write", t.cache_write),
                             ("cache_read", t.cache_read),
                             ("output", t.output_tokens)):
                if val <= 0:
                    continue
                seg = val / max_tok * plot_h
                c.create_rectangle(x0, y - seg, x1, y,
                                   fill=self._COLORS[key], outline="")
                y -= seg
            if t.errors:
                c.create_text((x0 + x1) / 2, y - 8, text="▾", fill=theme.ERROR,
                              font=theme.FONT_UI_SMALL_BOLD)

        # 权威耗时折线（次轴，归一到绘图高度）
        if max_dur > 0:
            pts = []
            for idx, t in enumerate(stats):
                if t.duration_ms is None:
                    continue
                x = self._PAD_L + idx * step + step / 2
                y = base_y - (t.duration_ms / max_dur) * plot_h * 0.9
                pts.append((x, y))
            for i in range(1, len(pts)):
                c.create_line(*pts[i - 1], *pts[i], fill="#c586c0", width=2)
            for x, y in pts:
                c.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#c586c0",
                              outline="")
            c.create_text(w - self._PAD_R, self._PAD_T - 12,
                          text=f"耗时（紫线，峰值 {fmt.fmt_duration_ms(max_dur, short=True)}）",
                          fill="#c586c0", font=theme.FONT_UI_SMALL, anchor="e")

        # X 轴回合刻度（稀疏）
        tick_every = max(1, n // 12)
        for idx in range(0, n, tick_every):
            x = self._PAD_L + idx * step + step / 2
            c.create_text(x, base_y + 12, text=str(idx + 1),
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # 混用多模型的会话：x 轴下方画逐回合模型色带 + 图例。
        distinct_models = sorted({t.model for t in stats if t.model})
        if len(distinct_models) > 1:
            from tcer.core.pricing import label as _model_label
            palette = ["#569cd6", "#4ec9b0", "#dcdcaa", "#ce9178",
                       "#c586c0", "#9cdcfe"]
            cmap = {m: palette[i % len(palette)]
                    for i, m in enumerate(distinct_models)}
            for idx, t in enumerate(stats):
                if not t.model:
                    continue
                x0 = self._PAD_L + idx * step + (step - bw) / 2
                c.create_rectangle(x0, base_y + 20, x0 + bw, base_y + 25,
                                   fill=cmap[t.model], outline="")
            lx = self._PAD_L
            for m in distinct_models:
                c.create_text(lx, base_y + 36, text="■", fill=cmap[m],
                              anchor="w", font=theme.FONT_UI_SMALL)
                name = _model_label(m)
                c.create_text(lx + 12, base_y + 36, text=name,
                              fill=theme.MUTED, anchor="w",
                              font=theme.FONT_UI_SMALL)
                lx += 12 + 7 * len(name) + 14
        else:
            c.create_text(self._PAD_L + plot_w / 2, h - 12, text="回合",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

    def _on_motion(self, event) -> None:
        idx = None
        for x0, x1, i in self._bar_x:
            if x0 - 2 <= event.x <= x1 + 2:
                idx = i
                break
        if idx is None:
            self._tooltip.hide()
            return
        t = self._stats[idx]
        # Codex 等源一个回合可能拆成多个 token 步：显示真实回合号，步序号补充。
        if t.turn != idx:
            lines = [f"回合 {t.turn + 1}（第 {idx + 1} 步）"]
        else:
            lines = [f"回合 {idx + 1}"]
        if t.ts:
            lines.append(fmt.fmt_dt(t.ts, fmt.FMT_SHORT_SECOND))
        lines.append(f"输入 {t.input_tokens:,} · 缓存写 {t.cache_write:,}")
        lines.append(f"缓存读 {t.cache_read:,} · 输出 {t.output_tokens:,}")
        if t.model:
            from tcer.core.pricing import label as _model_label
            lines.append(f"模型 {_model_label(t.model)}")
        if t.duration_ms is not None:
            lines.append(f"耗时 {fmt.fmt_duration_ms(t.duration_ms, short=True)}")
        if t.tool_calls:
            lines.append(f"工具调用 {t.tool_calls} 次")
        if t.errors:
            lines.append(f"⚠ 工具错误 {t.errors} 次")
        self._tooltip.show(event.x, event.y, lines)


class ProjectOverviewPopup:
    """项目总览 — 全部项目并排对比（点击表头排序）。

    聚合口径与主界面一致（同一 analyze_project 结果）；CTEI/评级为单会话
    指标不在此显示（见 CLAUDE.md 聚合层限制）。
    """

    _COLS = [
        ("source", "来源", 76, False),
        ("name", "项目", 240, False),
        ("sessions", "会话", 56, True),
        ("tokens", "总 Token", 110, True),
        ("cost", "成本", 90, True),
        ("net", "净增行", 80, True),
        ("tcer", "TCER", 76, True),
        ("chr", "缓存命中", 84, True),
        ("churn", "返工率", 72, True),
    ]

    def __init__(self, parent, rows) -> None:
        from .views import project_label, project_source_label

        win = _new_window(parent, "项目总览", "1000x560")
        self._sort_col = "cost"
        self._sort_desc = True

        total_cost = sum(a.aggregate.cost or 0 for _, a in rows)
        total_tok = sum(a.aggregate.usage.total for _, a in rows)
        total_net = sum(a.aggregate.net_loc or 0 for _, a in rows)
        head = tk.Frame(win, bg=theme.BG, padx=10, pady=8)
        head.pack(fill="x")
        tk.Label(head, text="项目总览", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(side="left")
        tk.Label(head,
                 text=f"{len(rows)} 个项目 · {total_tok:,} Token · "
                      f"{fmt.fmt_money(total_cost)} · 净增 {total_net:,} 行 · 点击表头排序",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
                     side="left", padx=12)
        from .views import ui_icon
        flat_button(head, "导出 HTML", self._export_html,
                    image=ui_icon(head, "export"), compound="left").pack(side="right")

        self._data = []
        for p, a in rows:
            agg = a.aggregate
            u = agg.usage
            self._data.append({
                "source": project_source_label(p),
                "name": project_label(p),
                "sessions": a.n_sessions,
                "tokens": u.total,
                "cost": agg.cost or 0.0,
                "net": agg.net_loc if agg.net_loc is not None else None,
                "tcer": agg.tcer,
                "chr": agg.chr,
                "churn": agg.churn_ratio,
            })

        frame = tk.Frame(win, bg=theme.BG)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = [c[0] for c in self._COLS]
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="none")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for key, label, width, _numeric in self._COLS:
            self.tree.heading(key, text=label,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width,
                             anchor="e" if key not in ("source", "name") else "w")
        self._render()

    _FMT = {
        "sessions": lambda v: str(v),
        "tokens": lambda v: f"{v:,}",
        "cost": lambda v: fmt.fmt_money(v),
        "net": lambda v: fmt.fmt_int(v),
        "tcer": lambda v: fmt.fmt_float(v, "0.0"),
        "chr": lambda v: fmt.fmt_pct(v),
        "churn": lambda v: fmt.fmt_pct(v),
    }

    def _export_html(self) -> None:
        from pathlib import Path as _Path
        from tkinter import filedialog, messagebox

        from .html_report import render_overview_html

        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML 文件", "*.html"), ("所有文件", "*.*")],
            initialfile="tcer-项目总览.html",
        )
        if not path:
            return
        try:
            _Path(path).write_text(render_overview_html(self._data), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("导出失败", str(e))

    def _sort_by(self, key: str) -> None:
        if self._sort_col == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = key
            self._sort_desc = True
        self._render()

    def _render(self) -> None:
        key = self._sort_col
        rows = sorted(
            self._data,
            key=lambda d: (d[key] is None,
                           d[key] if d[key] is not None else 0),
            reverse=self._sort_desc,
        )
        self.tree.delete(*self.tree.get_children())
        for d in rows:
            vals = []
            for ck, _label, _w, _num in self._COLS:
                v = d[ck]
                if v is None:
                    vals.append("-")
                elif ck in self._FMT:
                    vals.append(self._FMT[ck](v))
                else:
                    vals.append(str(v))
            self.tree.insert("", "end", values=vals)


class ToolSequencePopup:
    """工具序列 — 相邻工具调用的二元组频次 + 工作流模式信号。

    数据来自 tool_ops（按文件顺序）。绿色 = 健康模式（先读/搜后改），
    红色 = 风险模式（Edit→Edit 盲改连击、Bash→Bash 试错循环）。
    """

    _GOOD = {("Read", "Edit"), ("Read", "Write"), ("Grep", "Edit"),
             ("Grep", "Read"), ("Glob", "Read"), ("Edit", "Bash"),
             ("Write", "Bash"), ("MultiEdit", "Bash")}
    _BAD = {("Edit", "Edit"), ("Write", "Write"), ("Bash", "Bash"),
            ("Edit", "Write"), ("Write", "Edit")}

    def __init__(self, parent, usage, suffix: str = "") -> None:
        ops = list(usage.tool_ops)
        win = _new_window(parent, f"工具序列{suffix}", "640x620")
        tk.Label(win, text="工具序列（相邻调用转移）", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=8).pack()

        if len(ops) < 2:
            tk.Label(win, text="工具调用不足，无法分析序列。", bg=theme.BG,
                     fg=theme.MUTED, font=theme.FONT_UI, pady=30).pack()
            return

        bigrams: dict[tuple[str, str], int] = {}
        for a, b in zip(ops, ops[1:]):
            key = (a.tool, b.tool)
            bigrams[key] = bigrams.get(key, 0) + 1

        # 模式信号摘要
        n_pairs = len(ops) - 1
        edit_edit = sum(c for (a, b), c in bigrams.items()
                        if (a, b) in (("Edit", "Edit"), ("Write", "Write")))
        bash_bash = bigrams.get(("Bash", "Bash"), 0)
        read_edit = sum(c for (a, b), c in bigrams.items()
                        if a in ("Read", "Grep", "Glob") and b in ("Edit", "Write", "MultiEdit"))
        edit_verify = sum(c for (a, b), c in bigrams.items()
                          if a in ("Edit", "Write", "MultiEdit") and b in ("Bash", "PowerShell"))
        summary = tk.Frame(win, bg=theme.PANEL, padx=10, pady=6)
        summary.pack(fill="x", padx=10)
        for label, val, color in (
            ("读/搜→改（健康）", read_edit, theme.SUCCESS),
            ("改→验（健康）", edit_verify, theme.SUCCESS),
            ("盲改连击 Edit→Edit", edit_edit, theme.ERROR if edit_edit else theme.MUTED),
            ("试错循环 Bash→Bash", bash_bash, theme.WARNING if bash_bash else theme.MUTED),
        ):
            row = tk.Frame(summary, bg=theme.PANEL)
            row.pack(fill="x")
            tk.Label(row, text=label, bg=theme.PANEL, fg=theme.FG,
                     font=theme.FONT_UI, anchor="w").pack(side="left")
            tk.Label(row, text=f"{val}（{val / n_pairs * 100:.0f}%）",
                     bg=theme.PANEL, fg=color, font=theme.FONT_VALUE,
                     anchor="e").pack(side="right")

        # Top 转移条形
        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner
        top = sorted(bigrams.items(), key=lambda kv: -kv[1])[:20]
        max_c = top[0][1] if top else 1
        for (a, b), cnt in top:
            color = (theme.SUCCESS if (a, b) in self._GOOD
                     else theme.ERROR if (a, b) in self._BAD
                     else theme.ACCENT)
            row = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            row.pack(fill="x")
            tk.Label(row, text=f"{a} → {b}", bg=theme.PANEL, fg=theme.FG,
                     font=theme.FONT_MONO, width=28, anchor="w").pack(side="left")
            bar_bg = tk.Frame(row, bg=theme.CONTROL_BG, height=10, width=220)
            bar_bg.pack(side="left", fill="x", expand=True, padx=6)
            bar_bg.pack_propagate(False)
            tk.Frame(bar_bg, bg=color, height=10).place(
                relx=0, rely=0, relwidth=cnt / max_c, relheight=1.0)
            tk.Label(row, text=str(cnt), bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_MONO, width=6, anchor="e").pack(side="right")
        tk.Label(inner, text=f"共 {n_pairs} 次相邻转移 · 显示 Top {len(top)}",
                 bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                 pady=6).pack()
