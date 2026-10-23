"""会话记录阅读器（复刻 cc-switch 的会话记录查看体验）。

数据层在 ``tcer.core.transcript``（纯函数 / 无 Tk）；本模块只做展示：

- 主区是**单个 ``tk.Text`` 文档流**（不是一行一个控件），每轮一个 ``tn:<n>`` tag，每行一个
  ``rw:<key>`` / ``a:<n>`` mark（只用 tag 的话，几万个 tag 的 ``tag_delete`` 要数秒，mark 则近乎零成本）；
  轮次默认折叠，展开时才对该轮重新渲染（删除本轮 tag 范围再插入）。
- 首次渲染按字符预算分批（``after(1)`` 串联，不用 ``after_idle``），加载中可滚动。
- 加载在 daemon worker 线程里做，结果经窗口自有的 ``queue.Queue`` + 60ms 主线程轮询回填
  （禁止裸跨线程 ``after``，见 CLAUDE.md #38）；切换会话时先置位上一次的 ``cancel_event``。
- 右侧「对话目录」用 Canvas 自绘（右对齐气泡 / 红点 / 当前轮 rail），条目多时分批构建。
- 纯展示：所有数字来自 ``Transcript``，不进指标 / CSV。

Portions of the layout and wording are modelled on cc-switch
(https://github.com/farion1231/cc-switch), Copyright (c) 2025 Jason Young, MIT License.
See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import bisect
import json
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tcer.core import tool_summary as S
from tcer.core import transcript as T
from tcer.core import ui_prefs
from . import theme
from . import transcript_md as MD
from .metric_defs import format_value
from .platform import CLICK_CURSOR, bind_mousewheel
from .views import source_icon, ui_icon
from .widgets import FlatMenu, RoundedPill, Tooltip, flat_button, new_window

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CONTENT_MAX_W = 820          # 主区内容最大宽（px），其余留白居中
BUBBLE_FRAC = 0.72           # 提问气泡占内容宽比例
BUBBLE_BG = theme.CONTROL_BG  # 提问气泡底（中性灰，对齐 cc-switch；文档与目录同色）
OUTLINE_W = 260              # 右侧目录默认宽
IND = theme.PAD_L * 2        # AI 区（步骤 / 回复）左缩进（对应头像列）
DETAIL_IND = IND + theme.PAD_L * 2
BATCH_CHARS = 40_000         # 每批渲染的字符预算
PAGE_CHARS = 512 * 1024      # 「显示全部」一页字符数
QUESTION_FOLD_CHARS = 3000   # 超长提问折叠阈值
SEARCH_EXPAND_MAX = 60       # 命中轮数不超过它才全量展开
BATCH_SECONDS = 0.035        # 每批渲染的时间预算（再配合字符预算）
DIFF_MAX_LINES = 400
CODE_MAX_LINES = 400
OUTLINE_BATCH = 120
DIVIDER_SPACES = 400
FLASH_MS = 2000
STATUS_MS = 2500

PREF_EXPAND = "transcript_expand_all"
PREF_INJECTED = "transcript_show_injected"
PREF_THINKING = "transcript_include_thinking"
PREF_OUTLINE = "transcript_outline"

_PENDING = object()           # 懒取内容尚在 worker 里
_FAST_REFS = ("jsonl", "file")


# --------------------------------------------------------------------------- #
# 会话引用
# --------------------------------------------------------------------------- #
@dataclass
class TranscriptRef:
    """阅读器切换上一个 / 下一个会话用的最小定位信息。"""
    source: str
    path: str
    session_id: str | None = None
    title: str | None = None


def refs_from_reports(reports) -> list[TranscriptRef]:
    out = []
    for r in reports:
        m = r.meta
        out.append(TranscriptRef(
            source=m.source or "claude", path=str(m.path),
            session_id=m.session_id or Path(str(m.path)).stem, title=m.title))
    return out


def _clip(text: str, n: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


def _fit_text(font: tkfont.Font, text: str, width: int, lines: int = 1) -> str:
    """按像素宽把 text 截到约 ``lines`` 行，超出加省略号。"""
    budget = int(width * lines * 0.9)
    if width <= 0 or font.measure(text) <= budget:
        return text
    budget -= font.measure("…")
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def wrap_text(font: tkfont.Font, text: str, width: int) -> list[str]:
    """按像素宽把文本折成多行（保留原有换行；中英混排逐字折，西文尽量在空格处断）。"""
    width = max(40, width)
    out: list[str] = []
    for para in text.replace("\t", "    ").split("\n"):
        rest = para
        if not rest.strip():
            out.append("")
            continue
        while rest:
            if font.measure(rest) <= width:
                out.append(rest)
                break
            lo, hi = 1, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(rest[:mid]) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            cut = lo
            if cut < len(rest):
                sp = rest.rfind(" ", 0, cut + 1)
                if sp > cut // 2:
                    cut = sp
            out.append(rest[:cut].rstrip() or rest[:cut])
            rest = rest[cut:].lstrip(" ")
    return out or [""]


def _wrap_lines(font: tkfont.Font, text: str, width: int, max_lines: int) -> list[str]:
    """按像素宽逐字折行（中英混排通吃），最多 max_lines 行，超出末行加省略号。
    每行用二分找最长可容纳前缀，measure 次数 ≈ max_lines × log2(len)。"""
    text = " ".join(text.split())
    if not text:
        return [""]
    lines: list[str] = []
    rest = text
    while rest and len(lines) < max_lines:
        last = len(lines) == max_lines - 1
        if font.measure(rest) <= width:
            lines.append(rest)
            rest = ""
            break
        budget = width - (font.measure("…") if last else 0)
        lo, hi = 1, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(rest[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        if not last and cut < len(rest):
            sp = rest.rfind(" ", 0, cut + 1)
            if sp > cut // 2:          # 西文尽量在空格处断行
                cut = sp
        lines.append(rest[:cut].rstrip() + ("…" if last else ""))
        rest = rest[cut:].lstrip()
    return lines or [""]


def title_of(tr: "T.Transcript", ref: TranscriptRef | None = None) -> str:
    """页头标题：会话标题 → 首个提问摘要 → 项目目录名 → 会话 ID 前 8 位。"""
    if tr.title and tr.title.strip():
        return tr.title.strip()
    if ref is not None and ref.title and ref.title.strip():
        return ref.title.strip()
    for t in tr.turns:
        if t.question and t.question.text.strip():
            return _clip(" ".join(t.question.text.split()), 60)
    if tr.cwd:
        name = Path(tr.cwd.replace("\\", "/")).name
        if name:
            return name
    return (tr.session_id or Path(tr.path).stem)[:8]


# --------------------------------------------------------------------------- #
# 右侧「对话目录」（Canvas 自绘）
# --------------------------------------------------------------------------- #
class _Outline:
    """每个有提问 / 斜杠的轮一组：「你」右对齐气泡 + AI 步数 / 红点 / 回复摘要。"""

    def __init__(self, parent) -> None:
        self.frame = tk.Frame(parent, bg=theme.PANEL)
        head = tk.Frame(self.frame, bg=theme.PANEL_2)
        head.pack(fill="x")
        self._title = tk.Label(head, text="对话目录", bg=theme.PANEL_2, fg=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        self._title.pack(side="left", padx=theme.PAD_M, pady=theme.PAD_S)
        self.canvas = tk.Canvas(self.frame, bg=theme.PANEL, highlightthickness=0, bd=0,
                                takefocus=0)
        self.vbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.entries: list = []
        self.turns: list = []
        self._icon = None
        self._on_you = None
        self._on_ai = None
        self._y: list[tuple[int, int]] = []
        self._cur = -1
        self._gen = 0
        self._built = 0
        self._width = 0
        self._unbind_wheel = None
        self._relayout_after = None
        self._font = tkfont.Font(font=theme.FONT_UI_SMALL)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.tag_bind("ol_hit", "<Button-1>", self._on_click)
        self.canvas.tag_bind("ol_hit", "<Enter>",
                             lambda _e: self.canvas.configure(cursor=CLICK_CURSOR))
        self.canvas.tag_bind("ol_hit", "<Leave>",
                             lambda _e: self.canvas.configure(cursor="arrow"))

    # -- 滚轮 -----------------------------------------------------------------
    def _on_enter(self, _e=None) -> None:
        self._unbind_wheel = bind_mousewheel(self.canvas, self._wheel)

    def _wheel(self, units) -> None:
        first, last = self.canvas.yview()
        if last - first < 1.0:
            self.canvas.yview_scroll(units, "units")

    def _on_leave(self, _e=None) -> None:
        try:
            px, py = self.canvas.winfo_pointerx(), self.canvas.winfo_pointery()
            cx, cy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            if cx <= px < cx + self.canvas.winfo_width() \
                    and cy <= py < cy + self.canvas.winfo_height():
                return
        except tk.TclError:
            pass
        if self._unbind_wheel:
            self._unbind_wheel()
            self._unbind_wheel = None

    # -- 构建 -----------------------------------------------------------------
    def clear(self, title: str = "对话目录") -> None:
        self._gen += 1
        self.canvas.delete("all")
        self.entries, self.turns, self._y, self._cur, self._built = [], [], [], -1, 0
        self._title.configure(text=title)

    def set_entries(self, entries, turns, icon, on_you, on_ai) -> None:
        self.clear(f"对话目录 {len(entries)}" if entries else "对话目录")
        self.entries, self.turns, self._icon = list(entries), turns, icon
        self._on_you, self._on_ai = on_you, on_ai
        self._pending_cur = None
        self._width = max(0, self.canvas.winfo_width())
        self._build_from(0, self._gen)

    def _on_configure(self, e) -> None:
        if abs(e.width - self._width) < 4 or not self.entries:
            return
        if self._relayout_after is not None:
            try:
                self.canvas.after_cancel(self._relayout_after)
            except tk.TclError:
                pass
        self._relayout_after = self.canvas.after(150, self._relayout)

    def _relayout(self) -> None:
        self._relayout_after = None
        cur = self._cur
        self.canvas.delete("all")
        self._y, self._cur, self._built, self._gen = [], -1, 0, self._gen + 1
        self._width = max(0, self.canvas.winfo_width())
        self._pending_cur = cur if cur >= 0 else None
        self._build_from(0, self._gen)

    def _build_from(self, start: int, gen: int) -> None:
        if gen != self._gen:
            return
        end = min(len(self.entries), start + OUTLINE_BATCH)
        y = self._y[-1][1] if self._y else theme.PAD_S
        for i in range(start, end):
            y0 = y
            y = self._draw_entry(i, y)
            self._y.append((y0, y))
        self._built = end
        self.canvas.configure(scrollregion=(0, 0, max(1, self._width), y + theme.PAD_M))
        pend = getattr(self, "_pending_cur", None)
        if pend is not None and pend < self._built:
            self._pending_cur = None
            self.set_current(pend)
        if end < len(self.entries):
            self.canvas.after(1, lambda: self._build_from(end, gen))

    def _draw_entry(self, i: int, y: int) -> int:
        """画一个目录条目。文字自己按像素折行（``_wrap_lines``）、几何自己算，
        不依赖 canvas 的 ``width=`` 自动折行与 ``bbox``（省掉逐项 bbox 往返）。"""
        c, ent = self.canvas, self.entries[i]
        turn = self.turns[ent.turn_index]
        f = self._font
        ls = f.metrics("linespace")
        w = max(120, self._width or OUTLINE_W)
        padx = theme.PAD_M
        right, left = w - padx - 4, padx + 4
        inner = right - left
        tag_i = f"ei:{i}"
        top = y
        bg = c.create_rectangle(0, y, w, y + 1, fill=theme.PANEL_2, outline="", state="hidden",
                                tags=(f"ebg:{i}",))
        rail = c.create_rectangle(0, y, theme.RAIL_W_NAV, y + 1, fill=theme.ACCENT, outline="",
                                  state="hidden", tags=(f"erail:{i}",))
        y += theme.PAD_S
        # 你
        when = T.format_message_time(ent.ts)
        c.create_text(right, y, anchor="ne", text=f"你 · {when}" if when else "你",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, tags=("ol_hit", tag_i, "ol_you"))
        y += ls + theme.PAD_XS
        qtxt = T.snippet(turn.question.text, 160) if turn.question and turn.question.text \
            else (ent.question_preview or "")
        lines = _wrap_lines(f, qtxt or "（无文字）", inner - 2 * theme.PAD_S, 2)
        tw = max(f.measure(ln) for ln in lines)
        th = ls * len(lines)
        x1 = right
        x0 = right - tw - 2 * theme.PAD_S
        r = c.create_rectangle(x0, y, x1, y + th + 2 * theme.PAD_XS, fill=BUBBLE_BG,
                               outline="", tags=("ol_hit", tag_i, "ol_you"))
        c.create_text(x0 + theme.PAD_S, y + theme.PAD_XS, anchor="nw", text="\n".join(lines),
                      fill=theme.FG, font=theme.FONT_UI_SMALL, justify="left",
                      tags=("ol_hit", tag_i, "ol_you"))
        del r
        y += th + 2 * theme.PAD_XS + theme.PAD_S
        # AI
        x = left
        if self._icon is not None:
            c.create_image(x, y, anchor="nw", image=self._icon,
                           tags=("ol_hit", tag_i, "ol_ai"))
            x += 20
        label = f"{ent.step_count} 步"
        c.create_text(x, y + 1, anchor="nw", text=label, fill=theme.FG,
                      font=theme.FONT_UI_SMALL, tags=("ol_hit", tag_i, "ol_ai"))
        if ent.has_error:
            d = theme.PAD_S + 2
            cx = x + f.measure(label) + theme.PAD_S
            cy = y + 1 + ls // 2
            c.create_oval(cx, cy - d // 2, cx + d, cy + d // 2,
                          fill=theme.ERROR, outline="", tags=("ol_hit", tag_i, "ol_ai"))
        y += max(ls + 2, 16) + theme.PAD_XS
        if turn.aborted:
            reply, fill, font = "已中断", theme.WARNING, theme.FONT_UI_SMALL
        elif turn.final and turn.final.text.strip():
            reply = T.snippet(turn.final.text, 200) or "（无文字）"
            fill, font = theme.MUTED, theme.FONT_UI_SMALL
        else:
            reply, fill = "没有回复", theme.MUTED
            font = (theme.FONT_UI_SMALL[0], theme.FONT_UI_SMALL[1], "italic")
        lines = _wrap_lines(f, reply, inner, 2)
        c.create_text(left, y, anchor="nw", text="\n".join(lines), fill=fill, font=font,
                      justify="left", tags=("ol_hit", tag_i, "ol_ai"))
        y += ls * len(lines) + theme.PAD_S
        c.coords(bg, 0, top, w, y)
        c.coords(rail, 0, top, theme.RAIL_W_NAV, y)
        return y + theme.PAD_XS

    # -- 交互 -----------------------------------------------------------------
    def _on_click(self, _e=None) -> None:
        tags = self.canvas.gettags("current")
        idx = next((int(t[3:]) for t in tags if t.startswith("ei:")), None)
        if idx is None:
            return
        if "ol_ai" in tags:
            if self._on_ai:
                self._on_ai(self.entries[idx])
        elif self._on_you:
            self._on_you(self.entries[idx])

    def set_current(self, i: int) -> None:
        if i == self._cur:
            return
        if i >= self._built:
            self._pending_cur = i
            return
        c = self.canvas
        if 0 <= self._cur < self._built:
            for pre in ("ebg", "erail"):
                c.itemconfigure(f"{pre}:{self._cur}", state="hidden")
        self._cur = i
        if i < 0:
            return
        for pre in ("ebg", "erail"):
            c.itemconfigure(f"{pre}:{i}", state="normal")
        y0, y1 = self._y[i]
        total = max(1, self._y[-1][1] + theme.PAD_M)
        top, bot = c.yview()
        h = c.winfo_height()
        if h > 1 and total > h and (y0 < top * total or y1 > bot * total):
            c.yview_moveto(max(0.0, (y0 - theme.PAD_M) / total))


# --------------------------------------------------------------------------- #
# 阅读器窗口
# --------------------------------------------------------------------------- #
class TranscriptWindow:
    """会话记录阅读器（Toplevel，Esc 关闭；无显式关闭按钮）。"""

    def __init__(self, parent, controller, refs: list[TranscriptRef], index: int = 0) -> None:
        self._parent = parent
        self._ctl = controller
        self._refs = list(refs)
        self._idx = max(0, min(index, len(self._refs) - 1)) if self._refs else 0
        prefs = getattr(controller, "_ui_prefs", None)
        self._prefs: dict = prefs if isinstance(prefs, dict) else ui_prefs.load()
        self._expand_all = bool(self._prefs.get(PREF_EXPAND, False))
        self._show_injected = bool(self._prefs.get(PREF_INJECTED, False))
        self._include_thinking = bool(self._prefs.get(PREF_THINKING, False))
        self._outline_on = bool(self._prefs.get(PREF_OUTLINE, True))
        self._view = T.VIEW_ALL
        self._tr: T.Transcript | None = None
        self._turns: list = []
        self._entries: list = []
        self._style = S.get_style("generic")
        self._state = "idle"                 # idle / loading / ready / error
        self._error = ""
        self._cancel: threading.Event | None = None
        self._load_gen = 0
        self._ui_queue: queue.Queue = queue.Queue()
        self._closed = False
        # 文档状态
        self._turn_ov: dict = {}
        self._step_ov: dict = {}
        self._show_all: set = set()          # 「显示全部」输出的步骤
        self._open_text: set = set()         # 展开的超长提问 / 说明 key
        self._content: dict = {}             # 懒取内容缓存 key → str
        self._content_pending: set = set()
        self._content_turn: dict = {}        # key → turn index（取到后重绘）
        self._search: T.SearchHits | None = None
        self._query = ""
        self._hit_n = 0
        self._rows_cache = None
        self._rgen = 0
        self._render_pos = 0
        self._render_done = True
        self._any_rows = False
        self._turn_div: dict = {}
        self._turn_row_count: dict = {}
        self._acts: dict = {}
        self._act_seq = 0
        self._marks: dict = {}               # turn index -> 该轮的 mark 名（重绘 / 清空时统一 unset）
        self._cur_rt = -1
        self._search_open: set = set()       # 命中很多时，只展开这些轮（其余轮点开 / 跳到时再展开）
        self._eff_cache = None
        self._ctx: tuple = ()
        self._chars = 0
        self._row_total = 0
        self._restore_turn: int | None = None
        self._cfg_after = None
        self._scroll_after = None
        self._find_after = None
        self._status_after = None
        self._flash_after = None
        self._cur_turn = -1
        self._last_w = -1
        self._cw = CONTENT_MAX_W
        self._hover_act = None
        self._bfont = tkfont.Font(font=theme.FONT_UI)       # 气泡折行量字用
        self._space_w = max(1, self._bfont.measure(" "))
        self._tbl_font_b = tkfont.Font(font=theme.FONT_UI_BOLD)   # 表格列宽量字（取粗体，偏保守）
        self._rule_w = max(1, tkfont.Font(font=theme.FONT_UI_SMALL).measure(" "))
        self._tbl_seq = 0
        self._pad_font = tkfont.Font(font=(theme.FONT_UI[0], 3))
        self._render_bpx = 0
        self.perf: dict = {}
        self._cnt_var = None

        self._win = new_window(parent, "会话记录", "1180x760")
        self._cnt_var = tk.IntVar(self._win)
        self._build_ui()
        self._win.bind("<Escape>", self._on_escape)
        self._win.bind("<Control-f>", self._on_ctrl_f)
        self._win.bind("<Key-j>", lambda e: self._jump_turn(+1, e))
        self._win.bind("<Key-k>", lambda e: self._jump_turn(-1, e))
        self._win.bind("<Destroy>", self._on_destroy, add="+")
        self._poll_ui_queue()
        self._open_index(self._idx)

    # ------------------------------------------------------------------ 生命周期
    def alive(self) -> bool:
        try:
            return (not self._closed) and bool(self._win.winfo_exists())
        except tk.TclError:
            return False

    def open(self, refs: list[TranscriptRef], index: int) -> None:
        """复用已开的窗口：换会话序列并加载。"""
        self._refs = list(refs)
        self._open_index(max(0, min(index, len(self._refs) - 1)))
        try:
            self._win.deiconify()
            self._win.lift()
        except tk.TclError:
            pass

    def _on_destroy(self, e) -> None:
        if e.widget is not self._win:
            return
        self._closed = True
        self._rgen += 1
        if self._cancel is not None:
            self._cancel.set()

    def _on_escape(self, _e=None):
        if self._findbar.winfo_ismapped():
            self._close_find()
            return "break"
        self._win.destroy()
        return "break"

    # ------------------------------------------------------------------ 队列轮询
    def _poll_ui_queue(self) -> None:
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            if not self._closed and self._win.winfo_exists():
                self._win.after(30 if self._state == "loading" else 60, self._poll_ui_queue)
        except (tk.TclError, RuntimeError):
            pass

    # ------------------------------------------------------------------ UI 构建
    def _icon_btn(self, parent, role, tip, command, *, bg=None, text=""):
        img = ui_icon(parent, role)
        kw = dict(bg=bg) if bg else {}
        if img is not None:
            b = flat_button(parent, text, command, image=img, padx=theme.PAD_S,
                            pady=theme.PAD_S, **kw)
            b.image = img
        else:
            b = flat_button(parent, text or tip[:2], command, padx=theme.PAD_S, **kw)
        Tooltip(b, tip)
        return b

    def _build_ui(self) -> None:
        win = self._win
        # ---- 页头 ----
        hdr = tk.Frame(win, bg=theme.PANEL_2)
        hdr.pack(fill="x")
        row1 = tk.Frame(hdr, bg=theme.PANEL_2)
        row1.pack(fill="x", padx=theme.PAD_L, pady=(theme.PAD_M, theme.PAD_S))
        self._more_btn = self._icon_btn(row1, "more", "更多", self._open_more_menu,
                                        bg=theme.PANEL_2)
        self._more_btn.pack(side="right", padx=(theme.PAD_S, 0))
        self._resume_menu_btn = self._icon_btn(row1, "chevron-down", "进入目录并恢复…",
                                               self._open_resume_menu, bg=theme.CONTROL_BG)
        self._resume_menu_btn.pack(side="right")
        self._resume_btn = flat_button(row1, "复制恢复命令", self._copy_resume,
                                       image=ui_icon(row1, "play"), bg=theme.CONTROL_BG)
        self._resume_btn.pack(side="right", padx=(theme.PAD_M, 1))
        self._next_btn = self._icon_btn(row1, "chevron-right", "下一个会话",
                                        lambda: self._step_session(+1), bg=theme.PANEL_2)
        self._next_btn.pack(side="right")
        self._prev_btn = self._icon_btn(row1, "chevron-left", "上一个会话",
                                        lambda: self._step_session(-1), bg=theme.PANEL_2)
        self._prev_btn.pack(side="right", padx=(theme.PAD_M, 0))
        self._src_lbl = tk.Label(row1, bg=theme.PANEL_2)
        self._src_lbl.pack(side="left", padx=(0, theme.PAD_S))
        self._title_full = ""
        self._title_font = tkfont.Font(font=theme.FONT_HEADING)
        self._title_lbl = tk.Label(row1, text="", bg=theme.PANEL_2, fg=theme.FG_WHITE,
                                   font=theme.FONT_HEADING, anchor="w", width=1)
        self._title_lbl.pack(side="left", fill="x", expand=True)
        self._title_lbl.bind("<Configure>", lambda _e: self._fit_title())
        # 第 2 行：左信息条 + 右工具组（对齐 cc-switch 两行页头；工具组先 pack，窄窗时
        # 只挤压信息条，按钮不被截断）
        row2 = tk.Frame(hdr, bg=theme.PANEL_2)
        row2.pack(fill="x", padx=theme.PAD_L, pady=(0, theme.PAD_M))
        inner = tk.Frame(row2, bg=theme.PANEL_2)
        inner.pack(side="right")
        self._info = tk.Frame(row2, bg=theme.PANEL_2)
        self._info.pack(side="left", fill="x", expand=True)
        self._reason_lbl = tk.Label(hdr, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                    font=theme.FONT_UI_SMALL, anchor="w")
        tk.Frame(win, bg=theme.BORDER, height=1).pack(fill="x")

        self._outline_btn = self._icon_btn(inner, "panel-right", "显示 / 隐藏对话目录",
                                           self._toggle_outline, bg=theme.PANEL_2)
        self._outline_btn.pack(side="left", padx=(theme.PAD_M, theme.PAD_S))
        self._pills: dict = {}
        for key, label in ((T.VIEW_ALL, "全部"), (T.VIEW_CONVERSATION, "对话"),
                           (T.VIEW_CHANGES, "改动")):
            p = RoundedPill(inner, text=label, width=52, height=24, radius=theme.RADIUS_M,
                            fill=theme.CONTROL_BG, bg=theme.PANEL_2, fg=theme.MUTED,
                            font=theme.FONT_UI_SMALL,
                            command=lambda _p=None, k=key: self._set_view(k))
            p.pack(side="left", padx=(0, theme.PAD_XS))
            self._pills[key] = p
        self._refresh_pills()
        self._opt_btn = self._icon_btn(inner, "sliders", "显示选项", self._open_options_menu,
                                       bg=theme.PANEL_2)
        self._opt_btn.pack(side="left", padx=(theme.PAD_M, 0))
        self._expand_btn = self._icon_btn(inner, "expand-all", "展开全部过程",
                                          lambda: self._set_expand_all(True), bg=theme.PANEL_2)
        self._expand_btn.pack(side="left", padx=(theme.PAD_XS, 0))
        self._collapse_btn = self._icon_btn(inner, "collapse-all", "收起全部过程",
                                            lambda: self._set_expand_all(False), bg=theme.PANEL_2)
        self._collapse_btn.pack(side="left", padx=(theme.PAD_XS, 0))
        self._export_btn = self._icon_btn(inner, "export", "导出 Markdown", self._export_md,
                                          bg=theme.PANEL_2)
        self._export_btn.pack(side="left", padx=(theme.PAD_M, 0))
        self._find_btn = self._icon_btn(inner, "search", "查找（Ctrl+F）", self._toggle_find,
                                        bg=theme.PANEL_2)
        self._find_btn.pack(side="left", padx=(theme.PAD_XS, 0))
        # 状态提示（复制成功 / 加载进度）浮在信息条右端，紧贴工具组
        self._status = tk.Label(row2, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                font=theme.FONT_UI_SMALL)
        self._status.pack(side="right", padx=theme.PAD_M, before=self._info)

        # ---- 查找条（隐藏态）----
        self._findbar = tk.Frame(win, bg=theme.PANEL_2)
        fi = tk.Frame(self._findbar, bg=theme.PANEL_2)
        fi.pack(fill="x", padx=theme.PAD_L, pady=theme.PAD_S)
        self._find_var = tk.StringVar()
        self._find_entry = tk.Entry(fi, textvariable=self._find_var, width=28,
                                    bg=theme.CONTROL_BG, fg=theme.FG, insertbackground=theme.FG,
                                    relief="flat", bd=0, highlightthickness=0,
                                    font=theme.FONT_UI, selectbackground=theme.HOVER_ACCENT,
                                    selectforeground=theme.FG_WHITE)
        self._find_entry.pack(side="left", ipady=3, padx=(0, theme.PAD_M))
        self._find_var.trace_add("write", lambda *_a: self._schedule_find())
        self._find_entry.bind("<Return>", lambda _e: (self._find_step(+1), "break")[1])
        self._find_entry.bind("<Shift-Return>", lambda _e: (self._find_step(-1), "break")[1])
        self._find_count = tk.Label(fi, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                    font=theme.FONT_UI_SMALL, width=9, anchor="w")
        self._find_count.pack(side="left")
        self._icon_btn(fi, "arrow-up", "上一个（Shift+Enter）", lambda: self._find_step(-1),
                       bg=theme.PANEL_2).pack(side="left")
        self._icon_btn(fi, "arrow-down", "下一个（Enter）", lambda: self._find_step(+1),
                       bg=theme.PANEL_2).pack(side="left", padx=(theme.PAD_XS, 0))
        self._icon_btn(fi, "close", "关闭查找（Esc）", self._close_find,
                       bg=theme.PANEL_2).pack(side="right")

        # ---- 主体 ----
        self._body = tk.PanedWindow(win, orient="horizontal", sashwidth=3, sashrelief="flat",
                                    bd=0, bg=theme.BORDER, opaqueresize=True)
        self._body.pack(fill="both", expand=True)
        left = tk.Frame(self._body, bg=theme.BG)
        self._left = left
        self._outline = _Outline(self._body)
        self._body.add(left, stretch="always", minsize=420)
        if self._outline_on:
            self._body.add(self._outline.frame, stretch="never", minsize=200, width=OUTLINE_W)
        sb = ttk.Scrollbar(left, orient="vertical")
        sb.pack(side="right", fill="y")
        self._txt = tk.Text(
            left, wrap="char", bg=theme.BG, fg=theme.FG, font=theme.FONT_UI, relief="flat",
            bd=0, highlightthickness=0, padx=IND, pady=theme.PAD_L, cursor="arrow",
            state="disabled", insertwidth=0, yscrollcommand=lambda a, b: self._on_yscroll(sb, a, b),
            selectbackground=theme.HOVER_ACCENT, selectforeground=theme.FG_WHITE,
            inactiveselectbackground=theme.HOVER_ACCENT, exportselection=True, undo=False,
            takefocus=0)
        sb.configure(command=self._txt.yview)
        self._txt.pack(side="left", fill="both", expand=True)
        self._latest_btn = flat_button(left, "最新", self._scroll_latest,
                                       image=ui_icon(left, "latest"), primary=True)
        self._configure_tags()
        t = self._txt
        t.bind("<Configure>", self._on_text_configure)
        t.bind("<Button-1>", lambda _e: t.focus_set(), add="+")
        t.bind("<Button-3>", self._on_text_context)
        t.bind("<Motion>", self._on_motion)
        t.tag_bind("act", "<ButtonRelease-1>", self._on_act_click)
        t.tag_bind("act", "<Enter>", lambda _e: t.configure(cursor=CLICK_CURSOR))
        t.tag_bind("act", "<Leave>", lambda _e: t.configure(cursor="arrow"))

    # ------------------------------------------------------------------ tag 配置
    def _configure_tags(self) -> None:
        t = self._txt
        ui, mono = theme.FONT_UI, theme.FONT_MONO
        small = theme.FONT_UI_SMALL
        italic = (ui[0], ui[1], "italic")
        pad_s, pad_xs = theme.PAD_S, theme.PAD_XS
        t.tag_configure("sel", background=theme.HOVER_ACCENT, foreground=theme.FG_WHITE)
        # 文案行
        t.tag_configure("empty_msg", font=theme.FONT_UI, foreground=theme.MUTED,
                        justify="center", spacing1=theme.PAD_L * 4)
        t.tag_configure("err_msg", font=theme.FONT_UI, foreground=theme.ERROR,
                        justify="center", spacing1=theme.PAD_L * 4, spacing3=theme.PAD_S)
        # 数据推断说明（非告警）：弱化显示；页头「N 条提示」保留醒目入口
        t.tag_configure("warn_msg", font=small, foreground=theme.MUTED, spacing3=pad_s)
        # 分轮线：一行带删除线的空格（1px 线）
        t.tag_configure("divider", overstrike=True, overstrikefg=theme.BORDER,
                        foreground=theme.BG, wrap="none", spacing1=theme.PAD_L,
                        spacing3=theme.PAD_L)
        # 提问气泡（lmargin / tabs 随宽度在 _apply_metrics 里更新）
        t.tag_configure("q_meta", font=small, foreground=theme.MUTED, justify="right",
                        spacing1=theme.PAD_M, spacing3=pad_xs)
        # 提问气泡：每行 = 无底色前导空格 + 带底色正文（补空格对齐成实心矩形）+ 无底色换行，
        # 整行 justify=right。Tk 的 tag 底色会把行首的缩进 / 对齐偏移区一并涂满，所以
        # 行首 / 行尾必须是无底色字符，气泡才不会铺成整行色条。
        t.tag_configure("q_line", justify="right", spacing1=0, spacing3=0, font=ui)
        t.tag_configure("q_bub", foreground=theme.FG_WHITE, background=BUBBLE_BG)
        t.tag_configure("q_pad", font=(ui[0], 3), background=BUBBLE_BG)
        t.tag_configure("q_more", font=small, foreground=theme.CYAN, justify="right")
        # AI 头像行
        t.tag_configure("ai_head", spacing1=theme.PAD_M, spacing3=pad_s, lmargin1=0, lmargin2=0)
        t.tag_configure("ai_name", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE)
        t.tag_configure("ai_meta", font=small, foreground=theme.MUTED)
        t.tag_configure("ai_model", font=(theme.FONT_MONO[0], small[1]), foreground=theme.MUTED)
        # 时间线摘要行
        t.tag_configure("tl_head", font=small, foreground=theme.MUTED,
                        lmargin1=IND, lmargin2=IND, spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("tl_err", foreground=theme.ERROR)
        t.tag_configure("tl_more", font=small, foreground=theme.MUTED,
                        lmargin1=IND + theme.PAD_L, lmargin2=IND + theme.PAD_L,
                        spacing1=pad_xs, spacing3=pad_xs)
        # 步骤行
        for d in (0, 1):
            m = IND + d * theme.PAD_L * 2
            t.tag_configure(f"step_d{d}", lmargin1=m, lmargin2=m + theme.PAD_L,
                            spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("step_ui", font=ui, foreground=theme.FG)
        t.tag_configure("step_mono", font=mono, foreground=theme.FG)
        t.tag_configure("step_detail", font=small, foreground=theme.MUTED)
        t.tag_configure("step_meta", font=small, foreground=theme.MUTED)
        t.tag_configure("chev", font=small, foreground=theme.MUTED)
        for name, col in (("tone_success", theme.SUCCESS), ("tone_danger", theme.ERROR),
                          ("tone_warning", theme.WARNING), ("tone_muted", theme.MUTED),
                          ("tone_neutral", theme.MUTED)):
            t.tag_configure(name, foreground=col)
        # 步骤详情
        dm = DETAIL_IND
        t.tag_configure("dt_label", font=small, foreground=theme.MUTED, lmargin1=dm, lmargin2=dm,
                        spacing1=pad_s)
        t.tag_configure("dt_text", font=small, foreground=theme.FG, lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_code", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.PANEL_2, lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_out", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm + theme.PAD_L)
        t.tag_configure("dt_conn", font=(mono[0], small[1]), foreground=theme.MUTED)
        t.tag_configure("dt_file", font=(mono[0], small[1]), foreground=theme.CODE_MINT,
                        lmargin1=dm, lmargin2=dm, spacing1=pad_s)
        t.tag_configure("dt_gap", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_add", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.SUCCESS_TINT_BG, lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_del", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.ERROR_TINT_BG, lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_ctx", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm)
        t.tag_configure("dt_sign_add", foreground=theme.SUCCESS)
        t.tag_configure("dt_sign_del", foreground=theme.ERROR)
        t.tag_configure("dt_ln", foreground=theme.MUTED)
        t.tag_configure("dt_more", font=small, foreground=theme.CYAN, lmargin1=dm, lmargin2=dm)
        # 事件 / 注入
        t.tag_configure("ev_line", font=small, foreground=theme.MUTED, justify="center",
                        spacing1=theme.PAD_S, spacing3=theme.PAD_S)
        t.tag_configure("ev_danger", foreground=theme.ERROR)
        t.tag_configure("ev_detail", font=small, foreground=theme.MUTED, justify="center",
                        lmargin1=IND, lmargin2=IND)
        t.tag_configure("inj", font=small, foreground=theme.MUTED, background=theme.PANEL,
                        lmargin1=IND, lmargin2=IND, spacing1=pad_xs, spacing3=pad_xs)
        # Markdown（两套缩进：正文 / 说明）
        for sfx, base in (("", IND), ("_n", DETAIL_IND)):
            t.tag_configure(f"md_p{sfx}", font=ui, foreground=theme.FG, lmargin1=base,
                            lmargin2=base, spacing1=pad_xs, spacing3=pad_xs)
            for lv, f in ((1, theme.FONT_TITLE), (2, theme.FONT_HEADING),
                          (3, theme.FONT_UI_BOLD), (4, theme.FONT_UI_BOLD),
                          (5, theme.FONT_UI_BOLD), (6, theme.FONT_UI_BOLD)):
                t.tag_configure(f"md_h{lv}{sfx}", font=f, foreground=theme.FG_WHITE,
                                lmargin1=base, lmargin2=base, spacing1=theme.PAD_M,
                                spacing3=pad_s)
            for dp in range(4):
                lm = base + theme.PAD_M * (dp + 1)
                t.tag_configure(f"md_li{dp}{sfx}", font=ui, foreground=theme.FG, lmargin1=lm,
                                lmargin2=lm + theme.PAD_L, spacing1=pad_xs, spacing3=pad_xs)
            t.tag_configure(f"md_quote{sfx}", font=italic, foreground=theme.MUTED,
                            lmargin1=base + theme.PAD_M, lmargin2=base + theme.PAD_M,
                            spacing1=pad_xs, spacing3=pad_xs)
            t.tag_configure(f"md_codeblock{sfx}", font=mono, foreground=theme.FG,
                            background=theme.PANEL_2, lmargin1=base + theme.PAD_S,
                            lmargin2=base + theme.PAD_S, spacing1=0, spacing3=0)
            t.tag_configure(f"md_lang{sfx}", font=small, foreground=theme.MUTED,
                            lmargin1=base + theme.PAD_S, lmargin2=base + theme.PAD_S,
                            spacing1=pad_s)
            t.tag_configure(f"md_hr{sfx}", font=small, foreground=theme.BORDER,
                            lmargin1=base, lmargin2=base)
        # 表格：表头加粗提亮；分隔线 = 一行带删除线的空格（同分轮线做法，1px 细线）
        t.tag_configure("md_th", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE,
                        spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("md_td", font=ui, foreground=theme.FG)
        t.tag_configure("md_trule", font=small, overstrike=True, overstrikefg=theme.BORDER,
                        foreground=theme.BG, spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("md_gap", font=(ui[0], pad_s))
        t.tag_configure("md_bold", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE)
        t.tag_configure("md_italic", font=italic)
        t.tag_configure("md_code", font=mono, foreground=theme.CODE_MINT)
        t.tag_configure("md_strike", overstrike=True)
        t.tag_configure("md_link", foreground=theme.CYAN, underline=True)
        t.tag_configure("md_muted", foreground=theme.MUTED)
        # 行内状态（优先级最高，必须最后创建 / 抬升）
        t.tag_configure("row_hover", background=theme.HOVER_BG)
        t.tag_configure("flash", background=theme.INFO_TINT_BG)
        t.tag_configure("find_hit", background=theme.WARNING, foreground=theme.BG)
        t.tag_configure("find_cur", background=theme.ACCENT, foreground=theme.FG_WHITE)
        t.tag_raise("sel")

    def _bubble_px(self) -> int:
        """提问气泡最大像素宽（含左右各一个空格的内边距）。"""
        return max(200, int(self._cw * BUBBLE_FRAC))

    def _apply_metrics(self) -> None:
        """按窗口宽度更新内容留白 / 气泡边距 / 步骤元信息右对齐制表位。"""
        t = self._txt
        w = max(300, t.winfo_width())
        pad = max(IND, (w - CONTENT_MAX_W) // 2)
        t.configure(padx=pad)
        cw = w - 2 * pad
        self._cw = cw
        for tag in ("q_meta", "q_line", "q_more"):
            t.tag_configure(tag, rmargin=theme.PAD_S)
        for d in (0, 1):
            t.tag_configure(f"step_d{d}", tabs=(max(100, cw - theme.PAD_M), "right"))
        t.tag_configure("tl_head", rmargin=0)

    def _on_text_configure(self, e) -> None:
        if abs(e.width - self._last_w) < 6:
            return
        self._last_w = e.width
        if self._cfg_after is not None:
            try:
                self._win.after_cancel(self._cfg_after)
            except tk.TclError:
                pass
        self._cfg_after = self._win.after(120, self._apply_metrics_later)

    def _apply_metrics_later(self) -> None:
        self._cfg_after = None
        if self._closed:
            return
        self._apply_metrics()
        # 气泡是按宽度预折行的纯文本：宽度变化明显时整篇重排（分批，保持当前位置）
        if self._state == "ready" and abs(self._bubble_px() - self._render_bpx) > 16:
            self._rerender_all()

    # ------------------------------------------------------------------ 状态 / 小提示
    def _flash_status(self, text: str, *, error: bool = False) -> None:
        try:
            self._status.configure(text=text, fg=theme.ERROR if error else theme.SUCCESS)
            if self._status_after is not None:
                self._win.after_cancel(self._status_after)
            self._status_after = self._win.after(STATUS_MS, self._clear_status)
        except tk.TclError:
            pass

    def _clear_status(self) -> None:
        self._status_after = None
        if self._render_done:
            try:
                self._status.configure(text="", fg=theme.MUTED)
            except tk.TclError:
                pass

    def _set_progress(self) -> None:
        if self._render_done:
            return
        self._status.configure(text=f"已加载 {self._render_pos} / {len(self._turns)} 轮",
                               fg=theme.MUTED)

    def _fit_title(self) -> None:
        w = self._title_lbl.winfo_width()
        if w <= 1:
            return
        txt = _fit_text(self._title_font, self._title_full, w - 8, 1)
        if txt != str(self._title_lbl.cget("text")):
            self._title_lbl.configure(text=txt)

    # ------------------------------------------------------------------ 加载
    def _open_index(self, i: int) -> None:
        if not self._refs:
            self._show_error("没有可查看的会话")
            return
        self._idx = i
        self._start_load()

    def _step_session(self, delta: int) -> None:
        j = self._idx + delta
        if 0 <= j < len(self._refs):
            self._open_index(j)

    def _reload(self) -> None:
        self._start_load(use_cache=False)

    def _start_load(self, *, use_cache: bool = True) -> None:
        ref = self._refs[self._idx]
        if self._cancel is not None:
            self._cancel.set()
        cancel = threading.Event()
        self._cancel = cancel
        self._load_gen += 1
        gen = self._load_gen
        self._state = "loading"
        self.perf = {"t0": time.perf_counter()}
        self._reset_doc_state()
        self._tr = None
        self._turns, self._entries = [], []
        self._outline.clear()
        self._title_full = ref.title or Path(ref.path).stem
        self._title_lbl.configure(text=self._title_full)
        self._fit_title()
        self._fill_info(None)
        self._set_nav_state()
        self._show_message("正在加载会话记录…", "empty_msg")
        q = self._ui_queue

        def work() -> None:
            try:
                tr = T.load_transcript(ref.source, ref.path, ref.session_id,
                                       use_cache=use_cache, cancel_event=cancel)
            except T.TranscriptCancelled:
                return
            except Exception as e:     # 损坏 / 缺文件等一律回报到界面，不崩
                err_text = str(e) or e.__class__.__name__
                q.put(lambda: self._on_load_error(gen, err_text))
                return
            q.put(lambda: self._on_loaded(gen, tr))

        threading.Thread(target=work, daemon=True, name="transcript-load").start()

    def _on_load_error(self, gen: int, err_text: str) -> None:
        if gen != self._load_gen or self._closed:
            return
        self._show_error(err_text)

    def _show_error(self, err_text: str) -> None:
        self._state = "error"
        self._error = err_text
        self._render_done = True
        self._rgen += 1
        self._show_message(f"加载失败：{err_text}", "err_msg")
        self._set_nav_state()

    def _on_loaded(self, gen: int, tr: "T.Transcript") -> None:
        if gen != self._load_gen or self._closed:
            return
        self.perf["loaded"] = time.perf_counter() - self.perf["t0"]
        self._tr = tr
        self._turns = tr.turns
        self._entries = tr.index()
        self._style = tr.agent_style
        self._state = "ready"
        ref = self._refs[self._idx]
        self._title_full = title_of(tr, ref)
        self._title_lbl.configure(text=self._title_full)
        self._fit_title()
        try:
            self._win.title(f"会话记录 · {_clip(self._title_full, 40)}")
        except tk.TclError:
            pass
        icon = source_icon(self._win, tr.source)
        self._src_lbl.configure(image=icon if icon is not None else "", text="")
        self._src_lbl.image = icon
        self._fill_info(tr)
        self._set_nav_state()
        self._win.update_idletasks()      # 先排版，气泡折行要用文字区的真实宽度
        self._apply_metrics()
        self._outline.set_entries(self._entries, self._turns, icon,
                                  self._on_outline_you, self._on_outline_ai)
        self._start_render()
        self.perf["first_batch"] = time.perf_counter() - self.perf["t0"]

    # ------------------------------------------------------------------ 页头信息
    def _chip(self, parent, text: str, icon, command=None, tip: str | None = None):
        font = tkfont.Font(font=theme.FONT_UI_SMALL)
        w = font.measure(text) + (24 if icon is not None else 0) + 20
        p = RoundedPill(parent, text=text, icon=icon, width=w, height=22,
                        radius=theme.RADIUS_M, fill=theme.CONTROL_BG, bg=theme.PANEL_2,
                        fg=theme.FG, font=theme.FONT_UI_SMALL, command=command)
        if tip:
            Tooltip(p, tip)
        return p

    def _fill_info(self, tr) -> None:
        info = self._info
        for w in info.winfo_children():
            w.destroy()
        self._reason_lbl.pack_forget()
        if tr is None:
            return

        def sep():
            tk.Label(info, text="·", bg=theme.PANEL_2, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(side="left", padx=theme.PAD_S)

        def lab(text, *, mono=False, fg=theme.MUTED, tip=None):
            lb = tk.Label(info, text=text, bg=theme.PANEL_2, fg=fg,
                          font=(theme.FONT_MONO[0], theme.FONT_UI_SMALL[1]) if mono
                          else theme.FONT_UI_SMALL)
            lb.pack(side="left")
            if tip:
                Tooltip(lb, tip)
            return lb

        first = True

        def add(fn):
            nonlocal first
            if not first:
                sep()
            first = False
            fn()

        if tr.cwd:
            short = S.shorten_path(tr.cwd)
            if len(short) > 46:
                short = short[:20] + "…" + short[-24:]
            chip = self._chip(info, short, ui_icon(info, "folder"),
                              command=lambda _p=None: self._copy_text(tr.cwd, "已复制工作目录"),
                              tip=f"{tr.cwd}\n点击复制")
            add(lambda: chip.pack(side="left"))
        rng = T.format_time_range(tr.started_ts, tr.ended_ts)
        if rng:
            add(lambda: lab(rng))
        add(lambda: lab(f"{tr.question_count} 次提问"))
        add(lambda: lab(f"{tr.tool_call_count} 次工具调用"))
        if tr.model:
            add(lambda: lab(tr.model, mono=True,
                            tip="、".join(tr.models) if len(tr.models) > 1 else None))
        if tr.total_tokens is not None:
            parts = [f"输入 {S.format_tokens(tr.input_tokens or 0)}",
                     f"输出 {S.format_tokens(tr.output_tokens or 0)}",
                     f"缓存读 {S.format_tokens(tr.cache_read_tokens or 0)}",
                     f"缓存写 {S.format_tokens(tr.cache_write_tokens or 0)}"]
            add(lambda: lab(f"{S.format_tokens(tr.total_tokens)} Tokens", tip=" · ".join(parts)))
        else:
            add(lambda: lab("Tokens -"))
        cost_tip = "按价表标价估算"
        if tr.reported_cost_usd is not None:
            cost_tip += f"\n数据源自报：{format_value('cost', tr.reported_cost_usd)}"
        add(lambda: lab(format_value("cost", tr.cost_usd), fg=theme.FG, tip=cost_tip))
        if tr.warnings:
            add(lambda: lab(f"{len(tr.warnings)} 条提示", fg=theme.WARNING,
                            tip="\n".join(str(w) for w in tr.warnings[:8])))
        # 恢复按钮
        has_cmd = bool(tr.resume_command)
        self._resume_btn.configure(state="normal" if has_cmd else "disabled",
                                   fg=theme.FG if has_cmd else theme.MUTED)
        self._resume_menu_btn.configure(state="normal" if has_cmd else "disabled")
        if not has_cmd:
            why = ("WorkBuddy 暂无恢复命令" if tr.source == "workbuddy"
                   else "此会话无法恢复（缺少会话 ID 或数据源不支持）")
            self._reason_lbl.configure(text=why)
            self._reason_lbl.pack(fill="x", padx=theme.PAD_L, pady=(0, theme.PAD_S))

    def _set_nav_state(self) -> None:
        for b, ok in ((self._prev_btn, self._idx > 0),
                      (self._next_btn, self._idx < len(self._refs) - 1)):
            b.configure(state="normal" if ok else "disabled")
        ready = self._state == "ready"
        for b in (self._export_btn, self._find_btn, self._opt_btn, self._more_btn,
                  self._expand_btn, self._collapse_btn):
            b.configure(state="normal" if ready else "disabled")
        if not ready:
            self._resume_btn.configure(state="disabled")
            self._resume_menu_btn.configure(state="disabled")

    # ------------------------------------------------------------------ 剪贴板 / 菜单
    def _copy_text(self, text: str, ok_msg: str = "已复制") -> None:
        try:
            self._win.clipboard_clear()
            self._win.clipboard_append(text)
            self._flash_status(ok_msg)
        except tk.TclError:
            self._flash_status("复制失败", error=True)

    def _copy_resume(self) -> None:
        if self._tr and self._tr.resume_command:
            self._copy_text(self._tr.resume_command, "已复制恢复命令")

    def _popup_below(self, menu: FlatMenu, widget) -> None:
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _open_resume_menu(self) -> None:
        tr = self._tr
        if tr is None or not tr.resume_command:
            return
        m = FlatMenu(self._win)
        m.add_command(label="复制恢复命令", command=self._copy_resume,
                      image=ui_icon(self._win, "copy"), compound="left")
        for label, shell in (("PowerShell", "powershell"), ("命令提示符", "cmd"),
                             ("bash / zsh", "posix")):
            cmd = T.cd_resume_command(tr.cwd, tr.resume_command, shell)
            m.add_command(
                label=f"复制「进入目录并恢复」（{label}）",
                command=(lambda c=cmd: self._copy_text(c, "已复制命令")) if cmd else None,
                state="normal" if cmd else "disabled",
                image=ui_icon(self._win, "terminal"), compound="left")
        self._popup_below(m, self._resume_menu_btn)

    def _check_label(self, on: bool, label: str) -> str:
        return ("✓  " if on else "     ") + label

    def _save_pref(self, key: str, value: bool) -> None:
        self._prefs[key] = bool(value)
        ui_prefs.save(self._prefs)

    def _open_options_menu(self) -> None:
        m = FlatMenu(self._win)
        m.add_command(label=self._check_label(self._expand_all, "展开全部过程"),
                      command=lambda: self._set_expand_all(not self._expand_all))
        m.add_command(label=self._check_label(self._show_injected, "显示注入的上下文"),
                      command=lambda: self._set_show_injected(not self._show_injected))
        self._popup_below(m, self._opt_btn)

    def _open_more_menu(self) -> None:
        tr = self._tr
        m = FlatMenu(self._win)
        ready = tr is not None
        st = "normal" if ready else "disabled"
        m.add_command(label="复制整段为 Markdown", command=self._copy_markdown, state=st,
                      image=ui_icon(self._win, "copy"), compound="left")
        m.add_command(label="导出为 Markdown 文件…", command=self._export_md, state=st,
                      image=ui_icon(self._win, "export"), compound="left")
        m.add_command(label=self._check_label(self._include_thinking, "复制 / 导出时包含思考"),
                      command=lambda: self._set_include_thinking(not self._include_thinking))
        m.add_separator()
        m.add_command(label="复制会话 ID",
                      command=lambda: self._copy_text(tr.session_id or "", "已复制会话 ID"),
                      state=st if ready and tr.session_id else "disabled",
                      image=ui_icon(self._win, "copy"), compound="left")
        m.add_command(label="复制源文件路径",
                      command=lambda: self._copy_text(tr.path, "已复制源文件路径"), state=st,
                      image=ui_icon(self._win, "copy"), compound="left")
        m.add_separator()
        m.add_command(label="重新读取", command=self._reload,
                      image=ui_icon(self._win, "refresh"), compound="left")
        self._popup_below(m, self._more_btn)

    def _set_include_thinking(self, v: bool) -> None:
        self._include_thinking = v
        self._save_pref(PREF_THINKING, v)

    def _markdown(self) -> str:
        return T.transcript_to_markdown(self._tr, include_thinking=self._include_thinking)

    def _copy_markdown(self) -> None:
        if self._tr is not None:
            self._copy_text(self._markdown(), "已复制整段 Markdown")

    def _export_md(self) -> None:
        tr = self._tr
        if tr is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self._win, defaultextension=".md",
            initialfile=T.export_filename(self._title_full, fallback=tr.session_id or "session"),
            filetypes=[("Markdown", "*.md"), ("全部文件", "*.*")], title="导出会话记录")
        if not path:
            return
        try:
            Path(path).write_text(self._markdown(), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("导出失败", str(e), parent=self._win)
            return
        self._flash_status(f"已导出：{Path(path).name}")

    # ------------------------------------------------------------------ 视图 / 选项
    def _refresh_pills(self) -> None:
        for k, p in self._pills.items():
            on = k == self._view
            p.set_state(fill=theme.ACCENT if on else theme.CONTROL_BG,
                        fg=theme.FG_WHITE if on else theme.MUTED)

    def _set_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        self._refresh_pills()
        if self._state == "ready":
            self._rerender_all()

    def _set_expand_all(self, v: bool) -> None:
        self._expand_all = v
        self._turn_ov.clear()
        if not v:
            self._step_ov.clear()
        self._save_pref(PREF_EXPAND, v)
        if self._state == "ready":
            self._rerender_all()

    def _set_show_injected(self, v: bool) -> None:
        self._show_injected = v
        self._save_pref(PREF_INJECTED, v)
        if self._state == "ready":
            self._rerender_all()

    def _toggle_outline(self) -> None:
        self._outline_on = not self._outline_on
        self._save_pref(PREF_OUTLINE, self._outline_on)
        if self._outline_on:
            self._body.add(self._outline.frame, stretch="never", minsize=200, width=OUTLINE_W)
            self._cur_turn = -1
            self._on_scrolled()
        else:
            self._body.forget(self._outline.frame)

    def _rerender_all(self) -> None:
        self._start_render(restore_turn=self._top_turn())

    # ------------------------------------------------------------------ 文档渲染
    def _reset_doc_state(self) -> None:
        self._rgen += 1
        self._render_pos = 0
        self._render_done = True
        self._any_rows = False
        self._turn_div = {}
        self._turn_row_count = {}
        self._acts = {}
        self._rows_cache = None
        self._row_total = 0
        self._cur_turn = -1
        self._hover_act = None
        self._hit_clear()
        self._search = None
        self._search_open = set()
        self._eff_cache = None
        self._turn_ov.clear()
        self._step_ov.clear()
        self._show_all.clear()
        self._open_text.clear()
        self._content.clear()
        self._content_pending.clear()
        self._content_turn.clear()
        if self._find_var.get():
            self._find_var.set("")
        self._clear_doc()

    def _unset_marks(self, turn_idx: int | None = None) -> None:
        keys = list(self._marks) if turn_idx is None else [turn_idx]
        t = self._txt
        for k in keys:
            names = self._marks.pop(k, [])
            for n in names:
                self._acts.pop(n, None)
            for i in range(0, len(names), 500):
                try:
                    t.mark_unset(*names[i:i + 500])
                except tk.TclError:
                    pass

    def _mark(self, name: str) -> None:
        t = self._txt
        t.mark_set(name, "cur")
        t.mark_gravity(name, "left")
        self._marks.setdefault(self._cur_rt, []).append(name)

    def _clear_doc(self) -> None:
        t = self._txt
        self._unset_marks()
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")
        self._latest_btn.place_forget()
        t.yview_moveto(0)

    def _show_message(self, text: str, tag: str) -> None:
        self._clear_doc()
        t = self._txt
        t.configure(state="normal")
        t.insert("1.0", text + "\n", (tag,))
        t.configure(state="disabled")

    def _start_render(self, restore_turn: int | None = None) -> None:
        self._rgen += 1
        gen = self._rgen
        self._clear_doc()
        self._render_pos = 0
        self._render_done = False
        self._any_rows = False
        self._turn_div = {}
        self._turn_row_count = {}
        self._acts = {}
        self._rows_cache = None
        self._row_total = 0
        self._restore_turn = restore_turn
        self._render_bpx = self._bubble_px()
        tr = self._tr
        if tr is not None and tr.warnings:
            t = self._txt
            t.configure(state="normal")
            for w in tr.warnings[:5]:
                t.insert("end", f"提示：{_clip(str(w), 200)}\n", ("warn_msg",))
            t.configure(state="disabled")
        self._render_chunk(gen)

    def _render_chunk(self, gen: int, until: int | None = None) -> None:
        if gen != self._rgen or self._closed or self._render_done:
            return
        t = self._txt
        t.configure(state="normal")
        t.mark_set("cur", "end-1c")
        t.mark_gravity("cur", "right")
        start = t.index("cur")
        self._chars = 0
        turns = self._turns
        t_start = time.perf_counter()
        try:
            self._render_loop(turns, until, t_start)
        finally:
            dt = time.perf_counter() - t_start
            if until is None and dt > self.perf.get("max_chunk", 0.0):
                self.perf["max_chunk"] = dt
        self._highlight(start, "cur")
        t.configure(state="disabled")
        self._after_chunk(gen, until)

    def _render_loop(self, turns, until, t_start) -> None:
        while self._render_pos < len(turns):
            turn = turns[self._render_pos]
            self._render_pos += 1
            self._render_turn(turn)
            if until is None and (self._chars >= BATCH_CHARS
                                  or time.perf_counter() - t_start > BATCH_SECONDS):
                break
            if until is not None and self._render_pos > until:
                break

    def _after_chunk(self, gen: int, until) -> None:
        turns = self._turns
        if self._render_pos >= len(turns):
            self._finish_render()
        else:
            self._set_progress()
            if until is None:
                self._win.after(1, lambda: self._render_chunk(gen))
        if "first_paint" not in self.perf:
            self.perf["first_paint"] = time.perf_counter() - self.perf.get("t0", 0)
        self._on_scrolled()

    def _finish_render(self) -> None:
        self._render_done = True
        self.perf["rendered"] = time.perf_counter() - self.perf.get("t0", 0)
        self._status.configure(text="", fg=theme.MUTED)
        t = self._txt
        if not self._any_rows:
            t.configure(state="normal")
            if self._view == T.VIEW_CHANGES:
                msg = "这个会话没有文件改动"
            else:
                msg = "这个会话没有可显示的消息"
            t.insert("end-1c", msg + "\n", ("empty_msg",))
            t.configure(state="disabled")
        rt = self._restore_turn
        self._restore_turn = None
        if rt is not None and self._any_rows:
            self._goto_turn(rt, flash=False)
        if self._search is not None and self._search.total and rt is None:
            self._goto_hit(self._hit_n)

    def _ensure_rendered(self, turn_idx: int) -> None:
        """同步把渲染推进到包含 turn_idx 的那一轮（查找 / 目录跳转用）。"""
        guard = 0
        while not self._render_done and self._render_pos <= turn_idx and guard < 10000:
            guard += 1
            self._render_chunk(self._rgen, until=turn_idx)

    def _eff_search(self):
        """送进 flatten 的查找集合：命中的轮不多（≤60）时全量展开；很多时只展开
        ``_search_open`` 里的轮（当前命中所在轮），避免一搜常见词就把整段会话展开。"""
        h = self._search
        if h is None:
            return None
        keys = self._hit_keys(h)
        if len(keys) <= SEARCH_EXPAND_MAX:
            return h
        sig = (id(h), frozenset(self._search_open))
        if self._eff_cache is not None and self._eff_cache[0] == sig:
            return self._eff_cache[1]
        op = self._search_open
        eff = T.SearchHits(query=h.query, total=h.total, matches=h.matches)
        eff.turn_keys = h.turn_keys & op
        eff.question_keys = h.question_keys & op
        eff.final_keys = h.final_keys & op
        ids = set()
        for m in h.matches:
            if m.target == "step" and self._turns[m.turn].key in op:
                ids.add(m.target_id)
        for turn in self._turns:
            if turn.key in op:
                for st in turn.steps:
                    if st.kind == "merged" and any(c.id in ids for c in st.children):
                        ids.add(st.id)
        eff.step_ids = ids
        self._eff_cache = (sig, eff)
        return eff

    def _turn_rows(self, turn) -> list:
        return T.flatten_rows([turn], self._view, self._expand_all, turn_overrides=self._turn_ov,
                              step_overrides=self._step_ov, search=self._eff_search(),
                              show_injected=self._show_injected)

    def _rows(self) -> list:
        if self._rows_cache is None:
            self._rows_cache = T.flatten_rows(
                self._turns, self._view, self._expand_all, turn_overrides=self._turn_ov,
                step_overrides=self._step_ov, search=self._eff_search(),
                show_injected=self._show_injected)
        return self._rows_cache

    def _ins(self, text: str, *tags) -> None:
        self._txt.insert("cur", text, tags + self._ctx)
        self._chars += len(text)

    def _render_turn(self, turn, divider: bool | None = None) -> None:
        rows = self._turn_rows(turn)
        old_n = self._turn_row_count.get(turn.index, 0)
        self._row_total += len(rows) - old_n
        self._turn_row_count[turn.index] = len(rows)
        if not rows:
            return
        tn = f"tn:{turn.index}"
        self._cur_rt = turn.index
        div = self._any_rows if divider is None else divider
        self._turn_div[turn.index] = div
        self._any_rows = True
        self._ctx = (tn,)
        if div:
            self._ins(" " * DIVIDER_SPACES + "\n", "divider")
        head_done = False
        eff = self._eff_search()
        step_hits = eff.step_ids if eff is not None else ()
        hidden_fail = 0
        for ri, row in enumerate(rows):
            k = row.kind
            if k != "step" or row.step.id in step_hits:
                self._mark(f"rw:{row.key}")
            if k in ("timeline", "step", "final", "injected") and not head_done:
                head_done = True
                self._mark(f"ah:{turn.key}")
                self._render_ai_head(turn, tn)
            if k == "question":
                self._render_question(turn, row)
            elif k == "timeline":
                self._render_timeline(turn, row)
                hidden_fail = 0 if row.expanded else row.hidden_failures
            elif k == "step":
                self._render_step(turn, row)
                nxt = rows[ri + 1] if ri + 1 < len(rows) else None
                if hidden_fail and row.pinned and not (
                        nxt is not None and nxt.kind == "step" and nxt.pinned):
                    self._render_more_failures(turn, hidden_fail)
            elif k == "final":
                self._render_final(turn, row)
            elif k == "event":
                self._render_event(row)
            elif k == "injected":
                self._render_injected(row)
        self._ctx = ()

    # -- 各类行 ------------------------------------------------------------------
    def _render_ai_head(self, turn, tn: str) -> None:
        tr = self._tr
        icon = source_icon(self._win, tr.source)
        ts = None
        if turn.steps:
            mi = turn.steps[0].message_index if hasattr(turn.steps[0], "message_index") else None
            if mi is not None and 0 <= mi < len(tr.messages):
                ts = tr.messages[mi].ts
        if ts is None and turn.final is not None:
            ts = turn.final.ts
        if icon is not None:
            self._txt.image_create("cur", image=icon, padx=0)
            for tg in (tn, "ai_head"):
                self._txt.tag_add(tg, "cur-1c", "cur")
        name = T._APP_NAMES.get(tr.source, "助手")
        self._ins(("  " if icon is not None else "") + name, "ai_head", "ai_name")
        tm = T.format_message_time(ts)
        if tm:
            self._ins("  " + tm, "ai_head", "ai_meta")
        model = turn.final.model if turn.final and turn.final.model else None
        if model:
            self._ins("  ·  ", "ai_head", "ai_meta")
            self._ins(model, "ai_head", "ai_model")
        self._ins("\n", "ai_head")

    def _msg_text_of(self, mi: int) -> str:
        m = self._tr.messages[mi]
        return T.message_text(m)

    def _render_question(self, turn, row) -> None:
        q = turn.question
        tm = T.format_message_time(q.ts)
        self._ins(("你  " + tm) if tm else "你", "q_meta")
        self._ins("\n", "q_meta")
        text = q.text.strip("\n")
        key = f"q:{turn.key}"
        folded = False
        if len(text) > QUESTION_FOLD_CHARS and not (row.force_expanded or key in self._open_text):
            text, folded = text[:QUESTION_FOLD_CHARS], True
        sw = self._space_w
        lines = wrap_text(self._bfont, text or " ", self._render_bpx - 2 * sw)
        body_w = max(self._bfont.measure(ln) for ln in lines) + 2 * sw      # 气泡实心矩形宽
        pad_n = max(1, round(body_w / max(1, self._pad_font.measure(" "))))
        self._bubble_line(" " * pad_n, "q_pad")
        for ln in lines:
            body = " " + ln + " "
            body += " " * max(0, round((body_w - self._bfont.measure(body)) / sw))
            self._bubble_line(body, "q_bub")
        self._bubble_line(" " * pad_n, "q_pad")
        if folded:
            n = len(q.text)
            self._act(f"展开全文（共 {n} 字）", ("q_more",), lambda k=key, ti=turn.index:
                      self._toggle_text(k, ti), trailing="\n", trailing_tags=("q_more",))
        for i, im in enumerate(q.images):
            self._ins(f"[{im.alt or S.t('image.alt', index=i + 1)}]\n", "q_meta")

    def _bubble_line(self, body: str, tag: str) -> None:
        """一行气泡：无底色前导空格 + 带底色 body + 无底色换行（见 ``q_line`` 的说明）。"""
        self._ins(" ", "q_line")
        self._ins(body, "q_line", tag)
        self._ins("\n", "q_line")

    def _toggle_text(self, key: str, turn_idx: int) -> None:
        if key in self._open_text:
            self._open_text.discard(key)
        else:
            self._open_text.add(key)
        self._rerender_turn(turn_idx)

    def _render_timeline(self, turn, row) -> None:
        # 「还有 N 个失败」不进摘要行（与「F 个失败」重复），改在钉出的失败行之后单列
        text = S.format_turn_summary(row.summary, no_final=row.no_final, aborted=row.aborted)
        err = (S.t("summaryErrors", count=row.summary.error_count)
               if row.summary.error_count else "")

        def draw() -> None:
            self._ins("▾ " if row.expanded else "▸ ", "tl_head")
            if err and err in text:
                a, b = text.split(err, 1)
                self._ins(a, "tl_head")
                self._ins(err, "tl_head", "tl_err")
                self._ins(b + "\n", "tl_head")
            else:
                self._ins(text + "\n", "tl_head")

        self._act_block(draw, lambda k=turn.key, ex=row.expanded, ti=turn.index:
                        self._toggle_turn(k, ex, ti))

    def _render_more_failures(self, turn, n: int) -> None:
        self._act(f"… 还有 {n} 个失败，展开执行过程查看", ("tl_more",),
                  lambda k=turn.key, ti=turn.index: self._toggle_turn(k, False, ti),
                  trailing="\n", trailing_tags=("tl_more",))

    def _toggle_turn(self, key: str, expanded: bool, turn_idx: int) -> None:
        self._turn_ov[key] = not expanded
        self._rerender_turn(turn_idx)

    def _step_parts(self, step):
        """→ (glyph, tone, title, detail, meta_text, mono, expandable)"""
        st = self._style
        project_dir = self._tr.cwd if self._tr else None
        if step.kind == "merged":
            mt = S.format_merged_title(step)
            meta = S.format_step_meta(step, st)
            return (meta.glyph, meta.tone, mt.verb, mt.subtitle, S.format_step_meta_text(meta),
                    st.mono_titles, True)
        if step.kind == "tool":
            ti = S.format_step_title(step, st, project_dir)
            meta = S.format_step_meta(step, st)
            return (meta.glyph, meta.tone, ti.text, ti.detail or "",
                    S.format_step_meta_text(meta), st.mono_titles, True)
        if step.kind == "thinking":
            b = step.block
            label = S.t("thinkingRedacted") if b.redacted else st.thinking_label
            detail = "" if b.redacted else _clip((b.summary or b.text or "").strip(), 80)
            return (st.thinking_glyph or "•", "muted", label, detail, "", False,
                    bool(b.text or b.summary))
        if step.kind == "note":
            first = MD.plain_inline(step.text.strip().split("\n", 1)[0])
            return ("•", "muted", _clip(first, 110), "", "", False,
                    "\n" in step.text.strip() or len(first) > 110)
        if step.kind == "event":
            d = S.describe_event(step.block, step.body)
            return ("·", "danger" if d.tone == "danger" else "muted", d.label,
                    _clip(d.detail or "", 80), "", False, bool(d.detail))
        return ("•", "muted", "[图片]", "", "", False, False)

    def _render_step(self, turn, row) -> None:
        step = row.step
        glyph, tone, title, detail, meta, mono, expandable = self._step_parts(step)
        depth = 1 if row.depth else 0
        font_tag = "step_mono" if mono else "step_ui"
        base = (f"step_d{depth}",)
        tone_tag = f"tone_{tone}"

        def line() -> None:
            if expandable:
                self._ins("▾ " if row.expanded else "▸ ", *base, "chev")
            self._ins(glyph + " ", *base, tone_tag)
            self._ins(_clip(title, 120), *base, font_tag)
            if detail:
                self._ins("  " + _clip(detail, 90), *base, "step_detail")
            if meta:
                self._ins("\t" + meta, *base, "step_meta", tone_tag
                          if tone in ("danger", "warning") else "tone_muted")
            self._ins("\n", *base)

        if expandable:
            self._act_block(line, lambda sid=step.id, ex=row.expanded, ti=turn.index:
                            self._toggle_step(sid, ex, ti))
        else:
            line()
        if row.expanded and step.kind != "merged":
            self._render_step_detail(turn, row)

    def _toggle_step(self, sid: str, expanded: bool, turn_idx: int) -> None:
        self._step_ov[sid] = not expanded
        self._rerender_turn(turn_idx)

    # -- 步骤详情 ----------------------------------------------------------------
    def _dt_lines(self, lines, tag: str) -> None:
        for ln in lines:
            self._ins(ln + "\n", tag)

    def _render_step_detail(self, turn, row) -> None:
        step = row.step
        if step.kind == "note":
            self._md(step.text, note=True)
            return
        if step.kind == "thinking":
            b = step.block
            text = (b.summary + "\n" if b.summary else "") + (b.text or "")
            self._dt_lines(text.strip("\n").split("\n")[:60], "dt_text")
            return
        if step.kind == "event":
            d = S.describe_event(step.block, step.body)
            if d.detail:
                self._dt_lines(d.detail.split("\n")[:20], "dt_text")
            return
        if step.kind != "tool":
            return
        call, res = step.call, step.result
        failure = S.is_failure_step(step)
        if call is not None:
            if call.kind == S.KIND_SHELL:
                cmd = self._input_field(call, "command")
                if cmd and cmd.strip() != call.title.strip():
                    self._ins("命令\n", "dt_label")
                    self._code_lines(("$ " + cmd).split("\n"), "dt_code")
            if call.diff or call.kind in (S.KIND_EDIT, S.KIND_WRITE):
                self._render_diff(turn, call)
            elif call.kind in (S.KIND_MCP, S.KIND_OTHER, S.KIND_AGENT, S.KIND_WEB, S.KIND_ASK,
                               S.KIND_TODO) or (res is None and call.kind not in
                                                 (S.KIND_SHELL, S.KIND_READ, S.KIND_SEARCH)):
                args = self._pretty_args(call)
                if args:
                    self._ins("参数\n", "dt_label")
                    self._code_lines(args.split("\n"), "dt_code")
        if res is not None:
            self._render_output(turn, row, step, res)
        if step.result is not None and step.result.images:
            for im in step.result.images:
                kb = max(1, (im.size or 0) // 1024)
                self._ins(f"[图片 · {im.media_type} · {kb}KB]\n", "dt_label")

    def _code_lines(self, lines, tag: str) -> None:
        extra = 0
        if len(lines) > CODE_MAX_LINES:
            extra, lines = len(lines) - CODE_MAX_LINES, lines[:CODE_MAX_LINES]
        for ln in lines:
            self._ins(" " + (ln or " ") + " \n", tag)
        if extra:
            self._ins(f"… 还有 {extra} 行\n", "dt_label")

    def _input_field(self, call, key: str):
        if len(call.input_preview) < call.input_total_len:
            return None
        try:
            obj = json.loads(call.input_preview)
        except ValueError:
            return None
        v = obj.get(key) if isinstance(obj, dict) else None
        return v if isinstance(v, str) else None

    def _pretty_args(self, call) -> str:
        pv = call.input_preview or ""
        if not pv:
            return ""
        try:
            return json.dumps(json.loads(pv), ensure_ascii=False, indent=2)
        except ValueError:
            return pv + ("\n…（参数已截断）" if len(pv) < call.input_total_len else "")

    # -- 懒取全文 ----------------------------------------------------------------
    def _load_ref(self, key, turn_idx: int, fn, fast: bool):
        """取被截断的内容：快路径同步，其余放 worker。返回 str / None / _PENDING。"""
        if key in self._content:
            return self._content[key]
        if fast:
            try:
                val = fn()
            except Exception:
                val = None
            self._content[key] = val
            return val
        if key in self._content_pending:
            return _PENDING
        self._content_pending.add(key)
        self._content_turn[key] = turn_idx
        gen, q = self._load_gen, self._ui_queue

        def work() -> None:
            try:
                val = fn()
            except Exception:
                val = None
            q.put(lambda: self._on_content(gen, key, val))

        threading.Thread(target=work, daemon=True, name="transcript-content").start()
        return _PENDING

    def _on_content(self, gen: int, key, val) -> None:
        self._content_pending.discard(key)
        if gen != self._load_gen or self._closed:
            return
        self._content[key] = val
        ti = self._content_turn.get(key)
        if ti is not None:
            self._rerender_turn(ti)

    def _diff_text(self, turn, call):
        d = call.diff or {}
        ref = d.get("full")
        key = ("diff", call.id)
        if ref is not None:
            return self._load_ref(key, turn.index, lambda: T.read_content(ref, PAGE_CHARS * 4),
                                  ref.kind in _FAST_REFS)
        txt = T.build_input_diff(call, load_full=False)
        if txt:
            return txt
        if call.input_full is not None:
            return self._load_ref(key, turn.index, lambda: T.build_input_diff(call, True),
                                  call.input_full.kind in _FAST_REFS)
        return None

    def _render_diff(self, turn, call) -> None:
        text = self._diff_text(turn, call)
        if text is _PENDING:
            self._ins("正在读取改动…\n", "dt_label")
            return
        files = (call.diff or {}).get("files") or []
        if not text:
            return
        lines = T.parse_diff(text)
        if not lines:
            return
        # 每个文件的 +a −r（多文件先列清单）
        sums, cur = {}, None
        for ln in lines:
            if ln.type == "file":
                cur = ln.path
                sums.setdefault(cur, [0, 0])
            elif cur is not None and ln.type == "add":
                sums[cur][0] += 1
            elif cur is not None and ln.type == "del":
                sums[cur][1] += 1
        if len(files) > 1 or len(sums) > 1:
            self._ins("改动\n", "dt_label")
            for p, (a, r) in sums.items():
                self._ins(f"└ {S.shorten_path(p, self._tr.cwd)} (+{a} −{r})\n", "dt_file")
        if files and not any(ln.type == "file" for ln in lines):
            self._ins(S.shorten_path(files[0]["path"], self._tr.cwd) + "\n", "dt_file")
        shown, extra = 0, 0
        for ln in lines:
            if shown >= DIFF_MAX_LINES and ln.type in ("add", "del", "context"):
                extra += 1
                continue
            if ln.type == "file":
                self._ins(S.shorten_path(ln.path, self._tr.cwd) + "\n", "dt_file")
            elif ln.type == "gap":
                self._ins("⋯\n", "dt_gap")
            else:
                shown += 1
                on = "" if ln.old_no is None else str(ln.old_no)
                nn = "" if ln.new_no is None else str(ln.new_no)
                tag = {"add": "dt_add", "del": "dt_del"}.get(ln.type, "dt_ctx")
                self._ins(f"{on:>4} {nn:>4} ", tag, "dt_ln")
                sign = {"add": "+", "del": "-"}.get(ln.type, " ")
                self._ins(sign, tag, {"add": "dt_sign_add", "del": "dt_sign_del"}.get(ln.type, "dt_ln"))
                self._ins(" " + ln.text + "\n", tag)
        if extra:
            self._ins(f"… 还有 {extra} 行改动未显示\n", "dt_label")

    def _render_output(self, turn, row, step, res) -> None:
        key = ("out", step.id)
        full = self._content.get(key)
        pending = key in self._content_pending
        have_full = isinstance(full, str)
        text = full if have_full else (res.preview or "")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        limit = 10 ** 9 if step.id in self._show_all else row.preview_lines
        shown = lines[:limit]
        conn = (self._style.result_connector or "").strip()
        for i, ln in enumerate(shown):
            lead = (conn + " " if i == 0 else "  ") if conn else ""
            self._ins(lead, "dt_out", "dt_conn")
            self._ins(_clip_line(ln) + "\n", "dt_out")
        total = len(lines) if have_full else max(res.line_count, len(lines))
        more = total - len(shown)
        if pending:
            self._ins("正在读取完整输出…\n", "dt_label")
        elif more > 0:
            can = res.full is not None or len(lines) > len(shown)
            if can:
                self._act(f"… 还有 {more} 行，显示全部", ("dt_more",),
                          lambda s=step, r=res, ti=turn.index: self._show_all_output(s, r, ti),
                          trailing="\n", trailing_tags=("dt_more",))
            else:
                self._ins(f"… 还有 {more} 行（源文件未保留全文）\n", "dt_label")
        if have_full and len(full) >= PAGE_CHARS:
            self._ins("仅显示前 512K 字符；完整内容见源文件（更多 → 复制源文件路径）\n", "dt_label")
        if res.saved_path:
            self._ins(f"完整输出：{res.saved_path}\n", "dt_label")

    def _show_all_output(self, step, res, turn_idx: int) -> None:
        self._show_all.add(step.id)
        if res.full is not None and res.truncated:
            self._load_ref(("out", step.id), turn_idx, lambda: T.read_content(res.full, PAGE_CHARS),
                           res.full.kind in _FAST_REFS)
        self._rerender_turn(turn_idx)

    def _render_final(self, turn, row) -> None:
        if turn.steps and self._view == T.VIEW_ALL:
            self._ins("\n", "md_gap")      # 与上方执行过程拉开间距
        self._md(turn.final.text)

    def _render_event(self, row) -> None:
        d = S.describe_event(row.block, row.body)
        tags = ("ev_line", "ev_danger") if d.tone == "danger" else ("ev_line",)
        self._ins(f"──  {d.label}  ──\n", *tags)
        if d.detail:
            self._ins(_clip(d.detail, 400) + "\n", "ev_detail")

    def _render_injected(self, row) -> None:
        self._ins("注入的上下文\n", "dt_label")
        text = self._msg_text_of(row.message_index).strip()
        self._ins(_clip_block(text, 600) + "\n", "inj")

    # -- Markdown ----------------------------------------------------------------
    def _md(self, text: str, note: bool = False) -> None:
        sfx = "_n" if note else ""
        for b in MD.parse_blocks(text):
            k = b[0]
            if k == "code":
                lang, body = b[1], b[2]
                if lang:
                    self._ins(lang + "\n", f"md_lang{sfx}")
                self._code_lines(body.split("\n"), f"md_codeblock{sfx}")
                self._ins("\n", "md_gap")
            elif k == "h":
                self._md_inline(b[2], f"md_h{b[1]}{sfx}")
            elif k == "li":
                tag = f"md_li{b[1]}{sfx}"
                self._ins(b[2] + " ", tag)
                self._md_inline(b[3], tag)
            elif k == "quote":
                self._ins("▎ ", f"md_quote{sfx}")
                self._md_inline(b[1], f"md_quote{sfx}")
            elif k == "hr":
                self._ins("─" * 24 + "\n", f"md_hr{sfx}")
            elif k == "table":
                self._table(b[1], sfx)
                self._ins("\n", "md_gap")
            else:
                self._md_inline(b[1], f"md_p{sfx}")

    def _table(self, rows, sfx: str) -> None:
        """表格：按像素量宽排列宽 + 每表专属 tab stop 精确对齐（CJK 回退字体的字宽
        不是等宽 2 倍，按字符数对齐会错位）；单元格折行不截断，整表不软换行。"""
        base = DETAIL_IND if sfx else IND
        x0 = base + theme.PAD_S
        gap = theme.PAD_L * 2
        avail = max(200, self._cw - x0 - theme.PAD_M)
        widths, lines = MD.layout_table(rows, avail, measure=self._tbl_font_b.measure, gap=gap)
        stops, x = [], x0
        for w in widths[:-1]:
            x += w + gap
            stops.append(x)
        self._tbl_seq += 1
        tt = f"tbl{self._tbl_seq}"
        self._txt.tag_configure(tt, lmargin1=x0, lmargin2=x0, tabs=tuple(stops) or "",
                                wrap="none")
        rule_n = max(1, (sum(widths) + gap * (len(widths) - 1)) // self._rule_w)
        for kind, cells in lines:
            if kind == "sep":
                self._ins(" " * rule_n + "\n", tt, "md_trule")
            else:
                self._ins("\t".join(cells) + "\n", tt, "md_th" if kind == "head" else "md_td")

    def _md_inline(self, text: str, line_tag: str) -> None:
        for seg, flags in MD.inline_segments(text):
            self._ins(seg, line_tag, *flags)
        self._ins("\n", line_tag)

    # -- 可点击片段 --------------------------------------------------------------
    def _new_act(self, fn) -> str:
        """登记一个可点击片段：在 cur 处放 ``a:<n>`` mark（左重力，停在片段起点）。"""
        self._act_seq += 1
        name = f"a:{self._act_seq}"
        self._acts[name] = fn
        self._mark(name)
        return name

    def _act(self, text: str, tags: tuple, fn, *, trailing: str = "", trailing_tags: tuple = ()) -> None:
        self._new_act(fn)
        self._ins(text, *tags, "act")
        if trailing:
            self._ins(trailing, *trailing_tags, "act")

    def _act_block(self, draw, fn) -> None:
        """draw() 画若干 _ins；之前放 mark、之后给整段补上 act tag。"""
        t = self._txt
        start = t.index("cur")
        self._new_act(fn)
        draw()
        t.tag_add("act", start, "cur")

    def _act_name_at(self, idx: str) -> str | None:
        """点击处所属的 act：``act`` tag 内，向前找最近的 ``a:`` mark。"""
        t = self._txt
        if "act" not in t.tag_names(idx):
            return None
        m = t.mark_previous(f"{idx}+1c")
        for _ in range(40):
            if not m:
                return None
            if m.startswith("a:"):
                return m
            m = t.mark_previous(m)
        return None

    def _on_act_click(self, e) -> None:
        t = self._txt
        if t.tag_ranges("sel"):
            return
        name = self._act_name_at(f"@{e.x},{e.y}")
        fn = self._acts.get(name) if name else None
        if fn:
            fn()

    def _on_motion(self, e) -> None:
        t = self._txt
        name = self._act_name_at(f"@{e.x},{e.y}")
        if name == self._hover_act:
            return
        t.tag_remove("row_hover", "1.0", "end")
        self._hover_act = name
        if name:
            try:
                start = t.index(name)
                t.tag_add("row_hover", f"{start} linestart", f"{start} lineend+1c")
            except tk.TclError:
                pass

    # -- 重绘 --------------------------------------------------------------------
    def _rerender_turn(self, turn_idx: int) -> None:
        if self._closed or self._state != "ready":
            return
        if turn_idx >= self._render_pos:
            return          # 尚未渲染到，批渲染时会带上最新状态
        self._rows_cache = None
        t = self._txt
        tn = f"tn:{turn_idx}"
        rng = t.tag_ranges(tn)
        turn = self._turns[turn_idx]
        t.configure(state="normal")
        self._unset_marks(turn_idx)
        try:
            if rng:
                start, end = str(rng[0]), str(rng[-1])
                t.delete(start, end)
            else:
                # 本轮此前没有行（现在可能有了）：插到下一已渲染轮之前
                start = None
                for j in range(turn_idx + 1, self._render_pos):
                    r2 = t.tag_ranges(f"tn:{j}")
                    if r2:
                        start = str(r2[0])
                        break
                if start is None:
                    start = "end-1c"
            t.mark_set("cur", start)
            t.mark_gravity("cur", "right")
            self._chars = 0
            if rng:
                div = self._turn_div.get(turn_idx, False)
            else:
                div = any(self._turn_row_count.get(j) for j in range(turn_idx))
            self._render_turn(turn, div)
            self._highlight(start, "cur")
        finally:
            t.configure(state="disabled")
        self._after_edit()

    def _after_edit(self) -> None:
        self._on_scrolled()

    # ------------------------------------------------------------------ 滚动 / 目录联动
    def _on_yscroll(self, sb, first, last) -> None:
        sb.set(first, last)
        if self._closed:
            return
        self._update_latest_btn(float(last))
        if self._scroll_after is None:
            self._scroll_after = self._win.after(80, self._on_scrolled)

    def _update_latest_btn(self, last: float | None = None) -> None:
        if last is None:
            last = self._txt.yview()[1]
        show = self._row_total >= 8 and last < 0.999 and self._state == "ready"
        if show:
            self._latest_btn.place(relx=1.0, rely=1.0, x=-theme.PAD_L * 2, y=-theme.PAD_L * 2,
                                   anchor="se")
        else:
            self._latest_btn.place_forget()

    def _scroll_latest(self) -> None:
        while not self._render_done:
            self._ensure_rendered(len(self._turns))
        self._txt.yview_moveto(1.0)
        self._latest_btn.place_forget()
        self._on_scrolled()

    def _top_turn(self) -> int | None:
        t = self._txt
        try:
            if t.yview()[1] >= 0.999 and self._render_done and self._row_total:
                for n in range(len(self._turns) - 1, -1, -1):
                    if self._turn_row_count.get(n):
                        return n
            for name in t.tag_names(t.index("@0,0")):
                if name.startswith("tn:"):
                    return int(name[3:])
        except (tk.TclError, ValueError):
            pass
        return None

    def _on_scrolled(self) -> None:
        self._scroll_after = None
        if self._closed or self._state != "ready" or not self._outline_on:
            return
        n = self._top_turn()
        if n is None or n == self._cur_turn or not self._entries:
            return
        self._cur_turn = n
        keys = [e.turn_index for e in self._entries]
        i = bisect.bisect_right(keys, n) - 1
        self._outline.set_current(max(0, i))

    def _goto_turn(self, turn_idx: int, *, flash: bool = True) -> bool:
        """把第 turn_idx 轮（无行时取其后第一个有行的轮）滚到顶部。"""
        n, total = turn_idx, len(self._turns)
        while n < total:
            self._ensure_rendered(n)
            if self._turn_row_count.get(n):
                break
            n += 1
        else:
            return False
        rng = self._txt.tag_ranges(f"tn:{n}")
        if not rng:
            return False
        self._txt.yview(str(rng[0]))
        if flash:
            self._flash_range(str(rng[0]), str(rng[-1]))
        self._cur_turn = -1
        self._on_scrolled()
        return True

    def _flash_range(self, a: str, b: str) -> None:
        t = self._txt
        t.tag_remove("flash", "1.0", "end")
        t.tag_add("flash", a, b)
        if self._flash_after is not None:
            try:
                self._win.after_cancel(self._flash_after)
            except tk.TclError:
                pass
        self._flash_after = self._win.after(FLASH_MS, lambda: self._clear_flash())

    def _clear_flash(self) -> None:
        self._flash_after = None
        try:
            self._txt.tag_remove("flash", "1.0", "end")
        except tk.TclError:
            pass

    def _on_outline_you(self, ent) -> None:
        turn = self._turns[ent.turn_index]
        self._ensure_rendered(ent.turn_index)
        self._scroll_to_mark(f"rw:{turn.key}:question", fallback_turn=ent.turn_index)

    def _on_outline_ai(self, ent) -> None:
        turn = self._turns[ent.turn_index]
        self._ensure_rendered(ent.turn_index)
        t = self._txt
        for name in (f"rw:{turn.key}:final", f"ah:{turn.key}"):
            if name in t.mark_names():
                self._scroll_to_mark(name, fallback_turn=ent.turn_index)
                return
        self._goto_turn(ent.turn_index)

    def _scroll_to_mark(self, mark: str, *, fallback_turn: int) -> None:
        t = self._txt
        if mark not in t.mark_names():
            self._goto_turn(fallback_turn)
            return
        idx = t.index(mark)
        t.yview(idx)
        self._flash_range(f"{idx} linestart", f"{idx} lineend+1c")
        self._cur_turn = -1
        self._on_scrolled()

    def _jump_turn(self, delta: int, event=None):
        w = self._win.focus_get()
        if isinstance(w, (tk.Entry, ttk.Entry)):
            return None
        if self._state != "ready" or not self._entries:
            return "break"
        cur = self._top_turn()
        cur = 0 if cur is None else cur
        keys = [e.turn_index for e in self._entries]
        if delta > 0:
            j = bisect.bisect_right(keys, cur)
            if j >= len(keys):
                return "break"
        else:
            pos = bisect.bisect_right(keys, cur) - 1
            if pos < 0:
                return "break"
            j = pos
            if keys[pos] == cur:
                rng = self._txt.tag_ranges(f"tn:{cur}")
                # 视口顶部距本轮起点 ≤ 1 行（分轮线）就算「已在本轮开头」，再按 k 才去上一轮
                if rng and (int(self._txt.index("@0,0").split(".")[0])
                            - int(str(rng[0]).split(".")[0])) <= 1:
                    j = pos - 1
            if j < 0:
                return "break"
        self._goto_turn(keys[j])
        return "break"

    # ------------------------------------------------------------------ 查找
    def _on_ctrl_f(self, _e=None):
        self._open_find()
        return "break"

    def _toggle_find(self) -> None:
        if self._findbar.winfo_ismapped():
            self._close_find()
        else:
            self._open_find()

    def _open_find(self) -> None:
        if self._state != "ready":
            return
        if not self._findbar.winfo_ismapped():
            self._findbar.pack(fill="x", before=self._body)
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")

    def _close_find(self) -> None:
        self._findbar.pack_forget()
        self._find_var.set("")
        self._apply_query("")
        self._txt.focus_set()

    def _schedule_find(self) -> None:
        if self._find_after is not None:
            try:
                self._win.after_cancel(self._find_after)
            except tk.TclError:
                pass
        self._find_after = self._win.after(150, lambda: self._apply_query(self._find_var.get()))

    def _hit_clear(self) -> None:
        self._query = ""
        self._hit_n = 0

    @staticmethod
    def _hit_keys(h) -> set:
        return set() if h is None else (h.turn_keys | h.question_keys | h.final_keys)

    def _apply_query(self, query: str) -> None:
        self._find_after = None
        if self._state != "ready":
            return
        q = query.strip()
        old_eff = self._eff_search()
        new = T.find_search_hits(self._turns, q) if q else None
        self._query = q
        self._search = new
        self._search_open = set()
        if new is not None and new.matches and len(self._hit_keys(new)) > SEARCH_EXPAND_MAX:
            self._search_open.add(self._turns[new.matches[0].turn].key)
        self._eff_cache = None
        self._rows_cache = None
        self._hit_n = 0
        self._txt.tag_remove("find_hit", "1.0", "end")
        self._txt.tag_remove("find_cur", "1.0", "end")
        keys = self._hit_keys(old_eff) | self._hit_keys(self._eff_search())
        # 步骤展开集合可能变化（同一轮内命中步骤不同）→ 两边的命中轮都要重绘
        affected = [t.index for t in self._turns if t.key in keys and t.index < self._render_pos]
        if len(affected) > SEARCH_EXPAND_MAX:
            self._start_render()
        else:
            for ti in affected:
                self._rerender_turn(ti)
            self._txt.configure(state="normal")
            self._highlight("1.0", "end")
            self._txt.configure(state="disabled")
        self._update_find_count()
        if new is not None and new.total:
            self._goto_hit(0)

    def _highlight(self, start, end) -> None:
        q = self._query
        if not q:
            return
        t = self._txt
        idx, n = start, 0
        while n < 5000:
            pos = t.search(q, idx, stopindex=end, nocase=True, count=self._cnt_var)
            if not pos:
                break
            c = max(1, self._cnt_var.get())
            e = f"{pos}+{c}c"
            t.tag_add("find_hit", pos, e)
            idx, n = e, n + 1

    def _update_find_count(self) -> None:
        h = self._search
        if not self._query:
            txt = ""
        elif h is None or not h.total:
            txt = "无结果"
        else:
            txt = f"{self._hit_n + 1}/{h.total}"
        self._find_count.configure(text=txt, fg=theme.ERROR if txt == "无结果" else theme.MUTED)

    def _find_step(self, delta: int) -> None:
        h = self._search
        if h is None or not h.total:
            return
        self._goto_hit((self._hit_n + delta) % h.total)

    def _goto_hit(self, n: int) -> None:
        h = self._search
        if h is None or not h.total:
            return
        n %= h.total
        self._hit_n = n
        self._update_find_count()
        m = h.match_at(n)
        if m is None:
            return
        before = 0
        for mm in h.matches:
            if mm is m:
                break
            before += mm.count
        k = n - before
        key = self._turns[m.turn].key
        if (len(self._hit_keys(h)) > SEARCH_EXPAND_MAX and key not in self._search_open
                and m.target in ("step", "question", "final")):
            self._search_open.add(key)
            self._eff_cache = None
            self._rows_cache = None
            self._ensure_rendered(m.turn)
            self._rerender_turn(m.turn)
        rows = self._rows()
        ri = T.find_match_row_index(rows, m)
        if ri < 0:
            return
        row = rows[ri]
        self._ensure_rendered(row.turn)
        t = self._txt
        t.tag_remove("find_cur", "1.0", "end")
        mark = f"rw:{row.key}"
        trng = t.tag_ranges(f"tn:{row.turn}")
        if mark not in t.mark_names() or not trng:
            self._goto_turn(row.turn, flash=False)
            return
        rs, re_ = t.index(mark), str(trng[-1])
        pos, idx, last = rs, rs, None
        for _ in range(k + 1):
            r = t.tag_nextrange("find_hit", idx, re_)
            if not r:
                break
            pos, idx, last = str(r[0]), str(r[1]), r
        if last is not None:
            t.tag_add("find_cur", last[0], last[1])
        self._scroll_center(pos)
        self._cur_turn = -1
        self._on_scrolled()

    def _scroll_center(self, idx: str) -> None:
        t = self._txt
        t.see(idx)
        try:
            self._win.update_idletasks()
            bb = t.bbox(idx)
            if bb:
                dy = bb[1] - t.winfo_height() // 2
                if abs(dy) > 4:
                    t.yview_scroll(int(dy), "pixels")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 右键菜单
    def _on_text_context(self, e) -> None:
        t = self._txt
        idx = t.index(f"@{e.x},{e.y}")
        turn_idx = None
        for name in t.tag_names(idx):
            if name.startswith("tn:"):
                turn_idx = int(name[3:])
                break
        m = FlatMenu(self._win)
        has_sel = bool(t.tag_ranges("sel"))
        m.add_command(label="复制所选", command=lambda: self._copy_selection(),
                      state="normal" if has_sel else "disabled",
                      image=ui_icon(self._win, "copy"), compound="left")
        m.add_command(
            label="复制这一轮为 Markdown",
            command=lambda: self._copy_turn_md(turn_idx),
            state="normal" if turn_idx is not None else "disabled",
            image=ui_icon(self._win, "copy"), compound="left")
        m.tk_popup(e.x_root, e.y_root)

    def _copy_selection(self) -> None:
        try:
            self._copy_text(self._txt.get("sel.first", "sel.last"), "已复制所选")
        except tk.TclError:
            pass

    def _copy_turn_md(self, turn_idx: int | None) -> None:
        if turn_idx is None or self._tr is None:
            return
        tr = self._tr
        text = T.turn_to_markdown(
            self._turns[turn_idx], tr.agent_style, app_name=T._APP_NAMES.get(tr.source, "助手"),
            project_dir=tr.cwd, include_thinking=self._include_thinking)
        self._copy_text(text, "已复制这一轮")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clip_line(line: str, n: int = 600) -> str:
    return line if len(line) <= n else line[:n] + "…"


def _clip_block(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"
