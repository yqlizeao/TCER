"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``ScoreRankingView`` consumes the shared
``export.score_ranking`` / ``export.score_decompose`` helpers.
"""
from __future__ import annotations

import os
import re
import tkinter as tk
from pathlib import Path
from dataclasses import dataclass
from tkinter import ttk

from tcer.core import metrics
from tcer.core.export import score_decompose, score_decompose_avg
from tcer.core.insights import (session_insights, project_insights,
                                 activity_overview, claude_md_suggestions,
                                 feature_suggestions, horizon_suggestions)
from tcer.core import format as fmt
from tcer.core.format import FMT_SHORT_MINUTE, fmt_dt
from . import theme
from .metric_defs import (
    GROUPS, MODEL_GROUPS, UNSUPPORTED_LABEL,
    SCORE_AXES, SCORE_AXIS_NEUTRAL, format_axis,
    report_values, format_value, metric_name, metric_tip,
    model_display, model_raw, model_tip,
)

# 排名页对用户展示的「综合效率分」名称与简称（取自指标 SSOT）。
_SCORE_NAME = metric_name("score")        # 综合效率分
_SCORE_SHORT = "效率分"                    # 窄列/徽标用简称
_SCORE_TIP = metric_tip("score")          # 悬停完整解释
from .widgets import (CalendarPopup, Card, CollapsibleSection, FlatMenu,
                      MetricCell, ScrollFrame, SelectableLabel, Tooltip, flat_button)
from .platform import CLICK_CURSOR

_PER_ROW = 6  # metric tiles per grid row inside a group


def _short_name(project_hash: str) -> str:
    """Friendlier label for a project-hash folder: strip a leading drive token.

    Hash folders encode a full cwd with separators replaced by '-', so there is
    no reliable project-name delimiter.  Windows: drop a leading ``c--`` style
    drive token.  Unix: strip the leading ``-`` produced by the root ``/``.
    """
    for i in range(1, len(project_hash) - 2):
        if project_hash[i:i + 2] == "--":
            return project_hash[i + 2:]
    # Unix: "/" → "-", strip only the single leading dash for root
    if project_hash.startswith("-"):
        return project_hash[1:]
    return project_hash


def project_label(project) -> str:
    """Display label for a source-aware project ref or legacy Path."""
    source = getattr(project, "source", "claude")
    if source in ("codex", "opencode", "grok", "omp", "pi"):
        default = {"codex": "Codex", "opencode": "OpenCode", "grok": "Grok", "omp": "Oh My Pi", "pi": "Pi"}.get(source, source)
        return getattr(project, "display_name", None) or getattr(project, "key", default)
    name = getattr(project, "name", None) or getattr(project, "key", str(project))
    return _short_name(name)


def project_drive(project) -> str | None:
    """项目所在盘符（大写单字母）——跨盘同名项目靠它区分。

    Claude：key 是 cwd 编码（``c--GitHub-TCER``），首段即盘符；其余源从
    ``cwd`` 的 ``Path.drive`` 取。Unix / 无盘符路径返回 None（不显示）。
    """
    cwd = getattr(project, "cwd", None)
    if cwd:
        drive = Path(cwd).drive  # "C:" / ""
        return drive[0].upper() if drive else None
    key = getattr(project, "key", "") or ""
    if len(key) > 3 and key[1:3] == "--" and key[0].isalpha():
        return key[0].upper()
    return None


_SOURCE_DISPLAY = {
    "codex": "Codex", "opencode": "OpenCode", "grok": "Grok",
    "omp": "Oh My Pi", "pi": "Pi",
}


def source_label(source: str | None) -> str:
    """source key → 界面显示名（Claude 兜底；未知源显示原始 key，不假装是 Claude）。"""
    if not source or source == "claude":
        return "Claude"
    return _SOURCE_DISPLAY.get(source, source)


def project_source_label(project) -> str:
    return source_label(getattr(project, "source", "claude"))


_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
# PhotoImage 必须被 Python 引用持有，否则 GC 后卡片图标变空白——模块级缓存。
_ICON_CACHE: dict[str, "tk.PhotoImage | None"] = {}
_MISSING = object()  # 区分「未查询」与「查过但无图标」，让负缓存生效


def source_icon(master, icon_key: str):
    """16px 图标（tk.PhotoImage，模块级缓存防 GC）。

    *icon_key* 对应 ``assets/<icon_key>.png``（claude / ccswitch / codex /
    opencode / grok / …）。无对应资源（或 Tk 尚未就绪）返回 None，调用方
    回退到 ``[源名]`` 文字标注。构建期已用 PIL 把原图预缩到 16×16，运行时零依赖。
    """
    cached = _ICON_CACHE.get(icon_key, _MISSING)
    if cached is not _MISSING:
        return cached
    path = os.path.join(_ASSETS_DIR, f"{icon_key}.png")
    img = None
    if os.path.isfile(path):
        try:
            img = tk.PhotoImage(master=master, file=path)
        except tk.TclError:
            img = None
    _ICON_CACHE[icon_key] = img
    return img


def ui_icon(master, name: str):
    """16px 通用 UI 图标（``assets/ui-<name>.png``），与 source_icon 同一套模块级缓存。

    工具栏动作、页签等通用图标经此加载；无资源返回 None，调用方回退文字。
    来源 Icons8 material-outlined 白色，构建期缩 16×16，运行时零依赖。
    """
    return source_icon(master, f"ui-{name}")


def project_icon_key(project) -> str:
    """项目卡片的图标 key（对应 ``assets/<key>.png``）。

    Claude 项目区分标准 ``~/.claude``（claude 图标）与自定义配置根如
    ``~/.claude-proxy``（ccswitch 图标）；其余来源各自同名图标，无资源时
    由调用方回退到 ``[源名]`` 文字。
    """
    source = getattr(project, "source", "claude")
    if source != "claude":
        return source  # codex / opencode / grok / omp
    from tcer.core.paths import is_custom_claude_root
    if is_custom_claude_root(getattr(project, "path", None)):
        return "ccswitch"
    return "claude"


def ref_uid(ref) -> str:
    """项目 ref 的稳定唯一标识（跨根同 key 也能区分）。

    Claude 项目含所属 config root 名：``claude:.claude:<hash>`` 与
    ``claude:.claude-proxy:<hash>`` 不同；其他源 ``{source}:{key}``。
    """
    source = getattr(ref, "source", "claude")
    key = getattr(ref, "key", "")
    if source != "claude":
        return f"{source}:{key}"
    from tcer.core.paths import ref_root
    root = ref_root(ref)
    return f"claude:{root.name if root is not None else ''}:{key}"


def find_ref_by_uid(refs, uid):
    """按 uid 精确找项目 ref；失败则裸 key 降级取首个（规范根排前，旧 prefs 恢复路径）。"""
    if uid is None:
        return None
    for r in refs:
        if ref_uid(r) == uid:
            return r
    # 旧 prefs 存的是裸 key（或老格式 source:key）——按 key 取首个
    for r in refs:
        if getattr(r, "key", None) == uid:
            return r
    for r in refs:
        key = getattr(r, "key", "")
        if key and (uid == f"{getattr(r, 'source', '')}:{key}" or uid.endswith(":" + key)):
            return r
    return None


def project_open_path(project) -> str:
    source = getattr(project, "source", "claude")
    if source == "codex":
        from tcer.core.paths import codex_sessions_dir
        return str(codex_sessions_dir())
    if source == "grok":
        from tcer.core.paths import grok_sessions_dir
        return str(grok_sessions_dir())
    if source == "omp":
        from tcer.core.paths import omp_sessions_dir
        return str(omp_sessions_dir())
    if source == "pi":
        from tcer.core.paths import pi_sessions_dir
        return str(pi_sessions_dir())
    path = getattr(project, "path", None)
    cwd = getattr(project, "cwd", None)
    return str(path or cwd or project)


def _file_manager_label() -> str:
    """Platform-appropriate file manager name for menu labels."""
    from .platform import FILE_MANAGER_NAME
    return FILE_MANAGER_NAME


class FilterBar:
    """Top control bar: segmented view switch + filters + actions, single row."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(side="top", fill="x", padx=8, pady=6)

        # -- View switcher: segmented control --
        self.view_mode = controller.view_mode
        seg_bg = tk.Frame(bar, bg=theme.CONTROL_BG, padx=2, pady=2)
        seg_bg.pack(side="left", padx=(0, 12))
        self._view_btns: dict[str, tk.Label] = {}
        self._view_pills: dict[str, tk.Frame] = {}
        self._view_icon_lbls: dict[str, tk.Label] = {}
        _seg_icons = {"project": ui_icon(seg_bg, "project"), "session": ui_icon(seg_bg, "session")}
        for label, val in [("项目视角", "project"), ("会话视角", "session")]:
            # 每个 pill 一个容器 Frame：图标/文字 Label 都装在里面，三者 bg
            # 同步切换 → 图标与文字之间不留接缝（两个独立 Label 会有缝隙）。
            pill = tk.Frame(seg_bg, bg=theme.CONTROL_BG)
            pill.pack(side="left", padx=1)
            self._view_pills[val] = pill
            click = lambda e, v=val: self._set_view(v)
            icon = _seg_icons.get(val)
            if icon is not None:
                il = tk.Label(pill, image=icon, bg=theme.CONTROL_BG, cursor=CLICK_CURSOR)
                il.pack(side="left", padx=(4, 0))
                il.bind("<Button-1>", click)
                self._view_icon_lbls[val] = il
            btn = tk.Label(pill, text=label, pady=1, cursor=CLICK_CURSOR,
                           font=theme.FONT_UI_SMALL)
            btn.pack(side="left", padx=(2, 6))
            btn.bind("<Button-1>", click)
            pill.bind("<Button-1>", click)  # 图标与文字之间的空隙也可点
            self._view_btns[val] = btn
        self._update_view_btns()

        # -- Filters --
        # 任务类型选择弱化：从上栏移入「工具」菜单的级联子菜单（task_var 仍由本栏
        # 持有，供 get_params / restore_prefs 使用；菜单 radiobutton 直接绑 task_var）。
        self._task_display_names = {
            metrics.AUTO_TASK_TYPE: "自动",
            **{k: (v.get("name") or k) for k, v in metrics.TASK_CATEGORIES.items()},
        }
        default_task = metrics.DEFAULT_TASK_TYPE
        default_label = self._task_display_names.get(
            default_task, next(iter(self._task_display_names.values()), "代码创作"))
        self.task_var = tk.StringVar(value=default_label)
        self._task_reverse_map = {v: k for k, v in self._task_display_names.items()}

        tk.Label(bar, text="来源:", bg=theme.BG, fg=theme.FG).pack(side="left")
        self.source_var = tk.StringVar(value="全部")
        self._source_display_names = {
            "all": "全部",
            "claude": "Claude",
            "codex": "Codex",
            "opencode": "OpenCode",
            "grok": "Grok",
            "omp": "Oh My Pi",
            "pi": "Pi",
        }
        self._source_reverse_map = {v: k for k, v in self._source_display_names.items()}
        source_cb = ttk.Combobox(bar, textvariable=self.source_var, width=8,
                                 values=list(self._source_display_names.values()), state="readonly")
        source_cb.pack(side="left", padx=(4, 12))
        source_cb.bind("<<ComboboxSelected>>", self._on_source_change)
        Tooltip(source_cb, "选择数据来源：全部 / Claude / Codex / OpenCode / Grok / Oh My Pi / Pi")

        tk.Label(bar, text="时间:", bg=theme.BG, fg=theme.FG).pack(side="left")
        from datetime import datetime as _dt
        # 默认起始日期=今天（启动即看当天会话）；until 留空，可由持久化恢复。
        self.since_var = tk.StringVar(value=_dt.now().strftime("%Y-%m-%d"))
        self._date_entry(bar, self.since_var, "开始日期").pack(side="left", padx=2)
        tk.Label(bar, text="至", bg=theme.BG, fg=theme.FG).pack(side="left", padx=2)
        self.until_var = tk.StringVar(value="")
        self._date_entry(bar, self.until_var, "结束日期").pack(side="left", padx=2)

        for label, preset in (("今天", "today"), ("本周", "week"), ("本月", "month"), ("全部", "all")):
            flat_button(bar, label, lambda p=preset: self._set_preset(p),
                        padx=theme.PAD_S).pack(side="left", padx=theme.PAD_XS)

        # 刷新全部项目列表：重新扫描磁盘发现新会话/新项目，常驻入口（原右键菜单项）
        refresh_btn = flat_button(bar, "刷新", self.controller.refresh_projects,
                                  padx=theme.PAD_S, image=ui_icon(bar, "refresh"),
                                  compound="left")
        refresh_btn.pack(side="left", padx=(theme.PAD_M, theme.PAD_XS))
        Tooltip(refresh_btn, "重新扫描磁盘，刷新全部项目列表")

        # -- Actions (right side) --
        # 上传按钮默认常驻（配置移入 tcer_ui.json 后不再以「是否配置 URL」显隐）。
        from tcer.core import upload_config
        factories = [
            lambda: self._make_tool_menu(bar),
            lambda: self._make_export_menu(bar),
        ]
        if upload_config.upload_enabled():
            factories.append(lambda: self._make_upload_button(bar))
        # LLM 按钮常驻（同上传；点击打开设置弹窗，未配置时本身零联网）。
        factories.append(lambda: self._make_llm_button(bar))
        for factory in factories:
            factory().pack(side="right", padx=2)

        self.status = tk.Label(bar, text="就绪", bg=theme.BG, fg="#9cdcfe", anchor="e")
        self.status.pack(side="right", padx=(8, 4))

    def _set_view(self, mode: str) -> None:
        self.view_mode.set(mode)
        self._update_view_btns()
        self.controller._on_view_change()

    def _update_view_btns(self) -> None:
        current = self.view_mode.get()
        # 选中态用视角标识色：会话=蓝、项目=橙黄（与指标/模型页签视角图标同色系）。
        _sel = {"session": theme.ACCENT, "project": theme.VIEW_PROJECT}
        for val, btn in self._view_btns.items():
            bg = _sel.get(val, theme.ACCENT) if val == current else theme.CONTROL_BG
            fg = theme.FG_WHITE if val == current else theme.MUTED
            btn.config(bg=bg, fg=fg)
            self._view_pills[val].config(bg=bg)
            il = self._view_icon_lbls.get(val)
            if il is not None:
                il.config(bg=bg)

    def _pop_below(self, btn, build):
        """点击 btn 时在其正下方弹出 FlatMenu（每次重建以反映最新状态）。"""
        def cb():
            menu = FlatMenu(btn)
            build(menu)
            btn.update_idletasks()
            menu.tk_popup(btn.winfo_rootx(),
                          btn.winfo_rooty() + btn.winfo_height())
        return cb

    def _make_tool_menu(self, parent):
        btn = flat_button(parent, "工具 ▾", None, padx=6,
                          image=ui_icon(parent, "tools"), compound="left")
        btn.config(command=self._pop_below(btn, self._build_tool_menu))
        Tooltip(btn, "项目总览 · 同模型跨源对照 · 会话时间线 · 会话对比 · 工具序列 · 个人基准 · 任务类型 · 高级选项 · 检查更新 / 版本信息")
        return btn

    def _build_tool_menu(self, menu) -> None:
        c = self.controller
        menu.add_command(label="项目总览", command=c.show_project_overview)
        menu.add_command(label="同模型跨源对照", command=c.show_cross_source_compare)
        menu.add_command(label="项目画像", command=c.show_project_profile)
        menu.add_command(label="工具序列", command=c.show_tool_sequence)
        menu.add_command(label="会话时间线", command=c.show_session_timeline)
        menu.add_command(label="会话对比", command=c.show_session_compare)
        menu.add_separator()
        menu.add_command(label="计算个人基准", command=c.compute_baselines)
        menu.add_command(label="任务类型", state="disabled")  # 分组标题（不可点）
        for dn in self._task_display_names.values():
            menu.add_radiobutton(label=dn, variable=self.task_var, value=dn,
                                 command=self._on_task_type_change)
        menu.add_separator()
        menu.add_command(label="高级选项", command=c.show_advanced)
        menu.add_separator()
        from tcer import __version__
        menu.add_command(label=f"TCER  v{__version__}", state="disabled")  # 版本信息(标题,不可点)
        menu.add_command(label="检查更新…", command=c.check_for_update)
        menu.add_command(
            label=("●  " if c.auto_check_enabled() else "○  ") + "启动时自动检查更新",
            command=c.toggle_auto_check,
        )

    def _make_export_menu(self, parent):
        btn = flat_button(parent, "导出 ▾", None, padx=6,
                          image=ui_icon(parent, "export"), compound="left")
        btn.config(command=self._pop_below(btn, self._build_export_menu))
        Tooltip(btn, "项目级 / 会话级报告导出：HTML（自包含可分享）· Markdown · JSON · CSV")
        return btn

    def _build_export_menu(self, menu) -> None:
        for label, fmt in (("项目报告 (HTML)", "html"), ("项目报告 (Markdown)", "md"),
                           ("项目数据 (JSON)", "json"), ("项目数据 (CSV)", "csv")):
            menu.add_command(label=label, command=lambda f=fmt: self.controller.export(f))
        menu.add_separator()
        for label, fmt in (("当前会话报告 (HTML)", "html"), ("当前会话报告 (Markdown)", "md"),
                           ("当前会话数据 (JSON)", "json")):
            menu.add_command(label=label,
                             command=lambda f=fmt: self.controller.export(f, scope="session"))

    def _make_upload_button(self, parent) -> tk.Button:
        btn = flat_button(parent, "上传…", self.controller.show_upload,
                          padx=theme.PAD_M, image=ui_icon(parent, "upload"), compound="left")
        Tooltip(btn, "上传当前项目的效率报告到 TCER Server")
        return btn

    def _make_llm_button(self, parent) -> tk.Button:
        # ui-sparkle.png 素材暂缺时 ui_icon 返回 None → 纯文字降级，素材后补。
        btn = flat_button(parent, "LLM设置", self.controller.show_llm_config,
                          padx=theme.PAD_M, image=ui_icon(parent, "sparkle"),
                          compound="left")
        Tooltip(btn, "LLM 语义解读配置（OpenAI-compatible 端点，可选本地 Ollama；"
                     "解读入口在会话时间线弹窗）")
        return btn

    def _date_entry(self, bar, var, tip):
        wrap = tk.Frame(bar, bg=theme.BG)
        e = tk.Entry(wrap, textvariable=var, width=10, bg=theme.PANEL, fg=theme.FG,
                     insertbackground=theme.FG, relief="flat", highlightthickness=1,
                     highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        e.bind("<Return>", lambda ev: self._validate_and_reanalyze(var))
        e.bind("<FocusOut>", lambda ev: self._validate_and_reanalyze(var))
        Tooltip(e, tip + "（YYYY-MM-DD）。回车/失焦生效，或点 ▦ 选择日期。")
        e.pack(side="left")
        cal_icon = ui_icon(wrap, "calendar")
        cal = tk.Label(wrap, text="" if cal_icon else "▦", image=cal_icon, compound="left",
                       bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI, cursor=CLICK_CURSOR, padx=5,
                       highlightthickness=1, highlightbackground=theme.BORDER)
        Tooltip(cal, "点击选择日期")
        cal.bind("<Enter>", lambda _ev: cal.config(fg=theme.ACCENT), add="+")
        cal.bind("<Leave>", lambda _ev: cal.config(fg=theme.MUTED), add="+")
        cal.bind("<Button-1>", lambda ev: self._popup_calendar(cal, var))
        cal.pack(side="left")
        return wrap

    def _popup_calendar(self, anchor, var) -> None:
        """点日历图标弹出选日期；选定后写入输入框并重新分析。"""
        def on_select(s: str) -> None:
            var.set(s)
            # 起始日期联动左栏隐藏；结束日期只重新分析会话。
            if var is self.since_var:
                self.controller.apply_time_filter()
            else:
                self.controller.reanalyze()
        CalendarPopup(anchor, on_select, anchor=anchor, initial=var.get())

    @staticmethod
    def _validate_date(s: str) -> bool:
        if not s:
            return True
        from datetime import datetime
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _validate_and_reanalyze(self, var) -> None:
        v = var.get().strip()
        # 起始日期变化要联动左栏隐藏（apply_time_filter）；结束日期只影响会话级 reanalyze。
        target = (self.controller.apply_time_filter
                  if var is self.since_var else self.controller.reanalyze)
        if self._validate_date(v):
            target()
        else:
            var.set("")
            target()

    def _set_preset(self, preset: str) -> None:
        from datetime import datetime, timedelta
        today = datetime.now()
        if preset == "today":
            self.since_var.set(today.strftime("%Y-%m-%d"))
            self.until_var.set("")
        elif preset == "week":
            monday = today - timedelta(days=today.weekday())
            self.since_var.set(monday.strftime("%Y-%m-%d"))
            self.until_var.set("")
        elif preset == "month":
            self.since_var.set(today.replace(day=1).strftime("%Y-%m-%d"))
            self.until_var.set("")
        else:  # all
            self.since_var.set("")
            self.until_var.set("")
        self.controller.apply_time_filter()

    def _on_task_type_change(self, event=None) -> None:
        """任务类型变化时的回调（菜单 command 无 event，故可选）"""
        # task_var 存储的是中文名称，直接触发重新分析
        self.controller.reanalyze()

    def _on_source_change(self, event) -> None:
        self.controller.refresh_projects()

    def get_params(self) -> dict:
        """Analysis params owned by the bar (task type / time)."""
        # 将中文名称转换回英文 key
        display_name = self.task_var.get()
        task_type_key = self._task_reverse_map.get(display_name, display_name)
        return {
            "task_type": task_type_key,
            "since": self.since_var.get().strip() or None,
            "until": self.until_var.get().strip() or None,
        }

    def get_source(self) -> str:
        return self._source_reverse_map.get(self.source_var.get(), "all")

    def restore_prefs(self, prefs: dict) -> None:
        """恢复上次的来源/任务类型/时间区间筛选（在首次 refresh_projects 之前调用）。"""
        src = prefs.get("source")
        if src in self._source_display_names:
            self.source_var.set(self._source_display_names[src])
        tt = prefs.get("task_type")
        if tt in self._task_display_names:
            self.task_var.set(self._task_display_names[tt])
        # since 不恢复——启动固定为今天；until 可恢复上次的结束日期。
        until = prefs.get("until")
        if isinstance(until, str) and self._validate_date(until):
            self.until_var.set(until)

    def set_status(self, text: str) -> None:
        self.status.config(text=text)


class ProjectColumn:
    """Left column: a scrollable list of selectable project cards."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cards: list[Card] = []
        self._selected = None
        self._selected_idx: int | None = None
        self._hidden: set[int] = set()

        col = tk.Frame(parent, bg=theme.PANEL)
        col.pack(side="left", fill="both", expand=True)

        header = tk.Frame(col, bg=theme.PANEL)
        header.pack(fill="x", padx=6, pady=4)
        _pi = ui_icon(header, "project")
        if _pi is not None:
            tk.Label(header, image=_pi, bg=theme.PANEL).pack(side="left")
        self.count_label = tk.Label(header, text="项目", bg=theme.PANEL, fg=theme.FG,
                                    font=theme.FONT_HEADING, anchor="w")
        self.count_label.pack(side="left", padx=(theme.PAD_S, 0))

        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self.scroll = sf
        self.container = sf.inner

    def update(self, projects, empty_projects: set | None = None,
               preferred_uid: str | None = None,
               hidden_projects: set[int] | None = None) -> None:
        for card in self._cards:
            card.frame.destroy()
        self._cards.clear()
        if getattr(self, "_empty_hint", None) is not None:
            self._empty_hint.destroy()
            self._empty_hint = None
        self._selected = None
        self._selected_idx = None
        self._projects = projects
        self._empty = empty_projects or set()
        self._hidden = set(hidden_projects or set())
        for idx, d in enumerate(projects):
            card = self._make_card(d, idx, is_empty=(idx in self._empty))
            self._cards.append(card)
            if idx in self._hidden:            # 时间范围外：建好即隐藏
                card.frame.pack_forget()
        self._refresh_count_label()
        if not projects:
            # 空状态引导：告诉用户去哪里产生数据，而不是留一片空白。
            self._empty_hint = SelectableLabel(
                self.container,
                text="未发现任何会话数据\n\n"
                     "请确认本机存在以下任一目录：\n"
                     "~/.claude（Claude Code）\n"
                     "~/.codex（Codex）\n"
                     "~/.local/share/opencode（OpenCode）\n"
                     "~/.grok（Grok）\n\n"
                     "或切换顶部「来源」筛选后重试。",
                bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI,
                justify="left", pady=theme.PAD_L * 2)
            self._empty_hint.pack(padx=theme.PAD_M)
        self.scroll.update_scroll(reset=True)
        # 选中项目：优先恢复上次选中（启动时），否则第一个有数据且未隐藏的项目。
        if self._cards:
            idx = None
            if preferred_uid is not None:
                idx = next(
                    (i for i, p in enumerate(projects)
                     if (ref_uid(p) == preferred_uid
                         or getattr(p, "key", None) == preferred_uid)
                     and i not in self._empty and i not in self._hidden),
                    None,
                )
            if idx is None:
                idx = next(
                    (i for i in range(len(self._cards))
                     if i not in self._empty and i not in self._hidden),
                    None,
                )
            if idx is not None:
                self._select(self._cards[idx], idx)

    def _make_card(self, project_dir, idx, *, is_empty=False):
        card = Card(self.container,
                    on_click=lambda c, i=idx, e=is_empty: self._on_card_click(c, i, e),
                    on_right_click=lambda e, _i=idx, _d=project_dir: self._on_right_click(e, _i, _d),
                    padx=1, pady=1)
        name = project_label(project_dir)
        label = project_source_label(project_dir)
        if is_empty:
            name += " （无会话）"
        fg = theme.MUTED if is_empty else theme.FG
        icon = source_icon(card.frame, project_icon_key(project_dir))
        if icon is None:
            # 无图标资源：回退到 [源名] 文字前缀
            drive = project_drive(project_dir)
            prefix = f"[{label}] {drive}: " if drive else f"[{label}] "
            lbl = tk.Label(card.frame, text=prefix + name, bg=theme.PANEL_2, fg=fg,
                           font=theme.FONT_UI_BOLD, anchor="w")
            lbl.pack(fill="x", padx=3, pady=3)
            card.bind_to(lbl)
            return card
        # 图标 + 盘符 + 名称横排，取代 [Claude] 之类的文字标注。盘符小字灰显
        # （同事在不同盘建同名文件夹时，列表里靠它区分——名字本身被剥掉盘符）。
        row = tk.Frame(card.frame, bg=theme.PANEL_2)
        row.pack(fill="x", padx=3, pady=3)
        img_lbl = tk.Label(row, image=icon, bg=theme.PANEL_2)
        img_lbl.pack(side="left", padx=(0, 4))
        Tooltip(img_lbl, label)  # 悬停图标显示来源名（图标取代了文字标注）
        drive = project_drive(project_dir)
        bindees = [row, img_lbl]
        if drive:
            drive_lbl = tk.Label(row, text=f"{drive}:", bg=theme.PANEL_2,
                                 fg=theme.MUTED, font=theme.FONT_UI_SMALL_BOLD,
                                 anchor="w", padx=1)
            drive_lbl.pack(side="left", padx=(0, 3))
            Tooltip(drive_lbl, f"项目所在盘符 {drive}:（同名项目可能在不同盘）")
            bindees.append(drive_lbl)
        name_lbl = tk.Label(row, text=name, bg=theme.PANEL_2, fg=fg,
                            font=theme.FONT_UI_BOLD, anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)
        bindees.append(name_lbl)
        for w in bindees:
            card.bind_to(w)
        return card

    def _on_card_click(self, card, idx, is_empty):
        if is_empty:
            return  # 空项目不响应点击
        self._select(card, idx)

    def _select(self, card, idx=None, *, notify: bool = True):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        self._selected_idx = idx
        card.set_selected(True)
        if idx is not None and notify:
            self.controller.on_select_project(idx)

    def select_idx(self, idx: int, *, notify: bool = True) -> None:
        """按索引视觉选中（bounds 安全）。notify=False 不回调 controller。"""
        if 0 <= idx < len(self._cards):
            self._select(self._cards[idx], idx, notify=notify)

    def set_hidden(self, hidden) -> None:
        """轻量显隐：不重建卡片，按索引序 re-pack 可见项、forget 隐藏项。

        若当前选中卡被隐藏，清其高亮（改选由 controller 决定）。
        """
        self._hidden = set(hidden)
        for idx, card in enumerate(self._cards):
            if idx in self._hidden:
                card.frame.pack_forget()
            else:
                card.frame.pack(fill="x", padx=1, pady=1)   # 按索引序，顺序保持
        if self._selected_idx is not None and self._selected_idx in self._hidden:
            if self._selected is not None:
                self._selected.set_selected(False)
            self._selected = None
            self._selected_idx = None
        self._refresh_count_label()
        self.scroll.update_scroll()

    def _refresh_count_label(self) -> None:
        n = len(self._projects)
        h = len(self._hidden)
        if h:
            self.count_label.config(text=f"项目（{n - h}）（隐藏 {h}）")
        else:
            self.count_label.config(text=f"项目（{n}）")

    def _on_right_click(self, event, idx, project_dir):
        """Right-click context menu on a project card."""
        name = project_label(project_dir)
        is_empty = idx in self._empty
        menu = FlatMenu(self.container)

        if is_empty:
            menu.add_command(
                label=f"{name[:30]}（无会话数据）", state="disabled",
                image=ui_icon(self.container, "empty"), compound="left",
            )
        else:
            menu.add_command(
                label=f"刷新此项目 · {name[:30]}",
                command=lambda: self._select_and_refresh(idx),
                image=ui_icon(self.container, "refresh"), compound="left",
            )

            menu.add_separator()

            menu.add_command(
                label="项目视角",
                command=lambda: self._select_and_view(idx, "project"),
                image=ui_icon(self.container, "view-project"), compound="left",
            )
            menu.add_command(
                label="会话视角",
                command=lambda: self._select_and_view(idx, "session"),
                image=ui_icon(self.container, "view-session"), compound="left",
            )

        menu.add_separator()

        menu.add_command(
            label=f"在{_file_manager_label()}中打开",
            command=lambda: self._open_in_explorer(project_dir),
            image=ui_icon(self.container, "folder"), compound="left",
        )
        menu.add_command(
            label="复制项目路径",
            command=lambda: self._copy_text(project_open_path(project_dir)),
            image=ui_icon(self.container, "copy"), compound="left",
        )
        menu.add_command(
            label="复制项目名称",
            command=lambda: self._copy_text(name),
            image=ui_icon(self.container, "copy"), compound="left",
        )

        menu.tk_popup(event.x_root, event.y_root)

    def _select_and_refresh(self, idx):
        self._select(self._cards[idx], idx)

    def _select_and_view(self, idx, mode):
        already_selected = (self._selected is self._cards[idx])
        if already_selected and self.controller._current:
            # Data already loaded — just switch view mode and re-render
            self.controller.view_mode.set(mode)
            self.controller._on_view_change()
        else:
            # Need to load data first; switch mode, then select (triggers reanalyze)
            self.controller.view_mode.set(mode)
            self._select(self._cards[idx], idx)

    def _open_in_explorer(self, project_dir):
        from .platform import open_in_file_manager
        open_in_file_manager(project_open_path(project_dir))

    def _copy_text(self, text):
        self.controller.root.clipboard_clear()
        self.controller.root.clipboard_append(text)


class SessionColumn:
    """Middle column: a scrollable list of selectable session cards."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cards: list[Card] = []       # 当前过滤后可见的卡片
        self._all_cards: list[Card] = []   # 全量卡片（过滤只 pack/pack_forget 复用）
        self._selected = None

        col = tk.Frame(parent, bg=theme.PANEL)
        col.pack(side="left", fill="both", expand=True)

        header = tk.Frame(col, bg=theme.PANEL)
        header.pack(fill="x", padx=6, pady=4)
        _hi = ui_icon(header, "session")
        if _hi is not None:
            tk.Label(header, image=_hi, bg=theme.PANEL).pack(side="left")
        self.count_label = tk.Label(header, text="会话", bg=theme.PANEL, fg=theme.FG,
                                    font=theme.FONT_HEADING, anchor="w")
        self.count_label.pack(side="left", padx=(theme.PAD_S, 0))
        # 搜索框：放大镜置于框内左侧（Frame 包裹，视觉一体），点放大镜聚焦输入。
        self._filter_var = tk.StringVar(value="")
        search_wrap = tk.Frame(header, bg=theme.PANEL_2, highlightthickness=1,
                               highlightbackground=theme.BORDER)
        search_wrap.pack(side="right")  # 先 pack → 占最右
        _si = ui_icon(search_wrap, "search")
        if _si is not None:
            _s_lbl = tk.Label(search_wrap, image=_si, bg=theme.PANEL_2, cursor=CLICK_CURSOR)
            _s_lbl.pack(side="left", padx=(3, 0), pady=1)
            _s_lbl.bind("<Button-1>", lambda _e: search.focus_set())
        search = tk.Entry(search_wrap, textvariable=self._filter_var, width=10,
                          bg=theme.PANEL_2, fg=theme.FG, insertbackground=theme.FG,
                          relief="flat", borderwidth=0, highlightthickness=0,
                          font=theme.FONT_UI_SMALL)
        search.pack(side="left", padx=(2, 5), pady=1)
        Tooltip(search, "按标题 / 会话 ID / 模型 过滤（实时）")
        # 红旗快速过滤：点击只看打了红旗的会话（与搜索词叠加）。
        self._flag_only = tk.BooleanVar(value=False)
        self._ff_img = {"off": ui_icon(header, "flag"), "on": ui_icon(header, "flag-on")}
        _ff0 = self._ff_img["off"]
        if _ff0 is not None:
            self._flag_filter = tk.Label(header, image=_ff0, bg=theme.PANEL, cursor=CLICK_CURSOR)
            self._flag_filter.image = _ff0
        else:
            self._flag_filter = tk.Label(header, text="旗", bg=theme.PANEL,
                                         fg=theme.MUTED, font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        self._flag_filter.pack(side="right", padx=(6, 2))  # 后 pack → 搜索框左边
        self._flag_filter.bind("<Button-1>", lambda _e: self._toggle_flag_only())
        self._flag_filter.bind("<Enter>", lambda _e: self._flag_filter.configure(bg=theme.HOVER_BG))
        self._flag_filter.bind("<Leave>", lambda _e: self._flag_filter.configure(bg=theme.PANEL))
        Tooltip(self._flag_filter, "只看红旗会话")
        self._filter_var.trace_add("write", lambda *_a: self._render())
        self._all_reports: list = []
        # 当前项目下被置顶 / 标红的 sid 集合（由 controller 下发，排序与卡片图标用）。
        self._pinned: set[str] = set()
        self._flagged: set[str] = set()

        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self.scroll = sf
        self.container = sf.inner

    def update(self, reports, pinned=None, flagged=None, reset=True) -> None:
        if pinned is not None:
            self._pinned = set(pinned)
        if flagged is not None:
            self._flagged = set(flagged)
        self._all_reports = self._sorted(reports)
        self._rebuild_cards()
        self._render(reset=reset)

    def _rebuild_cards(self) -> None:
        """销毁并按当前排序/标记重建全部卡片（数据或置顶/红旗标记变化时）。

        搜索/红旗**过滤**不重建——见 _render（打字每键都重建 100 张卡要
        ~600ms，主线程明显卡顿；pack 复用降到毫秒级）。构建本身也分批走
        ``after_idle``：首批同步建好即可见，其余每空闲批补齐，长列表不再
        一次性冻结主线程。
        """
        if getattr(self, "_build_after", None) is not None:
            try:
                self.container.after_cancel(self._build_after)
            except tk.TclError:
                pass
            self._build_after = None
        for card in getattr(self, "_all_cards", ()):
            card.frame.destroy()
        self._all_cards = []
        self._selected = None
        self._pending_reports = list(self._all_reports)
        self._pending_select_sid = None
        self._build_batch()

    _BUILD_BATCH = 25

    def _build_batch(self) -> None:
        """建一批卡片（同步 ~150ms 上限），未完待续走 after_idle。"""
        batch = self._pending_reports[: self._BUILD_BATCH]
        del self._pending_reports[: self._BUILD_BATCH]
        needle = self._filter_var.get().strip().casefold()
        flag_only = self._flag_only.get()
        for r in batch:
            card = self._make_card(r)  # Card 自 pack（尾部追加，顺序正确）
            self._all_cards.append(card)
            if self._filtered_out(r, needle, flag_only):
                card.frame.pack_forget()
        if self._pending_reports:
            # 用 after(1) 而非 after_idle：idle 回调会被 update_scroll 里的
            # update_idletasks() 一次性全部触发（等于没分批）；带延迟的定时器
            # 只在主循环正常轮转时到期，批与批之间 UI 可响应输入。
            self._build_after = self.container.after(1, self._build_batch)
            return
        self._build_after = None
        # 全部建完：刷新计数/空提示，并补发构建期间被请求的选中。
        self._render()
        if self._pending_select_sid is not None:
            sid, self._pending_select_sid = self._pending_select_sid, None
            self.select_by_sid(sid, notify=False)

    def _filtered_out(self, r, needle: str, flag_only: bool) -> bool:
        if needle and not (
            needle in (r.meta.title or "").casefold()
            or needle in (r.meta.session_id or r.meta.path.stem).casefold()
            or any(needle in _m.casefold() for _m in r.usage.models)
        ):
            return True
        if flag_only and (r.meta.session_id or r.meta.path.stem) not in self._flagged:
            return True
        return False

    def _sorted(self, reports):
        """置顶段排前，段内及非置顶段均按结束时间倒序。"""
        return sorted(reports,
                      key=lambda r: (
                          1 if (r.meta.session_id or r.meta.path.stem) in self._pinned else 0,
                          r.usage.ended_at or r.usage.started_at or 0,
                      ),
                      reverse=True)

    def _apply_marks(self, pinned, flagged, keep_sid=None, reset=False) -> None:
        """toggle 后局部刷新：更新 marks → 重排 → 重绘 → 恢复选中。

        reset=True 滚到顶（置顶后看效果），False 保留滚动位置（红旗不改顺序）。
        """
        self._pinned = set(pinned)
        self._flagged = set(flagged)
        self._all_reports = self._sorted(self._all_reports)
        self._rebuild_cards()  # 置顶/红旗图标在卡片上，标记变化须重建
        self._render(reset=reset)
        if keep_sid is not None:
            self.select_by_sid(keep_sid, notify=False)

    def _toggle_flag_only(self) -> None:
        """切换「只看红旗」过滤，更新按钮图标并重绘。"""
        new = not self._flag_only.get()
        self._flag_only.set(new)
        img = self._ff_img["on"] if new else self._ff_img["off"]
        if img is not None:
            self._flag_filter.configure(image=img, text="")
            self._flag_filter.image = img
        else:
            self._flag_filter.configure(image="", text="旗",
                                        fg=theme.ERROR if new else theme.MUTED)
        self._render()

    def _render(self, reset: bool = False) -> None:
        """搜索 / 红旗过滤：只 pack/pack_forget 复用已建卡片，不销毁重建。

        搜索框每个键入字符都会走到这里——重建 100 张卡 ~600ms 会卡成幻灯片，
        pack 调整是毫秒级。保持可见卡片按 _all_reports 顺序重 pack（pack 顺序
        即显示顺序）。"""
        needle = self._filter_var.get().strip().casefold()
        flag_only = self._flag_only.get()
        self._reports = []
        self._cards = []
        for r, card in zip(self._all_reports, self._all_cards):
            if self._filtered_out(r, needle, flag_only):
                card.frame.pack_forget()
                continue
            card.frame.pack(fill="x", padx=1, pady=1)
            self._reports.append(r)
            self._cards.append(card)
        # 被过滤掉的卡片若处于选中态，视觉随隐藏消失；引用一并清掉。
        if self._selected is not None and self._selected not in self._cards:
            self._selected = None
            self._selected_idx = None
        if getattr(self, "_empty_hint", None) is not None:
            self._empty_hint.destroy()
            self._empty_hint = None
        if not self._reports:
            hint = ("无匹配会话，试试清空搜索框 / 关闭红旗过滤"
                    if (needle or flag_only)
                    else "该项目暂无会话\n（或尚未完成分析）")
            self._empty_hint = tk.Label(self.container, text=hint,
                                        bg=theme.PANEL, fg=theme.MUTED,
                                        font=theme.FONT_UI, justify="center",
                                        pady=theme.PAD_L * 2)
            self._empty_hint.pack(padx=theme.PAD_M)
        n_all = len(self._all_reports)
        label = (f"会话（{len(self._reports)}/{n_all}）" if (needle or flag_only)
                 else f"会话（{n_all}）")
        self.count_label.config(text=label)
        self.scroll.update_scroll(reset=reset)

    def _make_card(self, r):
        sid = r.meta.session_id or r.meta.path.stem
        title = r.meta.title or "(无标题)"
        card = Card(self.container,
                    on_click=lambda c, s=sid: self._select(c, s),
                    on_right_click=lambda e, _r=r, _s=sid: self._on_right_click(e, _r, _s))
        time_ms = r.usage.ended_at or r.usage.started_at
        # 时间行：左时间，右置顶/红旗可点击图标（左键 toggle，不触发卡片选中）。
        top_row = tk.Frame(card.frame, bg=theme.PANEL_2)
        top_row.pack(fill="x", padx=6, pady=(4, 1))
        t_lbl = tk.Label(top_row, text=fmt_dt(time_ms, FMT_SHORT_MINUTE) if time_ms else "-",
                         bg=theme.PANEL_2, fg="#888888", font=theme.FONT_MONO, anchor="w")
        t_lbl.pack(side="left")
        marks_row = tk.Frame(top_row, bg=theme.PANEL_2)
        marks_row.pack(side="right")
        self._mark_icon(marks_row, card, sid, "pin",
                        is_on=sid in self._pinned, tip="置顶 / 取消置顶")
        self._mark_icon(marks_row, card, sid, "flag",
                        is_on=sid in self._flagged, tip="红旗 / 取消红旗")
        title_disp = title[:35] + "..." if len(title) > 35 else title
        ti_lbl = tk.Label(card.frame, text=title_disp, bg=theme.PANEL_2, fg=theme.FG,
                          font=theme.FONT_UI_SMALL, anchor="w")
        ti_lbl.pack(fill="x", padx=6, pady=(1, 1))
        # 摘要行：左侧主模型短名，右侧消耗成本金额（纯文本，无卡片底）——
        # 不点开指标页就能扫一眼谁花的多少（数据全在 report 上，零额外扫描）。
        sum_row = tk.Frame(card.frame, bg=theme.PANEL_2)
        sum_row.pack(fill="x", padx=6, pady=(0, 1))
        sum_lbl = tk.Label(sum_row, bg=theme.PANEL_2, fg=theme.MUTED,
                           font=theme.FONT_UI_SMALL, anchor="w",
                           text=self._summary_line(r))
        sum_lbl.pack(side="left")
        # 成本对齐指标分类「总成本」的数值样式：FONT_VALUE 等宽粗体 + 按好坏
        # 方向着色——与 MetricCell.set_value 同一规则（down 指标：>0 红、
        # ==0 绿「零成本即最优」），精度按卡片场景收窄到两位小数。
        cost_fg = (theme.VALUE_BAD if r.cost > 0 else theme.VALUE_GOOD)
        cost_lbl = tk.Label(sum_row, bg=theme.PANEL_2, fg=cost_fg,
                            font=theme.FONT_VALUE, anchor="e",
                            text=f"${r.cost:.2f}")
        cost_lbl.pack(side="right")
        sid_disp = sid[:36] + "..." if len(sid) > 36 else sid
        sid_lbl = tk.Label(card.frame, text=sid_disp, bg=theme.PANEL_2, fg="#6B7077",
                           font=theme.FONT_MONO, cursor=CLICK_CURSOR, anchor="w")
        sid_lbl.pack(fill="x", padx=6, pady=(1, 4))
        # top_row/marks_row/时间标签随卡片选中；标记图标自行绑事件（见 _mark_icon）。
        for w in (top_row, t_lbl, marks_row, ti_lbl, sum_row, sum_lbl,
                  cost_lbl, sid_lbl):
            card.bind_to(w)
            w.bind("<Double-Button-1>", lambda e, s=sid: self.controller.show_session_detail(s))
        return card

    def _summary_line(self, r) -> str:
        """卡片摘要行文本：模型短名 · 持续时间（成本在右侧、评级已移除）。"""
        from tcer.core import pricing as _pricing
        from tcer.core.format import fmt_duration_ms
        main = max(r.usage.per_model.items(),
                   key=lambda kv: getattr(kv[1], "total", 0),
                   default=None) if r.usage.per_model else None
        model_txt = _pricing.label(main[0]) if main else "-"
        dur = fmt_duration_ms(r.usage.session_duration_ms)
        return f"{model_txt} · {dur}" if dur != "-" else model_txt

    def _mark_icon(self, parent, card, sid, kind, *, is_on, tip):
        """卡片右上角可点击标记图标：左键 toggle（不选中卡片），右键复用卡片菜单。

        kind 为 "pin"（置顶）/ "flag"（红旗）。激活态用彩色 ``<kind>-on`` 图标，
        未激活用灰色 ``<kind>`` 图标；缺资源回退到着色字符（置顶 ▾ / 红旗 ◆）。
        """
        img = ui_icon(self.container, f"{kind}-on" if is_on else kind)
        if img is not None:
            lbl = tk.Label(parent, image=img, bg=theme.PANEL_2, cursor=CLICK_CURSOR)
            lbl.image = img  # 防 GC（ui_icon 已模块级缓存，双保险）
        else:
            ch = "▾" if kind == "pin" else "◆"
            fg = (theme.ACCENT if kind == "pin" else theme.ERROR) if is_on else theme.MUTED
            lbl = tk.Label(parent, text=ch, bg=theme.PANEL_2, fg=fg,
                           font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        lbl.pack(side="left", padx=(2, 0))

        def toggle(_e):
            if kind == "pin":
                self.controller.toggle_session_pin(sid)
            else:
                self.controller.toggle_session_flag(sid)

        lbl.bind("<Button-1>", toggle)
        lbl.bind("<Button-3>", card._on_right_click)   # 右键仍走卡片菜单
        lbl.bind("<Enter>", lambda _e: lbl.configure(bg=theme.HOVER_BG))
        lbl.bind("<Leave>", lambda _e: lbl.configure(bg=theme.PANEL_2))
        Tooltip(lbl, tip)
        return lbl

    def _select(self, card, sid, *, notify=True):
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = card
        card.set_selected(True)
        if notify:
            self.controller.on_select_session(sid)

    def _on_right_click(self, event, report, sid):
        """Right-click context menu on a session card."""
        from . import popups
        menu = FlatMenu(self.container)

        # 标记操作（高频卡片状态管理，放最上面，与卡片角标一致）。
        menu.add_command(
            label="取消置顶" if sid in self._pinned else "置顶",
            command=lambda: self.controller.toggle_session_pin(sid),
            image=ui_icon(self.container, "pin-on"), compound="left",
        )
        menu.add_command(
            label="取消红旗" if sid in self._flagged else "加红旗",
            command=lambda: self.controller.toggle_session_flag(sid),
            image=ui_icon(self.container, "flag-on"), compound="left",
        )

        menu.add_separator()

        # Session info sub-items
        menu.add_command(
            label=f"查看详情 · {sid[:20]}…",
            command=lambda: self.controller.show_session_detail(sid),
            image=ui_icon(self.container, "session"), compound="left",
        )
        menu.add_command(
            label="查看工具调用",
            command=lambda: popups.ToolCallsPopup(
                self.controller.root, report.usage, f" · {sid[:16]}…"),
            image=ui_icon(self.container, "wrench"), compound="left",
        )
        # All sources keep a count; bodies are lazy-loaded on popup open.
        has_user_msgs = report.usage.user_msgs > 0
        menu.add_command(
            label=f"查看用户消息（{report.usage.user_msgs} 条）",
            command=lambda: self._show_user_msgs(report),
            state="normal" if has_user_msgs else "disabled",
            image=ui_icon(self.container, "chat"), compound="left",
        )
        has_files = bool(report.files_touched_details)
        menu.add_command(
            label=f"查看涉及文件（{report.files_touched} 个）",
            command=lambda: popups.FilesTouchedPopup(
                self.controller.root, report.files_touched_details,
                report.searched_paths_details),
            state="normal" if has_files else "disabled",
            image=ui_icon(self.container, "folder"), compound="left",
        )
        menu.add_command(
            label="查看模型使用",
            command=lambda: popups.ModelsPopup(
                self.controller.root, report.usage, f" · {sid[:16]}…"),
            image=ui_icon(self.container, "model"), compound="left",
        )

        menu.add_separator()

        # Analysis sub-items
        has_score = report.score is not None
        menu.add_command(
            label="查看效率雷达",
            command=lambda: popups.RadarPopup(
                self.controller.root, report, self._reports),
            state="normal" if has_score else "disabled",
            image=ui_icon(self.container, "target"), compound="left",
        )
        menu.add_command(
            label="在趋势图中定位",
            command=lambda: self._navigate_to_trend(sid),
            image=ui_icon(self.container, "trend"), compound="left",
        )

        menu.add_separator()

        # File location
        menu.add_command(
            label=f"在{_file_manager_label()}中打开",
            command=lambda: self._open_session_file(report),
            image=ui_icon(self.container, "folder"), compound="left",
        )

        # Copy actions
        menu.add_command(
            label="复制会话路径",
            command=lambda: self._copy_text(str(report.meta.path)),
            image=ui_icon(self.container, "copy"), compound="left",
        )
        menu.add_command(
            label="复制会话 ID",
            command=lambda: self._copy_text(sid),
            image=ui_icon(self.container, "copy"), compound="left",
        )
        title = report.meta.title or "(无标题)"
        menu.add_command(
            label="复制会话标题",
            command=lambda: self._copy_text(title),
            image=ui_icon(self.container, "copy"), compound="left",
        )
        cost_str = format_value("cost", report.cost)
        tcer_str = format_value("tcer", report.tcer)
        score_str = format_value("score", report.score)
        menu.add_command(
            label=f"复制摘要（TCER={tcer_str} · 效率分={score_str} · {cost_str}）",
            command=lambda: self._copy_text(
                f"会话: {sid}\n标题: {title}\n"
                f"TCER: {tcer_str} · 综合效率分: {score_str} · 成本: {cost_str}"),
            image=ui_icon(self.container, "copy"), compound="left",
        )

        menu.add_separator()

        # Destructive action — last item, gated behind a二次确认对话框.
        readonly = report.meta.source in ("codex", "opencode", "grok", "omp", "pi")
        delete_state = "disabled" if readonly else "normal"
        delete_label = "删除会话…" if not readonly else f"删除会话（{project_source_label(report.meta)} 只读）"
        menu.add_command(
            label=delete_label,
            command=lambda: self._confirm_delete(report, sid),
            state=delete_state,
            image=ui_icon(self.container, "trash"), compound="left",
        )

        menu.tk_popup(event.x_root, event.y_root)

    def _confirm_delete(self, report, sid):
        """弹出二次确认；确认后彻底删除该会话（含 subagent / tool-results）。"""
        from . import popups
        title = report.meta.title or "(无标题)"
        popups.ConfirmDeletePopup(
            self.controller.root,
            title=title, session_id=sid,
            on_confirm=lambda: self.controller.delete_session(report),
        )

    def _show_user_msgs(self, report):
        old = getattr(self.controller, "_rendered_report", None)
        self.controller._rendered_report = report
        self.controller.show_user_msgs()
        self.controller._rendered_report = old

    def _navigate_to_trend(self, sid):
        """Switch to trend tab and highlight this session's data point."""
        from tkinter import messagebox
        if not self.controller._current:
            messagebox.showinfo("定位", "请先分析一个项目，趋势图才有数据。")
            return
        # Switch notebook to the tab hosting the trend chart（按控件归属定位，
        # 不硬编码索引——页签重排后索引会失同步）。
        try:
            nb = self.controller._nb
            trend_root = self.controller.trend_chart._body  # 页签直接子控件
            for tab_id in nb.tabs():
                if nb.nametowidget(tab_id) is trend_root:
                    nb.select(tab_id)
                    break
        except Exception:
            pass
        # Ensure trend chart has data (may not have been drawn yet)
        tc = self.controller.trend_chart
        if not tc._reports:
            tc.update(self.controller._current.reports)
        # Highlight the session in the trend chart
        tc.select_session_by_sid(sid)
        # Also select in the session column for consistency
        self.controller.on_select_session(sid)

    def _copy_text(self, text):
        self.controller.root.clipboard_clear()
        self.controller.root.clipboard_append(text)

    def _open_session_file(self, report):
        from .platform import open_in_file_manager
        open_in_file_manager(str(report.meta.path))

    def clear_selection(self) -> None:
        if self._selected is not None:
            self._selected.set_selected(False)
        self._selected = None

    def select_first(self, *, notify=True) -> str | None:
        """Select the first session card (if any); return its sid."""
        if not self._cards:
            return None
        sid = self._reports[0].meta.session_id or self._reports[0].meta.path.stem
        self._select(self._cards[0], sid, notify=notify)
        return sid

    def select_by_sid(self, sid: str, *, notify=True) -> bool:
        """Select the card whose session id matches ``sid``; return True if found."""
        for card, r in zip(self._cards, self._reports):
            if (r.meta.session_id or r.meta.path.stem) == sid:
                self._select(card, sid, notify=notify)
                return True
        # 分批构建尚未完成：记下待选，最后一批建完时补发。
        if getattr(self, "_pending_reports", None):
            self._pending_select_sid = sid
        return False


@dataclass
class _MetricGrid:
    """Per-grid collapse state for MetricPanel: the cells, the expander label,
    and whether empty (「-」) cells are currently shown."""
    frame: tk.Frame
    cells: list
    expander: tk.Label
    expander_row: int
    expanded: bool = False


@dataclass
class _GroupState:
    """Per-group collapse state: header arrow label, body frame holding the
    group's subgroups/grids, and whether the group is collapsed."""
    name: str
    arrow: tk.Label
    body: tk.Frame
    collapsed: bool = False


class MetricPanel:
    """Right-column tab 1: the G1–G6 metric grid, built from metric_defs."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._cells: dict[str, MetricCell] = {}
        self._grids: list[_MetricGrid] = []
        self._groups: list[_GroupState] = []

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self.container = sf.inner

        for group in GROUPS:
            self._build_group(group)

    def _build_group(self, group) -> None:
        gframe = tk.Frame(self.container, bg=theme.BG)
        gframe.pack(fill="x", pady=(1, 0))
        header = tk.Frame(gframe, bg=theme.GROUP_COLORS[group.id], padx=6, pady=1)
        header.pack(fill="x")
        # 「代码产出与质量」(G4) 项多、常只需概览 → 默认折叠；其余默认展开。
        collapsed = group.id == "G4"
        arrow_lbl = tk.Label(header, text=f"{'▶' if collapsed else '▼'} {group.name}",
                             bg=theme.GROUP_COLORS[group.id], fg=theme.FG,
                             font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor=CLICK_CURSOR)
        arrow_lbl.pack(side="left")
        # body 容纳该组全部子组/网格；折叠时 pack_forget 它（整组收起）。
        body = tk.Frame(gframe, bg=theme.BG)
        body.pack(fill="x")
        if group.subgroups:
            for sg in group.subgroups:
                self._build_metric_grid(sg.metrics, sub_label=sg.name, parent=body)
        else:
            self._build_metric_grid(group.metrics, parent=body)
        gs = _GroupState(name=group.name, arrow=arrow_lbl, body=body, collapsed=collapsed)
        self._groups.append(gs)
        for w in (header, arrow_lbl):
            w.bind("<Button-1>", lambda e, s=gs: self._toggle_group(s))
        if collapsed:
            body.pack_forget()

    def _build_metric_grid(self, metrics, sub_label: str | None = None,
                           parent=None) -> None:
        parent = parent or self.container
        if sub_label:
            sub = tk.Frame(parent, bg=theme.PANEL, padx=8, pady=0)
            sub.pack(fill="x", pady=(1, 0))
            tk.Label(sub, text=f"· {sub_label}", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(parent, bg=theme.PANEL, padx=4, pady=1)
        grid.pack(fill="x", pady=(0, 0))
        cells: list[MetricCell] = []
        for i, metric in enumerate(metrics):
            if metric.key == "tools":
                on_click = self.controller.show_tool_calls
            elif metric.key == "models":
                on_click = self.controller.show_models
            elif metric.key == "user_msgs":
                on_click = self.controller.show_user_msgs
            elif metric.key == "files_touched":
                on_click = self.controller.show_files_touched
            elif metric.key == "memory_files":
                on_click = self.controller.show_memory_files
            elif metric.key == "cost":
                on_click = self.controller.show_cost_breakdown
            else:
                on_click = None
            from .metric_defs import APPROX_KEYS
            cell = MetricCell(grid, metric, on_click=on_click,
                              approx=metric.key in APPROX_KEYS)
            cell.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW, sticky="nsew", padx=2)
            self._cells[metric.key] = cell
            cells.append(cell)
        for c in range(_PER_ROW):
            grid.grid_columnconfigure(c, weight=1)
        # Collapse expander: lives INSIDE the grid so grid_remove/grid() preserves
        # its row — pack_forget + pack would reshuffle it past the next group
        # header. Row = len(metrics) is guaranteed below every cell row; empty
        # rows between collapse and expand are auto-collapsed by Tk's grid.
        exp_row = len(metrics)
        expander = tk.Label(grid, text="", bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor=CLICK_CURSOR)
        expander.grid(row=exp_row, column=0, columnspan=_PER_ROW,
                      sticky="w", pady=(1, 0))
        expander.grid_remove()  # hidden until _apply_grid finds empty cells
        state = _MetricGrid(frame=grid, cells=cells, expander=expander,
                            expander_row=exp_row, expanded=False)
        expander.bind("<Button-1>", lambda e, s=state: self._toggle(s))
        self._grids.append(state)

    def update(self, report) -> None:
        vals = report_values(report)
        for key, cell in self._cells.items():
            cell.set_value(vals.get(key, "-"))
        for state in self._grids:
            self._apply_grid(state)

    def clear(self) -> None:
        for cell in self._cells.values():
            cell.set_value("-")
        for state in self._grids:
            self._apply_grid(state)

    def _toggle(self, state: _MetricGrid) -> None:
        state.expanded = not state.expanded
        self._apply_grid(state)

    def _toggle_group(self, gs: _GroupState) -> None:
        """点击分组标题：折叠/展开整组（隐藏该组 body 下所有子组与网格）。"""
        gs.collapsed = not gs.collapsed
        gs.arrow.config(text=f"{'▶' if gs.collapsed else '▼'} {gs.name}")
        if gs.collapsed:
            gs.body.pack_forget()
        else:
            gs.body.pack(fill="x")

    def _apply_grid(self, state: _MetricGrid) -> None:
        """Reflow one grid: hide empty (「-」) cells when collapsed, repack the
        rest tightly, and show/hide the expander row. The empty set is recomputed
        every call so a session change that fills a previously-empty metric
        brings its cell back automatically; the user's expand/collapse choice
        persists on ``state.expanded`` across updates."""
        empty = [c for c in state.cells
                 if c.var.get() in ("-", UNSUPPORTED_LABEL)]
        n_empty = len(empty)
        if state.expanded or n_empty == 0:
            shown = state.cells
        else:
            empty_ids = {id(c) for c in empty}
            shown = [c for c in state.cells if id(c) not in empty_ids]
        for i, c in enumerate(shown):
            c.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW,
                         sticky="nsew", padx=2)
        if not state.expanded and n_empty > 0:
            for c in empty:
                c.frame.grid_remove()
        if n_empty == 0:
            state.expander.grid_remove()
        else:
            arrow = "▼" if state.expanded else "▶"
            action = "收起" if state.expanded else "展开"
            state.expander.config(text=f"{arrow} {n_empty} 项无数据或不适用（点击{action}）")
            state.expander.grid()


# --------------------------------------------------------------------------- #
# Charts (Canvas)
# --------------------------------------------------------------------------- #
class ScoreRankingView:
    """Tab 2: interactive 综合效率分 ranking dashboard.

    Layout:
      [Tier summary bar — clickable filter chips]
      [Treeview table (left) | Decompose panel (right)]

    Treeview columns: #, 会话, 效率分, 等级. Click header to sort.
    Decompose panel: summary card + 3-axis bars + project avg comparison.
    """

    # Axis metadata (names / formulas / 中性阈值) comes from the metric SSOT
    # (metric_defs.SCORE_AXES); colours are the shared theme value colours.

    def __init__(self, parent, controller=None) -> None:
        self._controller = controller
        self._ranking: list[tuple] = []  # (label, score, tier, report)
        self._avg_factors: dict[str, float] | None = None
        self._current_report = None
        self._grade_filter: str | None = None   # 选中的评级过滤（tier 名）
        self._sort_col: str = "score"
        self._sort_reverse: bool = True
        # 项目聚合报告（项目视角主体卡 + 贡献归因榜的基准分）。由 update() 传入。
        self._aggregate = None
        # 当前视角：project=贡献归因榜+项目概览，session=名次榜+会话构成。
        self._view_mode: str = "project"
        # 程序化设置 treeview 选中会触发 <<TreeviewSelect>>；置位时忽略回调，
        # 只让「用户真实点击」翻转视角，避免排序/视角同步造成的重入循环。
        self._suppress_select = False

        # -- Tier summary bar (top, 可折叠) --
        # 评级分布是「会话排名」的配套总览——排名表仅会话视角可见，故此条也
        # 只在会话视角显示（项目视角隐藏，见 _apply_layout）。
        self._grade_sec = grade_sec = CollapsibleSection(parent, "评级分布",
                                       theme.GROUP_COLORS["G_NEUTRAL"], expand=False)
        self._grade_canvas = tk.Canvas(grade_sec.content, bg=theme.PANEL, height=38,
                                       highlightthickness=0)
        self._grade_canvas.pack(fill="x", padx=2, pady=(0, 1))
        self._grade_canvas.bind("<Configure>", lambda e: self._draw_grade_bar())
        self._grade_canvas.bind("<Button-1>", self._on_grade_click)
        self._grade_rects: list[tuple[int, int, int, int, str]] = []

        # -- Split: table (left) + decompose (right) --
        paned = tk.PanedWindow(parent, orient="horizontal", bg=theme.BG, sashwidth=3)
        paned.pack(fill="both", expand=True, padx=2, pady=2)
        # TCER 回退提示条挂在 paned 之前（见 update）。
        self._note_parent = parent
        self._paned_ref = paned
        self._fallback_note = None
        self._fallback_tcer = False

        # 左栏（排名表）：会话视角收窄、项目视角保持较宽——minsize 取较小值，
        # 具体宽度由 _apply_sash 按视角设置。
        table_frame = tk.Frame(paned, bg=theme.BG)
        paned.add(table_frame, minsize=180)
        self._table_frame = table_frame

        decomp_frame = tk.Frame(paned, bg=theme.BG)
        paned.add(decomp_frame, minsize=340)
        # 左栏目标宽度（像素）：会话视角更窄（表只作定位），项目视角略宽。
        self._sash_session = 210
        self._sash_project = 300

        # -- Treeview with 可折叠标题 --
        self._tree_sec = tree_sec = CollapsibleSection(table_frame, "会话排名",
                                                       theme.GROUP_COLORS["G2"])
        # delta 列（贡献Δ = 会话分 − 项目聚合分）只在项目视角显示，用
        # displaycolumns 切换列集，无需重建 Treeview。
        cols = ("rank", "session", "score_val", "delta", "tier")
        self._tree = ttk.Treeview(tree_sec.content, columns=cols, show="headings",
                                  selectmode="browse", height=20)
        self._tree.heading("rank",    text="#",    anchor="center",
                           command=lambda: self._sort_by("rank"))
        self._tree.heading("session", text="标题", anchor="w",
                           command=lambda: self._sort_by("session"))
        self._tree.heading("score_val", text=_SCORE_SHORT, anchor="e",
                           command=lambda: self._sort_by("score"))
        self._tree.heading("delta", text="贡献Δ", anchor="e",
                           command=lambda: self._sort_by("delta"))
        self._tree.heading("tier",   text="等级", anchor="center",
                           command=lambda: self._sort_by("tier"))
        self._tree.column("rank",     width=40,  minwidth=30,  stretch=False, anchor="center")
        self._tree.column("session",  width=140, minwidth=80,  stretch=True,  anchor="w")
        self._tree.column("score_val", width=70,  minwidth=50,  stretch=False, anchor="e")
        self._tree.column("delta",    width=64,  minwidth=48,  stretch=False, anchor="e")
        self._tree.column("tier",    width=70,  minwidth=50,  stretch=False, anchor="center")

        sb = ttk.Scrollbar(tree_sec.content, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")  # 常驻细条：先占右侧
        self._tree.pack(fill="both", expand=True)

        # Mousewheel on enter/leave (same pattern as project/session columns)
        self._unbind_wheel = None
        self._tree.bind("<Enter>", self._on_tree_enter)
        self._tree.bind("<Leave>", self._on_tree_leave)

        # Tier → tag color（键名 tier_<名>，与 theme.GRADE_HEX 同源色）
        for _tname, _thex in theme.GRADE_HEX.items():
            self._tree.tag_configure(f"tier_{_tname}", foreground=_thex)

        # 默认（项目视角）显示贡献Δ列；会话视角切到名次榜时隐藏。
        self._apply_displaycolumns()

        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # -- Decompose panel (ScrollFrame with group headers) --
        decomp_sf = ScrollFrame(decomp_frame, bg=theme.BG)
        decomp_sf.canvas.pack(fill="both", expand=True)
        self._decomp_inner = decomp_sf.inner
        self._draw_decompose()

    # -- public API -----------------------------------------------------------

    def update(self, reports, aggregate=None) -> None:
        self._aggregate = aggregate
        scored = [r for r in reports if r.score is not None]
        scored.sort(key=lambda r: r.score, reverse=True)
        # 无综合效率分（如 no_loc 或会话无净增行/成本）时回退按 TCER 排名。
        self._fallback_tcer = False
        if not scored:
            by_tcer = [r for r in reports if r.tcer is not None]
            if by_tcer:
                self._fallback_tcer = True
                by_tcer.sort(key=lambda r: r.tcer, reverse=True)
                scored = by_tcer

        def _label(r):
            return r.meta.title or r.meta.session_id or r.meta.path.stem

        if self._fallback_tcer:
            self._ranking = [(_label(r), r.tcer, "", r) for r in scored]
        else:
            self._ranking = [(_label(r), r.score, r.tier or "", r) for r in scored]
        self._tree.heading("score_val",
                           text="TCER" if self._fallback_tcer else _SCORE_SHORT)
        if getattr(self, "_fallback_note", None) is None:
            self._fallback_note = SelectableLabel(
                self._note_parent,
                text="ℹ 会话缺少综合效率分（无净增行或成本数据）——当前按 TCER 排名。",
                bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI_SMALL,
                padx=theme.PAD_M, pady=theme.PAD_XS)
        if self._fallback_tcer:
            self._fallback_note.pack(fill="x", before=self._paned_ref)
        else:
            self._fallback_note.pack_forget()
        self._avg_factors = score_decompose_avg(reports)
        # 参与均值的已评分会话数：仅 1 个时「与项目均值对比」退化为自我对比
        # （均值==自身），无信息量 → 该区块自动隐藏（见 _build_avg_section）。
        self._scored_count = sum(1 for r in reports if r.score is not None)
        self._reports = list(reports)  # 供项目视角渲染
        self._current_report = None
        self._grade_filter = None
        self._apply_displaycolumns()
        self._apply_layout()
        self._rebuild_tree()
        self._draw_grade_bar()
        self._draw_decompose()

    def _apply_displaycolumns(self) -> None:
        """Treeview 列集：两视角都用名次榜（#/标题/效率分/等级），不再显示贡献Δ列。
        （贡献归因榜已按产品要求移除。）"""
        self._tree.configure(displaycolumns=("rank", "session", "score_val", "tier"))
        self._tree_sec.set_title("会话排名")

    def set_view_mode(self, mode: str, report=None) -> None:
        """由控制器按视角切换驱动（对齐指标分类/模型模型对比）。

        视角 = 分析单元的切换，不是「选没选行」：
        - session 视角：左表为名次榜（定位当前会话），右栏 = 该会话构成拆解 + 会话洞察。
        - project 视角：左表为贡献归因榜（每行标注该会话把项目分拉高↑/拉低↓多少，
          按贡献Δ排序），右栏 = 项目聚合分/等级/三轴/离散度 + 项目级系统性洞察。
        """
        self._view_mode = "session" if (mode == "session" and report is not None) else "project"
        if self._view_mode == "session":
            self._current_report = report
            iid = str(id(report))
            self._suppress_select = True
            try:
                if self._tree.exists(iid):
                    self._tree.selection_set(iid)
                    self._tree.see(iid)
            finally:
                self._suppress_select = False
        else:
            self._current_report = None
            self._suppress_select = True
            try:
                self._tree.selection_remove(*self._tree.selection())
            finally:
                self._suppress_select = False
        # 视角变了 → 刷新列集 + 按视角调左栏显隐/宽度。
        self._apply_displaycolumns()
        self._apply_layout()
        self._rebuild_tree()
        self._draw_decompose()

    def _apply_layout(self) -> None:
        """按视角控制左栏（排名表）显隐：
        - 项目视角：整栏隐藏，右侧项目画像独占全宽（排名表在此无意义）。
        - 会话视角：显示排名表并收窄（仅作会话定位导航）。
        """
        session = self._view_mode == "session"
        try:
            self._paned_ref.paneconfigure(self._table_frame, hide=not session)
        except tk.TclError:
            pass
        # 评级分布随排名表一起显隐（项目视角隐藏排名表 → 评级分布也无意义）。
        try:
            if session:
                self._grade_sec.frame.pack(fill="x", before=self._paned_ref)
            else:
                self._grade_sec.frame.pack_forget()
        except tk.TclError:
            pass
        if not session:
            return

        # 会话视角：布局就绪后把左栏设窄。
        target = self._sash_session

        def _place():
            try:
                if self._paned_ref.winfo_width() > target + 40:
                    self._paned_ref.sash_place(0, target, 1)
            except tk.TclError:
                pass
        self._paned_ref.after_idle(_place)

    # -- grade bar ------------------------------------------------------------

    def _draw_grade_bar(self) -> None:
        c = self._grade_canvas
        c.delete("all")
        self._grade_rects.clear()
        w = c.winfo_width()
        if w < 10:
            return

        grades_in_order = [label for label, _ in metrics.SCORE_TIER_BANDS]
        counts = {g: 0 for g in grades_in_order}
        for _, _, g, _ in self._ranking:
            if g in counts:
                counts[g] += 1
        total = sum(counts.values()) or 1

        x = 2
        bar_h = 22
        y0 = 8
        for g in grades_in_order:
            n = counts[g]
            if n == 0 and self._grade_filter != g:
                continue
            seg_w = max(28, int((n / total) * (w - 10)))
            if x + seg_w > w - 2:
                seg_w = w - 2 - x
            fill = theme.GRADE_HEX.get(g, theme.MUTED)
            if self._grade_filter and self._grade_filter != g:
                fill = theme.GRADE_DIM
            c.create_rectangle(x, y0, x + seg_w, y0 + bar_h,
                               fill=fill, outline=theme.BG, width=1)
            if seg_w > 36:
                c.create_text(x + seg_w / 2, y0 + bar_h / 2,
                              text=f"{g} {n}", fill=theme.FG_WHITE,
                              font=theme.FONT_UI_SMALL, anchor="center")
            self._grade_rects.append((x, y0, x + seg_w, y0 + bar_h, g))
            x += seg_w + 2

    def _on_grade_click(self, event) -> None:
        for x0, y0, x1, y1, g in self._grade_rects:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self._grade_filter = None if self._grade_filter == g else g
                self._rebuild_tree()
                self._draw_grade_bar()
                self._draw_decompose()
                return

    # -- Treeview -------------------------------------------------------------

    def _rebuild_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        items = [(l, c, g, r) for l, c, g, r in self._ranking
                 if not self._grade_filter or g == self._grade_filter]
        # 贡献Δ = 会话分 − 项目聚合分（仅项目视角、非 TCER 回退时可算）。
        agg_score = getattr(self._aggregate, "score", None)
        show_delta = (self._view_mode == "project" and not self._fallback_tcer
                      and agg_score is not None)

        def _delta(r):
            return (r.score - agg_score) if (show_delta and r.score is not None) else None

        # Apply sort. Index into (label, score, tier, report) tuple.
        col_map = {"rank": 1, "session": 0, "score": 1, "tier": 2}
        if self._sort_col == "delta":
            # 贡献榜默认排序：最拖累（Δ 最负）排最前；无 Δ 的沉底。
            items.sort(key=lambda t: (_delta(t[3]) is None,
                                      _delta(t[3]) if _delta(t[3]) is not None else 0.0),
                       reverse=self._sort_reverse)
        elif self._sort_col in col_map:
            idx = col_map[self._sort_col]
            items.sort(key=lambda t: t[idx], reverse=self._sort_reverse)
        # 回退到 TCER 排名时值列用 TCER 格式，否则用综合效率分格式。
        val_key = "tcer" if self._fallback_tcer else "score"
        for rank, (label, val, tier, report) in enumerate(items, 1):
            tag = f"tier_{tier}" if tier else ""
            d = _delta(report)
            d_txt = "—" if d is None else f"{d:+.1f}"
            self._tree.insert("", "end",
                              values=(rank, label, format_value(val_key, val), d_txt, tier),
                              tags=(tag,),
                              iid=str(id(report)))
        # Restore selection if report still visible（程序化选中，勿触发翻转回调）
        if self._current_report:
            iid = str(id(self._current_report))
            if self._tree.exists(iid):
                self._suppress_select = True
                try:
                    self._tree.selection_set(iid)
                    self._tree.see(iid)
                finally:
                    self._suppress_select = False

    def _on_tree_select(self, _event=None) -> None:
        if self._suppress_select:
            return  # 程序化选中（排序重建/视角同步），非用户点击，不翻转视角
        sel = self._tree.selection()
        if not sel:
            return
        iid = int(sel[0])
        for label, val, tier, report in self._ranking:
            if id(report) == iid:
                # 选中未变（Tk 的 <<TreeviewSelect>> 是 idle 队列异步投递，程序化
                # selection_set 后 _suppress_select 已在 finally 复位，延迟到达的
                # 事件会漏过守卫）→ 直接返回，避免重复 _draw_decompose 的 destroy 递归。
                if report is self._current_report:
                    return
                # 点行 = 仅选中该会话，不翻转视角（视角只由左上角分段控件切）。
                # 会话视角下 → 右栏刷新为该会话构成；项目视角下 → 纯导航高亮，
                # 右栏保持项目概览不变。
                self._current_report = report
                if self._view_mode == "session":
                    self._draw_decompose()
                # 通知控制器同步选中的 sid（on_select_session 按当前 view_mode
                # 决定是否刷新会话相关面板；不会强行翻转视角）。
                if self._controller is not None:
                    sid = report.meta.session_id or report.meta.path.stem
                    self._controller.on_select_session(sid)
                return

    def _on_tree_enter(self, _event=None) -> None:
        from .platform import bind_mousewheel
        self._unbind_wheel = bind_mousewheel(
            self._tree, lambda units: self._tree.yview_scroll(units, "units"))

    def _on_tree_leave(self, _event=None) -> None:
        if self._unbind_wheel:
            self._unbind_wheel()
            self._unbind_wheel = None

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            # 综合效率分默认降序；贡献Δ 默认升序（最拖累/最负排最前）。
            self._sort_reverse = (col == "score")
        self._rebuild_tree()

    # -- Decompose panel (ScrollFrame with group headers) ----------------------

    def _draw_decompose(self) -> None:
        for w in self._decomp_inner.winfo_children():
            w.destroy()

        # 项目视角：右栏是项目这个实体的画像（聚合分/等级/三轴+离散度 + 系统性洞察），
        # 而不是「会话视角的空态」。会话视角：选中会话的构成拆解 + 会话洞察。
        report = self._current_report
        if self._view_mode == "project" or report is None:
            self._draw_project_overview()
            return

        axes = score_decompose(report)
        if axes is None:
            tk.Label(self._decomp_inner, text=f"该会话无{_SCORE_NAME}数据",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI,
                     pady=40).pack()
            return

        self._build_summary_card(report)
        self._build_factor_section(axes, report)
        self._build_avg_section(axes)
        self._build_insights_section(report)

    # -- 项目视角右栏：项目实体画像 -------------------------------------------
    def _draw_project_overview(self) -> None:
        """项目视角右栏：项目聚合分/等级 + 三轴（含跨会话离散度）+ 项目级系统性洞察。

        这是「项目视角」的主答案——项目整体处在什么水平、是什么把分数拉高/拉低，
        对齐会话视角的「会话构成」结构（概览 → 构成 → 洞察）。
        """
        reports = getattr(self, "_reports", None) or []
        self._build_project_summary_card()
        agg_axes = score_decompose(self._aggregate) if self._aggregate is not None else None
        if agg_axes is not None:
            self._build_project_factor_section(agg_axes, reports)
        # 项目级系统性洞察（≥40% 会话复现的 drag / ≥60% 的 good）。
        sec = CollapsibleSection(self._decomp_inner, "洞察与意见 (项目)",
                                 theme.GROUP_COLORS["G6"], expand=True)
        wrap = tk.Frame(sec.content, bg=theme.PANEL, padx=6, pady=4)
        wrap.pack(fill="x", pady=(0, 1))
        self._render_insight_items(wrap, project_insights(reports))
        # 可复制的 CLAUDE.md 规则建议（仅当有系统性短板时出现）。
        self._build_claude_md_section(reports)
        # 值得一试的功能实践 + 前瞻工作流（仿 /insights Features to Try·On the Horizon）。
        self._build_feature_section(reports)
        self._build_horizon_section(reports)
        # 活动概览放最下方（确定性会话画像，参考性质，非主答案）。
        self._build_activity_overview(reports)

    def _build_activity_overview(self, reports) -> None:
        """活动概览：确定性会话画像（任务类型/工具/时段/规模/总量）。
        对标 /insights 的 What You Wanted·Top Tools·Session Types 等可量化部分。"""
        if not reports:
            return
        ov = activity_overview(reports)
        sec = CollapsibleSection(self._decomp_inner, "活动概览",
                                 theme.GROUP_COLORS["G2"], expand=False)
        box = tk.Frame(sec.content, bg=theme.PANEL, padx=10, pady=6)
        box.pack(fill="x", pady=(0, 1))

        # 总量一行
        SelectableLabel(box, text=f"{ov.n_sessions} 个会话 · 净增 {ov.total_net_loc:,} 行 · "
                        f"{ov.total_tool_calls:,} 次工具调用",
                        bg=theme.PANEL, fg=theme.FG, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", pady=(0, 4))

        def _dist_row(label, pairs, fmt=lambda k, v: f"{k} {v}"):
            if not pairs:
                return
            tk.Label(box, text=label, bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL, anchor="w").pack(fill="x", pady=(4, 0))
            SelectableLabel(box, text="  ·  ".join(fmt(k, v) for k, v in pairs),
                            bg=theme.PANEL, fg=theme.FG, font=theme.FONT_UI_SMALL,
                            justify="left").pack(fill="x")

        _dist_row("任务类型", ov.task_type_dist)
        _dist_row("最常用工具", ov.top_tools)
        _dist_row("活跃时段", ov.time_of_day)
        _dist_row("会话规模", ov.size_dist)

    def _build_claude_md_section(self, reports) -> None:
        """可复制的 CLAUDE.md 规则建议：把系统性短板转成可粘贴规则 + 复制按钮。
        对标 /insights 的 Suggested CLAUDE.md Additions。无系统性短板则不显示。"""
        suggestions = claude_md_suggestions(reports)
        if not suggestions:
            return
        sec = CollapsibleSection(self._decomp_inner, "建议加进 CLAUDE.md",
                                 theme.GROUP_COLORS["G6"], expand=False)
        for s in suggestions:
            card = tk.Frame(sec.content, bg=theme.PANEL, padx=8, pady=6)
            card.pack(fill="x", pady=(0, 1))
            # 证据行（为什么建议）
            SelectableLabel(card, text=s.evidence, bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL, justify="left").pack(fill="x")
            # 规则文本（可选中复制）
            SelectableLabel(card, text=s.rule, bg=theme.CONTROL_BG, fg=theme.FG,
                            font=theme.FONT_UI_SMALL, justify="left",
                            padx=6, pady=4).pack(fill="x", pady=(2, 2))

    def _build_reco_section(self, title: str, recos) -> None:
        """渲染一组 Recommendation（Features/Horizon 共用）：标题 + 为什么 +
        可粘贴 prompt + 复制按钮。无内容则不显示。"""
        if not recos:
            return
        sec = CollapsibleSection(self._decomp_inner, title,
                                 theme.GROUP_COLORS["G2"], expand=False)
        for rc in recos:
            card = tk.Frame(sec.content, bg=theme.PANEL, padx=8, pady=6)
            card.pack(fill="x", pady=(0, 1))
            SelectableLabel(card, text=f"\u25b8 {rc.title}", bg=theme.PANEL, fg=theme.FG,
                            font=theme.FONT_UI_BOLD, justify="left").pack(fill="x")
            SelectableLabel(card, text=rc.why, bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL, justify="left").pack(fill="x")
            if rc.prompt:
                SelectableLabel(card, text=rc.prompt, bg=theme.CONTROL_BG, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, justify="left",
                                padx=6, pady=4).pack(fill="x", pady=(2, 2))

    def _build_feature_section(self, reports) -> None:
        """值得一试：针对检测到的摩擦推荐可上手实践 + 可粘贴 prompt。"""
        self._build_reco_section("值得一试的用法", feature_suggestions(reports))

    def _build_horizon_section(self, reports) -> None:
        """前瞻工作流：把当前用法升级为更自动/并行的形态。"""
        self._build_reco_section("进阶工作流（前瞻）", horizon_suggestions(reports))

    def _build_project_summary_card(self) -> None:
        """项目主体卡：项目聚合分 + 等级 + 评分覆盖率 + 会话数。项目视角缺失已久的主答案。"""
        sec = CollapsibleSection(self._decomp_inner, "项目概览",
                                 theme.GROUP_COLORS["G6"], expand=True)
        card = tk.Frame(sec.content, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x", pady=(0, 1))

        agg = self._aggregate
        n_total = len(getattr(self, "_reports", None) or [])
        n_scored = getattr(self, "_scored_count", 0)
        agg_score = getattr(agg, "score", None)
        agg_tier = getattr(agg, "tier", None) or ""

        if agg_score is None:
            SelectableLabel(card, text="项目暂无综合效率分（会话缺净增行或成本数据）",
                            bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                            justify="left").pack(fill="x")
            return

        row = tk.Frame(card, bg=theme.PANEL)
        row.pack(fill="x")
        name_lbl = tk.Label(row, text=f"项目{_SCORE_NAME}", bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        name_lbl.pack(side="left")
        if _SCORE_TIP:
            Tooltip(name_lbl, _SCORE_TIP)
        tk.Label(row, text=format_value("score", agg_score), bg=theme.PANEL,
                 fg=theme.GRADE_HEX.get(agg_tier, theme.FG),
                 font=("Consolas", 16, "bold")).pack(side="left", padx=(4, 8))
        if agg_tier:
            tk.Label(row, text=agg_tier, bg=theme.GRADE_HEX.get(agg_tier, theme.MUTED),
                     fg=theme.FG_WHITE, font=theme.FONT_UI_SMALL_BOLD,
                     padx=6, pady=1).pack(side="left", padx=(0, 8))
        # 评分覆盖率：多少会话真正参与了评分（无产出轴的会话不计分）。
        tk.Label(row, text=f"评分覆盖 {n_scored}/{n_total}", bg=theme.PANEL,
                 fg=theme.MUTED, font=theme.FONT_UI).pack(side="right")

        SelectableLabel(card, text="＝ 全项目聚合的产出/成本/质量三轴加权（分子和÷分母和口径）",
                        bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", pady=(2, 0))
        if getattr(agg, "tcer", None) is not None:
            tk.Label(card, text=f"项目 TCER {agg.tcer:.1f} 行/百万", bg=theme.PANEL,
                     fg=theme.FG, font=theme.FONT_UI_SMALL, anchor="e").pack(anchor="e")

    def _build_project_factor_section(self, agg_axes, reports) -> None:
        """项目三轴构成：聚合轴值 + 会话离散度（min–median–max 须线），
        一眼看出哪条轴是短板、以及会话间是否分化严重。"""
        sec = CollapsibleSection(self._decomp_inner, "得分构成（三轴 · 含会话离散度）",
                                 theme.GROUP_COLORS["G2"], expand=True)
        grid = tk.Frame(sec.content, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        # 各轴收集所有已评分会话的分值，算 min/median/max。
        per_axis: dict[str, list[float]] = {a.key: [] for a in SCORE_AXES}
        for r in reports:
            d = score_decompose(r)
            if d is None:
                continue
            for k in per_axis:
                v = d.get(k)
                if v is not None:
                    per_axis[k].append(v)

        weights = metrics.SCORE_WEIGHTS
        for axis in SCORE_AXES:
            val = agg_axes.get(axis.key, 0.0)
            vals = sorted(per_axis.get(axis.key) or [])
            wt = weights.get(axis.key)
            wt_txt = f"权重{wt:.0%}" if wt is not None else ""
            axis_tip = (f"{axis.name}（{axis.formula}）\n{axis.tip}" if axis.tip else None)

            row = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=4)
            row.pack(fill="x")
            name_lbl = tk.Label(row, text=axis.name, bg=theme.PANEL, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, width=8, anchor="w",
                                cursor=CLICK_CURSOR)
            name_lbl.pack(side="left")
            color = theme.VALUE_GOOD if val >= SCORE_AXIS_NEUTRAL else theme.VALUE_BAD
            val_lbl = tk.Label(row, text=format_axis(val), bg=theme.PANEL, fg=color,
                               font=theme.FONT_VALUE, width=5, anchor="e")
            val_lbl.pack(side="left", padx=4)
            if axis_tip:
                Tooltip(name_lbl, axis_tip)
                Tooltip(val_lbl, axis_tip)

            # Bar 底 + min–max 须线 + 聚合值标记 + 中性参考线 0.5。
            # pack_propagate(False)：固定高度，防止只含 .place 子件的帧被压扁。
            bar_bg = tk.Frame(row, bg=theme.CONTROL_BG, height=10, width=120)
            bar_bg.pack(side="left", fill="x", expand=True, padx=4)
            bar_bg.pack_propagate(False)
            if vals:
                lo, hi = min(1.0, max(0.0, vals[0])), min(1.0, max(0.0, vals[-1]))
                if hi > lo:
                    tk.Frame(bar_bg, bg=theme.AXIS_SPREAD).place(
                        relx=lo, rely=0, relwidth=hi - lo, relheight=1.0)
            tk.Frame(bar_bg, bg=color, width=3).place(
                relx=min(1.0, max(0.0, val)), rely=0, relheight=1.0, anchor="n")
            tk.Frame(bar_bg, bg=theme.BAR_TICK, width=1).place(
                relx=0.5, rely=0, relheight=1.0)

            # 离散度文字：min–median–max（会话间分化提示）
            if len(vals) >= 2:
                med = vals[len(vals) // 2]
                spread = f"{format_axis(vals[0])}–{format_axis(med)}–{format_axis(vals[-1])}"
            else:
                spread = wt_txt
            tk.Label(row, text=spread, bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(side="left", padx=4)

        prod_frame = tk.Frame(sec.content, bg=theme.PANEL, padx=10, pady=6)
        prod_frame.pack(fill="x", pady=(0, 1))
        tk.Label(prod_frame, text="加权合成 =", bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI).pack(side="left")
        agg = self._aggregate
        tk.Label(prod_frame,
                 text=f"项目{_SCORE_NAME}  {format_value('score', getattr(agg, 'score', None))}",
                 bg=theme.PANEL,
                 fg=theme.GRADE_HEX.get(getattr(agg, "tier", None) or "", theme.FG),
                 font=theme.FONT_VALUE).pack(side="left", padx=4)

    def _build_summary_card(self, report) -> None:
        """Summary card: 综合效率分 + tier + rank, matching group header style."""
        sec = CollapsibleSection(self._decomp_inner, f"{_SCORE_NAME}概览",
                                 theme.GROUP_COLORS["G6"], expand=False)
        card = tk.Frame(sec.content, bg=theme.PANEL, padx=10, pady=8)
        card.pack(fill="x", pady=(0, 1))

        sid = report.meta.session_id or report.meta.path.stem
        SelectableLabel(card, text=sid[:40], bg=theme.PANEL, fg=theme.ACCENT,
                        font=theme.FONT_MONO, justify="left").pack(fill="x")

        # 综合效率分 + grade + rank row
        row = tk.Frame(card, bg=theme.PANEL)
        row.pack(fill="x", pady=(4, 0))

        score_val = report.score
        tier_ = report.tier or ""
        name_lbl = tk.Label(row, text=_SCORE_NAME, bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        name_lbl.pack(side="left")
        if _SCORE_TIP:
            Tooltip(name_lbl, _SCORE_TIP)
        val_lbl = tk.Label(row, text=format_value("score", score_val), bg=theme.PANEL,
                           fg=theme.GRADE_HEX.get(tier_, theme.FG),
                           font=("Consolas", 16, "bold"))
        val_lbl.pack(side="left", padx=(4, 8))
        if _SCORE_TIP:
            Tooltip(val_lbl, _SCORE_TIP)

        if tier_:
            badge = tk.Label(row, text=tier_, bg=theme.GRADE_HEX.get(tier_, theme.MUTED),
                             fg=theme.FG_WHITE, font=theme.FONT_UI_SMALL_BOLD, padx=6, pady=1)
            badge.pack(side="left", padx=(0, 8))

        # Rank
        for i, (l, cv, g, r) in enumerate(self._ranking):
            if r is report:
                total = len(self._ranking)
                tk.Label(row, text=f"排名 {i + 1}/{total}", bg=theme.PANEL,
                         fg=theme.MUTED, font=theme.FONT_UI).pack(side="right")
                break

        # 一句话解释：0–100 分怎么来的（三条正交轴加权，去术语门槛）。
        SelectableLabel(card, text="＝ 产出效率 · 成本 · 质量 三轴各比参考线，按会话规模收缩后加权",
                        bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                        justify="left").pack(fill="x", pady=(2, 0))

        # TCER
        if report.tcer is not None:
            tk.Label(card, text=f"TCER {report.tcer:.1f} 行/百万", bg=theme.PANEL,
                     fg=theme.FG, font=theme.FONT_UI_SMALL, anchor="e").pack(anchor="e")

    def _build_factor_section(self, axes, report) -> None:
        """Axis bars: the 3 orthogonal axes that make up 综合效率分.

        名称/公式/解释全部取自指标 SSOT（metric_defs.SCORE_AXES）。每轴 ∈[0,1]，
        0.5 = 与参考线持平（半饱和中性点）；悬停每行显示白话解释。
        """
        sec = CollapsibleSection(self._decomp_inner, "得分构成（三轴加权）",
                                 theme.GROUP_COLORS["G2"], expand=False)
        grid = tk.Frame(sec.content, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        weights = metrics.SCORE_WEIGHTS
        # Axis rows
        for axis in SCORE_AXES:
            val = axes.get(axis.key, 0.0)
            name, desc = axis.name, axis.formula
            wt = weights.get(axis.key)
            wt_txt = f"权重{wt:.0%}" if wt is not None else ""

            row = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=4)
            row.pack(fill="x")

            # Label + value（悬停名称/数值即见白话解释）
            axis_tip = f"{name}（{desc}）\n{axis.tip}" if axis.tip else None
            name_lbl = tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, width=8, anchor="w",
                                cursor=CLICK_CURSOR)
            name_lbl.pack(side="left")
            color = theme.VALUE_GOOD if val >= SCORE_AXIS_NEUTRAL else theme.VALUE_BAD
            val_lbl = tk.Label(row, text=format_axis(val), bg=theme.PANEL, fg=color,
                               font=theme.FONT_VALUE, width=5, anchor="e")
            val_lbl.pack(side="left", padx=4)
            if axis_tip:
                Tooltip(name_lbl, axis_tip)
                Tooltip(val_lbl, axis_tip)

            # Bar（0–1 满刻度；中点 0.5 = 与参考线持平）
            # pack_propagate(False)：固定高度，防止只含 .place 子件的帧被压扁。
            bar_bg = tk.Frame(row, bg=theme.CONTROL_BG, height=10, width=120)
            bar_bg.pack(side="left", fill="x", expand=True, padx=4)
            bar_bg.pack_propagate(False)
            bar_w = min(1.0, max(0.0, val))
            if bar_w > 0:
                tk.Frame(bar_bg, bg=color).place(
                    relx=0, rely=0, relwidth=bar_w, relheight=1.0)
            # 参考线 0.5（与基准持平）
            tk.Frame(bar_bg, bg=theme.BAR_TICK, width=1).place(
                    relx=0.5, rely=0, relheight=1.0)

            # Weight (short, muted)
            wt_lbl = tk.Label(row, text=wt_txt, bg=theme.PANEL, fg=theme.MUTED,
                              font=theme.FONT_UI_SMALL)
            wt_lbl.pack(side="left", padx=4)
            if axis_tip:
                Tooltip(wt_lbl, axis_tip)

        # Weighted-sum line — three axes blend into the final 综合效率分 (0–100).
        prod_frame = tk.Frame(sec.content, bg=theme.PANEL, padx=10, pady=6)
        prod_frame.pack(fill="x", pady=(0, 1))
        tk.Label(prod_frame, text="加权合成 =", bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI).pack(side="left")
        tk.Label(prod_frame, text=f"{_SCORE_NAME}  {format_value('score', report.score)}",
                 bg=theme.PANEL,
                 fg=theme.GRADE_HEX.get(report.tier or "", theme.FG),
                 font=theme.FONT_VALUE).pack(side="left", padx=4)

    def _build_avg_section(self, axes) -> None:
        """本会话三轴 vs 项目均值：显示「均值」与带符号差值 Δ（本会话 − 均值）。

        只在有 ≥2 个已评分会话时才有意义——单会话时均值==自身，三个数会与
        「得分构成」完全重复，故该区块隐藏（改提示一行）。
        """
        avg = self._avg_factors
        if avg is None:
            return
        if getattr(self, "_scored_count", 0) < 2:
            sec = CollapsibleSection(self._decomp_inner, "与项目均值对比",
                                     theme.GROUP_COLORS["G2"], expand=False)
            SelectableLabel(sec.content, text="仅 1 个已评分会话，暂无可对比的项目均值。",
                            bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                            padx=10, pady=6, justify="left").pack(fill="x", pady=(0, 1))
            return

        sec = CollapsibleSection(self._decomp_inner, "与项目均值对比",
                                 theme.GROUP_COLORS["G2"], expand=False)
        grid = tk.Frame(sec.content, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        for axis in SCORE_AXES:
            name = axis.name
            sel_val = axes.get(axis.key, 0.0)
            avg_val = avg.get(axis.key, 0.0)
            delta = sel_val - avg_val

            row = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=3)
            row.pack(fill="x")

            tk.Label(row, text=name, bg=theme.PANEL, fg=theme.FG,
                     font=theme.FONT_UI_SMALL, width=8, anchor="w").pack(side="left")

            # 项目均值（基准列，muted）
            tk.Label(row, text=f"均值 {format_axis(avg_val)}", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL, width=10, anchor="w").pack(side="left", padx=2)

            # 带符号差值 Δ = 本会话 − 均值（高于均值=绿↑，低于=红↓，持平=灰）
            if abs(delta) < 5e-3:
                arrow, dcolor = "≈", theme.MUTED
            elif delta > 0:
                arrow, dcolor = "▲", theme.VALUE_GOOD
            else:
                arrow, dcolor = "▼", theme.VALUE_BAD
            tk.Label(row, text=f"{arrow} {delta:+.2f}", bg=theme.PANEL, fg=dcolor,
                     font=theme.FONT_VALUE, width=8, anchor="e").pack(side="right")

    # -- 洞察与意见（可执行诊断，仿 Claude Code /insights + /doctor）------------
    _INSIGHT_STYLE = {
        # kind -> (章节标题, 前景色, 行首标记)
        "good": ("亮点", theme.VALUE_GOOD, "\u2713"),
        "drag": ("拖累项", theme.VALUE_BAD, "!"),
        "cost": ("金额", theme.VIEW_PROJECT, "\uffe5"),  # 橙黄 ¥ 标记：花钱相关
        "tip": ("快速改进", theme.ACCENT, "\u2192"),
    }
    _INSIGHT_ORDER = ("good", "drag", "cost", "tip")

    def _render_insight_items(self, parent, items) -> None:
        """\u628a\u4e00\u7ec4 Insight \u6309 \u4eae\u70b9/\u62d6\u7d2f\u9879/\u5feb\u901f\u6539\u8fdb \u5206\u7ec4\u6e32\u67d3\u5230 parent\u3002

        \u6bcf\u7ec4\u4e00\u4e2a\u53ef\u70b9\u51fb\u6298\u53e0\u7684\u5c0f\u6807\u9898\uff08\u25bc/\u25b6 + \u540d\u79f0 + \u8ba1\u6570\uff09\uff1b\u4eae\u70b9\uff08good\uff09
        \u9ed8\u8ba4\u6298\u53e0\uff08\u5148\u770b\u95ee\u9898\u3001\u518d\u770b\u8868\u626c\uff09\u3002\u6298\u53e0\u6001\u5b58 self._insight_collapsed\uff0c\u8de8\u9009\u4e2d\u4fdd\u6301\u3002
        good/drag/tip \u5171\u7528\u540c\u4e00\u6e32\u67d3\uff08\u5355\u4f1a\u8bdd\u4e0e\u9879\u76ee\u7ea7\u90fd\u8d70\u8fd9\u91cc\uff09\u3002
        """
        collapsed = getattr(self, "_insight_collapsed", None)
        if collapsed is None:
            collapsed = self._insight_collapsed = {"good": True}  # \u4eae\u70b9\u9ed8\u8ba4\u6298\u53e0
        by_kind = {"good": [], "drag": [], "cost": [], "tip": []}
        for it in items:
            by_kind.get(it.kind, by_kind["tip"]).append(it)
        rendered = False
        for kind in self._INSIGHT_ORDER:
            group = by_kind.get(kind) or []
            if not group:
                continue
            rendered = True
            head_txt, color, mark = self._INSIGHT_STYLE[kind]
            is_collapsed = collapsed.get(kind, False)

            # \u5206\u7ec4\u6807\u9898\uff08\u53ef\u70b9\u51fb\u6298\u53e0\uff09\uff1a\u25bc/\u25b6 + \u540d\u79f0\uff08N\uff09
            header = tk.Frame(parent, bg=theme.PANEL, cursor=CLICK_CURSOR)
            header.pack(fill="x", pady=(8, 2))
            arrow = "\u25b6" if is_collapsed else "\u25bc"
            head_lbl = tk.Label(header, text=f"{arrow} {head_txt}\uff08{len(group)}\uff09",
                                bg=theme.PANEL, fg=color, font=theme.FONT_UI_BOLD,
                                anchor="w")
            head_lbl.pack(side="left", fill="x", expand=True)

            # \u6b63\u6587\u5bb9\u5668\uff08\u6298\u53e0\u65f6 pack_forget\uff09\uff1b\u5de6\u4fa7\u8272\u6761 + \u7f29\u8fdb
            body = tk.Frame(parent, bg=theme.PANEL)
            for it in group:
                row = tk.Frame(body, bg=theme.PANEL)
                row.pack(fill="x", pady=(2, 3))
                # \u5de6\u4fa7\u5f69\u8272\u7ad6\u6761\uff08\u6309 kind \u4e0a\u8272\uff09
                tk.Frame(row, bg=color, width=3).pack(side="left", fill="y")
                body_col = tk.Frame(row, bg=theme.PANEL)
                body_col.pack(side="left", fill="x", expand=True, padx=(8, 0))
                # \u6807\u9898\u884c\uff1a\u6807\u8bb0 + \u7ed3\u8bba
                SelectableLabel(body_col, text=f"{mark} {it.title}", bg=theme.PANEL,
                                fg=color, font=theme.FONT_UI,
                                justify="left").pack(fill="x")
                if it.evidence:
                    SelectableLabel(body_col, text=it.evidence, bg=theme.PANEL,
                                    fg=theme.MUTED, font=theme.FONT_UI,
                                    justify="left").pack(fill="x", padx=(14, 0))
                if it.action:
                    SelectableLabel(body_col, text=f"\u2192 {it.action}", bg=theme.PANEL,
                                    fg=theme.FG, font=theme.FONT_UI,
                                    justify="left").pack(fill="x", padx=(14, 0))
            # body 必须锚定在自己 header 的正下方（after=header）。否则 pack 会把它
            # 追加到 parent 末尾——折叠再展开某组后，其正文会跳到整个面板最底部、
            # 脱离所属标题（金额组尤其明显，因其后还有「快速改进」组）。
            if not is_collapsed:
                body.pack(fill="x", after=header)

            def _toggle(_e=None, k=kind, b=body, hd=header, hl=head_lbl, ht=head_txt,
                        n=len(group), col=color):
                now = not self._insight_collapsed.get(k, False)
                self._insight_collapsed[k] = now
                arr = "\u25b6" if now else "\u25bc"
                hl.config(text=f"{arr} {ht}\uff08{n}\uff09")
                if now:
                    b.pack_forget()
                else:
                    b.pack(fill="x", after=hd)
            header.bind("<Button-1>", _toggle)
            head_lbl.bind("<Button-1>", _toggle)
        if not rendered:
            tk.Label(parent, text="\u6682\u65e0\u53ef\u6267\u884c\u6d1e\u5bdf\u3002",
                     bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI,
                     anchor="w").pack(fill="x")

    def _build_insights_section(self, report) -> None:
        """会话视角「洞察与意见」：把 core.insights 的诊断分组渲染，让用户知道具体改什么。"""
        sec = CollapsibleSection(self._decomp_inner, "\u6d1e\u5bdf\u4e0e\u610f\u89c1 (\u4f1a\u8bdd)",
                                 theme.GROUP_COLORS["G6"], expand=True)
        wrap = tk.Frame(sec.content, bg=theme.PANEL, padx=6, pady=4)
        wrap.pack(fill="x", pady=(0, 1))
        self._render_insight_items(wrap, session_insights(report))


# 图表组件已拆分至 charts.py；从这里 re-export 保持既有 import 路径可用。
from .charts import (  # noqa: F401
    DashboardChart, HeatmapChart, MetricTrendSelector, ScatterChart, TrendChart,
)

# ============================================================
# 模型对比 (Apple-style, matching MetricPanel layout)
# ============================================================

class ModelCompareView:
    """模型对比 — per-model stats in group/grid layout matching MetricPanel style."""

    _COL_COLORS = ["#569cd6", "#4ec9b0", "#dcdcaa", "#ce9178", "#9cdcfe", "#c586c0"]

    def __init__(self, parent, controller=None):
        self.parent = parent
        self._models: list = []
        self._groups: list[_GroupState] = []
        # 分组折叠状态（跨 update 保持）；「代码质量与行为」(M_QUAL) 默认折叠。
        self._group_collapsed: dict[str, bool] = {"M_QUAL": True}

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self._container = sf.inner

    def update(self, reports) -> None:
        from tcer.core.metrics import compare_models
        self._models = compare_models(reports)
        # Rebuild entire grid
        for w in self._container.winfo_children():
            w.destroy()
        if not self._models:
            tk.Label(self._container, text="无模型数据", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_UI, pady=40).pack()
            return
        self._build_header()
        # Per-model metric groups now come from the SSOT (metric_defs.MODEL_GROUPS):
        # labels, formatting, tooltips and 好坏方向 all live there, shared with the
        # other tabs' metric metadata.
        for group in MODEL_GROUPS:
            self._build_group(group)

    def _build_header(self) -> None:
        """Cost distribution bar + model summary, matching group header style."""
        # Group header with title
        header = tk.Frame(self._container, bg=theme.GROUP_COLORS["G_NEUTRAL"], padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ 模型对比", bg=theme.GROUP_COLORS["G_NEUTRAL"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        # Cost distribution bar
        total_cost = sum(mc.cost for mc in self._models)
        if total_cost > 0:
            bar = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
            bar.pack(fill="x")
            canvas = tk.Canvas(bar, bg=theme.PANEL, height=20, highlightthickness=0)
            canvas.pack(fill="x")

            def draw_bar(_e=None):
                canvas.delete("all")
                w = canvas.winfo_width()
                if w < 10:
                    return
                # Draw colored segments
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    color = self._COL_COLORS[i % len(self._COL_COLORS)]
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    canvas.create_rectangle(x1, 0, x2, 20, fill=color, outline="")
                    rx += rw
                # Draw model names on top (always visible)
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    cx = (x1 + x2) / 2
                    if x2 - x1 > 20:
                        canvas.create_text(cx, 10, text=mc.display_name,
                                           fill=theme.BG,
                                           font=(theme.FONT_MONO_NAME, 7))
                    rx += rw

            canvas.bind("<Configure>", draw_bar)
            canvas.after(10, draw_bar)

        # Summary grid: model names + cost + sessions
        grid = tk.Frame(self._container, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))
        for j, mc in enumerate(self._models):
            color = self._COL_COLORS[j % len(self._COL_COLORS)]
            cell = tk.Frame(grid, bg=theme.PANEL, padx=6, pady=2)
            cell.grid(row=0, column=j, sticky="nsew", padx=2)
            name_lbl = tk.Label(cell, text=mc.display_name, bg=theme.PANEL, fg=color,
                                font=theme.FONT_VALUE, anchor="w")
            name_lbl.pack(anchor="w")
            cost_str = model_display(mc, "m_cost")
            sub_lbl = tk.Label(cell, text=f"{cost_str} · {mc.session_count} 会话",
                               bg=theme.PANEL, fg=theme.MUTED,
                               font=theme.FONT_UI_SMALL, anchor="w")
            sub_lbl.pack(anchor="w")
            price_tip = _model_price_tip(mc)
            for w in (cell, name_lbl, sub_lbl):
                Tooltip(w, price_tip)
        for j in range(len(self._models)):
            grid.grid_columnconfigure(j, weight=1)

    def _build_group(self, group) -> None:
        """Build one per-model metric group from a metric_defs.Group (SSOT)."""
        collapsed = self._group_collapsed.get(group.id, False)
        gframe = tk.Frame(self._container, bg=theme.BG)
        gframe.pack(fill="x", pady=(1, 0))
        header = tk.Frame(gframe, bg=theme.GROUP_COLORS["G2"], padx=6, pady=3)
        header.pack(fill="x")
        arrow_lbl = tk.Label(header, text=f"{'▶' if collapsed else '▼'} {group.name}",
                             bg=theme.GROUP_COLORS["G2"], fg=theme.FG,
                             font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor=CLICK_CURSOR)
        arrow_lbl.pack(side="left")
        body = tk.Frame(gframe, bg=theme.BG)
        body.pack(fill="x")

        grid = tk.Frame(body, bg=theme.PANEL, padx=4, pady=4)
        grid.pack(fill="x", pady=(0, 1))

        # Column headers (model names)
        tk.Label(grid, text="", bg=theme.PANEL, width=14).grid(row=0, column=0)
        for j, mc in enumerate(self._models):
            color = self._COL_COLORS[j % len(self._COL_COLORS)]
            tk.Label(grid, text=mc.display_name, bg=theme.PANEL, fg=color,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="e").grid(
                         row=0, column=j + 1, sticky="e", padx=2)

        # Metric rows — name / value / tooltip / 好坏方向 all come from the SSOT.
        for i, metric in enumerate(group.metrics):
            key = metric.key
            tip_text = model_tip(key)

            name_lbl = tk.Label(grid, text=metric.name, bg=theme.PANEL, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, anchor="w")
            name_lbl.grid(row=i + 1, column=0, sticky="w")
            if tip_text:
                Tooltip(name_lbl, tip_text)

            # Gold-highlight the best value in this row. metric.sentiment follows
            # the metric's 词性: "up"=越大越好, "down"=越小越好. Skipped for metrics
            # with no good/bad direction, or when all models tie.
            row_colors: dict[int, str] = {}
            if metric.sentiment in ("up", "down"):
                valid = [(j, model_raw(mc, key)) for j, mc in enumerate(self._models)]
                valid = [(j, v) for j, v in valid if isinstance(v, (int, float))]
                distinct = {v for _, v in valid}
                if len(distinct) >= 2:
                    target = max(distinct) if metric.sentiment == "up" else min(distinct)
                    for j, v in valid:
                        if v == target:
                            row_colors[j] = theme.VALUE_BEST

            for j, mc in enumerate(self._models):
                val = model_display(mc, key)
                lbl = tk.Label(grid, text=val, bg=theme.PANEL,
                               fg=row_colors.get(j, theme.VALUE_NEUTRAL),
                               font=theme.FONT_VALUE, anchor="e")
                lbl.grid(row=i + 1, column=j + 1, sticky="e", padx=2)
                if tip_text:
                    Tooltip(lbl, tip_text)

        # Make columns expandable
        for j in range(len(self._models) + 1):
            grid.grid_columnconfigure(j, weight=1)

        gs = _GroupState(name=group.name, arrow=arrow_lbl, body=body, collapsed=collapsed)
        self._groups.append(gs)
        for w in (header, arrow_lbl):
            w.bind("<Button-1>", lambda e, s=gs, gid=group.id: self._toggle_group(s, gid))
        if collapsed:
            body.pack_forget()

    def _toggle_group(self, gs, gid) -> None:
        """点击分组标题：折叠/展开整组（状态记入 self._group_collapsed，跨 update 保持）。"""
        gs.collapsed = not gs.collapsed
        self._group_collapsed[gid] = gs.collapsed
        gs.arrow.config(text=f"{'▶' if gs.collapsed else '▼'} {gs.name}")
        if gs.collapsed:
            gs.body.pack_forget()
        else:
            gs.body.pack(fill="x")


def _model_price_tip(mc) -> str:
    """Tooltip text: a model's full list price (the four $/MTok billing rates).

    Rates come from ``pricing.resolve`` — the same table used to cost the
    session — so the card shows exactly what each dimension was charged at.
    Unknown models fall back to the Anthropic default list price, which is
    called out in the header so the user doesn't mistake it for the model's
    own official price.
    """
    from tcer.core import pricing

    def _rate(x: float) -> str:
        return f"${f'{x:.4f}'.rstrip('0').rstrip('.')}/百万"

    r = pricing.resolve(mc.model_id)
    known = pricing.table_key(mc.model_id) is not None
    title = "官方标价" if known else "默认配置价（未在价表中）"
    note = "" if known else "\n⚠️ 该模型未在价表中，按 Anthropic 通用 list 价回退，非其厂商官方价。"
    # 价表条目备注（_note）：多轨价（促销/峰时/Batch）、分段计费、别名跟随等，
    # 与四个展示单价同源，悬浮可见，让用户知道这套价取的是哪一轨。
    extra = pricing.note_for(mc.model_id)
    if extra:
        note += f"\nℹ️ {extra}"
    return (
        f"{mc.display_name} · {title}（$/百万 Token）\n"
        f"输入　　　{_rate(r['input'])}\n"
        f"输出　　　{_rate(r['output'])}\n"
        f"缓存创建　{_rate(r['cache_write'])}\n"
        f"缓存命中　{_rate(r['cache_read'])}{note}"
    )






class RealProjectsView:
    """项目聚合页签 — 卡片式：真实项目卡 + 各 agent 品牌图标条 + 可展开明细。

    数据来自 ``analyze.real_projects``（规范化 cwd 分组）+ 逐 ref 分析的聚合
    报告。每张卡：agent 图标条（悬停见来源名，图标优先于文字标注）+ 项目
    路径 + 汇总行 + 评级；点卡展开各源明细行。排序经工具栏下拉菜单。视图
    自身无分析状态，首次切入由控制器后台扫描（mtime 缓存），「刷新」强制重扫。
    """

    _SORTS = [
        ("n", "总会话数"), ("cost", "总成本"), ("tokens", "总 Token"),
        ("net", "总净增行"), ("display", "名称"),
    ]
    _SORT_LABEL = dict(_SORTS)
    # 明细网格列头一律取指标 SSOT 名（中心指标守则，不自造相似名——
    # 「效率分」≠「综合效率分」这类偏差就是这么来的）；TCER 按约定保留缩写。
    _TOKENS_NAME = metric_name("total_tokens")   # 总 Token
    _COST_NAME = metric_name("cost")             # 总成本
    _CPE_NAME = metric_name("cpe")               # 千行代码成本
    _CHR_NAME = metric_name("chr")               # 缓存命中率
    _TURNS_NAME = metric_name("turns")           # 请求数
    _MSGS_NAME = metric_name("user_msgs")        # 用户消息
    _NET_NAME = metric_name("net_loc")           # 净增行
    _SCORE_COL = f"项目{metric_name('score')}"   # 项目综合效率分（聚合级，
                                                 # 与效率榜项目视角同名同派生）
    # 列头口径前缀（用户约定，优先于「SSOT 全名」守则）：求和列冠「总」、
    # 比率列冠「平均」（实为按合计重算 ≠ 会话算术平均——5k 与 2M token 的
    # 会话等权平均会触发 Simpson 悖论；SSOT 全名与公式保留在表头悬浮里）。
    _COL_TURNS = f"总{metric_name('turns')}"     # 总请求数
    _COL_MSGS = f"总{metric_name('user_msgs')}"  # 总用户消息
    _COL_NET = f"总{metric_name('net_loc')}"     # 总净增行
    _COL_CPE = f"平均{metric_name('cpe')}"       # 平均千行代码成本
    _COL_TCER = "平均TCER"
    _COL_CHR = f"平均{metric_name('chr')}"       # 平均缓存命中率

    def __init__(self, parent, controller=None) -> None:
        self.controller = controller
        self._rows: list[dict] = []
        self._sort_col = "n"
        self._expanded: set[str] = set()

        head = tk.Frame(parent, bg=theme.PANEL)
        head.pack(fill="x", padx=theme.PAD_M, pady=(theme.PAD_S, 0))
        _hi = ui_icon(head, "layers")
        if _hi is not None:
            tk.Label(head, image=_hi, bg=theme.PANEL).pack(side="left")
        tk.Label(head, text="项目聚合", bg=theme.PANEL, fg=theme.FG,
                 font=theme.FONT_UI_BOLD).pack(side="left", padx=(theme.PAD_S, 0))
        tk.Label(head,
                 text="同一工作目录的各 agent 项目卡自动合并（父子路径不合并）",
                 bg=theme.PANEL, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=theme.PAD_M)
        self._sort_btn = flat_button(head, "排序：总会话数", self._pop_sort,
                                     image=ui_icon(head, "rank"),
                                     compound="left")
        self._sort_btn.pack(side="right")
        flat_button(head, "刷新", self._refresh,
                    image=ui_icon(head, "refresh"), compound="left").pack(
                        side="right", padx=(0, theme.PAD_S))

        self._hint = tk.Label(parent, text="首次进入自动扫描全部项目…",
                              bg=theme.PANEL, fg=theme.MUTED,
                              font=theme.FONT_UI, pady=24)
        sf = ScrollFrame(parent, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True,
                       padx=theme.PAD_M, pady=(theme.PAD_S, theme.PAD_M))
        self._sf = sf
        self._container = sf.inner

    # -- controller hooks -------------------------------------------------
    def on_show(self) -> None:
        """页签切入：未加载过则请控制器启动后台扫描（已加载/扫描中为 no-op）。"""
        if self.controller is not None and not self._rows:
            self.controller.real_projects_scan()

    def _refresh(self) -> None:
        if self.controller is not None:
            self.controller.real_projects_scan(force=True)

    def _pop_sort(self) -> None:
        menu = FlatMenu(self._sort_btn)
        for key, label in self._SORTS:
            mark = "✓ " if key == self._sort_col else ""
            menu.add_command(label=f"{mark}{label}",
                             command=lambda k=key: self.set_sort(k))
        self._sort_btn.update_idletasks()
        menu.tk_popup(self._sort_btn.winfo_rootx(),
                      self._sort_btn.winfo_rooty() + self._sort_btn.winfo_height())

    def set_sort(self, key: str) -> None:
        if key not in self._SORT_LABEL:
            return
        self._sort_col = key
        self._sort_btn.config(text=f"排序：{self._SORT_LABEL[key]}")
        self._render()

    # -- data -------------------------------------------------------------
    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows
        self._expanded &= {g["key"] for g in rows}
        self._hint.pack_forget()
        if not rows:
            self._hint.config(text="没有可聚合的项目（各数据源均无会话）。")
            self._hint.pack(expand=True)
            return
        self._render()

    def _render(self) -> None:
        for w in self._container.winfo_children():
            w.destroy()
        for g in self._ordered():
            self._make_card(g)
        self._sf.update_scroll(reset=True)

    def _ordered(self) -> list[dict]:
        key = self._sort_col
        if key == "display":
            return sorted(self._rows, key=lambda g: g["display"].lower())

        def rk(g):
            v = g["totals"].get(key)
            return v if isinstance(v, (int, float)) else 0.0

        return sorted(self._rows, key=lambda g: (-rk(g), g["display"].lower()))

    def _make_card(self, g: dict) -> None:
        t = g["totals"]
        expanded = g["key"] in self._expanded

        def _toggle(_card, _key=g["key"]):
            if _key in self._expanded:
                self._expanded.discard(_key)
            else:
                self._expanded.add(_key)
            self._render()

        card = Card(self._container, on_click=_toggle, padx=1, pady=1)

        # 第 1 行：项目路径（盘符统一大写，来自 _display_cwd）+ 右侧成本金额
        # （与会话卡片同款：$两位小数、方向着色——成本>0 红、$0 绿、FONT_VALUE）。
        row1 = tk.Frame(card.frame, bg=theme.PANEL_2)
        row1.pack(fill="x", padx=theme.PAD_S, pady=(theme.PAD_S, 0))
        arrow = tk.Label(row1, text="▾" if expanded else "▸", bg=theme.PANEL_2,
                         fg=theme.MUTED, font=theme.FONT_UI_SMALL)
        arrow.pack(side="left", padx=(2, 4))
        card.bind_to(arrow)
        cost = t.get("cost") or 0.0
        cost_fg = theme.VALUE_BAD if cost > 0 else theme.VALUE_GOOD
        cost_lbl = tk.Label(row1, bg=theme.PANEL_2, fg=cost_fg,
                            font=theme.FONT_VALUE, anchor="e",
                            text=f"${cost:.2f}")
        cost_lbl.pack(side="right", padx=(4, 0))
        card.bind_to(cost_lbl)
        name_lbl = tk.Label(row1, text=g["display"], bg=theme.PANEL_2,
                            fg=theme.FG, font=theme.FONT_UI_BOLD, anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)
        card.bind_to(name_lbl)

        # 第 2 行：agent 品牌图标条（悬停见来源名；图标取代文字标注——
        # 与项目卡同款 source_icon，Claude 自定义根自动用 ccswitch 图标）。
        row2 = tk.Frame(card.frame, bg=theme.PANEL_2)
        row2.pack(fill="x", padx=theme.PAD_S, pady=(2, 0))
        for r in g["refs"]:
            self._icon_chip(row2, card, r)

        # 第 3 行：摘要（会话/请求/用户消息/Token，空格分隔；效率与成本细节在展开明细里）。
        stat = tk.Label(
            card.frame, bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL, anchor="w",
            text=(f"{t.get('n', 0)} 会话  {fmt.fmt_int(t.get('requests'))} 请求  "
                  f"{fmt.fmt_int(t.get('user_msgs'))} 用户消息  "
                  f"{self._tok(t.get('tokens'))} Token"))
        stat.pack(fill="x", padx=theme.PAD_S,
                  pady=(2, theme.PAD_S if not expanded else 0))
        card.bind_to(stat)
        Tooltip(stat, "会话数 / 请求数（向模型 API 的请求；Grok 按回合内调用次数，"
                      "其余源按助手响应数）/ 用户消息数 / 消耗总 Token")

        if expanded:
            # 各源明细 = 固定列网格（每指标一列 + 表头），跨行严格对齐——
            # 此前每行一条右对齐长文本，数值宽度不同导致列天然不齐，无法对比。
            detail = tk.Frame(card.frame, bg=theme.PANEL)
            detail.pack(fill="x", padx=theme.PAD_S, pady=(2, theme.PAD_S))
            # (列名, 最小宽, 对齐, 口径键, 悬浮说明)；来源列 weight=1 拉伸，
            # 数值列定宽右对齐。列序按语义分组：活动量(会话/请求/消息) →
            # 消耗(Token/成本/千行成本) → 产出(净增行/TCER) → 缓存 → 总分。
            # 列名口径前缀见类常量注释；SSOT 全名与公式在悬浮里。
            cols = [
                ("来源", 130, "w", "src", "该工作目录下此 agent 的项目卡"),
                ("总会话", 50, "e", "n", "跨会话求和"),
                (self._COL_TURNS, 62, "e", "turns",
                 "跨会话求和（Grok 按 API 调用数，其余按助手响应数）"),
                (self._COL_MSGS, 76, "e", "msgs", "跨会话求和"),
                (self._TOKENS_NAME, 68, "e", "tokens", "跨会话求和"),
                (self._COST_NAME, 74, "e", "cost", "跨会话求和（价表计价）"),
                (self._COL_CPE, 100, "e", "cpe",
                 "总成本 ÷ 总净增行 × 1000（按合计重算，非会话算术平均）"),
                (self._COL_NET, 64, "e", "net", "跨会话求和"),
                (self._COL_TCER, 62, "e", "tcer",
                 "总净增行 ÷ 总 Token（按合计重算，非会话算术平均）"),
                (self._COL_CHR, 92, "e", "chr",
                 "缓存读 ÷ 总输入（按合计重算，非会话算术平均）"),
                (self._SCORE_COL, 94, "e", "score",
                 "项目级评分：从该源聚合轴输入重算（非会话平均）"),
            ]
            for ci, (_name, minw, _anchor, _key, _tip) in enumerate(cols):
                detail.columnconfigure(ci, minsize=minw,
                                       weight=1 if ci == 0 else 0)
            for ci, (name, _minw, anchor, _key, tip) in enumerate(cols):
                hdr = tk.Label(detail, text=name, bg=theme.PANEL, fg=theme.MUTED,
                               font=theme.FONT_UI_SMALL_BOLD, anchor=anchor)
                hdr.grid(row=0, column=ci, sticky="ew", padx=2, pady=(0, 1))
                Tooltip(hdr, tip)
            for ri, r in enumerate(g["refs"], start=1):
                cell0 = tk.Frame(detail, bg=theme.PANEL)
                cell0.grid(row=ri, column=0, sticky="ew", padx=2, pady=1)
                icon = source_icon(cell0, self._icon_key(r))
                if icon is not None:
                    il = tk.Label(cell0, image=icon, bg=theme.PANEL)
                    il.pack(side="left", padx=(0, 4))
                    Tooltip(il, r["label"])
                tk.Label(cell0, text=r["label"], bg=theme.PANEL, fg=theme.FG,
                         font=theme.FONT_UI_SMALL, anchor="w"
                         ).pack(side="left")
                score_txt = (f"{self._f(r.get('score'))} {r.get('tier') or '-'}"
                             if r.get("score") is not None else "-")
                values = [
                    fmt.fmt_int(r.get("n")), fmt.fmt_int(r.get("requests")),
                    fmt.fmt_int(r.get("user_msgs")), self._tok(r.get("tokens")),
                    fmt.fmt_money(r.get("cost")), self._usd(r.get("cpe")),
                    fmt.fmt_int(r.get("net")), self._f(r.get("tcer")),
                    fmt.fmt_pct(r.get("chr")), score_txt,
                ]
                for ci, (txt, (_name, _minw, anchor, _key, _tip)) in enumerate(
                        zip(values, cols[1:]), start=1):
                    tk.Label(detail, text=txt, bg=theme.PANEL, fg=theme.MUTED,
                             font=theme.FONT_UI_SMALL, anchor=anchor
                             ).grid(row=ri, column=ci, sticky="e", padx=2, pady=1)

    def _icon_chip(self, parent, card, r: dict) -> None:
        """卡片头的 agent 图标（16px，与项目卡同款）；无资源回退小字来源名。"""
        icon = source_icon(parent, self._icon_key(r))
        if icon is not None:
            il = tk.Label(parent, image=icon, bg=theme.PANEL_2)
            il.pack(side="left", padx=1)
            Tooltip(il, r["label"])
            card.bind_to(il)
        else:
            sl = tk.Label(parent, text=f"[{r['label']}]", bg=theme.PANEL_2,
                          fg=theme.MUTED, font=theme.FONT_UI_SMALL)
            sl.pack(side="left", padx=1)
            card.bind_to(sl)

    @staticmethod
    def _icon_key(r: dict) -> str:
        return r.get("icon") or r.get("source") or "claude"

    @staticmethod
    def _tok(v) -> str:
        """Token 大数中文量级（21.6亿），不足万位回落千分位整数。"""
        if v is None:
            return "-"
        approx = fmt.fmt_approx_cn(v)
        return approx.replace("≈ ", "") if approx else fmt.fmt_int(v)

    @staticmethod
    def _f(v, spec="0.0"):
        return "-" if v is None else fmt.fmt_float(v, spec)

    @staticmethod
    def _usd(v):
        return "-" if v is None else f"${v:.1f}"


class PhasePortraitWidget:
    """相空间收敛动力学相图（专属 dynamics 类型报告，其余报告自动隐藏）。

    横轴：语义偏离距离 Ds（0.0 狄拉克目标点 ← 1.0 初始偏离态）
    纵轴：回合推进进度
    要素：左下角高亮狄拉克目标点、右侧平庸代码吸引子引力漏斗、关键回合轨迹粒子与推进矢量箭头。
    """
    def __init__(self, parent) -> None:
        from .charts import _ChartTooltip, _aa_layer
        self._aa_layer = _aa_layer
        self.container = tk.Frame(parent, bg=theme.PANEL_2, highlightthickness=1,
                                  highlightbackground=theme.BORDER)
        self.head = tk.Frame(self.container, bg=theme.CARD_HEADER_BG, padx=10, pady=5)
        self.head.pack(fill="x")

        left_h = tk.Frame(self.head, bg=theme.CARD_HEADER_BG)
        left_h.pack(side="left")
        tk.Label(left_h, text="相空间收敛动力学相图", bg=theme.CARD_HEADER_BG,
                 fg=theme.FG_WHITE, font=theme.FONT_UI_BOLD).pack(side="left")
        self.state_badge = tk.Label(left_h, text="", bg=theme.CARD_HEADER_BG,
                                    font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.state_badge.pack(side="left", padx=(8, 0))
        self.cap_lbl = tk.Label(self.head, text="")  # 兼容测试与标量文本读取

        self.caps_frame = tk.Frame(self.head, bg=theme.CARD_HEADER_BG)
        self.caps_frame.pack(side="right")

        self.canvas = tk.Canvas(self.container, bg=theme.BG, height=240,
                                highlightthickness=0, cursor=CLICK_CURSOR)
        self.canvas.pack(fill="x", padx=4, pady=4)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _e: self._tooltip.hide())
        self.canvas.bind("<Destroy>", lambda _e: self._tooltip.hide())
        self._data: dict = {}
        self._report: dict = {}
        self._pts: list = []
        self._aa_imgs: list = []  # 抗锯齿 PhotoImage 引用防 GC
        self._tgt_pos: tuple[float, float] | None = None
        self._att_pos: tuple[float, float] | None = None
        self._pad_t: int = 34
    def render(self, dynamics_data: dict, report: dict) -> None:
        self._data = dynamics_data or {}
        self._report = report or {}
        # 1. 态势徽章（终态感知，自洽反映吸引子逃逸与向心突破）
        ctype = str(self._data.get("convergence_type") or "").lower()
        is_trapped = bool(self._data.get("attractor_trapped"))
        traj = self._data.get("trajectory") or []
        last_pt = traj[-1] if traj else {}
        last_evt = str(last_pt.get("event") or "").lower()
        last_vec = str(last_pt.get("vector") or "").lower()
        last_ds = float(last_pt.get("semantic_distance", 0.5)) if last_pt else 0.5

        # 检查是否达成逃逸或向心突破：
        # (A) 显式标为 escaped/breakthrough
        # (B) 末点带有 breakthrough 事件
        # (C) 虽曾受困但末尾向心大幅推进且偏离显著消减 (Ds <= 0.45 且 vector 推进)
        is_escaped = (
            ctype in ("escaped", "breakthrough")
            or last_evt == "breakthrough"
            or (is_trapped and last_vec in ("positive", "convergent") and last_ds <= 0.45)
        )

        if ctype == "dirac" or (last_ds <= 0.15 and not is_trapped):
            self.state_badge.config(text="[狄拉克向心收敛]", fg=theme.SUCCESS, bg=theme.DIRAC_CORE_BG)
        elif is_escaped:
            self.state_badge.config(text="[吸引子逃逸 / 向心突破]", fg=theme.SUCCESS, bg=theme.DIRAC_CORE_BG)
        elif ctype == "trapped" or is_trapped:
            self.state_badge.config(text="[平庸吸引子捕获]", fg=theme.ERROR, bg=theme.ERROR_TINT_BG)
        else:
            self.state_badge.config(text="[高熵漫游未收敛]", fg=theme.WARNING, bg=theme.WARN_TINT_BG)

        # 2. 三能力胶囊条（显示名称、分数与评级 Tooltip）
        for w in self.caps_frame.winfo_children():
            w.destroy()
        caps = self._data.get("capabilities") or {}
        if isinstance(caps, dict) and caps:
            items = [
                ("意图降熵", "意图降熵力", caps.get("intent_formalization"),
                 "衡量首轮需求形式化与边界把控能力。\n高分代表约束严谨清晰，前置消减不确定性；低分代表需求模糊宽泛。"),
                ("偏离敏锐", "偏离感知敏锐度", caps.get("drift_sensitivity"),
                 "衡量对代码架构违背与局部死修的嗅觉。\n高分代表敏锐察觉偏离并主动挂起；低分代表盲目打补丁或侵入底层资产。"),
                ("反馈收敛", "反馈收敛效率", caps.get("feedback_mutual_info"),
                 "衡量纠偏指令的互信息密度与介入时机。\n高分代表反馈精准向心制导；低分代表盲目试探或止损严重滞后。"),
            ]
            for short_name, full_name, score, desc in items:
                if score is None:
                    continue
                try:
                    s_val = int(score)
                except (ValueError, TypeError):
                    s_val = 50
                if s_val >= 65:
                    col = theme.SUCCESS
                    tier_desc = "优秀 / 敏锐向心"
                elif s_val < 40:
                    col = theme.ERROR
                    tier_desc = "严重受困 / 偏离失控"
                else:
                    col = theme.WARNING
                    tier_desc = "迟缓中等 / 震荡游走"

                chip = tk.Frame(self.caps_frame, bg=theme.PANEL_2, highlightthickness=1,
                                highlightbackground=theme.BORDER, padx=6, pady=2)
                chip.pack(side="left", padx=3)
                l_name = tk.Label(chip, text=f"{short_name} ", bg=theme.PANEL_2, fg=theme.MUTED,
                                  font=theme.FONT_UI_SMALL)
                l_name.pack(side="left")
                l_score = tk.Label(chip, text=str(score), bg=theme.PANEL_2, fg=col,
                                   font=theme.FONT_UI_SMALL_BOLD)
                l_score.pack(side="left")

                tip_text = (
                    f"{full_name}：{score} 分（{tier_desc}）\n"
                    f"{desc}\n"
                    "评分参考：≥65 敏锐向心 · 40-64 迟缓游走 · <40 严重受困"
                )
                tip = Tooltip(chip, tip_text)
                tip.bind_widget(l_name)
                tip.bind_widget(l_score)

            self.cap_lbl.config(
                text=f"意图降熵力 {caps.get('intent_formalization', '-')} · "
                     f"偏离敏锐度 {caps.get('drift_sensitivity', '-')} · "
                     f"反馈收敛效率 {caps.get('feedback_mutual_info', '-')}")
        else:
            self.cap_lbl.config(text="")
        self._redraw()

    def pack(self, **kw):
        self.container.pack(**kw)

    def pack_forget(self):
        self.container.pack_forget()
        self._tooltip.hide()

    def _on_motion(self, event) -> None:
        if not self._pts:
            self._tooltip.hide()
            return
        best_pt = None
        min_d2 = 24 * 24  # 扩大圆心检测半径至 24px (原 16px 过于严苛)
        for item in self._pts:
            px, py, pt = item[0], item[1], item[2]
            offset_y = item[3] if len(item) > 3 else -14
            # 1. 质点圆心欧氏距离
            d2 = (px - event.x) ** 2 + (py - event.y) ** 2
            # 2. 文本标签包围盒区域检测 (覆盖 T{turn} 与 ({event}) 文本)
            tx = px
            ty = py + offset_y
            text_hit = abs(event.x - tx) <= 30 and abs(event.y - ty) <= 18
            if text_hit:
                min_d2 = 0
                best_pt = (px, py, pt)
                break
            elif d2 < min_d2:
                min_d2 = d2
                best_pt = (px, py, pt)
        if best_pt:
            _, _, pt = best_pt[:3]
            t = pt.get("turn", "-")
            ds = pt.get("semantic_distance", 0.5)
            vec = str(pt.get("vector") or "neutral").lower()
            event_tag = str(pt.get("event") or "normal").lower()
            note = pt.get("note", "")
            status_map = {
                "positive": ("向心推进 (做正功/逼近目标)", theme.SUCCESS),
                "convergent": ("向心推进 (做正功/逼近目标)", theme.SUCCESS),
                "negative": ("离心发散 (偏离目标)", theme.ERROR),
                "divergent": ("严重发散 (偏离意图)", theme.ERROR),
                "trapped": ("死锁陷阱 (被平庸吸引子捕获)", theme.ERROR),
                "neutral": ("中性微调 / 震荡游走", theme.WARNING),
            }
            event_map = {
                "retry_loop": "连续重试死循环",
                "test_fail": "测试/环境报错干扰",
                "compaction": "上下文窗口压缩",
                "breakthrough": "向心关键突破",
            }
            status_str, status_col = status_map.get(vec, ("状态未定", theme.MUTED))
            lines = [
                f"回合 T{t} · 代码偏离度: {ds:.2f}",
                f"推进矢量: {status_str}",
            ]
            colors = [theme.FG_WHITE, status_col]
            if event_tag in event_map:
                lines.append(f"动力学事件: {event_map[event_tag]}")
                colors.append(theme.WARNING if event_tag != "breakthrough" else theme.SUCCESS)
            if note:
                lines.append(f"说明: {note}")
                colors.append(theme.MUTED)
            self._tooltip.show(event.x, event.y, lines, colors)
            return

        # 2. 检测狄拉克目标点 C_expert (外层势阱圆或标签文字)
        if self._tgt_pos:
            tgt_x, tgt_y = self._tgt_pos
            d_circle = ((tgt_x - event.x) ** 2 + (tgt_y - event.y) ** 2) ** 0.5
            text_hit = (tgt_x + 15 <= event.x <= tgt_x + 190) and abs(event.y - tgt_y) <= 18
            if d_circle <= 28 or text_hit:
                lines = [
                    "狄拉克目标点 · C_expert",
                    "状态特征: 零熵理想代码态 · 目标偏离 0.00",
                    "动力学定义: 完美契合业务意图的向心终点",
                    "工程含义: 逻辑精炼紧凑，无防御性样板与多余面条代码",
                ]
                colors = [theme.SUCCESS, theme.FG_WHITE, theme.MUTED, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        # 3. 检测平庸代码吸引子 P(C_mediocre) (核心同心圆或标题文字)
        if self._att_pos:
            att_x, att_y = self._att_pos
            d_circle = ((att_x - event.x) ** 2 + (att_y - event.y) ** 2) ** 0.5
            ty_mid = getattr(self, "_pad_t", 34) + 11
            text_hit = abs(event.x - att_x) <= 110 and abs(event.y - ty_mid) <= 18
            if d_circle <= 54 or text_hit:
                lines = [
                    "平庸代码吸引子 · P(C_mediocre)",
                    "状态特征: 高熵先验势阱 · 偏离危险区 (Ds ≈ 0.86)",
                    "动力学定义: 预训练面条代码的强大惯性黑洞",
                    "工程含义: 机械打补丁、过度封装、局部重试死锁",
                ]
                colors = [theme.ERROR, theme.FG_WHITE, theme.MUTED, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        # 均未命中
        self._tooltip.hide()

    @staticmethod
    def _get_arrowhead_poly(x0: float, y0: float, x1: float, y1: float,
                            length: float = 10, half_width: float = 5,
                            setback: float = 6) -> list[tuple[float, float]]:
        import math
        dx = x1 - x0
        dy = y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            return []
        ux = dx / dist
        uy = dy / dist
        tip_x = x1 - ux * setback
        tip_y = y1 - uy * setback
        bx = tip_x - ux * length
        by = tip_y - uy * length
        lx = bx - uy * half_width
        ly = by + ux * half_width
        rx = bx + uy * half_width
        ry = by - ux * half_width
        return [(tip_x, tip_y), (lx, ly), (rx, ry)]

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        self._pts.clear()
        self._aa_imgs.clear()
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 100:
            w = max(w, c.winfo_reqwidth(), 600)
        if h < 50:
            h = max(h, c.winfo_reqheight(), 240)
        pad_l, pad_r, pad_t, pad_b = 65, 45, 34, 30
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 10 or plot_h <= 10:
            return

        # 1. 物理成本 Y 轴刻度与横向虚线背景 (Layer 0: 最底层 Canvas 原生虚线)
        cost_str = str(self._report.get("cost_display") or "")
        m_cost = re.search(r"(\d+(?:\.\d+)?)", cost_str)
        max_cost_val = float(m_cost.group(1)) if m_cost else 10.0
        if max_cost_val <= 0:
            max_cost_val = 10.0
        y_ticks = []
        for frac, val in ((1.0, max_cost_val), (0.5, max_cost_val * 0.5), (0.0, 0.0)):
            gy = pad_t + (1.0 - frac) * plot_h
            if frac > 0:
                c.create_line(pad_l, gy, pad_l + plot_w, gy, fill=theme.BORDER, dash=(2, 4))
            val_txt = f"${val:.1f}" if max_cost_val >= 1 else f"${val:.2f}"
            y_ticks.append((gy, val_txt))

        # 关键状态分界线（收敛目标域推至最左侧 0.10，高熵危险域推至最右侧 0.90）
        line_target_x = pad_l + 0.10 * plot_w
        line_danger_x = pad_l + 0.90 * plot_w
        c.create_line(line_target_x, pad_t, line_target_x, pad_t + plot_h, fill=theme.PHASE_GRID_DIRAC, dash=(1, 4))
        c.create_line(line_danger_x, pad_t, line_danger_x, pad_t + plot_h, fill=theme.PHASE_GRID_TRAP, dash=(1, 4))

        # X 轴底线
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill=theme.BORDER, width=1)

        # 2. 收集抗锯齿图层 items (Layer 1: PIL 2× 超采样高清图层，线/多边形/圆点纯图元)
        aa_items: list = []

        # (A) 相平面速度流场微线段 (Phase Streamlines)：向心微导向场
        streamline_col = theme.PHASE_STREAMLINE
        for row_idx, gy_frac in enumerate((0.25, 0.55, 0.85)):
            sy = pad_t + gy_frac * plot_h
            for col_idx, gx_frac in enumerate((0.35, 0.52, 0.68)):
                sx = pad_l + gx_frac * plot_w
                aa_items.append(("line", [(sx + 8, sy - 4), (sx - 8, sy + 3)], streamline_col, 1))
                aa_items.append(("line", [(sx - 8, sy + 3), (sx - 4, sy + 1)], streamline_col, 1))

        # (B) 理想向心收敛走廊参考线 (Convergence Corridor)
        corridor_pts = [
            (pad_l + 0.85 * plot_w, pad_t + plot_h * 0.95),
            (pad_l + 0.50 * plot_w, pad_t + plot_h * 0.92),
            (pad_l + 0.22 * plot_w, pad_t + plot_h * 0.90),
            (pad_l + 0.05 * plot_w, pad_t + plot_h * 0.88),
        ]
        aa_items.append(("line", corridor_pts, theme.PHASE_CORRIDOR, 1))

        # (C) 平庸代码吸引子引力势阱与黑洞同心圆
        att_x = pad_l + plot_w * 0.86
        att_y = pad_t + 62
        self._att_pos = (att_x, att_y)
        self._pad_t = pad_t
        # 外层吸引盆 (Basin of Attraction) 等势圈
        aa_items.append(("dot", att_x, att_y, 52, None, theme.ATTRACTOR_BASIN_BORDER, 1))
        # 核心吸引子同心圆
        for r, col in zip((36, 24, 12), theme.ATTRACTOR_RINGS):
            aa_items.append(("dot", att_x, att_y, r, col, theme.ATTRACTOR_RINGS[1], 1))
        aa_items.append(("dot", att_x, att_y, 4, theme.ERROR, theme.ERROR, 1))

        # (D) 狄拉克目标点（外层低能势阱 + 双层发光圆）
        tgt_x = pad_l + plot_w * 0.05
        tgt_y = pad_t + plot_h * 0.88
        self._tgt_pos = (tgt_x, tgt_y)
        aa_items.append(("dot", tgt_x, tgt_y, 26, None, theme.DIRAC_WELL_BORDER, 1))
        aa_items.append(("dot", tgt_x, tgt_y, 16, theme.DIRAC_CORE_BG, theme.SUCCESS, 2))
        aa_items.append(("dot", tgt_x, tgt_y, 5, theme.SUCCESS, theme.SUCCESS, 1))
        # (E) 计算真实会话动力学轨迹节点（严格物理坐标映射与稳健兜底）
        traj = self._data.get("trajectory") or []
        n = len(traj)
        total_turns = self._report.get("turns")
        if not isinstance(total_turns, (int, float)) or total_turns <= 1:
            valid_turns = [pt.get("turn") for pt in traj if isinstance(pt.get("turn"), (int, float))]
            total_turns = max(valid_turns, default=1)
        total_turns = max(1, int(total_turns))

        if traj:
            for idx, pt in enumerate(traj):
                ds = max(0.0, min(1.0, float(pt.get("semantic_distance", 0.5))))
                px = pad_l + ds * plot_w
                t_val = pt.get("turn")
                if isinstance(t_val, (int, float)) and total_turns > 1:
                    frac = max(0.0, min(1.0, (float(t_val) - 1.0) / (float(total_turns) - 1.0)))
                else:
                    frac = idx / (n - 1) if n > 1 else 0.5
                py = (pad_t + plot_h) - frac * plot_h * 0.82 - 8
                offset_y = -14 if (idx % 2 == 0 and py > pad_t + 28) else 14
                self._pts.append((px, py, pt, offset_y))
            # 全抗锯齿矢量推进折线与箭头
            for i in range(1, len(self._pts)):
                x0, y0, prev_pt = self._pts[i - 1][:3]
                x1, y1, cur_pt = self._pts[i][:3]
                prev_ds = prev_pt.get("semantic_distance", 0.5)
                cur_ds = cur_pt.get("semantic_distance", 0.5)
                delta_ds = cur_ds - prev_ds
                vec = str(cur_pt.get("vector") or "neutral").lower()

                if vec in ("negative", "divergent", "trapped") or delta_ds > 0.02:
                    col = theme.ERROR
                    lw = 3
                elif vec in ("positive", "convergent") or delta_ds < -0.02:
                    col = theme.SUCCESS
                    lw = 3
                else:
                    col = theme.WARNING
                    lw = 2
                aa_items.append(("line", [(x0, y0), (x1, y1)], col, lw))
                poly = self._get_arrowhead_poly(x0, y0, x1, y1, length=10, half_width=5, setback=6)
                if poly:
                    aa_items.append(("polygon", poly, col, col))

            # (F) 质点多态化与动力学事件光晕
            for item in self._pts:
                x, y, pt = item[:3]
                vec = str(pt.get("vector") or "neutral").lower()
                event_tag = str(pt.get("event") or "normal").lower()
                base_col = theme.ERROR if vec in ("negative", "divergent", "trapped") else (
                    theme.SUCCESS if vec in ("positive", "convergent") else theme.WARNING)
                # 事件脉冲光圈
                if event_tag == "retry_loop":
                    aa_items.append(("dot", x, y, 9, None, theme.ERROR, 1))
                elif event_tag == "breakthrough":
                    aa_items.append(("dot", x, y, 9, None, theme.SUCCESS, 1))
                elif event_tag == "compaction":
                    aa_items.append(("dot", x, y, 8, None, theme.CHART_PALETTE[0], 1))
                elif vec in ("negative", "divergent", "trapped"):
                    aa_items.append(("dot", x, y, 8, None, theme.ERROR, 1))
                # 质点主体
                aa_items.append(("dot", x, y, 5, base_col, theme.FG_WHITE, 2))

        # 提交抗锯齿图层贴图（彻底消除折线、漏斗圆环与节点锯齿）
        self._aa_layer(c, aa_items, self._aa_imgs)

        # 3. 上层锐利文本标签 (Layer 2: 最顶层 Canvas 原生文本，绝对不被底图遮挡)
        # 吸引子标签与副标（置于圆环上方开阔安全区，与顶部域界标严格垂直错开）
        c.create_text(att_x, pad_t + 4, text="平庸代码吸引子 P(C_mediocre)",
                      fill=theme.ERROR, font=theme.FONT_UI_SMALL_BOLD, anchor="center")
        c.create_text(att_x, pad_t + 18, text="[引力势阱 / 预训练惯性漏斗]",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="center")

        # 狄拉克目标点标签与副标
        c.create_text(tgt_x + 22, tgt_y - 6, text="狄拉克目标点 C_expert",
                      fill=theme.SUCCESS, font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        c.create_text(tgt_x + 22, tgt_y + 8, text="[零熵理想代码态]",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")

        # 顶层极值域界标（推至两极，平实质朴）
        c.create_text(pad_l + 6, pad_t - 16, text="← 目标收敛区 (偏离 ≤ 0.10)",
                      fill=theme.PHASE_ZONE_DIRAC, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w - 6, pad_t - 16, text="高熵偏离区 (偏离 ≥ 0.90) →",
                      fill=theme.PHASE_ZONE_TRAP, font=theme.FONT_UI_SMALL, anchor="e")

        # X 轴刻度文本（通俗自然，指明代码与意图的对齐程度）
        c.create_text(pad_l, pad_t + plot_h + 12, text="0.0 (精准达成目标)",
                      fill=theme.SUCCESS, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w / 2, pad_t + plot_h + 12,
                      text="代码偏离度：向左贴近目标代码 · 向右偏离业务需求",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="center")
        c.create_text(pad_l + plot_w, pad_t + plot_h + 12, text="1.0 (严重偏离需求)",
                      fill=theme.ERROR, font=theme.FONT_UI_SMALL, anchor="e")
        # Y 轴刻度文本
        for gy, val_txt in y_ticks:
            c.create_text(pad_l - 6, gy, text=val_txt, fill=theme.MUTED,
                          font=theme.FONT_UI_SMALL, anchor="e")

        # 质点回合标签与事件微标（错开避让，在最顶层）
        for i, item in enumerate(self._pts):
            x, y, pt = item[:3]
            offset_y = item[3] if len(item) > 3 else (-14 if (i % 2 == 0 and y > pad_t + 28) else 14)
            t_val = pt.get("turn")
            event_tag = str(pt.get("event") or "normal").lower()
            t_str = f"T{t_val}" if t_val is not None else ""
            c.create_text(x, y + offset_y, text=t_str, fill=theme.FG_WHITE,
                          font=theme.FONT_UI_SMALL_BOLD)
            if event_tag in ("retry_loop", "breakthrough", "compaction", "test_fail"):
                evt_labels = {
                    "retry_loop": "重试",
                    "breakthrough": "突破",
                    "compaction": "压缩",
                    "test_fail": "报错",
                }
                lbl_text = evt_labels.get(event_tag, "")
                evt_col = theme.SUCCESS if event_tag == "breakthrough" else theme.ERROR
                c.create_text(x, y + offset_y + (10 if offset_y > 0 else -10),
                              text=f"({lbl_text})", fill=evt_col,
                              font=theme.FONT_UI_SMALL)

        if not traj:
            c.create_text(pad_l + plot_w / 2, pad_t + plot_h / 2,
                          text="（本动力学报告无细分轨迹采样数据）", fill=theme.MUTED)
class LlmReportsView:
    """「LLM 报告」页签 — 会话/项目/多源解读的持久化回看（左列表 + 右全高阅读区）。

    支持多来源扩展（会话收敛、相空间动力学、项目全局、模型对比等）；左侧带类型筛选与实时搜索；
    右侧为结构化卡片头（标题/来源/模型/档位/指标徽标 + 一键复制全文）与
    原生平滑滚动的排版阅读器（段落行距、悬挂缩进、层级标题与 Markdown 标签）。
    """

    _BODY_FONT = (theme.FONT_CJK, 10)

    REPORT_KINDS = {
        "session":  {"label": "会话", "color": theme.CHART_PALETTE[2], "desc": "会话过程收敛解读"},
        "dynamics": {"label": "相空间", "color": theme.CHART_PALETTE[4], "desc": "相空间收敛动力学分析"},
        "project":  {"label": "项目", "color": theme.CHART_PALETTE[0], "desc": "项目全局架构解读"},
        "compare":  {"label": "对比", "color": theme.CHART_PALETTE[3], "desc": "多模型/跨源对比解读"},
        "anomaly":  {"label": "诊断", "color": theme.WARNING, "desc": "异常卡死/返工诊断"},
        "general":  {"label": "通用", "color": theme.MUTED, "desc": "综合解读报告"},
    }

    def __init__(self, parent, controller=None) -> None:
        from .widgets import flat_button, FlatMenu
        self.controller = controller
        self._reports: list[dict] = []
        self._selected_id: str | None = None
        self._sort_col = "time"
        self._sort_desc = True
        self._kind_filter = "all"
        self._search_keyword = ""

        # 分栏：左列表 + 右阅读区，支持用户拖拽调整宽度（参考 ScoreRankingView）
        paned = tk.PanedWindow(parent, orient="horizontal", bg=theme.BG, sashwidth=3)
        paned.pack(fill="both", expand=True, padx=theme.PAD_S, pady=theme.PAD_S)
        self._paned_ref = paned
        self._sash_target = 330

        # 左：报告列表（类型 / 解读对象 / 模型 / 时间）— 仅用于点选定位，保持紧凑
        left = tk.Frame(paned, bg=theme.BG)
        paned.add(left, minsize=200)
        # 左顶部：标题栏 + 数量 + 清空按钮
        bar = tk.Frame(left, bg=theme.BG)
        bar.pack(fill="x", pady=(0, theme.PAD_XS))
        _icon = ui_icon(bar, "session")
        if _icon is not None:
            tk.Label(bar, image=_icon, bg=theme.BG).pack(side="left", padx=(0, 4))
        tk.Label(bar, text="LLM 报告", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_HEADING).pack(side="left")
        self._count_lbl = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED,
                                   font=theme.FONT_UI_SMALL)
        self._count_lbl.pack(side="left", padx=8)
        flat_button(bar, "清空", self._clear_all, padx=theme.PAD_S).pack(side="right", padx=2)

        # 搜索与类型过滤工具条
        filter_box = tk.Frame(left, bg=theme.BG)
        filter_box.pack(fill="x", pady=(0, 6))

        # 第一行：类型切换胶囊
        self._pill_frame = tk.Frame(filter_box, bg=theme.BG)
        self._pill_frame.pack(fill="x", pady=(0, 4))
        self._pill_btns: dict[str, tk.Widget] = {}
        for k, lbl in (("all", "全部"), ("session", "会话"), ("dynamics", "相空间"),
                       ("project", "项目"), ("compare", "对比"), ("other", "其他")):
            btn = tk.Label(self._pill_frame, text=lbl, bg=theme.PANEL_2,
                           fg=theme.FG, font=theme.FONT_UI_SMALL,
                           padx=6, pady=2, cursor=CLICK_CURSOR)
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda _e, kind=k: self._set_kind_filter(kind))
            self._pill_btns[k] = btn

        # 第二行：实时搜索框
        search_wrap = tk.Frame(filter_box, bg=theme.PANEL_2, highlightthickness=1,
                               highlightbackground=theme.BORDER)
        search_wrap.pack(fill="x", pady=(2, 0))
        _si = ui_icon(search_wrap, "search")
        if _si is not None:
            tk.Label(search_wrap, image=_si, bg=theme.PANEL_2).pack(
                side="left", padx=(4, 2), pady=2)
        self._search_var = tk.StringVar(value="")
        self._search_entry = tk.Entry(
            search_wrap, textvariable=self._search_var, bg=theme.PANEL_2,
            fg=theme.FG, insertbackground=theme.FG, relief="flat",
            borderwidth=0, highlightthickness=0, font=theme.FONT_UI_SMALL)
        self._search_entry.pack(side="left", fill="x", expand=True, padx=4, pady=2)
        self._search_entry.bind("<KeyRelease>", lambda _e: self._on_search_changed())
        Tooltip(self._search_entry, "按标题 / 解读对象 / 模型 / 内容 实时过滤")

        # 列表 Treeview
        tree_container = tk.Frame(left, bg=theme.PANEL)
        tree_container.pack(fill="both", expand=True)
        cols = ("kind", "title", "time")
        self._tree = ttk.Treeview(tree_container, columns=cols, show="headings",
                                  selectmode="browse")
        for col, text, w, mw, anchor, stretch in (
                ("kind", "类型", 46, 40, "center", False),
                ("title", "解读对象 / 标题", 180, 100, "w", True),
                ("time", "时间", 90, 82, "center", False)):
            self._tree.heading(col, text=text, anchor=anchor,
                               command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=w, minwidth=mw, stretch=stretch,
                              anchor=anchor)

        sb = ttk.Scrollbar(tree_container, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._unbind_wheel = None
        self._tree.bind("<Enter>", self._on_tree_enter)
        self._tree.bind("<Leave>", self._on_tree_leave)

        # 右键上下文菜单
        self._ctx_menu = FlatMenu(self._tree)
        self._ctx_menu.add_command(label="复制全文 Markdown", command=self._copy_markdown)
        self._ctx_menu.add_command(label="删除本条报告", command=self._delete_selected)
        self._tree.bind("<Button-3>", self._on_tree_context_menu)

        # 列表标签着色
        for k, meta in self.REPORT_KINDS.items():
            self._tree.tag_configure(f"kind_{k}", foreground=meta["color"])

        # 右：全高阅读区 — 作为报告展示主核心
        right = tk.Frame(paned, bg=theme.PANEL)
        paned.add(right, minsize=380)

        # 右顶部：结构化元数据 Hero 卡片
        self._header_card = tk.Frame(
            right, bg=theme.PANEL_2, relief="flat", highlightthickness=1,
            highlightbackground=theme.BORDER)
        self._header_card.pack(fill="x", padx=10, pady=(4, 6))

        # 卡片第一行：标题 + 操作按钮
        top_row = tk.Frame(self._header_card, bg=theme.PANEL_2)
        top_row.pack(fill="x", padx=10, pady=(8, 4))
        self._title_lbl = tk.Label(
            top_row, text="", bg=theme.PANEL_2, fg=theme.FG_WHITE,
            font=(theme.FONT_CJK, 12, "bold"), anchor="w", justify="left")
        self._title_lbl.pack(side="left", fill="x", expand=True)

        self._copy_btn = flat_button(
            top_row, "复制全文", self._copy_markdown, padx=theme.PAD_S)
        self._copy_btn.pack(side="right", padx=(4, 0))
        flat_button(top_row, "删除", self._delete_selected,
                    padx=theme.PAD_S).pack(side="right")

        # 卡片第二行：徽标与关键指标
        self._badge_row = tk.Frame(self._header_card, bg=theme.PANEL_2)
        self._badge_row.pack(fill="x", padx=10, pady=(0, 8))

        self._kind_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL, fg=theme.ACCENT,
            font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self._kind_badge.pack(side="left", padx=(0, 6))

        self._source_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL, fg=theme.FG,
            font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self._source_badge.pack(side="left", padx=(0, 6))

        self._model_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL, fg=theme.FG,
            font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self._model_badge.pack(side="left", padx=(0, 6))

        self._scope_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self._scope_badge.pack(side="left", padx=(0, 6))

        self._metrics_lbl = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._metrics_lbl.pack(side="left", padx=4)

        self._time_lbl = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._time_lbl.pack(side="right", padx=(4, 0))

        # 兼容旧代码引用的 _meta_lbl 属性（指向标题文本或空桩）
        self._meta_lbl = self._title_lbl

        # 正文阅读器（原生平滑滚动 Text + 自定义排版标签）
        # 专属相空间动力学相图组件（仅在选中 dynamics 报告时挂载显示）
        self._phase_portrait = PhasePortraitWidget(right)

        # 正文阅读器（原生平滑滚动 Text + 自定义排版标签）
        self._text_frame = text_frame = tk.Frame(right, bg=theme.PANEL)
        text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        text_sb = ttk.Scrollbar(text_frame, orient="vertical")
        self._body_lbl = tk.Text(
            text_frame, wrap="char", bg=theme.PANEL, fg=theme.FG_WHITE,
            font=self._BODY_FONT, relief="flat", bd=0, highlightthickness=0,
            padx=20, pady=16, yscrollcommand=text_sb.set,
            selectbackground=theme.HOVER_ACCENT, selectforeground=theme.FG_WHITE,
            inactiveselectbackground=theme.HOVER_ACCENT, cursor="arrow")
        text_sb.config(command=self._body_lbl.yview)
        text_sb.pack(side="right", fill="y")
        self._body_lbl.pack(side="left", fill="both", expand=True)
        self._body_lbl.bind("<Button-3>", self._on_body_context_menu)

        # 排版 Tag 配置
        self._body_lbl.tag_configure(
            "sec_head", font=(theme.FONT_CJK, 12, "bold"),
            foreground=theme.ACCENT, spacing1=18, spacing3=6)
        self._body_lbl.tag_configure(
            "md_head", font=(theme.FONT_CJK, 11, "bold"),
            foreground=theme.FG_WHITE, spacing1=12, spacing3=4)
        self._body_lbl.tag_configure(
            "md_h1", font=(theme.FONT_CJK, 13, "bold"),
            foreground=theme.FG_WHITE, spacing1=16, spacing3=6)
        self._body_lbl.tag_configure(
            "md_h2", font=(theme.FONT_CJK, 11, "bold"),
            foreground=theme.FG_WHITE, spacing1=12, spacing3=4)
        self._body_lbl.tag_configure(
            "md_h3", font=(theme.FONT_CJK, 10, "bold"),
            foreground=theme.CHART_PALETTE[1], spacing1=10, spacing3=3)
        self._body_lbl.tag_configure(
            "body", font=self._BODY_FONT, foreground=theme.FG,
            spacing1=3, spacing2=4, spacing3=3)
        self._body_lbl.tag_configure(
            "list_item", font=self._BODY_FONT, foreground=theme.FG,
            lmargin1=16, lmargin2=32, spacing1=3, spacing3=3)
        self._body_lbl.tag_configure(
            "sub_list_item", font=self._BODY_FONT, foreground=theme.FG,
            lmargin1=36, lmargin2=52, spacing1=2, spacing3=2)
        self._body_lbl.tag_configure(
            "sub2_list_item", font=self._BODY_FONT, foreground=theme.FG,
            lmargin1=56, lmargin2=72, spacing1=2, spacing3=2)
        self._body_lbl.tag_configure(
            "quote", font=(theme.FONT_CJK, 10, "italic"), foreground=theme.MUTED,
            lmargin1=24, lmargin2=24, spacing1=4, spacing3=4)
        self._body_lbl.tag_configure(
            "md_bold", font=(theme.FONT_CJK, 10, "bold"), foreground=theme.FG_WHITE)
        self._body_lbl.tag_configure(
            "md_italic", font=(theme.FONT_CJK, 10, "italic"))
        self._body_lbl.tag_configure(
            "md_mono", font=theme.FONT_MONO, background=theme.CONTROL_BG,
            foreground=theme.CHART_PALETTE[1])
        self._body_lbl.tag_configure(
            "code_block", font=theme.FONT_MONO, background=theme.BG,
            foreground=theme.FG_WHITE, lmargin1=20, lmargin2=20,
            spacing1=1, spacing2=2, spacing3=1)
        self._apply_sash()

    def _apply_sash(self) -> None:
        target = self._sash_target

        def _place():
            try:
                if self._paned_ref.winfo_width() > target + 40:
                    self._paned_ref.sash_place(0, target, 0)
            except tk.TclError:
                pass
        self._paned_ref.after_idle(_place)

    # -- 辅助解析 --
    @classmethod
    def _resolve_kind(cls, r: dict) -> str:
        k = r.get("kind")
        if k in cls.REPORT_KINDS:
            return k
        if r.get("session_id") or r.get("session_title"):
            return "session"
        if r.get("project_name") or r.get("project_cwd"):
            return "project"
        return "general"

    @classmethod
    def _resolve_title(cls, r: dict) -> str:
        t = str(r.get("title") or r.get("session_title") or
                r.get("project_name") or r.get("session_id") or "未命名报告")
        return t.strip().replace("\r", " ").replace("\n", " ")

    # -- 数据加载与过滤 --
    def on_show(self) -> None:
        """页签切入 / 报告新增后重载列表（保持当前选中）。"""
        from tcer.core import llm_reports, llm_prefs
        self._apply_sash()
        self._reports = llm_reports.load()
        self._refresh_list()

    def _set_kind_filter(self, kind: str) -> None:
        if self._kind_filter == kind:
            return
        self._kind_filter = kind
        self._refresh_list()

    def _on_search_changed(self) -> None:
        self._search_keyword = self._search_var.get().strip().lower()
        self._refresh_list()

    def _matches_filter(self, r: dict) -> bool:
        kind = self._resolve_kind(r)
        if self._kind_filter != "all":
            if self._kind_filter == "other":
                if kind in ("session", "dynamics", "project", "compare"):
                    return False
            elif kind != self._kind_filter:
                return False
        if self._search_keyword:
            kw = self._search_keyword
            title = self._resolve_title(r).lower()
            model = str(r.get("model") or "").lower()
            text = str(r.get("text") or "").lower()
            if kw not in title and kw not in model and kw not in text:
                return False
        return True

    def _refresh_list(self) -> None:
        # 更新过滤胶囊样式与计数
        counts: dict[str, int] = {"all": len(self._reports), "session": 0,
                                  "dynamics": 0, "project": 0, "compare": 0, "other": 0}
        for r in self._reports:
            k = self._resolve_kind(r)
            if k in counts:
                counts[k] += 1
            else:
                counts["other"] += 1
        for k, btn in self._pill_btns.items():
            active = (self._kind_filter == k)
            bg = theme.HOVER_ACCENT if active else theme.PANEL_2
            fg = theme.FG_WHITE if active else theme.FG
            label = {"all": "全部", "session": "会话", "dynamics": "相空间",
                     "project": "项目", "compare": "对比", "other": "其他"}.get(k, k)
            btn.config(text=f"{label} {counts.get(k, 0)}", bg=bg, fg=fg)

        # 过滤报告集合
        filtered = [r for r in self._reports if self._matches_filter(r)]

        # 排序
        key_fn = {
            "time": lambda r: r.get("created_at") or 0,
            "title": lambda r: self._resolve_title(r).lower(),
            "model": lambda r: str(r.get("model") or "").lower(),
            "kind": lambda r: self._resolve_kind(r),
        }.get(self._sort_col, lambda r: r.get("created_at") or 0)
        filtered.sort(key=key_fn, reverse=self._sort_desc)

        sel = self._selected_id
        self._tree.delete(*self._tree.get_children())

        if not filtered:
            from tcer.core import llm_prefs
            if not self._reports:
                note = "（暂无报告——在会话时间线弹窗点「LLM 解读」生成）"
                if not llm_prefs.enabled():
                    note += "\n\n尚未配置 LLM 服务：工具栏「LLM设置」完成配置后即可使用。"
            else:
                note = "（没有匹配当前筛选条件的 LLM 报告）"
            self._clear_header()
            self._body_lbl.configure(state="normal")
            self._body_lbl.delete("1.0", "end")
            self._body_lbl.insert("end", note, ("quote",))
            self._body_lbl.configure(state="disabled")
            self._count_lbl.config(text=f"0 / {len(self._reports)} 条")
            return

        for r in filtered:
            kind = self._resolve_kind(r)
            kind_lbl = self.REPORT_KINDS.get(kind, {}).get("label", "报告")
            self._tree.insert(
                "", "end", iid=r.get("id"),
                tags=(f"kind_{kind}",),
                values=(kind_lbl,
                        self._fmt_title(self._resolve_title(r)),
                        self._fmt_time(r.get("created_at"))))

        self._count_lbl.config(text=f"{len(filtered)} / {len(self._reports)} 条")
        if sel and self._tree.exists(sel):
            self._tree.selection_set(sel)
            self._tree.see(sel)
        elif filtered:
            first = self._tree.get_children()[0]
            self._tree.selection_set(first)
            self._tree.see(first)

        if self._tree.selection():
            self._on_select()

    def _clear_header(self) -> None:
        self._title_lbl.config(text="无选中的报告")
        self._kind_badge.config(text="")
        self._source_badge.config(text="")
        self._model_badge.config(text="")
        self._scope_badge.config(text="")
        self._metrics_lbl.config(text="")
        self._time_lbl.config(text="")

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = (col in ("time",))
        self._refresh_list()

    def _on_tree_enter(self, _event=None) -> None:
        from .platform import bind_mousewheel
        self._unbind_wheel = bind_mousewheel(
            self._tree, lambda units: self._tree.yview_scroll(units, "units"))

    def _on_tree_leave(self, _event=None) -> None:
        if self._unbind_wheel:
            self._unbind_wheel()
            self._unbind_wheel = None

    def select_report(self, report_id: str) -> None:
        """报告生成保存后由 controller 调用：选中并展示。"""
        self.on_show()
        if report_id and self._tree.exists(report_id):
            self._tree.selection_set(report_id)
            self._tree.see(report_id)
            self._on_select()

    # -- 选中与展示 --
    def _on_select(self, _event=None) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        rid = sel[0]
        r = next((x for x in self._reports if x.get("id") == rid), None)
        if r is None:
            return
        self._selected_id = rid

        # 刷新 Header Hero 卡片
        kind = self._resolve_kind(r)
        kind_meta = self.REPORT_KINDS.get(kind, self.REPORT_KINDS["general"])
        title = self._resolve_title(r)

        self._title_lbl.config(text=title)
        self._kind_badge.config(
            text=f"[{kind_meta['label']}解读]", fg=kind_meta["color"])
        self._source_badge.config(text=f"源: {r.get('source') or 'claude'}")
        self._model_badge.config(text=f"模型: {r.get('model') or '-'}")
        self._scope_badge.config(text=f"档位: {r.get('scope') or '-'}")

        # 构造关键指标摘要
        metrics_parts = []
        if r.get("turns"):
            metrics_parts.append(f"{r['turns']} 回合")
        if r.get("net_loc") is not None:
            nl = r["net_loc"]
            metrics_parts.append(f"净增 {nl:+d} 行" if isinstance(nl, int) else f"净增 {nl} 行")
        if r.get("cost_display"):
            metrics_parts.append(f"{r['cost_display']}")
        self._metrics_lbl.config(text=" · ".join(metrics_parts))
        self._time_lbl.config(text=self._fmt_time(r.get("created_at")))

        # 动力学相图视口联动（仅 dynamics 类型显示，其余报告完全隐藏）
        if kind == "dynamics":
            self._phase_portrait.render(r.get("dynamics_data") or {}, r)
            self._phase_portrait.pack(fill="x", padx=10, pady=(0, 6), before=self._text_frame)
        else:
            self._phase_portrait.pack_forget()

        # 渲染正文
        self._fill_body(str(r.get("text") or ""))
    def _copy_markdown(self) -> None:
        """一键复制当前报告全文到系统剪贴板。"""
        sel = self._tree.selection()
        if not sel:
            return
        r = next((x for x in self._reports if x.get("id") == sel[0]), None)
        if not r or not r.get("text"):
            return
        try:
            top = getattr(self.controller, "root", None) or self._tree.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(str(r["text"]))
            self._copy_btn.config(text="已复制 ✓")
            self._copy_btn.after(1500, lambda: self._copy_btn.config(text="复制全文"))
        except Exception:
            pass

    def _on_tree_context_menu(self, event) -> None:
        item = self._tree.identify_row(event.y)
        if item:
            self._tree.selection_set(item)
            self._on_select()
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_body_context_menu(self, event) -> None:
        from .widgets import FlatMenu
        m = FlatMenu(self._body_lbl)
        try:
            sel = self._body_lbl.get("sel.first", "sel.last")
        except tk.TclError:
            sel = ""
        if sel:
            m.add_command(label="复制所选内容", command=lambda: (
                self.controller.root.clipboard_clear(),
                self.controller.root.clipboard_append(sel)))
        m.add_command(label="复制全文 Markdown", command=self._copy_markdown)
        m.tk_popup(event.x_root, event.y_root)

    # -- 正文渲染：reflow + 结构化 Markdown ---------------------------------
    _SECTION_SPLIT_RE = re.compile(r"^(?:(\d+)[\.、\s]*)?【([^】]+)】(?:\s*(.*))?$")
    _MD_BOLD = re.compile(r"(?:\*\*|__)(.+?)(?:\*\*|__)")
    _MD_ITALIC = re.compile(r"\*([^*\n]+?)\*")
    _MD_CODE = re.compile(r"`([^`]+)`")
    _MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
    _HEADING_RE = re.compile(r"^(?:(?:(\d+)[\.、\s]*)?【[^】]+】|#{1,4}\s|>|▎|```|---|===)")
    _LIST_RE = re.compile(r"^([-*•·]|\d+[.、)])\s*")

    @classmethod
    def _reflow_lines(cls, text: str) -> list[str]:
        """段内硬换行合并：空行分段；标题/【】行独立；保留列表缩进；代码块内部不合并。"""
        out: list[str] = []
        buf = ""
        in_code = False

        def _flush() -> None:
            nonlocal buf
            if buf:
                out.append(buf)
            buf = ""

        for raw in text.replace("\r\n", "\n").split("\n"):
            s_strip = raw.strip()
            if s_strip.startswith("```"):
                _flush()
                out.append(s_strip)
                in_code = not in_code
                continue
            if in_code:
                out.append(raw.rstrip())
                continue
            if not s_strip:
                _flush()
                if out and out[-1] != "":
                    out.append("")
            elif cls._HEADING_RE.match(s_strip):
                _flush()
                m_sec = cls._SECTION_SPLIT_RE.match(s_strip)
                if m_sec and m_sec.group(3):
                    num, title, rest = m_sec.groups()
                    prefix = f"{num}. " if num else ""
                    out.append(f"{prefix}【{title}】")
                    if rest.strip():
                        buf = rest.strip()
                else:
                    out.append(s_strip)
            elif cls._LIST_RE.match(s_strip):
                _flush()
                buf = raw.rstrip()
            elif buf and buf[-1].isascii() and buf[-1].isalnum() \
                    and s_strip and s_strip[0].isascii() and s_strip[0].isalnum():
                buf += " " + s_strip
            else:
                buf += s_strip
        _flush()
        while out and out[-1] == "":
            out.pop()
        return out

    @staticmethod
    def _split_md(text: str, pattern) -> list[tuple[str, bool]]:
        out = []
        for i, part in enumerate(pattern.split(text)):
            if part:
                out.append((part, i % 2 == 1))
        return out

    def _insert_line(self, tb, ln: str, *, in_code_block: bool = False) -> None:
        if in_code_block:
            tb.insert("end", ln + "\n", ("code_block",))
            return
        if ln.startswith("```"):
            return
        s_strip = ln.strip()
        m_sec = self._SECTION_SPLIT_RE.match(s_strip)
        if m_sec:
            num, title, _ = m_sec.groups()
            prefix = f"{num}. " if num else ""
            tb.insert("end", f"{prefix}【{title}】\n", ("sec_head",))
            return
        m_head = re.match(r"^(#{1,4})\s+(.+)$", s_strip)
        if m_head:
            lvl = len(m_head.group(1))
            tb.insert("end", m_head.group(2) + "\n", ("md_head", f"md_h{lvl}"))
            return

        indent = len(ln) - len(ln.lstrip(" "))

        # 无序列表处理（支持多级嵌套与键值加粗）
        if re.match(r"^[-*•·]\s*", s_strip):
            clean_content = re.sub(r"^[-*•·]\s*", "", s_strip)
            if indent >= 4:
                prefix = "▪ "
                line_tag = "sub2_list_item"
            elif indent >= 2:
                prefix = "◦ "
                line_tag = "sub_list_item"
            else:
                prefix = "• "
                line_tag = "list_item"

            m_kv = re.match(r"^([^：:\n]{2,14}[：:])\s*(.*)$", clean_content)
            if m_kv and not clean_content.startswith("**"):
                k, v = m_kv.groups()
                tb.insert("end", prefix, (line_tag,))
                tb.insert("end", k + " ", (line_tag, "md_bold"))
                self._insert_inline(tb, v, line_tag=line_tag)
            else:
                self._insert_inline(tb, prefix + clean_content, line_tag=line_tag)
            tb.insert("end", "\n")
            return

        # 有序列表
        m_num_list = re.match(r"^(\d+[.、)])\s*(.+)$", s_strip)
        if m_num_list:
            clean_content = m_num_list.group(2)
            prefix = f"{m_num_list.group(1)} "
            line_tag = "sub_list_item" if indent >= 2 else "list_item"
            m_kv = re.match(r"^([^：:\n]{2,14}[：:])\s*(.*)$", clean_content)
            if m_kv and not clean_content.startswith("**"):
                k, v = m_kv.groups()
                tb.insert("end", prefix, (line_tag,))
                tb.insert("end", k + " ", (line_tag, "md_bold"))
                self._insert_inline(tb, v, line_tag=line_tag)
            else:
                self._insert_inline(tb, prefix + clean_content, line_tag=line_tag)
            tb.insert("end", "\n")
            return

        # 引用块
        if re.match(r"^(?:>|▎)\s*", s_strip):
            clean_ln = "▎ " + re.sub(r"^(?:>|▎)\s*", "", s_strip)
            self._insert_inline(tb, clean_ln, line_tag="quote")
            tb.insert("end", "\n")
            return

        # 分割线
        if s_strip and set(s_strip) <= set("-=*―—─") and len(s_strip) >= 3:
            tb.insert("end", "─" * 48 + "\n", ("divider",))
            return

        # 普通段落
        self._insert_inline(tb, s_strip, line_tag="body")
        tb.insert("end", "\n")
    def _insert_inline(self, tb, text: str, line_tag: str = "body") -> None:
        """行内样式管线：链接展开 → 粗体 → 斜体 → 行内代码（与行级 tag 叠加）。"""
        text = self._MD_LINK.sub(r"\1", text)
        parts = [(text, False, False, False)]
        for rx, flag in ((self._MD_BOLD, 0), (self._MD_ITALIC, 1),
                         (self._MD_CODE, 2)):
            nxt = []
            for seg, b, i, c in parts:
                for sub, hit in self._split_md(seg, rx):
                    nxt.append((sub,
                                b or (hit and flag == 0),
                                i or (hit and flag == 1),
                                c or (hit and flag == 2)))
            parts = nxt
        for seg, b, i, c in parts:
            if not seg:
                continue
            tags = [line_tag]
            if b:
                tags.append("md_bold")
            if i:
                tags.append("md_italic")
            if c:
                tags.append("md_mono")
            tb.insert("end", seg, tuple(tags))

    @staticmethod
    def clean_math_syntax(text: str) -> str:
        """清洗 LLM 输出中的 LaTeX 数学公式代码，转为通俗易懂的工程师自然文本。"""
        if not text or ("$" not in text and "\\" not in text):
            return text
        import re
        # 1. 常见领域名词与模型表达式直观化
        text = re.sub(r"\$P_?\{?model\}?\(C_?\{?mediocre\}?\)\$", "平庸代码吸引子", text)
        text = re.sub(r"\$C_?\{?expert\}?\$", "目标代码 C_expert", text)
        text = re.sub(r"\$I\(C_?\{?t\+1\}?;\s*F_?\{?t\}?\)\$", "反馈互信息", text)
        # 2. 移除常见 LaTeX 命令
        text = re.sub(r"\\mathcal\{I\}", "业务意图", text)
        text = re.sub(r"\\text\{([^}]+)\}", r"\1", text)
        text = re.sub(r"\\approx", "≈", text)
        text = re.sub(r"\\max", "最大值", text)
        text = re.sub(r"\\delta", "δ", text)
        # 3. 简化常见字母下标：D_s -> Ds, X_1 -> X1
        text = re.sub(r"([A-Za-z])_\{?([A-Za-z0-9]+)\}?", r"\1_\2", text)
        # 4. 剥离剩余的 $ ... $ 数学符号
        text = re.sub(r"\$([^$\n]+)\$", r"\1", text)
        # 5. 清理残留反斜杠
        text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
        return text

    @staticmethod
    def _pad_cjk_ascii(text: str) -> str:
        text = re.sub(r"(?<=[一-鿿（【“]) ?(?=[A-Za-z0-9])", " ", text)
        return re.sub(r"(?<=[A-Za-z0-9]) ?(?=[一-鿿])", " ", text)

    def _fill_body(self, text: str) -> None:
        tb = self._body_lbl
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        in_code_block = False
        text = self.clean_math_syntax(text)
        for ln in self._reflow_lines(text):
            if ln.strip().startswith("```"):
                in_code_block = not in_code_block
                continue
            if not ln.strip() and not in_code_block:
                tb.insert("end", "\n")
                continue
            self._insert_line(tb, ln, in_code_block=in_code_block)
        tb.configure(state="disabled")
    def _delete_selected(self) -> None:
        import tkinter.messagebox as mb
        from tcer.core import llm_reports
        sel = self._tree.selection()
        if not sel:
            return
        r = next((x for x in self._reports if x.get("id") == sel[0]), None)
        title = self._resolve_title(r or {})[:24]
        if not mb.askyesno("删除报告",
                           f"确定删除「{title}…」这条 LLM 报告？（不可恢复）",
                           parent=self._tree):
            return
        self._selected_id = None
        llm_reports.delete(sel[0])
        self.on_show()

    def _clear_all(self) -> None:
        import tkinter.messagebox as mb
        from tcer.core import llm_reports
        if not self._reports:
            return
        if mb.askyesno("清空 LLM 报告", f"确定删除全部 {len(self._reports)} 条报告？"
                       "（不可恢复）", parent=self._tree):
            self._selected_id = None
            llm_reports.clear()
            self.on_show()

    # -- fmt --
    @staticmethod
    def _fmt_time(ts) -> str:
        if not ts:
            return "-"
        return fmt.fmt_dt(int(ts), "%m-%d %H:%M")

    @staticmethod
    def _fmt_title(title: str) -> str:
        return str(title or "-").strip().replace("\r", " ").replace("\n", " ")

    @classmethod
    def _fmt_session(cls, r: dict) -> str:
        return cls._fmt_title(cls._resolve_title(r))
