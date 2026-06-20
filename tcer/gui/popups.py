"""Dialog windows: glossary, session detail, tool calls, calibration, baselines.

Each popup is a ``Toplevel`` built on demand and owns no long-lived state. They
render from ``metric_defs`` / the analysis result so they never duplicate the
metric definitions.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tcer.core import format as fmt
from tcer.core import metrics
from . import theme
from .metric_defs import CONCEPT_NOTES, LAYERS
from .widgets import ScrollFrame


def _new_window(parent, title, size, bg=theme.BG) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry(size)
    win.configure(bg=bg)
    return win


class GlossaryPopup:
    """指标说明 — renders every metric's tip from metric_defs + concept notes."""

    def __init__(self, parent) -> None:
        win = _new_window(parent, "指标说明", "580x720")
        frame = tk.Frame(win, bg=theme.BG)
        frame.pack(fill="both", expand=True, padx=4, pady=4)
        scrollbar = tk.Scrollbar(frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        txt = tk.Text(frame, bg=theme.PANEL, fg=theme.FG, wrap="word",
                      font=theme.FONT_UI, padx=12, pady=10, borderwidth=0,
                      yscrollcommand=scrollbar.set)
        txt.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=txt.yview)
        txt.tag_configure("h", foreground="#9cdcfe", font=theme.FONT_HEADING)
        for level, color in theme.LEVEL_COLORS.items():
            txt.tag_configure(level, foreground=color, font=theme.FONT_UI_BOLD)

        label = {"basic": "白色=基础", "advanced": "蓝色=高级",
                 "compound": "橘黄=复合", "ultimate": "红色=终极"}
        txt.insert("end", "TCER 指标速查\n\n", "h")
        txt.insert("end", "颜色说明: ", "h")
        for level in ("ultimate", "compound", "advanced", "basic"):
            txt.insert("end", f"● {label[level]} ", level)
        txt.insert("end", "\n\n")

        for layer in LAYERS:
            txt.insert("end", f"{layer.id} {layer.name} — {layer.desc}\n", "h")
            for m in layer.metrics:
                txt.insert("end", f"{m.name}" + (f"（{m.unit}）" if m.unit else "") + "\n", m.level)
                txt.insert("end", m.tip + "\n\n")
        txt.insert("end", "补充说明\n\n", "h")
        for name, tip, level in CONCEPT_NOTES:
            txt.insert("end", name + "\n", level)
            txt.insert("end", tip + "\n\n")
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy, bg=theme.ACCENT, fg=theme.FG,
                  relief="flat", padx=20, pady=4).pack(pady=6)


class SessionDetailPopup:
    """会话详情 — metadata + per-model cost breakdown (the grid covers the rest)."""

    def __init__(self, parent, report) -> None:
        r = report
        u = r.usage
        sid = (r.meta.session_id or r.meta.path.stem)[:16]
        win = _new_window(parent, f"会话详情 · {sid}…", "560x520")
        body = tk.Frame(win, bg=theme.BG, padx=12, pady=12)
        body.pack(fill="both", expand=True)

        def line(k, v):
            row = tk.Frame(body, bg=theme.BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=k, bg=theme.BG, fg=theme.MUTED, width=12, anchor="w",
                     font=theme.FONT_UI).pack(side="left")
            tk.Label(row, text=str(v), bg=theme.BG, fg=theme.FG, anchor="w",
                     justify="left", font=theme.FONT_UI).pack(side="left", fill="x")

        line("会话 ID", r.meta.session_id or "(无)")
        line("标题", r.meta.title or "(无标题)")
        line("工作目录", r.meta.cwd or "(未知)")
        line("路径", r.meta.path)
        line("开始", fmt.fmt_dt(u.started_at, "%Y-%m-%d %H:%M:%S"))
        line("结束", fmt.fmt_dt(u.ended_at, "%Y-%m-%d %H:%M:%S"))
        line("模型", fmt.models_label(u))
        line("Token 总量", f"{u.total:,}")

        tk.Label(body, text="逐模型成本", bg=theme.BG, fg="#9cdcfe",
                 font=theme.FONT_UI_BOLD).pack(anchor="w", pady=(12, 4))
        if u.per_model:
            for m, bucket in sorted(u.per_model.items()):
                cost = metrics.cost_usd(bucket, model=m)
                row = tk.Frame(body, bg=theme.BG)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=m or "(未记录)", bg=theme.BG, fg=theme.FG, anchor="w",
                         font=theme.FONT_MONO).pack(side="left")
                tk.Label(row, text=fmt.fmt_money(cost), bg=theme.BG, fg=theme.MUTED, anchor="e",
                         font=theme.FONT_MONO).pack(side="right")
        else:
            tk.Label(body, text="(无逐模型分桶)", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_UI).pack(anchor="w")

        if r.unseen_writes:
            tk.Label(body,
                     text=f"⚠ {r.unseen_writes} 个「未见文件的 Write」（LOC 假设为新文件，"
                          "若覆写已有文件会高估 added）",
                     bg=theme.BG, fg=theme.WARNING, justify="left",
                     font=theme.FONT_UI).pack(anchor="w", pady=(12, 0))


class ToolCallsPopup:
    """工具调用统计 — per-tool call count with a proportional bar."""

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        win = _new_window(parent, f"工具调用统计{title_suffix}", "500x600")
        tk.Label(win, text="工具调用详情", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="Claude Code 在此会话中调用的工具及次数", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_UI, pady=5).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        tc = usage.tool_calls
        if not tc:
            tk.Label(inner, text="未调用任何工具", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
        else:
            total = sum(tc.values())
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=(0, 10))
            tk.Label(head, text=f"总计 {total} 次调用 · {len(tc)} 种工具", bg="#2a2a2e",
                     fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()
            for name, count in sorted(tc.items(), key=lambda x: x[1], reverse=True):
                pct = count / total * 100 if total else 0
                row = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=6)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                         font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
                tk.Label(row, text=f"{count} 次（{pct:.1f}%）", bg=theme.PANEL, fg=theme.MUTED,
                         anchor="e", font=theme.FONT_MONO).pack(side="right")
                bar = tk.Frame(inner, bg=theme.PANEL, height=4)
                bar.pack(fill="x", padx=8, pady=(0, 8))
                tk.Frame(bar, bg=theme.ACCENT, width=int(pct * 4.5), height=4).pack(side="left")


class ModelsPopup:
    """模型使用详情 — per-model token usage, cost, and percentage breakdown."""

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        from .. import metrics as metrics_mod
        from ..format import fmt_money
        from ..pricing import label as model_label

        win = _new_window(parent, f"模型使用详情{title_suffix}", "520x560")
        tk.Label(win, text="模型使用详情", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="各模型的 Token 用量、成本及占比", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_UI, pady=5).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        per_model = usage.per_model
        if not per_model:
            tk.Label(inner, text="无逐模型数据", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
        else:
            total_tokens = usage.total
            total_cost = metrics_mod.cost_usd(usage)

            # Summary header
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=(0, 10))
            tk.Label(head, text=f"总计 {total_tokens:,} Token · {fmt_money(total_cost)} · "
                                f"{len(per_model)} 个模型",
                     bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

            # Per-model rows sorted by token count descending
            items = []
            for model_id, mu in per_model.items():
                model_total = mu.input_tokens + mu.cache_creation_input_tokens + \
                              mu.cache_read_input_tokens + mu.output_tokens
                cost = metrics_mod.cost_usd(mu, model=model_id or None)
                items.append((model_id, model_total, cost))
            items.sort(key=lambda x: x[1], reverse=True)

            for model_id, tok, cost in items:
                pct = tok / total_tokens * 100 if total_tokens else 0
                name = model_label(model_id) if model_id else "(未记录)"
                row = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=6)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                         font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
                tk.Label(row, text=f"{tok:,} Token · {fmt_money(cost)}（{pct:.1f}%）",
                         bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                         font=theme.FONT_MONO).pack(side="right")
                bar = tk.Frame(inner, bg=theme.PANEL, height=4)
                bar.pack(fill="x", padx=8, pady=(0, 8))
                tk.Frame(bar, bg=theme.ACCENT, width=int(pct * 4.5), height=4).pack(side="left")


class CalibratePopup:
    """校准结果 — tcer LOC vs git ground truth, per session + summary."""

    def __init__(self, parent, calibrations, text_report: str) -> None:
        win = _new_window(parent, "LOC 精度校准（对照 git）", "720x560")
        tk.Label(win, text="工具调用 LOC vs git 真实净增", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=8).pack()

        cols = ("session", "tcer", "git", "net")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=14)
        for c, t, w in (("session", "会话", 320), ("tcer", "工具调用 ±", 130),
                        ("git", "git ±", 130), ("net", "净偏差", 110)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w" if c == "session" else "e")
        tree.pack(fill="both", expand=True, padx=10, pady=8)

        tot_tcer = tot_git = 0
        for cal in calibrations:
            tcer_net = cal.tcer_added - cal.tcer_deleted
            git_net = cal.git_added - cal.git_deleted
            tot_tcer += tcer_net
            tot_git += git_net
            tree.insert("", "end", values=(
                cal.session_id[:38],
                f"+{cal.tcer_added} -{cal.tcer_deleted}",
                f"+{cal.git_added} -{cal.git_deleted}",
                f"{cal.net_deviation:+d}",
            ))

        summary = tk.Frame(win, bg=theme.BG, padx=10, pady=6)
        summary.pack(fill="x")
        factor = (tot_tcer / tot_git) if tot_git else 0
        ratio = ((tot_tcer / tot_git - 1) * 100) if tot_git else 0
        for text in (f"工具调用净增: {tot_tcer:+,}",
                     f"git 净增: {tot_git:+,}",
                     f"净偏差: {tot_tcer - tot_git:+,}（{ratio:+.1f}%）",
                     f"校准系数: {factor:.4f}"):
            tk.Label(summary, text=text, bg=theme.BG, fg=theme.FG, anchor="w",
                     font=theme.FONT_MONO).pack(anchor="w")

        tk.Button(win, text="复制文本报告", command=lambda: _copy(win, text_report),
                  bg=theme.PANEL, fg=theme.FG, relief="flat", padx=12, pady=4).pack(side="left",
                                                                                   padx=(10, 4), pady=8)
        tk.Button(win, text="关闭", command=win.destroy, bg=theme.ACCENT, fg=theme.FG,
                  relief="flat", padx=20, pady=4).pack(side="left", pady=8)


class BaselinesPopup:
    """计算出的个人基准 + 应用按钮（写回 composite_baselines.json）。"""

    def __init__(self, parent, values: dict, n_sessions: int, on_apply) -> None:
        win = _new_window(parent, "计算个人基准", "420x320")
        tk.Label(win, text=f"基于 {n_sessions} 个会话计算的基准", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=12).pack()

        body = tk.Frame(win, bg=theme.BG, padx=16)
        body.pack(fill="both", expand=True)
        for k, v, method in (("tcer", values["tcer"], "中位数"),
                             ("ncpi", values["ncpi"], "均值"),
                             ("cpe", values["cpe"], "中位数")):
            row = tk.Frame(body, bg=theme.BG)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=f"TCER/{k.upper()}" if k != "tcer" else "TCER", bg=theme.BG,
                     fg=theme.MUTED, width=10, anchor="w", font=theme.FONT_UI).pack(side="left")
            tk.Label(row, text=f"{v:.3f}（{method}）", bg=theme.BG, fg=theme.FG, anchor="w",
                     font=theme.FONT_MONO).pack(side="left")

        tk.Label(win, text="应用后将写入配置并立即重算综合效率指数刻度。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI, pady=10).pack()
        bar = tk.Frame(win, bg=theme.BG)
        bar.pack(pady=8)
        tk.Button(bar, text="应用为基准", command=lambda: (on_apply(values), win.destroy()),
                  bg=theme.ACCENT, fg=theme.FG, relief="flat", padx=16, pady=4).pack(side="left", padx=4)
        tk.Button(bar, text="取消", command=win.destroy, bg=theme.PANEL, fg=theme.FG,
                  relief="flat", padx=16, pady=4).pack(side="left", padx=4)


class AdvancedPopup:
    """高级选项 — code-dir 覆盖 + 跳过 LOC。读取/写回控制器状态。"""

    def __init__(self, parent, code_dir: str, no_loc: bool, on_apply) -> None:
        win = _new_window(parent, "高级选项", "460x240")
        tk.Label(win, text="高级选项", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=12).pack()

        body = tk.Frame(win, bg=theme.BG, padx=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="工作目录（累计 LOC 扫描目录，留空=用会话 cwd）:",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI).pack(anchor="w", pady=(8, 2))
        code_var = tk.StringVar(value=code_dir)
        tk.Entry(body, textvariable=code_var, width=52, bg=theme.PANEL, fg=theme.FG,
                 insertbackground=theme.FG, relief="flat", highlightthickness=1,
                 highlightbackground="#3e3e42").pack(anchor="w")
        no_loc_var = tk.BooleanVar(value=no_loc)
        tk.Checkbutton(body, text="跳过 LOC（仅 Token 指标，不算 TCER/CPE/CTEI）",
                       variable=no_loc_var, bg=theme.BG, fg=theme.FG, selectcolor=theme.PANEL,
                       activebackground=theme.BG, activeforeground=theme.FG).pack(anchor="w", pady=12)

        tk.Button(win, text="应用并重算",
                  command=lambda: (on_apply(code_var.get().strip() or None, no_loc_var.get()), win.destroy()),
                  bg=theme.ACCENT, fg=theme.FG, relief="flat", padx=16, pady=4).pack(pady=8)


class HighChurnFilesPopup:
    """高频改动文件 — files edited ≥3 times in this session, with counts."""

    def __init__(self, parent, details: dict[str, int]) -> None:
        win = _new_window(parent, "高频改动文件", "520x420")
        tk.Label(win, text="高频改动文件（≥3 次）", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="这些文件被反复修改，可能存在需求不清或需要重构。"
                 "「次数」包含同一个文件的 Write + Edit + MultiEdit 调用。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI, wraplength=480,
                 justify="left", padx=16).pack(pady=(0, 8))

        cols = ("path", "count")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=16)
        tree.heading("path", text="文件路径")
        tree.heading("count", text="改动次数")
        tree.column("path", width=400, anchor="w")
        tree.column("count", width=80, anchor="e")
        tree.pack(fill="both", expand=True, padx=10, pady=8)
        for fp, cnt in details.items():
            # Show relative path if possible, otherwise full
            display = fp if len(fp) < 60 else "…" + fp[-57:]
            tree.insert("", "end", values=(display, cnt))

        summary = tk.Frame(win, bg=theme.BG, padx=10, pady=6)
        summary.pack(fill="x")
        total = sum(details.values())
        tk.Label(summary, text=f"共 {len(details)} 个高频文件，合计 {total} 次改动",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_MONO).pack(anchor="w")


def _copy(win, text: str) -> None:
    win.clipboard_clear()
    win.clipboard_append(text)
    # small transient confirmation
    toast = tk.Label(win, text="已复制到剪贴板", bg=theme.SUCCESS, fg="#000000",
                     font=theme.FONT_UI, padx=8, pady=2)
    toast.place(relx=0.5, rely=0.02, anchor="n")
    win.after(1200, toast.destroy)
