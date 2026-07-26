"""GUI controller: owns state + background analysis, wires views together.

The controller is the only place that touches ``analyze`` / ``export`` and
``export`` and threads. Views are stateless presenters that call back into it
(``reanalyze`` / ``on_select_project`` / ``export`` / …). Analysis runs on a
daemon thread; results come back through a queue polled from the Tk main loop.
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tcer.core import analyze, export as export_mod, metrics
from tcer.core import ui_prefs, upload_client, upload_prefs
from tcer.core.paths import list_project_refs, project_has_sessions
from tcer.core.reader import discover_jsonl
from . import html_report, popups, theme, views
from .views import CteiRankingView, FilterBar, MetricPanel, ModelCompareView, ProjectColumn, SessionColumn, TrendChart


class TcerGui:
    def __init__(self, root) -> None:
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._projects: list = []
        self._current: analyze.ProjectAnalysis | None = None
        self._selected_project_idx: int | None = None
        self._selected_session_id: str | None = None
        self.view_mode = tk.StringVar(value="project")
        self._rendered_report = None  # last report rendered in MetricPanel (for popups)
        self._no_loc: bool = False
        self._analysis_generation = 0
        self._analysis_cancel = threading.Event()
        self._upload_prefs: dict = upload_prefs.load()
        self._auto_upload_after: str | None = None

        root.title("TCER — Token 转码效率计量")
        root.configure(bg=theme.BG)
        theme.setup_style(ttk)
        # Combobox 下拉列表是独立 Listbox，不吃 ttk style，只能经 option db 深色化。
        for opt, val in (("*TCombobox*Listbox*background", theme.PANEL),
                         ("*TCombobox*Listbox*foreground", theme.FG),
                         ("*TCombobox*Listbox*selectBackground", theme.ACCENT),
                         ("*TCombobox*Listbox*selectForeground", "#ffffff")):
            root.option_add(opt, val)

        # 界面偏好：恢复上次窗口几何，否则居中（略上移避开任务栏）。
        self._ui_prefs = ui_prefs.load()
        self._restore_project_key = self._ui_prefs.get("last_project")
        if ui_prefs.valid_geometry(self._ui_prefs.get("geometry")):
            root.geometry(self._ui_prefs["geometry"])
        else:
            w, h = 1600, 900
            sx = root.winfo_screenwidth()
            sy = root.winfo_screenheight()
            root.geometry(f"{w}x{h}+{(sx - w) // 2}+{(sy - h) // 2 - 40}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.filter = FilterBar(root, self)
        self.filter.restore_prefs(self._ui_prefs)
        self._build_body(root)
        self.refresh_projects()
        root.after(100, self._poll)
        if self._upload_prefs.get("auto_upload"):
            self._schedule_auto_upload()

    def _on_close(self) -> None:
        """关闭时保存界面偏好（几何/分栏/筛选/项目），失败不拦退出。"""
        try:
            proj = self._selected_project()
            ui_prefs.save({
                "geometry": self.root.geometry(),
                "sashes": [self._paned.sash_coord(i)[0] for i in (0, 1)],
                "source": self.filter.get_source(),
                "task_type": self.filter.get_params().get("task_type"),
                "last_project": getattr(proj, "key", None),
            })
        except tk.TclError:
            pass
        self.root.destroy()

    # --------------------------------------------------------------- layout
    def _build_body(self, root) -> None:
        paned = tk.PanedWindow(root, orient="horizontal", bg=theme.BG, sashwidth=4)
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        left_wrap = tk.Frame(paned, bg=theme.BG)
        paned.add(left_wrap, minsize=160)
        self.project_col = ProjectColumn(left_wrap, self)

        mid_wrap = tk.Frame(paned, bg=theme.BG)
        paned.add(mid_wrap, minsize=200)
        self.session_col = SessionColumn(mid_wrap, self)

        right = tk.Frame(paned, bg=theme.BG, width=900)
        paned.add(right, minsize=760)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        self._nb = nb
        tab_m = tk.Frame(nb, bg=theme.BG)
        tab_b = tk.Frame(nb, bg=theme.PANEL)
        tab_t = tk.Frame(nb, bg=theme.PANEL)
        tab_c = tk.Frame(nb, bg=theme.PANEL)
        nb.add(tab_m, text="指标分类")
        nb.add(tab_b, text="综合效率分排名")
        nb.add(tab_t, text="趋势")
        nb.add(tab_c, text="模型对比")

        self.metric_panel = MetricPanel(tab_m, self)
        self.ranking_view = CteiRankingView(tab_b, controller=self)
        self.trend_chart = TrendChart(tab_t, controller=self)
        self.model_compare = ModelCompareView(tab_c, controller=self)

        self._paned = paned
        root.update_idletasks()
        paned.sash_place(0, 190, 0)
        paned.sash_place(1, 420, 0)
        # 恢复上次的分栏位置（覆盖默认值；数据异常则保持默认）。
        saved = self._ui_prefs.get("sashes")
        if isinstance(saved, list) and len(saved) == 2:
            try:
                paned.sash_place(0, int(saved[0]), 0)
                paned.sash_place(1, int(saved[1]), 0)
            except (TypeError, ValueError, tk.TclError):
                pass

    # --------------------------------------------------------------- projects
    def refresh_projects(self) -> None:
        self._analysis_cancel.set()
        self._analysis_cancel = threading.Event()
        self._analysis_generation += 1
        source = self.filter.get_source()
        self._selected_project_idx = None
        self._clear_analysis_view()
        self._projects = list_project_refs(source)
        # 标记无会话项目（Claude / Codex / OpenCode / Grok 统一判定）
        self._empty_projects = {
            i for i, p in enumerate(self._projects)
            if not project_has_sessions(p)
        }
        # 启动时恢复上次选中的项目（一次性；之后的刷新回到默认选首个）。
        preferred = self._restore_project_key
        self._restore_project_key = None
        self.project_col.update(self._projects, self._empty_projects,
                                preferred_key=preferred)
        n_empty = len(self._empty_projects)
        n_live = len(self._projects) - n_empty
        status = f"发现 {len(self._projects)} 个项目"
        if n_empty:
            status += f"（{n_live} 有数据 · {n_empty} 无会话）"
        self.filter.set_status(status)

    def _clear_analysis_view(self) -> None:
        self._current = None
        self._selected_session_id = None
        self._rendered_report = None
        self.session_col.update([])
        self.ranking_view.update([])
        self.trend_chart.update([])
        self.model_compare.update([])
        self.metric_panel.clear()
        self._update_tab_names()

    def on_select_project(self, idx: int) -> None:
        self._selected_project_idx = idx
        self.reanalyze()

    def _selected_project(self):
        if self._selected_project_idx is None or self._selected_project_idx >= len(self._projects):
            return None
        return self._projects[self._selected_project_idx]

    # --------------------------------------------------------------- analysis
    def reanalyze(self) -> None:
        proj = self._selected_project()
        if proj is None:
            return
        self.filter.set_status(f"分析中… {views.project_label(proj)}")
        # Cancel any in-flight analysis so we don't pile up full JSONL walks.
        self._analysis_cancel.set()
        self._analysis_cancel = threading.Event()
        cancel_event = self._analysis_cancel
        self._analysis_generation += 1
        generation = self._analysis_generation
        params = self.filter.get_params()
        args = dict(
            project=proj.key,
            source=proj.source,
            project_ref=proj,
            task_type=params["task_type"],
            since=params["since"],
            until=params["until"],
            no_loc=self._no_loc,
            cancel_event=cancel_event,
        )
        threading.Thread(target=self._worker, args=(generation, args, cancel_event), daemon=True).start()

    def _worker(self, generation: int, args: dict, cancel_event: threading.Event) -> None:
        try:
            result = analyze.analyze_project(**args)
            if cancel_event.is_set():
                return  # superseded by a newer reanalyze
            self._q.put(("ok", generation, result))
        except analyze.AnalysisCancelled:
            return
        except Exception as e:  # noqa: BLE001 — surface any failure in the UI
            if cancel_event.is_set():
                return
            # User-facing message first; full traceback only for copy/debug.
            self._q.put((
                "err", generation,
                f"{type(e).__name__}: {e}\n\n—— 诊断信息 ——\n{traceback.format_exc()}",
            ))

    def _poll(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "ok":
                    _, generation, payload = item
                    if generation == self._analysis_generation:
                        self._on_analysis(payload)
                elif kind == "err":
                    # analysis error — gated by generation (stale if project switched)
                    _, generation, payload = item
                    if generation == self._analysis_generation:
                        self.filter.set_status("出错")
                        messagebox.showerror("TCER 分析出错", payload)
                elif kind == "overview":
                    _, rows, errors = item
                    note = f"（{errors} 个项目分析失败）" if errors else ""
                    self.filter.set_status(f"项目总览完成{note}")
                    popups.ProjectOverviewPopup(self.root, rows)
                elif kind == "upload":
                    _, dialog, ok, message = item
                    if ok:
                        self.filter.set_status(message)
                    if dialog is not None:
                        try:
                            dialog.set_status(message, error=not ok)
                        except tk.TclError:
                            pass  # dialog closed before result arrived
                # unknown kind: ignore — never unpack an unexpected tuple shape,
                # which would raise and stop _poll from rescheduling (freezes GUI).
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_analysis(self, a: analyze.ProjectAnalysis) -> None:
        proj = self._selected_project()
        if proj is None:
            return
        if a.project_ref and (
            a.project_ref.source != proj.source or a.project_ref.key != proj.key
        ):
            return
        self._current = a
        prev_sid = self._selected_session_id
        self._selected_session_id = None
        self.session_col.update(a.reports)
        # Preserve the prior selection across the refresh when it survived
        # (e.g. a reanalyze triggered indirectly by a date-filter FocusOut
        # firing as a popup closes); otherwise default to the most recent.
        # select_* set the visual selection only (notify=False) — the unified
        # render below handles metrics + trend exactly once.
        if prev_sid and self.session_col.select_by_sid(prev_sid, notify=False):
            self._selected_session_id = prev_sid
        elif a.reports:
            self._selected_session_id = self.session_col.select_first(notify=False)
        self.ranking_view.update(a.reports)
        # Trend + 模型对比 must respect the current view mode (project vs
        # session). _render_session_views handles both, plus the trend highlight.
        self._render_session_views()
        self._render_metrics()
        self._update_tab_names()
        status = f"完成 · 共 {a.n_sessions} 个会话"
        if self.filter.get_params().get("task_type") == metrics.AUTO_TASK_TYPE and a.reports:
            from collections import Counter
            counts = Counter(r.task_type for r in a.reports if r.task_type)
            if counts:
                parts = "、".join(
                    f"{metrics.TASK_CATEGORIES.get(k, {}).get('name', k)}×{n}"
                    for k, n in counts.most_common()
                )
                status += f" · 自动任务类型：{parts}"
        unmatched = metrics.unmatched_pricing_models(a.aggregate.usage)
        if unmatched:
            status += f" · ⚠ {len(unmatched)} 个模型默认价（点「模型」查看）"
        self.filter.set_status(status)

    # --------------------------------------------------------------- sessions / view
    def on_select_session(self, sid: str) -> None:
        self._selected_session_id = sid
        if self.view_mode.get() == "session":
            self._render_metrics()
            self._update_model_compare()
            # Highlight in trend without rebuilding the chart (preserves zoom).
            self.trend_chart.select_session_by_sid(sid)
        self._update_tab_names()

    def delete_session(self, report) -> None:
        """彻底删除一个会话（主文件 + subagent/tool-results 目录），随后刷新视图。

        删除按磁盘路径定位，保证不残留 subagent 数据。删除后若项目仍有会话则
        重算，否则刷新项目列表（该项目转为「无会话」）。
        """
        from tkinter import messagebox
        from tcer.core import reader

        sid = report.meta.session_id or report.meta.path.stem
        if report.meta.source in ("codex", "opencode", "grok"):
            label = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok"}.get(report.meta.source, report.meta.source)
            messagebox.showinfo("删除会话", f"{label} 会话当前仅支持只读分析，暂不删除本地会话数据。")
            return
        try:
            removed = reader.delete_session(report.meta.path)
        except OSError as e:
            messagebox.showerror("删除失败", f"无法删除会话 {sid[:16]}…\n{e}")
            return

        if self._selected_session_id == sid:
            self._selected_session_id = None
        self.session_col.clear_selection()

        proj = self._selected_project()
        if proj is not None and proj.source == "claude" and discover_jsonl(proj.key):
            self.reanalyze()
        else:
            # 最后一个会话被删 — 项目变空，回到项目列表状态。
            self._current = None
            self.refresh_projects()
        self.filter.set_status(f"已删除会话 · 移除 {len(removed)} 项磁盘对象")

    def _on_view_change(self) -> None:
        self._render_metrics()
        self._update_model_compare()
        self._update_tab_names()

    def _update_model_compare(self) -> None:
        """Update model compare based on view mode. Does not touch TrendChart."""
        if not self._current:
            return
        mode = self.view_mode.get()
        if mode == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            if report:
                self.model_compare.update([report])
        else:
            self.model_compare.update(self._current.reports)

    def _render_session_views(self) -> None:
        """Rebuild TrendChart and model compare. Called only on fresh analysis."""
        if not self._current:
            return
        self.trend_chart.update(self._current.reports)
        # Highlight selected session in trend chart
        if self._selected_session_id:
            self.trend_chart.select_session_by_sid(self._selected_session_id)
        self._update_model_compare()

    def _update_tab_names(self) -> None:
        """Update tab names with (项目) or (会话) suffix based on view mode."""
        mode = self.view_mode.get()
        suffix = "(会话)" if mode == "session" and self._selected_session_id else "(项目)"
        self._nb.tab(0, text=f"指标分类 {suffix}")
        self._nb.tab(1, text="综合效率分排名")
        self._nb.tab(2, text="趋势")
        self._nb.tab(3, text=f"模型对比 {suffix}")

    def _session_report(self, sid: str):
        for r in self._current.reports:
            if (r.meta.session_id or r.meta.path.stem) == sid:
                return r
        return None

    def _render_metrics(self) -> None:
        if not self._current:
            return
        if self.view_mode.get() == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            if report:
                self._rendered_report = report
                self.metric_panel.update(report)
                return
        self._rendered_report = self._current.aggregate
        self.metric_panel.update(self._current.aggregate)

    # --------------------------------------------------------------- popups
    def show_session_detail(self, sid: str) -> None:
        if not self._current:
            return
        report = self._session_report(sid)
        if report:
            popups.SessionDetailPopup(self.root, report)

    def show_tool_calls(self) -> None:
        if not self._current:
            return
        if self.view_mode.get() == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            usage = report.usage if report else self._current.aggregate.usage
            suffix = f" · {self._selected_session_id[:16]}…" if report else " · 项目汇总"
        else:
            usage = self._current.aggregate.usage
            suffix = " · 项目汇总"
        popups.ToolCallsPopup(self.root, usage, suffix)

    def show_models(self) -> None:
        if not self._current:
            return
        if self.view_mode.get() == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            usage = report.usage if report else self._current.aggregate.usage
            suffix = f" · {self._selected_session_id[:16]}…" if report else " · 项目汇总"
        else:
            usage = self._current.aggregate.usage
            suffix = " · 项目汇总"
        popups.ModelsPopup(self.root, usage, suffix)

    def show_cost_breakdown(self) -> None:
        if not self._current:
            return
        if self.view_mode.get() == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            usage = report.usage if report else self._current.aggregate.usage
            suffix = f" · {self._selected_session_id[:16]}…" if report else " · 项目汇总"
        else:
            usage = self._current.aggregate.usage
            suffix = " · 项目汇总"
        popups.CostBreakdownPopup(self.root, usage, suffix)

    def show_user_msgs(self) -> None:
        report = self._rendered_report
        if not report:
            messagebox.showinfo("用户消息", "当前会话未记录到用户消息。")
            return
        # 聚合视图要遍历项目全部会话文件（Claude 还含 subagents 目录），大项目
        # 在主线程读会卡死界面 — 放后台线程，读完回 Tk 主循环弹窗。
        reports = list(self._current.reports) if self._current else []
        self.filter.set_status("读取用户消息…")

        def _work() -> None:
            try:
                msgs, label = self._load_user_messages(report, reports)
                err = None
            except Exception as e:  # noqa: BLE001 — 后台线程兜底，错误回主线程展示
                msgs, label, err = None, "", str(e)
            self.root.after(0, lambda: self._show_user_msgs_done(msgs, label, err))

        threading.Thread(target=_work, daemon=True).start()

    def _show_user_msgs_done(self, msgs, label: str, err: str | None) -> None:
        self.filter.set_status("就绪")
        if err is not None:
            messagebox.showerror("用户消息", f"读取失败：{err}")
        elif msgs:
            popups.UserMsgsPopup(self.root, msgs)
        else:
            messagebox.showinfo("用户消息", f"当前 {label} 会话未记录到用户消息。")

    @staticmethod
    def _load_user_messages(report, reports) -> tuple[list[str], str]:
        """读取一个 report（会话或聚合）的全部用户消息文本（仅文件 IO，线程安全）。"""
        source = report.meta.source or "claude"
        is_agg = report.meta.session_id == "(aggregate)"
        msgs: list[str] = []

        if source == "codex":
            from tcer.core import codex_reader
            if is_agg:
                for r in reports:
                    msgs.extend(codex_reader.read_user_messages(r.meta.path))
            else:
                msgs = codex_reader.read_user_messages(report.meta.path)
            label = "Codex"
        elif source == "opencode":
            from tcer.core import opencode_reader
            if is_agg:
                for r in reports:
                    sid = r.meta.session_id
                    if sid:
                        msgs.extend(opencode_reader.read_user_messages(r.meta.path, sid))
            elif report.meta.session_id:
                msgs = opencode_reader.read_user_messages(report.meta.path, report.meta.session_id)
            label = "OpenCode"
        elif source == "grok":
            from tcer.core import grok_reader
            if is_agg:
                for r in reports:
                    msgs.extend(grok_reader.read_user_messages(r.meta.path))
            else:
                msgs = grok_reader.read_user_messages(report.meta.path)
            label = "Grok"
        else:
            # Claude: prefer cached texts (legacy), else lazy-read main + subagent files.
            if report.usage.user_message_texts:
                msgs = list(report.usage.user_message_texts)
            elif is_agg:
                for r in reports:
                    msgs.extend(TcerGui._claude_user_messages(r))
            else:
                msgs = TcerGui._claude_user_messages(report)
            label = "Claude"
        return msgs, label

    @staticmethod
    def _claude_user_messages(report) -> list[str]:
        """Lazy-load Claude user texts for one session (main + subagent jsonl)."""
        from tcer.core import reader
        path = report.meta.path
        if path is None:
            return []
        try:
            _, main, session_dir = reader.session_artifacts(path)
        except (OSError, ValueError, IndexError):
            # 路径形态异常（非标准会话布局）→ 退回按单文件读取。
            return reader.read_user_messages(path) if path.is_file() else []
        msgs: list[str] = []
        if main.is_file():
            msgs.extend(reader.read_user_messages(main))
        sub_dir = session_dir / "subagents"
        if sub_dir.is_dir():
            for f in sorted(sub_dir.glob("*.jsonl")):
                msgs.extend(reader.read_user_messages(f))
        return msgs

    def show_files_touched(self) -> None:
        report = self._rendered_report
        if report and report.files_touched_details:
            popups.FilesTouchedPopup(self.root, report.files_touched_details)
        else:
            messagebox.showinfo("涉及文件", "当前会话未涉及任何文件操作。")

    def show_memory_files(self) -> None:
        report = self._rendered_report
        # memory_files 是项目级指标，只在聚合报告上有数据
        agg = self._current.aggregate if self._current else None
        if agg and agg.memory_files is not None and agg.memory_dir:
            popups.MemoryFilesPopup(self.root, agg.memory_dir, agg.memory_files)
        else:
            messagebox.showinfo("项目记忆文件", "当前项目没有 memory/ 目录或目录为空。")

    # --------------------------------------------------------------- tools
    def compute_baselines(self) -> None:
        if not self._current:
            messagebox.showinfo("计算基准", "请先分析一个项目。")
            return
        eligible = metrics.baseline_eligible_reports(self._current.reports)
        need = metrics.MIN_BASELINE_SESSIONS
        values = metrics.compute_baselines(self._current.reports)
        if values is None:
            if len(eligible) == 0:
                msg = "没有可参与计算的会话（需同时具备 TCER 与 CPE，即有效净增行与成本）。"
            else:
                msg = (
                    f"有效会话不足：需要至少 {need} 个完整会话，"
                    f"当前仅 {len(eligible)} 个。\n"
                    "样本过少时中位数波动很大，暂不建议写入个人基准。"
                )
            messagebox.showinfo("计算基准", msg)
            return

        def _apply(v):
            metrics.save_baselines(v)
            self.reanalyze()

        popups.BaselinesPopup(self.root, values, len(eligible), _apply)

    def show_tool_sequence(self) -> None:
        report = self._rendered_report
        if not report or len(report.usage.tool_ops) < 2:
            self.filter.set_status("工具调用不足，无法分析序列")
            return
        suffix = (" · 项目汇总" if report.meta.session_id == "(aggregate)"
                  else f" · {(report.meta.session_id or '')[:16]}…")
        popups.ToolSequencePopup(self.root, report.usage, suffix)

    def show_project_overview(self) -> None:
        """跨项目总览：后台分析全部项目（走 mtime 缓存），弹窗可排序对比。"""
        if not self._projects:
            self.filter.set_status("无项目可汇总")
            return
        params = self.filter.get_params()
        projects = list(self._projects)
        self.filter.set_status(f"项目总览计算中…（{len(projects)} 个项目）")

        def _work() -> None:
            rows = []
            errors = 0
            for p in projects:
                try:
                    a = analyze.analyze_project(
                        p.key, source=getattr(p, "source", "claude"),
                        project_ref=p if hasattr(p, "session_paths") else None,
                        task_type=params["task_type"],
                        since=params["since"], until=params["until"],
                        no_loc=self._no_loc,
                    )
                except Exception:  # noqa: BLE001 — 单项目失败不拦总览
                    errors += 1
                    continue
                if a.n_sessions:
                    rows.append((p, a))
            self._q.put(("overview", rows, errors))

        threading.Thread(target=_work, daemon=True).start()

    def show_session_timeline(self) -> None:
        if not self._current:
            self.filter.set_status("无数据")
            return
        sid = self._selected_session_id
        report = self._session_report(sid) if sid else None
        if report is None:
            self.filter.set_status("请先在会话列表选中一个会话")
            return
        if not report.usage.turn_stats:
            self.filter.set_status("该会话未记录逐回合数据")
            return
        popups.SessionTimelinePopup(self.root, report)

    def show_session_compare(self) -> None:
        if not self._current or len(self._current.reports) < 2:
            self.filter.set_status("至少需要 2 个会话才能对比")
            return
        popups.SessionComparePopup(self.root, self._current.reports,
                                   preselect_sid=self._selected_session_id)

    def show_advanced(self) -> None:
        def _apply(no_loc):
            self._no_loc = no_loc
            self.reanalyze()

        popups.AdvancedPopup(self.root, self._no_loc, _apply)

    # --------------------------------------------------------------- export
    def export(self, fmt: str, scope: str = "project") -> None:
        if not self._current:
            self.filter.set_status("无数据可导出")
            return
        a = self._current
        report = None
        if scope == "session":
            sid = self._selected_session_id
            report = self._session_report(sid) if sid else None
            if report is None:
                self.filter.set_status("未选中会话，无法导出会话报告")
                return
        proj = self._selected_project()
        proj_name = views.project_label(proj) if proj is not None else a.project_hash
        source_label = (views.project_source_label(proj) if proj is not None
                        else (a.source or "claude").capitalize())
        if scope == "session":
            stem = f"tcer-会话-{(self._selected_session_id or 'session')[:12]}"
        else:
            stem = f"tcer-报告-{proj_name}"
        stem = "".join(ch for ch in stem if ch not in '<>:"/\\|?*').strip() or "tcer-report"
        ext = {"json": "json", "csv": "csv", "md": "md", "html": "html"}[fmt]
        path = filedialog.asksaveasfilename(
            defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")],
            initialfile=f"{stem}.{ext}",
        )
        if not path:
            return
        try:
            if scope == "session":
                if fmt == "html":
                    content = html_report.render_session_html(
                        report, project_name=proj_name, source_label=source_label)
                elif fmt == "json":
                    content = export_mod.session_to_json(report)
                else:
                    content = export_mod.to_markdown(
                        [report], report, 1, project_name=proj_name)
            elif fmt == "html":
                content = html_report.render_project_html(
                    a.reports, a.aggregate, project_name=proj_name,
                    source_label=source_label, n_sessions=a.n_sessions,
                    n_subagents=a.n_subagents)
            elif fmt == "json":
                content = export_mod.to_json(a.reports, a.aggregate, a.n_sessions)
            elif fmt == "csv":
                content = export_mod.to_csv(a.reports)
            else:
                content = export_mod.to_markdown(a.reports, a.aggregate, a.n_sessions,
                                                 project_name=proj_name)
            Path(path).write_text(content, encoding="utf-8")
            self.filter.set_status(f"已导出 → {Path(path).name}")
        except OSError as e:
            messagebox.showerror("导出失败", str(e))

    # --------------------------------------------------------------- upload
    def show_upload(self) -> None:
        projects = [(p.key, f"[{views.project_source_label(p)}] {views.project_label(p)}")
                    for p in self._projects]
        default_proj = None
        proj = self._selected_project()
        if proj is not None:
            default_proj = proj.key
        popups.UploadDialog(
            self.root,
            prefs=self._upload_prefs,
            projects=projects,
            default_project=default_proj,
            on_upload=self._start_upload,
            on_save_prefs=self._save_upload_prefs,
        )

    def _save_upload_prefs(self, prefs: dict) -> None:
        self._upload_prefs = prefs
        try:
            upload_prefs.save(prefs)
        except OSError:
            pass  # non-fatal — prefs just won't persist across restarts
        # (Re)arm or cancel the auto-upload timer to match the new setting.
        self._schedule_auto_upload()

    def _project_ref_by_key(self, key: str):
        for p in self._projects:
            if p.key == key:
                return p
        return None

    def _start_upload(self, prefs: dict, dialog=None) -> None:
        """Analyze each selected project fresh, then upload its own report.

        The earlier version reused ``self._current`` and merely relabelled it
        with the chosen project name — so every project uploaded identical data.
        Here each selected key is re-analyzed on a worker thread so each upload
        carries that project's real aggregate (+ sessions when 全部会话 is on).
        Returns immediately; the combined result arrives via the queue.
        """
        keys = list(prefs.get("last_projects") or [])
        if not keys:
            if dialog is not None:
                dialog.set_status("请至少选择一个项目", error=True)
            return
        refs = [(k, self._project_ref_by_key(k)) for k in keys]
        missing = [k for k, r in refs if r is None]
        refs = [(k, r) for k, r in refs if r is not None]
        if not refs:
            if dialog is not None:
                dialog.set_status("选中的项目已不存在，请刷新后重试", error=True)
            return
        params = self.filter.get_params()
        analysis_args = dict(
            task_type=params["task_type"],
            since=params["since"],
            until=params["until"],
            no_loc=self._no_loc,
        )
        threading.Thread(
            target=self._upload_worker,
            args=(prefs, refs, missing, analysis_args, dialog),
            daemon=True,
        ).start()

    def _upload_worker(self, prefs, refs, missing, analysis_args, dialog) -> None:
        """Off-thread: analyze + upload each selected project, aggregate results.

        Login happens once; per-project failures are collected without aborting
        the rest. Each project is analyzed fresh so its payload carries that
        project's own aggregate (and sessions when detail is on).
        """
        user = prefs.get("username") or None
        anonymous = bool(prefs.get("anonymous"))
        detail = bool(prefs.get("detail"))
        server_url = prefs["server_url"]

        # Anonymous uploads skip login entirely — the server accepts them with no
        # bearer token. Non-anonymous uploads still exchange credentials first.
        if anonymous:
            token = None
        else:
            try:
                token = upload_client.login(server_url, prefs["username"],
                                            prefs.get("password", ""))
            except upload_client.UploadError as e:
                self._q.put(("upload", dialog, False, f"登录失败：{e}"))
                return
            except Exception as e:  # noqa: BLE001
                self._q.put(("upload", dialog, False, f"登录出错：{e}"))
                return

        total_inserted = 0
        ok_projects = 0
        errors: list[str] = []
        for key, ref in refs:
            label = views.project_label(ref)
            try:
                a = analyze.analyze_project(
                    project=ref.key, source=ref.source, project_ref=ref,
                    **analysis_args,
                )
                payload = upload_client.build_payload(
                    aggregate=a.aggregate, reports=a.reports,
                    n_sessions=a.n_sessions, project=key, user=user,
                    anonymous=anonymous, detail=detail,
                )
                total_inserted += upload_client.upload(server_url, token, payload)
                ok_projects += 1
            except Exception as e:  # noqa: BLE001 — collect per-project failures
                errors.append(f"{label}: {e}")

        ok = ok_projects > 0 and not errors
        parts = [f"上传完成 · {ok_projects}/{len(refs)} 个项目 · 写入 {total_inserted} 条记录"]
        if missing:
            parts.append(f"（{len(missing)} 个已不存在，已跳过）")
        if errors:
            shown = "；".join(errors[:3])
            parts.append(f"失败：{shown}" + (f" 等 {len(errors)} 项" if len(errors) > 3 else ""))
        self._q.put(("upload", dialog, ok, " ".join(parts)))

    def _schedule_auto_upload(self) -> None:
        """Arm (or cancel) the background auto-upload timer per prefs."""
        if self._auto_upload_after is not None:
            try:
                self.root.after_cancel(self._auto_upload_after)
            except (ValueError, tk.TclError):
                pass
            self._auto_upload_after = None
        if not self._upload_prefs.get("auto_upload"):
            return
        interval_min = int(self._upload_prefs.get("interval_min", 30) or 30)
        self._auto_upload_after = self.root.after(
            max(1, interval_min) * 60_000, self._auto_upload_tick)

    def _auto_upload_tick(self) -> None:
        """Timer callback: silently upload remembered projects, then re-arm."""
        self._auto_upload_after = None
        prefs = self._upload_prefs
        if (prefs.get("server_url") and prefs.get("last_projects")
                and (prefs.get("anonymous") or prefs.get("username"))):
            self._start_upload(prefs, dialog=None)
        self._schedule_auto_upload()

    # --------------------------------------------------------------- entry
    @classmethod
    def run(cls) -> int:
        try:
            import tkinter as tk
        except ImportError:
            print("error: tkinter is not available in this Python build.")
            return 1
        _enable_windows_hidpi()
        root = tk.Tk()
        _apply_tk_scaling(root)
        cls(root)
        root.mainloop()
        return 0


def _enable_windows_hidpi() -> None:
    """Windows 高 DPI 感知。必须在创建 Tk 之前调用——否则系统把 96 DPI
    位图拉伸到高分屏，整个界面发糊（观感上「不精致」的最大来源）。"""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_DPI_AWARE
    except (OSError, AttributeError):
        pass


def _apply_tk_scaling(root) -> None:
    """按实际 DPI 设置 Tk 缩放，点单位字体随之放大到物理正确尺寸。"""
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
