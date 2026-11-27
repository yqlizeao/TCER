"""全局产物索引：各源写在项目目录之外、按会话归档的产物文件一览。

数据层在 ``tcer.core.artifacts``（纯目录枚举 + stat，不解析文件内容）；
本模块只做展示。交互心智 = 文件管理器：项目 → 会话 → 文件三级树
（父行图标 = 项目/源 logo/类型），右侧预览面板直接看文本产物前 300 行——
翻找调研/plan 不用开外部编辑器。默认「文档类」筛选把高价值文档顶到前排。
采集在 daemon worker 线程跑，结果经窗口自有 queue + 60ms 主线程轮询回填
（禁止裸跨线程 after，见 CLAUDE.md #38）。纯展示，不进指标 / CSV。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from tcer.core import artifacts
from . import theme
from .platform import FILE_MANAGER_NAME, open_in_file_manager
from .views import source_icon, ui_icon
from .widgets import (
    FlatMenu,
    RoundedEntry,
    RoundedFrame,
    ToggleSwitch,
    flat_button,
    new_window,
    show_error,
)

KIND_LABELS = {
    "plan": "计划", "report": "报告", "doc": "文档", "worker": "子代理稿",
    "media": "媒体", "code": "代码", "data": "数据", "backup": "历史备份",
    "upload": "用户上传", "internal": "内部数据", "other": "其他",
}
# kind → assets/ui-<name>.png
KIND_ICONS = {
    "plan": "target", "report": "book", "doc": "outline", "worker": "layers",
    "media": "grid", "code": "terminal", "data": "dashboard",
    "backup": "clock", "upload": "upload", "internal": "wrench", "other": "more",
}
SOURCE_LABELS = {"antigravity": "Antigravity", "claude": "Claude"}
_TEXT_EXTS = {".md", ".txt", ".rst", ".adoc", ".py", ".js", ".ts", ".tsx",
              ".jsx", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml",
              ".yml", ".toml", ".ini", ".cfg", ".log", ".html", ".css",
              ".sh", ".bat", ".ps1", ".sql", ".go", ".rs", ".java", ".c",
              ".h", ".cpp", ".cs", ".rb", ".php", ".vue", ".lua", ".r",
              ".pl", ".patch", ".diff", ".env", ".properties", ".proto"}
_PREVIEW_BYTES = 96 * 1024
_PREVIEW_LINES = 300


def _fmt_size(n: int | float) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v} B"


def _fmt_ts(ns: int) -> str:
    if not ns:
        return "-"
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M")


def _open_file(path: Path) -> None:
    """用系统默认程序打开文件（目录则进文件管理器）。纯本地操作。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        open_in_file_manager(str(path))


@dataclass
class _SessGroup:
    """一个会话的产物分组（树二级行）。"""
    source: str
    session_id: str
    title: str
    items: list = field(default_factory=list)

    @property
    def latest(self) -> int:
        return max((a.mtime_ns for a in self.items), default=0)

    @property
    def kinds_summary(self) -> str:
        c = Counter(a.kind for a in self.items)
        parts = [f"{KIND_LABELS.get(k, k)}{n}" for k, n in c.most_common(3)]
        if len(c) > 3:
            parts.append(f"等{len(c)}类")
        return " · ".join(parts)


@dataclass
class _ProjGroup:
    """一个项目的产物分组（树顶级行）。"""
    name: str
    sessions: list = field(default_factory=list)   # _SessGroup

    @property
    def n_items(self) -> int:
        return sum(len(s.items) for s in self.sessions)

    @property
    def total(self) -> int:
        return sum(a.size for s in self.sessions for a in s.items)

    @property
    def latest(self) -> int:
        return max((s.latest for s in self.sessions), default=0)


class ArtifactsWindow:
    """全局产物索引窗：项目→会话→文件三级树 + 右侧预览。"""

    _COLS = [
        ("kind", "类型", 130, False),
        ("size", "大小", 80, True),
        ("mtime", "时间", 112, True),
        ("where", "位置", 260, False),
    ]
    _UNLINKED = "（未关联会话）"
    _UNPROJ = "（未归属项目）"
    # 「文档类」快捷筛选：用户要找的计划/调研/文档/上传一键直达（默认值）
    _DOCS_KINDS = {"plan", "report", "doc", "upload"}

    def __init__(self, parent) -> None:
        self._win = new_window(parent, "全局产物索引", "1360x720")
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._q: queue.Queue = queue.Queue()
        self._gen = 0
        self._alive = True
        self._data: list[artifacts.Artifact] = []
        self._projs: list[_ProjGroup] = []
        self._iids: dict[str, object] = {}   # iid → _ProjGroup | _SessGroup | Artifact
        self._sort_col = "mtime"
        self._sort_desc = True
        self._kind_var = tk.StringVar(value="文档类")
        self._src_var = tk.StringVar(value="全部")
        self._internal_var = tk.BooleanVar(value=False)
        self._preview_var = tk.BooleanVar(value=True)
        self._search_var = tk.StringVar()
        self._status_var = tk.StringVar(value="正在扫描…")
        self._home = str(Path.home())
        self._icons: dict[str, object] = {}

        head_card = RoundedFrame(self._win, fill=theme.SURFACE_CARD,
                                 parent_bg=theme.BG,
                                 outline=theme.BORDER_SUBTLE,
                                 radius=theme.RADIUS_L)
        head_card.pack(fill="x", padx=10, pady=(10, 6))
        head = tk.Frame(head_card, bg=theme.SURFACE_CARD, padx=10, pady=6)
        head.pack(fill="x", padx=4, pady=2)
        tk.Label(head, text="全局产物索引", bg=theme.SURFACE_CARD,
                 fg=theme.FG_WHITE, font=theme.FONT_TITLE).pack(side="left")
        self._sum_lbl = tk.Label(head, text="", bg=theme.SURFACE_CARD,
                                 fg=theme.MUTED, font=theme.FONT_UI_SMALL)
        self._sum_lbl.pack(side="left", padx=12)
        flat_button(head, "刷新", self._reload,
                    image=ui_icon(head, "refresh"), compound="left").pack(
            side="right")

        bar_card = RoundedFrame(self._win, fill=theme.SURFACE_CARD,
                                parent_bg=theme.BG,
                                outline=theme.BORDER_SUBTLE,
                                radius=theme.RADIUS_L)
        bar_card.pack(fill="x", padx=10, pady=(0, 6))
        bar = tk.Frame(bar_card, bg=theme.SURFACE_CARD, padx=10, pady=6)
        bar.pack(fill="x", padx=4, pady=2)
        self._src_btn = flat_button(bar, "源: 全部 ▾", self._pick_source)
        self._src_btn.pack(side="left", padx=(0, 6))
        self._kind_btn = flat_button(bar, "类型: 文档类 ▾", self._pick_kind)
        self._kind_btn.pack(side="left", padx=(0, 6))
        ToggleSwitch(bar, self._internal_var,
                     on_toggle=self._apply_filter,
                     bg=theme.SURFACE_CARD).pack(side="left")
        tk.Label(bar, text="含内部数据", bg=theme.SURFACE_CARD, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(4, 10))
        ToggleSwitch(bar, self._preview_var,
                     on_toggle=self._toggle_preview,
                     bg=theme.SURFACE_CARD).pack(side="left")
        tk.Label(bar, text="预览", bg=theme.SURFACE_CARD, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(4, 10))
        self._expand_btn = flat_button(bar, "全部展开", self._toggle_expand)
        self._expand_btn.pack(side="left", padx=(0, 10))
        self._search = RoundedEntry(
            bar, textvariable=self._search_var, width=22, height=26,
            bg=theme.SURFACE_CARD, placeholder="搜索名称 / 路径 / 会话…",
            font=theme.FONT_UI_SMALL)
        self._search.pack(side="left")
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Label(bar, textvariable=self._status_var, bg=theme.SURFACE_CARD,
                 fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(side="right")

        body = tk.Frame(self._win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        table_card = RoundedFrame(body, fill=theme.SURFACE_CARD,
                                  parent_bg=theme.BG,
                                  outline=theme.BORDER_SUBTLE,
                                  radius=theme.RADIUS_L)
        table_card.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(table_card, bg=theme.SURFACE_CARD)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        cols = [c[0] for c in self._COLS]
        self._tree = ttk.Treeview(frame, columns=cols, show="tree headings",
                                  selectmode="browse")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)
        self._tree.heading("#0", text="名称（项目 › 会话 › 文件）")
        self._tree.column("#0", width=320, anchor="w")
        for key, label, width, _num in self._COLS:
            self._tree.heading(key, text=label,
                               command=lambda k=key: self._sort_by(k))
            self._tree.column(
                key, width=width,
                anchor="e" if key in ("size", "mtime") else "w")
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._ctx_menu)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Return>", lambda _e: self._open_selected_file())

        foot = tk.Frame(table_card, bg=theme.SURFACE_CARD)
        foot.pack(fill="x", padx=6, pady=(0, 6))
        flat_button(foot, "打开文件", self._open_selected_file).pack(
            side="left", padx=(0, 6))
        flat_button(foot, f"在{FILE_MANAGER_NAME}中打开",
                    self._open_selected_dir).pack(side="left", padx=(0, 6))
        flat_button(foot, "复制路径", self._copy_selected_path).pack(
            side="left")

        # ---- 右侧预览面板
        self._preview_card = RoundedFrame(body, fill=theme.SURFACE_CARD,
                                          parent_bg=theme.BG,
                                          outline=theme.BORDER_SUBTLE,
                                          radius=theme.RADIUS_L)
        self._preview_card.pack(side="right", fill="y", padx=(8, 0))
        self._preview_card.pack_propagate(False)
        self._preview_card.configure(width=400)
        pv = tk.Frame(self._preview_card, bg=theme.SURFACE_CARD)
        pv.pack(fill="both", expand=True, padx=8, pady=8)
        pv_head = tk.Frame(pv, bg=theme.SURFACE_CARD)
        pv_head.pack(fill="x", pady=(0, 6))
        self._pv_title = tk.Label(pv_head, text="预览", bg=theme.SURFACE_CARD,
                                  fg=theme.FG_WHITE, font=theme.FONT_UI,
                                  anchor="w", justify="left", wraplength=360)
        self._pv_title.pack(side="left", fill="x", expand=True)
        self._pv_meta = tk.Label(pv, text="选择左侧文件查看内容",
                                 bg=theme.SURFACE_CARD, fg=theme.MUTED,
                                 font=theme.FONT_UI_SMALL, anchor="w",
                                 justify="left", wraplength=360)
        self._pv_meta.pack(fill="x", pady=(0, 6))
        pv_body = tk.Frame(pv, bg=theme.SURFACE_CARD)
        pv_body.pack(fill="both", expand=True)
        self._pv_text = tk.Text(
            pv_body, wrap="word", bg=theme.PANEL_2, fg=theme.FG,
            insertbackground=theme.FG, relief="flat", bd=0,
            font=theme.FONT_UI_SMALL, padx=8, pady=6,
            state="disabled", cursor="arrow")
        pv_sb = ttk.Scrollbar(pv_body, orient="vertical",
                              command=self._pv_text.yview)
        self._pv_text.configure(yscrollcommand=pv_sb.set)
        pv_sb.pack(side="right", fill="y")
        self._pv_text.pack(fill="both", expand=True)

        self._load_icons()
        self._poll()
        self._reload()

    def _load_icons(self) -> None:
        self._icons["project"] = ui_icon(self._tree, "project")
        for k, name in KIND_ICONS.items():
            self._icons[k] = ui_icon(self._tree, name)
        for s in SOURCE_LABELS:
            self._icons[f"src:{s}"] = source_icon(self._tree, s)

    def _icon(self, key: str):
        return self._icons.get(key)

    # ------------------------------------------------------------ loading
    def _reload(self) -> None:
        self._gen += 1
        gen = self._gen
        self._status_var.set("正在扫描…")
        self._tree.delete(*self._tree.get_children())
        self._data = []
        self._projs = []

        def work():
            def _prog(phase, n):
                self._q.put(("prog", gen, (phase, n)))
            try:
                items = artifacts.collect(progress=_prog)
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", gen, f"{type(e).__name__}: {e}"))
                return
            self._q.put(("ok", gen, items))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                item = self._q.get_nowait()
                kind, gen = item[0], item[1]
                if gen != self._gen:
                    continue
                if kind == "ok":
                    self._data = item[2]
                    self._apply_filter()
                    self._status_var.set(
                        f"{len(self._data)} 项产物 · {len(self._projs)} 个项目 · "
                        f"{_fmt_size(sum(a.size for a in self._data))}")
                elif kind == "prog":
                    phase, n = item[2]
                    _names = {"antigravity": "Antigravity", "claude": "Claude"}
                    self._status_var.set(f"正在扫描 {_names.get(phase, phase)}… 已发现 {n} 项")
                elif kind == "err":
                    self._status_var.set("扫描失败")
                    show_error(self._win, "产物索引扫描失败", item[2])
        except queue.Empty:
            pass
        if self._alive:
            self._win.after(60, self._poll)

    def _on_close(self) -> None:
        self._alive = False
        self._gen += 1
        self._win.destroy()

    # ------------------------------------------------------------ filtering
    def _pick_source(self) -> None:
        m = FlatMenu(self._win)
        present = {"全部"}
        present.update(SOURCE_LABELS.get(a.source, a.source)
                       for a in self._data)
        order = ["全部"] + [v for v in SOURCE_LABELS.values() if v in present]
        order += sorted(present - set(order))
        for label in order:
            m.add_radiobutton(label=label, variable=self._src_var,
                              value=label,
                              command=self._apply_filter)
        m.tk_popup(self._src_btn.winfo_rootx(),
                   self._src_btn.winfo_rooty()
                   + self._src_btn.winfo_height())

    def _pick_kind(self) -> None:
        m = FlatMenu(self._win)
        present = {a.kind for a in self._data}
        order = ["全部", "文档类"] + [k for k in artifacts.KINDS if k in present]
        order += sorted(present - set(order))
        for k in order:
            if k == "文档类":
                n = sum(1 for a in self._data if a.kind in self._DOCS_KINDS)
                label = f"文档类（计划/报告/文档/上传，{n}）"
            elif k == "全部":
                label = "全部"
            else:
                label = (f"{KIND_LABELS.get(k, k)}"
                         f"（{sum(1 for a in self._data if a.kind == k)}）")
            m.add_radiobutton(label=label, variable=self._kind_var,
                              value=k, command=self._apply_filter)
        m.tk_popup(self._kind_btn.winfo_rootx(),
                   self._kind_btn.winfo_rooty()
                   + self._kind_btn.winfo_height())

    def _apply_filter(self) -> None:
        src = self._src_var.get()
        kind = self._kind_var.get()
        hide_internal = not self._internal_var.get()
        needle = self._search_var.get().strip().lower()
        projs: dict[str, _ProjGroup] = {}
        sess_map: dict[tuple, _SessGroup] = {}
        n_files = 0
        for a in self._data:
            if src != "全部" and SOURCE_LABELS.get(a.source, a.source) != src:
                continue
            if kind == "文档类":
                if a.kind not in self._DOCS_KINDS:
                    continue
            elif kind != "全部" and a.kind != kind:
                continue
            if hide_internal and a.kind == "internal":
                continue
            if needle:
                hay = (f"{a.path} {a.path.name} {a.session_title} "
                       f"{a.project} {KIND_LABELS.get(a.kind, a.kind)}").lower()
                if needle not in hay:
                    continue
            n_files += 1
            pname = a.project or self._UNPROJ
            pg = projs.get(pname)
            if pg is None:
                pg = projs[pname] = _ProjGroup(name=pname)
            skey = (a.source, pname, a.session_id)
            sg = sess_map.get(skey)
            if sg is None:
                sg = sess_map[skey] = _SessGroup(
                    source=a.source, session_id=a.session_id,
                    title=a.session_title
                    or (a.session_id[:8] if a.session_id else self._UNLINKED))
                pg.sessions.append(sg)
            sg.items.append(a)
        self._projs = list(projs.values())
        self._src_btn.configure(text=f"源: {src} ▾")
        k_label = (kind if kind in ("全部", "文档类")
                   else KIND_LABELS.get(kind, kind))
        self._kind_btn.configure(text=f"类型: {k_label} ▾")
        # 展开深度：搜索/具体类型 → 文件级展开；默认 → 只展开项目层
        depth = 2 if (needle or kind not in ("全部", "文档类")) else 1
        self._render(expand_depth=depth)
        if self._data:
            self._status_var.set(
                f"{len(self._data)} 项产物 · {len(self._projs)} 个项目"
                f"（筛选后 {n_files} 项）")

    # ------------------------------------------------------------ render
    def _disp_path(self, p: Path) -> str:
        s = str(p)
        if s.lower().startswith(self._home.lower()):
            return "~" + s[len(self._home):]
        return s

    def _file_text(self, a: artifacts.Artifact) -> str:
        return (f"{a.path.name}（{a.count} 个文件）"
                if a.count > 1 else a.path.name)

    def _file_values(self, a: artifacts.Artifact):
        return (KIND_LABELS.get(a.kind, a.kind),
                _fmt_size(a.size), _fmt_ts(a.mtime_ns),
                self._disp_path(a.path))

    def _sess_values(self, g: _SessGroup):
        return (g.kinds_summary, _fmt_size(sum(a.size for a in g.items)),
                _fmt_ts(g.latest), self._disp_path(self._group_dir(g)))

    def _proj_values(self, g: _ProjGroup):
        return (f"{len(g.sessions)} 个会话", _fmt_size(g.total),
                _fmt_ts(g.latest), "")

    _PSORT = {
        "size": lambda g: g.total,
        "mtime": lambda g: g.latest,
        "kind": lambda g: len(g.sessions),
    }
    _SSORT = {
        "size": lambda g: sum(a.size for a in g.items),
        "mtime": lambda g: g.latest,
        "kind": lambda g: g.kinds_summary.lower(),
    }
    _FSORT = {
        "size": lambda a: a.size,
        "mtime": lambda a: a.mtime_ns,
        "kind": lambda a: a.kind,
    }

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = col in ("size", "mtime")
        self._apply_filter()

    def _render(self, *, expand_depth: int = 1) -> None:
        pkey = self._PSORT.get(self._sort_col, lambda g: g.name.lower())
        skey = self._SSORT.get(self._sort_col, lambda g: g.title.lower())
        fkey = self._FSORT.get(self._sort_col, lambda a: a.mtime_ns)
        projs = sorted(self._projs, key=pkey, reverse=self._sort_desc)
        self._tree.delete(*self._tree.get_children())
        self._iids.clear()
        for pi, pg in enumerate(projs):
            pid = f"p{pi}"
            self._iids[pid] = pg
            self._tree.insert(
                "", "end", iid=pid,
                text=f"{pg.name}（{pg.n_items} 项）",
                image=self._icon("project"),
                values=self._proj_values(pg),
                open=expand_depth >= 1)
            sessions = sorted(pg.sessions, key=skey, reverse=self._sort_desc)
            for si, sg in enumerate(sessions):
                sid = f"{pid}.{si}"
                self._iids[sid] = sg
                self._tree.insert(
                    pid, "end", iid=sid,
                    text=f"{sg.title}（{len(sg.items)} 项）",
                    image=self._icon(f"src:{sg.source}"),
                    values=self._sess_values(sg),
                    open=expand_depth >= 2)
                for fi, a in enumerate(
                        sorted(sg.items, key=fkey, reverse=self._sort_desc)):
                    iid = f"{sid}.{fi}"
                    self._iids[iid] = a
                    self._tree.insert(sid, "end", iid=iid,
                                      text=self._file_text(a),
                                      image=self._icon(a.kind),
                                      values=self._file_values(a))

    def _toggle_expand(self) -> None:
        kids = self._tree.get_children()
        any_closed = kids and any(
            not self._tree.item(i, "open") for i in kids)
        for i in kids:
            self._tree.item(i, open=bool(any_closed))
            for j in self._tree.get_children(i):
                self._tree.item(j, open=bool(any_closed))
        self._expand_btn.configure(
            text="全部折叠" if any_closed else "全部展开")

    def _toggle_preview(self) -> None:
        if self._preview_var.get():
            self._preview_card.pack(side="right", fill="y", padx=(8, 0))
        else:
            self._preview_card.pack_forget()

    # ------------------------------------------------------------ preview
    def _set_preview(self, title: str, meta: str, body: str) -> None:
        self._pv_title.configure(text=title)
        self._pv_meta.configure(text=meta)
        self._pv_text.configure(state="normal")
        self._pv_text.delete("1.0", "end")
        self._pv_text.insert("1.0", body)
        self._pv_text.configure(state="disabled")

    def _preview_artifact(self, a: artifacts.Artifact) -> None:
        kind_lbl = KIND_LABELS.get(a.kind, a.kind)
        meta = (f"{kind_lbl} · {_fmt_size(a.size)} · {_fmt_ts(a.mtime_ns)}\n"
                f"{self._disp_path(a.path)}")
        if a.count > 1:
            names = []
            try:
                names = sorted(p.name for p in a.path.iterdir())[:40]
            except OSError:
                pass
            body = (f"聚合目录：{a.count} 个文件\n\n" + "\n".join(names)
                    + ("\n…" if len(names) >= 40 else ""))
            self._set_preview(a.path.name, meta, body)
            return
        ext = a.path.suffix.lower()
        if ext in _TEXT_EXTS and a.size <= 8 * 1024 * 1024:
            try:
                raw = a.path.read_bytes()[:_PREVIEW_BYTES]
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                body = "\n".join(lines[:_PREVIEW_LINES])
                if len(lines) > _PREVIEW_LINES or len(raw) >= _PREVIEW_BYTES:
                    body += "\n\n…（已截断，完整内容请打开文件）"
            except OSError as e:
                body = f"读取失败：{e}"
            self._set_preview(a.path.name, meta, body)
        else:
            self._set_preview(
                a.path.name, meta,
                "二进制 / 媒体文件或聚合目录，无文本预览。\n"
                "双击或点「打开文件」用系统默认程序查看。")

    def _preview_group(self, g) -> None:
        if isinstance(g, _ProjGroup):
            meta = f"{len(g.sessions)} 个会话 · {g.n_items} 项产物"
            body = "\n".join(
                f"{s.title}　{s.kinds_summary}" for s in g.sessions)
            self._set_preview(g.name, meta, body or "（空）")
        elif isinstance(g, _SessGroup):
            meta = (f"{SOURCE_LABELS.get(g.source, g.source)} · "
                    f"{len(g.items)} 项产物 · 最新 {_fmt_ts(g.latest)}")
            c = Counter(a.kind for a in g.items)
            body = "\n".join(
                f"{KIND_LABELS.get(k, k)}：{n} 项" for k, n in c.most_common())
            self._set_preview(g.title, meta, body)

    def _on_select(self, _e=None) -> None:
        s = self._selected()
        if isinstance(s, artifacts.Artifact):
            self._preview_artifact(s)
        elif s is not None:
            self._preview_group(s)

    # ------------------------------------------------------------ actions
    def _selected(self):
        sel = self._tree.selection()
        if not sel:
            return None
        return self._iids.get(sel[0])

    def _on_double_click(self, _e) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        if self._tree.get_children(iid):          # 组行：展开/折叠
            self._tree.item(iid, open=not self._tree.item(iid, "open"))
        else:
            self._open_selected_file()

    def _group_dir(self, g) -> Path:
        items = (g.items if isinstance(g, _SessGroup)
                 else [a for s in g.sessions for a in s.items])
        try:
            return Path(os.path.commonpath([str(a.path) for a in items]))
        except (ValueError, OSError):
            return items[0].path.parent if items else Path(".")

    def _open_selected_file(self) -> None:
        s = self._selected()
        if isinstance(s, artifacts.Artifact):
            _open_file(s.path)
        elif s is not None:
            open_in_file_manager(str(self._group_dir(s)))

    def _open_selected_dir(self) -> None:
        s = self._selected()
        if isinstance(s, artifacts.Artifact):
            open_in_file_manager(str(s.path))
        elif s is not None:
            open_in_file_manager(str(self._group_dir(s)))

    def _copy_selected_path(self) -> None:
        s = self._selected()
        if s is None:
            return
        p = s.path if isinstance(s, artifacts.Artifact) else self._group_dir(s)
        self._win.clipboard_clear()
        self._win.clipboard_append(str(p))
        self._status_var.set(f"已复制 {Path(p).name}")

    def _ctx_menu(self, event) -> None:
        iid = self._tree.identify_row(event.y)
        if iid:
            self._tree.selection_set(iid)
        s = self._selected()
        if s is None:
            return
        menu = FlatMenu(self._win)
        if isinstance(s, artifacts.Artifact):
            menu.add_command(label="打开文件",
                             command=self._open_selected_file,
                             image=ui_icon(self._win, "play"),
                             compound="left")
            menu.add_command(label=f"在{FILE_MANAGER_NAME}中打开",
                             command=self._open_selected_dir,
                             image=ui_icon(self._win, "folder"),
                             compound="left")
            menu.add_command(label="复制路径",
                             command=self._copy_selected_path,
                             image=ui_icon(self._win, "copy"),
                             compound="left")
        else:
            menu.add_command(label=f"在{FILE_MANAGER_NAME}中打开目录",
                             command=self._open_selected_dir,
                             image=ui_icon(self._win, "folder"),
                             compound="left")
            menu.add_command(label="复制目录路径",
                             command=self._copy_selected_path,
                             image=ui_icon(self._win, "copy"),
                             compound="left")
            menu.add_separator()
            menu.add_command(
                label="展开 / 折叠",
                command=lambda: self._tree.item(
                    iid, open=not self._tree.item(iid, "open")))
        menu.tk_popup(event.x_root, event.y_root)
