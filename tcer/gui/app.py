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
from tcer.core import upload_client, upload_prefs
from tcer.core.calibrate import calibrate_project
from tcer.core.paths import list_project_refs
from tcer.core.reader import discover_jsonl
from . import popups, theme, views
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
        self._code_dir: str | None = None
        self._no_loc: bool = False
        self._scan_code_dir: bool = False
        self._analysis_generation = 0
        self._upload_prefs: dict = upload_prefs.load()
        self._auto_upload_after: str | None = None

        root.title("TCER — Token 转码效率计量")
        root.configure(bg=theme.BG)
        theme.setup_style(ttk)

        # Center window on screen (shifted up slightly to account for taskbar)
        w, h = 1600, 900
        sx = root.winfo_screenwidth()
        sy = root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sx - w) // 2}+{(sy - h) // 2 - 40}")

        self.filter = FilterBar(root, self)
        self._build_body(root)
        self.refresh_projects()
        root.after(100, self._poll)
        if self._upload_prefs.get("auto_upload"):
            self._schedule_auto_upload()

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

        root.update_idletasks()
        paned.sash_place(0, 190, 0)
        paned.sash_place(1, 420, 0)

    # --------------------------------------------------------------- projects
    def refresh_projects(self) -> None:
        self._analysis_generation += 1
        source = self.filter.get_source()
        self._selected_project_idx = None
        self._clear_analysis_view()
        self._projects = list_project_refs(source)
        # 标记哪些项目没有会话数据（置灰显示）
        self._empty_projects = {
            i for i, p in enumerate(self._projects)
            if p.source == "claude" and not discover_jsonl(p.key)
        }
        self.project_col.update(self._projects, self._empty_projects)
        n_empty = len(self._empty_projects)
        status = f"发现 {len(self._projects)} 个项目"
        if n_empty:
            status += f"（{n_empty} 个无会话数据）"
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
            code_dir=self._code_dir,
            no_loc=self._no_loc,
            scan_code_dir=self._scan_code_dir,
        )
        threading.Thread(target=self._worker, args=(generation, args), daemon=True).start()

    def _worker(self, generation: int, args: dict) -> None:
        try:
            result = analyze.analyze_project(**args)
            self._q.put(("ok", generation, result))
        except Exception as e:  # noqa: BLE001 — surface any failure in the UI
            self._q.put(("err", generation, f"{e}\n{traceback.format_exc()}"))

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
                elif kind == "calibration":
                    _, payload = item
                    cals, text_report = payload
                    self.filter.set_status("校准完成")
                    popups.CalibratePopup(self.root, cals, text_report)
                elif kind == "calibration_err":
                    # calibration is user-initiated — no generation gate
                    _, payload = item
                    self.filter.set_status("校准出错")
                    messagebox.showerror("LOC 校准出错", payload)
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
        self.filter.set_status(f"完成 · 共 {a.n_sessions} 个会话")

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
        if report and report.meta.source == "codex":
            from tcer.core import codex_reader
            msgs: list[str] = []
            if report.meta.session_id == "(aggregate)" and self._current:
                for r in self._current.reports:
                    msgs.extend(codex_reader.read_user_messages(r.meta.path))
            else:
                msgs = codex_reader.read_user_messages(report.meta.path)
            if msgs:
                popups.UserMsgsPopup(self.root, msgs)
            else:
                messagebox.showinfo("用户消息", "当前 Codex 会话未记录到用户消息。")
            return
        if report and report.meta.source == "opencode":
            from tcer.core import opencode_reader
            msgs: list[str] = []
            if report.meta.session_id == "(aggregate)" and self._current:
                for r in self._current.reports:
                    sid = r.meta.session_id
                    if sid:
                        msgs.extend(opencode_reader.read_user_messages(r.meta.path, sid))
            elif report.meta.session_id:
                msgs = opencode_reader.read_user_messages(report.meta.path, report.meta.session_id)
            if msgs:
                popups.UserMsgsPopup(self.root, msgs)
            else:
                messagebox.showinfo("用户消息", "当前 OpenCode 会话未记录到用户消息。")
            return
        if report and report.meta.source == "grok":
            from tcer.core import grok_reader
            msgs: list[str] = []
            if report.meta.session_id == "(aggregate)" and self._current:
                for r in self._current.reports:
                    msgs.extend(grok_reader.read_user_messages(r.meta.path))
            else:
                msgs = grok_reader.read_user_messages(report.meta.path)
            if msgs:
                popups.UserMsgsPopup(self.root, msgs)
            else:
                messagebox.showinfo("用户消息", "当前 Grok 会话未记录到用户消息。")
            return
        if report and report.usage.user_message_texts:
            popups.UserMsgsPopup(self.root, report.usage.user_message_texts)
        else:
            messagebox.showinfo("用户消息", "当前会话未记录到用户消息。")

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
    def run_calibration(self) -> None:
        proj = self._selected_project()
        if proj is None:
            messagebox.showinfo("LOC 校准", "请先选择一个项目。")
            return
        if proj.source in ("codex", "opencode", "grok"):
            label = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok"}.get(proj.source, proj.source)
            messagebox.showinfo("LOC 校准", f"{label} 会话当前仅支持只读分析，暂不支持 LOC 校准。")
            return
        self.filter.set_status("校准中…")
        threading.Thread(target=self._calibration_worker, args=(proj.key,),
                         daemon=True).start()

    def _calibration_worker(self, project: str) -> None:
        try:
            cals = calibrate_project(project, code_dir=self._code_dir)
            lines = []
            for cal in cals:
                lines.append(f"{cal.session_id[:38]}  "
                             f"工具 +{cal.tcer_added} -{cal.tcer_deleted}  "
                             f"git +{cal.git_added} -{cal.git_deleted}  "
                             f"偏差 {cal.net_deviation:+d}")
            self._q.put(("calibration", (cals, "\n".join(lines))))
        except Exception as e:  # noqa: BLE001
            self._q.put(("calibration_err", f"校准出错: {e}"))

    def compute_baselines(self) -> None:
        if not self._current:
            messagebox.showinfo("计算基准", "请先分析一个项目。")
            return
        values = metrics.compute_baselines(self._current.reports)
        if values is None:
            messagebox.showinfo("计算基准", "没有足够数据的会话来计算基准。")
            return

        def _apply(v):
            metrics.save_baselines(v)
            self.reanalyze()

        popups.BaselinesPopup(self.root, values, self._current.n_sessions, _apply)

    def show_advanced(self) -> None:
        def _apply(code_dir, no_loc, scan_code_dir):
            self._code_dir = code_dir
            self._no_loc = no_loc
            self._scan_code_dir = scan_code_dir
            self.reanalyze()

        popups.AdvancedPopup(self.root, self._code_dir or "", self._no_loc,
                             self._scan_code_dir, _apply)

    # --------------------------------------------------------------- export
    def export(self, fmt: str) -> None:
        if not self._current:
            self.filter.set_status("无数据可导出")
            return
        ext = {"json": "json", "csv": "csv", "md": "md"}[fmt]
        path = filedialog.asksaveasfilename(
            defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")],
            initialfile=f"tcer-report.{ext}",
        )
        if not path:
            return
        a = self._current
        try:
            if fmt == "json":
                content = export_mod.to_json(a.reports, a.aggregate, a.n_sessions)
            elif fmt == "csv":
                content = export_mod.to_csv(a.reports)
            else:
                content = export_mod.to_markdown(a.reports, a.aggregate, a.n_sessions,
                                                 a.code_dir, project_name=a.project_hash)
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
            code_dir=self._code_dir,
            no_loc=self._no_loc,
            scan_code_dir=self._scan_code_dir,
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
        if (prefs.get("server_url") and prefs.get("username")
                and prefs.get("last_projects")):
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
        root = tk.Tk()
        cls(root)
        root.mainloop()
        return 0
