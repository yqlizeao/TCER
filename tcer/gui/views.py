"""Data-facing views: filter bar, project/session columns, metric panel, charts.

Each view is built from ``metric_defs`` / ``theme`` / ``widgets`` and calls back
into the controller (passed in) — views hold no analysis state of their own.
Chart classes draw on a ``tk.Canvas``; ``ScoreRankingView`` consumes the shared
``export.score_ranking`` / ``export.score_decompose`` helpers.
"""
from __future__ import annotations

import os
import re
import math
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


def format_card_title(title: str, max_len: int = 38) -> str:
    """智能清洗会话卡片标题（剥除超长本地路径前缀，提炼核心任务或意图）。"""
    t = (title or "").strip()
    if not t:
        return "(无标题)"
    for prefix in ("查看此会话：", "查看会话：", "查看：", "查看此会话: ", "查看会话: "):
        if t.startswith(prefix):
            rest = t[len(prefix):].strip()
            parts = rest.split()
            if len(parts) > 1 and any(parts[0].lower().endswith(ext) for ext in (".jsonl", ".json", ".sqlite")):
                instruction = " ".join(parts[1:]).strip()
                if instruction:
                    t = f"会话审查：{instruction}"
                    break
            if any(sep in rest for sep in ("\\", "/")):
                import os
                fname = os.path.basename(rest.replace("\\", "/").rstrip("/"))
                t = f"查看会话：{fname}"
                break
    return t[:max_len] + "..." if len(t) > max_len else t


# 排名页对用户展示的「综合效率分」名称与简称（取自指标 SSOT）。
_SCORE_NAME = metric_name("score")        # 综合效率分
_SCORE_SHORT = "效率分"                    # 窄列/徽标用简称
_SCORE_TIP = metric_tip("score")          # 悬停完整解释
from .widgets import (Card, CollapsibleSection, FlatMenu,
                      MetricCell, ScrollFrame, SelectableLabel, Tooltip, flat_button, WorkbenchTab,
                      RoundedPill, RoundedSearchBox, RoundedKpiChip, get_rounded_rect_img)
from .platform import CLICK_CURSOR

_PER_ROW = 6  # metric tiles per grid row inside a group


def dominant_model_label(usage) -> str:
    """会话主模型短名（``per_model`` 中 token 量最大者，经价表归一化）。

    卡片 / 状态栏共用同一口径（曾两处各自 max 复制且权重键取错成恒 0，
    混合会话显示成首键模型）。规范实现在 ``analyze._dominant_model_key``。
    """
    if not usage:
        return ""
    from tcer.core import analyze as _analyze, pricing as _pricing
    key = _analyze._dominant_model_key(usage)
    return _pricing.label(key) if key else ""

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
    if source in ("codex", "opencode", "grok", "omp", "pi", "antigravity"):
        default = {
            "codex": "Codex", "opencode": "OpenCode", "grok": "Grok",
            "omp": "Oh My Pi", "pi": "Pi", "antigravity": "Antigravity",
        }.get(source, source)
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
    "omp": "Oh My Pi", "pi": "Pi", "antigravity": "Antigravity",
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
    tk_app = getattr(master, "tk", None)
    app_id = id(tk_app) if tk_app is not None else None
    cache_key = (app_id, icon_key)
    cached = _ICON_CACHE.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached
    path = os.path.join(_ASSETS_DIR, f"{icon_key}.png")
    img = None
    if os.path.isfile(path):
        try:
            img = tk.PhotoImage(master=master, file=path)
        except tk.TclError:
            img = None
    _ICON_CACHE[cache_key] = img
    return img

def ui_icon(master, name: str, *, opacity: float = 1.0, size: int = 16):
    """通用 UI 图标（``assets/ui-<name>[-<size>].png``），支持按透明度调光和尺寸。"""
    tk_app = getattr(master, "tk", None)
    app_id = id(tk_app) if tk_app is not None else None
    size_suffix = f"-{size}" if size != 16 else ""
    cache_key = (app_id, f"ui-{name}{size_suffix}", int(opacity * 100))
    cached = _ICON_CACHE.get(cache_key, _MISSING)
    if cached is not _MISSING:
        return cached

    path = os.path.join(_ASSETS_DIR, f"ui-{name}{size_suffix}.png")
    if not os.path.isfile(path) and size != 16:
        path = os.path.join(_ASSETS_DIR, f"ui-{name}.png")

    if os.path.isfile(path):
        try:
            from PIL import Image, ImageTk
            im = Image.open(path).convert("RGBA")
            if opacity < 0.99:
                r, g, b, a = im.split()
                a = a.point(lambda v: int(v * opacity))
                im = Image.merge("RGBA", (r, g, b, a))
            img = ImageTk.PhotoImage(im, master=master)
            _ICON_CACHE[cache_key] = img
            return img
        except Exception:
            pass
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
    if source == "antigravity":
        from tcer.core.paths import antigravity_conversations_dir
        return str(antigravity_conversations_dir())
    path = getattr(project, "path", None)
    cwd = getattr(project, "cwd", None)
    return str(path or cwd or project)


def _file_manager_label() -> str:
    """Platform-appropriate file manager name for menu labels."""
    from .platform import FILE_MANAGER_NAME
    return FILE_MANAGER_NAME

class EditorWorkbench(tk.Frame):
    """VS Code 风格单窗口工作台编辑器（带顶部页签栏 Editor Tab Bar）。

    统一托管主区域所有页签（6 个内置基础视图 + 动态打开的分析/设置/上传页签），
    彻底消除弹出式子窗口和全屏模态遮罩，保证始终在唯一主窗口内流畅工作。
    """

    def __init__(self, parent, controller=None) -> None:
        super().__init__(parent, bg=theme.BG)
        self.controller = controller

        # 1. 顶部工作台页签栏（按要求移除：与左侧活动栏图标完全重复，不 pack 以释放 35px 纵向高度）
        self._tab_bar = tk.Frame(self, bg=theme.PANEL_2, height=0)
        self._tab_strip = tk.Frame(self._tab_bar, bg=theme.PANEL_2)

        self._tabs: dict[str, dict] = {}
        self._tab_order: list[str] = []
        self._active_tab: str | None = None
        self._history: list[str] = []
        self._handlers: list = []

        # 全局快捷键：Esc 或 Ctrl+W 关闭当前可关闭页签
        try:
            root = parent.winfo_toplevel()
            root.bind("<Escape>", self._on_escape, add="+")
            root.bind("<Control-w>", self._on_escape, add="+")
        except Exception:
            pass

    def _on_escape(self, _event=None):
        if self._active_tab and self._tabs.get(self._active_tab, {}).get("closable", False):
            self.close_tab(self._active_tab)
            return "break"

    def add(self, child, text: str = "标签", icon: str = "grid", **_kw) -> None:
        """注册内置基础页签（不可关闭，对齐 Notebook.add 协议）。"""
        tab_id = f"builtin_{len(self._tab_order)}"
        self._tab_order.append(tab_id)
        btn = self._create_tab_widget(tab_id, text, icon, closable=False)
        self._tabs[tab_id] = {
            "btn": btn,
            "frame": child,
            "title": text,
            "closable": False,
            "icon_name": icon,
            "target_w": 0,
        }

    def open_tab(self, title: str, size: str = "", bg: str = theme.BG,
                 key: str | None = None, icon: str | None = None) -> WorkbenchTab:
        """打开动态页签（弹窗/分析/设置），若同名已开则置顶激活。"""
        tab_id = key or f"tab_{title}"
        if tab_id in self._tabs:
            self.select_tab(tab_id)
            return self._tabs[tab_id]["frame"]

        if icon is None:
            if "上传" in title:
                icon = "upload"
            elif "设置" in title or "LLM" in title:
                icon = "sparkle"
            elif "时间线" in title:
                icon = "trend"
            elif "雷达" in title:
                icon = "target"
            elif "详情" in title or "会话" in title:
                icon = "session"
            elif "文件" in title:
                icon = "folder"
            elif "工具" in title:
                icon = "wrench"
            elif "模型" in title:
                icon = "model"
            elif "成本" in title:
                icon = "compare"
            elif "对比" in title:
                icon = "compare"
            elif "更新" in title:
                icon = "refresh"
            elif "删除" in title:
                icon = "trash"
            elif "消息" in title:
                icon = "chat"
            elif "记忆" in title:
                icon = "layers"
            elif "基准" in title:
                icon = "dashboard"
            else:
                icon = "tools"

        wpx = 0
        if size:
            try:
                wpx = int(size.split("x")[0])
            except Exception:
                wpx = 0

        frame = WorkbenchTab(self, self, tab_id, title, icon)
        btn = self._create_tab_widget(tab_id, title, icon, closable=True)

        self._tab_order.append(tab_id)
        self._tabs[tab_id] = {
            "btn": btn,
            "frame": frame,
            "title": title,
            "closable": True,
            "icon_name": icon,
            "target_w": wpx,
        }
        self.select_tab(tab_id)
        return frame

    def _create_tab_widget(self, tab_id: str, title: str, icon_name: str, closable: bool) -> tk.Frame:
        tab_box = tk.Frame(self._tab_strip, bg=theme.PANEL_2, cursor=CLICK_CURSOR)
        tab_box.pack(side="left", fill="y", padx=(2, 1), pady=(2, 0))

        accent = tk.Frame(tab_box, bg=theme.PANEL_2, height=2)
        accent.pack(side="top", fill="x")

        content_box = tk.Frame(tab_box, bg=theme.PANEL_2)
        content_box.pack(side="top", fill="both", expand=True, padx=(10, 8), pady=(4, 6))
        img = ui_icon(content_box, icon_name, size=16, opacity=0.75)
        ico = tk.Label(content_box, image=img, bg=theme.PANEL_2)
        ico.image = img
        ico.pack(side="left", padx=(0, 6))

        disp_title = title if len(title) <= 16 else title[:14] + "…"
        lbl = tk.Label(content_box, text=disp_title, bg=theme.PANEL_2, fg=theme.MUTED,
                       font=theme.FONT_UI)
        lbl.pack(side="left", padx=(0, 4))

        close_btn = None
        if closable:
            close_btn = tk.Label(content_box, text="✕", bg=theme.PANEL_2, fg=theme.MUTED,
                                 font=(theme.FONT_UI[0], 9), width=2, cursor=CLICK_CURSOR)
            close_btn.pack(side="right", padx=(2, 0))
            close_btn.bind("<Button-1>", lambda _e, tid=tab_id: self.close_tab(tid))
            close_btn.bind("<Enter>", lambda _e, cb=close_btn: cb.configure(fg=theme.FG_WHITE))
            close_btn.bind("<Leave>", lambda _e, cb=close_btn: cb.configure(fg=theme.MUTED))
            Tooltip(close_btn, "关闭 (Esc)")

        Tooltip(tab_box, title)
        Tooltip(content_box, title)
        Tooltip(lbl, title)


        def _on_click(_e):
            self.select_tab(tab_id)

        for w in (tab_box, content_box, ico, lbl):
            w.bind("<Button-1>", _on_click)

        if closable:
            for w in (tab_box, content_box, ico, lbl):
                w.bind("<Button-2>", lambda _e, tid=tab_id: self.close_tab(tid))

        tab_box._accent = accent
        tab_box._content_box = content_box
        tab_box._ico = ico
        tab_box._lbl = lbl
        tab_box._close_btn = close_btn
        tab_box._icon_name = icon_name

        def _on_enter(_e):
            if self._active_tab != tab_id:
                bg_col = theme.HOVER_BG
                tab_box.configure(bg=bg_col)
                accent.configure(bg=bg_col)
                content_box.configure(bg=bg_col)
                ico.configure(bg=bg_col)
                lbl.configure(bg=bg_col, fg=theme.FG_WHITE)
                if close_btn:
                    close_btn.configure(bg=bg_col)

        def _on_leave(e):
            if self._active_tab != tab_id:
                if e is not None:
                    try:
                        under = e.widget.winfo_containing(e.x_root, e.y_root)
                        curr = under
                        while curr is not None:
                            if curr == tab_box:
                                return  # 仍在页签容器内，不触发离开态
                            curr = getattr(curr, "master", None)
                    except Exception:
                        pass
                bg_col = theme.PANEL_2
                tab_box.configure(bg=bg_col)
                accent.configure(bg=bg_col)
                content_box.configure(bg=bg_col)
                ico.configure(bg=bg_col)
                lbl.configure(bg=bg_col, fg=theme.MUTED)
                if close_btn:
                    close_btn.configure(bg=bg_col)

        for w in (tab_box, content_box, lbl, ico):
            w.bind("<Enter>", _on_enter, add="+")
            w.bind("<Leave>", _on_leave, add="+")
        return tab_box

    def select_tab(self, tab_id: str | int):
        if isinstance(tab_id, int):
            if 0 <= tab_id < len(self._tab_order):
                tab_id = self._tab_order[tab_id]
            else:
                return
        elif not isinstance(tab_id, str) or tab_id.startswith("."):
            for tid in self._tab_order:
                if self._tabs[tid]["frame"] is tab_id or str(self._tabs[tid]["frame"]) == str(tab_id):
                    tab_id = tid
                    break

        if tab_id not in self._tabs:
            return

        prev_tab = self._active_tab
        self._active_tab = tab_id
        if prev_tab and prev_tab != tab_id and prev_tab not in self._history:
            self._history.append(prev_tab)

        for tid, tab_info in self._tabs.items():
            box = tab_info["btn"]
            if not box.winfo_exists():
                continue
            is_active = (tid == tab_id)
            bg_col = theme.BG if is_active else theme.PANEL_2
            accent_col = theme.CYAN if is_active else theme.PANEL_2
            fg_col = theme.FG_WHITE if is_active else theme.MUTED
            opacity = 1.0 if is_active else 0.7

            box.configure(bg=bg_col)
            box._accent.configure(bg=accent_col)
            box._content_box.configure(bg=bg_col)
            box._ico.configure(bg=bg_col)
            img = ui_icon(box._content_box, box._icon_name, size=16, opacity=opacity)
            box._ico.configure(image=img)
            box._ico.image = img
            box._lbl.configure(bg=bg_col, fg=fg_col)
            if box._close_btn:
                box._close_btn.configure(bg=bg_col)

            frame = tab_info["frame"]
            if frame.winfo_exists():
                if is_active:
                    target_w = tab_info.get("target_w", 0)
                    if target_w and target_w < 900:
                        frame.pack(fill="y", expand=True, pady=12)
                        frame.configure(width=target_w)
                        frame.pack_propagate(False)
                    else:
                        frame.pack(fill="both", expand=True)
                else:
                    frame.pack_forget()

        for h in self._handlers:
            try:
                h()
            except Exception:
                pass

    def close_tab(self, tab_id: str):
        if tab_id not in self._tabs:
            return
        tab_info = self._tabs[tab_id]
        if not tab_info.get("closable", False):
            return

        is_active = (self._active_tab == tab_id)
        frame = tab_info["frame"]
        btn = tab_info["btn"]

        if getattr(frame, "_closing", False):
            pass
        else:
            frame._closing = True

        del self._tabs[tab_id]
        if tab_id in self._tab_order:
            self._tab_order.remove(tab_id)
        if tab_id in self._history:
            self._history = [t for t in self._history if t != tab_id]

        if btn.winfo_exists():
            btn.destroy()
        if frame.winfo_exists():
            try:
                tk.Frame.destroy(frame)
            except Exception:
                pass

        if is_active:
            next_tab = None
            while self._history:
                candidate = self._history.pop()
                if candidate in self._tabs:
                    next_tab = candidate
                    break
            if not next_tab and self._tab_order:
                next_tab = self._tab_order[0]
            if next_tab:
                self.select_tab(next_tab)

    def update_tab_title(self, tab_id: str, new_title: str) -> None:
        if tab_id in self._tabs:
            self._tabs[tab_id]["title"] = new_title
            disp_title = new_title if len(new_title) <= 16 else new_title[:14] + "…"
            self._tabs[tab_id]["btn"]._lbl.configure(text=disp_title)

    def select(self, ref=None):
        if ref is None:
            return self.index("current")
        self.select_tab(ref)

    def index(self, ref):
        if ref == "current":
            if self._active_tab in self._tab_order:
                return self._tab_order.index(self._active_tab)
            return 0
        if ref == "end":
            return len(self._tab_order)
        if isinstance(ref, int):
            return ref
        for i, tid in enumerate(self._tab_order):
            if tid == ref or self._tabs[tid]["frame"] is ref or str(self._tabs[tid]["frame"]) == str(ref):
                return i
        return 0

    def tabs(self):
        return [str(self._tabs[tid]["frame"]) for tid in self._tab_order]

    def bind(self, _seq, handler) -> None:
        self._handlers.append(handler)


_NavStack = EditorWorkbench


class StatusIconBtn(tk.Frame):
    """状态栏纯图标小按钮：16x16 极简 Codicon，悬停微圆角底，带 Tooltip 与点击下钻。"""

    def __init__(self, parent, icon_name: str, *, command=None, tip: str = "") -> None:
        super().__init__(parent, bg=theme.STATUS_BG, cursor=CLICK_CURSOR if command else "arrow")
        self._command = command
        self._icon_name = icon_name
        self._tip = tip
        self._text = ""

        self._img_dim = ui_icon(self, icon_name, size=16, opacity=0.60)
        self._img_hov = ui_icon(self, icon_name, size=16, opacity=1.00)

        self._lbl = tk.Label(self, image=self._img_dim, bg=theme.STATUS_BG, padx=4, pady=2)
        self._lbl.image = self._img_dim
        self._lbl.pack(side="left")

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._lbl.bind("<Enter>", self._on_enter)
        self._lbl.bind("<Leave>", self._on_leave)

        if command:
            self.bind("<Button-1>", lambda _e: command())
            self._lbl.bind("<Button-1>", lambda _e: command())
        self._tip_item = Tooltip(self, tip if tip else "")
        self._tip_lbl = Tooltip(self._lbl, tip if tip else "")

    def set_tip(self, tip: str, text: str = "") -> None:
        self._tip = tip
        self._text = text
        if hasattr(self, "_tip_item"):
            self._tip_item.text = tip
        if hasattr(self, "_tip_lbl"):
            self._tip_lbl.text = tip
    def cget(self, attr: str):
        if attr == "text":
            return self._text
        return super().cget(attr)

    def configure(self, cnf=None, **kw):
        if cnf == "text":
            return self._text
        if "text" in kw:
            self._text = kw.pop("text")
        if kw:
            super().configure(**kw)

    config = configure

    def _on_enter(self, _e) -> None:
        if self._command and self._img_hov is not None:
            self._lbl.configure(image=self._img_hov)

    def _on_leave(self, e) -> None:
        if e is not None:
            try:
                under = e.widget.winfo_containing(e.x_root, e.y_root)
                curr = under
                while curr is not None:
                    if curr == self:
                        return
                    curr = getattr(curr, "master", None)
            except Exception:
                pass
        if self._img_dim is not None:
            self._lbl.configure(image=self._img_dim)

class StatusBar:
    """底部 22px 状态栏（STATUS_BG #303030 与卡片同阶，明度跳变即分隔——顶边线已删）。

    左侧：● 分析状态（就绪/分析中/已加载 N 会话；状态点允许语义色——状态栏是状态本体）
    右侧：VS Code 风格纯图标快捷入口群（一堆小 icon，无文字赘述，悬停微圆角高亮，点击呼出二级弹窗）
    """

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self.frame = tk.Frame(parent, bg=theme.STATUS_BG, height=22)
        self.frame.pack(side="bottom", fill="x")
        self.frame.pack_propagate(False)

        inner = tk.Frame(self.frame, bg=theme.STATUS_BG)
        inner.pack(fill="both", expand=True, padx=theme.PAD_M)

        # 左侧：分析状态点 + 文本
        left_box = tk.Frame(inner, bg=theme.STATUS_BG)
        left_box.pack(side="left", fill="y")
        self._dot = tk.Label(left_box, text="●", bg=theme.STATUS_BG, fg=theme.SUCCESS,
                             font=theme.FONT_STAT)
        self._dot.pack(side="left", padx=(0, 4))
        self._status_lbl = tk.Label(left_box, text="就绪", bg=theme.STATUS_BG,
                                    fg=theme.FG, font=theme.FONT_STAT)
        self._status_lbl.pack(side="left")

        # 右侧：状态栏纯图标快捷入口群（从右向左有序挂载）
        right_box = tk.Frame(inner, bg=theme.STATUS_BG)
        right_box.pack(side="right", fill="y")
        from tcer import __version__

        self._version_lbl = tk.Label(
            right_box, text=f"v{__version__}", bg=theme.STATUS_BG,
            fg=theme.MUTED, font=theme.FONT_STAT, cursor=CLICK_CURSOR)
        self._version_lbl.pack(side="right", padx=(8, 0))
        self._version_lbl.bind("<Button-1>", lambda _e: getattr(self.controller, "check_for_update", lambda **k: None)(silent=False))
        self._version_lbl.bind("<Enter>", lambda _e: self._version_lbl.configure(fg=theme.FG_WHITE))
        self._version_lbl.bind("<Leave>", lambda _e: self._version_lbl.configure(fg=theme.MUTED))
        Tooltip(self._version_lbl, f"TCER v{__version__} · 点击检查更新")

        self._tools_item = StatusIconBtn(
            right_box, "tools", command=lambda: getattr(self.controller, "show_tool_calls", lambda: None)(),
            tip="工具调用统计 · 点击查看调用次数与分布")
        self._tools_item.pack(side="right", padx=2, fill="y")

        self._cost_item = StatusIconBtn(
            right_box, "cost", command=lambda: getattr(self.controller, "show_cost_breakdown", lambda: None)(),
            tip="消耗金额明细 · 点击查看成本与各模型效率")
        self._cost_item.pack(side="right", padx=2, fill="y")

        self._turns_item = StatusIconBtn(
            right_box, "session", command=lambda: getattr(self.controller, "show_session_timeline", lambda: None)(),
            tip="会话时序与耗时 · 点击查看时间线")
        self._turns_item.pack(side="right", padx=2, fill="y")

        self._model_item = StatusIconBtn(
            right_box, "model", command=lambda: getattr(self.controller, "show_models", lambda: None)(),
            tip="主模型与数据源 · 点击查看模型使用详情")
        self._model_item.pack(side="right", padx=2, fill="y")

        # 兼容原有属性名
        self._metrics_lbl = self._turns_item
        self._meta_lbl = self._model_item

    def set_status(self, text: str, *, fg: str | None = None) -> None:
        clean_text = text.strip()
        self._status_lbl.configure(text=clean_text)
        if "错" in clean_text or "失败" in clean_text:
            dot_color = theme.ERROR
        elif "中…" in clean_text or "加载" in clean_text or "分析" in clean_text:
            dot_color = theme.WARNING
        else:
            dot_color = theme.SUCCESS
        self._dot.configure(fg=dot_color)
        if fg == theme.ACCENT:
            fg = theme.CYAN  # 防止暗蓝色在深灰底上对比度不足
        self._status_lbl.configure(fg=fg or theme.FG)

    def update_badges(self, *, model_text: str = "",
                      turns_text: str = "", cost_text: str = "",
                      tools_text: str = "", **_kw) -> None:
        self._model_item.set_tip(f"主模型: {model_text} · 点击查看详情" if model_text else "模型使用详情", text=model_text)
        self._turns_item.set_tip(f"{turns_text} · 点击查看时间线" if turns_text else "会话时间线", text=turns_text)
        self._cost_item.set_tip(f"总消耗: {cost_text} · 点击查看成本明细" if cost_text else "成本明细", text=cost_text)
        self._tools_item.set_tip(f"工具调用: {tools_text} · 点击查看统计" if tools_text else "工具调用统计", text=tools_text)

    def clear_badges(self) -> None:
        self.update_badges(model_text="", turns_text="", cost_text="", tools_text="")

    def set_meta(self, text: str, icon=None) -> None:
        self._model_item.set_tip(f"主模型: {text} · 点击查看详情" if text else "模型使用详情", text=text)

    def set_metrics(self, text: str, icon=None) -> None:
        self._turns_item.set_tip(f"{text} · 点击查看时间线" if text else "会话时间线", text=text)
class ActivityBar:
    """VS Code 风格活动栏（44px 宽，停靠在主工作台最左侧）。

    上半部分：核心视角导航
    - ui-project: 项目视角
    - ui-session: 会话视角
    - ui-layers: 全局项目聚合
    下半部分：系统功能工具
    - ui-tools: 实用工具菜单
    - ui-sparkle: LLM 动力学/复盘设置
    - ui-export: 导出菜单
    """

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self.frame = tk.Frame(parent, bg=theme.ACTIVITY_BG, width=44)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)

        # 1px 右边框
        border = tk.Frame(self.frame, bg=theme.BORDER, width=1)
        border.pack(side="right", fill="y")

        inner = tk.Frame(self.frame, bg=theme.ACTIVITY_BG)
        inner.pack(side="left", fill="both", expand=True)

        self._top_container = tk.Frame(inner, bg=theme.ACTIVITY_BG)
        self._top_container.pack(side="top", fill="x", pady=4)

        self._bot_container = tk.Frame(inner, bg=theme.ACTIVITY_BG)
        self._bot_container.pack(side="bottom", fill="x", pady=4)

        self._items: dict[str, tuple] = {}          # 全部项（hover 用）
        self._nav_keys: set[str] = set()            # 视角组（切换侧栏内容）
        self._page_keys: set[str] = set()           # 页组（切换主区内容）
        self._active_key: str = "project"
        self._active_page: str | None = None

        # 上半区：主视图页组（每图标切换主区一页）
        for i, (label, icon_name) in enumerate((
                ("指标看板", "dashboard"), ("模型对比", "model"), ("效率榜", "rank"),
                ("趋势分析", "trend"), ("项目聚合", "layers"), ("LLM 报告", "sparkle"),
                ("术语库", "book"))):
            self._add_nav_item(f"page:{i}", icon_name, label,
                               lambda idx=i: self.controller._nb.select(idx))

        # 下半区工具图标：先导出，最底为设置（标准 VS Code 齿轮在最底规范）
        from tcer.core import upload_config
        if upload_config.upload_enabled():
            self._add_tool_item("upload", "upload", "上传数据至团队后端", self._on_click_upload)
        self._add_tool_item("export", "export", "导出数据与报告", self._on_click_export)
        self._add_tool_item("settings", "settings", "设置", self._on_click_tools)
        self.set_page_active(0)
    def _add_nav_item(self, key: str, icon_name: str, tip: str, command) -> None:
        item = tk.Frame(self._top_container, bg=theme.ACTIVITY_BG, height=44, cursor=CLICK_CURSOR)
        item.pack(fill="x", pady=2)
        item.pack_propagate(False)

        rail = tk.Frame(item, bg=theme.ACTIVITY_BG, width=2)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)

        img_active = ui_icon(item, icon_name, opacity=1.0, size=22)
        img_dim = ui_icon(item, icon_name, opacity=0.65, size=22)
        img_hov = ui_icon(item, icon_name, opacity=0.90, size=22)

        lbl = tk.Label(item, image=img_dim, bg=theme.ACTIVITY_BG, cursor=CLICK_CURSOR)
        lbl.image = img_dim
        lbl.pack(fill="both", expand=True)

        Tooltip(item, tip)
        Tooltip(lbl, tip)

        for w in (item, lbl):
            w.bind("<Button-1>", lambda _e: command(), add="+")
            w.bind("<Enter>", lambda _e, k=key: self._on_hover(k, True, _e), add="+")
            w.bind("<Leave>", lambda _e, k=key: self._on_hover(k, False, _e), add="+")
        self._items[key] = (item, rail, lbl, img_active, img_dim, img_hov)
        (self._page_keys if key.startswith("page:") else self._nav_keys).add(key)

    def _add_tool_item(self, key: str, icon_name: str, tip: str, command) -> None:
        item = tk.Frame(self._bot_container, bg=theme.ACTIVITY_BG, height=40, cursor=CLICK_CURSOR)
        item.pack(fill="x", pady=2)
        item.pack_propagate(False)

        rail = tk.Frame(item, bg=theme.ACTIVITY_BG, width=2)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)


        img_active = ui_icon(item, icon_name, opacity=1.0, size=22)
        img_dim = ui_icon(item, icon_name, opacity=0.65, size=22)
        img_hov = ui_icon(item, icon_name, opacity=0.90, size=22)

        lbl = tk.Label(item, image=img_dim, bg=theme.ACTIVITY_BG, cursor=CLICK_CURSOR)
        lbl.image = img_dim
        lbl.pack(fill="both", expand=True)

        Tooltip(item, tip)
        Tooltip(lbl, tip)
        for w in (item, lbl):
            w.bind("<Button-1>", lambda _e: command(item), add="+")
            w.bind("<Enter>", lambda _e, k=key: self._on_hover(k, True, _e), add="+")
            w.bind("<Leave>", lambda _e, k=key: self._on_hover(k, False, _e), add="+")

        self._items[key] = (item, rail, lbl, img_active, img_dim, img_hov)

    def _on_hover(self, key: str, is_hover: bool, event=None) -> None:
        if key == self._active_key or key == self._active_page:
            return
        if not is_hover and event is not None:
            try:
                under = event.widget.winfo_containing(event.x_root, event.y_root)
                curr = under
                box = self._items[key][0]
                while curr is not None:
                    if curr == box:
                        return  # 仍在此项内，不触发取消悬停
                    curr = getattr(curr, "master", None)
            except Exception:
                pass
        item, rail, lbl, img_active, img_dim, img_hov = self._items[key]
        if is_hover:
            lbl.configure(image=img_hov)
        else:
            lbl.configure(image=img_dim)

    def set_active(self, key: str) -> None:
        """视角组兼容桩（视角切换已移至侧栏顶栏）。"""
        self._active_key = key

    def set_page_active(self, idx: int) -> None:
        """页组激活（原顶部页签栏）：idx 为主区页序号 0-5。"""
        key = f"page:{idx}"
        if key not in self._items:
            return
        self._active_page = key
        for k in self._page_keys:
            self._paint(k, k == key)

    def _paint(self, key: str, active: bool) -> None:
        item, rail, lbl, img_active, img_dim, img_hov = self._items[key]
        rail.configure(bg=theme.FG_WHITE if active else theme.ACTIVITY_BG)
        lbl.configure(bg=theme.ACTIVITY_BG, image=img_active if active else img_dim)
    def _on_click_tools(self, widget) -> None:
        if hasattr(self.controller, "filter"):
            menu = FlatMenu(widget)
            self.controller.filter._build_tool_menu(menu)
            widget.update_idletasks()
            menu.tk_popup(widget.winfo_rootx() + widget.winfo_width(),
                          widget.winfo_rooty() + widget.winfo_height())

    def _on_click_sparkle(self, _widget) -> None:
        if hasattr(self.controller, "show_llm_config"):
            self.controller.show_llm_config()

    def _on_click_export(self, widget) -> None:
        if hasattr(self.controller, "filter"):
            menu = FlatMenu(widget)
            self.controller.filter._build_export_menu(menu)
            widget.update_idletasks()
            menu.tk_popup(widget.winfo_rootx() + widget.winfo_width(),
                          widget.winfo_rooty() + widget.winfo_height())

    def _on_click_upload(self, _widget) -> None:
        if hasattr(self.controller, "show_upload"):
            self.controller.show_upload()

    def get_upload_widget(self):
        """活动栏上传按钮 Frame，供成功气泡定位。"""
        it = self._items.get("upload")
        return it[0] if it else None
class FilterBar:
    """Integrated filter and actions controller (Zero-waste panel integration)."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self.parent = parent
        self.view_mode = controller.view_mode

        # 过滤状态
        self._task_display_names = {
            metrics.AUTO_TASK_TYPE: "自动",
            **{k: (v.get("name") or k) for k, v in metrics.TASK_CATEGORIES.items()},
        }
        default_task = metrics.DEFAULT_TASK_TYPE
        default_label = self._task_display_names.get(
            default_task, next(iter(self._task_display_names.values()), "代码创作"))
        self.task_var = tk.StringVar(value=default_label)
        self._task_reverse_map = {v: k for k, v in self._task_display_names.items()}

        self.source_var = tk.StringVar(value="全部")
        self._source_display_names = {
            "all": "全部",
            "claude": "Claude",
            "codex": "Codex",
            "opencode": "OpenCode",
            "grok": "Grok",
            "omp": "Oh My Pi",
            "pi": "Pi",
            "antigravity": "Antigravity",
        }
        self._source_reverse_map = {v: k for k, v in self._source_display_names.items()}

        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")
        self.since_var = tk.StringVar(value=today_str)
        self.until_var = tk.StringVar(value="")
        self._current_preset = "today"

        # 下拉胶囊引用
        self._src_capsule: tk.Frame | None = None
        self._src_lbl: tk.Label | None = None
        self._time_capsule: tk.Frame | None = None
        self._time_lbl: tk.Label | None = None

        # 兼容状态 Label（底层状态通信用）
        self.status = tk.Label(parent, text="就绪", bg=theme.BG, fg=theme.SECTION_ACCENT)

    def mount_view_switcher(self, parent_frame) -> None:
        """在侧边栏顶栏挂载项目/会话视角切换圆角分段控件（取代原“资源管理器”文本）。"""
        self._view_btns: dict[str, tk.Widget] = {}
        self._view_pills: dict[str, tk.Widget] = {}
        self._view_icon_lbls: dict[str, tk.Widget] = {}

        seg_bg = tk.Frame(parent_frame, bg=theme.PANEL)
        seg_bg.pack(side="left", padx=(0, 4))

        _seg_icons = {
            "project": ui_icon(seg_bg, "project"),
            "session": ui_icon(seg_bg, "session"),
        }
        for label, val in [("项目", "project"), ("会话", "session")]:
            def _click(_w=None, v=val):
                self.controller.set_view_mode(v)

            pill = RoundedPill(seg_bg, text=label, icon=_seg_icons.get(val),
                               command=_click, width=58, height=22, radius=5,
                               bg=theme.PANEL)
            pill.pack(side="left", padx=1)
            self._view_pills[val] = pill
            self._view_btns[val] = pill

            tip = f"切换至{label}视角"
            Tooltip(pill, tip)

        self.update_view_btns()

    def update_view_btns(self) -> None:
        """根据当前 view_mode 刷新视角切换按钮高亮状态。"""
        if not hasattr(self, "_view_btns") or not self._view_btns:
            return
        current = self.view_mode.get()
        _sel_bg = {"session": theme.ACCENT, "project": theme.VIEW_PROJECT}
        for val, pill in self._view_btns.items():
            active = (val == current)
            fill = _sel_bg.get(val, theme.ACCENT) if active else theme.CONTROL_BG
            fg = theme.FG_WHITE if active else theme.MUTED
            pill.set_state(fill=fill, fg=fg)

    def mount_sidebar(self, parent_frame) -> None:
        """挂载数据来源与时间下拉胶囊至侧边栏顶栏（带抗锯齿圆角）。"""
        # 1. 右侧：数据来源下拉胶囊
        def _on_src_click(_widget):
            m = FlatMenu(self._src_capsule)
            for disp in self._source_display_names.values():
                def _pick(val=disp):
                    self.source_var.set(val)
                    self._src_capsule.set_text(f"{val} ▾")
                    self.controller.refresh_projects()
                m.add_command(disp, _pick)
            self._src_capsule.update_idletasks()
            m.tk_popup(self._src_capsule.winfo_rootx(),
                       self._src_capsule.winfo_rooty() + self._src_capsule.winfo_height())

        self._src_capsule = RoundedPill(parent_frame, text=f"{self.source_var.get()} ▾",
                                        command=_on_src_click, width=58, height=22, radius=5,
                                        bg=theme.PANEL)
        self._src_capsule.pack(side="right", padx=(2, 4))
        self._src_lbl = self._src_capsule
        Tooltip(self._src_capsule, "切换会话数据来源 (全部 / Claude / Codex / OpenCode / Grok / Oh My Pi / Pi)")

        # 2. 紧随来源左侧：时间下拉胶囊
        def _on_time_click(_widget):
            m = FlatMenu(self._time_capsule)
            for disp, preset in (("今天", "today"), ("本周", "week"), ("本月", "month"), ("全部", "all")):
                def _pick(p=preset):
                    self._set_preset(p)
                m.add_command(disp, _pick)
            self._time_capsule.update_idletasks()
            m.tk_popup(self._time_capsule.winfo_rootx(),
                       self._time_capsule.winfo_rooty() + self._time_capsule.winfo_height())

        _disp_names = {"today": "今天", "week": "本周", "month": "本月", "all": "全部"}
        cur_disp = _disp_names.get(self._current_preset, "今天")
        self._time_capsule = RoundedPill(parent_frame, text=f"{cur_disp} ▾",
                                         command=_on_time_click, width=58, height=22, radius=5,
                                         bg=theme.PANEL)
        self._time_capsule.pack(side="right", padx=(2, 4))
        self._time_lbl = self._time_capsule
        Tooltip(self._time_capsule, "筛选会话时间范围 (今天 / 本周 / 本月 / 全部)")

    def _set_preset(self, preset: str) -> None:
        from datetime import datetime, timedelta
        today = datetime.now()
        _disp_names = {"today": "今天", "week": "本周", "month": "本月", "all": "全部"}
        disp = _disp_names.get(preset, "今天")
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
        elif preset == "all":
            self.since_var.set("")
            self.until_var.set("")
        self._current_preset = preset
        if self._time_lbl is not None:
            self._time_lbl.set_text(f"{disp} ▾")
        self.controller.apply_time_filter()

    def _build_tool_menu(self, menu) -> None:
        c = self.controller
        menu.add_command(label="项目总览", command=c.show_project_overview)
        menu.add_command(label="同模型跨源对照", command=c.show_cross_source_compare)
        menu.add_command(label="项目画像", command=c.show_project_profile)
        menu.add_command(label="工具序列", command=c.show_tool_sequence)
        menu.add_command(label="会话时间线", command=c.show_session_timeline)
        menu.add_command(label="会话对比", command=c.show_session_compare)
        menu.add_separator()
        menu.add_command(label="LLM 设置…", command=c.show_llm_config)
        menu.add_command(label="计算个人基准…", command=c.compute_baselines)
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

    def _build_export_menu(self, menu) -> None:
        for label, fmt in (("项目报告 (HTML)", "html"), ("项目报告 (Markdown)", "md"),
                           ("项目数据 (JSON)", "json"), ("项目数据 (CSV)", "csv")):
            menu.add_command(label=label, command=lambda f=fmt: self.controller.export(f))
        menu.add_separator()
        for label, fmt in (("当前会话报告 (HTML)", "html"), ("当前会话报告 (Markdown)", "md"),
                           ("当前会话数据 (JSON)", "json")):
            menu.add_command(label=label,
                             command=lambda f=fmt: self.controller.export(f, scope="session"))

    def _on_task_type_change(self, event=None) -> None:
        """任务类型变化时的回调（菜单 command 无 event，故可选）"""
        # task_var 存储的是中文名称，直接触发重新分析
        self.controller.reanalyze()

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
        """恢复上次的来源/任务类型筛选（在首次 refresh_projects 之前调用）。"""
        src = prefs.get("source")
        if src in self._source_display_names:
            self.source_var.set(self._source_display_names[src])
        tt = prefs.get("task_type")
        if tt in self._task_display_names:
            self.task_var.set(self._task_display_names[tt])
        # 时间不恢复——启动固定为「今天」预设（until 恒空，无 UI 入口）。

    def set_status(self, text: str, *, fg: str | None = None) -> None:
        self.status.config(text=text, fg=fg or theme.SECTION_ACCENT)
        # 可见反馈走底部状态栏（self.status 兼容保留，测试直读）
        if hasattr(self.controller, "status_bar") and self.controller.status_bar is not None:
            self.controller.status_bar.set_status(text, fg=fg)

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

        tk.Frame(col, bg=theme.BORDER, height=1).pack(fill="x")
        header = tk.Frame(col, bg=theme.SECTION_HEADER_BG, height=28)
        header.pack(fill="x", pady=(2, 0))
        header.pack_propagate(False)
        _pi = ui_icon(header, "folder")
        if _pi is not None:
            tk.Label(header, image=_pi, bg=theme.SECTION_HEADER_BG).pack(side="left", padx=(6, 2))
        self.title_label = tk.Label(header, text="项目", bg=theme.SECTION_HEADER_BG, fg=theme.FG,
                                    font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        self.title_label.pack(side="left", padx=(theme.PAD_XS, 0))
        self._count_badge = RoundedPill(
            header, text="0", width=28, height=18, radius=4,
            fill=theme.CONTROL_BG, bg=theme.SECTION_HEADER_BG, fg=theme.MUTED,
            command=None)
        self._count_badge.pack(side="left", padx=(4, 0))
        self._count_tip = Tooltip(self._count_badge, "")
        self.count_label = tk.Label(header, text="项目")

        self._filter_var = tk.StringVar(value="")
        self._search_box = RoundedSearchBox(
            header, textvariable=self._filter_var, icon=ui_icon(header, "search"),
            width=110, height=22, radius=5, bg=theme.SECTION_HEADER_BG)
        self._search_box.pack(side="right", padx=(2, 4))
        self.search_entry = self._search_box.entry
        Tooltip(self._search_box.entry, "按项目名称 / 路径 / 来源过滤 · Esc 清空")
        Tooltip(self._search_box, "按项目名称 / 路径 / 来源过滤 · Esc 清空")
        self._filter_var.trace_add("write", lambda *_a: self._render_filter())
        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=(1, 4))
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
        self._render_filter()
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
                    bg=theme.PANEL, padx=1, pady=1)
        name = project_label(project_dir)
        label = project_source_label(project_dir)
        if is_empty:
            name += " （无会话）"
        fg = theme.MUTED if is_empty else theme.FG
        icon = source_icon(card.frame, project_icon_key(project_dir))

        # VS Code 紧凑文件树行 (height 24px)
        row = tk.Frame(card.frame, bg=theme.PANEL, height=24)
        row.pack(fill="x", padx=(4, 6))
        row.pack_propagate(False)

        bindees = [row]
        if icon is not None:
            img_lbl = tk.Label(row, image=icon, bg=theme.PANEL)
            img_lbl.pack(side="left", padx=(2, 6))
            Tooltip(img_lbl, label)
            bindees.append(img_lbl)

        name_lbl = tk.Label(row, text=name, bg=theme.PANEL, fg=fg,
                            font=theme.FONT_UI, anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)
        bindees.append(name_lbl)

        drive = project_drive(project_dir)
        if drive:
            drive_lbl = tk.Label(row, text=f"{drive.upper()}:", bg=theme.PANEL,
                                 fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")
            drive_lbl.pack(side="right", padx=(4, 0))
            bindees.append(drive_lbl)

        for w in bindees:
            card.bind_to(w)
        return card

    def _on_card_click(self, card, idx, is_empty):
        if is_empty:
            return  # 空项目不响应点击
        self._select(card, idx)
    def _select(self, card, idx, *, notify=True):
        if self._selected is not None:
            self._selected.set_state_rail(None)   # 清选中蓝条，防残留
            self._selected.set_selected(False)
        self._selected = card
        self._selected_idx = idx
        card.set_state_rail(theme.ACCENT)
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
        self._render_filter()
        if self._selected_idx is not None and self._selected_idx in self._hidden:
            if self._selected is not None:
                self._selected.set_selected(False)
            self._selected = None
            self._selected_idx = None

    def _refresh_count_label(self) -> None:
        n = len(self._projects)
        h = len(self._hidden)
        if h:
            self.count_label.config(text=f"项目（{n - h}）（隐藏 {h}）")
        else:
            self.count_label.config(text=f"项目（{n}）")

    def _render_filter(self) -> None:
        needle = self._filter_var.get().strip().casefold()
        tokens = needle.split()
        n_matched = 0
        n_vis = 0
        for idx, (p, card) in enumerate(zip(self._projects, self._cards)):
            if idx in self._hidden:
                card.frame.pack_forget()
                continue
            n_vis += 1
            if tokens:
                p_text = f"{project_label(p)} {getattr(p, 'path', '')} {getattr(p, 'cwd', '')} {getattr(p, 'source', '')}".casefold()
                if not all(tok in p_text for tok in tokens):
                    card.frame.pack_forget()
                    continue
            card.frame.pack(fill="x", padx=1, pady=1)
            n_matched += 1

        n_total = len(self._projects)
        h = len(self._hidden)
        if tokens:
            self._count_badge.set_text(f"{n_matched}/{n_vis}")
            self._count_tip.text = f"搜索匹配 {n_matched} 项 / 当前活跃 {n_vis} / 总计 {n_total} 个项目"
        else:
            self._count_badge.set_text(str(n_vis))
            self._count_tip.text = f"当前活跃 {n_vis} 项 / 隐藏 {h} 项 / 总计 {n_total} 个项目"

        self._refresh_count_label()
        self.scroll.update_scroll()
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

        tk.Frame(col, bg=theme.BORDER, height=1).pack(fill="x")
        header = tk.Frame(col, bg=theme.SECTION_HEADER_BG, height=28)
        header.pack(fill="x", pady=(2, 0))
        header.pack_propagate(False)
        _hi = ui_icon(header, "session")
        if _hi is not None:
            tk.Label(header, image=_hi, bg=theme.SECTION_HEADER_BG).pack(side="left", padx=(6, 2))
        self.title_label = tk.Label(header, text="会话", bg=theme.SECTION_HEADER_BG, fg=theme.FG,
                                    font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        self.title_label.pack(side="left", padx=(theme.PAD_XS, 0))
        self._count_badge = RoundedPill(
            header, text="0", width=28, height=18, radius=4,
            fill=theme.CONTROL_BG, bg=theme.SECTION_HEADER_BG, fg=theme.MUTED,
            command=None)
        self._count_badge.pack(side="left", padx=(4, 0))
        self._count_tip = Tooltip(self._count_badge, "")
        self.count_label = tk.Label(header, text="会话")

        # 搜索框与红旗过滤（带抗锯齿圆角底）
        self._filter_var = tk.StringVar(value="")
        _search_box = RoundedSearchBox(header, textvariable=self._filter_var,
                                       icon=ui_icon(header, "search"),
                                       width=110, height=22, radius=5,
                                       bg=theme.SECTION_HEADER_BG)
        _search_box.pack(side="right", padx=(2, 4))
        self.search_entry = _search_box.entry  # Ctrl+F 全局聚焦入口
        Tooltip(_search_box.entry, "按标题 / 会话 ID / 模型 过滤（实时）· Ctrl+F 聚焦")
        Tooltip(_search_box, "按标题 / 会话 ID / 模型 过滤（实时）· Ctrl+F 聚焦")
        self._flag_only = tk.BooleanVar(value=False)
        self._ff_img = {"off": ui_icon(header, "flag"), "on": ui_icon(header, "flag-on")}
        _ff0 = self._ff_img["off"]
        if _ff0 is not None:
            self._flag_filter = tk.Label(header, image=_ff0, bg=theme.SECTION_HEADER_BG, cursor=CLICK_CURSOR)
            self._flag_filter.image = _ff0
        else:
            self._flag_filter = tk.Label(header, text="旗", bg=theme.SECTION_HEADER_BG,
                                         fg=theme.MUTED, font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        self._flag_filter.pack(side="right", padx=(4, 2))
        self._flag_filter.bind("<Button-1>", lambda _e: self._toggle_flag_only())
        if _ff0 is not None:
            _ff_hov = ui_icon(header, "flag", opacity=1.0)
            self._flag_filter.bind("<Enter>", lambda _e: self._flag_filter.configure(
                image=self._ff_img["on"] if self._flag_only.get() else _ff_hov))
            self._flag_filter.bind("<Leave>", lambda _e: self._flag_filter.configure(
                image=self._ff_img["on"] if self._flag_only.get() else _ff0))
        Tooltip(self._flag_filter, "只看红旗会话")
        self._filter_var.trace_add("write", lambda *_a: self._render())
        self._all_reports: list = []
        # 当前项目下被置顶 / 标红的 sid 集合（由 controller 下发，排序与卡片图标用）。
        self._pinned: set[str] = set()
        self._flagged: set[str] = set()

        sf = ScrollFrame(col, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True, padx=6, pady=(1, 4))
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
        if flag_only and (r.meta.session_id or r.meta.path.stem) not in self._flagged:
            return True
        if not needle:
            return False
        tokens = needle.split()
        searchable_parts = [
            r.meta.title or "",
            r.meta.session_id or "",
            r.meta.path.stem or "",
            r.task_type or "",
            r.meta.git_branch or "",
            *(r.usage.models or ()),
            *(r.usage.per_model.keys() if hasattr(r.usage, "per_model") and r.usage.per_model else ()),
        ]
        text_corpus = " ".join(searchable_parts).casefold()
        for tok in tokens:
            if tok not in text_corpus:
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
            hint = ("无匹配会话，按 Esc 清空搜索框"
                    if (needle or flag_only)
                    else "该项目暂无会话\n（或尚未完成分析）")
            self._empty_hint = tk.Label(self.container, text=hint,
                                        bg=theme.PANEL, fg=theme.MUTED,
                                        font=theme.FONT_UI, justify="center",
                                        pady=theme.PAD_L * 2)
            self._empty_hint.pack(padx=theme.PAD_M)
        n_all = len(self._all_reports)
        n_cur = len(self._reports)
        if needle or flag_only:
            self._count_badge.set_text(f"{n_cur}/{n_all}")
            self.count_label.config(text=f"会话（{n_cur}/{n_all}）")
            self._count_tip.text = f"过滤匹配 {n_cur} 项 / 共 {n_all} 个会话"
        else:
            self._count_badge.set_text(str(n_all))
            self.count_label.config(text=f"会话（{n_all}）")
            self._count_tip.text = f"共 {n_all} 个会话"
        self.scroll.update_scroll(reset=reset)

    def _make_card(self, r):
        sid = r.meta.session_id or r.meta.path.stem
        title = r.meta.title or "(无标题)"
        card = Card(self.container,
                    on_click=lambda c, s=sid: self._select(c, s),
                    on_right_click=lambda e, _r=r, _s=sid: self._on_right_click(e, _r, _s),
                    bg=theme.PANEL, padx=1, pady=1)

        # 左侧状态高光条 (State Rail)
        rail_color = None
        tier = getattr(r, "tier", None)
        if tier in ("优秀", "良好"):
            rail_color = theme.SUCCESS
        elif tier in ("待改进", "低效"):
            rail_color = theme.WARNING
        card.set_state_rail(rail_color)

        time_ms = r.usage.ended_at or r.usage.started_at
        time_str = fmt_dt(time_ms, FMT_SHORT_MINUTE) if time_ms else "-"

        # Row 1: 状态指示点 + 时间（左） + 标记按钮（右，常驻静止防晃动）
        row1 = tk.Frame(card.frame, bg=card._bg)
        row1.pack(fill="x", padx=6, pady=(3, 1))
        dot_color = theme.SUCCESS if tier in ("优秀", "良好") else (
                    theme.WARNING if tier in ("待改进", "低效") else theme.MUTED)
        dot_lbl = tk.Label(row1, text="●", bg=card._bg, fg=dot_color,
                           font=theme.FONT_UI_SMALL)
        dot_lbl.pack(side="left", padx=(0, 4))
        card.track_bg(dot_lbl)
        dot_lbl.bind("<Button-1>", lambda _e: self._select(card, sid))

        t_lbl = tk.Label(row1, text=time_str, bg=card._bg, fg=theme.MUTED,
                         font=theme.FONT_MONO, anchor="w")
        t_lbl.pack(side="left", padx=(0, 4))

        marks_row = tk.Frame(row1, bg=card._bg)
        card.track_bg(marks_row)
        self._mark_icon(marks_row, card, sid, "pin",
                        is_on=sid in self._pinned, tip="置顶 / 取消置顶")
        self._mark_icon(marks_row, card, sid, "flag",
                        is_on=sid in self._flagged, tip="红旗 / 取消红旗")
        is_marked = (sid in self._pinned) or (sid in self._flagged)
        if is_marked:
            marks_row.pack(side="right", padx=(2, 0))
        else:
            def _on_enter_marks(_e):
                marks_row.pack(side="right", padx=(2, 0))
            def _on_leave_marks(_e):
                if (sid in self._pinned) or (sid in self._flagged):
                    return
                try:
                    px, py = card.frame.winfo_pointerx(), card.frame.winfo_pointery()
                    fx, fy = card.frame.winfo_rootx(), card.frame.winfo_rooty()
                    if fx <= px < fx + card.frame.winfo_width() \
                            and fy <= py < fy + card.frame.winfo_height():
                        return
                except Exception:
                    pass
                marks_row.pack_forget()
            card.frame.bind("<Enter>", _on_enter_marks, add="+")
            card.frame.bind("<Leave>", _on_leave_marks, add="+")

        # Row 2: 会话标题（独立全宽行，排版舒展，字号适中，绝不拥挤截断）
        row2 = tk.Frame(card.frame, bg=card._bg)
        row2.pack(fill="x", padx=6, pady=(1, 2))
        card.track_bg(row2)
        title_disp = format_card_title(title, 38)
        ti_lbl = tk.Label(row2, text=title_disp, bg=card._bg, fg=theme.FG,
                          font=theme.FONT_UI_SMALL, anchor="w")
        ti_lbl.pack(side="left", fill="x", expand=True)

        # Row 3: 摘要行——左「模型短名 · 时长」，右成本金额
        from tcer.core.format import fmt_duration_ms
        model_name = dominant_model_label(r.usage)
        dur = fmt_duration_ms(getattr(r.usage, "session_duration_ms", 0))
        sum_parts = [p for p in (model_name, dur) if p and p != "-"]
        sum_row = tk.Frame(card.frame, bg=card._bg)
        sum_row.pack(fill="x", padx=6, pady=(1, 3))
        card.track_bg(sum_row)
        sum_lbl = tk.Label(sum_row, text=" · ".join(sum_parts) or "-",
                           bg=card._bg, fg=theme.MUTED,
                           font=theme.FONT_UI_SMALL, anchor="w")
        sum_lbl.pack(side="left", fill="x", expand=True)

        cost_fg = theme.WARNING if r.cost >= 50.0 else theme.FG
        cost_lbl = tk.Label(sum_row, text=f"${r.cost:.2f}", bg=card._bg, fg=cost_fg,
                            font=theme.FONT_VALUE, anchor="e")
        cost_lbl.pack(side="right", padx=(4, 0))

        # Tooltip：完整标题 + ID + 详细摘要（回合/LOC/返工在此悬浮可见）
        turns = r.usage.assistant_msgs
        net_loc = r.net_loc or 0
        loc_str = f"{net_loc:+,} 行"
        churn = r.churn_ratio or 0.0
        rework_str = ("极少返工" if churn < 0.05
                      else f"{'高' if churn >= 0.20 else ''}返工 {churn*100:.0f}%")
        detail = " · ".join(filter(None, [
            dur if dur != "-" else "", f"{turns:,} 回合" if turns else "",
            loc_str, rework_str]))
        tip_text = (f"{title}\nID: {sid}\n{detail}\n"
                    "双击查看会话详情，右键更多操作")
        Tooltip(card.frame, tip_text)
        Tooltip(ti_lbl, tip_text)
        Tooltip(sum_lbl, tip_text)

        card.bind_to(row1)
        card.bind_to(t_lbl)
        card.bind_to(row2)
        card.bind_to(ti_lbl)
        card.bind_to(sum_row)
        card.bind_to(sum_lbl)
        card.bind_to(cost_lbl)

        for w in (row1, t_lbl, row2, ti_lbl, sum_row, sum_lbl, cost_lbl):
            w.bind("<Double-Button-1>", lambda e, s=sid: self.controller.show_session_detail(s))
        return card

    def _mark_icon(self, parent, card, sid, kind, *, is_on, tip):
        """卡片右上角可点击标记图标：左键 toggle（不选中卡片），右键复用卡片菜单。

        kind 为 "pin"（置顶）/ "flag"（红旗）。激活态用彩色 ``<kind>-on`` 图标，
        未激活用灰色 ``<kind>`` 图标；缺资源回退到着色字符（置顶 ▾ / 红旗 ◆）。
        底色随卡片 hover/选中联动（track_bg），图标自身 hover 高亮、离开还原。
        """

        def _card_bg():
            """卡片当前有效底色（选中 > 悬浮 > 常态）。"""
            if card._selected:
                return theme.SEL_ROW_ACTIVE
            if card._hovered:
                return theme.HOVER_BG
            return card._bg

        effective_bg = _card_bg()
        img_normal = ui_icon(self.container, f"{kind}-on" if is_on else kind, opacity=1.0 if is_on else 0.6)
        img_hover = ui_icon(self.container, f"{kind}-on" if is_on else kind, opacity=1.0)
        if img_normal is not None:
            lbl = tk.Label(parent, image=img_normal, bg=effective_bg, cursor=CLICK_CURSOR)
            lbl.image = img_normal  # 防 GC
        else:
            ch = "▾" if kind == "pin" else "◆"
            fg = (theme.ACCENT if kind == "pin" else theme.ERROR) if is_on else theme.MUTED
            lbl = tk.Label(parent, text=ch, bg=effective_bg, fg=fg,
                           font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR)
        lbl.pack(side="left", padx=(2, 0))
        card.track_bg(lbl)   # 随卡片 hover/选中联动变色（不绑事件，保 toggle 语义）

        def toggle(_e):
            if kind == "pin":
                self.controller.toggle_session_pin(sid)
            else:
                self.controller.toggle_session_flag(sid)

        lbl.bind("<Button-1>", toggle)
        lbl.bind("<Button-3>", card._on_right_click)   # 右键仍走卡片菜单
        # 悬停仅提亮图标透明度，底色始终随卡片底保持无缝一致，绝无灰块色斑
        if img_hover is not None:
            lbl.bind("<Enter>", lambda _e: lbl.configure(image=img_hover))
            lbl.bind("<Leave>", lambda _e: lbl.configure(image=img_normal))
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

        # LLM 深度解读分组（独立一组，高阶洞察直达）
        _sparkle = ui_icon(self.container, "sparkle")
        _layers = ui_icon(self.container, "layers")
        menu.add_command(
            label="LLM 过程解读",
            command=lambda: self.controller.run_session_llm_interpret(report),
            image=_sparkle, compound="left",
        )
        menu.add_command(
            label="相空间动力学分析",
            command=lambda: self.controller.run_session_dynamics_analysis(report),
            image=_layers, compound="left",
        )
        _crosscheck = ui_icon(self.container, "crosscheck")
        menu.add_command(
            label="纠正信号交叉验证",
            command=lambda: self.controller.run_correction_crosscheck(report),
            image=_crosscheck, compound="left",
        )
        _book = ui_icon(self.container, "book")
        menu.add_command(
            label="术语考古",
            command=lambda: self.controller.run_session_terms_archaeology(report),
            image=_book, compound="left",
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
        readonly = report.meta.source in ("codex", "opencode", "grok", "omp", "pi", "antigravity")
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


# 简要版精简保留的核心高信息量指标白名单（其余在简要版中隐藏，完整版展示全量）
BRIEF_METRIC_KEYS: frozenset[str] = frozenset({
    # G1 会话概况（保留：请求数、开始时间、结束时间、持续时长、模型、工具调用、用户消息）
    "turns", "started", "last_time", "duration", "models", "tools", "user_msgs",
    # G2 Token 用量（保留：总 Token、输入、输出、缓存创建、缓存命中）
    "total_tokens", "input", "output", "cache_write", "cache_read",
    # G3 缓存效率（全量保留）
    "chr", "io_ratio", "caf", "cache_efficiency", "cache_write_ratio", "non_cached_input_ratio",
    # G4 代码产出与质量
    # 基础（保留：净增行、写入行、删除行、涉及文件）
    "net_loc", "added", "deleted", "files_touched",
    # 行为（保留：读写比、编辑占比、探索占比、Bash 占比）
    "read_write_ratio", "edit_ratio", "exploration_ratio", "bash_ratio",
    # 质量（保留：返工率、先读后写率、工具错误率）
    "churn", "read_before_write", "tool_error_rate",
    # G5 成本分析（保留：总成本、每百万Token成本、千行代码成本）
    "cost", "cost_per_mt", "cpe",
    # G6 综合评分（保留：TCER、综合效率分、评级、任务类型）
    "tcer", "score", "tier", "task_type",
})

class MetricPanel:
    """Right-column tab 1: the G1–G6 metric grid, built from metric_defs.GROUPS."""

    def __init__(self, parent, controller) -> None:
        self.controller = controller
        self._chips: dict[str, tuple[tk.Label, tk.Label]] = {}
        self._groups: list[_GroupState] = []
        self._cells: dict[str, MetricCell] = {}
        self._grids: list[_MetricGrid] = []
        self._full_mode: bool = False

        sf = ScrollFrame(parent, bg=theme.BG)
        sf.canvas.pack(fill="both", expand=True)
        self.container = sf.inner

        # 顶部概况指示条
        self._build_header_strip()

        # 6 大指标分类 (G1–G6，以 metric_defs.GROUPS 为唯一真理源)
        for group in GROUPS:
            self._build_group(group)

    def _build_header_strip(self) -> None:
        """34px 汇总条：会话标题、耗时/回合、支出、净增、综合效率分。"""
        h = tk.Frame(self.container, bg=theme.BG, height=34)
        h.pack(fill="x", padx=8, pady=(4, 2))
        h.pack_propagate(False)
        tk.Frame(h, bg=theme.BORDER, height=1).pack(side="bottom", fill="x")

        self._h_title = tk.Label(h, text="待选会话", bg=theme.BG, fg=theme.FG,
                                 font=theme.FONT_UI_BOLD)
        self._h_title.pack(side="left", padx=(12, 8))

        self._h_dur = tk.Label(h, text="耗时 -", bg=theme.BG, fg=theme.MUTED,
                               font=theme.FONT_UI)
        self._h_dur.pack(side="left", padx=theme.PAD_M)

        # 右侧：会话/项目上下文与环境信息记录徽标（非点击按钮，专注记录关键信息）
        # 模式切换胶囊：简要版（默认，隐藏空/不适用项）⟷ 完整版（展示全量槽位）
        self._mode_btn = RoundedPill(
            h, text="简要版", width=68, height=22, radius=4,
            fill=theme.CONTROL_BG, hover_fill=theme.HOVER_BG,
            bg=theme.BG, fg=theme.ACCENT, font=theme.FONT_UI_SMALL_BOLD,
            command=self._toggle_view_density)
        self._mode_btn.pack(side="right", padx=(3, 6))
        Tooltip(self._mode_btn, "点击切换：【简要版】仅展示有数据的指标；【完整版】展开全量指标槽位。")

        self._tag_time = RoundedPill(
            h, text="-", width=120, height=22, radius=4,
            fill=theme.CONTROL_BG, hover_fill=None, bg=theme.BG, fg=theme.MUTED,
            font=theme.FONT_MONO, command=None)
        self._tag_time.pack(side="right", padx=(3, 4))
        Tooltip(self._tag_time, "会话启动/完成时间")

        self._tag_task = RoundedPill(
            h, text="任务: -", width=95, height=22, radius=4,
            fill=theme.CONTROL_BG, hover_fill=None, bg=theme.BG, fg=theme.FG,
            command=None)
        self._tag_task.pack(side="right", padx=3)
        Tooltip(self._tag_task, "任务类型（TTAF 归一化基准类别）")

        self._tag_meta = RoundedPill(
            h, text="", width=105, height=22, radius=4,
            fill=theme.CONTROL_BG, hover_fill=None, bg=theme.BG, fg=theme.MUTED,
            command=None)
        self._tag_meta.pack(side="right", padx=3)
        Tooltip(self._tag_meta, "环境信息（Git 分支 / 子代理折叠 / 推理档位）")

        # 保持旧属性与测试兼容别名
        self._h_cost = tk.Label(h, text="$0.00")
        self._h_loc = tk.Label(h, text="+0 行")
        self._h_score_val = tk.Label(h, text="-")
        self._h_score_tier = tk.Label(h, text="")
        self._chips["_sum_token"] = (self._h_cost, self._h_cost)
        self._chips["_sum_code"] = (self._h_loc, self._h_loc)
        self._chips["_sum_prompt"] = (self._h_dur, self._h_dur)
        self._chips["_sum_agent"] = (self._h_title, self._h_title)
    def _build_group(self, group) -> None:
        gframe = tk.Frame(self.container, bg=theme.BG)
        gframe.pack(fill="x", padx=8, pady=(4, 2))

        header_bg = theme.GROUP_COLORS.get(group.id, theme.PANEL_2)
        collapsed = False  # 全部分类（含 G4 代码产出与质量）默认展开

        # 采用自绘抗锯齿圆角底 Canvas 替代生硬直角横梁
        header = tk.Canvas(gframe, height=28, bg=theme.BG, highlightthickness=0, bd=0, cursor=CLICK_CURSOR)
        header.pack(fill="x")

        def _redraw_hdr(_e=None, h=header, bg_col=header_bg):
            w = h.winfo_width()
            ht = h.winfo_height()
            if w < 10 or ht < 10:
                return
            h.delete("hdr_bg")
            img = get_rounded_rect_img(h, w, ht, 6, bg_col, theme.BG)
            if img is not None:
                h.create_image(0, 0, anchor="nw", image=img, tags="hdr_bg")
            else:
                h.create_rectangle(0, 0, w, ht, fill=bg_col, outline="", tags="hdr_bg")
            h.tag_lower("hdr_bg")

        header.bind("<Configure>", _redraw_hdr)

        arrow_lbl = tk.Label(header, text=f"{'▸' if collapsed else '▾'} {group.name}",
                             bg=header_bg, fg=theme.FG,
                             font=theme.FONT_UI_BOLD, anchor="w", cursor=CLICK_CURSOR)
        header.create_window(8, 14, anchor="w", window=arrow_lbl, tags="arrow_win")

        # 组头右侧摘要
        s_lbl = tk.Label(header, text="", bg=header_bg, fg=theme.MUTED,
                         font=theme.FONT_UI_SMALL, anchor="e", cursor=CLICK_CURSOR)
        header.create_window(header.winfo_reqwidth() - 8, 14, anchor="e", window=s_lbl, tags="sum_win")

        def _reposition_sum(e, h=header):
            h.coords("sum_win", e.width - 8, 14)
        header.bind("<Configure>", _reposition_sum, add="+")

        body = tk.Frame(gframe, bg=theme.BG)
        body.pack(fill="x", pady=(2, 0))

        if group.subgroups:
            for sg in group.subgroups:
                self._build_metric_grid(sg.metrics, sub_label=sg.name, parent=body)
        else:
            self._build_metric_grid(group.metrics, parent=body)

        gs = _GroupState(name=group.name, arrow=arrow_lbl, body=body, collapsed=collapsed)
        self._groups.append(gs)

        for w in (header, arrow_lbl, s_lbl):
            w.bind("<Button-1>", lambda e, s=gs: self._toggle_group(s))

        if collapsed:
            body.pack_forget()

    def _build_metric_grid(self, metrics, sub_label: str | None = None,
                           parent=None) -> None:
        parent = parent or self.container
        if sub_label:
            sub = tk.Frame(parent, bg=theme.PANEL, padx=8, pady=1)
            sub.pack(fill="x", pady=(1, 0))
            tk.Label(sub, text=f"· {sub_label}", bg=theme.PANEL, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")

        grid = tk.Frame(parent, bg=theme.PANEL, padx=4, pady=2)
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
            cell.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW, sticky="nsew", padx=2, pady=2)
            self._cells[metric.key] = cell
            self._chips[metric.key] = (cell.value, cell.approx_lbl or cell.value)
            cells.append(cell)

        for c in range(_PER_ROW):
            grid.grid_columnconfigure(c, weight=1)

        exp_row = len(metrics)
        expander = tk.Label(grid, text="", bg=theme.PANEL, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor=CLICK_CURSOR)
        expander.grid(row=exp_row, column=0, columnspan=_PER_ROW,
                      sticky="w", pady=(1, 0))
        expander.grid_remove()
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

        # 刷新顶部汇总条
        title = getattr(report.meta, "title", None) or getattr(report.meta, "session_id", "(无标题)")
        is_agg = getattr(report.meta, "session_id", "") == "(aggregate)"
        if is_agg:
            title = "项目全量汇总"
        u = report.usage
        turns = getattr(u, "assistant_msgs", getattr(u, "turns", 0))
        self._h_title.config(text=f"{title[:32]} · {turns} 回合")

        from tcer.core.format import fmt_duration_ms
        dur_ms = getattr(u, "session_duration_ms", 0)
        self._h_dur.config(text=f"耗时 {fmt_duration_ms(dur_ms)}" if dur_ms else "耗时 -")

        cost_val = getattr(report, "cost", 0.0) or 0.0
        self._h_cost.config(text=f"${cost_val:.2f}")

        net_loc = getattr(report, "net_loc", 0) or 0
        self._h_loc.config(text=f"{net_loc:+,} 行")

        score = getattr(report, "score", None)
        self._h_score_val.config(text=f"{score:.1f}" if score is not None else "-")

        # 刷新右侧上下文元数据记录胶囊
        if is_agg:
            n_sess = len(getattr(self.controller, "_current", None).reports) if getattr(self.controller, "_current", None) else 0
            self._tag_time.set_text(f"共 {n_sess} 个会话")
            self._tag_task.set_text("全量聚合")
            self._tag_meta.set_text(f"来源: {report.meta.source}")
        else:
            time_ms = u.started_at or u.ended_at
            time_str = fmt_dt(time_ms, "%Y-%m-%d %H:%M") if time_ms else "-"
            self._tag_time.set_text(time_str)

            task_name = metrics.TASK_CATEGORIES.get(report.task_type, {}).get("name", report.task_type or "自动")
            self._tag_task.set_text(f"任务: {task_name}")

            meta_parts = []
            if getattr(report.meta, "git_branch", None):
                meta_parts.append(f"分支: {report.meta.git_branch}")
            elif getattr(report, "subagent_count", 0) > 0:
                meta_parts.append(f"{report.subagent_count} 子代理")
            elif getattr(report.meta, "reasoning_effort", None):
                meta_parts.append(f"推理: {report.meta.reasoning_effort}")
            else:
                meta_parts.append(f"来源: {report.meta.source}")
            self._tag_meta.set_text(" · ".join(meta_parts))

    def clear(self) -> None:
        self._h_title.config(text="待选会话")
        self._h_dur.config(text="耗时 -")
        self._h_cost.config(text="$0.00")
        self._h_loc.config(text="+0 行")
        self._h_score_val.config(text="-")
        self._tag_time.set_text("-")
        self._tag_task.set_text("任务: -")
        self._tag_meta.set_text("")
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
        gs.arrow.config(text=f"{'▸' if gs.collapsed else '▾'} {gs.name}")
        if gs.collapsed:
            gs.body.pack_forget()
        else:
            gs.body.pack(fill="x")

    def _toggle_view_density(self) -> None:
        self._full_mode = not getattr(self, "_full_mode", False)
        if self._full_mode:
            self._mode_btn.set_text("完整版")
            self._mode_btn.set_fg(theme.WARNING)
        else:
            self._mode_btn.set_text("简要版")
            self._mode_btn.set_fg(theme.ACCENT)
        for state in self._grids:
            state.expanded = self._full_mode
            self._apply_grid(state)

    def _apply_grid(self, state: _MetricGrid) -> None:
        """Reflow one grid: hide non-brief or empty (「-」) cells in 简要版,
        or show all in 完整版."""
        full = getattr(self, "_full_mode", False) or state.expanded
        if full:
            shown = state.cells
            hidden = []
        else:
            shown = [c for c in state.cells
                     if c.key in BRIEF_METRIC_KEYS and c.var.get() not in ("-", UNSUPPORTED_LABEL)]
            hidden = [c for c in state.cells if c not in shown]

        for i, c in enumerate(shown):
            c.frame.grid(row=i // _PER_ROW, column=i % _PER_ROW,
                         sticky="nsew", padx=2, pady=2)
        for c in hidden:
            c.frame.grid_remove()

        # 彻底移除各网格底部的机械展开行，由顶部的【简要版/完整版】全局切换
        state.expander.grid_remove()
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
        self._grade_canvas = tk.Canvas(grade_sec.content, bg=theme.PANEL, height=36,
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
        self._sash_session = 380
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
        self._tree.column("rank",     width=36,  minwidth=28,  stretch=False, anchor="center")
        self._tree.column("session",  width=200, minwidth=140, stretch=True,  anchor="w")
        self._tree.column("score_val", width=64, minwidth=50, stretch=False, anchor="e")
        self._tree.column("delta",    width=64,  minwidth=48,  stretch=False, anchor="e")
        self._tree.column("tier",    width=56,  minwidth=46,  stretch=False, anchor="center")

        sb = ttk.Scrollbar(tree_sec.content, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")  # 常驻细条：先占右侧
        self._tree.pack(fill="both", expand=True)
        self._update_headings()  # 默认排序列（效率分降序）初始即显方向指示

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
                bg=theme.PANEL, fg=theme.WARNING, font=theme.FONT_UI,
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

        bar_w = min(w - 24, 640)
        bar_h = 8
        x0 = 12
        y0 = 6

        # 背景凹槽 (深灰底)
        c.create_rectangle(x0, y0, x0 + bar_w, y0 + bar_h, fill=theme.CONTROL_BG, outline="", width=0)

        cur_x = x0
        for g in grades_in_order:
            n = counts[g]
            if n == 0:
                continue
            seg_w = max(8, int((n / total) * bar_w))
            if cur_x + seg_w > x0 + bar_w:
                seg_w = x0 + bar_w - cur_x
            fill = theme.GRADE_HEX.get(g, theme.MUTED)
            if self._grade_filter and self._grade_filter != g:
                fill = theme.GRADE_DIM
            c.create_rectangle(cur_x, y0, cur_x + seg_w, y0 + bar_h, fill=fill, outline="")
            cur_x += seg_w

        # 底部精致交互式 Tier 药丸项
        pill_y = 22
        px = x0
        for g in grades_in_order:
            n = counts[g]
            dot_col = theme.GRADE_HEX.get(g, theme.MUTED)
            is_active = (self._grade_filter == g)
            txt_col = theme.FG_WHITE if is_active else (theme.FG if n > 0 else theme.MUTED)

            start_x = px - 4
            t_dot = c.create_text(px, pill_y, text="●", fill=dot_col, font=("Segoe UI", 8), anchor="w")
            t_txt = c.create_text(px + 12, pill_y, text=f"{g} {n}", fill=txt_col,
                                  font=theme.FONT_UI_SMALL_BOLD if is_active else theme.FONT_UI_SMALL,
                                  anchor="w")
            bbox = c.bbox(t_txt)
            end_x = bbox[2] + 4 if bbox else px + 50
            if is_active:
                bg_rect = c.create_rectangle(start_x, pill_y - 8, end_x, pill_y + 8,
                                             fill=theme.CONTROL_BG, outline=dot_col, width=1)
                c.tag_lower(bg_rect, t_dot)

            self._grade_rects.append((start_x, pill_y - 10, end_x, pill_y + 10, g))
            px = end_x + 16
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
        self._update_headings()
        self._rebuild_tree()

    def _update_headings(self) -> None:
        """表头排序方向指示（VS Code 式）：当前排序列标题尾缀 ▾ 降序 / ▴ 升序。

        注意排序键与 heading 列名的映射（"score" 键 ↔ "score_val" 列）。"""
        base = {"rank": "#", "session": "标题", "score": _SCORE_SHORT,
                "delta": "贡献Δ", "tier": "等级"}
        col_map = {"score": "score_val"}
        for sort_key, text in base.items():
            if sort_key == self._sort_col:
                text += " ▾" if self._sort_reverse else " ▴"
            self._tree.heading(col_map.get(sort_key, sort_key), text=text)

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
            SelectableLabel(card, text=f"▸ {rc.title}", bg=theme.PANEL, fg=theme.FG,
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
            # 数值中性（异常才着色）；好坏方向由 bar 上的彩色标记表达，图形比小字可读
            color = theme.VALUE_GOOD if val >= SCORE_AXIS_NEUTRAL else theme.VALUE_BAD
            val_lbl = tk.Label(row, text=format_axis(val), bg=theme.PANEL, fg=theme.VALUE_NEUTRAL,
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
            # 数值中性（异常才着色）；好坏方向由 bar 上的彩色标记表达，图形比小字可读
            color = theme.VALUE_GOOD if val >= SCORE_AXIS_NEUTRAL else theme.VALUE_BAD
            val_lbl = tk.Label(row, text=format_axis(val), bg=theme.PANEL, fg=theme.VALUE_NEUTRAL,
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
        "good": ("亮点", theme.VALUE_GOOD, "✓"),
        "drag": ("拖累项", theme.VALUE_BAD, "!"),
        "cost": ("金额", theme.VIEW_PROJECT, "￥"),  # 橙黄 ¥ 标记：花钱相关
        "tip": ("快速改进", theme.ACCENT, "→"),
    }
    _INSIGHT_ORDER = ("good", "drag", "cost", "tip")

    def _render_insight_items(self, parent, items) -> None:
        """把一组 Insight 按 亮点/拖累项/快速改进 分组渲染到 parent。

        每组一个可点击折叠的小标题（▼/▶ + 名称 + 计数）；亮点（good）
        默认折叠（先看问题、再看表扬）。折叠态存 self._insight_collapsed，跨选中保持。
        good/drag/tip 共用同一渲染（单会话与项目级都走这里）。
        """
        collapsed = getattr(self, "_insight_collapsed", None)
        if collapsed is None:
            collapsed = self._insight_collapsed = {"good": True}  # 亮点默认折叠
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

            # 分组标题（可点击折叠）：▼/▶ + 名称（N）
            header = tk.Frame(parent, bg=theme.PANEL, cursor=CLICK_CURSOR)
            header.pack(fill="x", pady=(8, 2))
            arrow = "▶" if is_collapsed else "▼"
            head_lbl = tk.Label(header, text=f"{arrow} {head_txt}（{len(group)}）",
                                bg=theme.PANEL, fg=color, font=theme.FONT_UI_BOLD,
                                anchor="w")
            head_lbl.pack(side="left", fill="x", expand=True)

            # 正文容器（折叠时 pack_forget）；左侧色条 + 缩进
            body = tk.Frame(parent, bg=theme.PANEL)
            for it in group:
                row = tk.Frame(body, bg=theme.PANEL)
                row.pack(fill="x", pady=(2, 3))
                # 左侧彩色竖条（色轨制：级别靠色轨，正文全中性）
                tk.Frame(row, bg=color, width=theme.RAIL_W).pack(side="left", fill="y")
                body_col = tk.Frame(row, bg=theme.PANEL)
                body_col.pack(side="left", fill="x", expand=True, padx=(8, 0))
                # 标题行：标记 + 结论（中性色——级别由色轨与分组标题承担）
                SelectableLabel(body_col, text=f"{mark} {it.title}", bg=theme.PANEL,
                                fg=theme.FG, font=theme.FONT_UI,
                                justify="left").pack(fill="x")
                if it.evidence:
                    SelectableLabel(body_col, text=it.evidence, bg=theme.PANEL,
                                    fg=theme.MUTED, font=theme.FONT_UI,
                                    justify="left").pack(fill="x", padx=(14, 0))
                if it.action:
                    SelectableLabel(body_col, text=f"→ {it.action}", bg=theme.PANEL,
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
                arr = "▶" if now else "▼"
                hl.config(text=f"{arr} {ht}（{n}）")
                if now:
                    b.pack_forget()
                else:
                    b.pack(fill="x", after=hd)
            header.bind("<Button-1>", _toggle)
            head_lbl.bind("<Button-1>", _toggle)
        if not rendered:
            tk.Label(parent, text="暂无可执行洞察。",
                     bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI,
                     anchor="w").pack(fill="x")

    def _build_insights_section(self, report) -> None:
        """会话视角「洞察与意见」：把 core.insights 的诊断分组渲染，让用户知道具体改什么。"""
        sec = CollapsibleSection(self._decomp_inner, "洞察与意见 (会话)",
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

    def __init__(self, parent, controller=None):
        self.parent = parent
        self._models: list = []
        self._groups: list[_GroupState] = []
        # 分组折叠状态（跨 update 保持）；所有分组默认展开
        self._group_collapsed: dict[str, bool] = {}

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
        """Model summary（多模型时附成本占比条与模型卡片组）。"""
        header = tk.Frame(self._container, bg=theme.PANEL_2, padx=10, pady=6)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="模型对比", bg=theme.PANEL_2, fg=theme.FG,
                 font=theme.FONT_HEADING, anchor="w").pack(side="left")
        n_mod = len(self._models)
        subtitle = f"共 {n_mod} 个模型对比" if n_mod > 1 else "单模型深度表现"
        tk.Label(header, text=f" · {subtitle}", bg=theme.PANEL_2, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL, anchor="w").pack(side="left")

        # Cost distribution bar：仅多模型时绘制（段色锚定各列，单模型是纯噪声）。
        total_cost = sum(mc.cost for mc in self._models)
        if total_cost > 0 and len(self._models) >= 2:
            bar = tk.Frame(self._container, bg=theme.PANEL, padx=6, pady=4)
            bar.pack(fill="x")
            canvas = tk.Canvas(bar, bg=theme.PANEL, height=18, highlightthickness=0)
            canvas.pack(fill="x")

            def draw_bar(_e=None):
                canvas.delete("all")
                w = canvas.winfo_width()
                if w < 10:
                    return
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    color = theme.CHART_PALETTE[i % len(theme.CHART_PALETTE)]
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    canvas.create_rectangle(x1, 0, x2, 18, fill=color, outline="")
                    rx += rw
                rx = 0.0
                for i, mc in enumerate(self._models):
                    rw = mc.cost / total_cost
                    x1 = int(rx * w)
                    x2 = int((rx + rw) * w)
                    cx = (x1 + x2) / 2
                    if x2 - x1 > 28:
                        canvas.create_text(cx, 9, text=mc.display_name,
                                           fill=theme.FG_WHITE,
                                           font=(theme.FONT_MONO_NAME, 8, "bold"))
                    rx += rw

            canvas.bind("<Configure>", draw_bar)
            canvas.after(10, draw_bar)

        # Summary deck: elevated model cards
        deck = tk.Frame(self._container, bg=theme.BG)
        deck.pack(fill="x", pady=(4, 4))
        for j, mc in enumerate(self._models):
            cell = tk.Frame(deck, bg=theme.PANEL_2, padx=12, pady=6)
            cell.pack(side="left", fill="both", expand=True, padx=4)

            row0 = tk.Frame(cell, bg=theme.PANEL_2)
            row0.pack(fill="x")
            if len(self._models) >= 2:
                dot_color = theme.CHART_PALETTE[j % len(theme.CHART_PALETTE)]
                tk.Label(row0, text="●", bg=theme.PANEL_2, fg=dot_color,
                         font=theme.FONT_UI_SMALL).pack(side="left", padx=(0, 4))
            name_lbl = tk.Label(row0, text=mc.display_name, bg=theme.PANEL_2, fg=theme.FG_WHITE,
                                font=theme.FONT_VALUE, anchor="w")
            name_lbl.pack(side="left")

            cost_str = model_display(mc, "m_cost")
            share_str = f" · 占比 {mc.cost_share * 100:.1f}%" if len(self._models) >= 2 else ""
            sub_lbl = tk.Label(cell, text=f"{cost_str} · {mc.session_count} 会话{share_str}",
                               bg=theme.PANEL_2, fg=theme.MUTED,
                               font=theme.FONT_UI_SMALL, anchor="w")
            sub_lbl.pack(anchor="w", pady=(2, 0))

            price_tip = _model_price_tip(mc)
            for w in (cell, row0, name_lbl, sub_lbl):
                Tooltip(w, price_tip)

    def _build_group(self, group) -> None:
        """Build one per-model metric group from a metric_defs.Group (SSOT)."""
        collapsed = self._group_collapsed.get(group.id, False)
        gframe = tk.Frame(self._container, bg=theme.BG)
        gframe.pack(fill="x", pady=(4, 0))

        # 宝石色映射
        GROUP_ACCENTS = {
            "M_TOK": theme.GROUP_COLORS["G2"],   # 宝石蓝
            "M_COST": theme.GROUP_COLORS["G5"],  # 琥珀金
            "M_EFF": theme.GROUP_COLORS["G3"],   # 青碧绿
            "M_QUAL": theme.GROUP_COLORS["G4"],  # 翡翠绿
        }
        accent_col = GROUP_ACCENTS.get(group.id, theme.ACCENT)

        header_bg = theme.PANEL_2
        header = tk.Frame(gframe, bg=header_bg, height=28, cursor=CLICK_CURSOR)
        header.pack(fill="x")
        header.pack_propagate(False)

        # 3px 宝石色左侧指示条
        bar = tk.Frame(header, bg=accent_col, width=3)
        bar.pack(side="left", fill="y")

        arrow_lbl = tk.Label(header, text=f"{'▸' if collapsed else '▾'} {group.name}",
                             bg=header_bg, fg=theme.FG,
                             font=theme.FONT_UI_BOLD, anchor="w", cursor=CLICK_CURSOR)
        arrow_lbl.pack(side="left", fill="y", padx=(8, 6))

        tk.Label(header, text=f"{len(group.metrics)} 项", bg=header_bg, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="right", padx=10)

        body = tk.Frame(gframe, bg=theme.PANEL)
        body.pack(fill="x")

        # 多模型时渲染专属表头（仅出现一次，不再每个分组重复罗列冗余模型名）
        if len(self._models) >= 2:
            th_row = tk.Frame(body, bg=theme.PANEL_2, height=24)
            th_row.pack(fill="x")
            th_row.pack_propagate(False)
            tk.Label(th_row, text="指标名称", bg=theme.PANEL_2, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL_BOLD, width=18, anchor="w").pack(side="left", padx=(12, 8))
            th_right = tk.Frame(th_row, bg=theme.PANEL_2)
            th_right.pack(side="right", fill="both", expand=True, padx=(8, 16))
            for j, mc in enumerate(self._models):
                lbl = tk.Label(th_right, text=mc.display_name, bg=theme.PANEL_2, fg=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD, anchor="e")
                lbl.grid(row=0, column=j, sticky="nsew", padx=4)
                th_right.grid_columnconfigure(j, weight=1)

        # 逐行渲染指标（带斑马纹 + 悬停全行高亮 + 单模型紧凑自适应对齐）
        for i, metric in enumerate(group.metrics):
            key = metric.key
            tip_text = model_tip(key)
            row_bg = theme.PANEL_2 if i % 2 == 1 else theme.PANEL

            row = tk.Frame(body, bg=row_bg, height=27)
            row.pack(fill="x")
            row.pack_propagate(False)

            name_lbl = tk.Label(row, text=metric.name, bg=row_bg, fg=theme.FG,
                                font=theme.FONT_UI_SMALL, width=18, anchor="w")
            name_lbl.pack(side="left", padx=(12, 8))
            if tip_text:
                Tooltip(name_lbl, tip_text)

            widgets_in_row = [row, name_lbl]

            # 计算最优值高亮（金色 VALUE_BEST）
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

            # 单模型 vs 多模型排版
            if len(self._models) == 1:
                mc = self._models[0]
                val = model_display(mc, key)
                val_lbl = tk.Label(row, text=val, bg=row_bg,
                                   fg=row_colors.get(0, theme.FG_WHITE),
                                   font=theme.FONT_VALUE, anchor="e")
                val_lbl.pack(side="right", padx=(8, 16))
                if tip_text:
                    Tooltip(val_lbl, tip_text)
                widgets_in_row.append(val_lbl)
            else:
                val_container = tk.Frame(row, bg=row_bg)
                val_container.pack(side="right", fill="both", expand=True, padx=(8, 16))
                widgets_in_row.append(val_container)
                for j, mc in enumerate(self._models):
                    val = model_display(mc, key)
                    val_lbl = tk.Label(val_container, text=val, bg=row_bg,
                                       fg=row_colors.get(j, theme.VALUE_NEUTRAL),
                                       font=theme.FONT_VALUE, anchor="e")
                    val_lbl.grid(row=0, column=j, sticky="nsew", padx=4)
                    val_container.grid_columnconfigure(j, weight=1)
                    if tip_text:
                        Tooltip(val_lbl, tip_text)
                    widgets_in_row.append(val_lbl)

            # 悬停全行高亮反馈
            def _bind_hover(r=row, wlist=widgets_in_row, default_bg=row_bg):
                def _enter(_e):
                    for w in wlist:
                        try:
                            w.configure(bg=theme.CONTROL_BG)
                        except tk.TclError:
                            pass
                def _leave(_e):
                    for w in wlist:
                        try:
                            w.configure(bg=default_bg)
                        except tk.TclError:
                            pass
                for w in wlist:
                    w.bind("<Enter>", _enter, add="+")
                    w.bind("<Leave>", _leave, add="+")
            _bind_hover()

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
        gs.arrow.config(text=f"{'▸' if gs.collapsed else '▾'} {gs.name}")
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
        # （与会话卡片同款：$ 两位小数、FONT_VALUE、异常才着色——≥$50 警示橙，平时中性）
        row1 = tk.Frame(card.frame, bg=theme.PANEL_2)
        row1.pack(fill="x", padx=theme.PAD_S, pady=(theme.PAD_S, 0))
        arrow = tk.Label(row1, text="▾" if expanded else "▸", bg=theme.PANEL_2,
                         fg=theme.MUTED, font=theme.FONT_UI_SMALL)
        arrow.pack(side="left", padx=(2, 4))
        card.bind_to(arrow)
        cost = t.get("cost") or 0.0
        cost_fg = theme.WARNING if cost >= 50.0 else theme.FG
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


def _dynamics_terminal_state(dyn: dict) -> str:
    """相空间动力学终态判定（共享 SSOT）。

    PhasePortraitWidget.render 的态势徽章与 LlmReportsView._resolve_dynamics_status
    的列表态势着色共用同一实现，杜绝两份分支逻辑漂移。返回四态键：
    'dirac'（精准收敛）/ 'escaped'（成功破局）/ 'trapped'（陷入泥潭）/
    'wandering'（方向发散）。
    """
    dyn = dyn or {}
    ctype = str(dyn.get("convergence_type") or "").lower()
    is_trapped = bool(dyn.get("attractor_trapped"))
    traj = dyn.get("trajectory") or []
    last_pt = traj[-1] if traj else {}
    last_evt = str(last_pt.get("event") or "").lower()
    last_vec = str(last_pt.get("vector") or "").lower()
    try:
        last_ds = metrics.ds_of(last_pt) if last_pt else 0.5
    except (ValueError, TypeError):
        last_ds = 0.5

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
        return "dirac"
    if is_escaped:
        return "escaped"
    if ctype == "trapped" or is_trapped:
        return "trapped"
    return "wandering"


# 终态四态 → 态势徽章文案/配色（与 _dynamics_terminal_state 键位一一对应）
_DYNAMIC_STATE_BADGES = {
    "dirac": ("精准收敛 · 达成目标", theme.SUCCESS, theme.DIRAC_CORE_BG),
    "escaped": ("成功破局 · 达成收敛", theme.SUCCESS, theme.DIRAC_CORE_BG),
    "trapped": ("陷入泥潭 · 循环死锁", theme.ERROR, theme.ERROR_TINT_BG),
    "wandering": ("方向发散 · 未能收敛", theme.WARNING, theme.WARN_TINT_BG),
}


class PhasePortraitWidget:
    """相空间收敛动力学相图（专属 dynamics 类型报告，其余报告自动隐藏）。

    横轴：语义偏离距离 Ds（0.0 狄拉克目标点 ← 1.0 初始偏离态）
    纵轴：回合推进进度
    要素：左下角高亮狄拉克目标点、右侧平庸代码吸引子引力漏斗、关键回合轨迹粒子与推进矢量箭头。
    """
    # 画布几何 SSOT：内边距与最小画布下限，_redraw / _zoom / _clamp_pan /
    # _center_node 四处共用同一来源（原先各自硬编码，实际绘制与平移钳位用不同
    # 几何会导致缩放中心漂移）
    _PAD_L, _PAD_R, _PAD_T, _PAD_B = 68, 55, 38, 48
    _MIN_CANVAS_W, _MIN_CANVAS_H = 600, 480

    def __init__(self, parent) -> None:
        from .charts import _ChartTooltip, _aa_layer_flat
        self._aa_layer_flat = _aa_layer_flat
        self.container = tk.Frame(parent, bg=theme.PANEL_2, highlightthickness=1,
                                  highlightbackground=theme.BORDER)
        self.head = tk.Frame(self.container, bg=theme.CARD_HEADER_BG, padx=10, pady=5)
        self.head.pack(fill="x")

        # 第一行：主控栏（标题 + 视图模式切换 | 右侧四能力条）
        row_top = tk.Frame(self.head, bg=theme.CARD_HEADER_BG)
        row_top.pack(fill="x", pady=(0, 4))

        left_top = tk.Frame(row_top, bg=theme.CARD_HEADER_BG)
        left_top.pack(side="left")
        tk.Label(left_top, text="相空间收敛动力学相图", bg=theme.CARD_HEADER_BG,
                 fg=theme.FG_WHITE, font=theme.FONT_UI_BOLD).pack(side="left")

        self.mode_frame = tk.Frame(left_top, bg=theme.CARD_HEADER_BG)
        self.mode_frame.pack(side="left", padx=(14, 0))
        self._view_mode = "manifold"
        self._mode_btns = {}
        for m_key, m_name in (("manifold", "时序流形"), ("phase_plane", "相速度极限环")):
            btn = RoundedPill(self.mode_frame, text=m_name, radius=4, height=22,
                              fill=theme.PANEL_2, hover_fill=theme.HOVER_BG,
                              bg=theme.CARD_HEADER_BG, fg=theme.FG, font=theme.FONT_UI_SMALL,
                              command=lambda _p=None, m=m_key: self._set_mode(m))
            btn.pack(side="left", padx=2)
            self._mode_btns[m_key] = btn
        self._update_mode_btns()
        # 缩放平移交互控制（Zoom & Pan）
        self._zoom_scale: float = 1.0
        self._pan_x: float = 0.0
        self._pan_y: float = 0.0
        self._drag_data: dict = {"x": 0, "y": 0, "panned": False}
        self._drag_redraw_pending: bool = False
        self._drag_redraw_after: str | None = None  # 16ms 拖拽节流定时器 id（Destroy 时统一取消）
        self._wheel_zoom_pending: bool = False
        self._wheel_zoom_after: str | None = None   # 16ms 滚轮缩放节流定时器 id
        self._unbind_wheel = None                   # bind_all 滚轮解绑回调（Enter 绑定 / Leave·Destroy 解绑）
        self._wheel_zoom_accum: float = 1.0
        self._wheel_cx: float | None = None
        self._wheel_cy: float | None = None
        self._selected_node_idx: int | None = None
        self._hud_btns: list[tuple[float, float, float, float, str]] = []

        # 缩放控制微胶囊（保留百分比与复位按钮，无 emoji）
        self.zoom_frame = tk.Frame(left_top, bg=theme.CARD_HEADER_BG)
        self.zoom_frame.pack(side="left", padx=(12, 0))

        btn_out = RoundedPill(self.zoom_frame, text="－", width=22, height=20, radius=4,
                              fill=theme.PANEL_2, hover_fill=theme.HOVER_BG,
                              bg=theme.CARD_HEADER_BG, fg=theme.FG,
                              command=lambda: self._zoom(0.85))
        btn_out.pack(side="left", padx=1)

        self.zoom_lbl = tk.Label(self.zoom_frame, text="100%", bg=theme.CARD_HEADER_BG,
                                 fg=theme.MUTED, font=theme.FONT_UI_SMALL, padx=4)
        self.zoom_lbl.pack(side="left", padx=1)

        btn_in = RoundedPill(self.zoom_frame, text="＋", width=22, height=20, radius=4,
                             fill=theme.PANEL_2, hover_fill=theme.HOVER_BG,
                             bg=theme.CARD_HEADER_BG, fg=theme.FG,
                             command=lambda: self._zoom(1.18))
        btn_in.pack(side="left", padx=1)

        btn_rst = RoundedPill(self.zoom_frame, text="复位", width=36, height=20, radius=4,
                              fill=theme.PANEL_2, hover_fill=theme.HOVER_BG,
                              bg=theme.CARD_HEADER_BG, fg=theme.FG,
                              command=lambda: self._reset_zoom())
        btn_rst.pack(side="left", padx=(3, 1))

        Tooltip(self.zoom_frame,
                "视口缩放与平移漫游：\n"
                "• 鼠标滚轮：以当前光标为中心自适应无级缩放 (100%~500%)；\n"
                "• 拖拽漫游：鼠标中键/右键或按住空白处拖拽平滑漫游；\n"
                "• 单击复位：一键归位；\n"
                "• 分级展现 (LOD)：放大时自动展开被折叠抽稀的微观轮次。")
        self.caps_frame = tk.Frame(row_top, bg=theme.CARD_HEADER_BG)
        self.caps_frame.pack(side="right")

        # 第二行：物理遥测与运行态势徽标栏（终态、稳定性、推进节奏、有效产出率）
        row_badges = tk.Frame(self.head, bg=theme.CARD_HEADER_BG)
        row_badges.pack(fill="x", pady=(2, 0))

        self.state_badge = tk.Label(row_badges, text="", bg=theme.CARD_HEADER_BG,
                                    font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.state_badge.pack(side="left")
        Tooltip(self.state_badge,
                "整场会话最终交付态势：\n"
                "• 精准收敛：无多余冗余生成，代码直达用户真实目标；\n"
                "• 成功破局：虽曾遭遇死锁或偏离，但最终成功逃逸达成收敛；\n"
                "• 陷入泥潭：落入惯性套路，反复打补丁未脱困；\n"
                "• 持续发散：语义偏离过大且发散，未能达成目标。")

        self.lyapunov_badge = tk.Label(row_badges, text="", bg=theme.CARD_HEADER_BG,
                                       font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.lyapunov_badge.pack(side="left", padx=(6, 0))
        Tooltip(self.lyapunov_badge,
                "改动稳定性指数 λ：代码演化稳定性度量。\n"
                "λ < 0 代表代码收敛稳定，小调整能被吸收，走向交付；\n"
                "λ > 0 代表代码发散失控，局部误解被反复放大，越改越乱。")

        self.damping_badge = tk.Label(row_badges, text="", bg=theme.CARD_HEADER_BG,
                                      font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.damping_badge.pack(side="left", padx=(6, 0))
        Tooltip(self.damping_badge,
                "推进节奏比 ζ：系统收敛动力学稳定性度量。\n"
                "ζ < 0.70 为欠阻尼震荡：前后反复横跳、推倒重来；\n"
                "0.70 ≤ ζ ≤ 1.10 为临界平稳：少做无用功、平稳向前推进；\n"
                "ζ > 1.10 为过阻尼迟缓：推进停滞不前、盲目游走。")

        self.carnot_badge = tk.Label(row_badges, text="", bg=theme.CARD_HEADER_BG,
                                     font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.carnot_badge.pack(side="left", padx=(6, 0))
        Tooltip(self.carnot_badge,
                "代码有效率 η：最终有效代码占全部开销的比例。\n"
                "最终保留的有效代码行 vs 自己反复重构删除的废代码与压缩耗散。\n"
                "高效率代表一次写对少返工；低效率代表大量废代码被自己推翻重写。")

        self.swarm_badge = tk.Label(row_badges, text="", bg=theme.CARD_HEADER_BG,
                                    font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self.swarm_badge.pack(side="left", padx=(6, 0))
        Tooltip(self.swarm_badge,
                "多智能体协同分（2026 Swarm Synergy）：\n"
                "量化评估派生的并行子代理（探查/执行/审查等分工）究竟是在帮忙推进，\n"
                "还是带来了额外的离心发散噪音与沟通损耗。")
        self.cap_lbl = tk.Label(self.head, text="")  # 兼容测试与标量文本读取

        self.canvas = tk.Canvas(self.container, bg=theme.BG, height=495,
                                highlightthickness=0, cursor=CLICK_CURSOR)
        self.canvas.pack(fill="x", padx=4, pady=4)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_canvas_leave)
        self.canvas.bind("<Destroy>", self._on_canvas_destroy)
        # Windows 把滚轮事件投给焦点窗口而非指针下窗口，canvas 直绑收不到；
        # 统一经 platform.bind_mousewheel（Enter 时 bind_all 接管、Leave/Destroy
        # 解绑——与排名页/报告页 Treeview 滚轮同款用法）
        self.canvas.bind("<Enter>", self._on_canvas_enter)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._on_pan_end)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_pan_end)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Left>", lambda _e: self._step_node(-1))
        self.canvas.bind("<Right>", lambda _e: self._step_node(1))
        self.canvas.bind("<Escape>", lambda _e: self._deselect_node())
        self._data: dict = {}
        self._report: dict = {}
        self._pts: list = []
        self._tgt_pos: tuple[float, float] | None = None
        self._att_pos: tuple[float, float] | None = None
        self._regime_boxes: list = []
        self._waterbed_turns: set = set()
        self._pad_t: int = 34
        self._waterbed_boxes: list = []
        self._barrier_box: tuple | None = None
        self._horizon_box: tuple | None = None
        self._hud_card_box: tuple[float, float, float, float] | None = None
        self._impulse_boxes: list = []
    def render(self, dynamics_data: dict, report: dict) -> None:
        self._data = dynamics_data or {}
        self._report = report or {}
        # 切换报告重渲染时，已选质点索引可能超出新轨迹范围 → 复位探针避免悬空
        if self._selected_node_idx is not None:
            n_traj = len(self._data.get("trajectory") or [])
            if not (0 <= self._selected_node_idx < n_traj):
                self._selected_node_idx = None
        # 1. 态势徽章（终态判定走共享 SSOT，与报告列表态势着色同源）
        traj = self._data.get("trajectory") or []
        state_txt, state_col, state_bg = _DYNAMIC_STATE_BADGES[_dynamics_terminal_state(self._data)]
        self.state_badge.config(text=state_txt, fg=state_col, bg=state_bg)

        # 1.1 李雅普诺夫稳定性徽标
        _, avg_lam, _ = self._compute_lyapunov_stats(traj, self._data.get("lyapunov_exponent"))
        if avg_lam < 0:
            self.lyapunov_badge.config(
                text=f"收敛稳定 λ = {avg_lam:+.2f}", fg=theme.SUCCESS, bg=theme.DIRAC_CORE_BG)
        else:
            self.lyapunov_badge.config(
                text=f"失控发散 λ = {avg_lam:+.2f}", fg=theme.ERROR, bg=theme.ERROR_TINT_BG)

        # 1.2 控制论阻尼比徽标
        zeta = self._data.get("damping_ratio")
        if zeta is None:
            zeta, cat_key, cat_cn = metrics.compute_cybernetic_damping(traj)
        else:
            zeta = float(zeta)
            if zeta < 0.70:
                cat_key, cat_cn = "underdamped", "反复横跳"
            elif zeta > 1.10:
                cat_key, cat_cn = "overdamped", "卡壳停滞"
            else:
                cat_key, cat_cn = "critical", "平稳推进"

        if cat_key == "critical":
            self.damping_badge.config(
                text=f"阻尼比 ζ = {zeta:.2f} · 平稳推进", fg=theme.SUCCESS, bg=theme.DIRAC_CORE_BG)
        elif cat_key == "underdamped":
            self.damping_badge.config(
                text=f"阻尼比 ζ = {zeta:.2f} · 反复横跳", fg=theme.ERROR, bg=theme.ERROR_TINT_BG)
        else:
            self.damping_badge.config(
                text=f"阻尼比 ζ = {zeta:.2f} · 卡壳停滞", fg=theme.WARNING, bg=theme.WARN_TINT_BG)

        # 1.3 朗道尔卡诺效率徽标
        carnot = self._data.get("carnot_efficiency")
        if carnot is None:
            carnot = 0.68
        else:
            carnot = float(carnot)
        carnot_pct = int(round(carnot * 100))
        if carnot >= 0.60:
            carnot_txt = f"代码有效率 η = {carnot_pct}% · 极少返工"
            carnot_col = theme.SUCCESS
            carnot_bg = theme.DIRAC_CORE_BG
        elif carnot < 0.35:
            carnot_txt = f"代码有效率 η = {carnot_pct}% · 废码过多"
            carnot_col = theme.ERROR
            carnot_bg = theme.ERROR_TINT_BG
        else:
            carnot_txt = f"代码有效率 η = {carnot_pct}% · 中等返工"
            carnot_col = theme.WARNING
            carnot_bg = theme.WARN_TINT_BG
        self.carnot_badge.config(text=carnot_txt, fg=carnot_col, bg=carnot_bg)
        # 1.4 多智能体协同徽标 (2026 Agent Swarm)
        # LLM 遥测字段做数值防御（字符串 "82"/None 不崩；synergy_score 缺省或非法
        # 时隐藏徽标——metrics 层无子代理时不再造数，返回 None）
        swarm = self._data.get("swarm_synergy") or {}
        try:
            n_subs = int(swarm.get("total_subagents") or 0)
        except (ValueError, TypeError):
            n_subs = 0
        if n_subs > 0:
            try:
                s_score = float(swarm.get("synergy_score"))
            except (ValueError, TypeError):
                s_score = None
            if s_score is None:
                self.swarm_badge.config(text="")
            elif s_score >= 70:
                s_txt = f"多智能体协同 {s_score:.0f} 分 · 向心增益"
                s_col = theme.SUCCESS
                s_bg = theme.DIRAC_CORE_BG
                self.swarm_badge.config(text=s_txt, fg=s_col, bg=s_bg)
            elif s_score < 40:
                s_txt = f"多智能体协同 {s_score:.0f} 分 · 冗余耗散"
                s_col = theme.ERROR
                s_bg = theme.ERROR_TINT_BG
                self.swarm_badge.config(text=s_txt, fg=s_col, bg=s_bg)
            else:
                s_txt = f"多智能体协同 {s_score:.0f} 分 · 常规协同"
                s_col = theme.WARNING
                s_bg = theme.WARN_TINT_BG
                self.swarm_badge.config(text=s_txt, fg=s_col, bg=s_bg)
        else:
            self.swarm_badge.config(text="")

        # 2. 四能力胶囊条（显示名称、分数与评级 Tooltip）
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
                ("认知平衡", "认知负债平衡力", caps.get("epistemic_balance"),
                 "衡量先探查再动刀、控制先验不确定性的掌控力。\n高分代表充分探查再精准动刀；低分代表盲目在认知盲区切削底层代码。"),
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
                     f"反馈收敛效率 {caps.get('feedback_mutual_info', '-')} · "
                     f"认知平衡力 {caps.get('epistemic_balance', '-')}")
        else:
            self.cap_lbl.config(text="")
        self._redraw()

    def _set_mode(self, mode: str) -> None:
        if self._view_mode == mode:
            return
        self._view_mode = mode
        self._update_mode_btns()
        self._redraw()

    def _update_mode_btns(self) -> None:
        for m, btn in self._mode_btns.items():
            active = (self._view_mode == m)
            btn.config(
                bg=theme.HOVER_ACCENT if active else theme.PANEL_2,
                fg=theme.FG_WHITE if active else theme.FG,
                font=theme.FONT_UI_SMALL_BOLD if active else theme.FONT_UI_SMALL)

    def _select_node(self, idx: int) -> None:
        if not (0 <= idx < len(self._pts)):
            return
        self._selected_node_idx = idx
        self.canvas.focus_set()
        self._redraw()

    def _step_node(self, delta: int) -> None:
        if not self._pts:
            return
        n = len(self._pts)
        if self._selected_node_idx is None:
            self._selected_node_idx = 0 if delta >= 0 else (n - 1)
        else:
            self._selected_node_idx = (self._selected_node_idx + delta) % n
        self._center_node(self._selected_node_idx)

    def _plot_geometry(self) -> tuple[int, int]:
        """画布有效 (w, h)：未映射（无头/初建）时回退请求尺寸，统一最小下限。

        _redraw / _zoom / _clamp_pan / _center_node 共用同一来源，避免实际
        绘制与平移钳位各用一套几何导致缩放锚点漂移。
        """
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 100:
            w = max(w, self.canvas.winfo_reqwidth(), self._MIN_CANVAS_W)
        if h < 50:
            h = max(h, self.canvas.winfo_reqheight(), self._MIN_CANVAS_H)
        return w, h

    def _plot_size(self) -> tuple[float, float]:
        """由 _plot_geometry 推导绘图区 (plot_w, plot_h)（扣除四向内边距）。"""
        w, h = self._plot_geometry()
        return w - self._PAD_L - self._PAD_R, h - self._PAD_T - self._PAD_B

    def _total_turns(self, traj: list) -> int:
        """会话总回合数：报告 turns 字段优先，缺失/非法时回退轨迹最大 turn。"""
        total = self._report.get("turns")
        if not isinstance(total, (int, float)) or total <= 1:
            valid = [pt.get("turn") for pt in traj if isinstance(pt.get("turn"), (int, float))]
            total = max(valid, default=1)
        return max(1, int(total))

    def _center_node(self, idx: int | None = None) -> None:
        if idx is None:
            idx = self._selected_node_idx
        if idx is None or not (0 <= idx < len(self._pts)):
            return
        target_x, target_y = self._pts[idx][:2]
        plot_w, plot_h = self._plot_size()
        pad_l, pad_t = self._PAD_L, self._PAD_T
        screen_cx = pad_l + plot_w / 2.0
        screen_cy = pad_t + plot_h / 2.0
        dx = screen_cx - target_x
        dy = screen_cy - target_y
        self._pan_x += dx
        self._pan_y += dy
        self._clamp_pan()
        self._redraw()

    def _deselect_node(self) -> None:
        self._selected_node_idx = None
        self._redraw()

    def _on_pan_start(self, event) -> None:
        self._drag_data = {"x": event.x, "y": event.y, "panned": False}
        self.canvas.config(cursor="fleur")

    def _on_pan_move(self, event) -> None:
        dx = event.x - self._drag_data["x"]
        dy = event.y - self._drag_data["y"]
        if abs(dx) > 1 or abs(dy) > 1:
            self._drag_data["panned"] = True
            if self._zoom_scale > 1.01:
                self._pan_x += dx
                self._pan_y += dy
                self._drag_data["x"] = event.x
                self._drag_data["y"] = event.y
                self._clamp_pan()
                if not getattr(self, "_drag_redraw_pending", False):
                    self._drag_redraw_pending = True
                    self._drag_redraw_after = self.canvas.after(16, self._do_drag_redraw)

    def _on_pan_end(self, event) -> None:
        self.canvas.config(cursor=CLICK_CURSOR)
        self._drag_redraw_pending = False
        if getattr(self, "_drag_data", {}).get("panned"):
            self._redraw()

    def _do_drag_redraw(self) -> None:
        self._drag_redraw_pending = False
        self._drag_redraw_after = None
        self._redraw()

    def _draw_hud_card(self, c, plot_w: float, plot_h: float, pad_l: float, pad_t: float) -> None:
        """绘制当前选中质点的精显探针 HUD 卡片与十字准星。"""
        self._hud_btns.clear()
        if self._selected_node_idx is None or not (0 <= self._selected_node_idx < len(self._pts)):
            return
        item = self._pts[self._selected_node_idx]
        x, y, pt = item[:3]

        # 1. 质点双环发光十字准星
        c.create_oval(x - 13, y - 13, x + 13, y + 13, outline=theme.ACCENT, width=1.5)
        c.create_oval(x - 19, y - 19, x + 19, y + 19, outline=theme.ACCENT, dash=(2, 2), width=1)
        c.create_line(x - 24, y, x - 15, y, fill=theme.ACCENT, width=1.5)
        c.create_line(x + 15, y, x + 24, y, fill=theme.ACCENT, width=1.5)
        c.create_line(x, y - 24, x, y - 15, fill=theme.ACCENT, width=1.5)
        c.create_line(x, y + 15, x, y + 24, fill=theme.ACCENT, width=1.5)

        # 2. 悬浮/驻留 HUD 探针卡片（左下角紧凑布局）
        card_w = min(480, plot_w)  # 窄画布收缩卡宽，防「居中/X」按钮越出画布
        card_h = 96
        cx0 = pad_l + 10
        cy0 = pad_t + plot_h - card_h - 10
        cx1 = cx0 + card_w
        cy1 = cy0 + card_h
        self._hud_card_box = (cx0, cx1, cy0, cy1)
        c.create_rectangle(cx0, cy0, cx1, cy1, fill=theme.PHASE_HUD_BG, outline=theme.ACCENT, width=1.5)

        t_val = pt.get("turn")
        u_val = pt.get("user_turn") or pt.get("u")
        ds_val = metrics.ds_of(pt)
        ed_val = float(pt.get("epistemic_debt", 1.0))
        pot_val = float(pt.get("potential_energy", 0.0))
        reg_val = str(pt.get("regime") or "liquid").lower()
        evt_val = str(pt.get("event") or "normal").lower()
        vec_val = str(pt.get("vector") or "neutral").lower()
        note_val = str(pt.get("note") or "")

        # 标题栏（纯中文文字，无 emoji；turn 缺失时显示 "-" 而非「第 TNone 轮」）
        t_show = t_val if t_val is not None else "-"
        t_desc = f"质点探针 · 第 T{t_show} 轮" + (f" (用户 U{u_val})" if u_val else "")
        c.create_text(cx0 + 12, cy0 + 15, text=t_desc, fill=theme.FG_WHITE,
                      font=theme.FONT_UI_BOLD, anchor="w")

        # 交互按钮群（加高至 20px，加大点击命中容差，无 emoji）
        btn_y0, btn_y1 = cy0 + 5, cy0 + 25
        # [< 上轮]
        b1_x0, b1_x1 = cx1 - 184, cx1 - 126
        c.create_rectangle(b1_x0, btn_y0, b1_x1, btn_y1, fill=theme.PANEL_2, outline=theme.BORDER)
        c.create_text((b1_x0 + b1_x1) / 2, (btn_y0 + btn_y1) / 2, text="< 上轮", fill=theme.FG, font=theme.FONT_UI_SMALL, anchor="center")
        self._hud_btns.append((b1_x0, btn_y0, b1_x1, btn_y1, "prev"))

        # [下轮 >]
        b2_x0, b2_x1 = cx1 - 122, cx1 - 64
        c.create_rectangle(b2_x0, btn_y0, b2_x1, btn_y1, fill=theme.PANEL_2, outline=theme.BORDER)
        c.create_text((b2_x0 + b2_x1) / 2, (btn_y0 + btn_y1) / 2, text="下轮 >", fill=theme.FG, font=theme.FONT_UI_SMALL, anchor="center")
        self._hud_btns.append((b2_x0, btn_y0, b2_x1, btn_y1, "next"))

        # [居中]
        b3_x0, b3_x1 = cx1 - 60, cx1 - 22
        c.create_rectangle(b3_x0, btn_y0, b3_x1, btn_y1, fill=theme.PANEL_2, outline=theme.BORDER)
        c.create_text((b3_x0 + b3_x1) / 2, (btn_y0 + btn_y1) / 2, text="居中", fill=theme.FG, font=theme.FONT_UI_SMALL, anchor="center")
        self._hud_btns.append((b3_x0, btn_y0, b3_x1, btn_y1, "center"))

        # [关闭]
        b4_x0, b4_x1 = cx1 - 18, cx1 - 2
        c.create_rectangle(b4_x0, btn_y0, b4_x1, btn_y1, fill=theme.PANEL_2, outline=theme.BORDER)
        c.create_text((b4_x0 + b4_x1) / 2, (btn_y0 + btn_y1) / 2, text="X", fill=theme.MUTED, font=theme.FONT_UI_BOLD, anchor="center")
        self._hud_btns.append((b4_x0, btn_y0, b4_x1, btn_y1, "close"))

        # 行 2：Ds & 推进方向
        delta_ds_str = ""
        if self._selected_node_idx > 0:
            prev_ds = metrics.ds_of(self._pts[self._selected_node_idx - 1][2])
            dds = ds_val - prev_ds
            delta_ds_str = f" ({'↓' if dds < 0 else '↑'}{abs(dds):.4f})"
        vec_cn = {"positive": "向心推进", "convergent": "向心收敛", "negative": "离心偏离",
                  "divergent": "离心发散", "trapped": "泥潭受困", "neutral": "常规平衡"}.get(vec_val, vec_val)
        c.create_text(cx0 + 12, cy0 + 38,
                      text=f"语义偏离 Ds: {ds_val:.4f}{delta_ds_str} · 矢量: {vec_cn}",
                      fill=theme.ACCENT, font=theme.FONT_UI_SMALL, anchor="w")

        # 行 3：阶段与负债势能（平实语）
        reg_cn = {"gas": "探索期", "liquid": "构建期", "glass": "卡壳期", "crystal": "收尾期"}.get(reg_val, reg_val)
        debt_desc = "探查充分" if ed_val <= 1.0 else ("没搞清楚就大改" if ed_val >= 4.0 else "常规推演")
        c.create_text(cx0 + 12, cy0 + 56,
                      text=f"阶段: {reg_cn} · 认知负债 Ed: {ed_val:.2f} ({debt_desc}) · 势能 V: {pot_val:.3f}",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")

        # 行 4：事件与解读（事件名转中文，与悬浮 Tooltip 的胶囊文案风格统一，无方括号噪音）
        note_display = note_val if len(note_val) <= 40 else (note_val[:38] + "…")
        evt_cn = {
            "retry_loop": "重试死锁", "test_fail": "测试报错",
            "compaction": "上下文压缩", "breakthrough": "关键突破",
        }.get(evt_val)
        detail_parts = [p for p in (evt_cn, note_display) if p]
        c.create_text(cx0 + 12, cy0 + 76,
                      text=f"复盘解读: {' · '.join(detail_parts)}",
                      fill=theme.FG_WHITE, font=theme.FONT_UI_SMALL, anchor="w")
    @staticmethod
    def _calc_subagents(pt: dict) -> list[dict]:
        """提取或根据事实推导节点的并行子代理分工。"""
        subs = pt.get("subagents")
        if isinstance(subs, list) and subs:
            return subs
        note = str(pt.get("note") or "")
        if any(kw in note for kw in ("探查", "子任务", "子代理", "探针", "扫描", "并行", "检索")):
            # 视觉层示意卫星：仅据笔记关键词推断，与 metrics 层「不造数」口径区分，
            # 标 inferred 供 tooltip 加「据笔记推断」前缀（示意图 vs 真实遥测）
            return [
                {"name": "探查", "role": "查找相关代码", "semantic_delta": -0.04,
                 "status": "convergent", "inferred": True},
                {"name": "执行", "role": "动手实现修改", "semantic_delta": 0.01,
                 "status": "divergent", "inferred": True},
            ]
        return []

    @staticmethod
    def _compute_lyapunov_stats(traj: list, global_lam_val=None) -> tuple[list[float], float, str]:
        """计算逐节点局部李雅普诺夫指数与整场会话全局指数及视界拦截状态。"""
        if not traj:
            return [], -0.10, "safe"
        lam_list = []
        for i, n in enumerate(traj):
            if i == 0:
                lam = -0.05
            else:
                prev = traj[i - 1]
                ds_cur = metrics.ds_of(n)
                ds_prev = metrics.ds_of(prev)
                delta_ds = ds_cur - ds_prev
                evt = str(n.get("event") or "").lower()
                vec = str(n.get("vector") or "").lower()
                if evt == "breakthrough":
                    lam = -0.65
                elif delta_ds < -0.05:
                    lam = -0.35
                elif delta_ds < 0:
                    lam = -0.15
                elif evt == "retry_loop":
                    lam = +0.25
                elif delta_ds > 0.05:
                    lam = +0.55
                else:
                    lam = +0.10
            lam_list.append(lam)

        if isinstance(global_lam_val, (int, float)):
            avg_lam = float(global_lam_val)
        else:
            avg_lam = sum(lam_list) / len(lam_list) if lam_list else -0.10

        max_ds = max((metrics.ds_of(pt) for pt in traj), default=0.5)
        last_ds = metrics.ds_of(traj[-1]) if traj else 0.5
        if max_ds >= 0.78 and last_ds <= 0.45:
            horizon_status = "intercepted"
        elif last_ds >= 0.82 and avg_lam >= 0:
            horizon_status = "breached"
        else:
            horizon_status = "safe"

        return lam_list, avg_lam, horizon_status
    @staticmethod
    def _derive_flux_and_snr(node: dict, prev_node: dict | None = None) -> tuple[float, dict | None]:
        """推算/提取当前节点的信噪比 (SNR) 与人类外部控制冲量 (user_impulse)。"""
        snr = node.get("snr")
        if snr is None:
            evt = str(node.get("event") or "normal").lower()
            vec = str(node.get("vector") or "neutral").lower()
            if evt == "retry_loop":
                snr = 0.15
            elif evt == "test_fail":
                snr = 0.30
            elif evt == "breakthrough":
                snr = 0.90
            elif vec in ("negative", "divergent", "trapped"):
                snr = 0.25
            elif vec in ("positive", "convergent"):
                snr = 0.80
            else:
                snr = 0.55
        try:
            snr = max(0.1, min(1.0, float(snr)))
        except (ValueError, TypeError):
            snr = 0.55

        impulse = node.get("user_impulse")
        u_cur = node.get("user_turn") or node.get("u")
        u_prev = (prev_node.get("user_turn") or prev_node.get("u")) if prev_node else None

        is_user_entry = False
        if isinstance(impulse, dict) and impulse:
            is_user_entry = True
        elif u_cur is not None and (u_prev is None or u_cur != u_prev):
            is_user_entry = True

        if is_user_entry and not (isinstance(impulse, dict) and impulse):
            evt = str(node.get("event") or "normal").lower()
            vec = str(node.get("vector") or "neutral").lower()
            if evt == "breakthrough" or vec in ("positive", "convergent"):
                flux = "high"
                note = "强负熵向心制导"
            elif vec in ("negative", "divergent", "trapped"):
                flux = "low"
                note = "低互信息扰动"
            else:
                flux = "mid"
                note = "常规意图微调"
            impulse = {"flux": flux, "note": note}

        return snr, impulse if isinstance(impulse, dict) else None
    def pack(self, **kw):
        self.container.pack(**kw)

    def pack_forget(self):
        self.container.pack_forget()
        self._tooltip.hide()

    def _on_motion(self, event) -> None:
        if getattr(self, "_drag_data", {}).get("panned"):
            self._tooltip.hide()
            return
        # 0. 优先检测是否悬浮于 HUD 卡片按钮上，呈现手型光标与高亮反馈
        for bx0, by0, bx1, by1, _act in getattr(self, "_hud_btns", []):
            if (bx0 - 4) <= event.x <= (bx1 + 4) and (by0 - 4) <= event.y <= (by1 + 4):
                self.canvas.config(cursor=CLICK_CURSOR)
                self._tooltip.hide()
                return
        if getattr(self, "_hud_card_box", None) and (
            self._hud_card_box[0] <= event.x <= self._hud_card_box[1] and
            self._hud_card_box[2] <= event.y <= self._hud_card_box[3]
        ):
            self.canvas.config(cursor="arrow")
            self._tooltip.hide()
            return
        for bx0, bx1, by0, by1, r_turn, r_reg, r_ed in getattr(self, "_regime_boxes", []):
            if bx0 <= event.x <= bx1 and by0 - 4 <= event.y <= by1 + 4:
                reg_names = {
                    "gas": ("高熵气态", "源码架构探查阶段", theme.REGIME_GAS),
                    "liquid": ("凝聚液态", "主干业务构建阶段", theme.REGIME_LIQUID),
                    "glass": ("自旋玻璃态", "循环修补死锁阶段", theme.REGIME_GLASS),
                    "crystal": ("晶态终态", "目标精准收敛阶段", theme.REGIME_CRYSTAL),
                }
                r_title, r_desc, r_col = reg_names.get(r_reg, ("常规阶段", "", theme.MUTED))
                lines = [
                    f"阶段演化 · 回合 T{r_turn}",
                    f"当前阶段: {r_title}",
                    f"阶段特征: {r_desc}",
                    f"认知负债: Ed = {r_ed:.2f} · {'探查充分' if r_ed <= 1.0 else ('常规推演' if r_ed < 4.0 else '盲区动刀')}",
                ]
                ed_c = theme.DEBT_GLOW_SAFE if r_ed <= 1.0 else (theme.DEBT_GLOW_DANGER if r_ed >= 4.0 else theme.MUTED)
                colors = [theme.FG_WHITE, r_col, theme.MUTED, ed_c]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        if not self._pts:
            self._tooltip.hide()
            return
        best_item = None
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
                best_item = item
                break
            elif d2 < min_d2:
                min_d2 = d2
                best_item = item
        if best_item:
            px, py, pt = best_item[:3]
            t = pt.get("turn", "-")
            t_num = pt.get("turn")
            u = pt.get("user_turn") or pt.get("u")
            ds = metrics.ds_of(pt)
            vec = str(pt.get("vector") or "neutral").lower()
            event_tag = str(pt.get("event") or "normal").lower()
            note = pt.get("note", "")
            status_map = {
                "positive": ("向心推进", theme.SUCCESS),
                "convergent": ("向心推进", theme.SUCCESS),
                "negative": ("离心发散", theme.ERROR),
                "divergent": ("离心发散", theme.ERROR),
                "trapped": ("死锁陷阱", theme.ERROR),
                "neutral": ("中性微调", theme.WARNING),
            }
            event_map = {
                "retry_loop": "连续重试死循环",
                "test_fail": "测试/环境报错干扰",
                "compaction": "上下文窗口压缩",
                "breakthrough": "向心关键突破",
            }
            status_str, status_col = status_map.get(vec, ("状态未定", theme.MUTED))
            turn_desc = f"助手回合 T{t} · 用户消息 U{u}" if u is not None else f"回合 T{t}"
            lines = [
                f"{turn_desc} · 语义距离: {ds:.2f}",
                f"推进矢量: {status_str}",
            ]
            colors = [theme.FG_WHITE, status_col]
            # 若包含相速度数据（len >= 5），展示运动学速度与态势
            if len(best_item) > 4:
                v_val = best_item[4]
                if v_val < -0.01:
                    v_desc = "高速向心冲刺"
                elif v_val < 0:
                    v_desc = "向心逼近"
                elif v_val > 0.01:
                    v_desc = "快速离心发散"
                elif v_val > 0:
                    v_desc = "离心漂移"
                else:
                    v_desc = "相对静止平衡"
                v_col = theme.SUCCESS if v_val < 0 else (theme.ERROR if v_val > 0 else theme.MUTED)
                lines.append(f"相速度: {v_val:+.4f}/步 ({v_desc})")
                colors.append(v_col)
            # 势能高度 V(Ds)
            pe = pt.get("potential_energy")
            if pe is None:
                pe = metrics.compute_waddington_potential(ds)
            else:
                pe = float(pe)
            pe_desc = "接近目标（稳定区）" if pe < -0.8 else (
                "惯性套路区（偏离目标）" if ds > 0.75 else (
                    "关键转折关口" if abs(ds - 0.55) < 0.08 else "过渡区"))
            pe_col = theme.SUCCESS if pe < -0.6 else (theme.ERROR if pe > 0.0 else theme.MUTED)
            lines.append(f"势能高程: V = {pe:+.2f} · {pe_desc}")
            colors.append(pe_col)

            # 认知负债 Ed
            ed = float(pt.get("epistemic_debt", 1.0))
            ed_desc = "探查充分" if ed <= 1.0 else ("常规推演" if ed < 4.0 else "没搞清楚就大改")
            ed_col = theme.DEBT_GLOW_SAFE if ed <= 1.0 else (theme.DEBT_GLOW_DANGER if ed >= 4.0 else theme.MUTED)
            lines.append(f"认知负债: Ed = {ed:.2f} · {ed_desc}")
            colors.append(ed_col)

            # 动力学阶段（平实语：探索/构建/卡壳/收尾）
            reg = str(pt.get("regime") or "liquid").lower()
            reg_names = {
                "gas": ("探索期", theme.REGIME_GAS),
                "liquid": ("构建期", theme.REGIME_LIQUID),
                "glass": ("卡壳期", theme.REGIME_GLASS),
                "crystal": ("收尾期", theme.REGIME_CRYSTAL),
            }
            reg_name, reg_col = reg_names.get(reg, ("构建期", theme.MUTED))
            lines.append(f"当前阶段: {reg_name}")
            colors.append(reg_col)

            # 转折触发者（dyn-v4 遥测新增：区分用户推动 / AI 自主 / 环境冲击）
            trig = str(pt.get("trigger") or "").lower()
            trig_names = {
                "user": ("用户消息推动", theme.ACCENT),
                "ai": ("AI 自主决策", theme.WARNING),
                "env": ("报错或外部事件驱动", theme.ERROR),
                "none": ("常规推进", theme.MUTED),
            }
            if trig in trig_names:
                trig_name, trig_col = trig_names[trig]
                lines.append(f"本轮驱动: {trig_name}")
                colors.append(trig_col)

            # 局部瞬时阻尼比谱
            dspec = self._data.get("damping_spectrum") or {}
            td_map = dict(dspec.get("turn_damping", []))
            if t_num in td_map:
                loc_z = td_map[t_num]
                loc_desc = "平稳推进" if 0.7 <= loc_z <= 1.1 else ("反复横跳" if loc_z < 0.7 else "卡壳停滞")
                loc_col = theme.SUCCESS if 0.7 <= loc_z <= 1.1 else (theme.ERROR if loc_z < 0.7 else theme.WARNING)
                lines.append(f"局部节奏: ζ = {loc_z:.2f} · {loc_desc}")
                colors.append(loc_col)

            # 控制论水床因果链检测
            wb_turns = getattr(self, "_waterbed_turns", set())
            causal_links = self._data.get("waterbed_causality") or []
            found_causal = None
            for cl in causal_links:
                if cl.get("target_turn") == t_num:
                    found_causal = cl
                    break
            if found_causal:
                s_file = found_causal.get("source_file", "代码资产")
                s_turn = found_causal.get("source_turn", "-")
                b_rad = found_causal.get("blast_radius", 1)
                lines.append(f"连锁破坏: 修改 {s_file} (T{s_turn}) 导致后续报错 · 冲击半径 {b_rad}")
                colors.append(theme.WATERBED_ARC)
            elif t_num in wb_turns or (t != "-" and str(t).isdigit() and int(t) in wb_turns):
                lines.append("警示: 按下葫芦浮起瓢 · 修复一处引发别处报错")
                colors.append(theme.WATERBED_ARC)
            if abs(ds - 0.55) <= 0.04:
                lines.append("位置: 关键转折关口（Ds≈0.55 分水岭）")
                colors.append(theme.PHASE_BARRIER_CREST)

            # 双信源信息流：信噪比与外部控制冲量
            snr, impulse = self._derive_flux_and_snr(pt)
            lines.append(f"信噪比: {snr:.0%} ({'高信噪比/高内聚' if snr >= 0.7 else ('噪声偏多' if snr < 0.35 else '中等')})")
            colors.append(theme.SUCCESS if snr >= 0.7 else (theme.ERROR if snr < 0.35 else theme.MUTED))
            if impulse:
                flux_lbl = {"high": "关键纠偏", "mid": "常规指令", "low": "微弱扰动"}.get(impulse.get("flux"), "外部干预")
                lines.append(f"人类控制冲量: {flux_lbl} · {impulse.get('note', '')}")
                colors.append(theme.PHASE_IMPULSE if impulse.get("flux") == "high" else theme.MUTED)

            # 局部稳定性
            lam_val = -0.65 if event_tag == "breakthrough" else (-0.35 if vec in ("positive", "convergent") else (+0.25 if event_tag == "retry_loop" else 0.10))
            lam_desc = "强力收敛" if lam_val <= -0.4 else ("渐近稳定" if lam_val < 0 else "发散风险")
            lam_col = theme.SUCCESS if lam_val < 0 else theme.ERROR
            lines.append(f"改动稳定性: λ = {lam_val:+.2f} · {lam_desc}")
            colors.append(lam_col)

            # 多体系统：子智能体协同态势（关键词推断的示意卫星须注明非真实遥测）
            subs = self._calc_subagents(pt)
            if subs:
                inferred = any(s.get("inferred") for s in subs)
                lines.append(f"并行子代理{'（据笔记推断）' if inferred else ''}: 共 {len(subs)} 个分工协作")
                colors.append(theme.CHART_PALETTE[4])
                for sub in subs[:3]:
                    s_name = self._sub_display_name(sub)
                    s_role = sub.get("role", "任务")
                    s_delta = sub.get("semantic_delta", 0.0)
                    s_stat = "帮忙推进" if str(sub.get("status") or "").lower() == "convergent" else "越帮越忙"
                    s_col = theme.SUCCESS if s_stat == "帮忙推进" else theme.ERROR
                    lines.append(f"  • {s_name} · {s_role} · 偏离变化 {s_delta:+.2f} · {s_stat}")
                    colors.append(s_col)

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
                    "目标点 · 完全契合用户意图",
                    "状态特征: 契合真实意图 · 语义距离 0.00",
                    "动力学定义: 业务意图形式化收敛终点",
                    "工程含义: 逻辑精炼紧凑，无多余冗余生成",
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
                    "惯性套路区 · 套路化生成",
                    "状态特征: 套路化生成 · 偏离危险区 · Ds 约 0.86",
                    "动力学定义: 预训练模型的面条冗余惯性黑洞",
                    "工程含义: 盲目机械修改、过度封装、局部重试死锁",
                ]
                colors = [theme.ERROR, theme.FG_WHITE, theme.MUTED, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return
        # 4. 检测连锁反应（水床弧线）微型胶囊与弧线
        for bx0, bx1, by0, by1, wb_t in getattr(self, "_waterbed_boxes", []):
            if bx0 <= event.x <= bx1 and by0 - 6 <= event.y <= by1 + 6:
                lines = [
                    "连锁破坏 · 按下葫芦浮起瓢",
                    f"触发位置: 助手第 {wb_t} 轮",
                    "现象特征: 刚刚修复了前一个需求，却直接引发其他非关联模块报错挂掉",
                    "工程根因: 代码修改缺乏全局边界隔离，拆东墙补西墙",
                ]
                colors = [theme.WATERBED_ARC, theme.FG_WHITE, theme.MUTED, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        # 5. 检测鞍点势垒分水岭与胶囊
        bbox = getattr(self, "_barrier_box", None)
        if bbox:
            bx0, bx1, by0, by1, lx, pt, ph, b_txt, is_crossed = bbox
            capsule_hit = (bx0 <= event.x <= bx1 and by0 <= event.y <= by1)
            line_hit = (abs(event.x - lx) <= 12 and pt <= event.y <= pt + ph)
            if capsule_hit or line_hit:
                status_desc = "已成功跨越 · 进入顺畅收敛通道" if is_crossed else "尚未跨越 · 处于核心理解阻碍期"
                lines = [
                    "需求理解分水岭 · 关键转折关口",
                    "分界线: 语义偏离度 0.55",
                    f"当前状态: {status_desc}",
                    "工程意义: 整个会话中最难啃的技术理解卡点，跨过后代码迅速收敛",
                ]
                colors = [theme.PHASE_BARRIER_CREST, theme.FG_WHITE, theme.SUCCESS if is_crossed else theme.WARNING, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        # 6. 检测止损红线与视界拦截胶囊
        hbox = getattr(self, "_horizon_box", None)
        if hbox:
            bx0, bx1, by0, by1, lx, pt, ph, lbl_txt, h_stat = hbox
            capsule_hit = (bx0 <= event.x <= bx1 and by0 <= event.y <= by1)
            line_hit = (abs(event.x - lx) <= 14 and pt <= event.y <= pt + ph)
            if capsule_hit or line_hit:
                if h_stat == "intercepted":
                    h_col = theme.SUCCESS
                    h_advice = "用户的关键反馈成功力挽狂澜，避免了全盘推翻"
                elif h_stat == "breached":
                    h_col = theme.ERROR
                    h_advice = "偏离已无法挽回，继续对话只会产生更多冗余废代码，建议新开会话"
                else:
                    h_col = theme.MUTED
                    h_advice = "代码演化未越界，处于安全受控区间"
                lines = [
                    "止损临界红线 · 语义偏离度 0.82",
                    f"当前态势: {lbl_txt}",
                    f"工程建议: {h_advice}",
                ]
                colors = [h_col, theme.FG_WHITE, theme.MUTED]
                self._tooltip.show(event.x, event.y, lines, colors)
                return

        # 7. 检测人类外部信息注入标签
        for bx0, bx1, by0, by1, u_val, flux, note in getattr(self, "_impulse_boxes", []):
            if bx0 <= event.x <= bx1 and by0 <= event.y <= by1:
                flux_name = "关键纠偏制导" if flux == "high" else ("常规需求微调" if flux == "mid" else "低效催促指令")
                lines = [
                    f"用户外部指令 · 消息 U{u_val}",
                    f"指令性质: {flux_name}",
                    f"指令内容: {note}",
                    "向心贡献: 注入关键负熵，引导 AI 脱离歧途",
                ]
                colors = [theme.PHASE_IMPULSE if flux == "high" else theme.MUTED, theme.FG_WHITE, theme.MUTED, theme.MUTED]
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

    def _zoom(self, factor: float, cx: float | None = None, cy: float | None = None) -> None:
        new_scale = max(1.0, min(5.0, self._zoom_scale * factor))
        if abs(new_scale - self._zoom_scale) < 0.005:
            return
        plot_w, plot_h = self._plot_size()
        pad_l, pad_t = self._PAD_L, self._PAD_T
        if new_scale <= 1.01:
            self._zoom_scale = 1.0
            self._pan_x = 0.0
            self._pan_y = 0.0
        else:
            focus_x = cx if cx is not None else (pad_l + plot_w / 2.0)
            focus_y = cy if cy is not None else (pad_t + plot_h / 2.0)
            ratio = new_scale / self._zoom_scale
            # 严格以光标所在视口相对位置为锚点缩放，彻底消除漂移
            self._pan_x = (focus_x - pad_l) - (focus_x - pad_l - self._pan_x) * ratio
            self._pan_y = (focus_y - pad_t) - (focus_y - pad_t - self._pan_y) * ratio
            self._zoom_scale = new_scale
        self._clamp_pan(plot_w, plot_h)
        if hasattr(self, "zoom_lbl"):
            self.zoom_lbl.config(text=f"{int(round(self._zoom_scale * 100))}%")
        self._redraw()

    def _reset_zoom(self) -> None:
        self._zoom_scale = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0

        if hasattr(self, "zoom_lbl"):
            self.zoom_lbl.config(text="100%")
        self._redraw()

    def _on_canvas_enter(self, _event=None) -> None:
        # Windows 滚轮事件投给焦点窗口，canvas 直绑收不到 → Enter 时经 helper
        # bind_all 接管（沿用排名页/报告页 Treeview 滚轮的模式）
        from .platform import bind_mousewheel
        if self._unbind_wheel is None:
            self._unbind_wheel = bind_mousewheel(self.canvas, self._on_mousewheel)

    def _on_canvas_leave(self, _event=None) -> None:
        self._tooltip.hide()
        if self._unbind_wheel is not None:
            try:
                self._unbind_wheel()
            except tk.TclError:
                pass
            self._unbind_wheel = None

    def _on_canvas_destroy(self, _event=None) -> None:
        """canvas 销毁：藏 Tooltip、解除 bind_all 滚轮接管并取消未决的节流定时器。

        bind_all 的滚轮 handler 闭包引用 self，widget 销毁后不解绑会泄漏并劫持
        同应用其他组件的滚轮；after(16) 节流定时器同理需要 after_cancel。
        """
        try:
            self._tooltip.hide()
        except tk.TclError:
            pass  # tooltip 的 Toplevel 可能已随父窗口先行销毁
        if self._unbind_wheel is not None:
            try:
                self._unbind_wheel()
            except tk.TclError:
                pass
            self._unbind_wheel = None
        for attr in ("_drag_redraw_after", "_wheel_zoom_after"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.canvas.after_cancel(aid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _on_mousewheel(self, units: int) -> None:
        # bind_mousewheel 单位约定（三平台统一）：上滚 = 负值（win/mac 一格
        # -delta/120，linux Button-4 → -1）。「上滚=放大、下滚=缩小」
        factor = 1.15 if units < 0 else 0.87
        self._wheel_zoom_accum = getattr(self, "_wheel_zoom_accum", 1.0) * factor
        try:
            self._wheel_cx = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
            self._wheel_cy = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        except tk.TclError:
            self._wheel_cx = self._wheel_cy = None
        if not getattr(self, "_wheel_zoom_pending", False):
            self._wheel_zoom_pending = True
            self._wheel_zoom_after = self.canvas.after(16, self._apply_wheel_zoom)

    def _apply_wheel_zoom(self) -> None:
        self._wheel_zoom_pending = False
        self._wheel_zoom_after = None
        factor = getattr(self, "_wheel_zoom_accum", 1.0)
        self._wheel_zoom_accum = 1.0
        self._zoom(factor, getattr(self, "_wheel_cx", None), getattr(self, "_wheel_cy", None))

    def _on_press(self, event) -> None:
        # 1. 检测点击 HUD 探针卡片按钮（带 4px 容差补偿，确保边缘点击 100% 命中）
        for bx0, by0, bx1, by1, act in getattr(self, "_hud_btns", []):
            if (bx0 - 4) <= event.x <= (bx1 + 4) and (by0 - 4) <= event.y <= (by1 + 4):
                # HUD 步进后保留 ←/→/Esc 键盘巡航的焦点链（否则焦点留在别处）
                self.canvas.focus_set()
                if act == "prev":
                    self._step_node(-1)
                elif act == "next":
                    self._step_node(1)
                elif act == "center":
                    self._center_node(self._selected_node_idx)
                elif act == "close":
                    self._deselect_node()
                return

        # 1b. 若点击落在 HUD 卡片主体上，拦截事件防止误触底层拖拽
        if getattr(self, "_hud_card_box", None) and (
            self._hud_card_box[0] <= event.x <= self._hud_card_box[1] and
            self._hud_card_box[2] <= event.y <= self._hud_card_box[3]
        ):
            return

        # 2. 检测是否单击质点节点进入探针巡航（命中半径与悬浮高亮 24px 对齐）
        for idx, item in enumerate(self._pts):
            nx, ny = item[0], item[1]
            if math.hypot(event.x - nx, event.y - ny) <= 24:
                self._select_node(idx)
                return

        # 3. 空白处拖拽平移准备
        self._drag_data = {"x": event.x, "y": event.y, "panned": False}
        self.canvas.config(cursor="fleur")

    def _on_double_click(self, event) -> None:
        # 双击如果落在 HUD 探针卡片按钮上，视为连续单步递进，不重置缩放
        for bx0, by0, bx1, by1, act in getattr(self, "_hud_btns", []):
            if (bx0 - 4) <= event.x <= (bx1 + 4) and (by0 - 4) <= event.y <= (by1 + 4):
                if act == "prev":
                    self._step_node(-1)
                elif act == "next":
                    self._step_node(1)
                elif act == "center":
                    self._center_node(self._selected_node_idx)
                elif act == "close":
                    self._deselect_node()
                return
        if getattr(self, "_hud_card_box", None) and (
            self._hud_card_box[0] <= event.x <= self._hud_card_box[1] and
            self._hud_card_box[2] <= event.y <= self._hud_card_box[3]
        ):
            return
        # 双击命中质点 → 仅选中进入探针巡航，不触发视口复位
        for idx, item in enumerate(self._pts):
            nx, ny = item[0], item[1]
            if math.hypot(event.x - nx, event.y - ny) <= 24:
                self._select_node(idx)
                return
        self._reset_zoom()

    def _on_drag(self, event) -> None:
        dx = event.x - self._drag_data["x"]
        dy = event.y - self._drag_data["y"]
        if abs(dx) > 1 or abs(dy) > 1:
            self._drag_data["panned"] = True
            if self._zoom_scale > 1.01:
                self._pan_x += dx
                self._pan_y += dy
                self._drag_data["x"] = event.x
                self._drag_data["y"] = event.y
                self._clamp_pan()
                if not getattr(self, "_drag_redraw_pending", False):
                    self._drag_redraw_pending = True
                    self._drag_redraw_after = self.canvas.after(16, self._do_drag_redraw)

    def _on_release(self, event) -> None:
        self.canvas.config(cursor=CLICK_CURSOR)
        self._drag_redraw_pending = False
        if getattr(self, "_drag_data", {}).get("panned"):
            self._redraw()

    def _clamp_pan(self, plot_w: float | None = None, plot_h: float | None = None) -> None:
        if self._zoom_scale <= 1.01:
            self._pan_x = 0.0
            self._pan_y = 0.0
            return
        if plot_w is None or plot_h is None:
            plot_w, plot_h = self._plot_size()
        min_pan_x = plot_w * (1.0 - self._zoom_scale) - 80
        max_pan_x = 80
        min_pan_y = plot_h * (1.0 - self._zoom_scale) - 80
        max_pan_y = 80
        self._pan_x = max(min_pan_x, min(max_pan_x, self._pan_x))
        self._pan_y = max(min_pan_y, min(max_pan_y, self._pan_y))
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        self._pts.clear()
        self._regime_boxes.clear()
        self._waterbed_boxes.clear()
        self._impulse_boxes.clear()
        self._hud_btns.clear()
        self._barrier_box = None
        self._horizon_box = None
        self._hud_card_box = None
        pad_l, pad_r, pad_t, pad_b = self._PAD_L, self._PAD_R, self._PAD_T, self._PAD_B
        plot_w, plot_h = self._plot_size()
        if plot_w <= 10 or plot_h <= 10:
            return
        raw_traj = list(self._data.get("trajectory") or [])
        total_turns = self._total_turns(raw_traj)

        # 防御性终态事实锚定：若模型在中间突变点提前闭合数组，自动补齐真实会话终态节点
        traj = list(raw_traj)
        if traj and total_turns > 1:
            last_pt = traj[-1]
            last_t = last_pt.get("turn")
            if isinstance(last_t, (int, float)) and last_t < total_turns:
                terminal_pt = {
                    "turn": total_turns,
                    "user_turn": last_pt.get("user_turn") or last_pt.get("u"),
                    "semantic_distance": last_pt.get("semantic_distance", 0.35),
                    "vector": "positive" if last_pt.get("vector") in ("positive", "convergent") else "neutral",
                    "event": "normal",
                    "note": "会话终态收敛" if last_pt.get("vector") in ("positive", "convergent") else "会话终态",
                }
                traj.append(terminal_pt)

        if getattr(self, "_view_mode", "manifold") == "phase_plane":
            self._draw_phase_plane(c, plot_w, plot_h, pad_l, pad_r, pad_t, pad_b, traj)
        else:
            self._draw_manifold(c, plot_w, plot_h, pad_l, pad_r, pad_t, pad_b, traj)
    def _draw_waddington_landscape(self, pad_l: float, pad_t: float, plot_w: float, plot_h: float, aa_items: list,
                                  zoom: float = 1.0, pan_x: float = 0.0, pan_y: float = 0.0) -> None:
        """绘制 Waddington 双阱表观遗传连续势能曲面等高线。"""
        steps = 36
        plot_w_eff = plot_w * zoom
        plot_h_eff = plot_h * zoom
        for k in range(6):
            base_y_frac = 0.16 + k * 0.14
            base_y = pad_t + pan_y + base_y_frac * plot_h_eff
            line_pts = []
            for s in range(steps + 1):
                ds = 0.04 + (s / steps) * 0.92
                px = pad_l + pan_x + ds * plot_w_eff
                v = metrics.compute_waddington_potential(ds)
                py = base_y - v * (plot_h_eff * 0.09)
                line_pts.append((px, py))
            if k == 0 or k == 5:
                col = theme.PHASE_CONTOUR_LOW
            elif k in (2, 3):
                col = theme.PHASE_CONTOUR_HIGH
            else:
                col = theme.PHASE_CONTOUR_MID
            aa_items.append(("line", line_pts, col, 1))
    def _draw_regime_band(self, c, pad_l: float, pad_t: float, plot_w: float, plot_h: float, traj: list) -> None:
        """在画布底座绘制 SLDS 四相态连续光谱流带。"""
        self._regime_boxes.clear()
        if not traj:
            return
        band_y0 = pad_t + plot_h + 24
        band_h = 10
        band_y1 = band_y0 + band_h

        c.create_text(pad_l - 8, (band_y0 + band_y1) / 2, text="阶段演化",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")

        n_steps = len(traj)
        regime_colors = {
            "gas": theme.REGIME_GAS,
            "liquid": theme.REGIME_LIQUID,
            "glass": theme.REGIME_GLASS,
            "crystal": theme.REGIME_CRYSTAL,
        }

        for i, pt in enumerate(traj):
            reg = str(pt.get("regime") or "liquid").lower()
            col = regime_colors.get(reg, theme.REGIME_LIQUID)

            x0 = pad_l + (i / n_steps) * plot_w
            x1 = pad_l + ((i + 1) / n_steps) * plot_w

            c.create_rectangle(x0, band_y0, x1, band_y1, fill=col, outline=theme.BG, width=1)

            turn_val = pt.get("turn", i + 1)
            ed_val = float(pt.get("epistemic_debt", 1.0))
            self._regime_boxes.append((x0, x1, band_y0, band_y1, turn_val, reg, ed_val))

    def _draw_manifold(self, c, plot_w, plot_h, pad_l, pad_r, pad_t, pad_b, traj) -> None:
        """经典状态-代价时序流形视图：横轴语义距离 Ds × 纵轴物理成本 $。"""
        zoom = self._zoom_scale
        pan_x = self._pan_x
        pan_y = self._pan_y
        plot_w_eff = plot_w * zoom
        plot_h_eff = plot_h * zoom

        self._draw_manifold_y_axis(c, plot_w, plot_h, pad_l, pad_t, pan_y, plot_h_eff)
        self._draw_manifold_horizon(c, plot_w, plot_h, pad_l, pad_t, traj, pan_x, plot_w_eff)
        self._draw_manifold_axis_legend(c, plot_w, plot_h, pad_l, pad_t)

        aa_items: list = []

        # (A0) Waddington 双阱连续势能等高线
        self._draw_waddington_landscape(pad_l, pad_t, plot_w, plot_h, aa_items, zoom, pan_x, pan_y)

        # (A1) 鞍点势垒警戒脊线
        self._draw_manifold_barrier(c, pad_l, pad_t, plot_h, pan_x, plot_w_eff)

        # (A2) 理想向心收敛走廊参考线
        corridor_pts = [
            (pad_l + pan_x + 0.85 * plot_w_eff, pad_t + pan_y + plot_h_eff * 0.95),
            (pad_l + pan_x + 0.50 * plot_w_eff, pad_t + pan_y + plot_h_eff * 0.92),
            (pad_l + pan_x + 0.22 * plot_w_eff, pad_t + pan_y + plot_h_eff * 0.90),
            (pad_l + pan_x + 0.05 * plot_w_eff, pad_t + pan_y + plot_h_eff * 0.88),
        ]
        aa_items.append(("line", corridor_pts, theme.PHASE_CORRIDOR, 1))

        # (C) 平庸代码吸引子 + (D) 狄拉克目标点
        self._draw_manifold_poles(aa_items, pad_l, pad_t, pan_x, pan_y, zoom, plot_w_eff, plot_h_eff)

        # (E) 计算真实会话动力学轨迹节点与连线
        total_turns = self._total_turns(traj)
        self._calc_manifold_pts(traj, pad_l, pad_t, pan_x, pan_y, zoom, plot_w_eff, plot_h_eff, total_turns)
        self._draw_manifold_links(aa_items)
        impulse_dashed_lines, key_impulses = self._collect_manifold_impulses()
        # (3) 多体系统卫星微质点群 (Subagent Satellites)
        self._draw_satellite_dots(aa_items, 20)
        self._draw_manifold_halos(aa_items)

        self._aa_layer_flat(c, aa_items, "aa_data")
        # 控制论水床效应扰动弧
        self._draw_waterbed_arcs(c, traj)
        for lx0, ly0, lx1, ly1, col, d_pat, lw in impulse_dashed_lines:
            c.create_line(lx0, ly0, lx1, ly1, fill=col, dash=d_pat, width=lw)

        self._draw_manifold_labels(c, pad_t, key_impulses)
        if not traj:
            c.create_text(pad_l + plot_w / 2, pad_t + plot_h / 2,
                          text="（本动力学报告无细分轨迹采样数据）", fill=theme.MUTED)
        self._draw_regime_band(c, pad_l, pad_t, plot_w, plot_h, traj)
        self._draw_hud_card(c, plot_w, plot_h, pad_l, pad_t)

    def _draw_manifold_y_axis(self, c, plot_w, plot_h, pad_l, pad_t, pan_y, plot_h_eff) -> None:
        """(Y 轴) 物理成本刻度虚线与标签。"""
        cost_str = str(self._report.get("cost_display") or "")
        m_cost = re.search(r"(\d+(?:\.\d+)?)", cost_str)
        max_cost_val = float(m_cost.group(1)) if m_cost else 10.0
        if max_cost_val <= 0:
            max_cost_val = 10.0
        y_ticks = []
        for frac in (1.0, 0.75, 0.50, 0.25, 0.0):
            gy = pad_t + pan_y + (1.0 - frac) * plot_h_eff
            if pad_t - 6 <= gy <= pad_t + plot_h + 6:
                if 0.0 < frac < 1.0:
                    c.create_line(pad_l, gy, pad_l + plot_w, gy, fill=theme.BORDER, dash=(2, 4))
                val = max_cost_val * frac
                val_txt = f"${val:.1f}" if max_cost_val >= 1 else f"${val:.2f}"
                y_ticks.append((gy, val_txt))
                c.create_text(pad_l - 8, gy, text=val_txt, fill=theme.MUTED,
                              font=theme.FONT_UI_SMALL, anchor="e")

    def _draw_manifold_horizon(self, c, plot_w, plot_h, pad_l, pad_t, traj, pan_x, plot_w_eff) -> None:
        """关键状态分界线与止损红线（Ds=0.82 不可逆视界）及拦截状态胶囊。"""
        line_target_x = pad_l + pan_x + 0.10 * plot_w_eff
        line_danger_x = pad_l + pan_x + 0.90 * plot_w_eff
        c.create_line(line_target_x, pad_t, line_target_x, pad_t + plot_h, fill=theme.PHASE_GRID_DIRAC, dash=(1, 4))
        c.create_line(line_danger_x, pad_t, line_danger_x, pad_t + plot_h, fill=theme.PHASE_GRID_TRAP, dash=(1, 4))

        # 止损红线与不可逆视界 (Ds=0.82)
        _lams, _avg_lam, horizon_status = self._compute_lyapunov_stats(traj, self._data.get("lyapunov_exponent"))
        line_horizon_x = pad_l + pan_x + 0.82 * plot_w_eff
        c.create_line(line_horizon_x, pad_t + 18, line_horizon_x, pad_t + plot_h - 18, fill=theme.ERROR, dash=(3, 5), width=2)
        if horizon_status == "intercepted":
            lbl_txt = "成功纠偏 · 挽回失控"
            bw = len(lbl_txt) * 11 + 24
            bx0, by0, bx1, by1 = line_horizon_x - bw - 10, pad_t + 18, line_horizon_x - 10, pad_t + 38
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.DIRAC_CORE_BG, outline=theme.DIRAC_WELL_BORDER, width=1)
            c.create_oval(bx0 + 8, by0 + 7, bx0 + 14, by0 + 13, fill=theme.SUCCESS, outline="")
            c.create_text(bx0 + 20, (by0 + by1) / 2, text=lbl_txt, fill=theme.SUCCESS, font=theme.FONT_UI_SMALL_BOLD, anchor="w")
            self._horizon_box = (bx0, bx1, by0, by1, line_horizon_x, pad_t, plot_h, lbl_txt, "intercepted")
        elif horizon_status == "breached":
            lbl_txt = "越界失控"
            bw = len(lbl_txt) * 11 + 24
            bx0, by0, bx1, by1 = line_horizon_x + 10, pad_t + 18, line_horizon_x + bw + 10, pad_t + 38
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.ATTRACTOR_RINGS[0], outline=theme.ATTRACTOR_BASIN_BORDER, width=1)
            c.create_oval(bx0 + 8, by0 + 7, bx0 + 14, by0 + 13, fill=theme.ERROR, outline="")
            c.create_text(bx0 + 20, (by0 + by1) / 2, text=lbl_txt, fill=theme.ERROR, font=theme.FONT_UI_SMALL_BOLD, anchor="w")
            self._horizon_box = (bx0, bx1, by0, by1, line_horizon_x, pad_t, plot_h, lbl_txt, "breached")

    def _draw_manifold_axis_legend(self, c, plot_w, plot_h, pad_l, pad_t) -> None:
        """X 轴底线、顶部图例说明 (Legend) 与底部 X 轴说明。"""
        # X 轴底线
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill=theme.BORDER, width=1)

        # (F) 顶部图例说明 (Legend)
        leg_y = pad_t - 14
        leg_x = pad_l + 10
        c.create_line(leg_x, leg_y, leg_x + 18, leg_y, fill=theme.SUCCESS, width=2)
        c.create_text(leg_x + 22, leg_y, text="向心推进", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_line(leg_x + 80, leg_y, leg_x + 98, leg_y, fill=theme.ERROR, width=2)
        c.create_text(leg_x + 102, leg_y, text="离心偏离", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_oval(leg_x + 160, leg_y - 4, leg_x + 168, leg_y + 4, outline=theme.DIRAC_WELL_BORDER, fill=theme.DIRAC_CORE_BG, width=1)
        c.create_text(leg_x + 172, leg_y, text="目标点", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_oval(leg_x + 260, leg_y - 4, leg_x + 268, leg_y + 4, outline=theme.ATTRACTOR_BASIN_BORDER, fill=theme.ATTRACTOR_RINGS[0], width=1)
        c.create_text(leg_x + 272, leg_y, text="惯性套路区", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")

        # 底部 X 轴说明
        c.create_text(pad_l, pad_t + plot_h + 12, text="0.0 (契合真实意图)",
                      fill=theme.SUCCESS, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w / 2, pad_t + plot_h + 12,
                      text="语义距离：向左逼近目标达成 · 向右偏离真实意图",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="center")
        c.create_text(pad_l + plot_w, pad_t + plot_h + 12, text="1.0 (严重偏离意图)",
                      fill=theme.ERROR, font=theme.FONT_UI_SMALL, anchor="e")

    def _draw_manifold_barrier(self, c, pad_l, pad_t, plot_h, pan_x, plot_w_eff) -> None:
        """(A1) 鞍点势垒警戒脊线与突破/未突破状态胶囊。"""
        line_barrier_x = pad_l + pan_x + 0.55 * plot_w_eff
        c.create_line(line_barrier_x, pad_t + 18, line_barrier_x, pad_t + plot_h - 18,
                      fill=theme.PHASE_BARRIER_CREST, dash=(2, 4), width=1)
        barrier_crossed = bool(self._data.get("barrier_crossed"))
        barrier_turn = self._data.get("barrier_turn")
        if barrier_crossed:
            b_txt = f"转折突破 T{barrier_turn}" if barrier_turn else "转折突破"
            bw = len(b_txt) * 9 + 16
            bx0, by0, bx1, by1 = line_barrier_x - bw / 2, pad_t + 18, line_barrier_x + bw / 2, pad_t + 36
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.CARD_HEADER_BG, outline=theme.PHASE_BARRIER_CREST, width=1)
            c.create_text(line_barrier_x, (by0 + by1) / 2, text=b_txt,
                          fill=theme.PHASE_BARRIER_CREST, font=theme.FONT_UI_SMALL_BOLD, anchor="center")
            self._barrier_box = (bx0, bx1, by0, by1, line_barrier_x, pad_t, plot_h, b_txt, True)
        else:
            b_txt = "转折关口 · Ds 0.55"
            bw = len(b_txt) * 8 + 14
            bx0, by0, bx1, by1 = line_barrier_x - bw / 2, pad_t + 18, line_barrier_x + bw / 2, pad_t + 36
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.CARD_HEADER_BG, outline=theme.BORDER, width=1)
            c.create_text(line_barrier_x, (by0 + by1) / 2, text=b_txt,
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="center")
            self._barrier_box = (bx0, bx1, by0, by1, line_barrier_x, pad_t, plot_h, b_txt, False)

    def _draw_manifold_poles(self, aa_items, pad_l, pad_t, pan_x, pan_y, zoom, plot_w_eff, plot_h_eff) -> None:
        """(C) 平庸代码吸引子引力势阱同心圆 + (D) 狄拉克目标点。"""
        att_x = pad_l + pan_x + plot_w_eff * 0.86
        att_y = pad_t + pan_y + 90 * zoom
        self._att_pos = (att_x, att_y)
        self._pad_t = pad_t
        for r, col in zip((76, 56, 40), (theme.ATTRACTOR_BASIN_BORDER, theme.ATTRACTOR_BASIN_BORDER, theme.ATTRACTOR_RINGS[0])):
            aa_items.append(("dot", att_x, att_y, r * zoom, None, col, 1))
        for r, col in zip((28, 18, 10), theme.ATTRACTOR_RINGS):
            aa_items.append(("dot", att_x, att_y, r * zoom, col, theme.ATTRACTOR_RINGS[1], 1))
        aa_items.append(("dot", att_x, att_y, 4 * zoom, theme.ERROR, theme.ERROR, 1))

        tgt_x = pad_l + pan_x + plot_w_eff * 0.05
        tgt_y = pad_t + pan_y + plot_h_eff * 0.88
        self._tgt_pos = (tgt_x, tgt_y)
        for r in (36, 24):
            aa_items.append(("dot", tgt_x, tgt_y, r * zoom, None, theme.DIRAC_WELL_BORDER, 1))
        aa_items.append(("dot", tgt_x, tgt_y, 16 * zoom, theme.DIRAC_CORE_BG, theme.SUCCESS, 2))
        aa_items.append(("dot", tgt_x, tgt_y, 5 * zoom, theme.SUCCESS, theme.SUCCESS, 1))

    def _calc_manifold_pts(self, traj, pad_l, pad_t, pan_x, pan_y, zoom, plot_w_eff, plot_h_eff, total_turns) -> None:
        """(E) 计算轨迹节点屏幕坐标并填充 self._pts（LLM 遥测数值容错）。"""
        n = len(traj)
        for idx, pt in enumerate(traj):
            try:
                ds = metrics.ds_of(pt)
            except (ValueError, TypeError):
                ds = 0.5
            px = pad_l + pan_x + ds * plot_w_eff
            t_val = pt.get("turn")
            if isinstance(t_val, (int, float)) and total_turns > 1:
                frac = max(0.0, min(1.0, (float(t_val) - 1.0) / (float(total_turns) - 1.0)))
            else:
                frac = idx / (n - 1) if n > 1 else 0.5
            py = (pad_t + pan_y + plot_h_eff) - frac * plot_h_eff * 0.82 - 8 * zoom
            offset_y = -14 if (idx % 2 == 0 and py > pad_t + 28) else 14
            self._pts.append((px, py, pt, offset_y))

    def _draw_manifold_links(self, aa_items) -> None:
        """轨迹连线：SNR 光晕底衬层 + 矢量着色段与推进箭头。"""
        for i in range(1, len(self._pts)):
            x0, y0, prev_pt = self._pts[i - 1][:3]
            x1, y1, cur_pt = self._pts[i][:3]
            snr_prev, _ = self._derive_flux_and_snr(prev_pt)
            snr_cur, _ = self._derive_flux_and_snr(cur_pt, prev_pt)
            avg_snr = (snr_prev + snr_cur) / 2.0
            if avg_snr >= 0.60:
                glow_lw = max(4, int(round(avg_snr * 8)))
                aa_items.append(("line", [(x0, y0), (x1, y1)], theme.DIRAC_WELL_BORDER, glow_lw))
            elif avg_snr < 0.35:
                aa_items.append(("line", [(x0, y0), (x1, y1)], theme.ATTRACTOR_BASIN_BORDER, 3))

        for i in range(1, len(self._pts)):
            x0, y0, prev_pt = self._pts[i - 1][:3]
            x1, y1, cur_pt = self._pts[i][:3]
            prev_ds = metrics.ds_of(prev_pt)
            cur_ds = metrics.ds_of(cur_pt)
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

    def _collect_manifold_impulses(self) -> tuple[list, list]:
        """(2) 人类信源信息注入冲量引导线：仅收集最具向心突破意义的至多 2 条
        关键纠偏（杜绝数十条灰线遮挡主轨迹）。空轨迹时安全返回空表。"""
        impulse_dashed_lines: list[tuple[float, float, float, float, str, tuple, int]] = []
        key_impulses: list = []
        for i_pt, item in enumerate(self._pts):
            x, y, pt = item[:3]
            prev_pt = self._pts[i_pt - 1][2] if i_pt > 0 else None
            _snr, impulse = self._derive_flux_and_snr(pt, prev_pt)
            if impulse and impulse.get("flux") == "high":
                cur_ds = metrics.ds_of(pt)
                prev_ds = metrics.ds_of(prev_pt) if prev_pt else 0.5
                if cur_ds < prev_ds - 0.01:
                    key_impulses.append((i_pt, x, y, pt, impulse))

        for i_pt, x, y, pt, impulse in key_impulses[:2]:
            u_val = pt.get("user_turn") or pt.get("u")
            arr_len = 38
            ix0 = x + arr_len * 0.75
            iy0 = y - arr_len * 0.45
            impulse_dashed_lines.append((x + 4, y - 2, ix0, iy0, theme.PHASE_IMPULSE, (3, 2), 1))
            self._impulse_boxes.append((x + 28, x + 120, y - 26, y - 8, u_val, "high", impulse.get("note", "")))
        return impulse_dashed_lines, key_impulses

    @staticmethod
    def _satellite_ring(x: float, y: float, subs: list, orbit_r: float) -> list[tuple[float, float, dict]]:
        """按轨道环均分计算子智能体卫星落点 [(sx, sy, sub), ...]（至多 3 颗）。"""
        k_total = len(subs)
        out = []
        for k, sub in enumerate(subs[:3]):
            angle = -math.pi / 4 + k * (math.pi * 2 / max(k_total, 3))
            out.append((x + orbit_r * math.cos(angle), y + orbit_r * math.sin(angle), sub))
        return out

    def _draw_satellite_dots(self, aa_items: list, orbit_r: float) -> None:
        """(3) 多体系统卫星微质点群：主质点周围画引力细线与卫星微点（抗锯齿层）。"""
        for item in self._pts:
            x, y, pt = item[:3]
            subs = self._calc_subagents(pt)
            for sx, sy, sub in self._satellite_ring(x, y, subs, orbit_r):
                s_status = str(sub.get("status") or "convergent").lower()
                s_col = theme.SUCCESS if s_status == "convergent" else theme.ERROR
                tether_col = theme.DIRAC_WELL_BORDER if s_status == "convergent" else theme.ATTRACTOR_BASIN_BORDER
                aa_items.append(("line", [(x, y), (sx, sy)], tether_col, 1))
                aa_items.append(("dot", sx, sy, 3, s_col, theme.FG_WHITE, 1))

    # LLM 遥测 subagents 的常用英文角色名 → 平实中文（图面统一说人话）
    _SUB_NAME_CN = {
        "scout": "探查", "worker": "执行", "reviewer": "审查",
        "planner": "规划", "researcher": "调研", "explorer": "探查",
        "implementer": "实现", "coder": "编码", "tester": "测试",
    }

    @classmethod
    def _sub_display_name(cls, sub: dict) -> str:
        raw = str(sub.get("name") or "Sub")
        return cls._SUB_NAME_CN.get(raw.strip().lower(), raw)

    def _draw_satellite_labels(self, c, orbit_r: float, cluster_px: float) -> None:
        """卫星名文本标注：密集簇内（与其他质点距 < cluster_px）只画微点不写名。"""
        n_pts = len(self._pts)
        for i, item in enumerate(self._pts):
            x, y, pt = item[:3]
            subs = self._calc_subagents(pt)
            if not subs:
                continue
            is_clustered = any(j != i and ((self._pts[j][0] - x)**2 + (self._pts[j][1] - y)**2) < cluster_px**2
                               for j in range(n_pts))
            if is_clustered:
                continue
            for sx, sy, sub in self._satellite_ring(x, y, subs, orbit_r):
                s_name = self._sub_display_name(sub)
                s_col = theme.SUCCESS if str(sub.get("status") or "").lower() == "convergent" else theme.ERROR
                c.create_text(sx + 5, sy, text=s_name, fill=s_col,
                              font=theme.FONT_UI_SMALL_BOLD, anchor="w")

    def _draw_manifold_halos(self, aa_items) -> None:
        """轨迹质点：认知负债动态光晕 + 起终点/事件环 + 矢量基色核。"""
        n_pts = len(self._pts)
        for i_pt, item in enumerate(self._pts):
            x, y, pt = item[:3]
            vec = str(pt.get("vector") or "neutral").lower()
            event_tag = str(pt.get("event") or "normal").lower()
            base_col = theme.ERROR if vec in ("negative", "divergent", "trapped") else (
                theme.SUCCESS if vec in ("positive", "convergent") else theme.WARNING)
            # 认知负债动态光晕
            ed = float(pt.get("epistemic_debt", 1.0))
            if ed >= 4.0:
                aa_items.append(("dot", x, y, 13, None, theme.DEBT_GLOW_DANGER, 1))
                aa_items.append(("dot", x, y, 17, None, theme.DEBT_GLOW_DANGER, 1))
            elif ed <= 1.0:
                aa_items.append(("dot", x, y, 9, None, theme.DEBT_GLOW_SAFE, 1))

            if i_pt == 0:
                aa_items.append(("dot", x, y, 8, None, theme.PHASE_START_HALO, 1))
            elif i_pt == n_pts - 1:
                aa_items.append(("dot", x, y, 8, None, theme.SUCCESS if vec in ("positive", "convergent") else theme.ERROR, 1))
            if event_tag == "retry_loop":
                aa_items.append(("dot", x, y, 9, None, theme.ERROR, 1))
            elif event_tag == "breakthrough":
                aa_items.append(("dot", x, y, 9, None, theme.SUCCESS, 1))
            elif event_tag == "compaction":
                aa_items.append(("dot", x, y, 8, None, theme.CHART_PALETTE[0], 1))
            elif vec in ("negative", "divergent", "trapped"):
                aa_items.append(("dot", x, y, 8, None, theme.ERROR, 1))
            aa_items.append(("dot", x, y, 5, base_col, theme.FG_WHITE, 2))

    def _draw_waterbed_arcs(self, c, traj) -> None:
        """控制论水床效应扰动弧：16 段二次贝塞尔自适应起拱 + 胶囊标签。"""
        waterbed_turns = metrics.detect_waterbed_events(traj)
        self._waterbed_turns = set(waterbed_turns)
        for wb_t in waterbed_turns:
            for i_wb in range(1, len(self._pts)):
                cur_pt = self._pts[i_wb][2]
                cur_t = cur_pt.get("turn") or (i_wb + 1)
                if cur_t == wb_t:
                    x0, y0 = self._pts[i_wb - 1][:2]
                    x1, y1 = self._pts[i_wb][:2]
                    mid_x = (x0 + x1) / 2.0
                    arch_h = max(26.0, min(52.0, math.hypot(x1 - x0, y1 - y0) * 0.45))
                    mid_y = min(y0, y1) - arch_h
                    arc_pts = []
                    for t_step in range(17):
                        t_ratio = t_step / 16.0
                        bx = (1 - t_ratio) ** 2 * x0 + 2 * (1 - t_ratio) * t_ratio * mid_x + t_ratio ** 2 * x1
                        by = (1 - t_ratio) ** 2 * y0 + 2 * (1 - t_ratio) * t_ratio * mid_y + t_ratio ** 2 * y1
                        arc_pts.append((bx, by))
                    for idx_arc in range(len(arc_pts) - 1):
                        c.create_line(arc_pts[idx_arc][0], arc_pts[idx_arc][1],
                                      arc_pts[idx_arc + 1][0], arc_pts[idx_arc + 1][1],
                                      fill=theme.WATERBED_ARC, dash=(3, 3), width=1)
                    wb_bw = 64
                    wb_bx0, wb_by0, wb_bx1, wb_by1 = mid_x - wb_bw / 2, mid_y - 14, mid_x + wb_bw / 2, mid_y + 4
                    c.create_rectangle(wb_bx0, wb_by0, wb_bx1, wb_by1, fill=theme.CARD_HEADER_BG, outline=theme.WATERBED_ARC, width=1)
                    c.create_text(mid_x, (wb_by0 + wb_by1) / 2, text="连锁反应", fill=theme.WATERBED_ARC,
                                  font=theme.FONT_UI_SMALL_BOLD, anchor="center")
                    self._waterbed_boxes.append((wb_bx0, wb_bx1, wb_by0, wb_by1, wb_t))
                    break

    def _draw_manifold_labels(self, c, pad_t, key_impulses) -> None:
        """上层文本标签：回合号 / 关键纠偏冲量 / 事件标签 / 失控拐点（2D 欧氏防叠）。"""
        dspec = self._data.get("damping_spectrum") or {}
        bif_turns = {b["turn"] for b in dspec.get("bifurcations", []) if b.get("to_cat") == "underdamped"}
        n_pts = len(self._pts)
        drawn_label_boxes: list[tuple[float, float]] = []
        seen_drawn_u: set = set()
        prev_event_tag = None

        for i, item in enumerate(self._pts):
            x, y, pt = item[:3]
            t_val = pt.get("turn")
            event_tag = str(pt.get("event") or "normal").lower()
            u_val = pt.get("user_turn") or pt.get("u")
            prev_u = (self._pts[i - 1][2].get("user_turn") or self._pts[i - 1][2].get("u")) if i > 0 else None

            # 判断局部轨迹切线是否近乎垂直（陡峭爬升/暴跌段）
            is_vertical = False
            if i > 0 and i < n_pts - 1:
                dx = abs(self._pts[i + 1][0] - self._pts[i - 1][0])
                dy = abs(self._pts[i + 1][1] - self._pts[i - 1][1])
                is_vertical = (dy > 1.3 * max(1.0, dx))

            # 候选偏移位置：若为陡峭垂直段，左右错开排版；否则上下交错
            if is_vertical:
                offset_x = -26 if (i % 2 == 0) else 26
                offset_y = 0
                anchor_pos = "e" if (i % 2 == 0) else "w"
            else:
                offset_x = 0
                offset_y = item[3] if len(item) > 3 else (-14 if (i % 2 == 0 and y > pad_t + 28) else 14)
                anchor_pos = "center"

            cand_x = x + offset_x
            cand_y = y + offset_y

            is_milestone = (i == 0 or i == n_pts - 1 or event_tag in ("breakthrough", "compaction") or any(k[0] == i for k in key_impulses[:2]))
            # 2D 欧氏防叠检测 (dx^2 + dy^2 < 38^2)
            is_colliding = any((cand_x - lx)**2 + (cand_y - ly)**2 < 38**2 for lx, ly in drawn_label_boxes)

            if is_colliding and is_vertical:
                alt_offset_x = -offset_x
                alt_cand_x = x + alt_offset_x
                if not any((alt_cand_x - lx)**2 + (cand_y - ly)**2 < 38**2 for lx, ly in drawn_label_boxes):
                    cand_x = alt_cand_x
                    anchor_pos = "e" if alt_offset_x < 0 else "w"
                    is_colliding = False

            can_draw = is_milestone or not is_colliding

            prev_pt = self._pts[i - 1][2] if i > 0 else None
            _snr, impulse = self._derive_flux_and_snr(pt, prev_pt)

            if can_draw:
                drawn_label_boxes.append((cand_x, cand_y))
                t_str = f"T{t_val}·U{u_val}" if (t_val is not None and u_val is not None) else (f"T{t_val}" if t_val is not None else "")
                c.create_text(cand_x, cand_y, text=t_str, fill=theme.FG_WHITE,
                              font=theme.FONT_UI_SMALL_BOLD, anchor=anchor_pos)

            # 人类冲量文本标注（仅高亮极其关键的突破纠偏，其余常规指令收拢至悬浮卡片）
            if impulse and impulse.get("flux") == "high" and any(k[0] == i for k in key_impulses[:2]):
                c.create_text(x + 36, y - 19, text=f"U{u_val} 关键纠偏", fill=theme.PHASE_IMPULSE,
                              font=theme.FONT_UI_SMALL_BOLD, anchor="w")

            # 事件标签标注（连续同类事件如重试死锁，仅在首轮标记一次，杜绝重叠堆叠）
            is_new_event = (event_tag != prev_event_tag or event_tag in ("breakthrough", "barrier_leap", "compaction", "course_correction", "prune"))
            recognized_events = ("retry_loop", "breakthrough", "barrier_leap", "compaction", "test_fail", "course_correction", "prune")
            if is_new_event and event_tag in recognized_events:
                evt_labels = {
                    "retry_loop": "重试",
                    "breakthrough": "突破",
                    "barrier_leap": "转正突破",
                    "course_correction": "转折纠偏",
                    "prune": "回退剪枝",
                    "compaction": "压缩",
                    "test_fail": "报错",
                }
                lbl_text = evt_labels.get(event_tag, "")
                if event_tag in ("breakthrough", "barrier_leap"):
                    evt_col = theme.SUCCESS
                elif event_tag == "course_correction":
                    evt_col = theme.ACCENT
                elif event_tag == "prune":
                    evt_col = theme.WARNING
                elif event_tag == "compaction":
                    evt_col = theme.MUTED
                else:
                    evt_col = theme.ERROR

                evt_y = cand_y + (10 if offset_y >= 0 else -10) if can_draw else y + 12
                evt_anchor = anchor_pos if can_draw else "center"
                c.create_text(cand_x if can_draw else x, evt_y,
                              text=lbl_text, fill=evt_col,
                              font=theme.FONT_UI_SMALL, anchor=evt_anchor)
            prev_event_tag = event_tag

            if t_val in bif_turns and event_tag not in recognized_events:
                c.create_text(cand_x if can_draw else x, (cand_y + 10) if can_draw else (y + 12),
                              text="失控拐点", fill=theme.ERROR,
                              font=theme.FONT_UI_SMALL, anchor=anchor_pos if can_draw else "center")
        # 卫星标签标注（密集簇只画微质点与引力细线，避免遮挡相邻主轨迹与水床标签）
        self._draw_satellite_labels(c, 20, 36)

    def _draw_phase_plane(self, c, plot_w, plot_h, pad_l, pad_r, pad_t, pad_b, traj) -> None:
        """P1 相速度极限环对偶相平面：横轴语义距离 Ds × 纵轴相速度 dDs/dt。"""
        zoom = self._zoom_scale
        pan_x = self._pan_x
        pan_y = self._pan_y
        plot_w_eff = plot_w * zoom
        zero_y = pad_t + 0.5 * plot_h + pan_y

        # 1. 计算相速度与非线性缩放坐标
        raw_nodes = self._calc_phase_nodes(traj)
        v_vals = [abs(n["v"]) for n in raw_nodes if n["v"] != 0]
        v_max = max(v_vals) if v_vals else 0.05
        v_max = max(0.005, v_max)

        for idx, n in enumerate(raw_nodes):
            ds = n["ds"]
            px = pad_l + pan_x + ds * plot_w_eff
            v = n["v"]
            ratio = (abs(v) / v_max) ** 0.55 if v_max > 0 else 0
            sign = 1 if v > 0 else (-1 if v < 0 else 0)
            # y 映射的常量项 (0.5*plot_h) 折进 world 坐标随 zoom 缩放：缩放锚定
            # 公式假设 screen = pad + pan + zoom*world，原式 zero_y 内含不随 zoom
            # 缩放的常量项会破坏仿射假设，放大时质点相对光标漂移
            py = pad_t + pan_y + zoom * (0.5 * plot_h - sign * ratio * 0.4 * plot_h)
            offset_y = -14 if (idx % 2 == 0 and py > pad_t + 28) else 14
            self._pts.append((px, py, n["pt"], offset_y, v))

        # 2. 背景参考系 (Layer 0) + 止损红线与视界判定 + X 轴底线
        self._draw_phase_reference(c, plot_w, plot_h, pad_l, pad_t, zero_y)
        self._draw_phase_horizon(c, plot_w, plot_h, pad_l, pad_t, traj, pan_x, plot_w_eff)
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill=theme.BORDER, width=1)

        # 3. 抗锯齿图层 items (Layer 1)
        aa_items: list = []
        self._draw_phase_poles(aa_items, pad_l, pan_x, zoom, plot_w_eff, zero_y)
        self._draw_phase_links(aa_items)
        # 多体系统卫星微质点群 (相平面投影)
        self._draw_satellite_dots(aa_items, 18)
        self._draw_phase_halos(aa_items)

        self._aa_layer_flat(c, aa_items, "aa_data")

        # 4. 上层文本标签 (Layer 2)
        self._draw_phase_legend(c, plot_w, plot_h, pad_l, pad_t)
        self._draw_phase_labels(c, pad_t)
        if not traj:
            c.create_text(pad_l + plot_w / 2, pad_t + plot_h / 2,
                          text="（本动力学报告无细分轨迹采样数据）", fill=theme.MUTED)
        self._draw_regime_band(c, pad_l, pad_t, plot_w, plot_h, traj)
        self._draw_hud_card(c, plot_w, plot_h, pad_l, pad_t)

    def _calc_phase_nodes(self, traj: list) -> list[dict]:
        """计算逐节点相速度 (dDs/dt) 与悬停元数据的原始节点表。"""
        raw_nodes = []
        for i, pt in enumerate(traj):
            ds = metrics.ds_of(pt)
            t = pt.get("turn", i + 1)
            if i == 0:
                v = 0.0
            else:
                prev = traj[i - 1]
                prev_ds = metrics.ds_of(prev)
                prev_t = prev.get("turn", i)
                dt = max(1, t - prev_t)
                v = (ds - prev_ds) / dt
            raw_nodes.append({"pt": pt, "turn": t, "u": pt.get("user_turn") or pt.get("u"), "ds": ds, "v": v, "evt": pt.get("event")})
        return raw_nodes

    def _draw_phase_reference(self, c, plot_w, plot_h, pad_l, pad_t, zero_y) -> None:
        """背景参考系：零平衡线 + 发散/收敛分区虚线与区名。"""
        c.create_line(pad_l, zero_y, pad_l + plot_w, zero_y, fill=theme.BORDER, width=2)
        c.create_text(pad_l - 6, zero_y, text="0 (平衡)", fill=theme.MUTED,
                      font=theme.FONT_UI_SMALL, anchor="e")

        div_y = pad_t + 0.18 * plot_h
        c.create_line(pad_l, div_y, pad_l + plot_w, div_y, fill=theme.PHASE_GRID_TRAP, dash=(2, 4))
        c.create_text(pad_l - 6, div_y, text="+发散", fill=theme.ERROR,
                      font=theme.FONT_UI_SMALL, anchor="e")
        c.create_text(pad_l + 12, pad_t + 12, text="↑ 离心发散区 (dDs/dt > 0 · 偏离真实意图)",
                      fill=theme.PHASE_ZONE_TRAP, font=theme.FONT_UI_SMALL, anchor="w")

        conv_y = pad_t + 0.82 * plot_h
        c.create_line(pad_l, conv_y, pad_l + plot_w, conv_y, fill=theme.PHASE_GRID_DIRAC, dash=(2, 4))
        c.create_text(pad_l - 6, conv_y, text="-收敛", fill=theme.SUCCESS,
                      font=theme.FONT_UI_SMALL, anchor="e")
        c.create_text(pad_l + 12, pad_t + plot_h - 12, text="↓ 向心收敛区 (dDs/dt < 0 · 逼近目标达成)",
                      fill=theme.PHASE_ZONE_DIRAC, font=theme.FONT_UI_SMALL, anchor="w")

    def _draw_phase_horizon(self, c, plot_w, plot_h, pad_l, pad_t, traj, pan_x, plot_w_eff) -> None:
        """止损红线（Ds=0.82）与视界拦截状态徽章胶囊（含 pan/zoom 偏移）。"""
        _lams_p, _avg_lam_p, horizon_status_p = self._compute_lyapunov_stats(traj, self._data.get("lyapunov_exponent"))
        line_horizon_x = pad_l + pan_x + 0.82 * plot_w_eff
        c.create_line(line_horizon_x, pad_t + 18, line_horizon_x, pad_t + plot_h - 18, fill=theme.ERROR, dash=(3, 5), width=2)
        # 止损视界拦截状态徽章胶囊（对偶相平面）
        if horizon_status_p == "intercepted":
            lbl_txt = "成功纠偏"
            bw = len(lbl_txt) * 12 + 24
            bx0, by0, bx1, by1 = line_horizon_x - bw - 10, pad_t + 18, line_horizon_x - 10, pad_t + 38
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.DIRAC_CORE_BG, outline=theme.DIRAC_WELL_BORDER, width=1)
            c.create_oval(bx0 + 8, by0 + 7, bx0 + 14, by0 + 13, fill=theme.SUCCESS, outline="")
            c.create_text(bx0 + 20, (by0 + by1) / 2, text=lbl_txt, fill=theme.SUCCESS, font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        elif horizon_status_p == "breached":
            lbl_txt = "偏离过大"
            bw = len(lbl_txt) * 12 + 24
            bx0, by0, bx1, by1 = line_horizon_x + 10, pad_t + 18, line_horizon_x + bw + 10, pad_t + 38
            c.create_rectangle(bx0, by0, bx1, by1, fill=theme.ATTRACTOR_RINGS[0], outline=theme.ATTRACTOR_BASIN_BORDER, width=1)
            c.create_oval(bx0 + 8, by0 + 7, bx0 + 14, by0 + 13, fill=theme.ERROR, outline="")
            c.create_text(bx0 + 20, (by0 + by1) / 2, text=lbl_txt, fill=theme.ERROR, font=theme.FONT_UI_SMALL_BOLD, anchor="w")

    def _draw_phase_poles(self, aa_items, pad_l, pan_x, zoom, plot_w_eff, zero_y) -> None:
        """狄拉克目标点、平庸代码吸引子与 retry_loop 极限环（抗锯齿层）。"""
        tgt_x = pad_l + pan_x + plot_w_eff * 0.05
        tgt_y = zero_y
        self._tgt_pos = (tgt_x, tgt_y)
        for r in (30, 20):
            aa_items.append(("dot", tgt_x, tgt_y, r * zoom, None, theme.DIRAC_WELL_BORDER, 1))
        aa_items.append(("dot", tgt_x, tgt_y, 14 * zoom, theme.DIRAC_CORE_BG, theme.SUCCESS, 2))
        aa_items.append(("dot", tgt_x, tgt_y, 5 * zoom, theme.SUCCESS, theme.SUCCESS, 1))

        att_x = pad_l + pan_x + plot_w_eff * 0.86
        att_y = zero_y
        self._att_pos = (att_x, att_y)
        for r, col in zip((48, 34, 22), (theme.ATTRACTOR_BASIN_BORDER, theme.ATTRACTOR_RINGS[0], theme.ATTRACTOR_RINGS[1])):
            aa_items.append(("dot", att_x, att_y, r * zoom, None, col, 1))
        aa_items.append(("dot", att_x, att_y, 4 * zoom, theme.ERROR, theme.ERROR, 1))
        cycle_pts = [item for item in self._pts if str(item[2].get("event") or "").lower() == "retry_loop"]
        if cycle_pts:
            for c_pt in cycle_pts:
                cx, cy = c_pt[0], c_pt[1]
                aa_items.append(("dot", cx, cy, 32, None, theme.ATTRACTOR_BASIN_BORDER, 2))
                aa_items.append(("dot", cx, cy, 20, None, theme.ERROR, 1))

    def _draw_phase_links(self, aa_items) -> None:
        """相轨迹连线：按相速度符号着色 + 推进箭头。"""
        for i in range(1, len(self._pts)):
            x0, y0, prev_pt = self._pts[i - 1][:3]
            x1, y1, cur_pt = self._pts[i][:3]
            v_cur = self._pts[i][4]
            col = theme.SUCCESS if v_cur < 0 else (theme.ERROR if v_cur > 0 else theme.WARNING)
            lw = 3 if abs(v_cur) > 0.01 else 2
            aa_items.append(("line", [(x0, y0), (x1, y1)], col, lw))
            poly = self._get_arrowhead_poly(x0, y0, x1, y1, length=10, half_width=5, setback=6)
            if poly:
                aa_items.append(("polygon", poly, col, col))

    def _draw_phase_halos(self, aa_items) -> None:
        """相平面质点：起终点/事件环 + 相速度基色核。"""
        n_pts = len(self._pts)
        for i_pt, item in enumerate(self._pts):
            x, y, pt = item[:3]
            v_pt = item[4]
            event_tag = str(pt.get("event") or "normal").lower()
            base_col = theme.SUCCESS if v_pt < 0 else (theme.ERROR if v_pt > 0 else theme.WARNING)
            if i_pt == 0:
                aa_items.append(("dot", x, y, 8, None, theme.PHASE_START_HALO, 1))
            elif i_pt == n_pts - 1:
                aa_items.append(("dot", x, y, 8, None, theme.SUCCESS if v_pt < 0 else theme.ERROR, 1))
            if event_tag == "retry_loop":
                aa_items.append(("dot", x, y, 10, None, theme.ERROR, 1))
            elif event_tag == "breakthrough":
                aa_items.append(("dot", x, y, 9, None, theme.SUCCESS, 1))
            aa_items.append(("dot", x, y, 5, base_col, theme.FG_WHITE, 2))

    def _draw_phase_legend(self, c, plot_w, plot_h, pad_l, pad_t) -> None:
        """上层图例（狄拉克/吸引子标签 + 顶部图例条）与底部 X 轴底座说明。"""
        tgt_x, tgt_y = self._tgt_pos
        att_x, att_y = self._att_pos
        c.create_text(tgt_x + 22, tgt_y, text="目标点",
                      fill=theme.SUCCESS, font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        c.create_text(att_x, att_y - 36, text="惯性套路区",
                      fill=theme.ERROR, font=theme.FONT_UI_SMALL_BOLD, anchor="center")

        leg_w = 460
        leg_x = max(pad_l + 8, pad_l + (plot_w - leg_w) / 2)
        leg_y = pad_t - 16
        c.create_line(leg_x, leg_y, leg_x + 14, leg_y, fill=theme.SUCCESS, width=2)
        c.create_text(leg_x + 18, leg_y, text="向心收敛", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_line(leg_x + 105, leg_y, leg_x + 119, leg_y, fill=theme.ERROR, width=2)
        c.create_text(leg_x + 123, leg_y, text="离心发散", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_oval(leg_x + 210, leg_y - 4, leg_x + 218, leg_y + 4, outline=theme.SUCCESS, fill=theme.DIRAC_CORE_BG, width=1)
        c.create_text(leg_x + 222, leg_y, text="目标点", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_oval(leg_x + 340, leg_y - 4, leg_x + 348, leg_y + 4, outline=theme.ERROR, fill=theme.ATTRACTOR_RINGS[0], width=1)
        c.create_text(leg_x + 352, leg_y, text="惯性套路区", fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")

        c.create_text(pad_l, pad_t + plot_h + 12, text="0.0 (契合真实意图)",
                      fill=theme.SUCCESS, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w / 2, pad_t + plot_h + 12,
                      text="语义距离：向左逼近目标达成 · 向右偏离真实意图",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="center")
        c.create_text(pad_l + plot_w, pad_t + plot_h + 12, text="1.0 (严重偏离意图)",
                      fill=theme.ERROR, font=theme.FONT_UI_SMALL, anchor="e")

    def _draw_phase_labels(self, c, pad_t) -> None:
        """逐点回合号/事件标签（2D 欧氏防叠）与卫星名标注。"""
        n_pts_p = len(self._pts)
        drawn_boxes_p: list[tuple[float, float]] = []
        for i, item in enumerate(self._pts):
            x, y, pt = item[:3]
            offset_y = item[3] if len(item) > 3 else (-14 if (i % 2 == 0 and y > pad_t + 28) else 14)
            t_val = pt.get("turn")
            u_val = pt.get("user_turn") or pt.get("u")
            prev_u = (self._pts[i - 1][2].get("user_turn") or self._pts[i - 1][2].get("u")) if i > 0 else None
            event_tag = str(pt.get("event") or "normal").lower()
            is_milestone = (i == 0 or i == n_pts_p - 1 or event_tag in ("breakthrough", "compaction"))
            cand_x = x
            cand_y = y + offset_y
            is_colliding = any((cand_x - lx)**2 + (cand_y - ly)**2 < 38**2 for lx, ly in drawn_boxes_p)
            can_draw = is_milestone or not is_colliding

            if can_draw:
                drawn_boxes_p.append((cand_x, cand_y))
                t_str = f"T{t_val}·U{u_val}" if (t_val is not None and u_val is not None) else (f"T{t_val}" if t_val is not None else "")
                c.create_text(cand_x, cand_y, text=t_str, fill=theme.FG_WHITE,
                              font=theme.FONT_UI_SMALL_BOLD)
                if event_tag in ("retry_loop", "breakthrough", "compaction", "test_fail"):
                    evt_labels = {
                        "retry_loop": "极限环死锁",
                        "breakthrough": "向心冲刺",
                        "compaction": "压缩",
                        "test_fail": "报错",
                    }
                    lbl_text = evt_labels.get(event_tag, "")
                    evt_col = theme.SUCCESS if event_tag == "breakthrough" else theme.ERROR
                    c.create_text(cand_x, cand_y + (10 if offset_y > 0 else -10),
                                  text=lbl_text, fill=evt_col,
                                  font=theme.FONT_UI_SMALL)
        # 卫星标签标注（密集簇只画微质点与引力细线）
        self._draw_satellite_labels(c, 18, 32)

class _LlmTreeShim:
    """向后兼容代理：将原有对 Treeview 接口的调用无缝映射至 Card 列表与报告数据。"""

    def __init__(self, view) -> None:
        self._view = view

    def get_children(self) -> tuple[str, ...]:
        return tuple(r["id"] for r in self._view._visible_reports if "id" in r)

    def exists(self, item_id: str) -> bool:
        return item_id in self._view._report_to_card

    def item(self, item_id: str, option: str | None = None, **kwargs):
        r = next((x for x in self._view._reports if x.get("id") == item_id), None)
        if not r:
            return () if option == "values" else {}
        kind = self._view._resolve_kind(r)
        if kind == "dynamics":
            _k, kind_lbl, _c = self._view._resolve_dynamics_status(r)
        else:
            kind_lbl = self._view.REPORT_KINDS.get(kind, {}).get("label", "报告")
        vals = (kind_lbl, self._view._resolve_title(r), self._view._fmt_time(r.get("created_at")))
        if option == "values":
            return vals
        return {"values": vals, "tags": ()}

    def selection(self) -> tuple[str, ...]:
        return (self._view._selected_id,) if self._view._selected_id else ()

    def selection_set(self, item_id: str) -> None:
        self._view.select_report(item_id)

    def see(self, item_id: str) -> None:
        pass

    def delete(self, *items: str) -> None:
        pass

    def cget(self, option: str):
        if option == "columns":
            return ("kind", "title", "time")
        return None

    def column(self, col: str, option: str | None = None, **kw):
        if option == "width":
            return 90 if col == "time" else (56 if col == "kind" else 170)
        if option == "anchor":
            return "center" if col in ("time", "kind") else "w"
        return {"width": 90, "anchor": "center"}

    def heading(self, col: str, **kw):
        pass

    def tag_configure(self, tag: str, **kw):
        pass

    def bind(self, sequence=None, func=None, add=None):
        pass

    @property
    def _w(self):
        try:
            return self._view._scroll.canvas._w
        except Exception:
            return ""

    @property
    def tk(self):
        try:
            return self._view._scroll.canvas.tk
        except Exception:
            return None

    def winfo_toplevel(self):
        try:
            return self._view._scroll.canvas.winfo_toplevel()
        except Exception:
            return None


class LlmReportsView:
    """「LLM 报告」页签 — 会话/项目/多源解读的持久化回看（左列表 + 右全高阅读区）。

    支持多来源扩展（会话收敛、相空间动力学、项目全局、模型对比等）；左侧带类型筛选与实时搜索；
    右侧为结构化卡片头（标题/来源/模型/档位/指标徽标 + 一键复制全文）与
    原生平滑滚动的排版阅读器（段落行距、悬挂缩进、层级标题与 Markdown 标签）。
    """

    _BODY_FONT = (theme.FONT_CJK, 10)

    REPORT_KINDS = {
        "session":  {"label": "会话", "color": theme.CHART_PALETTE[2], "desc": "会话过程收敛解读", "icon": "session"},
        "dynamics": {"label": "相空间", "color": theme.CHART_PALETTE[4], "desc": "相空间收敛动力学分析", "icon": "layers"},
        "crosscheck": {"label": "对账", "color": theme.CHART_PALETTE[1], "desc": "纠正信号双引擎交叉验证（正则 vs Jev）", "icon": "crosscheck"},
        "terms":    {"label": "术语", "color": theme.ACCENT, "desc": "从会话历史挖掘团队术语", "icon": "book"},
        "ambiguity": {"label": "歧义", "color": theme.WARNING, "desc": "需求文本术语歧义探测", "icon": "target"},
        "project":  {"label": "项目", "color": theme.CHART_PALETTE[0], "desc": "项目全局架构解读", "icon": "project"},
        "compare":  {"label": "对比", "color": theme.CHART_PALETTE[3], "desc": "多模型/跨源对比解读", "icon": "compare"},
        "anomaly":  {"label": "诊断", "color": theme.WARNING, "desc": "异常卡死/返工诊断", "icon": "tools"},
        "general":  {"label": "通用", "color": theme.MUTED, "desc": "综合解读报告", "icon": "sparkle"},
    }

    def __init__(self, parent, controller=None, on_cancel_tasks=None) -> None:
        from .widgets import flat_button, FlatMenu, Card, ScrollFrame
        self.controller = controller
        self._cancel_tasks_btn = None  # 未注入 on_cancel_tasks 回调时完全不显示取消按钮
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
        self._sash_target = 250  # 适度舒展的报告索引列，兼顾卡片标题完整度与右侧阅读沉浸感

        # 左：报告列表（类型 / 解读对象 / 模型 / 时间）— 保持紧凑，把黄金空间留给阅读区
        left = tk.Frame(paned, bg=theme.BG)
        paned.add(left, minsize=180, width=250)
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
        self._count_lbl.pack(side="left", padx=(4, 2))
        # Jev 引擎累计账单（方案 F）：opt-in 付费必须可见——只记真实网络调用，
        # 缓存命中不计；按官方输入单价折算（输出免费）
        self._usage_lbl = tk.Label(bar, text="", bg=theme.BG, fg=theme.MUTED,
                                   font=theme.FONT_UI_SMALL)
        Tooltip(self._usage_lbl,
                "TypeSafe Jev 引擎累计计费口径：仅真实网络调用（判定缓存命中不计），"
                "按官方输入单价 $0.042/百万 token 折算，输出 token 免费")

        # 右侧操作图标组：清空与取消任务（纯 Codicon 图标化，沉浸底色，悬停高亮，永不截断）
        _trash_icon = ui_icon(bar, "trash")
        clear_btn = flat_button(bar, "清空", self._clear_all,
                                image=_trash_icon, bg=theme.BG,
                                padx=2, pady=1)
        clear_btn.pack(side="right", padx=(2, 0))
        Tooltip(clear_btn, "清空所有本地 LLM 报告记录")

        if on_cancel_tasks is not None:
            _stop_icon = ui_icon(bar, "stop")
            self._cancel_tasks_btn = flat_button(bar, "取消生成中任务", on_cancel_tasks,
                                                 image=_stop_icon, bg=theme.BG,
                                                 padx=2, pady=1)
            self._cancel_tasks_btn.pack(side="right", padx=(2, 0))
            Tooltip(self._cancel_tasks_btn, "取消生成中任务（终止所有进行中与排队中的后台分析任务）")
        # 搜索与类型过滤工具条（零多余空白死区，与侧栏自然紧凑贴合）
        self._filter_box = filter_box = tk.Frame(left, bg=theme.BG)
        filter_box.pack(fill="x", pady=(0, 2))

        # 类型切换胶囊（流式自适应紧凑排布，零多余空白死区）
        self._pill_frame = pill_box = tk.Frame(filter_box, bg=theme.BG)
        pill_box.pack(fill="x", pady=(0, 2))
        self._pill_btns: dict[str, RoundedPill] = {}

        pills = [
            ("all", "全部", 44),
            ("session", "会话", 44),
            ("dynamics", "相空间", 54),
            ("crosscheck", "对账", 44),
            ("terms", "术语", 44),
            ("ambiguity", "歧义", 44),
            ("project", "项目", 44),
            ("compare", "对比", 44),
            ("other", "其他", 42),
        ]
        for k, lbl, w in pills:
            btn = RoundedPill(pill_box, text=lbl, width=w, height=22, radius=5,
                              fill=theme.CONTROL_BG, hover_fill=theme.HOVER_BG,
                              bg=theme.BG, fg=theme.FG, font=theme.FONT_UI_SMALL,
                              command=lambda _p=None, kind=k: self._set_kind_filter(kind))
            btn.pack(side="left", padx=(0, 2))
            self._pill_btns[k] = btn

        # 实时搜索框：与会话列表 (SessionColumn) 100% 一致的 RoundedSearchBox（抗锯齿圆角槽 + 内嵌放大镜）
        self._search_var = tk.StringVar(value="")
        from .widgets import RoundedSearchBox
        self._search_box = RoundedSearchBox(
            filter_box, textvariable=self._search_var,
            icon=ui_icon(filter_box, "search"),
            width=240, height=22, radius=5,
            fill=theme.CONTROL_BG, bg=theme.BG, fg=theme.FG)
        self._search_box.pack(fill="x", pady=(2, 4))
        self.search_entry = self._search_entry = self._search_box.entry
        self._search_var.trace_add("write", lambda *_a: self._on_search_changed())
        Tooltip(self._search_box, "按标题 / 解读对象 / 模型 / 内容 实时过滤 · Esc 清除")
        Tooltip(self._search_entry, "按标题 / 解读对象 / 模型 / 内容 实时过滤 · Esc 清除")

        # 卡片式报告列表容器（使用 ScrollFrame + Card，对齐 SessionColumn）
        self._tree_container = tree_container = tk.Frame(left, bg=theme.PANEL)
        tree_container.pack(fill="both", expand=True)
        self._scroll = ScrollFrame(tree_container, bg=theme.PANEL)
        self._scroll.canvas.pack(fill="both", expand=True, padx=2, pady=(1, 2))
        self.container = self._scroll.inner
        self._cards: list[Card] = []
        self._card_to_report: dict[Card, dict] = {}
        self._report_to_card: dict[str, Card] = {}
        self._visible_reports: list[dict] = []
        self._selected_card: Card | None = None
        self._tree = _LlmTreeShim(self)

        # 键盘上下键切换卡片
        self._scroll.canvas.bind("<Up>", lambda _e: self._on_nav_key(-1))
        self._scroll.canvas.bind("<Down>", lambda _e: self._on_nav_key(1))

        # 右：全高阅读区 — 作为报告展示主核心
        right = tk.Frame(paned, bg=theme.PANEL)
        paned.add(right, minsize=380)

        # 右顶部：结构化元数据 Hero 卡片（明度阶分区，无边框）
        self._header_card = tk.Frame(
            right, bg=theme.PANEL_2, relief="flat")
        self._header_card.pack(fill="x", padx=10, pady=(4, 6))

        # 卡片第一行：标题 + 操作按钮
        top_row = tk.Frame(self._header_card, bg=theme.PANEL_2)
        top_row.pack(fill="x", padx=10, pady=(8, 4))
        self._title_lbl = tk.Label(
            top_row, text="", bg=theme.PANEL_2, fg=theme.FG_WHITE,
            font=(theme.FONT_CJK, 12, "bold"), anchor="w", justify="left")
        self._title_lbl.pack(side="left", fill="x", expand=True)

        # 右侧操作区：复制与删除按钮（Codicon 图标化，沉浸于 PANEL_2，悬浮高亮）
        _copy_icon = ui_icon(top_row, "copy")
        self._copy_btn = flat_button(
            top_row, "复制全文", self._copy_markdown,
            image=_copy_icon, bg=theme.PANEL_2,
            padx=4, pady=2)
        self._copy_btn.pack(side="right", padx=(3, 0))
        self._copy_tip = Tooltip(self._copy_btn, "复制全文 Markdown（含遥测 JSON）")

        _del_icon = ui_icon(top_row, "trash")
        self._del_btn = flat_button(
            top_row, "删除", self._delete_selected,
            image=_del_icon, bg=theme.PANEL_2,
            padx=4, pady=2)
        self._del_btn.pack(side="right", padx=(3, 0))
        Tooltip(self._del_btn, "删除本条报告")

        # 卡片第二行：徽标与关键指标
        self._badge_row = tk.Frame(self._header_card, bg=theme.PANEL_2)
        self._badge_row.pack(fill="x", padx=10, pady=(0, 8))

        self._kind_badge = RoundedPill(
            self._badge_row, text="", width=95, height=22, radius=4,
            fill=theme.PANEL, hover_fill=None, bg=theme.PANEL_2, fg=theme.ACCENT,
            command=None)
        self._kind_badge.pack(side="left", padx=(0, 6))

        # 元数据项：回归纯净排版流（去框化设计，杜绝截断与 Badge 视觉过载）
        self._source_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._source_badge.pack(side="left", padx=(0, 4))

        self._model_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.FG,
            font=theme.FONT_UI_SMALL)
        self._model_badge.pack(side="left", padx=(0, 4))
        self._model_tip = Tooltip(self._model_badge, "当前报告所用 LLM 模型")

        self._scope_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._scope_badge.pack(side="left", padx=(0, 4))

        self._metrics_lbl = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._metrics_lbl.pack(side="left", padx=4)

        # 审计校验徽标：机械检查 LLM 输出是否守住审计契约（缺小节/笼统表扬/
        # 转折锚点不足）。旧报告无该字段时隐藏；警示列于 Tooltip。
        self._audit_badge = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL, padx=6, pady=1)
        self._audit_tip = Tooltip(
            self._audit_badge,
            "对报告正文的机械审计校验（本地规则，非二次 LLM 调用）：\n"
            "· 缺少必备小节 / 含笼统表扬措辞 / 转折锚点不足时亮警示，\n"
            "  说明模型未完全遵守审计立场——相关结论请折扣采信。")
        self._time_lbl = tk.Label(
            self._badge_row, text="", bg=theme.PANEL_2, fg=theme.MUTED,
            font=theme.FONT_UI_SMALL)
        self._time_lbl.pack(side="right", padx=(4, 0))

        # 兼容旧代码引用的 _meta_lbl 属性（指向标题文本或空桩）
        self._meta_lbl = self._title_lbl

        # 专属相空间动力学相图折叠面板（对齐指标看板折叠风格，仅在选中 dynamics 报告时挂载显示）
        self._phase_container = tk.Frame(right, bg=theme.PANEL)
        self._phase_collapsed: bool = False

        # 分组折叠标题栏（样式与交互完全对齐 MetricPanelView 指标看板分组）
        self._phase_header = tk.Frame(self._phase_container, bg=theme.SECTION_HEADER_BG,
                                      padx=10, pady=4, cursor=CLICK_CURSOR)
        self._phase_header.pack(fill="x", pady=(0, 2))

        self._phase_arrow = tk.Label(
            self._phase_header, text="▾ 相空间收敛动力学相图",
            font=theme.FONT_UI_BOLD, fg=theme.FG,
            bg=theme.SECTION_HEADER_BG, cursor=CLICK_CURSOR)
        self._phase_arrow.pack(side="left")

        self._phase_tip_lbl = tk.Label(
            self._phase_header, text="（点击可折叠图表，专注通读下方正文报告）",
            font=theme.FONT_UI_SMALL, fg=theme.MUTED,
            bg=theme.SECTION_HEADER_BG, cursor=CLICK_CURSOR)
        self._phase_tip_lbl.pack(side="left", padx=(8, 0))

        self._phase_summary_lbl = tk.Label(
            self._phase_header, text="",
            font=theme.FONT_UI_SMALL_BOLD, fg=theme.MUTED,
            bg=theme.SECTION_HEADER_BG, cursor=CLICK_CURSOR)
        self._phase_summary_lbl.pack(side="right", padx=(0, 6))

        for _w in (self._phase_header, self._phase_arrow, self._phase_tip_lbl, self._phase_summary_lbl):
            _w.bind("<Button-1>", lambda _e: self._toggle_phase_portrait())

        # 相图主体容器（用于折叠/展开）
        self._phase_body = tk.Frame(self._phase_container, bg=theme.PANEL)
        self._phase_body.pack(fill="x")

        self._phase_portrait = PhasePortraitWidget(self._phase_body)

        # 正文阅读器（原生平滑滚动 Text + 自定义排版标签）
        self._text_frame = text_frame = tk.Frame(right, bg=theme.PANEL)
        text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        text_sb = ttk.Scrollbar(text_frame, orient="vertical")
        self._body_lbl = tk.Text(
            text_frame, wrap="word", bg=theme.PANEL, fg=theme.FG_WHITE,
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
            "md_mono", font=theme.FONT_MONO,
            foreground=theme.CODE_MINT)
        self._body_lbl.tag_configure(
            "code_block", font=theme.FONT_MONO, background=theme.BG,
            foreground=theme.FG_WHITE, lmargin1=16, lmargin2=16,
            spacing1=4, spacing2=2, spacing3=4)
        self._body_lbl.tag_configure(
            "tbl_border", font=theme.FONT_MONO, foreground=theme.BORDER)
        self._body_lbl.tag_configure(
            "tbl_header", font=theme.FONT_MONO, foreground=theme.FG_WHITE)
        self._body_lbl.tag_configure(
            "tbl_cell", font=theme.FONT_MONO, foreground=theme.FG)
        self._body_lbl.tag_configure(
            "tbl_highlight", font=theme.FONT_MONO, foreground=theme.WARNING)
        self._body_lbl.tag_configure(
            "tbl_alert", font=theme.FONT_MONO, foreground=theme.ERROR)
        self._body_lbl.tag_configure(
            "tbl_good", font=theme.FONT_MONO, foreground=theme.SUCCESS)
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
    def _resolve_dynamics_status(cls, r: dict) -> tuple[str, str, str]:
        """解析相空间动力学报告的态势结论 (status_key, label, color)。

        终态判定走共享 SSOT _dynamics_terminal_state（与相图态势徽章同源）。
        """
        dyn = r.get("dynamics_data") or {}
        if not dyn:
            return "dynamics", "相空间", theme.CHART_PALETTE[4]
        status_map = {
            "dirac": ("dirac", "收敛", theme.SUCCESS),
            "escaped": ("escaped", "逃逸", theme.SUCCESS),
            "trapped": ("trapped", "捕获", theme.ERROR),
            "wandering": ("wandering", "漫游", theme.WARNING),
        }
        return status_map[_dynamics_terminal_state(dyn)]
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
        self._update_usage_lbl()
        self._refresh_list()

    def _update_usage_lbl(self) -> None:
        """刷新 Jev 引擎累计账单行（零请求时隐藏）。"""
        try:
            from tcer.core import typesafe_client
            stats = typesafe_client.usage_stats()
            cost = typesafe_client.usage_cost_usd(stats)
        except Exception:
            if hasattr(self, "_usage_lbl"):
                self._usage_lbl.pack_forget()
            return
        if hasattr(self, "_usage_lbl"):
            self._usage_lbl.pack_forget()
        if not stats or not stats.get("requests"):
            return
        toks = stats.get("input_tokens") or 0
        tok_txt = f"{toks / 1_000_000:.2f}M" if toks >= 1_000_000 else f"{toks / 1000:.1f}k"
        tip_text = (
            f"LLM 报告收信箱\n"
            f"TypeSafe Jev 引擎累计调用: {stats['requests']} 次\n"
            f"输入 Token: {tok_txt} · 预估计费: ≈${cost:.3f}\n"
            "（按官方输入单价 $0.042/百万 token 折算，输出 token 免费）"
        )
        if hasattr(self, "_count_lbl"):
            Tooltip(self._count_lbl, tip_text)

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
                if kind in ("session", "dynamics", "crosscheck", "project", "compare"):
                    return False
            elif kind != self._kind_filter:
                return False
        if self._search_keyword:
            kw = self._search_keyword
            title = self._resolve_title(r).lower()
            model = str(r.get("model") or "").lower()
            text = str(r.get("text") or "").lower()
            dyn_status = self._resolve_dynamics_status(r)[1] if kind == "dynamics" else ""
            if kw not in title and kw not in model and kw not in text and kw not in dyn_status:
                return False
        return True

    def _make_card(self, r: dict):
        from .widgets import Card
        rid = r.get("id")
        kind = self._resolve_kind(r)
        title = self._resolve_title(r)
        time_str = self._fmt_time(r.get("created_at"))

        card = Card(
            self.container,
            on_click=lambda c, _id=rid: self.select_report(_id),
            on_right_click=lambda e, _id=rid: self._on_card_context_menu(e, _id),
            bg=theme.PANEL, padx=1, pady=1
        )

        # State Rail 着色
        rail_col = None
        if kind == "dynamics":
            status_key, _, col = self._resolve_dynamics_status(r)
            rail_col = col
        elif kind == "crosscheck":
            xdata = r.get("crosscheck_data") or {}
            cnts = xdata.get("counts") or {}
            if cnts.get("agree_correction"):
                rail_col = theme.ERROR
            elif cnts.get("jev_only") or cnts.get("jev_uncertain"):
                rail_col = theme.WARNING
            else:
                rail_col = theme.SUCCESS
        elif kind == "terms":
            rail_col = theme.ACCENT
        elif kind == "ambiguity":
            amb = r.get("ambiguity_data") or {}
            if amb.get("flagged_terms"):
                rail_col = theme.ERROR
            else:
                rail_col = theme.SUCCESS
        elif kind == "session":
            rail_col = theme.CHART_PALETTE[2]
        elif kind == "project":
            rail_col = theme.CHART_PALETTE[0]
        elif kind == "compare":
            rail_col = theme.CHART_PALETTE[3]
        card.set_state_rail(rail_col)

        # Row 1: 图标 + 类型徽章 + 时间（左），右侧为态势/状态胶囊
        row1 = tk.Frame(card.frame, bg=card._bg)
        row1.pack(fill="x", padx=6, pady=(3, 1))
        card.track_bg(row1)

        kind_meta = self.REPORT_KINDS.get(kind, self.REPORT_KINDS["general"])
        icon_name = kind_meta.get("icon", "session")
        _ico = ui_icon(row1, icon_name)
        if _ico is not None:
            ico_lbl = tk.Label(row1, image=_ico, bg=card._bg)
            ico_lbl.pack(side="left", padx=(0, 4))
            card.track_bg(ico_lbl)
            card.bind_to(ico_lbl)

        k_lbl = tk.Label(row1, text=kind_meta["label"], bg=card._bg, fg=kind_meta["color"],
                         font=theme.FONT_UI_SMALL_BOLD)
        k_lbl.pack(side="left", padx=(0, 4))
        card.track_bg(k_lbl)
        card.bind_to(k_lbl)

        t_lbl = tk.Label(row1, text=time_str, bg=card._bg, fg=theme.MUTED,
                         font=theme.FONT_MONO)
        t_lbl.pack(side="left", padx=(0, 4))
        card.track_bg(t_lbl)
        card.bind_to(t_lbl)

        # 右侧态势/结论胶囊
        status_text = ""
        status_fg = theme.MUTED
        if kind == "dynamics":
            _, s_lbl, s_col = self._resolve_dynamics_status(r)
            status_text = f"● {s_lbl}"
            status_fg = s_col
        elif kind == "crosscheck":
            xdata = r.get("crosscheck_data") or {}
            cnts = xdata.get("counts") or {}
            uncertain = cnts.get("jev_uncertain", 0)
            jev_only = cnts.get("jev_only", 0)
            if jev_only > 0:
                status_text = f"漏报 {jev_only}"
                status_fg = theme.WARNING
            elif uncertain > 0:
                status_text = f"灰色 {uncertain}"
                status_fg = theme.WARNING
            elif cnts.get("agree_correction", 0) > 0:
                status_text = f"纠正 {cnts['agree_correction']}"
                status_fg = theme.ERROR
            else:
                status_text = "双引擎一致"
                status_fg = theme.SUCCESS
        elif r.get("audit_warnings") == []:
            status_text = "✓ 守约"
            status_fg = theme.SUCCESS

        if status_text:
            st_lbl = tk.Label(row1, text=status_text, bg=card._bg, fg=status_fg,
                              font=theme.FONT_UI_SMALL, anchor="e")
            st_lbl.pack(side="right")
            card.track_bg(st_lbl)
            card.bind_to(st_lbl)

        # Row 2: 解读对象与标题（紧凑卡片展示，剥除长路径前缀）
        row2 = tk.Frame(card.frame, bg=card._bg)
        row2.pack(fill="x", padx=6, pady=(1, 2))
        card.track_bg(row2)
        ti_disp = format_card_title(title, 26)
        ti_lbl = tk.Label(row2, text=ti_disp, bg=card._bg, fg=theme.FG,
                          font=theme.FONT_UI_SMALL, anchor="w", justify="left")
        ti_lbl.pack(side="left", fill="x", expand=True)
        card.track_bg(ti_lbl)
        card.bind_to(ti_lbl)

        # Row 3: 底部摘要行（模型 · 回合数）
        row3 = tk.Frame(card.frame, bg=card._bg)
        row3.pack(fill="x", padx=6, pady=(1, 3))
        card.track_bg(row3)

        sum_parts = []
        if r.get("model"):
            m_short = str(r["model"]).split("/")[-1]
            if len(m_short) > 16:
                m_short = m_short[:15] + "…"
            sum_parts.append(m_short)
        if r.get("turns"):
            sum_parts.append(f"{r['turns']}轮")

        sum_txt = " · ".join(sum_parts) or "—"
        sum_lbl = tk.Label(row3, text=sum_txt, bg=card._bg, fg=theme.MUTED,
                           font=theme.FONT_UI_SMALL, anchor="w")
        sum_lbl.pack(side="left", fill="x", expand=True)
        card.track_bg(sum_lbl)
        card.bind_to(sum_lbl)

        return card

    def _on_card_context_menu(self, event, report_id: str) -> None:
        self.select_report(report_id)
        from .widgets import FlatMenu
        menu = FlatMenu(self.container)
        menu.add_command(label="复制全文 Markdown", command=self._copy_markdown)
        menu.add_command(label="删除本条报告", command=self._delete_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_nav_key(self, delta: int) -> None:
        if not self._cards:
            return
        curr_idx = -1
        if self._selected_card in self._cards:
            curr_idx = self._cards.index(self._selected_card)
        new_idx = max(0, min(len(self._cards) - 1, curr_idx + delta))
        if new_idx != curr_idx:
            r = self._card_to_report.get(self._cards[new_idx])
            if r and r.get("id"):
                self.select_report(r["id"])

    def _select_card(self, card) -> None:
        if not card or card not in self._card_to_report:
            return
        r = self._card_to_report[card]
        if self._selected_card and self._selected_card != card:
            try:
                self._selected_card.set_selected(False)
            except Exception:
                pass
        self._selected_card = card
        try:
            card.set_selected(True)
            self._scroll.see(card.frame)
        except Exception:
            pass
        self._selected_id = r.get("id")
        self._on_select()

    def _refresh_list(self) -> None:
        # 更新过滤胶囊样式与计数
        counts: dict[str, int] = {"all": len(self._reports), "session": 0,
                                  "dynamics": 0, "crosscheck": 0, "project": 0,
                                  "compare": 0, "other": 0}
        for r in self._reports:
            k = self._resolve_kind(r)
            if k in counts:
                counts[k] += 1
            else:
                counts["other"] += 1
        for k, btn in self._pill_btns.items():
            cnt = counts.get(k, 0)
            active = (self._kind_filter == k)
            if cnt == 0 and not active and k != "all":
                btn.pack_forget()
                continue
            else:
                btn.pack(side="left", padx=(0, 2))
            bg = theme.SEL_ROW_ACTIVE if active else theme.CONTROL_BG
            fg = theme.FG_WHITE if active else theme.MUTED
            label = {"all": "全部", "session": "会话", "dynamics": "相空间",
                     "crosscheck": "对账", "project": "项目", "compare": "对比",
                     "other": "其他"}.get(k, k)
            btn.config(text=f"{label} {cnt}", bg=bg, fg=fg)

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
        self._visible_reports = filtered

        # 清理旧卡片
        for c in self._cards:
            try:
                c.frame.destroy()
            except Exception:
                pass
        self._cards.clear()
        self._card_to_report.clear()
        self._report_to_card.clear()
        self._selected_card = None

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
            self._scroll.update_scroll(reset=True)
            return

        for r in filtered:
            card = self._make_card(r)
            self._cards.append(card)
            self._card_to_report[card] = r
            rid = r.get("id")
            if rid:
                self._report_to_card[rid] = card

        self._scroll.update_scroll(reset=False)
        self._count_lbl.config(text=f"{len(filtered)} / {len(self._reports)} 条")

        # 保持或设定选中项
        if sel and sel in self._report_to_card:
            self.select_report(sel)
        elif filtered and filtered[0].get("id") in self._report_to_card:
            self.select_report(filtered[0]["id"])

    def _clear_header(self) -> None:
        self._title_lbl.config(text="无选中的报告")
        self._kind_badge.config(text="")
        self._source_badge.config(text="")
        self._model_badge.config(text="")
        self._model_badge.pack_forget()
        self._scope_badge.config(text="")
        self._metrics_lbl.config(text="")
        self._time_lbl.config(text="")
        self._audit_badge.config(text="")

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = (col in ("time",))
        self._update_headings()
        self._refresh_list()

    def _update_headings(self) -> None:
        pass

    def _on_tree_enter(self, _event=None) -> None:
        pass

    def _on_tree_leave(self, _event=None) -> None:
        pass

    def select_report(self, report_id: str) -> None:
        """报告生成保存后由 controller 调用：选中并展示。"""
        if not self._reports:
            self.on_show()
        if not report_id:
            return
        card = self._report_to_card.get(report_id)
        if card:
            self._select_card(card)
        else:
            r = next((x for x in self._reports if x.get("id") == report_id), None)
            if r:
                self._selected_id = report_id
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
        if kind == "dynamics":
            _status_key, s_lbl, s_col = self._resolve_dynamics_status(r)
            self._kind_badge.config(text=f"相空间 · {s_lbl}", fg=s_col)
        else:
            self._kind_badge.config(
                text=f"{kind_meta['label']}解读", fg=kind_meta["color"])
        src_val = r.get('source') or 'claude'
        self._source_badge.config(text=f"源: {src_val} ·")
        # 模型信息：旧报告无 model 字段时整体隐藏（不显示占位），长名称自然延展永不截断
        model_name = r.get("model")
        if model_name:
            self._model_badge.config(text=f"模型: {model_name} ·")
            self._model_tip.text = f"LLM 模型: {model_name}"
            self._model_badge.pack(side="left", padx=(0, 4),
                                   before=self._scope_badge)
        else:
            self._model_badge.config(text="")
            self._model_badge.pack_forget()
        # 「范围」= 授权数据范围摘要（勿叫「档位」——供给档 standard/rich/full 是另一维度）
        scope_val = r.get('scope') or '—'
        self._scope_badge.config(text=f"范围: {scope_val} ·")
        warns = r.get("audit_warnings")
        semantic = r.get("audit_semantic")
        sem_warns = (semantic or {}).get("warnings") if isinstance(semantic, dict) else None
        if isinstance(warns, list) and warns:
            local_total = len(warns)
        elif isinstance(warns, list):
            local_total = 0
        else:
            local_total = None
        sem_total = len(sem_warns) if isinstance(sem_warns, list) else None
        # 合并徽标：本地正则 + Jev 语义审计（方案 A）两路计数；任一来源缺失
        # （旧报告 / 未配置 typesafe key）按已有来源显示，不虚报
        if local_total or sem_total:
            parts = [n for n in (local_total, sem_total) if n]
            self._audit_badge.config(
                text=f"审计校验: {sum(parts)} 项警示", fg=theme.WARNING)
            tip_lines = []
            if local_total:
                tip_lines.append("【本地规则】")
                tip_lines.extend(f"· {w}" for w in warns)
            if sem_total:
                tip_lines.append("【语义判定 · Jev】")
                tip_lines.extend(f"· {w}" for w in sem_warns)
            self._audit_tip.text = "\n".join(tip_lines)
        elif local_total == 0:
            sem_note = ""
            if isinstance(semantic, dict) and semantic.get("probs_display"):
                # 语义审计通过：附概率明细供深度采信
                detail = " · ".join(semantic["probs_display"])
                sem_note = f"\n【语义判定 · Jev】全部通过。{detail}"
            self._audit_badge.config(text="审计校验: 通过", fg=theme.SUCCESS)
            self._audit_tip.text = (
                "本地机械规则全部通过：小节齐全、无笼统表扬、转折锚点达标。" + sem_note)
        else:
            self._audit_badge.config(text="", fg=theme.MUTED)
            self._audit_tip.text = ""

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

        # 动力学相图视口联动（仅 dynamics 类型显示，支持类似指标看板的一键折叠）
        if kind == "dynamics":
            try:
                self._phase_portrait.render(r.get("dynamics_data") or {}, r)
                st_key, st_txt, st_col = self._resolve_dynamics_status(r)
                ddata = r.get("dynamics_data") or {}
                par = ddata.get("parameters") or {}
                zeta = par.get("damping_ratio")
                zeta_s = f" · ζ={zeta:.2f}" if zeta is not None else ""
                self._phase_summary_lbl.config(text=f"● {st_txt}{zeta_s}", fg=st_col)
            except Exception:
                # 兜底：相图渲染异常时降级隐藏，保证右侧正文阅读区不受牵连
                self._phase_container.pack_forget()
                self._phase_portrait.pack_forget()
            else:
                self._phase_container.pack(fill="x", padx=10, pady=(0, 6), before=self._text_frame)
                if self._phase_collapsed:
                    self._phase_portrait.pack_forget()
                    self._phase_body.pack_forget()
                    self._phase_arrow.config(text="▸ 相空间收敛动力学相图")
                    self._phase_tip_lbl.config(text="（图表已折叠 · 点击展开相图）")
                else:
                    self._phase_body.pack(fill="x")
                    self._phase_portrait.pack(fill="x")
                    self._phase_arrow.config(text="▾ 相空间收敛动力学相图")
                    self._phase_tip_lbl.config(text="（点击可折叠图表，专注通读下方正文报告）")
        else:
            self._phase_container.pack_forget()
            self._phase_portrait.pack_forget()

        # 渲染正文
        self._fill_body(str(r.get("text") or ""))

    def _toggle_phase_portrait(self) -> None:
        """折叠/展开相空间动力学相图（对齐指标看板交互规范）。"""
        self._phase_collapsed = not self._phase_collapsed
        arrow_str = "▸" if self._phase_collapsed else "▾"
        self._phase_arrow.config(text=f"{arrow_str} 相空间收敛动力学相图")
        if self._phase_collapsed:
            self._phase_portrait.pack_forget()
            self._phase_body.pack_forget()
            self._phase_tip_lbl.config(text="（图表已折叠 · 点击展开相图）")
        else:
            self._phase_body.pack(fill="x")
            self._phase_portrait.pack(fill="x")
            self._phase_tip_lbl.config(text="（点击可折叠图表，专注通读下方正文报告）")
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
            if hasattr(self, "_copy_tip") and self._copy_tip is not None:
                self._copy_tip.text = "已复制 ✓"
            def _reset_copy():
                try:
                    self._copy_btn.config(text="复制全文")
                    if hasattr(self, "_copy_tip") and self._copy_tip is not None:
                        self._copy_tip.text = "复制全文 Markdown（含遥测 JSON）"
                except Exception:
                    pass
            self._copy_btn.after(1500, _reset_copy)
        except Exception:
            pass

    def select_session_report(self, sid: str) -> bool:
        """从主列表选中会话时自动联动展示专属报告。"""
        if not sid:
            return False
        for r in self._reports:
            r_sid = str(r.get("session_id") or r.get("id") or "")
            if r_sid == sid or sid in r_sid:
                self.select_report(r.get("id"))
                return True
        return False
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
            elif s_strip.startswith("|"):
                _flush()
                out.append(s_strip)
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
        # 清洗孤立列表分隔符伪影（如 • -- / - --- / --）
        if re.match(r"^[-*•·]\s*[-=—_]{2,}\s*$", s_strip) or re.match(r"^[-=—_]{2,}\s*$", s_strip):
            return

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

        # 表格行
        if s_strip.startswith("|") and s_strip.endswith("|"):
            # 分隔行，如 |---|---|...|
            if re.match(r"^\|[\s\-:|]+\|$", s_strip):
                tb.insert("end", "─" * 48 + "\n", ("divider",))
                return
            cells = [c.strip() for c in s_strip.strip("|").split("|")]
            formatted = " │ ".join(cells)
            tb.insert("end", "  " + formatted + "\n", ("code_block",))
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

    @staticmethod
    def _cjk_width(s: str) -> int:
        import unicodedata
        w = 0
        for ch in s:
            status = unicodedata.east_asian_width(ch)
            w += 2 if status in ('F', 'W') else 1
        return w

    @classmethod
    def _strip_md(cls, s: str) -> str:
        s = cls._MD_LINK.sub(r"\1", s)
        s = cls._MD_BOLD.sub(r"\1", s)
        s = cls._MD_ITALIC.sub(r"\1", s)
        s = cls._MD_CODE.sub(r"\1", s)
        return s.strip()

    def _render_table_block(self, tb, table_lines: list[str]) -> None:
        """结构化高精度等宽表格渲染引擎：全角/半角精密对齐、表头加粗、单元格语义高亮。"""
        import unicodedata
        rows: list[list[str]] = []
        for ln in table_lines:
            s = ln.strip()
            if not s.startswith("|") or not s.endswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            rows.append(cells)

        if len(rows) < 2:
            for ln in table_lines:
                tb.insert("end", ln + "\n", ("code_block",))
            return

        header = rows[0]
        sep = rows[1]
        data_rows = rows[2:]

        num_cols = len(header)
        col_widths = [self._cjk_width(self._strip_md(c)) for c in header]

        # 解析对齐方式
        alignments = []
        for c in sep:
            c_strip = c.strip()
            if c_strip.startswith(":") and c_strip.endswith(":"):
                alignments.append("center")
            elif c_strip.endswith(":"):
                alignments.append("right")
            else:
                alignments.append("left")
        while len(alignments) < num_cols:
            alignments.append("left")

        # 计算最大列宽（给单单元格设置合理的上限，例如 38 宽）
        max_limit = 38
        for r in data_rows:
            for i, c in enumerate(r):
                if i < num_cols:
                    cw = self._cjk_width(self._strip_md(c))
                    if cw > max_limit:
                        cw = max_limit
                    if cw > col_widths[i]:
                        col_widths[i] = cw

        padded_widths = [w + 2 for w in col_widths]

        def _pad(text: str, target_w: int, align: str = "left") -> str:
            curr_w = self._cjk_width(text)
            diff = max(0, target_w - curr_w)
            if align == "right":
                return " " * diff + text
            elif align == "center":
                left = diff // 2
                right = diff - left
                return " " * left + text + " " * right
            return text + " " * diff

        # 顶边框 ┌───┬───┐
        top_border = "  ┌" + "┬".join("─" * w for w in padded_widths) + "┐\n"
        tb.insert("end", top_border, ("tbl_border",))

        # 表头
        tb.insert("end", "  │", ("tbl_border",))
        for i, c in enumerate(header):
            w = padded_widths[i]
            disp = _pad(c, w - 2, "center")
            tb.insert("end", f" {disp} ", ("tbl_header",))
            tb.insert("end", "│", ("tbl_border",))
        tb.insert("end", "\n")

        # 分隔线 ├───┼───┤
        mid_border = "  ├" + "┼".join("─" * w for w in padded_widths) + "┤\n"
        tb.insert("end", mid_border, ("tbl_border",))

        # 数据行
        for r in data_rows:
            tb.insert("end", "  │", ("tbl_border",))
            for i in range(num_cols):
                raw_val = r[i] if i < len(r) else ""
                clean_val = self._strip_md(raw_val)
                # 超长内容截断
                if self._cjk_width(clean_val) > max_limit:
                    truncated = ""
                    tw = 0
                    for ch in clean_val:
                        ch_w = 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
                        if tw + ch_w > max_limit - 2:
                            break
                        truncated += ch
                        tw += ch_w
                    clean_val = truncated + "…"

                w = padded_widths[i]
                disp = _pad(clean_val, w - 2, alignments[i])

                # 智能语义着色
                cell_tag = "tbl_cell"
                if "**" in raw_val:
                    if any(k in raw_val for k in ("灰色地带", "仅正则", "不确定")):
                        cell_tag = "tbl_highlight"
                    elif any(k in raw_val for k in ("疑似漏报", "疑似误报", "错误", "死锁")):
                        cell_tag = "tbl_alert"
                    elif any(k in raw_val for k in ("双方·纠正", "确认纠正", "收敛", "通过", "命中")):
                        cell_tag = "tbl_good"
                    else:
                        cell_tag = "tbl_header"
                elif clean_val == "命中":
                    cell_tag = "tbl_good"

                tb.insert("end", f" {disp} ", (cell_tag,))
                tb.insert("end", "│", ("tbl_border",))
            tb.insert("end", "\n")

        # 底边框 └───┴───┘
        bot_border = "  └" + "┴".join("─" * w for w in padded_widths) + "┘\n\n"
        tb.insert("end", bot_border, ("tbl_border",))

    def _fill_body(self, text: str) -> None:
        tb = self._body_lbl
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        in_code_block = False
        text = self.clean_math_syntax(text)
        # 清理报告尾部供相空间相图消费的裸 JSON 遥测代码块（正文流无需倾倒整段 JSON，相图与复制全文已保留）
        text = re.sub(
            r"(?:##\s*(?:[一二三四五六七八九十\d]+[\.、\s]*)?动力学遥测数据[^\n]*\n+)?```json\s*\{[\s\S]*?\"trajectory\"[\s\S]*?\}\s*```",
            "> ▎ 动力学遥测数据已就绪（详见上方交互式相空间相图，点击右上角「复制全文」可导出原始 JSON 遥测）",
            text,
            flags=re.IGNORECASE
        )
        lines = self._reflow_lines(text)
        idx = 0
        n = len(lines)
        while idx < n:
            ln = lines[idx]
            s_strip = ln.strip()
            if s_strip.startswith("```"):
                in_code_block = not in_code_block
                idx += 1
                continue
            if not in_code_block and s_strip.startswith("|") and s_strip.endswith("|"):
                # 收集连续的表格行
                tbl = [ln]
                idx += 1
                while idx < n and lines[idx].strip().startswith("|") and lines[idx].strip().endswith("|"):
                    tbl.append(lines[idx])
                    idx += 1
                self._render_table_block(tb, tbl)
                continue
            if not s_strip and not in_code_block:
                tb.insert("end", "\n")
                idx += 1
                continue
            self._insert_line(tb, ln, in_code_block=in_code_block)
            idx += 1
        tb.configure(state="disabled")
    def _delete_selected(self) -> None:
        import tkinter.messagebox as mb
        from tcer.core import llm_reports
        sel = self._tree.selection()
        if not sel:
            return
        r = next((x for x in self._reports if x.get("id") == sel[0]), None)
        title = self._resolve_title(r or {})[:24]
        parent = getattr(self.controller, "root", None) or self.container.winfo_toplevel()
        if not mb.askyesno("删除报告",
                           f"确定删除「{title}…」这条 LLM 报告？（不可恢复）",
                           parent=parent):
            return
        self._selected_id = None
        llm_reports.delete(sel[0])
        self.on_show()

    def _clear_all(self) -> None:
        import tkinter.messagebox as mb
        from tcer.core import llm_reports
        if not self._reports:
            return
        parent = getattr(self.controller, "root", None) or self.container.winfo_toplevel()
        if mb.askyesno("清空 LLM 报告", f"确定删除全部 {len(self._reports)} 条报告？"
                       "（不可恢复）", parent=parent):
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


# 术语库表单的中文显示值 SSOT（key → 界面文案；保存时反查）。
# GUI 全中文红线：不向用户暴露 slug/status/mda_layer 等字段名与英文枚举。
_STATUS_CN = {"active": "活跃", "draft": "草稿", "deprecated": "已废弃"}
_MDA_CN = {"": "不限", "mechanics": "机制", "dynamics": "动态", "aesthetics": "体验"}
_STATUS_CN_KEY = {v: k for k, v in _STATUS_CN.items()}
_MDA_CN_KEY = {v: k for k, v in _MDA_CN.items()}


class TermbaseView:
    """第 7 页签：团队术语库（SharedBrain MVP · F1 核心工作台）。

    左侧：词条卡片列表（状态筛选胶囊 + 实时搜索 + 新建/删除/导入/导出/考古/检测工具条）。
    右侧：词条编辑表单（即时校验 + 显式保存 + 角色通道渲染 + 误解陷阱清单）。
    """

    def __init__(self, parent, controller=None) -> None:
        from .widgets import flat_button, FlatMenu, Card, ScrollFrame, RoundedSearchBox, RoundedPill
        from tcer.core.termbase import default_termbase_path, load_termbase

        self.container = parent
        self.controller = controller
        self._termbase_path = default_termbase_path()
        self._termbase = load_termbase(self._termbase_path)
        self._selected_slug: str | None = None
        self._status_filter = "all"
        self._search_kw = ""
        self._cards: list[Card] = []
        self._card_map: dict[str, Card] = {}
        self._misconception_rows: list[dict] = []
        self._rendering_entries: dict[str, tk.Entry] = {}
        self._baseline_snapshot: dict | None = None

        paned = tk.PanedWindow(parent, orient="horizontal", bg=theme.BG, sashwidth=3)
        paned.pack(fill="both", expand=True, padx=theme.PAD_S, pady=theme.PAD_S)
        self._paned_ref = paned

        left = tk.Frame(paned, bg=theme.BG)
        right = tk.Frame(paned, bg=theme.BG)
        paned.add(left, minsize=280, width=330)
        paned.add(right, minsize=500)
        self._left_frame = left
        self._right_frame = right

        # --- 左侧工具顶栏 ---
        bar = tk.Frame(left, bg=theme.BG)
        bar.pack(fill="x", pady=(0, 4))

        self._count_lbl = tk.Label(
            bar, text="共 0 个词条", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL
        )
        self._count_lbl.pack(side="left")

        btn_box = tk.Frame(bar, bg=theme.BG)
        btn_box.pack(side="right")

        _plus = ui_icon(btn_box, "plus")
        self._new_btn = flat_button(
            btn_box, "新建", self._create_new_term, primary=True, image=_plus, padx=4, pady=1
        )
        self._new_btn.pack(side="left", padx=(0, 2))
        Tooltip(self._new_btn, "新建空白词条草稿")

        _trash = ui_icon(btn_box, "trash")
        self._del_btn = flat_button(
            btn_box, "删除", self._delete_current_term, image=_trash, padx=4, pady=1
        )
        self._del_btn.pack(side="left", padx=(0, 2))
        Tooltip(self._del_btn, "删除当前选中的词条（不可恢复）")

        _more_btn = flat_button(btn_box, "操作 ▾", self._on_more_menu, padx=4, pady=1)
        _more_btn.pack(side="left")
        self._more_btn = _more_btn

        # 过滤与搜索条
        filter_box = tk.Frame(left, bg=theme.BG)
        filter_box.pack(fill="x", pady=(0, 4))

        pill_box = tk.Frame(filter_box, bg=theme.BG)
        pill_box.pack(fill="x", pady=(0, 2))
        self._pill_btns: dict[str, RoundedPill] = {}
        pills = (("all", "全部", 52), ("active", "活跃", 52), ("draft", "草稿", 52), ("deprecated", "已废弃", 60))
        for k, lbl, w in pills:
            btn = RoundedPill(
                pill_box,
                text=lbl,
                width=w,
                height=22,
                radius=5,
                fill=theme.CONTROL_BG,
                hover_fill=theme.HOVER_BG,
                bg=theme.BG,
                fg=theme.FG,
                font=theme.FONT_UI_SMALL,
                command=lambda _p=None, st=k: self._set_status_filter(st),
            )
            btn.pack(side="left", padx=(0, 2))
            self._pill_btns[k] = btn

        self._search_var = tk.StringVar(value="")
        self._search_box = RoundedSearchBox(
            filter_box,
            textvariable=self._search_var,
            icon=ui_icon(filter_box, "search"),
            width=240,
            height=22,
            radius=5,
            fill=theme.CONTROL_BG,
            bg=theme.BG,
            fg=theme.FG,
        )
        self._search_box.pack(fill="x", pady=(2, 2))
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())

        # 词条列表容器
        left_list_frame = tk.Frame(left, bg=theme.BG)
        left_list_frame.pack(fill="both", expand=True)
        self._scroll = ScrollFrame(left_list_frame, bg=theme.BG)
        self._list_container = self._scroll.inner

        # --- 右侧编辑区 ---
        right_edit_frame = tk.Frame(right, bg=theme.BG)
        right_edit_frame.pack(fill="both", expand=True)
        self._edit_scroll = ScrollFrame(right_edit_frame, bg=theme.BG)
        self._edit_container = self._edit_scroll.inner

        self.on_show()

    def on_show(self) -> None:
        """页签切入或外部变动时重载术语库数据。"""
        from tcer.core.termbase import default_termbase_path, load_termbase

        self._termbase_path = default_termbase_path()
        self._termbase = load_termbase(self._termbase_path)
        if not self._selected_slug and self._termbase.terms:
            self._selected_slug = self._termbase.terms[0].slug
        elif self._selected_slug and not any(t.slug == self._selected_slug for t in self._termbase.terms):
            self._selected_slug = self._termbase.terms[0].slug if self._termbase.terms else None

        self._refresh_list()
        self._render_editor()

    def _set_status_filter(self, st: str) -> None:
        if self._status_filter == st:
            return
        if not self._check_unsaved_and_confirm():
            return
        self._status_filter = st
        self._refresh_list()

    def _on_search_changed(self) -> None:
        self._search_kw = self._search_var.get().strip().lower()
        self._refresh_list()

    def _refresh_list(self) -> None:
        from .widgets import Card

        for w in self._list_container.winfo_children():
            w.destroy()
        self._cards.clear()
        self._card_map.clear()

        terms = self._termbase.terms
        counts = {
            "all": len(terms),
            "active": sum(1 for t in terms if t.status == "active"),
            "draft": sum(1 for t in terms if t.status == "draft"),
            "deprecated": sum(1 for t in terms if t.status == "deprecated"),
        }

        # 更新胶囊状态与数量
        for k, btn in self._pill_btns.items():
            cnt = counts.get(k, 0)
            active = k == self._status_filter
            bg = theme.SEL_ROW_ACTIVE if active else theme.CONTROL_BG
            fg = theme.FG_WHITE if active else theme.MUTED
            lbl = {"all": "全部", "active": "活跃", "draft": "草稿", "deprecated": "已废弃"}.get(k, k)
            btn.config(text=f"{lbl} {cnt}", bg=bg, fg=fg)

        self._count_lbl.config(text=f"共 {len(terms)} 个词条")

        # 过滤
        filtered = []
        for t in terms:
            if self._status_filter != "all" and t.status != self._status_filter:
                continue
            if self._search_kw:
                blob = f"{t.pref_label} {t.slug} {t.term_en} {t.definition} {' '.join(t.alt_labels)}".lower()
                if self._search_kw not in blob:
                    continue
            filtered.append(t)

        if not filtered:
            if not terms:
                # 提示空库并引导运行考古或新建
                empty_frame = tk.Frame(self._list_container, bg=theme.BG, pady=30)
                empty_frame.pack(fill="x", padx=14)
                tk.Label(
                    empty_frame,
                    text="当前术语库为空。\n\n"
                         "您可以点击上方「新建」录入新词条，\n"
                         "从操作菜单中「从考古报告合入新词」，\n"
                         "或在会话列表中右键运行「术语考古」。",
                    bg=theme.BG,
                    fg=theme.MUTED,
                    font=theme.FONT_UI,
                    justify="center",
                ).pack(pady=(0, 12))
                from .widgets import flat_button

                btn_row = tk.Frame(empty_frame, bg=theme.BG)
                btn_row.pack()
                flat_button(
                    btn_row,
                    "新建词条",
                    self._create_new_term,
                    primary=True,
                    image=ui_icon(btn_row, "plus"),
                ).pack(side="left", padx=3)
                flat_button(
                    btn_row,
                    "合入考古提案",
                    self._open_import_from_reports,
                    primary=False,
                    image=ui_icon(btn_row, "sparkle"),
                ).pack(side="left", padx=3)
            else:
                tk.Label(
                    self._list_container,
                    text="无匹配词条",
                    bg=theme.BG,
                    fg=theme.MUTED,
                    font=theme.FONT_UI,
                    pady=20,
                ).pack()
            return

        for t in filtered:
            card = self._make_term_card(t)
            self._cards.append(card)
            self._card_map[t.slug] = card
            if t.slug == self._selected_slug:
                card.set_selected(True)

    def _make_term_card(self, term) -> Card:
        from .widgets import Card
        from tcer.core.termbase import STATUS_LABELS

        slug = term.slug
        card = Card(
            self._list_container,
            on_click=lambda c, _s=slug: self.select_term(_s),
            bg=theme.PANEL,
            padx=2,
            pady=2,
        )

        # State rail 着色
        rail_col = (
            theme.ACCENT
            if term.status == "active"
            else (theme.WARNING if term.status == "draft" else theme.MUTED)
        )
        card.set_state_rail(rail_col)

        # Row 1: pref_label + status badge
        row1 = tk.Frame(card.frame, bg=card._bg)
        row1.pack(fill="x", padx=6, pady=(3, 1))
        card.track_bg(row1)

        name_lbl = tk.Label(
            row1,
            text=term.pref_label,
            bg=card._bg,
            fg=theme.FG_WHITE,
            font=theme.FONT_UI_BOLD,
            anchor="w",
        )
        name_lbl.pack(side="left", fill="x", expand=True)
        card.track_bg(name_lbl)
        card.bind_to(name_lbl)

        st_text = STATUS_LABELS.get(term.status, term.status)
        st_fg = (
            theme.ACCENT
            if term.status == "active"
            else (theme.WARNING if term.status == "draft" else theme.MUTED)
        )
        st_lbl = tk.Label(
            row1,
            text=st_text,
            bg=theme.PANEL_2,
            fg=st_fg,
            font=theme.FONT_UI_SMALL,
            padx=4,
            pady=1,
        )
        st_lbl.pack(side="right")
        card.bind_to(st_lbl)

        # Row 2: slug + term_en
        row2 = tk.Frame(card.frame, bg=card._bg)
        row2.pack(fill="x", padx=6, pady=(1, 1))
        card.track_bg(row2)

        sub_parts = [term.slug]
        if term.term_en:
            sub_parts.append(term.term_en)
        if term.mda_layer:
            sub_parts.append(f"[{_MDA_CN.get(term.mda_layer, term.mda_layer)}]")

        sub_lbl = tk.Label(
            row2,
            text=" · ".join(sub_parts),
            bg=card._bg,
            fg=theme.MUTED,
            font=theme.FONT_MONO,
            anchor="w",
        )
        sub_lbl.pack(side="left", fill="x", expand=True)
        card.track_bg(sub_lbl)
        card.bind_to(sub_lbl)

        # Row 3: definition snippet
        def_txt = (term.definition or "").strip().replace("\n", " ")
        if len(def_txt) > 36:
            def_txt = def_txt[:35] + "…"
        if def_txt:
            row3 = tk.Frame(card.frame, bg=card._bg)
            row3.pack(fill="x", padx=6, pady=(1, 3))
            card.track_bg(row3)

            desc_lbl = tk.Label(
                row3,
                text=def_txt,
                bg=card._bg,
                fg=theme.MUTED,
                font=theme.FONT_UI_SMALL,
                anchor="w",
            )
            desc_lbl.pack(side="left", fill="x", expand=True)
            card.track_bg(desc_lbl)
            card.bind_to(desc_lbl)

        return card

    def _get_form_snapshot(self) -> dict | None:
        if not hasattr(self, "_f_slug") or not self._f_slug.winfo_exists():
            return None
        return {
            "slug": self._f_slug.get().strip(),
            "pref_label": self._f_pref.get().strip(),
            "status": self._f_status.get().strip(),
            "term_en": self._f_en.get().strip(),
            "mda_layer": self._f_mda.get().strip(),
            "owner": self._f_owner.get().strip(),
            "alt_labels": self._f_alts.get().strip(),
            "definition": self._f_def.get("1.0", "end").strip(),
            "renderings": {
                rk: ent.get().strip()
                for rk, ent in self._rendering_entries.items()
                if ent.winfo_exists()
            },
            "misconceptions": [
                {
                    "role": r["role_var"].get().strip(),
                    "wrong": r["wrong_ent"].get().strip(),
                    "actual": r["act_ent"].get().strip(),
                }
                for r in self._misconception_rows
                if r["wrong_ent"].winfo_exists()
            ],
            "notes": self._f_notes.get().strip(),
        }

    def _has_unsaved_changes(self) -> bool:
        if self._baseline_snapshot is None:
            return False
        cur = self._get_form_snapshot()
        if cur is None:
            return False
        return cur != self._baseline_snapshot

    def _check_unsaved_and_confirm(self) -> bool:
        if not self._has_unsaved_changes():
            return True
        from tkinter import messagebox
        parent_win = getattr(self.controller, "root", None) or self.container.winfo_toplevel()
        pref = self._f_pref.get().strip() if hasattr(self, "_f_pref") and self._f_pref.winfo_exists() else ""
        pref = pref or self._selected_slug or "当前词条"
        res = messagebox.askyesnocancel(
            "未保存修改",
            f"词条「{pref}」有未保存的修改，是否在切换前保存？",
            parent=parent_win,
        )
        if res is True:
            return self._save_current_entry()
        elif res is False:
            return True
        else:
            return False

    def select_term(self, slug: str) -> None:
        if self._selected_slug == slug and self._card_map.get(slug):
            return
        if not self._check_unsaved_and_confirm():
            return
        if self._selected_slug and self._selected_slug in self._card_map:
            self._card_map[self._selected_slug].set_selected(False)
        self._selected_slug = slug
        if slug in self._card_map:
            self._card_map[slug].set_selected(True)
        self._render_editor()

    def _render_editor(self) -> None:
        from .widgets import flat_button
        from tcer.core.termbase import MDA_LAYERS, ROLE_LABELS, STATUS_LABELS

        for w in self._edit_container.winfo_children():
            w.destroy()
        self._misconception_rows.clear()
        self._rendering_entries.clear()

        term = next((t for t in self._termbase.terms if t.slug == self._selected_slug), None)
        if not term:
            empty = tk.Frame(self._edit_container, bg=theme.BG, pady=60)
            empty.pack(fill="both", expand=True)
            tk.Label(
                empty,
                text="请在左侧选择词条，或点击上方「新建」创建新词条",
                bg=theme.BG,
                fg=theme.MUTED,
                font=theme.FONT_UI,
            ).pack()
            return

        # 头部控制栏
        head = tk.Frame(self._edit_container, bg=theme.PANEL, padx=14, pady=10)
        head.pack(fill="x")

        head_left = tk.Frame(head, bg=theme.PANEL)
        head_left.pack(side="left", fill="x", expand=True)

        tk.Label(
            head_left,
            text=f"编辑词条: {term.pref_label}",
            bg=theme.PANEL,
            fg=theme.FG_WHITE,
            font=theme.FONT_HEADING,
        ).pack(anchor="w")

        self._save_status_lbl = tk.Label(
            head_left,
            text="",
            bg=theme.PANEL,
            fg=theme.SUCCESS,
            font=theme.FONT_UI_SMALL,
        )
        self._save_status_lbl.pack(anchor="w", pady=(2, 0))

        head_right = tk.Frame(head, bg=theme.PANEL)
        head_right.pack(side="right")

        _save_icon = ui_icon(head_right, "save")
        save_btn = flat_button(
            head_right,
            "保存词条",
            self._save_current_entry,
            primary=True,
            image=_save_icon,
            padx=12,
            pady=4,
        )
        save_btn.pack(side="right")

        # 表单主体
        body = tk.Frame(self._edit_container, bg=theme.BG, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        def make_entry(parent, label_text: str, default_val: str = "", width: int = 24):
            f = tk.Frame(parent, bg=theme.BG)
            f.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(f, text=label_text, bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
                anchor="w"
            )
            ent = tk.Entry(
                f,
                bg=theme.CONTROL_BG,
                fg=theme.FG,
                insertbackground=theme.FG,
                font=theme.FONT_UI,
                relief="flat",
                highlightthickness=0,
            )
            ent.insert(0, default_val)
            ent.pack(fill="x", pady=(2, 0))
            return ent

        # Row 1: 主标签 / 标识 / 状态
        r1 = tk.Frame(body, bg=theme.BG)
        r1.pack(fill="x", pady=(0, theme.PAD_M))
        self._f_pref = make_entry(r1, "主标签（必填）", term.pref_label)
        self._f_slug = make_entry(r1, "标识（小写英文、数字、连字符）", term.slug)

        # Status 下拉（项目惯例 ttk.Combobox；显示纯中文，保存时反查 key）
        st_frame = tk.Frame(r1, bg=theme.BG)
        st_frame.pack(side="left", padx=(0, theme.PAD_M))
        tk.Label(st_frame, text="状态", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
            anchor="w"
        )
        self._f_status = tk.StringVar(
            value=_STATUS_CN.get(term.status, _STATUS_CN["active"]))
        st_menu = ttk.Combobox(
            st_frame, textvariable=self._f_status, state="readonly",
            values=list(_STATUS_CN.values()))
        st_menu.pack(pady=(2, 0))

        # Row 2: 英文名 / 概念层面 / 负责人
        r2 = tk.Frame(body, bg=theme.BG)
        r2.pack(fill="x", pady=(0, theme.PAD_M))
        self._f_en = make_entry(r2, "英文名称", term.term_en)

        mda_frame = tk.Frame(r2, bg=theme.BG)
        mda_frame.pack(side="left", padx=(0, theme.PAD_M))
        _mda_lbl = tk.Label(mda_frame, text="概念层面", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL)
        _mda_lbl.pack(anchor="w")
        Tooltip(_mda_lbl, "机制＝代码实现规则；动态＝实际运行行为；体验＝用户感受。不确定可留空")
        self._f_mda = tk.StringVar(
            value=_MDA_CN.get(term.mda_layer or "", _MDA_CN[""]))
        mda_menu = ttk.Combobox(
            mda_frame, textvariable=self._f_mda, state="readonly",
            values=list(_MDA_CN.values()))
        mda_menu.pack(pady=(2, 0))

        self._f_owner = make_entry(r2, "负责人", term.owner)

        # Row 3: 别名
        r3 = tk.Frame(body, bg=theme.BG)
        r3.pack(fill="x", pady=(0, theme.PAD_M))
        alt_str = ", ".join(term.alt_labels)
        self._f_alts = make_entry(r3, "别名与俗称（多个用逗号分隔）", alt_str)

        # Row 4: 释义
        r4 = tk.Frame(body, bg=theme.BG)
        r4.pack(fill="x", pady=(0, theme.PAD_M + theme.PAD_XS))
        tk.Label(
            r4, text="释义（必填）", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL
        ).pack(anchor="w")
        self._f_def = tk.Text(
            r4,
            height=4,
            bg=theme.CONTROL_BG,
            fg=theme.FG,
            insertbackground=theme.FG,
            font=theme.FONT_UI,
            relief="flat",
            padx=6,
            pady=6,
            highlightthickness=0,
        )
        self._f_def.insert("1.0", term.definition)
        self._f_def.pack(fill="x", pady=(2, 0))

        # Section: 各职能怎么理解它
        r_rend = tk.Frame(body, bg=theme.BG)
        r_rend.pack(fill="x", pady=(theme.PAD_S, theme.PAD_M + theme.PAD_XS))
        _rend_lbl = tk.Label(
            r_rend,
            text="各职能怎么理解它",
            bg=theme.BG,
            fg=theme.FG_WHITE,
            font=theme.FONT_UI_BOLD,
        )
        _rend_lbl.pack(anchor="w", pady=(0, theme.PAD_S))
        Tooltip(_rend_lbl, "同一条术语在不同职能语境下的说法；留空的职能在歧义检测中沿用主释义")

        rend_grid = tk.Frame(r_rend, bg=theme.BG)
        rend_grid.pack(fill="x")
        for i, (rk, rlabel) in enumerate(ROLE_LABELS.items()):
            row_idx = i // 2
            col_idx = i % 2
            cell = tk.Frame(rend_grid, bg=theme.BG)
            cell.grid(row=row_idx, column=col_idx, sticky="ew",
                      padx=(0, theme.PAD_M), pady=(0, theme.PAD_S + theme.PAD_XS))
            rend_grid.columnconfigure(col_idx, weight=1)

            tk.Label(
                cell, text=f"{rlabel}：", bg=theme.BG, fg=theme.MUTED, font=theme.FONT_UI_SMALL
            ).pack(anchor="w")
            ent = tk.Entry(
                cell,
                bg=theme.CONTROL_BG,
                fg=theme.FG,
                insertbackground=theme.FG,
                font=theme.FONT_UI_SMALL,
                relief="flat",
                highlightthickness=0,
            )
            ent.insert(0, term.renderings.get(rk, ""))
            ent.pack(fill="x", pady=(2, 0))
            self._rendering_entries[rk] = ent

        # Section: 常见误解
        r_misc = tk.Frame(body, bg=theme.BG)
        r_misc.pack(fill="x", pady=(theme.PAD_S, theme.PAD_M + theme.PAD_XS))

        misc_head = tk.Frame(r_misc, bg=theme.BG)
        misc_head.pack(fill="x", pady=(0, theme.PAD_S))
        _misc_lbl = tk.Label(
            misc_head,
            text="常见误解",
            bg=theme.BG,
            fg=theme.FG_WHITE,
            font=theme.FONT_UI_BOLD,
        )
        _misc_lbl.pack(side="left")
        Tooltip(_misc_lbl, "该词条容易被哪个职能误解成什么、真相是什么；歧义检测据此生成提醒")

        flat_button(
            misc_head,
            "添加一条误解",
            self._add_misconception_row,
            padx=6,
            pady=1,
        ).pack(side="right")

        self._misc_container = tk.Frame(r_misc, bg=theme.BG)
        self._misc_container.pack(fill="x")

        for m in term.misconceptions:
            self._add_misconception_row(m)

        # Row Notes
        r_notes = tk.Frame(body, bg=theme.BG)
        r_notes.pack(fill="x", pady=(theme.PAD_S, theme.PAD_M + theme.PAD_XS))
        self._f_notes = make_entry(r_notes, "备注", term.notes)

        # 记录基线快照供防丢脏检查
        self._baseline_snapshot = self._get_form_snapshot()

    def _add_misconception_row(self, data: dict | None = None) -> None:
        from .widgets import flat_button
        from tcer.core.termbase import ROLE_LABELS, _ROLE_KEY_ALIASES

        row_frame = tk.Frame(self._misc_container, bg=theme.PANEL, padx=8, pady=4)
        row_frame.pack(fill="x", pady=(0, theme.PAD_S))

        # 职能下拉（纯中文显示，保存反查 key）
        raw_role = (data or {}).get("role", "designer")
        role_key = _ROLE_KEY_ALIASES.get(raw_role, raw_role)
        if role_key not in ROLE_LABELS:
            role_key = "designer"

        role_var = tk.StringVar(value=ROLE_LABELS.get(role_key, "策划"))
        role_menu = ttk.Combobox(
            row_frame, textvariable=role_var, state="readonly", width=8,
            values=list(ROLE_LABELS.values()))
        role_menu.pack(side="left", padx=(0, theme.PAD_S))

        # 误解内容
        tk.Label(row_frame, text="误读以为：", bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
            side="left"
        )
        wrong_ent = tk.Entry(
            row_frame,
            bg=theme.CONTROL_BG,
            fg=theme.FG,
            insertbackground=theme.FG,
            font=theme.FONT_UI_SMALL,
            relief="flat",
            width=22,
            highlightthickness=0,
        )
        wrong_ent.insert(0, (data or {}).get("wrong", ""))
        wrong_ent.pack(side="left", padx=(2, theme.PAD_S), fill="x", expand=True)

        # 澄清
        tk.Label(row_frame, text="实际是：", bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(
            side="left"
        )
        act_ent = tk.Entry(
            row_frame,
            bg=theme.CONTROL_BG,
            fg=theme.FG,
            insertbackground=theme.FG,
            font=theme.FONT_UI_SMALL,
            relief="flat",
            width=22,
            highlightthickness=0,
        )
        act_ent.insert(0, (data or {}).get("actual", ""))
        act_ent.pack(side="left", padx=(2, theme.PAD_S), fill="x", expand=True)

        # 删除行（小图标按钮，不用文本符号）
        del_btn = flat_button(
            row_frame,
            "",
            lambda: self._remove_misconception_row(row_item),
            image=ui_icon(row_frame, "trash"),
            padx=4,
            pady=1,
        )
        del_btn.pack(side="right")

        row_item = {
            "frame": row_frame,
            "role_var": role_var,
            "wrong_ent": wrong_ent,
            "act_ent": act_ent,
        }
        self._misconception_rows.append(row_item)

    def _remove_misconception_row(self, row_item: dict) -> None:
        if row_item in self._misconception_rows:
            self._misconception_rows.remove(row_item)
            row_item["frame"].destroy()

    def _save_current_entry(self) -> bool:
        import re
        from tcer.core.termbase import save_termbase, TermEntry, validate_entry

        if not self._selected_slug:
            return False

        slug = self._f_slug.get().strip()
        norm_slug = re.sub(r"[^a-zA-Z0-9-]+", "-", slug.replace("_", "-")).lower().strip("-")
        if norm_slug and norm_slug != slug:
            slug = norm_slug
            self._f_slug.delete(0, "end")
            self._f_slug.insert(0, slug)

        pref_label = self._f_pref.get().strip()

        status = _STATUS_CN_KEY.get(self._f_status.get().strip(), "active")

        term_en = self._f_en.get().strip()

        mda_layer = _MDA_CN_KEY.get(self._f_mda.get().strip(), "")

        owner = self._f_owner.get().strip()
        notes = self._f_notes.get().strip()

        alt_raw = self._f_alts.get().strip()
        raw_parts = re.split(r"[,，;；\n]+", alt_raw)
        seen_alts = set()
        alt_labels = []
        for a in raw_parts:
            s = a.strip()
            if s and s != pref_label and s not in seen_alts:
                seen_alts.add(s)
                alt_labels.append(s)

        definition = self._f_def.get("1.0", "end").strip()

        renderings = {}
        for rk, ent in self._rendering_entries.items():
            val = ent.get().strip()
            if val:
                renderings[rk] = val

        misconceptions = []
        from tcer.core.termbase import ROLE_LABELS as _RL
        _role_cn_key = {v: k for k, v in _RL.items()}
        for r_item in self._misconception_rows:
            role = _role_cn_key.get(r_item["role_var"].get().strip(), "designer")
            wrong = r_item["wrong_ent"].get().strip()
            actual = r_item["act_ent"].get().strip()
            if wrong or actual:
                misconceptions.append({"role": role, "wrong": wrong, "actual": actual})

        orig_term = next((t for t in self._termbase.terms if t.slug == self._selected_slug), None)
        extra = dict(orig_term.extra) if orig_term and orig_term.extra else {}

        new_entry = TermEntry(
            slug=slug,
            pref_label=pref_label,
            term_en=term_en,
            alt_labels=alt_labels,
            mda_layer=mda_layer,
            status=status,
            owner=owner,
            definition=definition,
            renderings=renderings,
            misconceptions=misconceptions,
            notes=notes,
            extra=extra,
        )

        other_slugs = {t.slug for t in self._termbase.terms if t.slug != self._selected_slug}
        errs = validate_entry(new_entry, other_slugs)
        if errs:
            self._save_status_lbl.config(text=f"保存失败: {errs[0]}", fg=theme.ERROR)
            return False

        # 更新词条并原子保存
        idx = next((i for i, t in enumerate(self._termbase.terms) if t.slug == self._selected_slug), -1)
        if idx >= 0:
            self._termbase.terms[idx] = new_entry
        else:
            self._termbase.terms.append(new_entry)

        self._selected_slug = slug
        save_termbase(self._termbase, self._termbase_path)
        self._baseline_snapshot = self._get_form_snapshot()
        self._save_status_lbl.config(text="✓ 已保存修改", fg=theme.SUCCESS)
        self._refresh_list()
        return True

    def _create_new_term(self) -> None:
        if not self._check_unsaved_and_confirm():
            return
        from tcer.core.termbase import save_termbase, TermEntry

        base_slug = "new-concept"
        slug = base_slug
        count = 1
        existing = {t.slug for t in self._termbase.terms}
        while slug in existing:
            slug = f"{base_slug}-{count}"
            count += 1

        entry = TermEntry(
            slug=slug,
            pref_label="新词条",
            status="draft",
            definition="",
        )
        self._termbase.terms.insert(0, entry)
        self._selected_slug = slug
        save_termbase(self._termbase, self._termbase_path)
        self._refresh_list()
        self._render_editor()
        if hasattr(self, "_f_pref") and self._f_pref.winfo_exists():
            self._f_pref.focus_set()
            self._f_pref.select_range(0, "end")

    def _delete_current_term(self) -> None:
        from tkinter import messagebox
        from tcer.core.termbase import save_termbase

        if not self._selected_slug:
            return

        term = next((t for t in self._termbase.terms if t.slug == self._selected_slug), None)
        if not term:
            return

        parent_win = getattr(self.controller, "root", None) or self.container.winfo_toplevel()
        if not messagebox.askyesno(
            "删除词条",
            f"确定彻底删除词条「{term.pref_label}」（{term.slug}）？\n（该操作不可恢复）",
            parent=parent_win,
        ):
            return

        self._baseline_snapshot = None
        self._termbase.terms = [t for t in self._termbase.terms if t.slug != self._selected_slug]
        self._selected_slug = self._termbase.terms[0].slug if self._termbase.terms else None
        save_termbase(self._termbase, self._termbase_path)
        self._refresh_list()
        self._render_editor()

    def _on_more_menu(self) -> None:
        from .widgets import FlatMenu

        menu = FlatMenu(self.container)
        menu.add_command(label="从考古报告合入新词…", command=self._open_import_from_reports)
        menu.add_command(label="粘贴导入词条（JSON）…", command=self._open_import)
        menu.add_separator()
        menu.add_command(label="按名称或黑话查词条", command=self._open_lookup)
        menu.add_command(label="检测一段话的误解风险", command=self._open_ambiguity)
        menu.add_command(label="从会话历史挖掘术语", command=self._open_archaeology)
        menu.add_separator()
        menu.add_command(label="导出词条库（JSON）…", command=self._export_json)
        menu.add_command(label="导出对照表（Markdown）…", command=self._export_markdown)
        menu.add_separator()
        menu.add_command(label="切换词条库文件…", command=self._choose_path)

        x = self._more_btn.winfo_rootx()
        y = self._more_btn.winfo_rooty() + self._more_btn.winfo_height()
        menu.tk_popup(x, y)

    def _open_import_from_reports(self) -> None:
        from .popups import TermImportPopup

        popup = TermImportPopup(self.container, on_imported=self.on_show)
        popup._load_from_latest_report()

    def _open_lookup(self) -> None:
        from .popups import TermLookupPopup

        TermLookupPopup(self.container, controller=self.controller, termbase=self._termbase)

    def _open_ambiguity(self) -> None:
        from .popups import AmbiguityDetectPopup

        AmbiguityDetectPopup(self.container, controller=self.controller, termbase=self._termbase)

    def _open_archaeology(self) -> None:
        if self.controller and hasattr(self.controller, "run_terms_archaeology_current"):
            self.controller.run_terms_archaeology_current()
        else:
            from tkinter import messagebox

            messagebox.showinfo(
                "术语考古",
                "请在左侧会话列表中右键点击具体会话，选择「术语考古」，\n"
                "或在分析完成后从会话上下文触发。",
                parent=self.container.winfo_toplevel(),
            )

    def _open_import(self) -> None:
        from .popups import TermImportPopup

        TermImportPopup(self.container, on_imported=self.on_show)

    def _export_json(self) -> None:
        import json
        from tkinter import filedialog, messagebox

        p = filedialog.asksaveasfilename(
            title="导出词条库为 JSON",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("全部文件", "*.*")],
            parent=self.container.winfo_toplevel(),
        )
        if not p:
            return
        try:
            from tcer.core.termbase import save_termbase

            save_termbase(self._termbase, Path(p))
            messagebox.showinfo("导出成功", f"词条库已导出至:\n{p}", parent=self.container.winfo_toplevel())
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self.container.winfo_toplevel())

    def _export_markdown(self) -> None:
        from tkinter import filedialog, messagebox
        from tcer.core.termbase import export_markdown

        p = filedialog.asksaveasfilename(
            title="导出词条库为 Markdown",
            defaultextension=".md",
            filetypes=[("Markdown 文件", "*.md"), ("全部文件", "*.*")],
            parent=self.container.winfo_toplevel(),
        )
        if not p:
            return
        try:
            md = export_markdown(self._termbase)
            Path(p).write_text(md, encoding="utf-8")
            messagebox.showinfo("导出成功", f"Markdown 对照表已导出至:\n{p}", parent=self.container.winfo_toplevel())
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self.container.winfo_toplevel())

    def _choose_path(self) -> None:
        from tkinter import filedialog
        from tcer.core import ui_prefs

        chosen = filedialog.askopenfilename(
            title="选择词条库 JSON 文件",
            filetypes=[("JSON 文件", "*.json"), ("全部文件", "*.*")],
            parent=self.container.winfo_toplevel(),
        )
        if not chosen:
            return
        ui_prefs.set_termbase_path(chosen)
        self.on_show()