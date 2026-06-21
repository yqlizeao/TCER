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
from tcer.core.calibrate import calibrate_project
from tcer.core.paths import list_projects
from tcer.core.reader import discover_jsonl
from . import popups, theme, views
from .views import CteiRankingView, FilterBar, MetricPanel, ProjectColumn, SessionColumn, TrendChart


class TcerGui:
    def __init__(self, root) -> None:
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._projects: list[Path] = []
        self._current: analyze.ProjectAnalysis | None = None
        self._selected_project_idx: int | None = None
        self._selected_session_id: str | None = None
        self.view_mode = tk.StringVar(value="project")
        self._rendered_report = None  # last report rendered in MetricPanel (for popups)
        self._code_dir: str | None = None
        self._no_loc: bool = False

        root.title("TCER — Token 转码效率计量")
        root.geometry("1600x900")
        root.configure(bg=theme.BG)
        theme.setup_style(ttk)

        self.filter = FilterBar(root, self)
        self._build_body(root)
        self.refresh_projects()
        root.after(100, self._poll)

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
        nb.add(tab_m, text="指标分类")
        nb.add(tab_b, text="综合效率指数排名")
        nb.add(tab_t, text="趋势")

        self.metric_panel = MetricPanel(tab_m, self)
        self.ranking_view = CteiRankingView(tab_b, controller=self)
        self.trend_chart = TrendChart(tab_t, controller=self)

        root.update_idletasks()
        paned.sash_place(0, 190, 0)
        paned.sash_place(1, 420, 0)

    # --------------------------------------------------------------- projects
    def refresh_projects(self) -> None:
        self._projects = list_projects()
        # 标记哪些项目没有会话数据（置灰显示）
        self._empty_projects = {
            i for i, p in enumerate(self._projects)
            if not discover_jsonl(p.name)
        }
        self.project_col.update(self._projects, self._empty_projects)
        n_empty = len(self._empty_projects)
        status = f"发现 {len(self._projects)} 个项目"
        if n_empty:
            status += f"（{n_empty} 个无会话数据）"
        self.filter.set_status(status)

    def on_select_project(self, idx: int) -> None:
        self._selected_project_idx = idx
        self.reanalyze()

    def _selected_project(self) -> Path | None:
        if self._selected_project_idx is None or self._selected_project_idx >= len(self._projects):
            return None
        return self._projects[self._selected_project_idx]

    # --------------------------------------------------------------- analysis
    def reanalyze(self) -> None:
        proj = self._selected_project()
        if proj is None:
            return
        self.filter.set_status(f"分析中… {views._short_name(proj.name)}")
        params = self.filter.get_params()
        args = dict(
            project=proj.name,
            task_type=params["task_type"],
            since=params["since"],
            until=params["until"],
            code_dir=self._code_dir,
            no_loc=self._no_loc,
        )
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _worker(self, args: dict) -> None:
        try:
            result = analyze.analyze_project(**args)
            self._q.put(("ok", result))
        except Exception as e:  # noqa: BLE001 — surface any failure in the UI
            self._q.put(("err", f"{e}\n{traceback.format_exc()}"))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "ok":
                    self._on_analysis(payload)
                elif kind == "calibration":
                    cals, text_report = payload
                    self.filter.set_status("校准完成")
                    popups.CalibratePopup(self.root, cals, text_report)
                else:
                    self.filter.set_status("出错")
                    messagebox.showerror("TCER 分析出错", payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_analysis(self, a: analyze.ProjectAnalysis) -> None:
        self._current = a
        self._selected_session_id = None
        self.session_col.update(a.reports)
        self.session_col.clear_selection()
        self.ranking_view.update(a.reports)
        self.trend_chart.update(a.reports)
        self._render_metrics()
        self.filter.set_status(f"完成 · 共 {a.n_sessions} 个会话")

    # --------------------------------------------------------------- sessions / view
    def on_select_session(self, sid: str) -> None:
        self._selected_session_id = sid
        if self.view_mode.get() == "session":
            self._render_metrics()

    def _on_view_change(self) -> None:
        self._render_metrics()

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

    # --------------------------------------------------------------- tools
    def run_calibration(self) -> None:
        proj = self._selected_project()
        if proj is None:
            messagebox.showinfo("LOC 校准", "请先选择一个项目。")
            return
        self.filter.set_status("校准中…")
        threading.Thread(target=self._calibration_worker, args=(proj.name,),
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
            self._q.put(("err", f"校准出错: {e}"))

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
        def _apply(code_dir, no_loc):
            self._code_dir = code_dir
            self._no_loc = no_loc
            self.reanalyze()

        popups.AdvancedPopup(self.root, self._code_dir or "", self._no_loc, _apply)

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
