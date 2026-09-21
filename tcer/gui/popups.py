"""Dialog windows: glossary, session detail, tool calls, baselines.

Each popup is a ``Toplevel`` built on demand and owns no long-lived state. They
render from ``metric_defs`` / the analysis result so they never duplicate the
metric definitions.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from tcer.core import format as fmt
from tcer.core import metrics
from . import theme
from .metric_defs import METRIC_BY_KEY
from .widgets import CheckRow, ScrollFrame, SelectableLabel, Tooltip, flat_button, new_window
from .platform import CLICK_CURSOR


# 共享弹窗外壳在 widgets.new_window；保留旧名兼容既有调用。
_new_window = new_window


class UpdatePopup:
    """检查更新结果弹窗:展示当前/最新版本、发布说明,并引导前往下载。

    纯展示组件——不自己联网。由 controller 在后台线程拿到
    ``update_check.latest_release()`` 结果后,回到主线程创建本弹窗。

    *release* 为 ``latest_release()`` 的返回 dict 或 ``None``(检查失败)。
    """

    _NOTES_LIMIT = 1200  # 发布说明超长截断,避免弹窗无限增高

    def __init__(self, parent, current_version, release, controller=None) -> None:
        import sys
        import webbrowser

        from tcer.core import update_check

        self._controller = controller
        self._current = current_version
        self._release = release
        win = _new_window(parent, "检查更新", "520x360")
        self._win = win
        win.grab_release()  # 非模态:不阻塞主界面(检查更新是辅助动作)

        tk.Label(win, text="检查更新", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        body = tk.Frame(win, bg=theme.BG)
        self._body = body
        body.pack(fill="both", expand=True, padx=12)

        if release is None:
            # 检查失败——给 Releases 页兜底
            SelectableLabel(body, text="检查失败:无法连接 GitHub。", bg=theme.BG,
                            fg=theme.WARNING, font=theme.FONT_UI,
                            justify="left").pack(fill="x", pady=(4, 2))
            SelectableLabel(body, text=f"当前版本:v{current_version}", bg=theme.BG,
                            fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(fill="x")
            url = f"https://github.com/{update_check.GITHUB_REPO}/releases"
            flat_button(body, "前往 Releases 页", primary=True,
                        command=lambda: webbrowser.open(url)
                        ).pack(anchor="w", pady=(12, 0))
            return

        # 有结果:新版 / 已是最新,都展示该 release 的发布说明
        newer = update_check.is_newer(release["tag"], current_version)
        if newer:
            tk.Label(body, text="● 发现新版本", bg=theme.BG, fg=theme.SUCCESS,
                     font=theme.FONT_UI_BOLD).pack(anchor="w", pady=(4, 2))
            SelectableLabel(body,
                            text=f"当前 v{current_version}   →   最新 {release['tag']}",
                            bg=theme.BG, fg=theme.FG, font=theme.FONT_UI).pack(fill="x")
        else:
            tk.Label(body, text="● 已是最新版本", bg=theme.BG, fg=theme.SUCCESS,
                     font=theme.FONT_UI_BOLD).pack(anchor="w", pady=(4, 2))
            SelectableLabel(body, text=f"当前版本 v{current_version}(已是最新)",
                            bg=theme.BG, fg=theme.FG, font=theme.FONT_UI).pack(fill="x")
        if newer:
            # 按钮(立即更新 / 前往下载)置于发布说明**之前**:长文本会撑高滚动区,
            # 按钮放后面易被挤出可视行被裁;放顶部始终可见。
            from tcer.core import updater
            can_self = (controller is not None and getattr(sys, "frozen", False)
                        and updater.asset_for_current_platform(release) is not None)
            if can_self:
                self._update_btn = flat_button(
                    body, "立即更新", primary=True,
                    command=lambda: controller.start_self_update(release, self),
                    padx=theme.PAD_L, pady=theme.PAD_S)
                self._update_btn.pack(anchor="w", pady=(8, 0))
            else:
                flat_button(body, "前往下载", primary=True,
                            command=lambda: webbrowser.open(release.get("url") or "")
                            ).pack(anchor="w", pady=(8, 0))
        # 下载进度文字:按钮下方、发布说明上方(初始空,下载时由 set_progress 填字)
        self._progress = tk.Label(body, text="", bg=theme.BG, fg=theme.MUTED,
                                  font=theme.FONT_UI_SMALL, wraplength=400, justify="left")
        self._progress.pack(anchor="w", pady=(4, 0))
        # 发布说明(已清理 markdown):纯文本展示在进度下方
        notes = update_check.render_notes(release.get("notes") or "")
        if notes:
            sf = ScrollFrame(body, bg=theme.PANEL)
            sf.canvas.pack(fill="both", expand=True, pady=(8, 0))
            if len(notes) > self._NOTES_LIMIT:
                notes = notes[:self._NOTES_LIMIT].rstrip() + "\n…"
            SelectableLabel(sf.inner, text=notes, bg=theme.PANEL, fg=theme.FG,
                            font=theme.FONT_UI_SMALL,
                            justify="left").pack(fill="x")
        # 无底部「关闭」按钮:标题栏 × 即可关闭

    def set_progress(self, text):
        """供 controller 在主线程更新下载进度文字(label 已在按钮下方常驻)。"""
        try:
            self._progress.config(text=text)
        except tk.TclError:
            pass

    def offer_manual_download(self, url):
        """自动更新失败后,提供「前往下载」回退(浏览器下载更鲁棒、可走代理)。"""
        import webbrowser

        try:
            flat_button(self._body, "前往下载", primary=True,
                        command=lambda: webbrowser.open(url or "")
                        ).pack(anchor="w", pady=(8, 0))
        except tk.TclError:
            pass


class SessionDetailPopup:
    """会话详情 — metadata + per-model cost breakdown, unified card style."""

    _COST_COLOR = theme.WARNING  # 成本条暖橙（与 WARNING 同源）

    def __init__(self, parent, report) -> None:
        from tcer.core import metrics as metrics_mod
        from tcer.core.pricing import label as model_label

        r = report
        u = r.usage
        sid = (r.meta.session_id or r.meta.path.stem)[:16]
        win = _new_window(parent, f"会话详情 · {sid}…", "740x540")
        tk.Label(win, text="会话详情", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        # Summary header
        total_cost = metrics_mod.cost_usd(u)
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"{r.meta.title or '(无标题)'} · {fmt.models_label(u)}",
                 bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI_BOLD).pack()
        tk.Label(head, text=f"{u.total:,} Token · {fmt.fmt_money(total_cost)}",
                 bg=theme.CARD_HEADER_BG, fg=theme.SUCCESS, font=theme.FONT_UI).pack()

        # Metadata card
        def meta_row(key, val):
            row = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=2)
            row.pack(fill="x")
            tk.Label(row, text=key, bg=theme.PANEL, fg=theme.MUTED, width=10,
                     anchor="w", font=theme.FONT_UI).pack(side="left")
            SelectableLabel(row, text=str(val), bg=theme.PANEL, fg=theme.FG,
                            font=theme.FONT_UI,
                            justify="left").pack(side="left", fill="x", expand=True)

        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        meta_row("会话 ID", r.meta.session_id or "(无)")
        meta_row("工作目录", r.meta.cwd or "(未知)")
        meta_row("开始", fmt.fmt_dt(u.started_at, fmt.FMT_SECOND))
        meta_row("结束", fmt.fmt_dt(u.ended_at, fmt.FMT_SECOND))

        # Per-model cost section
        _SKIP = {"<synthetic>", ""}
        per_model = {k: v for k, v in u.per_model.items() if k not in _SKIP and k}

        if per_model:
            # Section header
            tk.Frame(inner, bg=theme.PANEL, height=10).pack(fill="x")
            sec = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=4)
            sec.pack(fill="x")
            tk.Label(sec, text="逐模型成本", bg=theme.PANEL, fg=theme.SECTION_ACCENT,
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
                bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=8)
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
            SelectableLabel(warn,
                            text=f"⚠ {r.unseen_writes} 个「未见文件的 Write」（LOC 假设为新文件，"
                                 "若覆写已有文件会高估 added）",
                            bg=theme.PANEL, fg=theme.WARNING, justify="left",
                            font=theme.FONT_UI).pack(fill="x")


class ToolCallsPopup:
    """工具调用统计 — per-tool call count with stacked bar (success / error)."""

    _COLORS = {
        "success": theme.ACCENT,   # blue
        "error":   theme.ERROR,    # red
    }

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        win = _new_window(parent, f"工具调用统计{title_suffix}", "740x540")
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
            head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
            head.pack(fill="x", pady=10)
            summary = f"总计 {total} 次调用 · {len(tc)} 种工具"
            if total_errs:
                summary += f" · {total_errs} 次错误"
            tk.Label(head, text=summary, bg=theme.CARD_HEADER_BG,
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
                bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=10)
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
        "input":          theme.TOKEN_COLORS["input"],
        "output":         theme.TOKEN_COLORS["output"],
        "cache_creation": theme.TOKEN_COLORS["cache_write"],
        "cache_read":     theme.TOKEN_COLORS["cache_read"],
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

        win = _new_window(parent, f"模型使用详情{title_suffix}", "740x540")
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
            head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
            head.pack(fill="x", pady=10)
            tk.Label(head, text=f"总计 {total_tokens:,} Token · {fmt_money(total_cost)} · "
                                f"{len(per_model)} 个模型",
                     bg=theme.CARD_HEADER_BG, fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()
            if unmatched:
                warn = tk.Frame(inner, bg=theme.WARN_TINT_BG, padx=10, pady=6)
                warn.pack(fill="x", pady=(0, 4))
                names = "、".join(pricing_mod.label(m) for m in unmatched[:6])
                more = f" 等 {len(unmatched)} 个" if len(unmatched) > 6 else ""
                SelectableLabel(
                    warn,
                    text=f"⚠ {len(unmatched)} 个模型未在价表中（按默认 list 价）：{names}{more}\n"
                         f"成本可能偏差；可在 tcer/config/model_pricing.json 补充条目。",
                    bg=theme.WARN_TINT_BG, fg=theme.WARNING,
                    font=theme.FONT_UI, justify="left",
                ).pack(fill="x")

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
                bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=10)
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

    _COLOR = theme.WARNING  # 成本条暖橙（与 WARNING 同源）
    # Top 3 efficiency — gold / purple / blue
    _MEDAL = list(theme.MEDAL_COLORS)

    def __init__(self, parent, usage, title_suffix: str = "") -> None:
        from tcer.core import metrics as metrics_mod
        from tcer.core.format import fmt_money
        from tcer.core.pricing import label as model_label

        win = _new_window(parent, f"成本明细{title_suffix}", "740x540")
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
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"总计 {fmt_money(total_cost)} · {len(per_model)} 个模型",
                 bg=theme.CARD_HEADER_BG, fg=self._COLOR, font=theme.FONT_UI_BOLD).pack()
        if unmatched:
            warn = tk.Frame(inner, bg=theme.WARN_TINT_BG, padx=10, pady=6)
            warn.pack(fill="x", pady=(0, 4))
            SelectableLabel(
                warn,
                text=f"⚠ {len(unmatched)} 个模型未在价表中，成本按默认 list 价估算（见各行「默认价」标记）。",
                bg=theme.WARN_TINT_BG, fg=theme.WARNING, font=theme.FONT_UI, justify="left",
            ).pack(fill="x")

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
            bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=10)
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


class BaselinesPopup:
    """个人基准校准 —— 上下布局、先开窗后校准、可选全部会话 / 逐项目 / 按数据源。

    交互：开窗即在（不预先计算）→ 用户选模式 + 是否过滤离群 → 点「开始校准」→
    后台计算完成回填结果 → 「应用为基准」写入。无「取消」按钮（标题栏 × / Esc 关闭）。

    回调契约（均由控制器提供，弹窗不碰数据）：
      on_compute(mode, filter_outliers, callback) —— 后台按模式汇总会话（忽略时间
        范围），完成后在主线程调 callback(result)。result:
          mode=="all"         → {"values": {...}, "n": int, "note": str}
          mode=="per_proj"    → {"per_project": {uid: {"values":{...}, "n":int, "label":str}},
                                 "note": str}
          mode=="per_source"  → {"per_source": [{"source": str, "label": str,
                                 "values": {...}|None, "n": int}], "note": str}
        任一模式 values 为 None 表示样本不足（result 带 "msg" 说明）。
      on_apply(mode, payload) —— mode=="all": payload={tcer,cpe} 写全局；
        mode=="per_proj": payload={uid: {tcer,cpe}} 写逐项目；
        mode=="per_source": payload={source: {tcer,cpe}} 写逐源（跨源公平比较）。
    """

    _COLOR = theme.BASELINE_ACCENT

    def __init__(self, parent, *, on_compute, on_apply,
                 current_project_label: str | None = None) -> None:
        self._on_compute = on_compute
        self._on_apply = on_apply
        self._result = None  # 最近一次计算结果（用于应用）

        win = _new_window(parent, "计算个人基准", "740x540")
        self._win = win
        tk.Label(win, text="计算个人基准", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(pady=(8, 4))

        # -- 操作按钮：紧跟标题下方（上下排列）--
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(fill="x", padx=10, pady=(0, 6))
        self._calc_btn = flat_button(btn_bar, "开始校准", self._do_compute,
                                     primary=True, padx=theme.PAD_L)
        self._calc_btn.pack(fill="x", pady=(0, 4))
        self._apply_btn = flat_button(btn_bar, "应用为基准", self._do_apply,
                                      padx=theme.PAD_L)
        self._apply_btn.pack(fill="x")
        self._set_apply_enabled(False)

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        inner = sf.inner
        self._inner = inner

        # -- 模式选择（醒目分区标题 + 上下堆叠可点行）--
        self._section_header(inner, "计算范围")
        self._mode = tk.StringVar(value="all")
        self._mode_rows: dict[str, CheckRow] = {}
        for val, text, hint in [
            ("all", "基于全部会话", "跨所有项目汇总一个统一基准"),
            ("per_proj", "逐项目分别校准", "为每个项目单独生成基准"),
            ("per_source", "按数据源分别校准", "跨源公平比较：每个 agent 用自己的中位水平做基准"),
        ]:
            var = tk.BooleanVar(value=(val == "all"))
            row = CheckRow(inner, text, var, on_toggle=lambda v=val: self._pick_mode(v),
                           hint=hint)
            self._mode_rows[val] = row

        # -- 计算选项（醒目分区标题）--
        self._section_header(inner, "计算选项")
        self._filter_outliers = tk.BooleanVar(value=True)
        CheckRow(inner, "忽略近零产出的离群会话", self._filter_outliers,
                 hint=f"净增行 < {metrics.MIN_BASELINE_NET_LOC} 的会话会失真",
                 tooltip="例如「10 行改动花 $17」这类会话，每千行成本被放大到失真，"
                         "计入会拉偏成本基准。默认忽略。")

        # 计算方法：中位数（抗离群）/ 平均数（对全体敏感），单选
        self._method = tk.StringVar(value="median")
        self._method_rows: dict[str, CheckRow] = {}
        for val, text, hint in [
            ("median", "用中位数", "取中间值，不受个别极端会话影响（推荐）"),
            ("mean", "用平均数", "全体会话的算术平均，对高/低值都敏感"),
        ]:
            var = tk.BooleanVar(value=(val == "median"))
            row = CheckRow(inner, text, var, on_toggle=lambda v=val: self._pick_method(v),
                           hint=hint)
            self._method_rows[val] = row

        SelectableLabel(inner, text="基准与筛选时间范围无关，始终基于全部历史会话。",
                        bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", padx=10, pady=(8, 2))

        # -- 结果区（校准后填充）--
        self._section_header(inner, "校准结果")
        self._result_frame = tk.Frame(inner, bg=theme.PANEL)
        self._result_frame.pack(fill="x", pady=(2, 0))
        tk.Label(self._result_frame, text="点上方「开始校准」计算基准。",
                 bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI,
                 anchor="w").pack(anchor="w", padx=10, pady=6)

        self._pick_mode("all")

    @staticmethod
    def _metric_label(key: str) -> str:
        """SSOT 指标名 + 单位（如「TCER（行/百万）」「千行代码成本（美元/千行）」）。
        取自 metric_defs.METRIC_BY_KEY——遵守中心指标守则，不自造相似名。"""
        m = METRIC_BY_KEY.get(key)
        if not m:
            return key
        return f"{m.name}（{m.unit}）" if m.unit else m.name

    @staticmethod
    def _metric_short(key: str) -> str:
        """SSOT 指标名（紧凑，逐项目一行两列用）。"""
        m = METRIC_BY_KEY.get(key)
        return m.name if m else key

    @staticmethod
    def _section_header(parent, text: str) -> None:
        """醒目分区标题：色条 + 加粗白字（区别于淡灰说明文字）。"""
        bar = tk.Frame(parent, bg=theme.PANEL)
        bar.pack(fill="x", padx=8, pady=(10, 3))
        tk.Frame(bar, bg=theme.BASELINE_ACCENT, width=3).pack(side="left", fill="y")
        tk.Label(bar, text=text, bg=theme.PANEL, fg=theme.FG_WHITE,
                 font=theme.FONT_UI_BOLD, anchor="w").pack(side="left", padx=(8, 0))

    def _pick_method(self, method: str) -> None:
        self._method.set(method)
        for val, row in self._method_rows.items():
            row.var.set(val == method)
            row._draw()
        # 换方法后旧结果失效
        self._result = None
        self._set_apply_enabled(False)

    # -- mode radio (single-select via CheckRow rows) --
    def _pick_mode(self, mode: str) -> None:
        self._mode.set(mode)
        for val, row in self._mode_rows.items():
            row.var.set(val == mode)
            row._draw()
        # 换模式后旧结果失效
        self._result = None
        self._set_apply_enabled(False)
        self._clear_results()

    def _set_apply_enabled(self, on: bool) -> None:
        try:
            self._apply_btn.config(state="normal" if on else "disabled")
        except tk.TclError:
            pass

    def _clear_results(self) -> None:
        for w in self._result_frame.winfo_children():
            w.destroy()

    # -- compute (delegates to controller; async) --
    def _do_compute(self) -> None:
        self._clear_results()
        self._result = None
        self._set_apply_enabled(False)
        tk.Label(self._result_frame, text="校准中…", bg=theme.PANEL,
                 fg=theme.MUTED, font=theme.FONT_UI).pack(anchor="w", padx=10, pady=8)
        try:
            self._calc_btn.config(state="disabled")
        except tk.TclError:
            pass
        self._on_compute(self._mode.get(), bool(self._filter_outliers.get()),
                         self._method.get(), self._on_computed)

    def _on_computed(self, result: dict) -> None:
        """主线程回调：渲染结果。"""
        try:
            self._calc_btn.config(state="normal")
        except tk.TclError:
            return  # 窗口已关
        self._clear_results()
        self._result = result
        mode = self._mode.get()
        if mode == "all":
            self._render_all(result)
        elif mode == "per_source":
            self._render_per_source(result)
        else:
            self._render_per_project(result)

    def _render_all(self, result: dict) -> None:
        note = result.get("note") or ""
        values = result.get("values")
        if values is None:
            SelectableLabel(self._result_frame, text=result.get("msg", "样本不足，无法计算基准。"),
                            bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI,
                            justify="left").pack(fill="x", padx=10, pady=8)
            return
        head = tk.Frame(self._result_frame, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=(6, 0))
        tk.Label(head, text=f"基于 {result.get('n', 0)} 个会话计算 · {note}".rstrip(" ·"),
                 bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI_BOLD,
                 wraplength=420, justify="left").pack(anchor="w")
        current = {"tcer": metrics.TCER_BASELINE, "cpe": metrics.CPE_BASELINE}
        for key in ("tcer", "cpe"):
            self._value_card(self._result_frame, key, values[key], current.get(key))
        self._set_apply_enabled(True)

    def _render_per_project(self, result: dict) -> None:
        note = result.get("note") or ""
        # per_project 现为**有序列表**，含全部项目（样本不足者 values=None）。
        per = result.get("per_project") or []
        if not per:
            SelectableLabel(self._result_frame, text=result.get("msg", "没有可用于计算的项目。"),
                            bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI,
                            justify="left").pack(fill="x", padx=10, pady=8)
            return
        n_ok = sum(1 for it in per if it.get("values"))
        head = tk.Frame(self._result_frame, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=(2, 0))
        tk.Label(head, text=f"共 {len(per)} 个项目 · {n_ok} 个可校准 · {note}".rstrip(" ·"),
                 bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI_BOLD,
                 wraplength=440, justify="left").pack(anchor="w")
        for info in per:
            vals = info.get("values")
            ok = bool(vals)
            tk.Frame(self._result_frame, bg=theme.PANEL, height=6).pack(fill="x")
            title = tk.Frame(self._result_frame, bg=theme.PANEL)
            title.pack(fill="x", padx=10)
            tk.Label(title, text=info.get("label", info.get("uid", "")),
                     bg=theme.PANEL, fg=theme.FG if ok else theme.MUTED,
                     font=theme.FONT_UI_BOLD, anchor="w").pack(side="left")
            tk.Label(title, text=f"{info.get('n', 0)} 会话", bg=theme.PANEL,
                     fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e").pack(side="right")
            if ok:
                line = tk.Frame(self._result_frame, bg=theme.PANEL)
                line.pack(fill="x", padx=10)
                tk.Label(line, text=f"{self._metric_short('tcer')} {vals.get('tcer', 0):.2f}",
                         bg=theme.PANEL, fg=self._COLOR, font=theme.FONT_MONO,
                         anchor="w").pack(side="left")
                tk.Label(line, text=f"{self._metric_short('cpe')} {vals.get('cpe', 0):.2f}",
                         bg=theme.PANEL, fg=self._COLOR, font=theme.FONT_MONO,
                         anchor="e").pack(side="right")
            else:
                SelectableLabel(self._result_frame,
                                text=info.get("reason", "样本不足，跳过"),
                                bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI
                                ).pack(fill="x", padx=10)
        if n_ok:
            self._set_apply_enabled(True)

    def _render_per_source(self, result: dict) -> None:
        note = result.get("note") or ""
        per = result.get("per_source") or []
        if not per:
            SelectableLabel(self._result_frame, text=result.get("msg", "没有可用于计算的会话。"),
                            bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI,
                            justify="left").pack(fill="x", padx=10, pady=8)
            return
        n_ok = sum(1 for it in per if it.get("values"))
        head = tk.Frame(self._result_frame, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=(2, 0))
        tk.Label(head,
                 text=f"共 {len(per)} 个数据源 · {n_ok} 个可校准 · {note}".rstrip(" ·"),
                 bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI_BOLD,
                 wraplength=440, justify="left").pack(anchor="w")
        SelectableLabel(self._result_frame,
                        text="逐源基准写入后，各源分数回到「相对该源自己的中位水平」——"
                             "跨源比较才公平；不写入则所有源共用全局基准。",
                        bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", padx=10, pady=(4, 0))
        for info in per:
            vals = info.get("values")
            ok = bool(vals)
            tk.Frame(self._result_frame, bg=theme.PANEL, height=6).pack(fill="x")
            title = tk.Frame(self._result_frame, bg=theme.PANEL)
            title.pack(fill="x", padx=10)
            tk.Label(title, text=info.get("label", info.get("source", "")),
                     bg=theme.PANEL, fg=theme.FG if ok else theme.MUTED,
                     font=theme.FONT_UI_BOLD, anchor="w").pack(side="left")
            tk.Label(title, text=f"{info.get('n', 0)} 会话", bg=theme.PANEL,
                     fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e").pack(side="right")
            if ok:
                line = tk.Frame(self._result_frame, bg=theme.PANEL)
                line.pack(fill="x", padx=10)
                tk.Label(line, text=f"{self._metric_short('tcer')} {vals.get('tcer', 0):.2f}",
                         bg=theme.PANEL, fg=self._COLOR, font=theme.FONT_MONO,
                         anchor="w").pack(side="left")
                tk.Label(line, text=f"{self._metric_short('cpe')} {vals.get('cpe', 0):.2f}",
                         bg=theme.PANEL, fg=self._COLOR, font=theme.FONT_MONO,
                         anchor="e").pack(side="right")
            else:
                SelectableLabel(self._result_frame,
                                text=info.get("reason", "样本不足，跳过"),
                                bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI
                                ).pack(fill="x", padx=10)
        if n_ok:
            self._set_apply_enabled(True)

    def _value_card(self, parent, key, val, cur) -> None:
        tk.Frame(parent, bg=theme.PANEL, height=6).pack(fill="x")
        card = tk.Frame(parent, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x")
        hdr = tk.Frame(card, bg=theme.PANEL)
        hdr.pack(fill="x")
        tk.Label(hdr, text=self._metric_label(key), bg=theme.PANEL, fg=theme.FG,
                 anchor="w", font=theme.FONT_VALUE).pack(side="left")
        tk.Label(hdr, text=f"{val:.3f}", bg=theme.PANEL, fg=self._COLOR,
                 anchor="e", font=theme.FONT_MONO).pack(side="right")
        if cur:
            diff_pct = (val - cur) / cur * 100
            better = diff_pct < 0 if key == "cpe" else diff_pct > 0
            diff_fg = theme.SUCCESS if better else theme.ERROR
            cmp_row = tk.Frame(card, bg=theme.PANEL)
            cmp_row.pack(fill="x")
            tk.Label(cmp_row, text=f"中位数 · 当前基准 {cur:.3f}", bg=theme.PANEL,
                     fg=theme.MUTED, font=(theme.FONT_MONO_NAME, 8)).pack(side="left")
            tk.Label(cmp_row, text=f"{diff_pct:+.1f}%", bg=theme.PANEL, fg=diff_fg,
                     font=(theme.FONT_MONO_NAME, 8, "bold")).pack(side="right")

    def _do_apply(self) -> None:
        if not self._result:
            return
        mode = self._mode.get()
        if mode == "all":
            values = self._result.get("values")
            if not values:
                return
            self._on_apply("all", values)
        elif mode == "per_source":
            per = self._result.get("per_source") or []
            payload = {info["source"]: info["values"]
                       for info in per if info.get("values")}
            if not payload:
                return
            self._on_apply("per_source", payload)
        else:
            per = self._result.get("per_project") or []
            payload = {info["uid"]: info["values"] for info in per if info.get("values")}
            if not payload:
                return
            self._on_apply("per_proj", payload)
        self._win.destroy()


class HtmlSectionsPopup:
    """HTML 报告选节 — 导出项目报告前勾选要包含的章节（默认全选 = 旧行为）。"""

    def __init__(self, parent, on_ok) -> None:
        from .html_report import PROJECT_SECTIONS, _SECTION_LABELS
        win = _new_window(parent, "HTML 报告章节", "520x360")
        tk.Label(win, text="选择要包含的章节", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        inner = tk.Frame(win, bg=theme.PANEL)
        inner.pack(fill="both", expand=True, padx=10, pady=5)
        card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=10)
        card.pack(fill="x")
        vars_ = {}
        for key in PROJECT_SECTIONS:
            v = tk.BooleanVar(value=True)
            vars_[key] = v
            CheckRow(card, _SECTION_LABELS.get(key, key), v)

        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        flat_button(btn_bar, "继续导出",
                    lambda: (on_ok([k for k, v in vars_.items() if v.get()]),
                             win.destroy()),
                    primary=True, padx=theme.PAD_L).pack(side="left", padx=theme.PAD_S)
        flat_button(btn_bar, "取消", win.destroy,
                    padx=theme.PAD_L).pack(side="left", padx=theme.PAD_S)


class AdvancedPopup:
    """高级选项 — 跳过 LOC 开关（产品定位：只分析会话数据，无仓库扫描项）。"""

    def __init__(self, parent, no_loc: bool, on_apply) -> None:
        win = _new_window(parent, "高级选项", "520x360")
        tk.Label(win, text="高级选项", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        inner = tk.Frame(win, bg=theme.PANEL)
        inner.pack(fill="both", expand=True, padx=10, pady=10)

        card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=10)
        card.pack(fill="x")
        no_loc_var = tk.BooleanVar(value=no_loc)
        CheckRow(card, "跳过 LOC（仅 Token 指标，不算 TCER/CPE/综合效率分）", no_loc_var)
        SelectableLabel(card,
                        text="全部指标均来自会话数据回放；TCER 不读取真实仓库、不依赖 git。",
                        bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", pady=(6, 0))

        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(pady=8)
        flat_button(btn_bar, "应用并重算",
                    lambda: (on_apply(no_loc_var.get()), win.destroy()),
                    primary=True, padx=theme.PAD_L).pack(side="left", padx=theme.PAD_S)
        flat_button(btn_bar, "取消", win.destroy,
                    padx=theme.PAD_L).pack(side="left", padx=theme.PAD_S)


class UserMsgsPopup:
    """用户消息 — 卡片式列表。

    ``messages`` 支持两种形态：
      * ``list[str]``：单会话视图，扁平消息列表。
      * ``list[tuple[str, list[str]]]``：聚合视图，``(会话标识, 该会话消息)``
        分组；每组前渲染一条来源标识条（标题 + sessionid，均已限长）。
    """

    _ACCENT = theme.CHART_PALETTE[0]  # blue accent for badges

    def __init__(self, parent, messages) -> None:
        groups = self._normalize(messages)
        total = sum(len(msgs) for _, msgs in groups)
        total_chars = sum(len(m) for _, msgs in groups for m in msgs)
        win = _new_window(parent, "用户消息", "740x540")
        tk.Label(win, text="用户消息", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=10).pack()

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        if not total:
            tk.Label(inner, text="未记录到用户消息", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
            return

        # Summary header
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=10)
        summary = f"共 {total} 条消息 · {total_chars:,} 字符"
        grouped = len(groups) > 1 or (len(groups) == 1 and groups[0][0] is not None)
        if grouped:
            summary += f" · {len(groups)} 个会话"
        tk.Label(head, text=summary, bg=theme.CARD_HEADER_BG, fg=theme.SUCCESS,
                 font=theme.FONT_UI_BOLD).pack()

        idx = 0
        for label, msgs in groups:
            if label is not None:
                self._session_bar(inner, label, len(msgs))
            for txt in msgs:
                idx += 1
                self._msg_card(inner, idx, txt)

    @staticmethod
    def _normalize(messages) -> "list[tuple[str | None, list[str]]]":
        """Coerce either input shape into ``[(label|None, [msg, ...]), ...]``."""
        if messages and isinstance(messages[0], tuple):
            return [(lbl, list(msgs)) for lbl, msgs in messages]
        return [(None, list(messages))]

    def _session_bar(self, parent, label: str, count: int) -> None:
        bar = tk.Frame(parent, bg=theme.BG, padx=10, pady=5)
        bar.pack(fill="x", pady=(10, 2))
        tk.Label(bar, text=label, bg=theme.BG, fg=self._ACCENT,
                 font=theme.FONT_UI_BOLD, anchor="w").pack(side="left")
        tk.Label(bar, text=f"{count} 条", bg=theme.BG, fg=theme.MUTED,
                 font=(theme.FONT_MONO_NAME, 8)).pack(side="right")

    def _msg_card(self, parent, idx: int, txt: str) -> None:
        card = tk.Frame(parent, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        card.pack(fill="x", pady=4)
        hdr = tk.Frame(card, bg=theme.CARD_HEADER_BG)
        hdr.pack(fill="x")
        badge = tk.Label(hdr, text=f"#{idx}", bg=self._ACCENT, fg=theme.FG_WHITE,
                         font=(theme.FONT_MONO_NAME, 8, "bold"), padx=6, pady=1)
        badge.pack(side="left")
        tk.Label(hdr, text=f"{len(txt)} 字符", bg=theme.CARD_HEADER_BG, fg=theme.MUTED,
                 font=(theme.FONT_MONO_NAME, 8)).pack(side="right")
        SelectableLabel(card, text=txt, bg=theme.CARD_HEADER_BG, fg=theme.FG,
                        font=theme.FONT_UI, justify="left",
                        width=60).pack(fill="x", pady=(4, 0))


class FilesTouchedPopup:
    """涉及文件 — all files read/written/edited, with proportional bars.

    每个文件按产出分类染色：代码=蓝、测试=紫、文档=青（与 loc 的
    _is_test_file / _is_doc_file / _is_code 判定一致）。"""

    _COLOR_CODE = theme.CHART_PALETTE[0]   # 蓝 — 代码
    _COLOR_TEST = theme.CHART_PALETTE[4]   # 紫 — 测试
    _COLOR_DOC = theme.TOKEN_COLORS["cache_read"]  # 青 — 文档
    _COLOR_OTHER = theme.MUTED             # 灰 — 非产出（只读的图片/二进制等）

    @classmethod
    def _file_kind(cls, fp: str):
        """(色, 类别名)：测试 / 文档 / 代码 / 其他（非产出）。"""
        from tcer.core.loc import _is_test_file, _is_doc_file, _is_code
        if _is_test_file(fp):
            return cls._COLOR_TEST, "测试"
        if _is_doc_file(fp):
            return cls._COLOR_DOC, "文档"
        if _is_code(fp):
            return cls._COLOR_CODE, "代码"
        return cls._COLOR_OTHER, "其他"

    def __init__(self, parent, details: dict[str, int],
                 searched: dict[str, int] | None = None) -> None:
        win = _new_window(parent, "涉及文件", "740x540")
        tk.Label(win, text=f"涉及文件（共 {len(details)} 个）", bg=theme.BG,
                 fg=theme.FG, font=theme.FONT_HEADING, pady=10).pack()
        SelectableLabel(win, text="会话中被读取、写入或编辑过的文件及操作次数。",
                        bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                        justify="left").pack(fill="x")

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        inner = sf.inner

        sorted_items = sorted(details.items(), key=lambda x: x[1], reverse=True)
        total_ops = sum(details.values())
        max_cnt = sorted_items[0][1] if sorted_items else 1

        # Summary header
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"共 {len(details)} 个文件 · 合计 {total_ops} 次操作",
                 bg=theme.CARD_HEADER_BG, fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

        # 目录热度：按父目录聚合操作次数（哪个模块最烫）。
        dir_counts: dict[str, int] = {}
        for fp, cnt in details.items():
            parent = str(Path(fp).parent).replace("\\", "/")
            if parent in (".", "/"):
                parent = "(根目录)"
            dir_counts[parent] = dir_counts.get(parent, 0) + cnt
        top_dirs = sorted(dir_counts.items(), key=lambda x: -x[1])[:6]
        if len(dir_counts) > 1:
            sec = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=4)
            sec.pack(fill="x")
            tk.Label(sec, text="目录热度", bg=theme.PANEL, fg=theme.SECTION_ACCENT,
                     font=theme.FONT_UI_BOLD).pack(anchor="w")
            max_dir = top_dirs[0][1]
            for d, cnt in top_dirs:
                d_disp = d if len(d) < 50 else "…" + d[-47:]
                row = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=1)
                row.pack(fill="x")
                SelectableLabel(row, text=d_disp, bg=theme.PANEL, fg=theme.MUTED,
                                font=(theme.FONT_MONO_NAME, 8),
                                width=48).pack(side="left")
                bar_bg = tk.Frame(row, bg=theme.CONTROL_BG, height=6)
                bar_bg.pack(side="left", fill="x", expand=True, padx=4)
                tk.Frame(bar_bg, bg=theme.WARNING, height=6).place(
                    relx=0, rely=0, relwidth=cnt / max_dir, relheight=1.0)
                tk.Label(row, text=str(cnt), bg=theme.PANEL, fg=theme.MUTED,
                         font=(theme.FONT_MONO_NAME, 8), width=5,
                         anchor="e").pack(side="right")
            tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")

        # 搜索足迹：被 Grep/Glob 扫过的路径（含目录）——AI 的探索范围，与上方
        # 「文件列表」（真实读/写/改的文件）分开，避免把搜索目录误当文件计数。
        if searched:
            top_searched = sorted(searched.items(), key=lambda x: -x[1])[:8]
            sec = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=4)
            sec.pack(fill="x")
            tk.Label(sec, text=f"搜索足迹（{len(searched)} 处）",
                     bg=theme.PANEL, fg=theme.SECTION_ACCENT,
                     font=theme.FONT_UI_BOLD).pack(anchor="w")
            SelectableLabel(sec, text="Grep/Glob 扫过的路径及次数，反映探索范围（不计入涉及文件）。",
                            bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL,
                            justify="left").pack(fill="x")
            max_s = top_searched[0][1]
            for p, cnt in top_searched:
                p_disp = p.replace("\\", "/")
                p_disp = p_disp if len(p_disp) < 50 else "…" + p_disp[-47:]
                row = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=1)
                row.pack(fill="x")
                SelectableLabel(row, text=p_disp, bg=theme.PANEL, fg=theme.MUTED,
                                font=(theme.FONT_MONO_NAME, 8),
                                width=48).pack(side="left")
                bar_bg = tk.Frame(row, bg=theme.CONTROL_BG, height=6)
                bar_bg.pack(side="left", fill="x", expand=True, padx=4)
                tk.Frame(bar_bg, bg=theme.SECTION_ACCENT, height=6).place(
                    relx=0, rely=0, relwidth=cnt / max_s, relheight=1.0)
                tk.Label(row, text=str(cnt), bg=theme.PANEL, fg=theme.MUTED,
                         font=(theme.FONT_MONO_NAME, 8), width=5,
                         anchor="e").pack(side="right")
            tk.Frame(inner, bg=theme.PANEL, height=8).pack(fill="x")

        # 分类图例（代码/测试/文档三色）
        legend = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
        legend.pack(fill="x")
        for color, name in ((self._COLOR_CODE, "代码"), (self._COLOR_TEST, "测试"),
                            (self._COLOR_DOC, "文档")):
            sw = tk.Frame(legend, bg=color, width=10, height=10)
            sw.pack(side="left", padx=(0, 3))
            sw.pack_propagate(False)
            tk.Label(legend, text=name, bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 12))

        for fp, cnt in sorted_items:
            display = fp if len(fp) < 55 else "…" + fp[-52:]
            color, kind = self._file_kind(fp)

            tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
            hdr = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            hdr.pack(fill="x")
            # 类别标记（彩色小方块）+ 文件名
            sw = tk.Frame(hdr, bg=color, width=8, height=8)
            sw.pack(side="left", padx=(0, 5))
            sw.pack_propagate(False)
            SelectableLabel(hdr, text=display, bg=theme.PANEL, fg=theme.FG,
                            font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
            tk.Label(hdr, text=f"{cnt} 次", bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                     font=theme.FONT_MONO).pack(side="right")

            bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            bar_frame.pack(fill="x")
            bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=8)
            bar_bg.pack(fill="x")
            tk.Frame(bar_bg, bg=color, height=8).place(
                relx=0, rely=0, relwidth=cnt / max_cnt, relheight=1.0)


class MemoryFilesPopup:
    """项目记忆文件 — 展示 memory/ 下的文件列表，带跳转到目录按钮。

    风格与 FilesTouchedPopup 一致（卡片 + 比例条），多一个「打开目录」按钮。
    """

    _COLOR = theme.CHART_PALETTE[4]  # purple accent for memory files

    def __init__(self, parent, memory_dir: str, files: list[str]) -> None:
        from .platform import open_in_file_manager, FILE_MANAGER_NAME

        count = len(files)
        win = _new_window(parent, "项目记忆文件", "740x540")
        tk.Label(win, text=f"项目记忆文件（{count} 个）", bg=theme.BG,
                 fg=theme.FG, font=theme.FONT_HEADING, pady=10).pack()
        SelectableLabel(win, text=f"路径：{memory_dir}",
                        bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                        justify="left").pack(fill="x")

        # 按钮栏：打开目录（居中）
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(fill="x", padx=10, pady=(4, 8))
        from .views import ui_icon
        flat_button(btn_bar, f"在{FILE_MANAGER_NAME}中打开目录",
                    lambda: open_in_file_manager(memory_dir),
                    padx=theme.PAD_L, image=ui_icon(btn_bar, "folder"),
                    compound="left").pack(anchor="center")

        sf = ScrollFrame(win, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        inner = sf.inner

        # Summary header
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=8)
        head.pack(fill="x", pady=10)
        tk.Label(head, text=f"共 {count} 个文件 · memory/",
                 bg=theme.CARD_HEADER_BG, fg=theme.SUCCESS, font=theme.FONT_UI_BOLD).pack()

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
            SelectableLabel(hdr, text=name, bg=theme.PANEL, fg=theme.FG,
                            font=theme.FONT_MONO).pack(side="left", fill="x", expand=True)
            size_txt = f"{size:,} B" if size < 1024 else f"{size / 1024:.1f} KB"
            tk.Label(hdr, text=size_txt, bg=theme.PANEL, fg=theme.MUTED, anchor="e",
                     font=theme.FONT_MONO).pack(side="right")

            bar_frame = tk.Frame(inner, bg=theme.PANEL, padx=8, pady=2)
            bar_frame.pack(fill="x")
            bar_bg = tk.Frame(bar_frame, bg=theme.CONTROL_BG, height=8)
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
    #
    # ``ref=None`` means "resolve from the live SSOT at construction time" (see
    # ``_resolve_axes``): TCER / CPE baselines (``metrics.TCER_BASELINE`` /
    # ``CPE_BASELINE``). Hardcoding them drifted stale after the config moved and
    # broke silently after "保存个人基准" — the SSOT is the only correct source.
    # 综合效率分本身有界 0–100，用 "score" 归一（÷100），无需参照基准。
    _AXES = [
        ("score", "score",     100.0),
        ("chr",   "pct100",    1.0),
        ("cpe",   "grade_inv", None),
        ("churn", "pct_inv",   1.0),
        ("read_write_ratio", "ratio", 3.0),
        ("tcer",  "grade",     None),
    ]

    @staticmethod
    def _resolve_axes():
        """Bind live baselines into the axis refs (SSOT, not frozen)."""
        refs = {
            "tcer": metrics.TCER_BASELINE,
            "cpe": metrics.CPE_BASELINE,
        }
        return [
            (key, ntype, refs[key] if ref is None else ref)
            for key, ntype, ref in RadarPopup._AXES
        ]

    def __init__(self, parent, report, all_reports) -> None:
        import math
        from .metric_defs import raw_value as metric_raw_value
        from .metric_defs import display as metric_display, METRIC_BY_KEY

        sid = (report.meta.session_id or report.meta.path.stem)[:16]
        win = _new_window(parent, f"效率雷达 · {sid}…", "520x560")
        win.bind("<Escape>", lambda e: win.destroy())  # 无显式关闭按钮,Esc 兜底
        tk.Label(win, text="六维效率雷达", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=8).pack()

        # Summary header — 综合效率分 string straight from the SSOT (matches 指标分类).
        head = tk.Frame(win, bg=theme.CARD_HEADER_BG, padx=10, pady=6)
        head.pack(fill="x", padx=10, pady=(0, 4))
        tier_ = report.tier or "-"
        score_val = metric_display(report, "score")
        tk.Label(head, text=f"{report.meta.title or sid}  效率分 {score_val}  评级 {tier_}",
                 bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI).pack()

        # Radar canvas
        canvas = tk.Canvas(win, bg=theme.PANEL, highlightthickness=0,
                           width=400, height=400)
        canvas.pack(padx=16, pady=8)

        # Normalize each axis to 0-1 using absolute scales; label + value from SSOT.
        axis_data = []
        for key, ntype, ref in self._resolve_axes():
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
            canvas.create_polygon(pts, outline=theme.BORDER, fill="", dash=(2, 3))
        canvas.create_text(cx + R * 0.52, cy - 4, text="50%",
                           fill="#444444", font=theme.FONT_MONO)

        # Axes + labels
        for ai, (key, label, value_text, norm) in enumerate(axis_data):
            angle = math.pi / 2 + 2 * math.pi * ai / n
            ex = cx + R * math.cos(angle)
            ey = cy - R * math.sin(angle)
            canvas.create_line(cx, cy, ex, ey, fill=theme.BORDER)
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
                              fill=theme.RADAR_FILL, width=2)
        for ai in range(0, len(data_pts), 2):
            px, py = data_pts[ai], data_pts[ai + 1]
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3,
                               fill=theme.ACCENT, outline=theme.FG)

        canvas.create_text(cx, 14, text="绝对刻度，外圈 = 100%",
                           fill=theme.MUTED, font=theme.FONT_UI_SMALL)
        # 无显式「关闭」按钮:标题栏 × / Esc 即可

    @staticmethod
    def _normalize(raw, ntype, ref):
        """Normalize raw value to 0-1 using absolute scale."""
        if raw is None:
            return 0.0
        if ntype == "score":
            return max(0.0, min(1.0, raw / ref))  # 综合效率分 0–100 → 0–1
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

    _DANGER = theme.DANGER
    _DANGER_ACTIVE = theme.DANGER_ACTIVE

    def __init__(self, parent, *, title: str, session_id: str, on_confirm) -> None:
        win = _new_window(parent, "删除会话", "520x360")
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
        SelectableLabel(body, text=f"将永久删除本地会话“{disp_title}”",
                        bg=theme.BG, fg=theme.FG, font=theme.FONT_UI,
                        justify="left").pack(fill="x", pady=(2, 0))
        SelectableLabel(body, text=f"Session ID: {session_id}",
                        bg=theme.BG, fg=theme.MUTED, font=theme.FONT_MONO,
                        justify="left").pack(fill="x", pady=(2, 0))
        SelectableLabel(body, text="将一并删除其 subagent 与 tool-results 数据，此操作不可恢复。",
                        bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                        justify="left").pack(fill="x", pady=(12, 0))

        # 按钮行（右对齐）
        btn_bar = tk.Frame(win, bg=theme.BG)
        btn_bar.pack(fill="x", padx=20, pady=(8, 16))

        def _do_delete():
            win.destroy()
            on_confirm()

        del_btn = tk.Button(btn_bar, text="删除会话", command=_do_delete,  # style-exempt: style.md §3 豁免：删除确认红色警示
                            bg=self._DANGER, fg=theme.FG_WHITE, relief="flat",
                            activebackground=self._DANGER_ACTIVE, activeforeground=theme.FG_WHITE,
                            padx=16, pady=5, font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR)
        del_btn.pack(side="right")
        cancel_btn = tk.Button(btn_bar, text="取消", command=win.destroy,  # style-exempt: style.md §3 豁免：删除确认
                              bg=theme.PANEL_2, fg=theme.FG, relief="flat",
                              activebackground=theme.PANEL, activeforeground=theme.FG,
                              padx=16, pady=5, font=theme.FONT_UI, cursor=CLICK_CURSOR)
        cancel_btn.pack(side="right", padx=(0, 8))

        win.bind("<Escape>", lambda e: win.destroy())
        cancel_btn.focus_set()          # 默认聚焦取消，回车不会误删
        win.grab_set()                  # 模态


class UploadDialog:
    """上传到 TCER Server — 「配置 + 选项目 + 上传」。

    服务器地址 / Auth Token / 是否附带明细 / 自动上传在此**可编辑**，保存后写回
    ``tcer_ui.json`` 的 ``upload`` 段（``on_save_config``）。
    项目列表按照工作目录分组展示，同目录的多个 agent 集中排布；各 agent 采用与主界面
    一致的 16×16 图标（source_icon，复用资产）取代中括号文字标签。
    上传按钮前设有自动上传勾选框，勾选后客户端按记录的上传时间戳每小时自动检查并上传新会话。
    """

    def __init__(self, parent, *, prefs: dict, projects: list,
                 default_project: str | None, on_upload, on_save_prefs,
                 on_save_config, config: dict) -> None:
        self._on_upload = on_upload
        self._on_save_prefs = on_save_prefs
        self._on_save_config = on_save_config
        self._projects = projects
        self._default_url = str(config.get("default_url") or "")

        win = _new_window(parent, "上传到 TCER Server", "760x580")
        self._win = win
        tk.Label(win, text="上传到 TCER Server", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING, pady=8).pack(side="top", fill="x")

        # 底部操作区与状态行先 pack 在底部，确保任何高度下都固定可见
        action = tk.Frame(win, bg=theme.BG)
        action.pack(side="bottom", fill="x", padx=16, pady=(4, 8))

        # -- 上传按钮前的自动上传勾选框 --
        self._auto_upload_var = tk.BooleanVar(value=bool(config.get("auto_upload", False)))
        auto_row = tk.Frame(action, bg=theme.BG, cursor=CLICK_CURSOR)
        auto_row.pack(fill="x", pady=(0, 6))
        self._auto_box, toggle_auto = self._create_checkbox_widget(
            auto_row, self._auto_upload_var, on_toggle=self._on_toggle_auto_upload)
        self._auto_box.pack(side="left", padx=(0, 6))
        self._auto_chk = self._auto_box  # 兼容别名
        auto_lbl = tk.Label(
            auto_row, text="每小时自动检查并上传新会话（保持当前所选列表与详情设置）",
            bg=theme.BG, fg=theme.FG, font=theme.FONT_UI, anchor="w",
            cursor=CLICK_CURSOR)
        auto_lbl.pack(side="left")
        auto_row.bind("<Button-1>", toggle_auto)
        auto_lbl.bind("<Button-1>", toggle_auto)
        Tooltip(auto_row, "勾选后客户端将在后台按照记录的上传时间戳，每小时自动检查所选项目是否有新会话，并自动上传")

        self._upload_btn = tk.Button(  # style-exempt: style.md §3 豁免：UploadDialog
            action, text="立即上传", command=self._do_upload, bg=theme.ACCENT,
            fg=theme.FG_WHITE, relief="flat", padx=16, pady=6, font=theme.FONT_UI_BOLD,
            cursor=CLICK_CURSOR)
        self._upload_btn.pack(fill="x")

        # -- 状态行 --
        self._status = tk.Label(win, text="", bg=theme.BG, fg=theme.MUTED,
                                font=theme.FONT_UI, wraplength=520, justify="left")
        self._status.pack(side="bottom", fill="x", padx=12, pady=(2, 2))

        # -- 中间可滚动表单区 --
        sf = ScrollFrame(win, bg=theme.PANEL)
        self._sf = sf  # 供 _fit_window 读取表单实际高度
        sf.canvas.pack_forget()
        sf.canvas.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 4))
        inner = sf.inner

        # -- 上传目标卡片（可编辑）：服务器地址 / Auth Token / 是否含明细 --
        cfg_card = self._card(inner, "上传目标（留空则用默认；改动点「立即上传」或勾选自动上传时保存）")
        self._url_var = tk.StringVar(value=str(config.get("url") or ""))
        self._token_var = tk.StringVar(value=str(config.get("auth_token") or ""))
        self._detail_var = tk.BooleanVar(value=bool(config.get("detail")))
        self._labeled_entry(cfg_card, "服务器地址", self._url_var,
                            placeholder=f"默认 {self._default_url}")
        self._labeled_entry(cfg_card, "Auth Token", self._token_var,
                            placeholder="留空 = 匿名上传")

        detail_row = tk.Frame(cfg_card, bg=theme.PANEL, cursor=CLICK_CURSOR)
        detail_row.pack(fill="x", pady=(4, 0))
        self._detail_box, toggle_detail = self._create_checkbox_widget(
            detail_row, self._detail_var, on_toggle=self._on_detail_toggle)
        self._detail_box.pack(side="left", padx=(0, 6))
        detail_lbl = tk.Label(
            detail_row, text="附带每会话明细（完整对话）",
            bg=theme.PANEL, fg=theme.FG, font=theme.FONT_UI, anchor="w",
            cursor=CLICK_CURSOR)
        detail_lbl.pack(side="left")
        detail_row.bind("<Button-1>", toggle_detail)
        detail_lbl.bind("<Button-1>", toggle_detail)

        # -- 项目选择卡（按目录分组显示，同目录多个 agent 集中排布，默认收起紧凑展示） --
        card3 = self._card(inner, "项目选择（按工作目录分组，默认收起；可展开查看与选择各 Agent）")

        # 构建分组数据与预选
        self._groups = self._build_groups(projects)
        self._proj_vars: dict[str, tk.BooleanVar] = {}
        self._box_widgets: dict[str, tk.Widget] = {}
        self._hidden_keys: set[str] = set(prefs.get("hidden_dirs") or [])
        self._show_hidden: bool = False
        all_uids = [it["uid"] for g in self._groups for it in g["items"]]

        preselect = set(prefs.get("last_projects") or [])
        if not preselect and default_project:
            preselect.add(default_project)
        if not preselect and all_uids:
            preselect.add(all_uids[0])

        for uid in all_uids:
            self._proj_vars[uid] = tk.BooleanVar(value=(uid in preselect))

        # 顶部工具栏（全选、清空、全部展开、全部收起、显示隐藏、已选计数）
        sel_btn_row = tk.Frame(card3, bg=theme.PANEL)
        sel_btn_row.pack(fill="x", pady=(0, 6))
        tk.Button(sel_btn_row, text="全选",  # style-exempt: style.md §3 豁免：UploadDialog
                  command=self._select_all,
                  bg=theme.PANEL_2, fg=theme.FG, relief="flat", padx=8,
                  font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 4))
        tk.Button(sel_btn_row, text="清空",  # style-exempt: style.md §3 豁免：UploadDialog
                  command=self._clear_all,
                  bg=theme.PANEL_2, fg=theme.FG, relief="flat", padx=8,
                  font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 8))
        tk.Button(sel_btn_row, text="全部展开",  # style-exempt: style.md §3 豁免：UploadDialog
                  command=self._expand_all,
                  bg=theme.PANEL, fg=theme.MUTED, relief="flat", padx=6,
                  font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 4))
        tk.Button(sel_btn_row, text="全部收起",  # style-exempt: style.md §3 豁免：UploadDialog
                  command=self._collapse_all,
                  bg=theme.PANEL, fg=theme.MUTED, relief="flat", padx=6,
                  font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 4))
        self._show_hidden_btn = tk.Button(  # style-exempt: style.md §3 豁免：UploadDialog
            sel_btn_row, text="显示隐藏",
            command=self._toggle_show_hidden,
            bg=theme.PANEL, fg=theme.MUTED, relief="flat", padx=6,
            font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        self._show_hidden_btn.pack(side="left", padx=(0, 8))

        self._count_lbl = tk.Label(sel_btn_row, text="", bg=theme.PANEL, fg=theme.MUTED,
                                   font=theme.FONT_UI_SMALL)
        self._count_lbl.pack(side="left")

        # 渲染目录分组卡片
        self._render_grouped_projects(card3)
        self._refresh_visibility()
        self._update_count()
        # 窗口关闭（点 × 或按 Esc）时均保证保存当前偏好与选择
        win.protocol("WM_DELETE_WINDOW", self._on_window_close)
        win.bind("<Escape>", lambda e: self._on_window_close())
        # 按表单实际高度收紧窗口，消除底部留白。
        self._fit_window()

    def _build_groups(self, projects) -> list[dict]:
        """把项目列表按真实工作目录分组，提取 agent 图标与规范标签。"""
        from . import views

        if projects and hasattr(projects[0], "source"):
            from tcer.core import analyze
            raw_groups = analyze.real_projects(projects)
            groups = []
            for g in raw_groups:
                src_count = {}
                for r in g.refs:
                    src_count[r.source] = src_count.get(r.source, 0) + 1
                items = []
                for r in g.refs:
                    uid = views.ref_uid(r)
                    icon_key = views.project_icon_key(r)
                    src_lbl = views.project_source_label(r)
                    p_lbl = views.project_label(r)
                    sub = (r.config_root.name if r.source == "claude" and r.config_root is not None else r.key)
                    if src_count.get(r.source, 0) > 1:
                        src_lbl = f"{src_lbl}（{sub}）"
                    disp = f"{src_lbl} · {p_lbl}" if p_lbl and p_lbl != g.display and p_lbl != r.source else src_lbl
                    items.append({
                        "uid": uid,
                        "ref": r,
                        "icon_key": icon_key,
                        "source_label": src_lbl,
                        "display": disp,
                    })
                groups.append({
                    "key": g.key,
                    "display": g.display,
                    "items": items,
                })
            return groups

        # 回退兼容 tuple[str, str] 列表（旧测试/调用）
        groups = []
        for item in projects:
            if isinstance(item, (tuple, list)):
                uid, disp = item[0], item[1]
            else:
                uid = disp = str(item)
            icon_key = "claude"
            src_lbl = "Agent"
            clean_disp = disp
            if disp.startswith("[") and "]" in disp:
                tag = disp[1:disp.index("]")].strip()
                clean_disp = disp[disp.index("]") + 1:].strip()
                src_lbl = tag
                icon_key = tag.lower()
            groups.append({
                "key": uid,
                "display": clean_disp or uid,
                "items": [{
                    "uid": uid,
                    "ref": None,
                    "icon_key": icon_key,
                    "source_label": src_lbl,
                    "display": clean_disp or uid,
                }],
            })
        return groups

    @staticmethod
    def _truncate_path(path: str, max_chars: int = 40) -> str:
        """路径超长截断并加 ...（如 D:\\repos\\...\\game-smoke），防止挤压右侧按钮。"""
        if len(path) <= max_chars:
            return path
        head_len = 14
        tail_len = max_chars - head_len - 3
        return f"{path[:head_len]}...{path[-tail_len:]}"

    def _create_checkbox_widget(self, parent, var: tk.BooleanVar, on_toggle=None):
        """深色微型复选框（13×13 像素小巧尺寸），选中时强调色底 + 纯白粗体 ✓。"""
        is_on = var.get()
        box = tk.Frame(
            parent,
            width=13,
            height=13,
            bg=theme.ACCENT if is_on else theme.PANEL_2,
            highlightthickness=1,
            highlightbackground=theme.ACCENT if is_on else theme.BORDER,
            cursor=CLICK_CURSOR,
        )
        box.pack_propagate(False)
        check_lbl = tk.Label(
            box,
            text="✓" if is_on else "",
            bg=theme.ACCENT if is_on else theme.PANEL_2,
            fg=theme.FG_WHITE,
            font=("Segoe UI", 7, "bold"),
            bd=0,
            cursor=CLICK_CURSOR,
        )
        check_lbl.place(relx=0.5, rely=0.5, anchor="center")

        def _refresh():
            val = var.get()
            c = theme.ACCENT if val else theme.PANEL_2
            bc = theme.ACCENT if val else theme.BORDER
            box.configure(bg=c, highlightbackground=bc)
            check_lbl.configure(text="✓" if val else "", bg=c)

        def _toggle(_e=None):
            var.set(not var.get())
            _refresh()
            if on_toggle:
                on_toggle()

        box.bind("<Button-1>", _toggle)
        check_lbl.bind("<Button-1>", _toggle)
        box._refresh = _refresh
        return box, _toggle

    def _render_grouped_projects(self, parent) -> None:
        """绘制按工作目录分组的卡片，默认收起以保持紧凑，提供选本组与展开/收起切换。"""
        from . import views

        for g in self._groups:
            grp_box = tk.Frame(parent, bg=theme.PANEL, highlightthickness=1,
                               highlightbackground=theme.BORDER)
            grp_box.pack(fill="x", pady=(0, 6))

            # 分组头部
            grp_hdr = tk.Frame(grp_box, bg=theme.PANEL_2, padx=8, pady=5, cursor=CLICK_CURSOR)
            grp_hdr.pack(fill="x")

            # 右侧操作区先 pack(side="right")，确保最高占位优先级，无论路径多长都不被挤压
            btn_frame = tk.Frame(grp_hdr, bg=theme.PANEL_2)
            btn_frame.pack(side="right")

            toggle_btn = tk.Button(  # style-exempt: style.md §3 豁免：UploadDialog
                btn_frame, text="展开 ▾",
                command=lambda grp=g: self._toggle_collapse(grp),
                bg=theme.PANEL, fg=theme.FG, relief="flat", padx=6,
                font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
            toggle_btn.pack(side="right", padx=(4, 0))
            g["toggle_btn"] = toggle_btn

            g_uids = [it["uid"] for it in g["items"]]
            select_btn = tk.Button(  # style-exempt: style.md §3 豁免：UploadDialog
                btn_frame, text="选本组",
                command=lambda u=g_uids: self._toggle_group(u),
                bg=theme.PANEL, fg=theme.FG, relief="flat", padx=6,
                font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
            select_btn.pack(side="right")
            g["select_btn"] = select_btn

            # 左侧依次 pack
            arrow_lbl = tk.Label(grp_hdr, text="▸", bg=theme.PANEL_2, fg=theme.MUTED,
                                 font=theme.FONT_UI_BOLD, width=2)
            arrow_lbl.pack(side="left")
            g["arrow_lbl"] = arrow_lbl

            dir_icon = views.ui_icon(grp_hdr, "project")
            if dir_icon is not None:
                tk.Label(grp_hdr, image=dir_icon, bg=theme.PANEL_2).pack(side="left", padx=(0, 6))

            disp_text = self._truncate_path(g["display"])
            hdr_title = tk.Label(grp_hdr, text=disp_text, bg=theme.PANEL_2, fg=theme.FG,
                                 font=theme.FONT_UI_BOLD, anchor="w")
            hdr_title.pack(side="left")
            if disp_text != g["display"]:
                Tooltip(hdr_title, g["display"])

            count_lbl = tk.Label(grp_hdr, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                 font=theme.FONT_UI_SMALL)
            count_lbl.pack(side="left", padx=(6, 0))
            g["count_lbl"] = count_lbl

            hidden_tag = tk.Label(grp_hdr, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                  font=theme.FONT_UI_SMALL)
            hidden_tag.pack(side="left", padx=(4, 0))
            g["hidden_tag"] = hidden_tag

            # 右键菜单：隐藏此目录 / 取消隐藏
            # 右键菜单：隐藏此目录 / 取消隐藏（挂在顶层窗口上，生命周期与子容器解耦）
            def _pop_context_menu(event, grp=g):
                from .widgets import FlatMenu
                menu = FlatMenu(self._win)
                is_h = grp["key"] in self._hidden_keys
                if not is_h:
                    menu.add_command(label="隐藏此目录", command=lambda k=grp["key"]: self._hide_group(k))
                else:
                    menu.add_command(label="取消隐藏", command=lambda k=grp["key"]: self._unhide_group(k))
                menu.tk_popup(event.x_root, event.y_root)
            for w in (grp_hdr, arrow_lbl, hdr_title, count_lbl, hidden_tag):
                w.bind("<Button-3>", _pop_context_menu)
                w.bind("<Button-2>", _pop_context_menu)

            # 点击头部空白区域切换展开/收起
            grp_hdr.bind("<Button-1>", lambda e, grp=g: self._toggle_collapse(grp))
            arrow_lbl.bind("<Button-1>", lambda e, grp=g: self._toggle_collapse(grp))
            hdr_title.bind("<Button-1>", lambda e, grp=g: self._toggle_collapse(grp))
            count_lbl.bind("<Button-1>", lambda e, grp=g: self._toggle_collapse(grp))
            g["grp_box"] = grp_box
            # 分组子项容器（默认收起，不 pack）
            items_box = tk.Frame(grp_box, bg=theme.PANEL, padx=12, pady=4)
            g["items_box"] = items_box
            g["is_expanded"] = False

            for it in g["items"]:
                row = tk.Frame(items_box, bg=theme.PANEL, pady=2, cursor=CLICK_CURSOR)
                row.pack(fill="x")

                var = self._proj_vars[it["uid"]]
                box, toggle_fn = self._create_checkbox_widget(
                    row, var, on_toggle=self._on_item_check)
                box.pack(side="left", padx=(0, 6))
                self._box_widgets[it["uid"]] = box

                # 来源/品牌 16×16 图标（与主界面一致，复用资产）
                icon = views.source_icon(row, it["icon_key"])
                if icon is not None:
                    il = tk.Label(row, image=icon, bg=theme.PANEL, cursor=CLICK_CURSOR)
                    il.pack(side="left", padx=(0, 6))
                    Tooltip(il, it["source_label"])
                    il.bind("<Button-1>", toggle_fn)

                nl = tk.Label(row, text=it["display"], bg=theme.PANEL, fg=theme.FG,
                              font=theme.FONT_UI, anchor="w", cursor=CLICK_CURSOR)
                nl.pack(side="left", fill="x", expand=True)
                nl.bind("<Button-1>", toggle_fn)
                row.bind("<Button-1>", toggle_fn)

        self._refresh_group_counts()

    def _toggle_collapse(self, grp: dict) -> None:
        """切换单个目录分组的展开/收起状态。"""
        exp = not grp.get("is_expanded", False)
        grp["is_expanded"] = exp
        if exp:
            grp["items_box"].pack(fill="x", padx=12, pady=4)
            grp["toggle_btn"].configure(text="收起 ▴")
            grp["arrow_lbl"].configure(text="▾")
        else:
            grp["items_box"].pack_forget()
            grp["toggle_btn"].configure(text="展开 ▾")
            grp["arrow_lbl"].configure(text="▸")
        self._fit_window()

    def _expand_all(self) -> None:
        """全部展开所有目录分组。"""
        for g in self._groups:
            g["is_expanded"] = True
            g["items_box"].pack(fill="x", padx=12, pady=4)
            g["toggle_btn"].configure(text="收起 ▴")
            g["arrow_lbl"].configure(text="▾")
        self._fit_window()

    def _collapse_all(self) -> None:
        """全部收起所有目录分组。"""
        for g in self._groups:
            g["is_expanded"] = False
            g["items_box"].pack_forget()
            g["toggle_btn"].configure(text="展开 ▾")
            g["arrow_lbl"].configure(text="▸")
        self._fit_window()

    def _refresh_group_counts(self) -> None:
        """刷新每个目录头部显示的已选数量以及选本组/已选高亮按钮状态。"""
        for g in self._groups:
            total = len(g["items"])
            sel = sum(1 for it in g["items"] if self._proj_vars[it["uid"]].get())
            lbl = g.get("count_lbl")
            if lbl is not None and lbl.winfo_exists():
                lbl.configure(text=f"（共 {total} 个 · 已选 {sel}）")

            btn = g.get("select_btn")
            if btn is not None and btn.winfo_exists():
                all_selected = (total > 0 and sel == total)
                if all_selected:
                    btn.configure(text="已选", bg=theme.ACCENT, fg=theme.FG_WHITE)
                else:
                    btn.configure(text="选本组", bg=theme.PANEL, fg=theme.FG)

    def _hide_group(self, key: str) -> None:
        """把指定目录分组标记为隐藏并立即刷新界面且落盘持久化。"""
        self._hidden_keys.add(key)
        self._refresh_visibility()
        self._save_all()

    def _unhide_group(self, key: str) -> None:
        """取消指定目录分组的隐藏并立即刷新界面且落盘持久化。"""
        self._hidden_keys.discard(key)
        self._refresh_visibility()
        self._save_all()

    def _toggle_show_hidden(self) -> None:
        """切换是否显示被隐藏的目录分组。"""
        self._show_hidden = not self._show_hidden
        if self._show_hidden:
            self._show_hidden_btn.configure(bg=theme.ACCENT, fg=theme.FG_WHITE)
        else:
            self._show_hidden_btn.configure(bg=theme.PANEL, fg=theme.MUTED)
        self._refresh_visibility()

    def _refresh_visibility(self) -> None:
        """根据 _hidden_keys 与 _show_hidden 动态显示或隐藏卡片。"""
        for g in self._groups:
            grp_box = g.get("grp_box")
            if grp_box is None or not grp_box.winfo_exists():
                continue
            is_h = g["key"] in self._hidden_keys
            hidden_tag = g.get("hidden_tag")
            if is_h:
                if self._show_hidden:
                    grp_box.pack(fill="x", pady=(0, 6))
                    if hidden_tag and hidden_tag.winfo_exists():
                        hidden_tag.configure(text="（已隐藏）")
                else:
                    grp_box.pack_forget()
            else:
                grp_box.pack(fill="x", pady=(0, 6))
                if hidden_tag and hidden_tag.winfo_exists():
                    hidden_tag.configure(text="")
        try:
            self._sf.inner.update_idletasks()
        except Exception:
            pass
        self._fit_window()
    def _refresh_all_checkboxes(self) -> None:
        """批量刷新所有复选框方块的高亮与白勾状态。"""
        for box in self._box_widgets.values():
            if hasattr(box, "_refresh"):
                box._refresh()
        if hasattr(self, "_auto_box") and hasattr(self._auto_box, "_refresh"):
            self._auto_box._refresh()
        if hasattr(self, "_detail_box") and hasattr(self._detail_box, "_refresh"):
            self._detail_box._refresh()

    def _toggle_group(self, uids: list[str]) -> None:
        all_checked = all(self._proj_vars[u].get() for u in uids if u in self._proj_vars)
        new_state = not all_checked
        for u in uids:
            if u in self._proj_vars:
                self._proj_vars[u].set(new_state)
        self._refresh_all_checkboxes()
        self._on_item_check()

    def _select_all(self) -> None:
        for var in self._proj_vars.values():
            var.set(True)
        self._refresh_all_checkboxes()
        self._on_item_check()

    def _clear_all(self) -> None:
        for var in self._proj_vars.values():
            var.set(False)
        self._refresh_all_checkboxes()
        self._on_item_check()

    def _update_count(self) -> None:
        selected = sum(1 for v in self._proj_vars.values() if v.get())
        total = len(self._proj_vars)
        self._count_lbl.config(text=f"已选 {selected} / 共 {total} 个 agent")
        self._refresh_group_counts()
    def _on_item_check(self) -> None:
        self._update_count()
        if self._auto_upload_var.get():
            self._save_all()

    def _on_detail_toggle(self) -> None:
        if self._auto_upload_var.get():
            self._save_all()

    def _on_toggle_auto_upload(self) -> None:
        enabled = self._auto_upload_var.get()
        self._save_all()
        if enabled:
            self.set_status("已开启每小时自动检查并上传新会话")
        else:
            self.set_status("已关闭自动上传")

    def _save_all(self) -> None:
        prefs = self._collect()
        self._save_config_safe(
            url=self._url_var.get(),
            auth_token=self._token_var.get(),
            detail=self._detail_var.get(),
            auto_upload=self._auto_upload_var.get(),
        )
        self._on_save_prefs(prefs)

    def _save_config_safe(self, **kwargs) -> None:
        try:
            self._on_save_config(**kwargs)
        except TypeError:
            # 兼容仅接收 3 个参数的测试 mock
            legacy = {k: v for k, v in kwargs.items() if k in ("url", "auth_token", "detail")}
            self._on_save_config(**legacy)

    # -- small builders --
    def _card(self, inner, title: str) -> tk.Frame:
        tk.Frame(inner, bg=theme.PANEL, height=6).pack(fill="x")
        head = tk.Frame(inner, bg=theme.CARD_HEADER_BG, padx=10, pady=6)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=theme.CARD_HEADER_BG, fg=theme.FG,
                 font=theme.FONT_UI_BOLD).pack(anchor="w")
        card = tk.Frame(inner, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x")
        return card

    def _labeled_entry(self, card, label: str, var, *, placeholder: str = "") -> None:
        """一行「标签 + 深色输入框」，风格与顶栏日期输入框一致（highlight 边框）。"""
        row = tk.Frame(card, bg=theme.PANEL)
        row.pack(fill="x", pady=(2, 2))
        tk.Label(row, text=label, bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL, width=11, anchor="w").pack(side="left")
        e = tk.Entry(row, textvariable=var, bg=theme.PANEL_2, fg=theme.FG,
                     insertbackground=theme.FG, relief="flat", highlightthickness=1,
                     highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT,
                     font=theme.FONT_UI)
        e.pack(side="left", fill="x", expand=True)
        if placeholder:
            Tooltip(e, placeholder)

    def _fit_window(self) -> None:
        """按表单实际高度收紧窗口，消除底部留白。封顶 720 防过高。"""
        win = self._win
        win.update_idletasks()
        canv = self._sf.canvas
        inner_h = self._sf.inner.winfo_reqheight()
        top = canv.winfo_y()
        status_h = self._status.winfo_reqheight() + 2
        action_h = next((w.winfo_reqheight() for w in win.winfo_children()
                         if isinstance(w, tk.Frame) and w is not canv), 0)
        win_h = max(420, min(top + inner_h + status_h + action_h + 20, 720))
        cur_h = win.winfo_height()
        adj = (cur_h - win_h) // 2 if cur_h > 200 else 0
        cur_w = win.winfo_width() if win.winfo_width() > 300 else 760
        win.geometry(f"{cur_w}x{int(win_h)}+{int(win.winfo_x())}+{int(win.winfo_y() + adj)}")

    # -- prefs / status --
    def _collect(self) -> dict:
        selected = [uid for uid, var in self._proj_vars.items() if var.get()]
        return {
            "last_projects": selected,
            "hidden_dirs": sorted(self._hidden_keys),
        }

    def set_status(self, text: str, *, error: bool = False) -> None:
        if not self._status.winfo_exists():
            return
        self._status.config(text=text, fg=theme.ERROR if error else theme.SUCCESS)

    def _do_upload(self) -> None:
        prefs = self._collect()
        if not prefs["last_projects"]:
            self.set_status("请至少选择一个项目", error=True)
            return
        # 先保存上传配置（写回 tcer_ui.json 的 upload 段），再保存项目选择、上传。
        # _start_upload 从 upload_config 读回，故此处保存是上传取到最新配置的前提。
        self._save_all()
        self.set_status("上传中…")
        self._on_upload(prefs, self)

    def _on_window_close(self) -> None:
        """关闭窗口前保存当前偏好与隐藏目录设置。"""
        try:
            self._save_all()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass


class UploadSuccessBubble:
    """在活动栏上传按钮右侧弹出的主动提示气泡（通知自动上传成功）。

    显示约 6 秒后自动关闭，点击立即关闭；深色主题卡片，ACCENT 描边，无抢焦。
    """
    _current = None

    @classmethod
    def show(cls, anchor_widget, title: str, subtitle: str, timeout_ms: int = 6000):
        if cls._current is not None:
            try:
                cls._current.destroy()
            except Exception:
                pass
            cls._current = None

        if not anchor_widget:
            return None
        try:
            if not anchor_widget.winfo_exists():
                return None
            anchor_widget.update_idletasks()
            x_root = anchor_widget.winfo_rootx()
            y_root = anchor_widget.winfo_rooty()
            w_w = anchor_widget.winfo_width()
            w_h = anchor_widget.winfo_height()
        except Exception:
            return None

        bubble = tk.Toplevel(anchor_widget)
        bubble.wm_overrideredirect(True)
        try:
            bubble.wm_attributes("-topmost", True)
        except Exception:
            pass

        bubble.configure(bg=theme.ACCENT)

        inner = tk.Frame(bubble, bg=theme.PANEL_2, padx=10, pady=7)
        inner.pack(padx=1, pady=1)

        row1 = tk.Frame(inner, bg=theme.PANEL_2)
        row1.pack(fill="x")

        tk.Label(row1, text="✓", fg=theme.SUCCESS, bg=theme.PANEL_2,
                 font=theme.FONT_UI_BOLD).pack(side="left", padx=(0, 6))

        tk.Label(row1, text=title, fg=theme.FG_WHITE, bg=theme.PANEL_2,
                 font=theme.FONT_UI_BOLD).pack(side="left")

        close_btn = tk.Label(row1, text="×", fg=theme.MUTED, bg=theme.PANEL_2,
                             font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR)
        close_btn.pack(side="right", padx=(8, 0))

        if subtitle:
            row2 = tk.Frame(inner, bg=theme.PANEL_2)
            row2.pack(fill="x", pady=(3, 0))
            tk.Label(row2, text=subtitle, fg=theme.MUTED, bg=theme.PANEL_2,
                     font=theme.FONT_UI_SMALL).pack(side="left")

        def _dismiss(_event=None):
            if cls._current is bubble:
                cls._current = None
            try:
                bubble.destroy()
            except Exception:
                pass

        bubble.bind("<Button-1>", _dismiss)
        inner.bind("<Button-1>", _dismiss)
        close_btn.bind("<Button-1>", _dismiss)

        bubble.update_idletasks()
        b_w = bubble.winfo_reqwidth()
        b_h = bubble.winfo_reqheight()

        pos_x = x_root + w_w + 6
        pos_y = y_root + (w_h - b_h) // 2

        bubble.wm_geometry(f"+{pos_x}+{pos_y}")
        cls._current = bubble

        bubble.after(timeout_ms, _dismiss)
        return bubble
class LlmConfigPopup:
    """LLM 语义解读设置 — OpenAI-compatible 端点 + 数据出境三档。

    「测试连接」在后台线程用**表单当前值**（未保存）发最小 chat ping，一次
    验证端点/模型名/鉴权（不用 GET /v1/models——部分兼容服务不实现）。
    api_key 明文存本机 ``tcer_llm.json``（本地单用户信任模型，同上传 token，
    便于用户核对/更换）。保存后由 controller 的 on_save 回调刷新工具栏入口
    显隐。两个文本框**同时留空保存 = 清除配置**（GUI 回退到无 LLM 入口）。
    """

    def __init__(self, parent, *, config: dict, on_save) -> None:
        from tcer.core import llm_client, llm_prefs
        self._llm_client = llm_client
        self._llm_prefs = llm_prefs
        self._on_save = on_save

        win = _new_window(parent, "LLM 设置", "540x480")
        self._win = win
        import queue as _queue
        self._ui_queue: _queue.Queue = _queue.Queue()
        self._poll_ui_queue()
        tk.Label(win, text="LLM 语义解读", bg=theme.BG, fg=theme.FG_WHITE,
                 font=theme.FONT_HEADING, pady=10).pack(side="top", fill="x")

        # 底部操作栏先行 pack(side="bottom")，确保中间滚动区获得准确剩余高度
        action = tk.Frame(win, bg=theme.BG)
        action.pack(side="bottom", fill="x", padx=16, pady=(4, 10))
        self._action_frame = action
        self._test_btn = flat_button(action, "测试连接", self._do_test)
        self._test_btn.pack(side="left")
        flat_button(action, "保存", self._do_save, primary=True).pack(side="right")

        self._status = tk.Label(win, text="", bg=theme.BG, fg=theme.MUTED,
                                font=theme.FONT_UI, wraplength=440, justify="left")
        self._status.pack(side="bottom", fill="x", padx=12, pady=(0, 2))

        # 中间内容滚动容器：独立 body_frame 容纳 ScrollFrame
        body_frame = tk.Frame(win, bg=theme.BG)
        body_frame.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 4))
        sf = ScrollFrame(body_frame, bg=theme.BG)
        self._sf = sf
        inner = sf.inner
        svc = self._card(inner, "服务（OpenAI-compatible 端点）")
        self._url_var = tk.StringVar(value=str(config.get("base_url") or ""))
        self._key_var = tk.StringVar(value=str(config.get("api_key") or ""))
        self._model_var = tk.StringVar(value=str(config.get("model") or ""))
        self._labeled_entry(
            svc, "服务地址", self._url_var,
            placeholder="http://localhost:11434/v1 或 https://api.x.ai/v1（可省略 /v1）")
        self._labeled_entry(
            svc, "API Key", self._key_var,
            placeholder="留空 = 无鉴权（本地 Ollama）；明文存本机")
        self._labeled_entry(
            svc, "模型", self._model_var,
            placeholder="如 qwen3:8b / claude-sonnet-5")

        # TypeSafe Jev (System One) 判定卡片（与通用 LLM 结构一致）
        ts_card = self._card(inner, "TypeSafe（Jev System One 判定服务）")
        self._typesafe_url_var = tk.StringVar(value=str(config.get("typesafe_base_url") or "https://api.typesafe.ai"))
        self._typesafe_key_var = tk.StringVar(value=str(config.get("typesafe_key") or ""))
        self._typesafe_model_var = tk.StringVar(value=str(config.get("typesafe_model") or "jev-latest"))

        self._labeled_entry(
            ts_card, "服务地址", self._typesafe_url_var,
            placeholder="默认 https://api.typesafe.ai（可省略 /v1）")
        self._labeled_entry(
            ts_card, "API Key", self._typesafe_key_var,
            placeholder="留空 = 未启用；明文存本机")
        self._labeled_entry(
            ts_card, "模型", self._typesafe_model_var,
            placeholder="默认 jev-latest（或 jev-preview）")

        scope_card = self._card(inner, "数据出境范围（可多选）")
        stored_scopes = set(self._llm_prefs.normalize_scopes(
            config.get("scopes") if "scopes" in config else config.get("scope")))
        self._scope_vars: dict[str, tk.BooleanVar] = {}
        self._scope_var = tk.StringVar(
            value=self._llm_prefs.scopes_summary(list(stored_scopes)))
        for s in ("metrics", "dialog", "tools"):
            var = tk.BooleanVar(value=(s in stored_scopes))
            self._scope_vars[s] = var

            # 独立条目卡片：轻量抬升底色 + 边框 + 左右两列排版
            item = tk.Frame(scope_card, bg=theme.PANEL_2, relief="flat",
                            highlightthickness=1, highlightbackground=theme.BORDER,
                            padx=10, pady=8, cursor=CLICK_CURSOR)
            item.pack(fill="x", pady=(0, 6))

            # 左列：自定义复选框（纯白对勾 + 强调色底色，彻底消除 Windows 灰色模糊暗纹）
            cb_canvas = tk.Canvas(item, width=16, height=16, bg=theme.PANEL_2,
                                  highlightthickness=0, bd=0, cursor=CLICK_CURSOR)
            def _draw_cb(_c=cb_canvas, _v=var):
                _c.delete("all")
                if _v.get():
                    _c.create_rectangle(1, 1, 15, 15, outline=theme.ACCENT,
                                       fill=theme.ACCENT, width=1)
                    _c.create_line(4, 8, 7, 11, 12, 4, fill=theme.FG_WHITE, width=2,
                                   capstyle="round", joinstyle="round")
                else:
                    _c.create_rectangle(1, 1, 15, 15, outline=theme.BORDER_HOVER,
                                       fill=theme.BG, width=1)
            _draw_cb()
            var.trace_add("write", lambda *_, c=cb_canvas, v=var: _draw_cb(c, v))
            cb_canvas.pack(side="left", anchor="nw", padx=(0, 8), pady=(2, 0))
            # 右列：独立的文本容器，使标题与描述绝对物理左对齐
            text_col = tk.Frame(item, bg=theme.PANEL_2, cursor=CLICK_CURSOR)
            text_col.pack(side="left", fill="both", expand=True)

            title_lbl = tk.Label(
                text_col, text=self._llm_prefs.SCOPE_LABELS[s],
                bg=theme.PANEL_2, fg=theme.FG_WHITE, font=theme.FONT_UI_BOLD,
                anchor="w", justify="left", cursor=CLICK_CURSOR)
            title_lbl.pack(fill="x")

            desc_lbl = tk.Label(
                text_col, text=self._llm_prefs.SCOPE_DESCRIPTIONS[s],
                bg=theme.PANEL_2, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                wraplength=380, anchor="w", justify="left", cursor=CLICK_CURSOR)
            desc_lbl.pack(fill="x", pady=(2, 0))

            # 交互增强：点击条目卡片任意区域均可直接切换勾选状态
            def _make_toggle(target_var=var):
                def _toggle(_event=None):
                    target_var.set(not target_var.get())
                    self._on_scope_changed()
                return _toggle

            toggle_fn = _make_toggle(var)
            for w_elem in (item, cb_canvas, text_col, title_lbl, desc_lbl):
                w_elem.bind("<Button-1>", toggle_fn)

            # 悬停轻微高亮反馈
            def _make_hover(target_frame=item):
                def _enter(_e):
                    try:
                        target_frame.config(highlightbackground=theme.BORDER_HOVER)
                    except tk.TclError:
                        pass
                def _leave(e):
                    if e is not None:
                        try:
                            under = e.widget.winfo_containing(e.x_root, e.y_root)
                            curr = under
                            while curr is not None:
                                if curr == target_frame:
                                    return
                                curr = getattr(curr, "master", None)
                        except Exception:
                            pass
                    try:
                        target_frame.config(highlightbackground=theme.BORDER)
                    except tk.TclError:
                        pass
                return _enter, _leave

            _hover_enter, _hover_leave = _make_hover(item)
            for w_elem in (item, cb_canvas, text_col, title_lbl, desc_lbl):
                w_elem.bind("<Enter>", _hover_enter)
                w_elem.bind("<Leave>", _hover_leave)

        # 过程数据供给档：控制 read_dialogue 纳入的工具结果深度与逐回合时序密度。
        # 单选条目（点击整卡切换；选中态强调色边框 + 圆点）。
        detail_card = self._card(inner, "过程数据供给量（影响 prompt 规模与 token 消耗）")
        cur_detail = self._llm_prefs.normalize_dialog_detail(config.get("dialog_detail"))
        self._detail_var = tk.StringVar(value=cur_detail)
        self._detail_items: dict[str, tk.Frame] = {}

        def _paint_detail(d_key: str) -> None:
            frame = self._detail_items.get(d_key)
            if frame is not None and frame.winfo_exists():
                # 选中态：强调色边框并加粗（子元素底色固定，避免半选视觉残影）
                on = self._detail_var.get() == d_key
                frame.config(highlightbackground=(
                    theme.ACCENT if on else theme.BORDER),
                    highlightthickness=2 if on else 1)

        def _pick_detail(d_key: str) -> None:
            self._detail_var.set(d_key)
            for k in self._detail_items:
                _paint_detail(k)

        for d_key, (d_label, d_tip) in self._llm_prefs.DIALOG_DETAIL_LABELS.items():
            d_item = tk.Frame(detail_card, bg=theme.PANEL, relief="flat",
                              highlightthickness=1, highlightbackground=theme.BORDER,
                              padx=10, pady=7, cursor=CLICK_CURSOR)
            d_item.pack(fill="x", pady=(0, 5))
            dot = tk.Canvas(d_item, width=14, height=14, bg=theme.PANEL,
                            highlightthickness=0, bd=0, cursor=CLICK_CURSOR)
            dot.pack(side="left", padx=(0, 8))
            txt_col = tk.Frame(d_item, bg=theme.PANEL, cursor=CLICK_CURSOR)
            txt_col.pack(side="left", fill="both", expand=True)
            # 点击绑定必须覆盖 Label 本身——Label 在条目上层遮挡容器，漏绑即
            # 「点了没反应」（曾因此报「完整」无法选中：描述长、Label 占满整行）。
            lbl_title = tk.Label(txt_col, text=d_label, bg=theme.PANEL,
                                 fg=theme.FG_WHITE, font=theme.FONT_UI_BOLD,
                                 anchor="w", cursor=CLICK_CURSOR)
            lbl_title.pack(fill="x")
            lbl_desc = tk.Label(txt_col, text=d_tip, bg=theme.PANEL, fg=theme.MUTED,
                                font=theme.FONT_UI_SMALL, wraplength=360, anchor="w",
                                justify="left", cursor=CLICK_CURSOR)
            lbl_desc.pack(fill="x")
            self._detail_items[d_key] = d_item

            def _draw_dot(_c=dot, _k=d_key):
                _c.delete("all")
                on = self._detail_var.get() == _k
                outline = theme.ACCENT if on else theme.BORDER_HOVER
                _c.create_oval(2, 2, 12, 12, outline=outline, width=1)
                if on:
                    _c.create_oval(5, 5, 9, 9, fill=theme.ACCENT, outline="")

            self._detail_dot_drawers = getattr(self, "_detail_dot_drawers", {})
            self._detail_dot_drawers[d_key] = _draw_dot

            def _make_pick(k=d_key):
                return (lambda _e=None: (_pick_detail(k),
                                         [fn() for fn in
                                          self._detail_dot_drawers.values()]))
            pick = _make_pick(d_key)
            for w_el in (d_item, dot, txt_col, lbl_title, lbl_desc):
                w_el.bind("<Button-1>", pick)
        # 初始选中态绘制
        for fn in self._detail_dot_drawers.values():
            fn()
        for k in self._detail_items:
            _paint_detail(k)

        win.bind("<Escape>", lambda e: win.destroy())
        self._fit_window()

    # -- small builders（UploadDialog 同款）--
    def _card(self, inner, title: str) -> tk.Frame:
        wrap = tk.Frame(inner, bg=theme.PANEL, highlightthickness=1,
                        highlightbackground=theme.BORDER)
        wrap.pack(fill="x", padx=2, pady=(0, 10))
        head = tk.Frame(wrap, bg=theme.CARD_HEADER_BG, padx=12, pady=6)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=theme.CARD_HEADER_BG, fg=theme.FG_WHITE,
                 font=theme.FONT_UI_BOLD).pack(anchor="w")
        card = tk.Frame(wrap, bg=theme.PANEL, padx=12, pady=10)
        card.pack(fill="x")
        return card

    def _labeled_entry(self, card, label: str, var, *, placeholder: str = "") -> None:
        row = tk.Frame(card, bg=theme.PANEL)
        row.pack(fill="x", pady=(3, 3))
        tk.Label(row, text=label, bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL, width=10, anchor="w").pack(side="left")
        e = tk.Entry(row, textvariable=var, bg=theme.PANEL_2, fg=theme.FG,
                     insertbackground=theme.FG, relief="flat", highlightthickness=1,
                     highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT,
                     font=theme.FONT_UI)
        e.pack(side="left", fill="x", expand=True, ipady=3)
        if placeholder:
            Tooltip(e, placeholder)

    def _fit_window(self) -> None:
        win = self._win
        win.update_idletasks()
        from .platform import get_monitor_work_area
        m_left, m_top, m_right, m_bottom = get_monitor_work_area(win)
        m_h = m_bottom - m_top

        top_win = win.master.winfo_toplevel()
        top_win.update_idletasks()

        inner_h = self._sf.inner.winfo_reqheight()
        status_h = self._status.winfo_reqheight()
        action_h = self._action_frame.winfo_reqheight()
        needed_h = 45 + inner_h + status_h + action_h + 30

        # 最大高度留足任务栏与屏幕边距（留出 120px 安全区）
        max_h = min(620, m_h - 120)
        win_h = max(440, min(needed_h, max_h))
        win_w = 540

        # 重新根据实际高度居中计算 x 与 y（绝不向下溢出穿透任务栏）
        pw = top_win.winfo_width()
        ph = top_win.winfo_height()
        px = top_win.winfo_rootx()
        py = top_win.winfo_rooty()

        x = px + (pw - win_w) // 2
        y = py + (ph - win_h) // 2

        if y + win_h > m_bottom - 10:
            y = max(m_top + 25, m_bottom - 10 - win_h)
        if x + win_w > m_right - 10:
            x = max(m_left + 10, m_right - win_w - 10)
        if x < m_left + 10:
            x = m_left + 10
        if y < m_top + 25:
            y = m_top + 25

        win.geometry(f"{win_w}x{int(win_h)}+{int(x)}+{int(y)}")
        win.update_idletasks()
        self._sf._apply_scrollregion()
    def _poll_ui_queue(self) -> None:
        import queue as _queue
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except _queue.Empty:
            pass
        except Exception:
            pass
        try:
            if self._win.winfo_exists():
                self._win.after(60, self._poll_ui_queue)
        except (tk.TclError, RuntimeError):
            pass

    def set_status(self, text: str, *, error: bool = False) -> None:
        try:
            if not self._status.winfo_exists():
                return
            self._status.config(text=text,
                                fg=theme.ERROR if error else theme.SUCCESS)
        except tk.TclError:
            pass

    # -- actions --
    def _on_scope_changed(self) -> None:
        sel = [s for s in ("metrics", "dialog", "tools")
               if self._scope_vars.get(s, tk.BooleanVar()).get()]
        self._scope_var.set(self._llm_prefs.scopes_summary(sel))
    def _do_test(self) -> None:
        from threading import Thread
        url = self._url_var.get().strip()
        model = self._model_var.get().strip()
        if not url or not model:
            self.set_status("请先填写服务地址与模型", error=True)
            return
        key = self._key_var.get().strip() or None
        self._test_btn.config(state="disabled", text="测试中…")
        self.set_status("连接中…")
        def work():
            try:
                self._llm_client.test_connection(base_url=url, api_key=key,
                                                 model=model)
                ok, msg = True, "连接成功"
            except self._llm_client.LlmError as e:
                ok, msg = False, str(e)
            self._ui_queue.put(lambda: self._on_test_done(ok, msg))
            try:
                self._win.after(0, lambda: None)
            except Exception:
                pass
        Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, msg: str) -> None:
        try:
            if not self._win.winfo_exists():
                return
            self._test_btn.config(state="normal", text="测试连接")
            self.set_status(msg, error=not ok)
        except tk.TclError:
            pass

    def _do_save(self) -> None:
        sel_scopes = [s for s in ("metrics", "dialog", "tools")
                      if self._scope_vars.get(s, tk.BooleanVar()).get()]
        cfg = {
            "base_url": self._url_var.get().strip(),
            "api_key": self._key_var.get().strip(),
            "model": self._model_var.get().strip(),
            "scopes": sel_scopes,
            "scope": self._llm_prefs.scopes_summary(sel_scopes),
            "dialog_detail": self._detail_var.get(),
            "typesafe_key": self._typesafe_key_var.get().strip(),
            "typesafe_base_url": self._typesafe_url_var.get().strip() or "https://api.typesafe.ai",
            "typesafe_model": self._typesafe_model_var.get().strip() or "jev-latest",
        }
        if bool(cfg["base_url"]) != bool(cfg["model"]):
            self.set_status("通用 LLM 服务地址与模型需同时填写（或同时留空以清除配置）",
                            error=True)
            return
        self._on_save(**cfg)
        status_msg = "已保存配置"
        if cfg["typesafe_key"]:
            status_msg += f"（TypeSafe {cfg['typesafe_model']} 已就绪）"
        if not cfg["base_url"]:
            status_msg += "（通用 LLM 入口已清除）"
        self.set_status(status_msg)


def _copy(win, text: str) -> None:
    win.clipboard_clear()
    win.clipboard_append(text)
    # small transient confirmation
    toast = tk.Label(win, text="已复制到剪贴板", bg=theme.SUCCESS, fg=theme.BG,
                     font=theme.FONT_UI, padx=8, pady=2)
    toast.place(relx=0.5, rely=0.02, anchor="n")
    win.after(1200, toast.destroy)


# 分析类弹窗已拆分至 popups_analysis.py；re-export 保持既有 import 路径。
from .popups_analysis import (  # noqa: E402,F401
    CrossSourceModelsPopup, ProjectOverviewPopup, ProjectProfilePopup,
    SessionComparePopup, SessionTimelinePopup, ToolSequencePopup,
)
