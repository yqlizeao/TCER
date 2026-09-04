"""GUI controller: owns state + background analysis, wires views together.

The controller is the only place that touches ``analyze`` / ``export`` and
``export`` and threads. Views are stateless presenters that call back into it
(``reanalyze`` / ``on_select_project`` / ``export`` / …). Analysis runs on a
daemon thread; results come back through a queue polled from the Tk main loop.
"""
from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tcer import __version__
from tcer.core import analyze, export as export_mod, metrics
from tcer.core import ui_prefs, update_check, updater, upload_client, upload_prefs
from tcer.core.paths import (
    list_project_refs, project_has_sessions, project_latest_activity_ms,
    ref_root, since_date_to_ms,
)
from tcer.core.reader import discover_jsonl
from . import html_report, popups, theme, views
from .views import ScoreRankingView, FilterBar, MetricPanel, ModelCompareView, ProjectColumn, SessionColumn, TrendChart


# 发布版(PyInstaller 打包,sys.frozen=True)默认开启「启动时自动检查更新」;
# 源码运行(python -m tcer,frozen 不存在)默认关闭——开发者无需每次启动查更新。
_DEFAULT_AUTO_CHECK = bool(getattr(sys, "frozen", False))


class TcerGui:
    def __init__(self, root) -> None:
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._projects: list = []
        self._activity_cache: dict[int, int | None] = {}
        self._current: analyze.ProjectAnalysis | None = None
        self._selected_project_idx: int | None = None
        self._selected_session_id: str | None = None
        self.view_mode = tk.StringVar(value="project")
        self._rendered_report = None  # last report rendered in MetricPanel (for popups)
        self._no_loc: bool = False
        self._analysis_generation = 0
        self._analysis_cancel = threading.Event()
        self._upload_prefs: dict = upload_prefs.load()

        root.title("TCER")
        root.configure(bg=theme.BG)
        theme.setup_style(ttk)
        # Combobox 下拉列表是独立 Listbox，不吃 ttk style，只能经 option db 深色化。
        for opt, val in (("*TCombobox*Listbox*background", theme.PANEL),
                         ("*TCombobox*Listbox*foreground", theme.FG),
                         ("*TCombobox*Listbox*selectBackground", theme.ACCENT),
                         ("*TCombobox*Listbox*selectForeground", theme.FG_WHITE),
                         # 去掉 tk 按钮/复选/单选点击后的聚焦框（Entry 不在此列——
                         # 日期/搜索框的边框靠各自显式 highlightthickness=1 保留）。
                         ("*Button*highlightThickness", 0),
                         ("*Checkbutton*highlightThickness", 0),
                         ("*Radiobutton*highlightThickness", 0),
                         # 去掉右键/下拉菜单的系统边框：Menu 的 borderWidth/activeBorderWidth
                         # 默认 1，边色随系统主题（浅色系统下是一圈很宽的白边）。
                         ("*Menu*borderWidth", 0),
                         ("*Menu*activeBorderWidth", 0)):
            root.option_add(opt, val)

        # 界面偏好：恢复上次窗口几何，否则居中（略上移避开任务栏）。
        self._ui_prefs = ui_prefs.load()
        # 「启动时自动检查更新」默认值随运行形态:发布版默认开、源码默认关;
        # 联网仅查公开 Release 信息、后台静默、不发送任何用户数据。
        self._ui_prefs.setdefault("check_update_on_start", _DEFAULT_AUTO_CHECK)
        # 会话级用户标记（置顶 / 红旗）：复合 key 列表，跨重启保留、按项目隔离。
        self._pinned_keys = self._ui_prefs.setdefault("pinned_sessions", [])
        self._flagged_keys = self._ui_prefs.setdefault("flagged_sessions", [])
        self._restore_project_uid = self._ui_prefs.get("last_project")
        sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
        fitted = clamp_geometry(self._ui_prefs.get("geometry") or "", sx, sy)
        if fitted is not None:
            root.geometry(fitted)
        else:
            # 默认尺寸随屏幕收缩（小屏笔记本/部分 mac 放不下固定 1600×900）
            w, h = min(1600, int(sx * 0.92)), min(900, int(sy * 0.86))
            root.geometry(f"{w}x{h}+{(sx - w) // 2}+{max(0, (sy - h) // 2 - 40)}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.filter = FilterBar(root, self)
        self.filter.restore_prefs(self._ui_prefs)
        self._build_body(root)
        self.refresh_projects()
        root.after(100, self._poll)
        if self._ui_prefs.get("check_update_on_start"):
            # opt-in 启动自动检查更新:延后 2s 避开启动繁忙,仅「有新版」才弹窗
            root.after(2000, lambda: self.check_for_update(silent=True))

    # ------------------------------------------------------- update checking
    def check_for_update(self, silent: bool = False) -> None:
        """后台查 GitHub 最新版,回主线程弹「检查更新」窗口。

        silent=True(启动自动检查):仅当有新版才弹,失败/最新不打扰。
        silent=False(用户点按钮):总是弹窗(含「已是最新 / 检查失败」)。
        联网在 daemon 线程进行,绝不阻塞 Tk 主循环。
        """
        def _work():
            release = update_check.latest_release()
            try:
                self.root.after(0, lambda: self._show_update(release, silent))
            except tk.TclError:
                pass  # 窗口已关闭

        threading.Thread(target=_work, daemon=True).start()

    def _show_update(self, release, silent: bool) -> None:
        if silent and (release is None
                       or not update_check.is_newer(release["tag"], __version__)):
            return  # 静默模式:无新版/失败都不打扰
        popups.UpdatePopup(self.root, __version__, release, controller=self)

    def auto_check_enabled(self) -> bool:
        """是否已开启「启动时自动检查更新」(供工具菜单显示 ●/○ 勾选态)。

        默认值随运行形态:发布版(sys.frozen)默认开,源码默认关;
        用户手动改过后以其选择为准。
        """
        return bool(self._ui_prefs.get("check_update_on_start", _DEFAULT_AUTO_CHECK))

    def toggle_auto_check(self) -> None:
        """翻转「启动时自动检查更新」开关并即时落盘(供工具菜单点击)。"""
        self._ui_prefs["check_update_on_start"] = not self.auto_check_enabled()
        try:
            ui_prefs.save(self._ui_prefs)
        except Exception:
            pass

    def start_self_update(self, release, popup):
        """后台下载新版本 → 替换当前可执行文件 → 重启(仅发布版,按钮已校验)。

        进度经 ``root.after`` 回主线程更新弹窗(后台线程不直接碰 Tk 控件)。
        """
        asset = updater.asset_for_current_platform(release)
        if asset is None:
            self.root.after(0, lambda: popup.set_progress("未找到当前平台的安装包,请手动下载。"))
            return
        name, url = asset
        dest = updater.download_target()

        def _work():
            try:
                def cb(d, t):
                    msg = f"下载中… {d // 1024} KB" + (f" / {t // 1024} KB" if t else "")
                    try:
                        self.root.after(0, lambda m=msg: popup.set_progress(m))
                    except tk.TclError:
                        pass
                self.root.after(0, lambda: popup.set_progress(f"正在下载 {name}…"))
                updater.download(url, dest, progress_cb=cb)
                self.root.after(0, lambda: popup.set_progress("下载完成,即将重启以完成更新…"))
                updater.apply_and_restart(dest)
                self.root.after(800, self._quit_for_update)
            except Exception as e:
                self.root.after(0, lambda: popup.set_progress(f"更新失败:{e}"))
                self.root.after(0, lambda: popup.offer_manual_download(release.get("url", "")))

        threading.Thread(target=_work, daemon=True).start()

    def _quit_for_update(self):
        """下载替换就绪后,退出当前进程让新版本接管。"""
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        raise SystemExit(0)

    def _on_close(self) -> None:
        """关闭时保存界面偏好（几何/分栏/筛选/项目），失败不拦退出。

        更新 ``self._ui_prefs`` 后整体落盘（而非字面量 dict 覆盖），这样 ``upload``
        等本进程未持有的段不会被抹掉——UploadDialog 写入的上传配置得以保留。
        """
        try:
            proj = self._selected_project()
            params = self.filter.get_params()
            self._ui_prefs.update({
                "geometry": self.root.geometry(),
                "sashes": [self._paned.sash_coord(i)[0] for i in (0, 1)],
                "source": self.filter.get_source(),
                "task_type": params.get("task_type"),
                "until": params.get("until"),
                "last_project": views.ref_uid(proj) if proj else None,
                "check_update_on_start": self._ui_prefs.get("check_update_on_start", False),
                "pinned_sessions": self._pinned_keys,
                "flagged_sessions": self._flagged_keys,
            })
            ui_prefs.save(self._ui_prefs)
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
        self._dirty_tabs: dict[int, bool] = {}  # 页签 → 待补渲染是否需重灌数据
        # 切页签时补渲染被惰性跳过的视图（见 _render_active_tab）。
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._render_tab(
            self._nb.index(self._nb.select())))
        tab_m = tk.Frame(nb, bg=theme.BG)
        tab_b = tk.Frame(nb, bg=theme.PANEL)
        tab_t = tk.Frame(nb, bg=theme.PANEL)
        tab_c = tk.Frame(nb, bg=theme.PANEL)
        tab_r = tk.Frame(nb, bg=theme.PANEL)
        tab_l = tk.Frame(nb, bg=theme.BG)
        nb.add(tab_m, text="指标分类", image=views.ui_icon(nb, "grid"), compound="left")
        nb.add(tab_c, text="模型对比", image=views.ui_icon(nb, "compare"), compound="left")
        nb.add(tab_b, text="效率榜", image=views.ui_icon(nb, "rank"), compound="left")
        nb.add(tab_t, text="趋势", image=views.ui_icon(nb, "trend"), compound="left")
        nb.add(tab_r, text="项目聚合", image=views.ui_icon(nb, "layers"), compound="left")
        nb.add(tab_l, text="LLM 报告", image=views.ui_icon(nb, "chat"), compound="left")

        self.metric_panel = MetricPanel(tab_m, self)
        self.ranking_view = ScoreRankingView(tab_b, controller=self)
        self.trend_chart = TrendChart(tab_t, controller=self)
        self.model_compare = ModelCompareView(tab_c, controller=self)
        self.real_projects_view = views.RealProjectsView(tab_r, controller=self)
        self.llm_reports_view = views.LlmReportsView(tab_l, controller=self)
        self._llm_tab = tab_l
        # 项目聚合页签独立于当前分析（后台扫全部项目），首次切入加载。
        self._realproj_loaded = False
        self._realproj_scanning = False

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
        # 标记无会话项目（Claude / Codex / OpenCode / Grok / Oh My Pi 统一判定）
        self._empty_projects = {
            i for i, p in enumerate(self._projects)
            if not project_has_sessions(p)
        }
        # 每个项目最近活动时间（切 source 时算一次；切时间筛选只比缓存，零 IO）。
        self._activity_cache = {
            i: project_latest_activity_ms(p) for i, p in enumerate(self._projects)
        }
        # 启动时恢复上次选中的项目（一次性；之后的刷新回到默认选首个）。
        preferred = self._restore_project_uid
        self._restore_project_uid = None
        self.project_col.update(self._projects, self._empty_projects,
                                preferred_uid=preferred,
                                hidden_projects=self._compute_hidden())
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

    def _compute_hidden(self) -> set[int]:
        """按当前 since 隐藏「最近活动早于 since」或无活动的项目 idx。

        until 不参与左栏（仅影响会话级 reanalyze）。无 since → 空集（全显）。
        """
        since_ms = since_date_to_ms(self.filter.get_params().get("since"))
        if since_ms is None:
            return set()
        return {
            i for i in range(len(self._projects))
            if (self._activity_cache.get(i) is None
                or self._activity_cache[i] < since_ms)
        }

    def apply_time_filter(self) -> None:
        """时间筛选变化时：按 since 隐藏左栏项目，必要时改选可见项，再 reanalyze 一次。

        until 不参与左栏。任务类型变化不走此路（直接 reanalyze）。
        """
        hidden = self._compute_hidden()
        self.project_col.set_hidden(hidden)
        cur = self._selected_project_idx
        if cur is None or cur in hidden:
            new = next((i for i in range(len(self._projects))
                        if i not in hidden and i not in self._empty_projects), None)
            if new is not None:
                self._selected_project_idx = new
                self.project_col.select_idx(new, notify=False)
            else:
                # 无可见项目：必须取消在途分析并 bump generation，否则上一次
                # reanalyze 的 worker（generation 未变）回填时会命中 _on_analysis，
                # 把刚清空的视图重新填上、状态错乱。
                self._analysis_cancel.set()
                self._analysis_cancel = threading.Event()
                self._analysis_generation += 1
                self._selected_project_idx = None
                self._clear_analysis_view()
                self.filter.set_status("所选时间范围内无项目活动")
                return
        self.reanalyze()

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
        # 基准解析链与 analyze/audit 一致：逐项目 > 逐数据源 > 全局。
        pb = metrics.resolve_baselines(views.ref_uid(proj), source=proj.source)
        args = dict(
            project=proj.key,
            source=proj.source,
            project_ref=proj,
            task_type=params["task_type"],
            baseline_tcer=pb["tcer"],
            baseline_cpe=pb["cpe"],
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
                elif kind == "cross_source":
                    _, models, errors = item
                    note = f"（{errors} 个项目分析失败）" if errors else ""
                    self.filter.set_status(
                        f"跨源对照完成 · {len(models)} 个模型{note}")
                    popups.CrossSourceModelsPopup(self.root, models)
                elif kind == "realproj":
                    _, rows, errors = item
                    self._realproj_scanning = False
                    self._realproj_loaded = True
                    note = f"（{errors} 张项目卡分析失败）" if errors else ""
                    self.filter.set_status(
                        f"项目聚合完成 · {len(rows)} 个真实项目{note}")
                    self.real_projects_view.set_rows(rows)
                elif kind == "baselines":
                    _, mode, min_loc, method, per_project, errors, callback = item
                    self._on_baselines_computed(mode, min_loc, method, per_project, errors, callback)
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
        except Exception:  # noqa: BLE001 — _poll 必须永远重新排程
            # 渲染期异常（如缺 PIL 时的图表错误）绝不能终止轮询——after 一旦
            # 不再排程，后续所有分析结果都会静默丢弃、状态栏永久卡「分析中…」。
            # 结果处理是一条条独立排队的，跳过当前条、继续处理后续条即可。
            try:
                self.filter.set_status("渲染出错（已跳过一条结果）")
            except Exception:
                pass
        self.root.after(120, self._poll)

    def _on_analysis(self, a: analyze.ProjectAnalysis) -> None:
        proj = self._selected_project()
        if proj is None:
            # generation 门已放行（最新一代），但选中项目已被清空（如时间筛选
            # 后无可见项目）——不能静默 return，否则 reanalyze 设的「分析中…」
            # 永不复位、右上角卡死，只能重开项目。复位到就绪态。
            self.filter.set_status("就绪")
            return
        if a.project_ref and views.ref_uid(a.project_ref) != views.ref_uid(proj):
            # 结果属于旧选中项目（切项目/切时间区间改选了可见项）。generation
            # 门放行了它但 ref 已错位——丢弃结果，同时立刻为「当前」项目重新分析，
            # 而不是把「分析中…」留在屏上永不落地。
            self.reanalyze()
            return
        self._current = a
        prev_sid = self._selected_session_id
        self._selected_session_id = None
        _pinned, _flagged = self._marks_for_current_project()
        self.session_col.update(a.reports, pinned=_pinned, flagged=_flagged)
        # Preserve the prior selection across the refresh when it survived
        # (e.g. a reanalyze triggered indirectly by a date-filter FocusOut
        # firing as a popup closes); otherwise default to the most recent.
        # select_* set the visual selection only (notify=False) — the unified
        # render below handles metrics + trend exactly once.
        if prev_sid and self.session_col.select_by_sid(prev_sid, notify=False):
            self._selected_session_id = prev_sid
        elif a.reports:
            self._selected_session_id = self.session_col.select_first(notify=False)
        # 按页签惰性渲染：切项目只画当前看得见的页签（四视图全画要 ~600ms），
        # 其余标记待渲染，用户切过去时经 <<NotebookTabChanged>> 补上。
        self._refresh_views(full=True)
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
        """选中某会话（中栏卡片 / 效率榜排名行触发）。

        不翻转视角——视角切换只由左上角分段控件负责。会话视角下刷新会话相关面板；
        项目视角下仅记录选中。
        """
        self._selected_session_id = sid
        if self.view_mode.get() == "session":
            self._refresh_views(full=False)
        self._update_tab_names()

    # --------------------------------------------------------------- session marks
    def _session_mark_key(self, sid):
        proj = self._selected_project()
        if proj is None:
            return None
        return f"{views.ref_uid(proj)}::{sid}"

    def _marks_for_current_project(self):
        """当前项目下被置顶 / 标红的 sid 集合（剥掉复合 key 的项目前缀）。"""
        proj = self._selected_project()
        if proj is None:
            return set(), set()
        prefix = views.ref_uid(proj) + "::"
        pinned = {k[len(prefix):] for k in self._pinned_keys if k.startswith(prefix)}
        flagged = {k[len(prefix):] for k in self._flagged_keys if k.startswith(prefix)}
        return pinned, flagged

    def _save_marks(self) -> None:
        ui_prefs.save(self._ui_prefs)

    def _toggle_session_mark(self, sid, keys: list, *, reset=False) -> None:
        key = self._session_mark_key(sid)
        if key is None:
            return
        if key in keys:
            keys.remove(key)
        else:
            keys.append(key)
        self._save_marks()
        pinned, flagged = self._marks_for_current_project()
        # 恢复原先选中的会话（不抢选中）；置顶 reset 滚到顶看效果。
        self.session_col._apply_marks(pinned, flagged,
                                      keep_sid=self._selected_session_id, reset=reset)

    def toggle_session_pin(self, sid) -> None:
        self._toggle_session_mark(sid, self._pinned_keys, reset=True)

    def toggle_session_flag(self, sid) -> None:
        self._toggle_session_mark(sid, self._flagged_keys, reset=False)

    def _remove_session_marks(self, sid) -> None:
        """删除会话后清理其标记 key，防残留。"""
        key = self._session_mark_key(sid)
        if key is None:
            return
        if key in self._pinned_keys:
            self._pinned_keys.remove(key)
        if key in self._flagged_keys:
            self._flagged_keys.remove(key)
        self._save_marks()

    def delete_session(self, report) -> None:
        """彻底删除一个会话（主文件 + subagent/tool-results 目录），随后刷新视图。

        删除按磁盘路径定位，保证不残留 subagent 数据。删除后若项目仍有会话则
        重算，否则刷新项目列表（该项目转为「无会话」）。
        """
        from tkinter import messagebox
        from tcer.core import reader

        sid = report.meta.session_id or report.meta.path.stem
        if report.meta.source in ("codex", "opencode", "grok", "omp", "pi"):
            label = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok", "omp": "Oh My Pi", "pi": "Pi"}.get(report.meta.source, report.meta.source)
            messagebox.showinfo("删除会话", f"{label} 会话当前仅支持只读分析，暂不删除本地会话数据。")
            return
        try:
            removed = reader.delete_session(report.meta.path)
        except OSError as e:
            messagebox.showerror("删除失败", f"无法删除会话 {sid[:16]}…\n{e}")
            return
        self._remove_session_marks(sid)

        if self._selected_session_id == sid:
            self._selected_session_id = None
        self.session_col.clear_selection()

        proj = self._selected_project()
        _has = False
        if proj is not None and proj.source == "claude":
            _r = ref_root(proj)
            _has = bool(discover_jsonl(proj.key, roots=[_r] if _r is not None else None))
        if _has:
            self.reanalyze()
        else:
            # 最后一个会话被删 — 项目变空，回到项目列表状态。
            self._current = None
            self.refresh_projects()
        self.filter.set_status(f"已删除会话 · 移除 {len(removed)} 项磁盘对象")

    def _on_view_change(self) -> None:
        self._refresh_views(full=False)
        self._update_tab_names()

    # --------------------------------------------------------------- 页签惰性渲染
    def _refresh_views(self, *, full: bool) -> None:
        """四个页签全部标脏，只立即渲染当前页签（其余切到时补）。

        ``full=True`` = 新分析结果（页签要重灌数据）；``False`` = 视角/选中
        变化（数据不变，仅按当前选中重画）。仍挂起的 full 脏标记不被 False
        刷新降级——未访问过的页签首次补渲染时依旧要灌数据。
        """
        for i in range(4):
            if full or i not in self._dirty_tabs:
                self._dirty_tabs[i] = full
        self._render_tab(self._nb.index(self._nb.select()))

    def _render_tab(self, idx: int) -> None:
        """渲染指定页签（未标脏则跳过）。补渲染时按脏标记决定是否重灌数据。"""
        if idx == 5:
            # LLM 报告页签不依赖 self._current（读持久化文件），每次切入重载。
            self.llm_reports_view.on_show()
            return
        if idx == 4:
            # 项目聚合页签不依赖 self._current（自扫全部项目），首次切入加载。
            self.real_projects_view.on_show()
            return
        if not self._current or idx not in self._dirty_tabs:
            return
        full = self._dirty_tabs.pop(idx)
        if idx == 0:
            self._render_metrics()
        elif idx == 1:
            self._update_model_compare()
        elif idx == 2:
            if full:
                self.ranking_view.update(self._current.reports,
                                         aggregate=self._current.aggregate)
            self._update_ranking_view()
        else:
            if full:
                self.trend_chart.update(self._current.reports)
            # Highlight selected session in trend chart (without rebuild — zoom)
            if self._selected_session_id:
                self.trend_chart.select_session_by_sid(self._selected_session_id)

    def _update_ranking_view(self) -> None:
        """按视角驱动综合效率分排名右栏（对齐指标分类/模型对比的项目/会话切换）。

        会话视角且有选中 → 展示该会话构成拆解 + 会话洞察；否则 → 项目视角排名概览
        + 项目洞察。排名表本身（左栏）在两种视角都在，作导航用。
        """
        if not self._current:
            return
        mode = self.view_mode.get()
        if mode == "session" and self._selected_session_id:
            report = self._session_report(self._selected_session_id)
            self.ranking_view.set_view_mode("session", report)
        else:
            self.ranking_view.set_view_mode("project")

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

    def _update_tab_names(self) -> None:
        """页签名加 (项目)/(会话) 后缀 + 彩色视角图标。

        ttk 页签无法 per-tab 染文字色，故用 per-tab image 区分：指标分类/模型对比
        显示彩色视角图标（会话=蓝双气泡、项目=橙文件），排名/趋势保留原功能图标。
        """
        mode = self.view_mode.get()
        is_session = mode == "session" and self._selected_session_id
        suffix = "(会话)" if is_session else "(项目)"
        view_icon = views.ui_icon(self._nb, "view-session" if is_session else "view-project")
        self._nb.tab(0, text=f"指标分类 {suffix}", image=view_icon, compound="left")
        self._nb.tab(1, text=f"模型对比 {suffix}", image=view_icon, compound="left")
        self._nb.tab(2, text=f"效率榜 {suffix}", image=view_icon, compound="left")
        self._nb.tab(3, text="趋势")

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
    def _session_label(report) -> str:
        """会话来源标识：``日期 · 标题 · sessionid``。

        日期取会话开始日 (started_at，本地时区 YYYY-MM-DD)，无时间戳则省略；
        标题取会话标题（无标题回退「无标题」）截断到 24 字符；sessionid 取
        主 id 段截断到 12 字符——标题与 id 都限长，避免聚合视图来源条过长换行。
        """
        from tcer.core import format as fmt_mod
        meta = report.meta
        title = (meta.title or "").strip() or "无标题"
        if len(title) > 24:
            title = title[:24] + "…"
        parts: list[str] = []
        day = fmt_mod.fmt_dt(report.usage.started_at, fmt_mod.FMT_DATE)
        if day != "-":
            parts.append(day)
        parts.append(title)
        sid = (meta.session_id or "").strip()
        if sid and sid != "(aggregate)":
            parts.append(sid[:12] + ("…" if len(sid) > 12 else ""))
        return " · ".join(parts)

    @staticmethod
    def _load_user_messages(report, reports):
        """读取一个 report 的全部用户消息（仅文件 IO，线程安全）。

        单会话视图返回 ``list[str]``；聚合视图返回 ``list[(会话标识, [消息])]``，
        每组带来源标识（标题 + sessionid，均限长），空消息的会话被跳过。
        """
        source = report.meta.source or "claude"
        is_agg = report.meta.session_id == "(aggregate)"

        def _read_one(r) -> list[str]:
            if source == "codex":
                from tcer.core import codex_reader
                return codex_reader.read_user_messages(r.meta.path)
            if source == "opencode":
                from tcer.core import opencode_reader
                sid = r.meta.session_id
                return opencode_reader.read_user_messages(r.meta.path, sid) if sid else []
            if source == "grok":
                from tcer.core import grok_reader
                return grok_reader.read_user_messages(r.meta.path)
            if source == "omp":
                from tcer.core import omp_reader
                return omp_reader.read_user_messages(r.meta.path)
            if source == "pi":
                from tcer.core import pi_reader
                return pi_reader.read_user_messages(r.meta.path)
            # Claude
            return TcerGui._claude_user_messages(r)

        label = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok",
                 "omp": "Oh My Pi", "pi": "Pi"}.get(source, "Claude")

        if is_agg:
            groups: list[tuple[str, list[str]]] = []
            for r in reports:
                msgs = _read_one(r)
                if msgs:
                    groups.append((TcerGui._session_label(r), msgs))
            return groups, label

        # Single-session view: Claude legacy cache wins when present.
        if source not in ("codex", "opencode", "grok", "omp", "pi") \
                and report.usage.user_message_texts:
            return list(report.usage.user_message_texts), label
        return _read_one(report), label

    @staticmethod
    def _claude_user_messages(report) -> list[str]:
        """Lazy-load Claude user texts for one session (main jsonl only).

        Subagent files are skipped on purpose: their only user-role messages
        are the Task tool's dispatch prompt (e.g. "You are researching…"),
        never real human input — including them leaked subagent prompts into
        the popup as fake "user messages".
        """
        from tcer.core import reader
        path = report.meta.path
        if path is None:
            return []
        try:
            _, main, _ = reader.session_artifacts(path)
        except (OSError, ValueError, IndexError):
            # 路径形态异常（非标准会话布局）→ 退回按单文件读取。
            return reader.read_user_messages(path) if path.is_file() else []
        return reader.read_user_messages(main) if main.is_file() else []

    def show_files_touched(self) -> None:
        report = self._rendered_report
        if report and report.files_touched_details:
            popups.FilesTouchedPopup(self.root, report.files_touched_details,
                                     report.searched_paths_details)
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
        """打开个人基准校准弹窗。先开窗，由弹窗按需触发后台校准（不预先计算）。

        弹窗提供：全部会话 / 逐项目 两种模式、离群过滤开关（默认开）。基准始终
        基于**全部历史会话**，不受顶栏时间范影响。
        """
        if not self._projects:
            messagebox.showinfo("计算基准", "无项目可用于计算。")
            return
        proj = self._selected_project()
        popups.BaselinesPopup(
            self.root,
            on_compute=self._baseline_compute,
            on_apply=self._baseline_apply,
            current_project_label=views.project_label(proj) if proj else None,
        )

    def _baseline_compute(self, mode: str, filter_outliers: bool, method: str, callback) -> None:
        """后台按模式汇总会话并算基准，完成后在主线程调 callback(result)。

        基准与时间范围无关——**不传 since/until**，始终全量历史会话。method 为
        中位数/平均数。task_type 用当前筛选值（只影响 TCER 归一，不影响 eligible）。
        """
        params = self.filter.get_params()
        projects = list(self._projects)
        min_loc = None if filter_outliers else 0
        self.filter.set_status(f"个人基准校准中…（{mode} · {len(projects)} 个项目）")

        def _work() -> None:
            per_project = []  # (uid, label, reports)
            errors = 0
            for p in projects:
                try:
                    a = analyze.analyze_project(
                        p.key, source=getattr(p, "source", "claude"),
                        project_ref=p if hasattr(p, "session_paths") else None,
                        task_type=params["task_type"],
                        no_loc=self._no_loc,   # 注意：不传 since/until —— 基准不受时间范围影响
                    )
                except Exception:  # noqa: BLE001 — 单项目失败不拦汇总
                    errors += 1
                    continue
                per_project.append((views.ref_uid(p), views.project_label(p), a.reports))
            self._q.put(("baselines", mode, min_loc, method, per_project, errors, callback))

        threading.Thread(target=_work, daemon=True).start()

    def _on_baselines_computed(self, mode, min_loc, method, per_project, errors, callback) -> None:
        """主线程：后台汇总的会话按模式算成基准结结果，回调给弹窗渲染。"""
        note = f"{errors} 个项目分析失败已跳过" if errors else ""
        need = metrics.MIN_BASELINE_SESSIONS
        if mode == "all":
            all_reports = [r for _, _, reps in per_project for r in reps]
            eligible = metrics.baseline_eligible_reports(all_reports, min_net_loc=min_loc)
            values = metrics.compute_baselines(all_reports, min_net_loc=min_loc, method=method)
            msg = ""
            if values is None:
                msg = (f"有效会话不足：需至少 {need} 个完整会话，当前跨项目共 "
                       f"{len(eligible)} 个。样本过少时波动大，暂不建议写入。")
            self.filter.set_status("个人基准校准完成" if values else "个人基准：样本不足")
            callback({"values": values, "n": len(eligible), "note": note, "msg": msg})
        elif mode == "per_source":   # 按数据源 —— 跨源公平比较用的逐源基准
            all_reports = [r for _, _, reps in per_project for r in reps]
            by_src = metrics.compute_baselines_per_source(
                all_reports, min_net_loc=min_loc, method=method)
            out = []
            for src in sorted(by_src):
                n = len(metrics.baseline_eligible_reports(
                    [r for r in all_reports if r.meta.source == src],
                    min_net_loc=min_loc))
                vals = by_src[src]
                item = {"source": src, "label": views.source_label(src), "n": n,
                        "values": vals}
                if vals is None:
                    item["reason"] = f"有效会话 {n} 个 < 所需 {need}，跳过"
                out.append(item)
            n_ok = sum(1 for it in out if it["values"])
            self.filter.set_status(
                f"按源校准完成 · {n_ok}/{len(out)} 个源可校准" if n_ok else "按源校准：无源达标")
            callback({"per_source": out, "note": note,
                      "msg": "" if out else "没有可用于计算的会话。"})
        else:    # per_proj —— 列出**全部项目**，样本不足者也显示（values=None）
            out = []
            for uid, label, reps in per_project:
                n = len(metrics.baseline_eligible_reports(reps, min_net_loc=min_loc))
                vals = metrics.compute_baselines(reps, min_net_loc=min_loc, method=method)
                item = {"uid": uid, "label": label, "n": n, "values": vals}
                if vals is None:
                    item["reason"] = f"有效会话 {n} 个 < 所需 {need}，跳过"
                out.append(item)
            # 有会话的项目排前、可校准的更靠前，便于查看
            out.sort(key=lambda it: (it["values"] is None, -it["n"]))
            n_ok = sum(1 for it in out if it["values"])
            self.filter.set_status(
                f"逐项目校准完成 · {n_ok}/{len(out)} 可校准" if n_ok else "逐项目校准：无项目达标")
            callback({"per_project": out, "note": note,
                      "msg": "" if out else "没有可用于计算的项目。"})

    def _baseline_apply(self, mode: str, payload) -> None:
        """应用基准：全局 / 逐项目 / 逐数据源写入 config，然后重算当前项目。"""
        if mode == "all":
            metrics.save_baselines(payload)  # 写全局
        elif mode == "per_source":
            for src, vals in payload.items():
                metrics.save_baselines(vals, source=src)  # 逐源写入
        else:
            for uid, vals in payload.items():
                metrics.save_baselines(vals, project_uid=uid)  # 逐项目写入
        self.reanalyze()

    def show_tool_sequence(self) -> None:
        report = self._rendered_report
        if not report or len(report.usage.tool_ops) < 2:
            self.filter.set_status("工具调用不足，无法分析序列")
            return
        suffix = (" · 项目汇总" if report.meta.session_id == "(aggregate)"
                  else f" · {(report.meta.session_id or '')[:16]}…")
        popups.ToolSequencePopup(self.root, report.usage, suffix)

    def show_cross_source_compare(self) -> None:
        """同模型跨源对照：后台分析全部项目，同一模型在不同 agent 里的中位数对比。"""
        if not self._projects:
            self.filter.set_status("无项目可汇总")
            return
        params = self.filter.get_params()
        projects = list(self._projects)
        self.filter.set_status(f"跨源对照计算中…（{len(projects)} 个项目）")

        def _work() -> None:
            reports = []
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
                except Exception:  # noqa: BLE001 — 单项目失败不拦对照
                    errors += 1
                    continue
                reports.extend(a.reports)
            models = analyze.cross_source_models(reports)
            self._q.put(("cross_source", models, errors))

        threading.Thread(target=_work, daemon=True).start()

    def real_projects_scan(self, *, force: bool = False) -> None:
        """项目聚合页签数据：后台扫全部项目 → 按真实工作目录分组 + 逐 ref 聚合。

        聚合口径：Token/成本/净增行/缓存token 求和；TCER/CPE/缓存命中按合计重算
        （非各卡均值——避免 Simpson 悖论）；效率分从聚合轴输入重算（与 server
        ``_agg_metrics`` 同一处理：聚合 TCER 作产出轴代理、质量轴用求和比率）。
        首次切入自动扫（force=False 且未加载过）；工具栏「刷新」强制重扫。
        """
        if self._realproj_scanning or (self._realproj_loaded and not force):
            return
        if not self._projects:
            self.real_projects_view.set_rows([])
            return
        self._realproj_scanning = True
        params = self.filter.get_params()
        projects = list(self._projects)
        self.filter.set_status(f"项目聚合计算中…（{len(projects)} 张项目卡）")

        def _work() -> None:
            import statistics
            from collections import Counter

            from tcer.core.paths import project_has_sessions

            refs = [p for p in projects if project_has_sessions(p)]
            groups = analyze.real_projects(refs)
            rows = []
            errors = 0
            for g in groups:
                ref_rows = []
                tot = {"n": 0, "tokens": 0, "cost": 0.0, "net": 0,
                       "requests": 0, "user_msgs": 0}
                sums = {"cr": 0, "inp": 0, "cw": 0, "added": 0, "reworked": 0,
                        "tools": 0, "tool_errors": 0}
                rbws = []
                for ref in g.refs:
                    try:
                        a = analyze.analyze_project(
                            ref.key, source=ref.source, project_ref=ref,
                            task_type=params["task_type"],
                            since=params["since"], until=params["until"],
                            no_loc=self._no_loc,
                        )
                    except Exception:  # noqa: BLE001 — 单卡失败不拦聚合
                        errors += 1
                        continue
                    agg = a.aggregate
                    u = agg.usage
                    tot["n"] += a.n_sessions
                    tot["tokens"] += u.total
                    tot["cost"] += agg.cost or 0.0
                    tot["net"] += agg.net_loc or 0
                    # 请求数口径同指标 SSOT（Grok 按 API 调用数，其余按助手响应数）。
                    tot["requests"] += (u.api_calls or u.assistant_msgs)
                    tot["user_msgs"] += u.user_msgs
                    sums["cr"] += u.cache_read_input_tokens
                    sums["inp"] += u.input_tokens
                    sums["cw"] += u.cache_creation_input_tokens
                    sums["added"] += agg.code_added or 0
                    sums["reworked"] += (agg.code_reworked
                                         if agg.code_reworked is not None
                                         else agg.code_deleted) or 0
                    sums["tools"] += sum(u.tool_calls.values())
                    sums["tool_errors"] += u.tool_errors
                    if agg.read_before_write is not None:
                        rbws.append(agg.read_before_write)
                    ref_rows.append({
                        "source": ref.source,
                        "icon": views.project_icon_key(ref),
                        "sub": (ref.config_root.name if ref.source == "claude"
                                and ref.config_root is not None else ref.key),
                        "n": a.n_sessions,
                        "requests": (u.api_calls or u.assistant_msgs),
                        "user_msgs": u.user_msgs,
                        "tokens": u.total,
                        "cost": agg.cost,
                        "net": agg.net_loc,
                        "tcer": agg.tcer,
                        "cpe": agg.cpe,
                        "chr": agg.chr,
                        "score": agg.score,
                        "tier": agg.tier,
                    })
                if not ref_rows:
                    continue
                # 同组同源多卡（Claude 多配置根）在标签后标注所属根，便于区分。
                src_count = Counter(r["source"] for r in ref_rows)
                for r in ref_rows:
                    label = views.source_label(r["source"])
                    if src_count[r["source"]] > 1:
                        label += f"（{r['sub']}）"
                    r["label"] = label
                # 聚合口径：比率一律「分子和 ÷ 分母和」重算，不取各卡平均。
                denom_in = sums["inp"] + sums["cw"] + sums["cr"]
                tot["chr"] = (sums["cr"] / denom_in) if denom_in else None
                tot["tcer"] = (tot["net"] / (tot["tokens"] / 1e6)) if tot["tokens"] else None
                tot["cpe"] = (tot["cost"] / tot["net"] * 1000) if tot["net"] > 0 else None
                churn = (sums["reworked"] / sums["added"]) if sums["added"] else None
                err = (sums["tool_errors"] / sums["tools"]) if sums["tools"] else None
                rbw = statistics.median(rbws) if rbws else None
                score = metrics.efficiency_score(
                    tot["tcer"], tot["cpe"], churn, err, rbw, net_loc=tot["net"])
                tot["score"] = score
                tot["tier"] = metrics.tier(score)
                rows.append({"key": g.key, "display": g.display,
                             "refs": ref_rows, "totals": tot})
            self._q.put(("realproj", rows, errors))

        threading.Thread(target=_work, daemon=True).start()

    def show_project_profile(self) -> None:
        """项目画像：跨会话热点文件 / 模型混用策略 / 技能·MCP 复用。"""
        if not self._current:
            self.filter.set_status("请先分析一个项目")
            return
        popups.ProjectProfilePopup(self.root, self._current)

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
        # LLM 解读的数据懒加载器（弹窗 worker 线程内调用，主线程零 IO）：
        # dialogue=完整对话时间线（Claude 源专属——其余源无 assistant 文本
        # 读取，弹窗内回退到用户消息采样）；user_texts=各源 read_user_messages。
        load_dialogue = None
        if (report.meta.source or "claude") == "claude":
            from tcer.core import reader
            load_dialogue = lambda: reader.read_dialogue(report.meta.path)
        popups.SessionTimelinePopup(
            self.root, report,
            load_user_texts=lambda: TcerGui._load_user_messages(report, [])[0],
            load_dialogue=load_dialogue,
            on_report_saved=self._on_llm_report_saved)

    def _on_llm_report_saved(self, report_id: str) -> None:
        """解读生成落盘后：切到「LLM 报告」页签并选中该条（大区阅读）。"""
        self._nb.select(self._llm_tab)
        self.llm_reports_view.select_report(report_id)

    def show_llm_config(self) -> None:
        """LLM 设置弹窗（本地表单零联网；连接测试为用户显式点击）。"""
        from tcer.core import llm_prefs
        popups.LlmConfigPopup(self.root, config=llm_prefs.stored_config(),
                              on_save=self._save_llm_config)

    def _save_llm_config(self, *, base_url: str, api_key: str, model: str,
                         scope: str, scopes: list[str] | None = None,
                         **_extra) -> None:
        from tcer.core import llm_prefs
        cfg = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "scope": scope,
        }
        if scopes is not None:
            cfg["scopes"] = scopes
        llm_prefs.save(cfg)

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
        # 项目级 HTML：先选章节（默认全选 = 旧行为），再走保存对话框。
        if fmt == "html" and scope != "session":
            popups.HtmlSectionsPopup(self.root, self._export_project_html)
            return
        self._do_export(fmt, scope)

    def _export_project_html(self, sections: list[str]) -> None:
        """选节回调：无章节时取消导出，否则按选中章节导出。"""
        if not sections:
            self.filter.set_status("未选择任何章节，已取消导出")
            return
        self._do_export("html", "project", html_sections=sections)

    def _do_export(self, fmt: str, scope: str, html_sections=None) -> None:
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
                    n_subagents=a.n_subagents, sections=html_sections)
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
        from tcer.core import upload_config
        projects = [(views.ref_uid(p), f"[{views.project_source_label(p)}] {views.project_label(p)}")
                    for p in self._projects]
        default_proj = None
        proj = self._selected_project()
        if proj is not None:
            default_proj = views.ref_uid(proj)
        popups.UploadDialog(
            self.root,
            prefs=self._upload_prefs,
            projects=projects,
            default_project=default_proj,
            on_upload=self._start_upload,
            on_save_prefs=self._save_upload_prefs,
            on_save_config=self._save_upload_config,
            # 当前上传配置（存储原始值：url/token 空串即"未配置"，占位提示内置默认/匿名）。
            # 本地单用户工具，token 明文就存在同机 json，回填明文无额外泄露、也便于核对。
            config=upload_config.stored_config(),
        )

    def _save_upload_config(self, *, url: str, auth_token: str, detail: bool) -> None:
        """把 dialog 编辑的上传配置写回 ``tcer_ui.json`` 的 upload 段。

        写回后同步 ``self._ui_prefs``（退出时整体落盘的内存副本），避免 ``_on_close``
        用旧副本覆盖掉刚保存的 upload 段。
        """
        from tcer.core import upload_config
        upload_config.save(url=url, auth_token=auth_token, detail=detail)
        self._ui_prefs = ui_prefs.load()

    def _save_upload_prefs(self, prefs: dict) -> None:
        """Persist only the remembered project selection (rest lives in
        ``tcer_ui.json`` 的 upload 段, see ``upload_config``)."""
        self._upload_prefs = prefs
        try:
            upload_prefs.save(prefs)
        except OSError:
            pass  # non-fatal — prefs just won't persist across restarts

    def _project_ref_by_uid(self, uid: str):
        return views.find_ref_by_uid(self._projects, uid)

    def _start_upload(self, prefs: dict, dialog=None) -> None:
        """Analyze each selected project fresh, then upload its own report.

        Server URL / auth (Auth Token) / detail come from ``upload_config``
        (``tcer_ui.json`` 的 upload 段, saved by the dialog before this fires) —
        ``prefs`` only supplies the project selection. ``server_url()`` returns
        None when unconfigured (开源库无内置默认地址); guard and prompt the user.
        Each selected key is re-analyzed on a worker thread so each upload carries
        that project's real aggregate (+ sessions when detail is on). Returns
        immediately; the combined result arrives via the queue.
        """
        from tcer.core import upload_config
        server_url = upload_config.server_url()
        if not server_url:
            if dialog is not None:
                dialog.set_status("请先填写上传服务器地址", error=True)
            return
        keys = list(prefs.get("last_projects") or [])
        if not keys:
            if dialog is not None:
                dialog.set_status("请至少选择一个项目", error=True)
            return
        refs = [(k, self._project_ref_by_uid(k)) for k in keys]
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
        cfg = dict(server_url=server_url, auth_token=upload_config.auth_token(),
                   detail=upload_config.upload_detail())
        threading.Thread(
            target=self._upload_worker,
            args=(cfg, refs, missing, analysis_args, dialog),
            daemon=True,
        ).start()

    def _upload_worker(self, cfg, refs, missing, analysis_args, dialog) -> None:
        """Off-thread: analyze + upload each selected project, aggregate results.

        Auth comes from ``upload_config`` (``tcer_ui.json``): an auth token
        authenticates the upload as its owning user (server fills ``person``);
        no token → anonymous upload. Per-project
        failures are collected without aborting the rest. Each project is analyzed
        fresh so its payload carries that project's own aggregate (and sessions
        when detail is on).
        """
        server_url = cfg["server_url"]
        auth_token = cfg.get("auth_token")
        detail = bool(cfg.get("detail"))

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
                total_inserted += upload_client.token_upload(
                    server_url=server_url, auth_token=auth_token,
                    aggregate=a.aggregate, reports=a.reports,
                    n_sessions=a.n_sessions, project=key, detail=detail,
                )
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

    # --------------------------------------------------------------- entry
    @classmethod
    def run(cls) -> int:
        try:
            import tkinter as tk
        except ImportError:
            print("error: tkinter is not available in this Python build.")
            return 1
        _enable_windows_hidpi()
        _set_windows_app_id()
        root = tk.Tk()
        _apply_tk_scaling(root)
        _set_window_icon(root)
        _apply_dark_titlebar(root)
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


def _set_windows_app_id() -> None:
    """设置 AppUserModelID，让 Windows 任务栏显示本程序图标。

    Tk 程序默认无 AppID，任务栏按 ``python.exe`` 分组、显示默认 Python 图标——
    即使 ``iconbitmap`` 已设好窗口图标。显式设 AppID 后任务栏才认本程序自己的
    图标（与标题栏一致）。必须在创建 Tk 之前调用。
    """
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("leo.TCER.app")
    except (OSError, AttributeError):
        pass


def _apply_tk_scaling(root) -> None:
    """按实际 DPI 设置 Tk 缩放，点单位字体随之放大到物理正确尺寸。

    mac（Aqua）例外：Tk 自己按点管理字体，显式改 scaling 反而会虚胖。
    """
    import sys
    if sys.platform == "darwin":
        return
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass


def clamp_geometry(geometry: str, sw: int, sh: int) -> str | None:
    """把恢复的窗口几何钳制进当前屏幕——跨机器/跨分辨率迁移的关键防线。

    在 1920×1080 存下的 ``1600x900+169+40``，搬到 1366×768 笔记本或 mac
    1440×900 上会超出屏幕；多显示器拔掉后 ``+3000+200`` 会落在不存在的屏
    幕外（窗口整个不可见，看起来像「程序没开」）。尺寸收进屏幕、位置保证
    标题栏留在可视区。不合法输入返回 None（走默认居中）。
    """
    import re
    m = re.fullmatch(r"(\d{3,5})x(\d{3,5})[+-](-?\d+)[+-](-?\d+)", geometry or "")
    if not m:
        return None
    w, h, x, y = map(int, m.groups())
    w = max(640, min(w, sw))
    h = max(480, min(h, sh - 60))          # 留任务栏余量
    x = min(max(0, x), max(0, sw - 120))   # 至少露出 120px 可拖回
    y = min(max(0, y), max(0, sh - 60))
    return f"{w}x{h}+{x}+{y}"


def _set_window_icon(root) -> None:
    """设置窗口图标（标题栏 + 任务栏）——多尺寸 iconphoto。

    传 16/32/48/64/128/256 多张图，Windows 各场景选最匹配尺寸：标题栏选 16、
    任务栏选 48/64、Alt-Tab 选大图。这比单张大图大幅缩放清晰（标题栏不再糊），
    也避开 ``iconbitmap`` 底层 ``LoadImage`` 仅取单尺寸再二次缩放的问题。Tk 8.6
    的 ``iconphoto`` 支持多图。PhotoImage 列表存 ``root._tcer_icons`` 持引用防 GC。
    """
    import os
    assets = os.path.join(os.path.dirname(__file__), "assets")
    imgs = []
    for s in (32, 48, 64, 128, 256):
        p = os.path.join(assets, f"tcer_logo_{s}.png")
        if os.path.isfile(p):
            try:
                imgs.append(tk.PhotoImage(file=p))
            except tk.TclError:
                pass
    if imgs:
        root._tcer_icons = imgs
        try:
            root.iconphoto(True, *imgs)
            return
        except tk.TclError:
            pass
    png = os.path.join(assets, "tcer_logo.png")
    if os.path.isfile(png):
        try:
            root._tcer_icon = tk.PhotoImage(file=png)
            root.iconphoto(True, root._tcer_icon)
        except tk.TclError:
            pass


def _apply_dark_titlebar(root) -> None:
    """Windows: 标题栏跟随系统暗/亮主题。实现见 ``platform.apply_dark_titlebar``
    （主窗口与所有子窗口共用；子窗口经 widgets.new_window 自动应用）。"""
    from .platform import apply_dark_titlebar
    apply_dark_titlebar(root)
