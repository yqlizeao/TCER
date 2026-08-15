"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``ScoreRankingView`` consumes the shared
``export.score_ranking`` / ``export.score_decompose`` helpers.
"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from dataclasses import dataclass
from tkinter import ttk

from tcer.core import metrics
from tcer.core.export import score_decompose, score_decompose_avg
from tcer.core.insights import (session_insights, project_insights,
                                 activity_overview, claude_md_suggestions,
                                 feature_suggestions, horizon_suggestions)
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


def project_source_label(project) -> str:
    source = getattr(project, "source", "claude")
    if source == "codex":
        return "Codex"
    if source == "opencode":
        return "OpenCode"
    if source == "grok":
        return "Grok"
    if source == "omp":
        return "Oh My Pi"
    if source == "pi":
        return "Pi"
    return "Claude"


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
        Tooltip(btn, "项目总览 · 会话时间线 · 会话对比 · 工具序列 · 个人基准 · 任务类型 · 高级选项 · 检查更新 / 版本信息")
        return btn

    def _build_tool_menu(self, menu) -> None:
        c = self.controller
        menu.add_command(label="项目总览", command=c.show_project_overview)
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


