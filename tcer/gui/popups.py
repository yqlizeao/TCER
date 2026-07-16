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
from .metric_defs import CONCEPT_NOTES, GROUPS, METRIC_BY_KEY
from .widgets import ScrollFrame


def _new_window(parent, title, size, bg=theme.BG) -> tk.Toplevel:
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=bg)
    # Center relative to parent window
    parent.update_idletasks()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    w, h = (int(x) for x in size.split("x"))
    x = px + (pw - w) // 2
    y = py + (ph - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")
    return win


class SessionDetailPopup:
    """会话详情 — metadata + per-model cost breakdown, unified card style."""

    _COST_COLOR = "#ce9178"  # warm orange for cost bars

    def __init__(self, parent, report) -> None:
        from tcer.core import metrics as metrics_mod
        from tcer.core.pricing import label as model_label

        r = report
        u = r.usage
        sid = (r.meta.session_id or r.meta.path.stem)[:16]
        win = _new_window(parent, f"会话详情 · {sid}…", "580x600")
        tk.Label(win, text="会话详情", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        # Summary header
        total_cost = metrics_mod.cost_usd(u)
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"{r.meta.title or '(无标题)'} · {fmt.models_label(u)}",
                 bg="#2a2a2e", fg=theme.FG, font=theme.FONT_UI_BOLD).pack()
        tk.Label(head, text=f"{u.total:,} Token · {fmt.fmt_money(total_cost)}",
                 bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI).pack()

        # Metadata card
        def meta_row(key, val):
            row = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=2)
            row.pack(fill="x")
            tk.Label(row, text=key, bg=theme.PANEL, fg=theme.MUTED, width=10,
                     anchor="w", font=theme.FONT_UI).pack(side="left")
            tk.Label(row, text=str(val), bg=theme.PANEL, fg=theme.FG,
                     anchor="w", font=theme.FONT_UI, wraplength=400,
                     justify="left").pack(side="left", fill="x", expand=True)

        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        meta_row("会话 ID", r.meta.session_id or "(无)")
        meta_row("工作目录", r.meta.cwd or "(未知)")
        meta_row("开始", fmt.fmt_dt(u.started_at, "%Y-%m-%d %H:%M:%S"))
        meta_row("结束", fmt.fmt_dt(u.ended_at, "%Y-%m-%d %H:%M:%S"))

        # Per-model cost section
        _SKIP = {"<synthetic>", ""}
        per_model = {k: v for k, v in u.per_model.items() if k not in _SKIP and k}

        if per_model:
            # Section header
            tk.Frame(inner, bg=theme.PANEL, height=10).pack(fill="x")
            sec = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=4)
            sec.pack(fill="x")
            tk.Label(sec, text="逐模型成本", bg=theme.PANEL, fg="#9cdcfe",
                     font=theme.FONT_UI_BOLD).pack(anchor="w")

            # Build sorted items
            cost_items = []
            for m, bucket in per_model.items():
                cost = metrics_mod.cost_usd(bucket, model=m)
                tok = bucket.input_tokens + bucket.output_tokens + bucket.cache_creation_input_tokens + bucket.cache_read_input_tokens
                cost_items.append((m, cost, tok))
            cost_items.sort(key=lambda x: x[1], reverse=True)
            max_cost = cost_items[0][1] if cost_items else 1

            for m, cost, tok in cost_items:
                pct = cost / total_cost * 100 if total_cost else 0
                name = model_label(m) if m else "(未记录)"

                tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
                hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                hdr.pack(fill="x")
                tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                         font=theme.FONT_VALUE).pack(side="left")
                tk.Label(hdr, text=f"{fmt.fmt_money(cost)}（{pct:.1f}%）",
                         bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                         font=theme.FONT_MONO).pack(side="right")

                bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                bar_frame.pack(fill="x")
                bar_bg = tk.Frame(bar_frame, bg="#333333", height=8)
                bar_bg.pack(fill="x")
                if max_cost > 0:
                    tk.Frame(bar_bg, bg=self._COST_COLOR, height=8).place(
                        relx=0, rely=0, relwidth=cost / max_cost, relheight=1.0)

                det = tk.Frame(inner, bg=theme.PANEL, padx=12, pady=4)
                det.pack(fill="x")
                tk.Label(det, text=f"Token {tok:,}",
                         bg=theme.PANEL, fg=theme.MUTED,
                         font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)

        # Warning for unseen writes
        if r.unseen_writes:
            tk.Frame(inner, bg=theme.PANEL, height=10).pack(fill="x")
            warn = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=6)
            warn.pack(fill="x")
            tk.Label(warn,
                     text=f"⚠ {r.unseen_writes} 个「未见文件的 Write」（LOC 假设为新文件，"
                          "若覆写已有文件会高估 added）",
                     bg=theme.PANEL, fg=theme.WARNING, justify="left",
                     font=theme.FONT_UI, wraplength=520).pack(anchor="w")


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
    # Token-type labels sourced from the metric SSOT (G2 names) so they read
    # identically to the 指标分类 tab — input/output/缓存创建/缓存命中.
    _LABELS = {
        "input":          METRIC_BY_KEY["input"].name,
        "output":         METRIC_BY_KEY["output"].name,
        "cache_creation": METRIC_BY_KEY["cache_write"].name,
        "cache_read":     METRIC_BY_KEY["cache_read"].name,
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
            from tcer.core import pricing as pricing_mod
            unmatched = metrics_mod.unmatched_pricing_models(usage)

            # Summary header
            head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
            head.pack(fill="x", pady=10)
            tk.Label(head, text=f"总计 {total_tokens:,} Token · {fmt_money(total_cost)} · "
                                f"{len(per_model)} 个模型",
                     bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()
            if unmatched:
                warn = tk.Frame(inner, bg="#3a2a1a", padx=10, pady=6)
                warn.pack(fill="x", pady=(0, 4))
                names = "、".join(pricing_mod.label(m) for m in unmatched[:6])
                more = f" 等 {len(unmatched)} 个" if len(unmatched) > 6 else ""
                tk.Label(
                    warn,
                    text=f"⚠ {len(unmatched)} 个模型未在价表中（按默认 list 价）：{names}{more}\n"
                         f"成本可能偏差；可在 tcer/config/model_pricing.json 补充条目。",
                    bg="#3a2a1a", fg=theme.WARNING,
                    font=theme.FONT_UI, justify="left", wraplength=500,
                ).pack(anchor="w")

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
                on_default = model_id in unmatched

                # --- Header row: model name + total + cost ---
                tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")
                hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
                hdr.pack(fill="x")
                title = f"{name} · 默认价" if on_default else name
                tk.Label(hdr, text=title, bg=theme.PANEL,
                         fg=theme.WARNING if on_default else theme.FG,
                         anchor="w", font=theme.FONT_VALUE).pack(side="left")
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
    # Top 3 efficiency — gold / purple / blue
    _MEDAL = ["#ffd700", "#a335ee", "#0070dd"]

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
        unmatched = set(metrics_mod.unmatched_pricing_models(usage))

        # Build items: (model_id, cost, total_tokens, tokens_per_dollar)
        items = []
        for model_id, mu in per_model.items():
            tok = mu.input_tokens + mu.output_tokens + mu.cache_creation_input_tokens + mu.cache_read_input_tokens
            cost = metrics_mod.cost_usd(mu, model=model_id or None)
            tpd = tok / cost if cost > 0 else float("inf")
            items.append((model_id, cost, tok, tpd))

        # Rank by efficiency (top 3 get medals)
        ranked = sorted(items, key=lambda x: x[3], reverse=True)
        medal_map: dict[str, int] = {}
        for rank, (mid, *_) in enumerate(ranked):
            if rank < 3:
                medal_map[mid] = rank

        # Sort display by cost descending
        items.sort(key=lambda x: x[1], reverse=True)

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"总计 {fmt_money(total_cost)} · {len(per_model)} 个模型",
                 bg="#2a2a2e", fg=self._COLOR, font=theme.FONT_UI_BOLD).pack()
        if unmatched:
            warn = tk.Frame(inner, bg="#3a2a1a", padx=10, pady=6)
            warn.pack(fill="x", pady=(0, 4))
            tk.Label(
                warn,
                text=f"⚠ {len(unmatched)} 个模型未在价表中，成本按默认 list 价估算（见各行「默认价」标记）。",
                bg="#3a2a1a", fg=theme.WARNING, font=theme.FONT_UI, wraplength=500, justify="left",
            ).pack(anchor="w")

        max_cost = items[0][1] if items else 1

        for model_id, cost, tok, tpd in items:
            pct = cost / total_cost * 100 if total_cost else 0
            name = model_label(model_id) if model_id else "(未记录)"
            if model_id in unmatched:
                name = f"{name} · 默认价"

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

            # Detail line: tokens + cost-effectiveness + medal
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
                rank = medal_map.get(model_id)
                eff_color = self._MEDAL[rank] if rank is not None else theme.MUTED
            tk.Label(det, text=eff_text,
                     bg=theme.PANEL, fg=eff_color,
                     font=(theme.FONT_MONO_NAME, 8), anchor="w").pack(side="left", padx=8)


class CalibratePopup:
    """校准结果 — tcer LOC vs git ground truth, unified card style."""

    _TCER_COLOR = "#569cd6"   # blue for tcer
    _GIT_COLOR = "#4ec9b0"    # teal for git
    _DEV_COLOR = "#f48771"    # red for deviation

    def __init__(self, parent, calibrations, text_report: str) -> None:
        win = _new_window(parent, "LOC 精度校准（对照 git）", "600x580")
        tk.Label(win, text="LOC 精度校准", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        # Compute totals
        tot_tcer = tot_git = 0
        for cal in calibrations:
            tot_tcer += cal.tcer_added - cal.tcer_deleted
            tot_git += cal.git_added - cal.git_deleted
        factor = (tot_tcer / tot_git) if tot_git else 0
        ratio = ((tot_tcer / tot_git - 1) * 100) if tot_git else 0

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"{len(calibrations)} 个会话",
                 bg="#2a2a2e", fg=theme.FG, font=theme.FONT_UI_BOLD).pack()
        tk.Label(head, text=f"工具调用净增 {tot_tcer:+,}  git 净增 {tot_git:+,}  "
                            f"偏差 {tot_tcer - tot_git:+,}（{ratio:+.1f}%）",
                 bg="#2a2a2e", fg=self._DEV_COLOR, font=theme.FONT_UI).pack()
        tk.Label(head, text=f"校准系数: {factor:.4f}",
                 bg="#2a2a2e", fg=theme.MUTED, font=theme.FONT_UI).pack()

        # Per-session cards
        for cal in calibrations:
            tcer_net = cal.tcer_added - cal.tcer_deleted
            git_net = cal.git_added - cal.git_deleted
            dev = cal.net_deviation

            tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
            card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=6)
            card.pack(fill="x")

            # Session ID + deviation
            hdr = tk.Frame(card, bg=theme.PANEL)
            hdr.pack(fill="x")
            tk.Label(hdr, text=cal.session_id[:38], bg=theme.PANEL, fg=theme.FG,
                     anchor="w", font=theme.FONT_MONO).pack(side="left")
            dev_color = self._DEV_COLOR if abs(dev) > 100 else theme.MUTED
            tk.Label(hdr, text=f"{dev:+d}", bg=theme.PANEL, fg=dev_color,
                     anchor="e", font=theme.FONT_MONO).pack(side="right")

            # Detail line: tcer vs git
            det = tk.Frame(card, bg=theme.PANEL)
            det.pack(fill="x", pady=2)
            tk.Label(det, text=f"工具调用 +{cal.tcer_added} -{cal.tcer_deleted}",
                     bg=theme.PANEL, fg=self._TCER_COLOR,
                     font=(theme.FONT_MONO_NAME, 8)).pack(side="left", padx=(0, 12))
            tk.Label(det, text=f"git +{cal.git_added} -{cal.git_deleted}",
                     bg=theme.PANEL, fg=self._GIT_COLOR,
                     font=(theme.FONT_MONO_NAME, 8)).pack(side="left")

        # Bottom buttons
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        tk.Button(btn_bar, text="复制文本报告", command=lambda: _copy(win, text_report),
                  bg=theme.PANEL, fg=theme.FG, relief="flat", padx=12, pady=4).pack(side="left", padx=4)
        tk.Button(btn_bar, text="关闭", command=win.destroy, bg=theme.ACCENT, fg=theme.FG,
                  relief="flat", padx=20, pady=4).pack(side="left", padx=4)


class BaselinesPopup:
    """计算出的个人基准 + 应用按钮，统一卡片风格。"""

    _COLOR = "#dcdcaa"  # yellow for baseline values

    def __init__(self, parent, values: dict, n_sessions: int, on_apply) -> None:
        win = _new_window(parent, "计算个人基准", "440x360")
        tk.Label(win, text="计算个人基准", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"基于 {n_sessions} 个会话计算",
                 bg="#2a2a2e", fg=theme.FG, font=theme.FONT_UI_BOLD).pack()

        # Baseline cards
        for key, method in [("tcer", "中位数"), ("ncpi", "均值"), ("cpe", "中位数")]:
            val = values[key]
            tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
            card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
            card.pack(fill="x")

            hdr = tk.Frame(card, bg=theme.PANEL)
            hdr.pack(fill="x")
            name = {"tcer": "TCER（行/百万Token）", "ncpi": "NCPI（代码库贡献度）",
                    "cpe": "CPE（千行成本·美元）"}[key]
            tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG,
                     anchor="w", font=theme.FONT_VALUE).pack(side="left")
            tk.Label(hdr, text=f"{val:.3f}", bg=theme.PANEL, fg=self._COLOR,
                     anchor="e", font=theme.FONT_MONO).pack(side="right")

            tk.Label(card, text=f"计算方式: {method}", bg=theme.PANEL, fg=theme.MUTED,
                     font=(theme.FONT_MONO_NAME, 8)).pack(anchor="w")

        # Note
        tk.Frame(inner, bg=theme.PANEL, height=10).pack(fill="x")
        tk.Label(inner, text="应用后将写入配置并立即重算综合效率分刻度。",
                 bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI,
                 wraplength=380, justify="left").pack(padx=10, pady=4)

        # Buttons
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        tk.Button(btn_bar, text="应用为基准", command=lambda: (on_apply(values), win.destroy()),
                  bg=theme.ACCENT, fg=theme.FG, relief="flat", padx=16, pady=4).pack(side="left", padx=4)
        tk.Button(btn_bar, text="取消", command=win.destroy, bg=theme.PANEL, fg=theme.FG,
                  relief="flat", padx=16, pady=4).pack(side="left", padx=4)


class AdvancedPopup:
    """高级选项 — code-dir 覆盖 + 跳过 LOC + 代码库扫描开关，统一卡片风格。"""

    def __init__(self, parent, code_dir: str, no_loc: bool, scan_code_dir: bool,
                 on_apply) -> None:
        win = _new_window(parent, "高级选项", "480x300")
        tk.Label(win, text="高级选项", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text="自定义分析参数",
                 bg="#2a2a2e", fg=theme.FG, font=theme.FONT_UI_BOLD).pack()

        # Code dir card
        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        card1 = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
        card1.pack(fill="x")
        tk.Label(card1, text="工作目录", bg=theme.PANEL, fg=theme.FG,
                 font=theme.FONT_VALUE).pack(anchor="w")
        tk.Label(card1, text="累计 LOC 扫描目录，留空则使用会话 cwd",
                 bg=theme.PANEL, fg=theme.MUTED,
                 font=(theme.FONT_MONO_NAME, 8)).pack(anchor="w", pady=(0, 4))
        code_var = tk.StringVar(value=code_dir)
        tk.Entry(card1, textvariable=code_var, width=48, bg="#1e1e1e", fg=theme.FG,
                 insertbackground=theme.FG, relief="flat", highlightthickness=1,
                 highlightbackground="#3e3e42").pack(anchor="w")

        # No-LOC card
        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        card2 = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
        card2.pack(fill="x")
        no_loc_var = tk.BooleanVar(value=no_loc)
        tk.Checkbutton(card2, text="跳过 LOC（仅 Token 指标，不算 TCER/CPE/CTEI）",
                       variable=no_loc_var, bg=theme.PANEL, fg=theme.FG, selectcolor="#1e1e1e",
                       activebackground=theme.PANEL, activeforeground=theme.FG,
                       font=theme.FONT_UI).pack(anchor="w")

        # Scan-code-dir card — opt-in tree_loc (NCPI/CTEI denominator). Off by
        # default: scanning a large repo (Rust target/, vendored deps, …) can
        # freeze the UI for minutes.
        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        card3 = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
        card3.pack(fill="x")
        scan_code_var = tk.BooleanVar(value=scan_code_dir)
        tk.Checkbutton(card3, text="扫描代码库目录（计算 NCPI/CTEI，大项目可能很慢）",
                       variable=scan_code_var, bg=theme.PANEL, fg=theme.FG, selectcolor="#1e1e1e",
                       activebackground=theme.PANEL, activeforeground=theme.FG,
                       font=theme.FONT_UI).pack(anchor="w")

        # Buttons
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        tk.Button(btn_bar, text="应用并重算",
                  command=lambda: (on_apply(code_var.get().strip() or None, no_loc_var.get(), scan_code_var.get()), win.destroy()),
                  bg=theme.ACCENT, fg=theme.FG, relief="flat", padx=16, pady=4).pack(side="left", padx=4)
        tk.Button(btn_bar, text="取消", command=win.destroy, bg=theme.PANEL, fg=theme.FG,
                  relief="flat", padx=16, pady=4).pack(side="left", padx=4)


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


class MemoryFilesPopup:
    """项目记忆文件 — 展示 memory/ 下的文件列表，带跳转到目录按钮。

    风格与 FilesTouchedPopup 一致（卡片 + 比例条），多一个「打开目录」按钮。
    """

    _COLOR = "#c586c0"  # purple accent for memory files

    def __init__(self, parent, memory_dir: str, files: list[str]) -> None:
        from .platform import open_in_file_manager, FILE_MANAGER_NAME

        count = len(files)
        win = _new_window(parent, "项目记忆文件", "560x460")
        tk.Label(win, text=f"项目记忆文件（{count} 个）", bg=theme.BG,
                 fg=theme.FG, font=theme.FONT_HEADING, pady=10).pack()
        tk.Label(win, text=f"路径：{memory_dir}",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI, wraplength=520,
                 justify="left").pack()

        # 按钮栏：打开目录（居中）
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(fill="x", padx=10, pady=(4, 8))
        tk.Button(btn_bar, text=f"📂 在{FILE_MANAGER_NAME}中打开目录",
                  command=lambda: open_in_file_manager(memory_dir),
                  bg=theme.PANEL_2, fg=theme.FG, relief="flat",
                  activebackground=theme.PANEL, activeforeground=theme.FG,
                  padx=12, pady=3, font=theme.FONT_UI, cursor="hand2").pack(anchor="center")

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        inner = sf.inner

        # Summary header
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"共 {count} 个文件 · memory/",
                 bg="#2a2a2e", fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

        if count == 0:
            tk.Label(inner, text="该目录下暂无记忆文件", bg=theme.PANEL,
                     fg=theme.MUTED, font=theme.FONT_UI, pady=30).pack()
            return

        # 按文件名排序
        from pathlib import Path as PPath
        sorted_files = sorted(files, key=lambda f: PPath(f).name)
        max_size = max((PPath(f).stat().st_size for f in sorted_files if PPath(f).exists()), default=1)

        for fp in sorted_files:
            p = PPath(fp)
            name = p.name
            try:
                size = p.stat().st_size
            except OSError:
                size = 0

            # 卡片（与 FilesTouchedPopup 同结构）
            tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
            hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            hdr.pack(fill="x")
            tk.Label(hdr, text=name, bg=theme.PANEL, fg=theme.FG, anchor="w",
                     font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
            size_txt = f"{size:,} B" if size < 1024 else f"{size / 1024:.1f} KB"
            tk.Label(hdr, text=size_txt, bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                     font=theme.FONT_MONO).pack(side="right")

            bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            bar_frame.pack(fill="x")
            bar_bg = tk.Frame(bar_frame, bg="#333333", height=8)
            bar_bg.pack(fill="x")
            tk.Frame(bar_bg, bg=self._COLOR, height=8).place(
                relx=0, rely=0, relwidth=size / max_size, relheight=1.0)


class RadarPopup:
    """六维效率雷达 — hexagonal radar chart with absolute-grade normalization.

    Each axis uses a fixed reference scale (grade thresholds or natural bounds)
    instead of project min/max, so outliers don't distort the shape.
    """

    # (key, norm_type, ref) — axis label + value text come from the metric SSOT
    # (metric_defs) so the radar reads exactly like the 指标分类 tab. ``norm_type``
    # / ``ref`` are radar-only (how the 0–1 polygon radius is scaled).
    _AXES = [
        ("ctei",  "grade",     2.0),
        ("chr",   "pct100",    1.0),
        ("cpe",   "grade_inv", 8.22),
        ("churn", "pct_inv",   1.0),
        ("read_write_ratio", "ratio", 3.0),
        ("tcer",  "grade",     76.59),
    ]

    def __init__(self, parent, report, all_reports) -> None:
        import math
        from .metric_defs import raw_value as metric_raw_value
        from .metric_defs import display as metric_display, METRIC_BY_KEY

        sid = (report.meta.session_id or report.meta.path.stem)[:16]
        win = _new_window(parent, f"效率雷达 · {sid}…", "460x560")
        tk.Label(win, text="六维效率雷达", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=8).pack()

        # Summary header — CTEI string straight from the SSOT (matches 指标分类).
        head = tk.Frame(win, bg="#2a2a2e", padx=10, pady=6)
        head.pack(fill="x", padx=10, pady=(0, 4))
        grade = report.grade or "-"
        ctei_val = metric_display(report, "ctei")
        tk.Label(head, text=f"{report.meta.title or sid}  CTEI {ctei_val}  评级 {grade}",
                 bg="#2a2a2e", fg=theme.FG, font=theme.FONT_UI).pack()

        # Radar canvas
        canvas = tk.Canvas(win, bg=theme.PANEL, highlightthickness=0,
                           width=400, height=400)
        canvas.pack(padx=16, pady=8)

        # Normalize each axis to 0-1 using absolute scales; label + value from SSOT.
        axis_data = []
        for key, ntype, ref in self._AXES:
            raw = metric_raw_value(report, key)
            norm = self._normalize(raw, ntype, ref)
            label = METRIC_BY_KEY[key].name
            value_text = metric_display(report, key)
            axis_data.append((key, label, value_text, norm))

        # Draw hexagonal radar
        cx, cy, R = 200, 200, 140
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
        for ai, (key, label, value_text, norm) in enumerate(axis_data):
            angle = math.pi / 2 + 2 * math.pi * ai / n
            ex = cx + R * math.cos(angle)
            ey = cy - R * math.sin(angle)
            canvas.create_line(cx, cy, ex, ey, fill="#3e3e42")
            lx = cx + (R + 24) * math.cos(angle)
            ly = cy - (R + 24) * math.sin(angle)
            canvas.create_text(lx, ly, text=label, fill=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD)
            rx = cx + (R + 24) * math.cos(angle)
            ry = cy - (R + 24) * math.sin(angle) + 14
            canvas.create_text(rx, ry, text=value_text, fill=theme.MUTED,
                               font=theme.FONT_MONO)

        # Data polygon
        data_pts = []
        for ai, (key, label, value_text, norm) in enumerate(axis_data):
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

        canvas.create_text(cx, 14, text="绝对刻度，外圈 = 100%",
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


class ConfirmDeletePopup:
    """二次确认删除会话的模态对话框（仿 ccswitch 删除确认）。

    强调「不可恢复」，默认聚焦在「取消」上以防误删；点「删除会话」才触发
    ``on_confirm()``。删除真正的磁盘操作由调用方在回调里完成。
    """

    _DANGER = "#e53935"          # 醒目红 — 删除按钮
    _DANGER_ACTIVE = "#c62828"   # 按下/悬停态

    def __init__(self, parent, *, title: str, session_id: str, on_confirm) -> None:
        win = _new_window(parent, "删除会话", "460x250")
        win.transient(parent)
        win.resizable(False, False)

        # 标题行：警告图标 + 标题
        head = tk.Frame(win, bg=theme.BG)
        head.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(head, text="⚠", bg=theme.BG, fg=self._DANGER,
                 font=(theme.FONT_MONO_NAME, 18, "bold")).pack(side="left", padx=(0, 8))
        tk.Label(head, text="删除会话", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(side="left")

        body = tk.Frame(win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=20)
        disp_title = title if len(title) <= 36 else title[:36] + "…"
        tk.Label(body, text=f"将永久删除本地会话“{disp_title}”",
                 bg=theme.BG, fg=theme.FG, font=theme.FONT_UI,
                 anchor="w", justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(body, text=f"Session ID: {session_id}",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_MONO,
                 anchor="w", justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(body, text="将一并删除其 subagent 与 tool-results 数据，此操作不可恢复。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                 anchor="w", justify="left", wraplength=410).pack(anchor="w", pady=(12, 0))

        # 按钮行（右对齐）
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(fill="x", padx=20, pady=(8, 16))

        def _do_delete():
            win.destroy()
            on_confirm()

        del_btn = tk.Button(btn_bar, text="删除会话", command=_do_delete,
                            bg=self._DANGER, fg="#ffffff", relief="flat",
                            activebackground=self._DANGER_ACTIVE, activeforeground="#ffffff",
                            padx=16, pady=5, font=theme.FONT_UI_BOLD, cursor="hand2")
        del_btn.pack(side="right")
        cancel_btn = tk.Button(btn_bar, text="取消", command=win.destroy,
                              bg=theme.PANEL_2, fg=theme.FG, relief="flat",
                              activebackground=theme.PANEL, activeforeground=theme.FG,
                              padx=16, pady=5, font=theme.FONT_UI, cursor="hand2")
        cancel_btn.pack(side="right", padx=(0, 8))

        win.bind("<Escape>", lambda e: win.destroy())
        cancel_btn.focus_set()          # 默认聚焦取消，回车不会误删
        win.grab_set()                  # 模态


class UploadDialog:
    """上传到 TCER Web — 服务器/账号/选项面板，统一卡片风格。

    Collects server/credentials/options and hands them to ``on_upload`` (which
    runs the actual HTTP call off the Tk thread) via a callback. The dialog only
    gathers input, persists prefs, and shows status; it never touches the
    network itself. ``projects`` is a list of ``(key, display)`` tuples.
    """

    def __init__(self, parent, *, prefs: dict, projects: list[tuple[str, str]],
                 default_project: str | None, on_upload, on_save_prefs) -> None:
        self._on_upload = on_upload
        self._on_save_prefs = on_save_prefs
        self._projects = projects

        win = _new_window(parent, "上传到 TCER Web", "480x680")
        self._win = win
        tk.Label(win, text="上传到 TCER Web", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        inner = sf.inner

        # -- Server + credentials card --
        card1 = self._card(inner, "服务器与账号")
        self.server_var = tk.StringVar(value=prefs.get("server_url", ""))
        self._entry(card1, "服务器地址", self.server_var)
        self.user_var = tk.StringVar(value=prefs.get("username", ""))
        self._entry(card1, "账号", self.user_var)
        self.pwd_var = tk.StringVar(value=prefs.get("password", ""))
        self._entry(card1, "密码", self.pwd_var, show="*")
        self.remember_var = tk.BooleanVar(value=bool(prefs.get("remember_password")))
        self._check(card1, "记住密码（明文 base64 混淆存储，非加密）", self.remember_var)

        # -- Options card --
        card2 = self._card(inner, "上传选项")
        self.anon_var = tk.BooleanVar(value=bool(prefs.get("anonymous")))
        self._check(card2, "匿名上传（按账号生成稳定的匿名代号，便于 web 端归并）",
                    self.anon_var)

        # -- Project multi-select listbox --
        tk.Label(card2, text="选择项目（可多选，Ctrl/Shift 点选）",
                 bg=theme.PANEL, fg=theme.FG, font=theme.FONT_UI,
                 anchor="w").pack(anchor="w", pady=(6, 0))
        self._proj_keys = [k for k, _ in projects]
        proj_displays = [d for _, d in projects]
        lb_frame = tk.Frame(card2, bg=theme.PANEL)
        lb_frame.pack(fill="x", pady=(2, 2))
        self._proj_lb = tk.Listbox(
            lb_frame, selectmode="extended", height=6, exportselection=False,
            bg="#1e1e1e", fg=theme.FG, relief="flat", highlightthickness=1,
            highlightbackground="#3e3e42", selectbackground=theme.ACCENT,
            selectforeground="#ffffff", font=theme.FONT_UI, activestyle="none")
        lb_sb = tk.Scrollbar(lb_frame, orient="vertical", command=self._proj_lb.yview)
        self._proj_lb.configure(yscrollcommand=lb_sb.set)
        self._proj_lb.pack(side="left", fill="both", expand=True)
        lb_sb.pack(side="right", fill="y")
        for d in proj_displays:
            self._proj_lb.insert("end", d)
        # Pre-select: current project + any remembered from prefs.
        preselect = set(prefs.get("last_projects") or [])
        if default_project:
            preselect.add(default_project)
        selected_idx = [i for i, k in enumerate(self._proj_keys) if k in preselect]
        if not selected_idx and self._proj_keys:
            selected_idx = [0]
        for i in selected_idx:
            self._proj_lb.selection_set(i)
        if selected_idx:
            self._proj_lb.see(selected_idx[0])

        sel_btn_row = tk.Frame(card2, bg=theme.PANEL)
        sel_btn_row.pack(anchor="w", pady=(0, 4))
        tk.Button(sel_btn_row, text="全选",
                  command=lambda: self._proj_lb.selection_set(0, "end"),
                  bg=theme.PANEL_2, fg=theme.FG, relief="flat", padx=8,
                  font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 4))
        tk.Button(sel_btn_row, text="清空",
                  command=lambda: self._proj_lb.selection_clear(0, "end"),
                  bg=theme.PANEL_2, fg=theme.FG, relief="flat", padx=8,
                  font=theme.FONT_UI_SMALL).pack(side="left")

        # 会话对话内容：每个会话始终作为独立指标行上传（后端按 session-id 去重）；
        # 勾选后额外附带该会话的逐条用户对话内容，否则仅上传指标。
        self.all_sessions_var = tk.BooleanVar(
            value=bool(prefs.get("all_sessions") or prefs.get("detail")))
        self._check(card2, "附带会话对话内容（默认各会话仅上传指标；后端按 session-id 去重）",
                    self.all_sessions_var)

        # -- Auto-upload card --
        card3 = self._card(inner, "自动上传")
        self.auto_var = tk.BooleanVar(value=bool(prefs.get("auto_upload")))
        self._check(card3, "启用后台定时上传", self.auto_var)
        int_row = tk.Frame(card3, bg=theme.PANEL)
        int_row.pack(anchor="w", pady=(2, 0))
        tk.Label(int_row, text="间隔（分钟）", bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI).pack(side="left")
        self.interval_var = tk.StringVar(value=str(prefs.get("interval_min", 30)))
        tk.Entry(int_row, textvariable=self.interval_var, width=6, bg="#1e1e1e",
                 fg=theme.FG, insertbackground=theme.FG, relief="flat",
                 highlightthickness=1, highlightbackground="#3e3e42").pack(side="left", padx=6)

        # -- Status line --
        self._status = tk.Label(win, text="", bg=theme.BG, fg=theme.MUTED,
                                font=theme.FONT_UI, wraplength=440, justify="left")
        self._status.pack(fill="x", padx=12, pady=(2, 0))

        # -- Buttons --
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        self._upload_btn = tk.Button(btn_bar, text="完成上传", command=self._do_upload,
                                     bg=theme.ACCENT, fg=theme.FG, relief="flat",
                                     padx=16, pady=4)
        self._upload_btn.pack(side="left", padx=4)
        tk.Button(btn_bar, text="关闭", command=win.destroy, bg=theme.PANEL,
                  fg=theme.FG, relief="flat", padx=16, pady=4).pack(side="left", padx=4)

    # -- small builders --
    def _card(self, inner, title: str) -> tk.Frame:
        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        head = tk.Frame(inner, bg="#2a2a2e", padx=10, pady=6)
        head.pack(fill="x")
        tk.Label(head, text=title, bg="#2a2a2e", fg=theme.FG,
                 font=theme.FONT_UI_BOLD).pack(anchor="w")
        card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x")
        return card

    def _entry(self, card, label: str, var, show: str = "") -> None:
        tk.Label(card, text=label, bg=theme.PANEL, fg=theme.FG,
                 font=theme.FONT_UI, anchor="w").pack(anchor="w", pady=(4, 0))
        tk.Entry(card, textvariable=var, width=48, bg="#1e1e1e", fg=theme.FG,
                 insertbackground=theme.FG, relief="flat", highlightthickness=1,
                 highlightbackground="#3e3e42", show=show).pack(anchor="w")

    def _check(self, card, label: str, var) -> None:
        tk.Checkbutton(card, text=label, variable=var, bg=theme.PANEL, fg=theme.FG,
                       selectcolor="#1e1e1e", activebackground=theme.PANEL,
                       activeforeground=theme.FG, font=theme.FONT_UI,
                       anchor="w").pack(anchor="w", pady=(4, 0))

    # -- prefs / status --
    def _collect(self) -> dict:
        try:
            interval = max(1, int(self.interval_var.get().strip() or "30"))
        except ValueError:
            interval = 30
        proj_keys = [self._proj_keys[i] for i in self._proj_lb.curselection()]
        all_sessions = bool(self.all_sessions_var.get())
        return {
            "server_url": self.server_var.get().strip(),
            "username": self.user_var.get().strip(),
            "password": self.pwd_var.get(),
            "remember_password": bool(self.remember_var.get()),
            "anonymous": bool(self.anon_var.get()),
            "last_projects": proj_keys,
            "all_sessions": all_sessions,
            "detail": all_sessions,  # 全部会话 ⇒ 上传明细
            "auto_upload": bool(self.auto_var.get()),
            "interval_min": interval,
        }

    def set_status(self, text: str, *, error: bool = False) -> None:
        if not self._status.winfo_exists():
            return
        self._status.config(text=text, fg=theme.ERROR if error else theme.SUCCESS)

    def _do_upload(self) -> None:
        prefs = self._collect()
        if not prefs["server_url"] or not prefs["username"]:
            self.set_status("请填写服务器地址与账号", error=True)
            return
        if not prefs["last_projects"]:
            self.set_status("请至少选择一个项目", error=True)
            return
        self._on_save_prefs(prefs)
        self.set_status("上传中…")
        self._on_upload(prefs, self)


def _copy(win, text: str) -> None:
    win.clipboard_clear()
    win.clipboard_append(text)
    # small transient confirmation
    toast = tk.Label(win, text="已复制到剪贴板", bg=theme.SUCCESS, fg="#000000",
                     font=theme.FONT_UI, padx=8, pady=2)
    toast.place(relx=0.5, rely=0.02, anchor="n")
    win.after(1200, toast.destroy)
