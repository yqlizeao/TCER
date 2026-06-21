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
from .metric_defs import CONCEPT_NOTES, GROUPS
from .widgets import ScrollFrame


def _new_window(parent, title, size, bg=theme.BG) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry(size)
    win.configure(bg=bg)
    return win


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
            from tcer.core.pricing import label as model_label
            for m, bucket in sorted(u.per_model.items()):
                if m in ("<synthetic>", ""):
                    continue
                cost = metrics.cost_usd(bucket, model=m)
                row = tk.Frame(body, bg=theme.BG)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=model_label(m) if m else "(未记录)", bg=theme.BG, fg=theme.FG,
                         anchor="w", font=theme.FONT_MONO).pack(side="left")
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
    """工具调用统计 — per-tool call count with stacked bar (success / error)."""

    _COLORS = {
        "success": theme.ACCENT,   # blue
        "error":   theme.ERROR,    # red
    }

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        win = _new_window(parent, f"工具调用统计{title_suffix}", "520x600")
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
            total_errs = usage.tool_errors
            # Summary header
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=10)
            summary = f"总计 {total} 次调用 · {len(tc)} 种工具"
            if total_errs:
                summary += f" · {total_errs} 次错误"
            tk.Label(head, text=summary, bg="#2a2a2e",
                     fg=theme.ERROR if total_errs else theme.SUCCESS,
                     font=theme.FONT_UI_BOLD).pack()

            for name, count in sorted(tc.items(), key=lambda x: x[1], reverse=True):
                pct = count / total * 100 if total else 0
                errs = usage.tool_errors_by_tool.get(name, 0)
                ok = count - errs

                # --- Header row: tool name + count ---
                tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")
                hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                hdr.pack(fill="x")
                tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                         font=theme.FONT_VALUE).pack(side="left")
                tk.Label(hdr, text=f"{count} 次（{pct:.1f}%）",
                         bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                         font=theme.FONT_MONO).pack(side="right")

                # --- Stacked bar (relwidth-based, resize-safe) ---
                bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                bar_frame.pack(fill="x")
                bar_bg = tk.Frame(bar_frame, bg="#333333", height=10)
                bar_bg.pack(fill="x")
                if count > 0:
                    if ok > 0:
                        tk.Frame(bar_bg, bg=self._COLORS["success"], height=10).place(
                            relx=0, rely=0, relwidth=ok / count, relheight=1.0)
                    if errs > 0:
                        tk.Frame(bar_bg, bg=self._COLORS["error"], height=10).place(
                            relx=ok / count, rely=0, relwidth=errs / count, relheight=1.0)

                # --- Detail line ---
                det = tk.Frame(inner, bg=theme.PANEL, padx=12, pady=4)
                det.pack(fill="x")
                tk.Label(det, text=f"成功 {ok} 次",
                         bg=theme.PANEL, fg=self._COLORS["success"],
                         font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)
                if errs:
                    tk.Label(det, text=f"错误 {errs} 次（{errs/count*100:.0f}%）",
                             bg=theme.PANEL, fg=self._COLORS["error"],
                             font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)


class ModelsPopup:
    """模型使用详情 — per-model token usage with 4-type color breakdown."""

    # Token type colors (stacked bar segments)
    _COLORS = {
        "input":          "#569cd6",  # blue
        "output":         "#4ec9b0",  # teal
        "cache_creation": "#dcdcaa",  # yellow
        "cache_read":     "#6a6a6a",  # gray
    }
    _LABELS = {
        "input":          "输入",
        "output":         "输出",
        "cache_creation": "缓存写入",
        "cache_read":     "缓存读取",
    }
    # Models to hide from the popup (ccswitch synthetic stubs, always zero usage)
    _SKIP_MODELS = {"<synthetic>"}

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        from tcer.core import metrics as metrics_mod
        from tcer.core.format import fmt_money
        from tcer.core.pricing import label as model_label

        win = _new_window(parent, f"模型使用详情{title_suffix}", "560x620")
        tk.Label(win, text="模型使用详情", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="各模型的 Token 用量、成本及四类 Token 构成", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_UI, pady=5).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        per_model = usage.per_model
        # Filter out synthetic / junk models
        per_model = {k: v for k, v in per_model.items()
                     if k not in self._SKIP_MODELS and k}

        if not per_model:
            tk.Label(inner, text="无逐模型数据", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
        else:
            total_tokens = sum(
                mu.input_tokens + mu.cache_creation_input_tokens +
                mu.cache_read_input_tokens + mu.output_tokens
                for mu in per_model.values()
            )
            total_cost = metrics_mod.cost_usd(usage)

            # Summary header
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=10)
            tk.Label(head, text=f"总计 {total_tokens:,} Token · {fmt_money(total_cost)} · "
                                f"{len(per_model)} 个模型",
                     bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

            # Per-model blocks sorted by token count descending
            items = []
            for model_id, mu in per_model.items():
                model_total = mu.input_tokens + mu.cache_creation_input_tokens + \
                              mu.cache_read_input_tokens + mu.output_tokens
                cost = metrics_mod.cost_usd(mu, model=model_id or None)
                items.append((model_id, mu, model_total, cost))
            items.sort(key=lambda x: x[2], reverse=True)

            for model_id, mu, tok, cost in items:
                pct = tok / total_tokens * 100 if total_tokens else 0
                name = model_label(model_id) if model_id else "(未记录)"

                # --- Header row: model name + total + cost ---
                tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")
                hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                hdr.pack(fill="x")
                tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                         font=theme.FONT_VALUE).pack(side="left")
                tk.Label(hdr, text=f"{tok:,} Token · {fmt_money(cost)}（{pct:.1f}%）",
                         bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                         font=theme.FONT_MONO).pack(side="right")

                # --- Stacked bar (relwidth-based, resize-safe) ---
                vals = [mu.input_tokens, mu.output_tokens,
                        mu.cache_creation_input_tokens, mu.cache_read_input_tokens]
                keys = ["input", "output", "cache_creation", "cache_read"]
                bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                bar_frame.pack(fill="x")
                bar_bg = tk.Frame(bar_frame, bg="#333333", height=10)
                bar_bg.pack(fill="x")
                if tok > 0:
                    relx = 0.0
                    for v, k in zip(vals, keys):
                        if v > 0:
                            rw = v / tok
                            seg = tk.Frame(bar_bg, bg=self._COLORS[k], height=10)
                            seg.place(relx=relx, rely=0, relwidth=rw, relheight=1.0)
                            relx += rw

                # --- Detail line: 4 types with color (compact) ---
                det = tk.Frame(inner, bg=theme.PANEL, padx=12, pady=4)
                det.pack(fill="x")
                for v, k in zip(vals, keys):
                    sub_pct = v / tok * 100 if tok else 0
                    # Abbreviate cache_read to save horizontal space
                    label_text = f"{self._LABELS[k]} {v:,}（{sub_pct:.0f}%）"
                    lbl = tk.Label(det, text=label_text,
                                   bg=theme.PANEL, fg=self._COLORS[k],
                                   font=(theme.FONT_MONO_NAME, 8), anchor="w")
                    lbl.pack(side="left", padx=8)


class CostBreakdownPopup:
    """成本明细 — per-model cost sorted by cost, with cost-effectiveness metric."""

    _COLOR = "#ce9178"  # warm orange for cost bars

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        from tcer.core import metrics as metrics_mod
        from tcer.core.format import fmt_money
        from tcer.core.pricing import label as model_label

        win = _new_window(parent, f"成本明细{title_suffix}", "560x560")
        tk.Label(win, text="成本明细", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="各模型成本、Token 效率（每美元 Token 数）", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_UI, pady=5).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        per_model = usage.per_model
        _SKIP = {"<synthetic>", ""}
        per_model = {k: v for k, v in per_model.items() if k not in _SKIP and k}

        if not per_model:
            tk.Label(inner, text="无逐模型数据", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
            return

        total_cost = metrics_mod.cost_usd(usage)

        # Build items: (model_id, cost, total_tokens, tokens_per_dollar)
        items = []
        for model_id, mu in per_model.items():
            tok = mu.input_tokens + mu.output_tokens + mu.cache_creation_input_tokens + mu.cache_read_input_tokens
            cost = metrics_mod.cost_usd(mu, model=model_id or None)
            tpd = tok / cost if cost > 0 else float("inf")
            items.append((model_id, cost, tok, tpd))
        items.sort(key=lambda x: x[1], reverse=True)

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"总计 {fmt_money(total_cost)} · {len(per_model)} 个模型",
                 bg="#2a2a2e", fg=self._COLOR, font=theme.FONT_UI_BOLD).pack()

        max_cost = items[0][1] if items else 1

        for model_id, cost, tok, tpd in items:
            pct = cost / total_cost * 100 if total_cost else 0
            name = model_label(model_id) if model_id else "(未记录)"

            # Header row
            tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")
            hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            hdr.pack(fill="x")
            tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                     font=theme.FONT_VALUE).pack(side="left")
            tk.Label(hdr, text=f"{fmt_money(cost)}（{pct:.1f}%）",
                     bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                     font=theme.FONT_MONO).pack(side="right")

            # Cost bar
            bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            bar_frame.pack(fill="x")
            bar_bg = tk.Frame(bar_frame, bg="#333333", height=10)
            bar_bg.pack(fill="x")
            if max_cost > 0:
                tk.Frame(bar_bg, bg=self._COLOR, height=10).place(
                    relx=0, rely=0, relwidth=cost / max_cost, relheight=1.0)

            # Detail line: tokens + cost-effectiveness
            det = tk.Frame(inner, bg=theme.PANEL, padx=12, pady=4)
            det.pack(fill="x")
            tk.Label(det, text=f"Token {tok:,}",
                     bg=theme.PANEL, fg=theme.MUTED,
                     font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)
            if tpd == float("inf"):
                eff_text = "效率 ∞（免费）"
                eff_color = theme.SUCCESS
            else:
                eff_text = f"效率 {tpd:,.0f} Token/$"
                eff_color = theme.SUCCESS if tpd > 1_000_000 else theme.MUTED
            tk.Label(det, text=eff_text,
                     bg=theme.PANEL, fg=eff_color,
                     font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)


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


class UserMsgsPopup:
    """用户消息 — all user messages in this session, card-style layout."""

    _ACCENT = "#569cd6"  # blue accent for badges

    def __init__(self, parent, messages: list[str]) -> None:
        total_chars = sum(len(m) for m in messages)
        win = _new_window(parent, "用户消息", "620x500")
        tk.Label(win, text="用户消息", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        if not messages:
            tk.Label(inner, text="未记录到用户消息", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
        else:
            # Summary header
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=10)
            tk.Label(head, text=f"共 {len(messages)} 条消息 · {total_chars:,} 字符",
                     bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

            for idx, txt in enumerate(messages, 1):
                # Card frame
                card = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
                card.pack(fill="x", pady=4)

                # Header row: badge + char count
                hdr = tk.Frame(card, bg="#2a2a2e")
                hdr.pack(fill="x")
                badge = tk.Label(hdr, text=f"#{idx}", bg=self._ACCENT, fg="#ffffff",
                                 font=(theme.FONT_MONO_NAME, 8, "bold"), padx=6, pady=1)
                badge.pack(side="left")
                tk.Label(hdr, text=f"{len(txt)} 字符", bg="#2a2a2e", fg=theme.MUTED,
                         font=(theme.FONT_MONO_NAME, 8)).pack(side="right")

                # Message text
                tk.Label(card, text=txt, bg="#2a2a2e", fg=theme.FG,
                         font=theme.FONT_UI, wraplength=540, justify="left",
                         anchor="w").pack(fill="x", pady=(4, 0))


class FilesTouchedPopup:
    """涉及文件 — all files read/written/edited, with proportional bars."""

    _COLOR = "#569cd6"  # blue

    def __init__(self, parent, details: dict[str, int]) -> None:
        win = _new_window(parent, "涉及文件", "560x480")
        tk.Label(win, text=f"涉及文件（共 {len(details)} 个）", bg=theme.BG,
                 fg=theme.FG, font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text="会话中被读取、写入或编辑过的文件及操作次数。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI, wraplength=520,
                 justify="left").pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        sorted_items = sorted(details.items(), key=lambda x: x[1], reverse=True)
        total_ops = sum(details.values())
        max_cnt = sorted_items[0][1] if sorted_items else 1

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"共 {len(details)} 个文件 · 合计 {total_ops} 次操作",
                 bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

        for fp, cnt in sorted_items:
            display = fp if len(fp) < 55 else "…" + fp[-52:]

            tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
            hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            hdr.pack(fill="x")
            tk.Label(hdr, text=display, bg=theme.PANEL, fg=theme.FG, anchor="w",
                     font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
            tk.Label(hdr, text=f"{cnt} 次", bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                     font=theme.FONT_MONO).pack(side="right")

            bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            bar_frame.pack(fill="x")
            bar_bg = tk.Frame(bar_frame, bg="#333333", height=8)
            bar_bg.pack(fill="x")
            tk.Frame(bar_bg, bg=self._COLOR, height=8).place(
                relx=0, rely=0, relwidth=cnt / max_cnt, relheight=1.0)


class RadarPopup:
    """六维效率雷达 — hexagonal radar chart with absolute-grade normalization.

    Each axis uses a fixed reference scale (grade thresholds or natural bounds)
    instead of project min/max, so outliers don't distort the shape.
    """

    # (key, label, norm_type, ref)
    # norm_type:
    #   "grade"     = val/ref (越高越好, ref=优秀阈值)
    #   "grade_inv" = ref/val (越低越好, ref=优秀阈值)
    #   "pct"       = 0-1 比率, 直接使用
    #   "pct100"    = 0-100 百分比, /100 后使用
    #   "pct_inv"   = 1-val (越低越好, 0-1 比率)
    #   "ratio"     = val/ref (有参考值)
    _AXES = [
        ("ctei",  "综合效率", "grade",     2.0),
        ("chr",   "缓存命中", "pct100",    1.0),
        ("cpe",   "千行成本", "grade_inv", 8.22),
        ("churn", "返工率",   "pct_inv",   1.0),
        ("read_write_ratio", "读写比", "ratio", 3.0),
        ("tcer",  "编码效率", "grade",     76.59),
    ]

    def __init__(self, parent, report, all_reports) -> None:
        import math
        from tcer.gui.views import metric_raw_value

        sid = (report.meta.session_id or report.meta.path.stem)[:16]
        win = _new_window(parent, f"效率雷达 · {sid}…", "440x480")

        canvas = tk.Canvas(win, bg=theme.PANEL, highlightthickness=0,
                           width=400, height=400)
        canvas.pack(padx=16, pady=16)

        # Normalize each axis to 0-1 using absolute scales
        axis_data = []
        for key, label, ntype, ref in self._AXES:
            raw = metric_raw_value(report, key)
            norm = self._normalize(raw, ntype, ref)
            axis_data.append((key, label, raw, norm))

        # Draw hexagonal radar
        cx, cy, R = 200, 210, 140
        n = len(axis_data)

        # Concentric grid rings
        for frac in (0.25, 0.5, 0.75, 1.0):
            pts = []
            for ai in range(n):
                angle = math.pi / 2 + 2 * math.pi * ai / n
                px = cx + R * frac * math.cos(angle)
                py = cy - R * frac * math.sin(angle)
                pts.extend([px, py])
            canvas.create_polygon(pts, outline="#3e3e42", fill="", dash=(2, 3))
        canvas.create_text(cx + R * 0.52, cy - 4, text="50%",
                           fill="#444444", font=theme.FONT_MONO)

        # Axes + labels
        for ai, (key, label, raw, norm) in enumerate(axis_data):
            angle = math.pi / 2 + 2 * math.pi * ai / n
            ex = cx + R * math.cos(angle)
            ey = cy - R * math.sin(angle)
            canvas.create_line(cx, cy, ex, ey, fill="#3e3e42")
            lx = cx + (R + 24) * math.cos(angle)
            ly = cy - (R + 24) * math.sin(angle)
            canvas.create_text(lx, ly, text=label, fill=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD)
            raw_text = self._fmt_raw(key, raw)
            rx = cx + (R + 24) * math.cos(angle)
            ry = cy - (R + 24) * math.sin(angle) + 14
            canvas.create_text(rx, ry, text=raw_text, fill=theme.MUTED,
                               font=theme.FONT_MONO)

        # Data polygon
        data_pts = []
        for ai, (key, label, raw, norm) in enumerate(axis_data):
            angle = math.pi / 2 + 2 * math.pi * ai / n
            px = cx + R * norm * math.cos(angle)
            py = cy - R * norm * math.sin(angle)
            data_pts.extend([px, py])
        canvas.create_polygon(data_pts, outline=theme.ACCENT,
                              fill="#1a3a5a", width=2)
        for ai in range(0, len(data_pts), 2):
            px, py = data_pts[ai], data_pts[ai + 1]
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3,
                               fill=theme.ACCENT, outline=theme.FG)

        canvas.create_text(cx, 14, text="六维效率雷达（绝对刻度，外圈=100%）",
                           fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        tk.Button(win, text="关闭", command=win.destroy, bg=theme.ACCENT,
                  fg=theme.FG, relief="flat", padx=20, pady=4).pack(pady=6)

    @staticmethod
    def _normalize(raw, ntype, ref):
        """Normalize raw value to 0-1 using absolute scale."""
        if raw is None:
            return 0.0
        if ntype == "grade":
            return max(0.0, min(1.0, raw / ref))
        if ntype == "grade_inv":
            return max(0.0, min(1.0, ref / raw)) if raw > 0 else 1.0
        if ntype == "pct100":
            return max(0.0, min(1.0, raw / 100.0))
        if ntype == "pct":
            return max(0.0, min(1.0, raw))
        if ntype == "pct_inv":
            return max(0.0, min(1.0, 1.0 - raw))
        if ntype == "ratio":
            return max(0.0, min(1.0, raw / ref))
        return 0.0

    @staticmethod
    def _fmt_raw(key, raw):
        if raw is None:
            return "—"
        if key == "chr":
            # metric_raw_value already scales chr to 0-100
            return f"{raw:.1f}%"
        if key == "churn":
            # churn is 0-1 ratio, display as percentage
            return f"{raw * 100:.1f}%"
        if key == "cpe":
            return f"${raw:.1f}"
        if key == "ctei":
            return f"{raw:.2f}"
        if key == "tcer":
            return f"{raw:.1f}"
        if key == "read_write_ratio":
            return f"{raw:.2f}"
        return f"{raw:g}"


def _copy(win, text: str) -> None:
    win.clipboard_clear()
    win.clipboard_append(text)
    # small transient confirmation
    toast = tk.Label(win, text="已复制到剪贴板", bg=theme.SUCCESS, fg="#000000",
                     font=theme.FONT_UI, padx=8, pady=2)
    toast.place(relx=0.5, rely=0.02, anchor="n")
    win.after(1200, toast.destroy)
