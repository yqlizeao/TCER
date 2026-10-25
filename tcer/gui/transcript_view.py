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
import re
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
from .platform import CLICK_CURSOR, FILE_MANAGER_NAME, bind_mousewheel, open_in_file_manager
from .views import source_icon, ui_icon
from .widgets import (
    FlatMenu,
    RoundedPill,
    SegmentedControl,
    StatusBadge,
    Tooltip,
    flat_button,
    get_rounded_rect_img,
    new_window,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CONTENT_MAX_W = 820          # 主区内容最大宽（px），其余留白居中
BUBBLE_FRAC = 0.74           # 提问气泡占内容宽比例
BUBBLE_BG = theme.TRANSCRIPT_BUBBLE_BG["generic"]  # 默认提问气泡底（按数据源动态切换）
OUTLINE_W = 268              # 右侧目录默认宽
IND = theme.PAD_L * 2        # AI 区（步骤 / 回复）左缩进（对应头像列）
DETAIL_IND = IND + theme.PAD_L * 2
BATCH_CHARS = 40_000         # 每批渲染的字符预算
PAGE_CHARS = 512 * 1024      # 「显示全部」一页字符数
QUESTION_FOLD_CHARS = 3000   # 超长提问折叠阈值
SEARCH_EXPAND_MAX = 60       # 命中轮数不超过它才全量展开
BATCH_SECONDS = 0.035        # 每批渲染的时间预算（再配合字符预算）
DIFF_MAX_LINES = 400
CODE_MAX_LINES = 400
CODE_FOLD_LINES = 80         # 围栏代码块默认折叠阈值（超出可点击展开全部）
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
_GAP_TAGS = frozenset({
    "divider", "q_gap_top", "q_pad", "ai_gap_top", "ai_gap_bot",
    "md_gap", "md_gap_h_top", "md_gap_h_bot", "md_gap_p", "md_gap_li", "md_wrap_gap",
    "md_hr", "md_hr_n", "md_trule",
    "md_th_pad", "md_th_inner", "md_td_pad", "md_td_inner", "md_td_alt_pad", "md_td_alt_inner",
    "chg_gap", "chg_sep",
})


def _fmt_turn_cost(cost: float | None) -> str:
    """格式化单轮成本（如 $0.42、$0.008），为 0 或未知时返回空串。"""
    if cost is None or cost <= 0:
        return ""
    if cost >= 1.0:
        return f"${cost:,.2f}"
    if cost >= 0.01:
        return f"${cost:.2f}"
    if cost >= 0.001:
        return f"${cost:.3f}"
    return f"${cost:.4f}"


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


def _fit_px(font: tkfont.Font, text: str, max_px: int) -> str:
    """按精确像素上限把单行 text 截断并加省略号。"""
    text = text.replace("\n", " ")
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    ell = font.measure("…")
    if max_px <= ell:
        return "…"
    budget = max_px - ell
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "…") if lo > 0 else "…"


def _smart_cut(rest: str, cut: int) -> int:
    """在二分截断点 ``cut`` 附近应用西文整词/路径边界与中文避头尾（Kinsoku）禁则。"""
    if cut >= len(rest) or cut <= 1:
        return cut
    # 1) 西文单词或路径中间被切断时，优先回退到空格或路径分隔符后
    if rest[cut - 1].isascii() and rest[cut - 1].isalnum() and rest[cut].isascii() and rest[cut].isalnum():
        best = -1
        for sep in (" ", "/", "\\", "-"):
            idx = rest.rfind(sep, 0, cut)
            if idx >= 0:
                cand = idx if sep == " " else idx + 1
                if cand > best:
                    best = cand
        if best > cut // 2:
            cut = best
    else:
        sp = rest.rfind(" ", 0, cut + 1)
        if sp > cut // 2:
            cut = sp
    # 2) 行首禁则：下一行首字符若为句读/右括号，回退 1~2 字把前字一起带下去
    steps = 0
    while cut > 2 and steps < 2:
        nxt = rest[cut:].lstrip(" ")
        if nxt and nxt[0] in MD.KINSOKU_NO_START:
            cut -= 1
            steps += 1
        else:
            break
    # 3) 行尾禁则：本行末字符若为左括号/前引号，回退让其跟随下一行内容
    while cut > 2 and rest[cut - 1] in MD.KINSOKU_NO_END:
        cut -= 1
    return cut


def wrap_text(font: tkfont.Font, text: str, width: int) -> list[str]:
    """按像素宽把文本折成多行（保留原有换行；西文整词/路径优先，遵守中文避头尾禁则）。"""
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
            cut = _smart_cut(rest, lo) if lo < len(rest) else lo
            out.append(rest[:cut].rstrip() or rest[:cut])
            rest = rest[cut:].lstrip(" ")
    return out or [""]


def _wrap_lines(font: tkfont.Font, text: str, width: int, max_lines: int) -> list[str]:
    """按像素宽折行（西文整词/路径边界 + 中文避头尾），最多 max_lines 行，超出末行加省略号。"""
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
        cut = _smart_cut(rest, lo) if (not last and lo < len(rest)) else lo
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
# 滚动条语义概览标尺（Overview Ruler / Marker Rail）
# --------------------------------------------------------------------------- #
class _MarkerRail:
    """主滚动条左侧的 10px 语义全息标尺：将全长会话映射为多色刻度与视口指示框。

    颜色编码（排除高频的普通单步工具报错，聚焦高信噪比语义）：
    - 翠绿（``theme.SUCCESS``）：代码改动轮次（Edit / Write）
    - 亮紫（``theme.PURPLE``）：成本 / Token 热点轮次（占比 ≥25% 且显著偏高）
    - 冷青（``theme.CYAN``）：上下文演进轮次（上下文压缩 / 切换模型 / 派生子代理）
    - 冰蓝（``theme.TRANSCRIPT_SYN_KW``）：深度推演长链（≥15 步）
    - 金黄（``theme.LEVEL_COMPOUND``）：当前查找（Ctrl+F）命中所在轮次
    - 珊瑚红（``theme.ERROR``）：仅限会话中断（aborted）或系统级错误事件
    """

    _TAG_PALETTE = {
        "change": theme.SUCCESS,
        "hotspot": theme.PURPLE,
        "context": theme.CYAN,
        "deep": theme.TRANSCRIPT_SYN_KW,
        "search": theme.LEVEL_COMPOUND,
        "abort": theme.ERROR,
    }

    def __init__(self, parent, on_jump) -> None:
        self.canvas = tk.Canvas(parent, width=10, bg=theme.PANEL, highlightthickness=0,
                                bd=0, takefocus=0, cursor=CLICK_CURSOR)
        self._on_jump = on_jump
        self._turns: list = []
        self._telemetries: list = []
        self._search_turns: set[int] = set()
        self._vp: tuple[float, float] = (0.0, 1.0)
        self._ticks: list[tuple[float, float, int, tuple[str, ...], str]] = []
        self._height = 0
        self._tip_win: tk.Toplevel | None = None
        self._tip_lbl: tk.Label | None = None
        self._hover_turn = -1
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    def clear(self) -> None:
        self._turns, self._telemetries, self._ticks = [], [], []
        self._search_turns = set()
        self._vp = (0.0, 1.0)
        self._hide_tip()
        self.canvas.delete("all")

    def set_turns(self, turns, telemetries, search_turn_keys=()) -> None:
        self._turns = list(turns)
        self._telemetries = list(telemetries)
        key_set = set(search_turn_keys or ())
        self._search_turns = {t.index for t in self._turns if t.key in key_set}
        self._rebuild_ticks()
        self._redraw()

    def set_search_keys(self, search_turn_keys=()) -> None:
        key_set = set(search_turn_keys or ())
        new_set = {t.index for t in self._turns if t.key in key_set}
        if new_set != self._search_turns:
            self._search_turns = new_set
            self._rebuild_ticks()
            self._redraw()

    def set_viewport(self, first: float, last: float) -> None:
        vp = (max(0.0, min(1.0, float(first))), max(0.0, min(1.0, float(last))))
        if abs(vp[0] - self._vp[0]) < 0.002 and abs(vp[1] - self._vp[1]) < 0.002:
            return
        self._vp = vp
        self._redraw_viewport()

    def _rebuild_ticks(self) -> None:
        self._ticks = []
        n = len(self._turns)
        if not n:
            return
        weights = [max(2, len(t.steps) + (2 if t.question else 0) + (3 if t.final else 0))
                   for t in self._turns]
        total_w = max(1, sum(weights))
        acc = 0
        for i, turn in enumerate(self._turns):
            f0 = acc / total_w
            acc += weights[i]
            f1 = acc / total_w
            tel = self._telemetries[i] if i < len(self._telemetries) else None
            tags: list[str] = []
            labels: list[str] = []
            if i in self._search_turns:
                tags.append("search")
                labels.append("查找命中")
            if tel is not None:
                if tel.has_code_change:
                    tags.append("change")
                    labels.append(f"代码改动 +{tel.added} −{tel.removed}")
                if tel.is_hotspot:
                    tags.append("hotspot")
                    c_str = _fmt_turn_cost(tel.cost_usd)
                    t_str = S.format_tokens(tel.total_tokens) if tel.total_tokens else ""
                    hs = " · ".join(x for x in (t_str and f"{t_str} tok", c_str) if x)
                    labels.append(f"Token/成本热点（{hs}）" if hs else "Token/成本热点")
                if tel.has_context_shift:
                    tags.append("context")
                    labels.append("上下文演进（压缩/切模型/子代理）")
                if tel.is_deep_turn and "hotspot" not in tags:
                    tags.append("deep")
                    labels.append(f"深度推演（{len(turn.steps)} 步）")
                if tel.is_severe_abort:
                    tags.append("abort")
                    labels.append("会话中断 / 系统异常")
            if tags:
                q_snip = ""
                if turn.question and turn.question.text:
                    q_snip = _clip(" ".join(turn.question.text.split()), 28)
                head = f"第 {i + 1} 轮" + (f" · {q_snip}" if q_snip else "")
                tip = head + "\n" + " · ".join(labels)
                self._ticks.append((f0, f1, i, tuple(tags), tip))

    def _on_configure(self, e) -> None:
        if abs(e.height - self._height) < 2:
            return
        self._height = e.height
        self._redraw()

    def _redraw_viewport(self) -> None:
        c = self.canvas
        h = max(1, self._height or c.winfo_height())
        c.delete("vp")
        f0, f1 = self._vp
        if f1 - f0 >= 0.995:
            return
        y0 = max(1, int(f0 * h))
        y1 = min(h - 1, max(y0 + 12, int(f1 * h)))
        c.create_rectangle(1, y0, 9, y1, fill=theme.TRANSCRIPT_RAIL_VP,
                           outline=theme.TRANSCRIPT_RAIL_BORDER, tags=("vp",))
        c.tag_lower("vp")

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        h = max(1, self._height or c.winfo_height())
        self._redraw_viewport()
        for f0, f1, turn_idx, tags, _tip in self._ticks:
            yc = int(((f0 + f1) * 0.5) * h)
            th = max(3 if "search" in tags else 2, min(8, int((f1 - f0) * h * 0.5)))
            y0 = max(1, min(h - th - 1, yc - th // 2))
            y1 = y0 + th
            cols = [self._TAG_PALETTE[t] for t in tags if t in self._TAG_PALETTE][:3]
            if not cols:
                continue
            seg_w = 8 / len(cols)
            for si, col in enumerate(cols):
                x0 = 1 + int(round(si * seg_w))
                x1 = 1 + int(round((si + 1) * seg_w))
                c.create_rectangle(x0, y0, max(x0 + 1, x1), y1, fill=col, outline="",
                                   tags=("tick", f"tk:{turn_idx}"))

    def _hit_tick(self, y: int) -> tuple[int, str] | None:
        h = max(1, self._height or self.canvas.winfo_height())
        best = None
        best_dist = 7
        for f0, f1, turn_idx, _tags, tip in self._ticks:
            yc = int(((f0 + f1) * 0.5) * h)
            dist = abs(y - yc)
            if dist <= best_dist:
                best_dist = dist
                best = (turn_idx, tip)
        return best

    def _on_motion(self, e) -> None:
        hit = self._hit_tick(e.y)
        if hit is None:
            self._hover_turn = -1
            self._hide_tip()
            return
        turn_idx, tip = hit
        self._hover_turn = turn_idx
        self._show_tip(tip, e.y_root)

    def _on_leave(self, _e=None) -> None:
        self._hover_turn = -1
        self._hide_tip()

    def _show_tip(self, text: str, y_root: int) -> None:
        try:
            if self._tip_win is None or not self._tip_win.winfo_exists():
                tw = tk.Toplevel(self.canvas)
                tw.wm_overrideredirect(True)
                try:
                    tw.attributes("-topmost", True)
                except tk.TclError:
                    pass
                tw.configure(bg=theme.BORDER)
                lbl = tk.Label(tw, text=text, bg=theme.PANEL_2, fg=theme.FG,
                               font=theme.FONT_UI_SMALL, justify="left",
                               padx=theme.PAD_S, pady=theme.PAD_XS)
                lbl.pack(padx=1, pady=1)
                self._tip_win = tw
                self._tip_lbl = lbl
            else:
                self._tip_lbl.configure(text=text)
            self._tip_win.update_idletasks()
            tw_w = self._tip_win.winfo_reqwidth()
            x = max(8, self.canvas.winfo_rootx() - tw_w - 6)
            y = max(8, y_root - 14)
            self._tip_win.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _hide_tip(self) -> None:
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None
            self._tip_lbl = None

    def _on_click(self, e) -> None:
        if not self._turns:
            return
        hit = self._hit_tick(e.y)
        if hit is not None:
            self._on_jump(hit[0])
            return
        h = max(1, self._height or self.canvas.winfo_height())
        frac = max(0.0, min(0.999, e.y / h))
        idx = min(len(self._turns) - 1, int(frac * len(self._turns)))
        self._on_jump(idx)


# --------------------------------------------------------------------------- #
# 右侧「对话目录」（Canvas 自绘）
# --------------------------------------------------------------------------- #
class _Outline:
    """每个有提问 / 斜杠的轮一组：「你」右对齐气泡 + AI 步数 / 红点 / 回复摘要。"""

    def __init__(self, parent) -> None:
        self.frame = tk.Frame(parent, bg=theme.PANEL)
        head = tk.Frame(self.frame, bg=theme.PANEL)
        head.pack(fill="x")
        self._title = tk.Label(head, text="对话目录", bg=theme.PANEL, fg=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD, anchor="w")
        self._title.pack(side="left", padx=theme.PAD_M, pady=theme.PAD_S)
        self._err_lbl = tk.Label(head, text="", bg=theme.PANEL, fg=theme.ERROR,
                                 font=theme.FONT_UI_SMALL, cursor=CLICK_CURSOR,
                                 padx=theme.PAD_S, pady=1)
        self._err_lbl.bind("<Button-1>", self._step_error)
        self._err_lbl.bind("<Enter>", lambda _e: self._err_lbl.configure(bg=theme.HOVER_BG))
        self._err_lbl.bind("<Leave>", lambda _e: self._err_lbl.configure(bg=theme.PANEL))
        Tooltip(self._err_lbl, "点击循环定位含失败或中断的轮次")
        tk.Frame(self.frame, bg=theme.BORDER, height=1).pack(fill="x")
        self.canvas = tk.Canvas(self.frame, bg=theme.PANEL, highlightthickness=0, bd=0,
                                takefocus=0)
        self.vbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.entries: list = []
        self.turns: list = []
        self.telemetries: list = []
        self._err_indices: list[int] = []
        self._err_pos = -1
        self._icon = None
        self._source = "generic"
        self._on_you = None
        self._on_ai = None
        self._on_err = None
        self._y: list[tuple[int, int]] = []
        self._y_ai: list[int] = []
        self._cur = -1
        self._hover = -1
        self._gen = 0
        self._built = 0
        self._width = 0
        self._unbind_wheel = None
        self._relayout_after = None
        self._font = tkfont.Font(font=theme.FONT_UI_SMALL)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)

    # -- 滚轮 -----------------------------------------------------------------
    def _on_enter(self, _e=None) -> None:
        self._unbind_wheel = bind_mousewheel(self.canvas, self._wheel)

    def _wheel(self, units) -> None:
        first, last = self.canvas.yview()
        if last - first < 1.0:
            self.canvas.yview_scroll(units, "units")

    def _on_leave(self, _e=None) -> None:
        self._set_hover(-1)
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
        self.entries, self.turns, self.telemetries, self._y, self._y_ai = [], [], [], [], []
        self._cur, self._hover, self._built = -1, -1, 0
        self._err_indices, self._err_pos = [], -1
        self._err_lbl.pack_forget()
        self._title.configure(text=title)

    def set_entries(self, entries, turns, icon, on_you, on_ai, source: str = "generic",
                    on_err=None, telemetries=None) -> None:
        self.clear(f"对话目录 {len(entries)}" if entries else "对话目录")
        self.entries, self.turns, self._icon = list(entries), turns, icon
        self.telemetries = list(telemetries) if telemetries else []
        self._source = source or "generic"
        self._on_you, self._on_ai, self._on_err = on_you, on_ai, on_err
        self._err_indices = [i for i, e in enumerate(self.entries) if getattr(e, "has_error", False)]
        if self._err_indices:
            self._err_lbl.configure(text=f"● {len(self._err_indices)} 轮异常")
            self._err_lbl.pack(side="right", padx=(0, theme.PAD_S), pady=theme.PAD_XS)
        self._pending_cur = None
        self._width = max(0, self.canvas.winfo_width())
        self._build_from(0, self._gen)

    def _step_error(self, _e=None) -> None:
        if not self._err_indices:
            return
        n_err = len(self._err_indices)
        self._err_pos = (self._err_pos + 1) % n_err
        idx = self._err_indices[self._err_pos]
        self._err_lbl.configure(text=f"● {n_err} 轮异常 ({self._err_pos + 1}/{n_err})")
        self.set_current(idx)
        if self._on_err:
            self._on_err(self.entries[idx], self._err_pos + 1, n_err)

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
        self._y, self._y_ai = [], []
        self._cur, self._hover, self._built, self._gen = -1, -1, 0, self._gen + 1
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
        tel = self.telemetries[ent.turn_index] if ent.turn_index < len(self.telemetries) else None
        f = self._font
        ls = f.metrics("linespace")
        w = max(120, self._width or OUTLINE_W)
        padx = theme.PAD_M
        right, left = w - padx - 4, padx + 4
        inner = right - left
        tag_i = f"ei:{i}"
        top = y
        src = self._source
        bub_col = theme.TRANSCRIPT_BUBBLE_BG.get(src, BUBBLE_BG)
        rail_col = theme.TRANSCRIPT_AGENT_ACCENT.get(src, theme.ACCENT)
        bg = c.create_rectangle(4, y, w - 4, y + 1, fill=theme.PANEL_2, outline="", state="hidden",
                                tags=("ol_hit", tag_i, f"ebg:{i}"))
        rail = c.create_rectangle(2, y + 4, 2 + theme.RAIL_W_NAV, y + 1, fill=rail_col, outline="",
                                  state="hidden", tags=("ol_hit", tag_i, f"erail:{i}"))
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
        bw = max(16, x1 - x0)
        bh = max(14, th + 2 * theme.PAD_XS + 2)
        bimg = get_rounded_rect_img(c, bw, bh, theme.RADIUS_M, bub_col, theme.PANEL)
        if bimg is not None:
            c.create_image(x0, y, anchor="nw", image=bimg, tags=("ol_hit", tag_i, "ol_you"))
        else:
            c.create_rectangle(x0, y, x1, y + bh, fill=bub_col, outline="",
                               tags=("ol_hit", tag_i, "ol_you"))
        c.create_text(x0 + theme.PAD_S, y + theme.PAD_XS + 1, anchor="nw", text="\n".join(lines),
                      fill=theme.FG, font=theme.FONT_UI_SMALL, justify="left",
                      tags=("ol_hit", tag_i, "ol_you"))
        y += bh + theme.PAD_S
        # AI 起点（用于整卡上下半区点击分流：上半区跳提问，下半区跳回复）
        self._y_ai.append(y)
        x = left
        if self._icon is not None:
            c.create_image(x, y, anchor="nw", image=self._icon,
                           tags=("ol_hit", tag_i, "ol_ai"))
            x += 20
        label = f"{ent.step_count} 步"
        c.create_text(x, y + 1, anchor="nw", text=label, fill=theme.MUTED,
                      font=theme.FONT_UI_SMALL, tags=("ol_hit", tag_i, "ol_ai"))
        bx = x + f.measure(label) + theme.PAD_S
        if ent.has_error:
            d = theme.PAD_S + 2
            cy = y + 1 + ls // 2
            c.create_oval(bx, cy - d // 2, bx + d, cy + d // 2,
                          fill=theme.ERROR, outline="", tags=("ol_hit", tag_i, "ol_ai"))
            bx += d + theme.PAD_XS
        if tel is not None and tel.is_hotspot:
            c.create_text(bx, y + 1, anchor="nw", text="◆", fill=theme.PURPLE,
                          font=theme.FONT_UI_SMALL, tags=("ol_hit", tag_i, "ol_ai"))
            bx += f.measure("◆") + theme.PAD_XS
        if tel is not None and tel.has_context_shift:
            c.create_text(bx, y + 1, anchor="nw", text="◈", fill=theme.CYAN,
                          font=theme.FONT_UI_SMALL, tags=("ol_hit", tag_i, "ol_ai"))
        if tel is not None and (tel.added or tel.removed):
            rx = right
            mfont = (theme.FONT_MONO[0], theme.FONT_UI_SMALL[1])
            if tel.removed:
                r_txt = f"−{tel.removed}"
                c.create_text(rx, y + 1, anchor="ne", text=r_txt, fill=theme.ERROR,
                              font=mfont, tags=("ol_hit", tag_i, "ol_ai"))
                rx -= f.measure(r_txt) + theme.PAD_XS
            if tel.added:
                a_txt = f"+{tel.added}"
                c.create_text(rx, y + 1, anchor="ne", text=a_txt, fill=theme.SUCCESS,
                              font=mfont, tags=("ol_hit", tag_i, "ol_ai"))
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
        c.coords(bg, 4, top, w - 4, y)
        c.coords(rail, 2, top + 4, 2 + theme.RAIL_W_NAV, max(top + 6, y - 4))
        return y + theme.PAD_XS

    # -- 交互 -----------------------------------------------------------------
    def _hit_at(self, e=None) -> tuple[int | None, str]:
        """按画布纵坐标定位目录卡片与上下半区（零死角），无坐标时回退 current tag。"""
        if e is not None and getattr(e, "y", None) is not None and self._y:
            cy = self.canvas.canvasy(e.y)
            # 二分定位卡片区间（含卡片间 PAD_XS 缝隙就近归属）
            lo, hi = 0, len(self._y) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                y0, y1 = self._y[mid]
                if cy < y0:
                    hi = mid - 1
                elif cy > y1:
                    lo = mid + 1
                else:
                    zone = "ai" if (mid < len(self._y_ai) and cy >= self._y_ai[mid]) else "you"
                    return mid, zone
        tags = self.canvas.gettags("current")
        idx = next((int(t[3:]) for t in tags if t.startswith("ei:")), None)
        if idx is not None:
            return idx, ("ai" if "ol_ai" in tags else "you")
        return None, "you"

    def _set_hover(self, idx: int) -> None:
        if idx == self._hover:
            return
        c = self.canvas
        old = self._hover
        self._hover = idx
        if 0 <= old < self._built and old != self._cur:
            c.itemconfigure(f"ebg:{old}", state="hidden", fill=theme.PANEL_2)
        if 0 <= idx < self._built and idx != self._cur:
            c.itemconfigure(f"ebg:{idx}", state="normal", fill=theme.HOVER_BG)
        c.configure(cursor=CLICK_CURSOR if idx >= 0 else "arrow")

    def _on_motion(self, e) -> None:
        idx, _ = self._hit_at(e)
        self._set_hover(-1 if idx is None else idx)

    def _on_click(self, e=None) -> None:
        idx, zone = self._hit_at(e)
        if idx is None or not (0 <= idx < len(self.entries)):
            return
        self.set_current(idx)
        if zone == "ai" and self._on_ai:
            self._on_ai(self.entries[idx])
        elif self._on_you:
            self._on_you(self.entries[idx])

    def set_current(self, i: int) -> None:
        if i >= self._built:
            self._pending_cur = i
            return
        c = self.canvas
        if 0 <= self._cur < self._built and self._cur != i:
            c.itemconfigure(f"ebg:{self._cur}",
                            state="normal" if self._cur == self._hover else "hidden",
                            fill=theme.HOVER_BG if self._cur == self._hover else theme.PANEL_2)
            c.itemconfigure(f"erail:{self._cur}", state="hidden")
        self._cur = i
        if i < 0:
            return
        c.itemconfigure(f"ebg:{i}", state="normal", fill=theme.PANEL_2)
        c.itemconfigure(f"erail:{i}", state="normal")
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
        self._telemetries: list[T.TurnTelemetry] = []
        self._file_changes: list[T.FileChangeEntry] = []
        self._chg_paths: dict[str, tuple[str, str]] = {}
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
        self._path_cache: dict = {}          # 行内路径存在性缓存 raw → (resolved, exists) | None
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
        self._act_inline: set = set()
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
        self._outline_lock_until = 0.0
        self._cur_turn = -1
        self._last_w = -1
        self._cw = CONTENT_MAX_W
        self._hover_act = None
        self._bfont = tkfont.Font(font=theme.FONT_UI)       # 气泡 / 正文折行量字用
        self._bfont_bold = tkfont.Font(font=theme.FONT_UI_BOLD)
        self._sfont = tkfont.Font(font=theme.FONT_UI_SMALL)
        self._mfont = tkfont.Font(font=theme.FONT_MONO)
        self._cfont = tkfont.Font(font=(theme.FONT_MONO[0], theme.FONT_UI_SMALL[1]))
        self._hfonts = {
            1: tkfont.Font(font=theme.FONT_TITLE),
            2: tkfont.Font(font=theme.FONT_HEADING),
            3: tkfont.Font(font=(theme.FONT_UI_BOLD[0], 10, "bold")),
        }
        self._space_w = max(1, self._bfont.measure(" "))
        self._tbl_font_b = self._bfont_bold                 # 表格列宽量字（取粗体，偏保守）
        self._rule_w = max(1, tkfont.Font(font=theme.FONT_UI_SMALL).measure(" "))
        self._tbl_seq = 0
        self._qb_seq = 0
        self._pad_font = tkfont.Font(font=(theme.FONT_UI[0], 5))
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
    def _icon_btn(self, parent, role, tip, command, *, bg=None, hover_bg=None, text=""):
        img_on = ui_icon(parent, role, opacity=0.95)
        img_off = ui_icon(parent, role, opacity=0.26)
        kw = {}
        if bg:
            kw["bg"] = bg
        if hover_bg:
            kw["hover_bg"] = hover_bg
        if img_on is not None:
            b = flat_button(parent, text, command, image=img_on, padx=theme.PAD_S,
                            pady=theme.PAD_S, **kw)
            b.image = img_on
            b._img_on = img_on
            b._img_off = img_off
        else:
            b = flat_button(parent, text or tip[:2], command, padx=theme.PAD_S, **kw)
        b.bind("<Enter>", lambda _e, btn=b: (
            btn.config(bg=btn._hover_bg) if str(btn.cget("state")) != "disabled" else None))
        b.bind("<Leave>", lambda _e, btn=b: btn.config(bg=btn._base_bg))
        Tooltip(b, tip)
        return b

    @staticmethod
    def _set_btn_state(btn, enabled: bool) -> None:
        btn.configure(state="normal" if enabled else "disabled")
        img = getattr(btn, "_img_on" if enabled else "_img_off", None)
        if img is not None:
            btn.configure(image=img)
            btn.image = img

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
        self._resume_box = tk.Frame(row1, bg=theme.PANEL_2)
        self._resume_box.pack(side="right", padx=(theme.PAD_M, 0))
        self._resume_btn = flat_button(self._resume_box, "复制恢复命令", self._copy_resume,
                                       image=ui_icon(row1, "play", opacity=0.98),
                                       bg=theme.TRANSCRIPT_ACTION_BG,
                                       hover_bg=theme.TRANSCRIPT_ACTION_HOVER, fg=theme.FG_WHITE)
        self._resume_btn.pack(side="left", fill="y")
        self._resume_sep = tk.Frame(self._resume_box, bg=theme.PANEL_2, width=1)
        self._resume_sep.pack(side="left", fill="y")
        self._resume_menu_btn = self._icon_btn(
            self._resume_box, "chevron-down", "进入目录并恢复…", self._open_resume_menu,
            bg=theme.TRANSCRIPT_ACTION_BG, hover_bg=theme.TRANSCRIPT_ACTION_HOVER)
        self._resume_menu_btn.pack(side="left", fill="y")
        self._nav_box = tk.Frame(row1, bg=theme.PANEL, padx=2, pady=1,
                                 highlightthickness=1, highlightbackground=theme.BORDER_SUBTLE)
        self._nav_box.pack(side="right", padx=(theme.PAD_M, 0))
        self._prev_btn = self._icon_btn(self._nav_box, "chevron-left", "上一个会话",
                                        lambda: self._step_session(-1), bg=theme.PANEL)
        self._prev_btn.pack(side="left")
        self._nav_lbl = tk.Label(self._nav_box, text="1/1", bg=theme.PANEL, fg=theme.MUTED,
                                 font=(theme.FONT_MONO[0], theme.FONT_UI_SMALL[1]), padx=4)
        self._nav_lbl.pack(side="left")
        self._next_btn = self._icon_btn(self._nav_box, "chevron-right", "下一个会话",
                                        lambda: self._step_session(+1), bg=theme.PANEL)
        self._next_btn.pack(side="left")
        self._src_lbl = tk.Label(row1, bg=theme.PANEL_2)
        self._src_lbl.pack(side="left", padx=(0, theme.PAD_S))
        self._proj_badge_box = tk.Frame(row1, bg=theme.PANEL_2)
        self._proj_badge_box.pack(side="left", padx=(0, theme.PAD_S))
        self._title_full = ""
        self._title_font = tkfont.Font(font=theme.FONT_HEADING)
        self._title_lbl = tk.Label(row1, text="", bg=theme.PANEL_2, fg=theme.FG_WHITE,
                                   font=theme.FONT_HEADING, anchor="w", width=1)
        self._title_lbl.pack(side="left", fill="x", expand=True)
        self._title_lbl.bind("<Configure>", lambda _e: self._fit_title())
        tk.Frame(hdr, bg=theme.DIVIDER, height=1).pack(fill="x")
        # 第 2 行：左信息条 + 右工具组（对齐 cc-switch 两行页头；工具组先 pack，窄窗时
        # 只挤压信息条，按钮不被截断）
        row2 = tk.Frame(hdr, bg=theme.PANEL_2)
        row2.pack(fill="x", padx=theme.PAD_L, pady=(theme.PAD_S, theme.PAD_S))
        inner = tk.Frame(row2, bg=theme.PANEL_2)
        inner.pack(side="right")
        self._info = tk.Frame(row2, bg=theme.PANEL_2)
        self._info.pack(side="left", fill="x", expand=True)
        self._reason_lbl = tk.Label(hdr, text="", bg=theme.PANEL_2, fg=theme.MUTED,
                                    font=theme.FONT_UI_SMALL, anchor="w")
        tk.Frame(win, bg=theme.DIVIDER, height=1).pack(fill="x")

        self._outline_btn = self._icon_btn(inner, "panel-right", "显示 / 隐藏对话目录",
                                           self._toggle_outline,
                                           bg=theme.CONTROL_BG if self._outline_on else theme.PANEL_2)
        self._outline_btn.pack(side="left", padx=(theme.PAD_M, theme.PAD_S))
        self._pills: dict = {}
        self._view_seg = SegmentedControl(
            inner,
            [(T.VIEW_ALL, "全部"), (T.VIEW_CONVERSATION, "对话"), (T.VIEW_CHANGES, "改动")],
            value=self._view,
            on_change=self._set_view,
            height=26,
            bg=theme.PANEL_2,
            track_fill=theme.PANEL,
            active_fill=theme.CONTROL_BG,
            outline=theme.BORDER_SUBTLE,
            radius=theme.RADIUS_M,
        )
        self._view_seg.pack(side="left", padx=(0, theme.PAD_XS))
        self._refresh_pills()
        opt_on = self._expand_all or self._show_injected
        self._opt_btn = self._icon_btn(inner, "sliders", "显示选项", self._open_options_menu,
                                       bg=theme.CONTROL_BG if opt_on else theme.PANEL_2)
        self._opt_btn.pack(side="left", padx=(theme.PAD_M, 0))
        # 展开/收起与导出已收纳在「显示选项」与顶栏「⋯」菜单中，此处保留属性供外部状态同步而不重复排布
        self._expand_btn = self._icon_btn(inner, "expand-all", "展开全部过程",
                                          lambda: self._set_expand_all(True), bg=theme.PANEL_2)
        self._collapse_btn = self._icon_btn(inner, "collapse-all", "收起全部过程",
                                            lambda: self._set_expand_all(False), bg=theme.PANEL_2)
        self._export_btn = self._icon_btn(inner, "export", "导出 Markdown", self._export_md,
                                          bg=theme.PANEL_2)
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
        find_box = tk.Frame(fi, bg=theme.CONTROL_BG, padx=theme.PAD_S, pady=2)
        find_box.pack(side="left", padx=(0, theme.PAD_M))
        self._find_entry = tk.Entry(find_box, textvariable=self._find_var, width=28,
                                    bg=theme.CONTROL_BG, fg=theme.FG, insertbackground=theme.FG,
                                    relief="flat", bd=0, highlightthickness=0,
                                    font=theme.FONT_UI, selectbackground=theme.HOVER_ACCENT,
                                    selectforeground=theme.FG_WHITE)
        self._find_entry.pack(side="left", ipady=1)
        self._find_var.trace_add("write", lambda *_a: self._schedule_find())
        self._find_entry.bind("<Return>", lambda _e: (self._find_step(+1), "break")[1])
        self._find_entry.bind("<Shift-Return>", lambda _e: (self._find_step(-1), "break")[1])
        self._find_entry.bind("<Down>", lambda _e: (self._find_step(+1), "break")[1])
        self._find_entry.bind("<Up>", lambda _e: (self._find_step(-1), "break")[1])
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
        self._marker_rail = _MarkerRail(left, on_jump=self._on_rail_jump)
        self._marker_rail.canvas.pack(side="right", fill="y")
        tk.Frame(left, bg=theme.BORDER, width=1).pack(side="right", fill="y")
        self._txt = tk.Text(
            left, wrap="char", bg=theme.BG, fg=theme.FG, font=theme.FONT_UI, relief="flat",
            bd=0, highlightthickness=0, padx=IND, pady=theme.PAD_L, cursor="arrow",
            state="disabled", insertwidth=0, yscrollcommand=lambda a, b: self._on_yscroll(sb, a, b),
            selectbackground=theme.BG, selectforeground=theme.FG,
            inactiveselectbackground=theme.BG, exportselection=False, undo=False,
            takefocus=0)
        sb.configure(command=self._txt.yview)
        self._txt.pack(side="left", fill="both", expand=True)
        self._latest_btn = flat_button(left, "最新", self._scroll_latest,
                                       image=ui_icon(left, "latest"), bg=theme.PANEL_2,
                                       hover_bg=theme.CONTROL_BG, fg=theme.FG)
        self._configure_tags()
        t = self._txt
        t.bind("<Configure>", self._on_text_configure)
        t.bind("<Button-1>", lambda _e: t.focus_set(), add="+")
        t.bind("<Button-3>", self._on_text_context)
        t.bind("<Motion>", self._on_motion)
        t.bind("<<Selection>>", self._on_selection_changed)
        t.bind("<<Copy>>", self._on_copy_event)
        t.bind("<Control-c>", self._on_copy_event)
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
        bg = theme.BG
        # 禁用原生 sel 的整行换行符涂抹，改用 sel_vis 仅高亮真实文字字符（彻底消除左侧梳齿黑块与右侧整屏蓝块）
        t.tag_configure("sel", background="", foreground="")
        t.tag_lower("sel")
        # 文案行
        t.tag_configure("empty_msg", font=theme.FONT_UI, foreground=theme.MUTED,
                        justify="center", spacing1=theme.PAD_L * 4)
        t.tag_configure("err_msg", font=theme.FONT_UI, foreground=theme.ERROR,
                        justify="center", spacing1=theme.PAD_L * 4, spacing3=theme.PAD_S)
        # 数据推断说明（非告警）：弱化显示；页头「N 条提示」保留醒目入口
        t.tag_configure("warn_msg", font=small, foreground=theme.MUTED, spacing3=pad_s)
        # 分轮线：一行带删除线的空格（1px 线）
        t.tag_configure("divider", font=small, overstrike=True, overstrikefg=theme.DIVIDER,
                        foreground=bg, wrap="none", spacing1=theme.PAD_L,
                        spacing3=theme.PAD_L)
        # 提问气泡（每轮独立 qb:<n> 控制 lmargin1=lmargin2=box_left 与 rmargin，结合
        # lmargincolor/rmargincolor 让左右边缘像素级严格对齐成实心矩形，绝无阶梯锯齿）
        t.tag_configure("q_gap_top", font=(ui[0], theme.PAD_M), spacing1=0, spacing3=0)
        t.tag_configure("q_meta", font=small, foreground=theme.MUTED, justify="right",
                        spacing1=2, spacing3=pad_xs)
        t.tag_configure("q_line", spacing1=1, spacing2=4, spacing3=1, font=ui, wrap="none")
        t.tag_configure("q_bub", foreground=theme.FG_WHITE, background=BUBBLE_BG)
        t.tag_configure("q_pad", font=(ui[0], 6), spacing1=0, spacing3=0, background=BUBBLE_BG)
        t.tag_configure("q_more", font=small, foreground=theme.CYAN, justify="right",
                        spacing1=2, spacing3=2)
        # AI 头像行（上下外边距独立为无底色间距行，避免行内动作按钮悬停拉出高灰柱）
        t.tag_configure("ai_gap_top", font=(ui[0], theme.PAD_L), spacing1=0, spacing3=0)
        t.tag_configure("ai_gap_bot", font=(ui[0], pad_s), spacing1=0, spacing3=0)
        t.tag_configure("ai_head", spacing1=2, spacing3=2, lmargin1=0, lmargin2=0)
        t.tag_configure("ai_name", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE)
        t.tag_configure("ai_meta", font=small, foreground=theme.MUTED)
        t.tag_configure("ai_model", font=(theme.FONT_MONO[0], small[1]), foreground=theme.MUTED)
        t.tag_configure("ai_loc_add", font=(theme.FONT_MONO[0], small[1]), foreground=theme.SUCCESS)
        t.tag_configure("ai_loc_del", font=(theme.FONT_MONO[0], small[1]), foreground=theme.ERROR)
        # 改动视图顶栏「涉及文件汇总」卡片
        t.tag_configure("chg_gap", font=(ui[0], pad_s), spacing1=0, spacing3=0)
        t.tag_configure("chg_head", font=theme.FONT_UI_SMALL_BOLD, foreground=theme.FG_WHITE,
                        background=theme.PANEL_2, lmargin1=IND, lmargin2=IND,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg,
                        spacing1=pad_xs + 1, spacing3=pad_xs + 1, wrap="none")
        t.tag_configure("chg_row", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.PANEL, lmargin1=IND, lmargin2=IND,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg,
                        spacing1=3, spacing3=3, wrap="none")
        t.tag_configure("chg_sep", font=(ui[0], 1), overstrike=True, overstrikefg=theme.DIVIDER,
                        foreground=bg, lmargin1=IND, lmargin2=IND, rmargin=theme.PAD_M,
                        lmargincolor=bg, rmargincolor=bg, spacing1=0, spacing3=0, wrap="none")
        t.tag_configure("chg_path", foreground=theme.CODE_MINT)
        t.tag_configure("chg_more", font=small, foreground=theme.CYAN,
                        background=theme.PANEL, lmargin1=IND, lmargin2=IND,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg,
                        spacing1=3, spacing3=3, wrap="none")
        # 时间线摘要行
        t.tag_configure("tl_head", font=small, foreground=theme.MUTED,
                        lmargin1=IND, lmargin2=IND, lmargincolor=bg, rmargincolor=bg,
                        spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("tl_bold", font=theme.FONT_UI_SMALL_BOLD, foreground=theme.MUTED)
        t.tag_configure("tl_err", foreground=theme.ERROR)
        t.tag_configure("tl_more", font=small, foreground=theme.MUTED,
                        lmargin1=IND + theme.PAD_L, lmargin2=IND + theme.PAD_L,
                        lmargincolor=bg, rmargincolor=bg, spacing1=pad_xs, spacing3=pad_xs)
        # 步骤行（wrap="none" + 像素级预算截断，保证 │ 时间线竖轨永不断裂）
        for d in (0, 1):
            m = IND + d * theme.PAD_L * 2
            t.tag_configure(f"step_d{d}", lmargin1=m, lmargin2=m + theme.PAD_L,
                            lmargincolor=bg, rmargincolor=bg, spacing1=pad_xs, spacing3=pad_xs,
                            wrap="none")
        t.tag_configure("step_rail", font=small, foreground=theme.BORDER)
        t.tag_configure("step_ui", font=ui, foreground=theme.FG)
        t.tag_configure("step_verb", font=theme.FONT_UI_BOLD, foreground=theme.FG)
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
                        lmargincolor=bg, rmargincolor=bg, spacing1=pad_s)
        t.tag_configure("dt_text", font=small, foreground=theme.FG, lmargin1=dm, lmargin2=dm,
                        lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_code", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.TRANSCRIPT_CODE_BG, lmargin1=dm, lmargin2=dm,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_out", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm + theme.PAD_L, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_conn", font=(mono[0], small[1]), foreground=theme.MUTED)
        t.tag_configure("dt_file", font=(mono[0], small[1]), foreground=theme.CODE_MINT,
                        lmargin1=dm, lmargin2=dm, lmargincolor=bg, rmargincolor=bg, spacing1=pad_s)
        t.tag_configure("dt_act", font=small, foreground=theme.MUTED)
        t.tag_configure("dt_gap", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_add", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.SUCCESS_TINT_BG, lmargin1=dm, lmargin2=dm,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_del", font=(mono[0], small[1]), foreground=theme.FG,
                        background=theme.ERROR_TINT_BG, lmargin1=dm, lmargin2=dm,
                        rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_ctx", font=(mono[0], small[1]), foreground=theme.MUTED,
                        lmargin1=dm, lmargin2=dm, lmargincolor=bg, rmargincolor=bg)
        t.tag_configure("dt_sign_add", foreground=theme.SUCCESS)
        t.tag_configure("dt_sign_del", foreground=theme.ERROR)
        t.tag_configure("dt_ln", foreground=theme.MUTED)
        t.tag_configure("dt_more", font=small, foreground=theme.CYAN, lmargin1=dm, lmargin2=dm,
                        lmargincolor=bg, rmargincolor=bg)
        # 事件 / 注入
        t.tag_configure("ev_line", font=small, foreground=theme.MUTED, justify="center",
                        spacing1=theme.PAD_S, spacing3=theme.PAD_S)
        t.tag_configure("ev_danger", foreground=theme.ERROR)
        t.tag_configure("ev_detail", font=small, foreground=theme.MUTED, justify="center",
                        lmargin1=IND, lmargin2=IND)
        t.tag_configure("inj", font=small, foreground=theme.MUTED, background=theme.PANEL,
                        lmargin1=IND, lmargin2=IND, rmargin=theme.PAD_M,
                        lmargincolor=bg, rmargincolor=bg, spacing1=pad_xs, spacing3=pad_xs)
        # Markdown（两套缩进：正文 / 说明；文本行自身 spacing1=1/spacing3=1，段落间距由无底色
        # md_gap_* 空行承担，彻底杜绝 md_code 背景色向上下间距和左缩进溢出成高灰柱）
        bullet_w = self._bfont.measure("•  ")
        num_w = self._bfont.measure("1. ")
        self._li_lmargin2: dict[str, int] = {}
        for sfx, base in (("", IND), ("_n", DETAIL_IND)):
            t.tag_configure(f"md_p{sfx}", font=ui, foreground=theme.FG, lmargin1=base,
                            lmargin2=base, lmargincolor=bg, rmargincolor=bg,
                            spacing1=1, spacing2=4, spacing3=1, wrap="none")
            for lv, f in ((1, theme.FONT_TITLE), (2, theme.FONT_HEADING),
                          (3, theme.FONT_UI_BOLD), (4, theme.FONT_UI_BOLD),
                          (5, theme.FONT_UI_BOLD), (6, theme.FONT_UI_BOLD)):
                t.tag_configure(f"md_h{lv}{sfx}", font=f, foreground=theme.FG_WHITE,
                                lmargin1=base, lmargin2=base, lmargincolor=bg, rmargincolor=bg,
                                spacing1=1, spacing2=2, spacing3=1, wrap="none")
            for dp in range(4):
                lm = base + theme.PAD_M * (dp + 1)
                lm_b = lm + bullet_w
                lm_n = lm + num_w
                self._li_lmargin2[f"md_li{dp}{sfx}"] = lm_b
                self._li_lmargin2[f"md_oli{dp}{sfx}"] = lm_n
                t.tag_configure(f"md_li{dp}{sfx}", font=ui, foreground=theme.FG, lmargin1=lm,
                                lmargin2=lm_b, lmargincolor=bg, rmargincolor=bg,
                                spacing1=1, spacing2=4, spacing3=1, wrap="none")
                t.tag_configure(f"md_li{dp}{sfx}_cont", font=ui, foreground=theme.FG,
                                lmargin1=lm_b, lmargin2=lm_b, lmargincolor=bg, rmargincolor=bg,
                                spacing1=1, spacing2=4, spacing3=1, wrap="none")
                t.tag_configure(f"md_oli{dp}{sfx}", font=ui, foreground=theme.FG, lmargin1=lm,
                                lmargin2=lm_n, lmargincolor=bg, rmargincolor=bg,
                                spacing1=1, spacing2=4, spacing3=1, wrap="none")
                t.tag_configure(f"md_oli{dp}{sfx}_cont", font=ui, foreground=theme.FG,
                                lmargin1=lm_n, lmargin2=lm_n, lmargincolor=bg, rmargincolor=bg,
                                spacing1=1, spacing2=4, spacing3=1, wrap="none")
            t.tag_configure(f"md_quote{sfx}", font=italic, foreground=theme.MUTED,
                            lmargin1=base + theme.PAD_M, lmargin2=base + theme.PAD_M,
                            lmargincolor=bg, rmargincolor=bg,
                            spacing1=1, spacing2=4, spacing3=1, wrap="none")
            t.tag_configure(f"md_codeblock{sfx}", font=mono, foreground=theme.FG,
                            background=theme.TRANSCRIPT_CODE_BG, lmargin1=base + theme.PAD_S,
                            lmargin2=base + theme.PAD_S, rmargin=theme.PAD_M,
                            lmargincolor=bg, rmargincolor=bg, spacing1=1, spacing3=1)
            t.tag_configure(f"md_lang{sfx}", font=(mono[0], small[1]), foreground=theme.MUTED,
                            background=theme.TRANSCRIPT_CODE_HEAD_BG,
                            lmargin1=base + theme.PAD_S, lmargin2=base + theme.PAD_S,
                            rmargin=theme.PAD_M, lmargincolor=bg, rmargincolor=bg,
                            spacing1=pad_xs, spacing3=pad_xs)
            t.tag_configure(f"md_hr{sfx}", font=small, overstrike=True, overstrikefg=theme.DIVIDER,
                            foreground=bg, wrap="none", lmargin1=base, lmargin2=base,
                            lmargincolor=bg, rmargincolor=bg,
                            spacing1=theme.PAD_M, spacing3=theme.PAD_M)
        # 表格：仅表头下绘 1px 细线；表体靠斑马纹微底色 + 独立上下内边距行自然分界
        # （避免每行删除线造成的双层黑缝，也避免行内代码胶囊底色纵向拉伸粘连）
        t.tag_configure("md_th", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE,
                        background=theme.PANEL_2, spacing1=1, spacing3=1)
        t.tag_configure("md_th_pad", font=(ui[0], 4), background=theme.PANEL_2,
                        spacing1=0, spacing3=0)
        t.tag_configure("md_th_inner", font=(ui[0], 2), background=theme.PANEL_2,
                        spacing1=0, spacing3=0)
        t.tag_configure("md_td", font=ui, foreground=theme.FG, background=bg,
                        spacing1=1, spacing3=1)
        t.tag_configure("md_td_pad", font=(ui[0], 4), background=bg,
                        spacing1=0, spacing3=0)
        t.tag_configure("md_td_inner", font=(ui[0], 2), background=bg,
                        spacing1=0, spacing3=0)
        t.tag_configure("md_td_alt", font=ui, foreground=theme.FG,
                        background=theme.TRANSCRIPT_TABLE_ALT_BG,
                        spacing1=1, spacing3=1)
        t.tag_configure("md_td_alt_pad", font=(ui[0], 4),
                        background=theme.TRANSCRIPT_TABLE_ALT_BG, spacing1=0, spacing3=0)
        t.tag_configure("md_td_alt_inner", font=(ui[0], 2),
                        background=theme.TRANSCRIPT_TABLE_ALT_BG, spacing1=0, spacing3=0)
        t.tag_configure("md_trule", font=(ui[0], 2), overstrike=True, overstrikefg=theme.DIVIDER,
                        foreground=bg, spacing1=0, spacing3=0)
        # 无底色间距行（避免污染行内代码底色高度）
        t.tag_configure("md_gap", font=(ui[0], pad_s), spacing1=0, spacing3=0)
        t.tag_configure("md_gap_h_top", font=(ui[0], 9), spacing1=0, spacing3=0)
        t.tag_configure("md_gap_h_bot", font=(ui[0], 4), spacing1=0, spacing3=0)
        t.tag_configure("md_gap_p", font=(ui[0], 6), spacing1=0, spacing3=0)
        t.tag_configure("md_gap_li", font=(ui[0], 3), spacing1=0, spacing3=0)
        t.tag_configure("md_wrap_gap", font=(ui[0], 2), spacing1=0, spacing3=0)
        t.tag_configure("md_code_more", font=small, foreground=theme.CYAN,
                        lmargin1=IND + theme.PAD_S, lmargin2=IND + theme.PAD_S,
                        lmargincolor=bg, rmargincolor=bg, spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("md_code_more_n", font=small, foreground=theme.CYAN,
                        lmargin1=DETAIL_IND + theme.PAD_S, lmargin2=DETAIL_IND + theme.PAD_S,
                        lmargincolor=bg, rmargincolor=bg, spacing1=pad_xs, spacing3=pad_xs)
        t.tag_configure("md_li_num", font=theme.FONT_UI_BOLD, foreground=theme.MUTED)
        t.tag_configure("md_quote_bar", font=ui, foreground=theme.BORDER_HOVER)
        t.tag_configure("md_bold", font=theme.FONT_UI_BOLD, foreground=theme.FG_WHITE)
        t.tag_configure("md_italic", font=italic)
        t.tag_configure("md_code", font=(mono[0], small[1]),
                        foreground=theme.TRANSCRIPT_CODE_FG,
                        background=theme.TRANSCRIPT_CODE_BG)
        t.tag_configure("md_tbl_code", font=(mono[0], small[1]),
                        foreground=theme.TRANSCRIPT_CODE_FG,
                        background=theme.CONTROL_BG)
        t.tag_configure("md_code_path", foreground=theme.CODE_MINT)
        t.tag_configure("md_strike", overstrike=True)
        t.tag_configure("md_link", foreground=theme.CYAN, underline=True)
        t.tag_configure("md_muted", foreground=theme.MUTED)
        t.tag_configure("q_act", font=small, foreground=theme.MUTED)
        # 围栏代码语法高亮色（置于 md_codeblock 之后以覆盖默认前景色）
        t.tag_configure("syn_kw", foreground=theme.TRANSCRIPT_SYN_KW)
        t.tag_configure("syn_str", foreground=theme.TRANSCRIPT_SYN_STR)
        t.tag_configure("syn_cmt", foreground=theme.TRANSCRIPT_SYN_CMT)
        t.tag_configure("syn_fn", foreground=theme.TRANSCRIPT_SYN_FN)
        t.tag_configure("syn_num", foreground=theme.TRANSCRIPT_SYN_NUM)
        for hi_tag in ("tone_success", "tone_warning", "tone_danger",
                       "dt_sign_add", "dt_sign_del", "ai_loc_add", "ai_loc_del", "chg_path"):
            t.tag_raise(hi_tag)
        # 行内状态（优先级最高，必须最后创建 / 抬升）
        t.tag_configure("row_hover", background=theme.HOVER_BG)
        t.tag_configure("act_hover", foreground=theme.FG_WHITE)
        t.tag_configure("chip_hover", foreground=theme.FG_WHITE, background=theme.CONTROL_BG)
        t.tag_configure("act_ok", foreground=theme.SUCCESS)
        t.tag_configure("flash", background=theme.HOVER_BG)
        t.tag_configure("err_flash", background=theme.ERROR_TINT_BG)
        t.tag_configure("find_hit", background=theme.WARNING, foreground=theme.BG)
        t.tag_configure("find_cur", background=theme.ACCENT, foreground=theme.FG_WHITE)
        t.tag_configure("sel_vis", background=theme.TRANSCRIPT_SEL_BG, foreground=theme.FG_WHITE)
        t.tag_raise("sel_vis")

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
        t.tag_configure("ai_head", tabs=(max(100, cw - theme.PAD_M), "right"))
        t.tag_configure("dt_file", tabs=(max(100, cw - theme.PAD_M), "right"))
        t.tag_configure("chg_head", tabs=(max(100, cw - theme.PAD_M), "right"))
        t.tag_configure("chg_row", tabs=(max(100, cw - theme.PAD_M), "right"))
        for d in (0, 1):
            t.tag_configure(f"step_d{d}", tabs=(max(100, cw - theme.PAD_M), "right"))
        for sfx in ("", "_n"):
            t.tag_configure(f"md_lang{sfx}", tabs=(max(100, cw - theme.PAD_M), "right"))
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
    def _flash_status(self, text: str, *, error: bool = False, fg: str | None = None) -> None:
        try:
            color = fg if fg is not None else (theme.ERROR if error else theme.SUCCESS)
            self._status.configure(text=text, fg=color)
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
        self._telemetries = T.compute_turn_telemetries(tr)
        self._file_changes = T.summarize_file_changes(tr)
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
        bub_col = theme.TRANSCRIPT_BUBBLE_BG.get(tr.source, BUBBLE_BG)
        self._txt.tag_configure("q_bub", background=bub_col)
        self._txt.tag_configure("q_pad", background=bub_col)
        self._fill_info(tr)
        self._set_nav_state()
        self._win.update_idletasks()      # 先排版，气泡折行要用文字区的真实宽度
        self._apply_metrics()
        self._outline.set_entries(self._entries, self._turns, icon,
                                  self._on_outline_you, self._on_outline_ai, tr.source,
                                  on_err=self._on_outline_err, telemetries=self._telemetries)
        self._refresh_marker_rail()
        self._start_render()
        self.perf["first_batch"] = time.perf_counter() - self.perf["t0"]

    def _refresh_marker_rail(self) -> None:
        search_keys = self._hit_keys(self._search) if self._search is not None else ()
        self._marker_rail.set_turns(self._turns, self._telemetries, search_keys)

    def _on_rail_jump(self, turn_idx: int) -> None:
        if 0 <= turn_idx < len(self._turns):
            self._goto_turn(turn_idx, flash=True)
            self._lock_outline_to_turn(turn_idx)

    # ------------------------------------------------------------------ 页头信息
    def _chip(self, parent, text: str, icon, command=None, tip: str | None = None):
        fspec = (theme.FONT_MONO[0], theme.FONT_UI_SMALL[1])
        font = tkfont.Font(font=fspec)
        w = font.measure(text) + (24 if icon is not None else 0) + 20
        p = RoundedPill(parent, text=text, icon=icon, width=w, height=22,
                        radius=theme.RADIUS_M, fill=theme.CONTROL_BG, bg=theme.PANEL_2,
                        fg=theme.FG, font=fspec, command=command, outline=theme.BORDER_SUBTLE)
        if tip:
            Tooltip(p, tip)
        return p

    def _fill_info(self, tr) -> None:
        info = self._info
        for w in info.winfo_children():
            w.destroy()
        for w in self._proj_badge_box.winfo_children():
            w.destroy()
        self._reason_lbl.pack_forget()
        if tr is None:
            return
        if tr.cwd:
            proj_name = Path(tr.cwd).name or tr.cwd
            if len(proj_name) > 20:
                proj_name = proj_name[:18] + "…"
            StatusBadge(self._proj_badge_box, text=proj_name, kind="neutral",
                        bg=theme.PANEL_2, radius=theme.RADIUS_M).pack(side="left")

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
            if len(short) > 34:
                short = short[:15] + "…" + short[-17:]
            chip = self._chip(info, short, ui_icon(info, "folder"),
                              command=lambda _p=None: self._copy_text(tr.cwd, "已复制工作目录"),
                              tip=f"{tr.cwd}\n左键复制 · 右键在{FILE_MANAGER_NAME}中打开")
            chip.bind("<Button-3>", lambda e, p=tr.cwd: self._open_path_menu(p, p, e))
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
        cost_str = (f"${tr.cost_usd:,.2f}" if isinstance(tr.cost_usd, (int, float)) and tr.cost_usd >= 1.0
                    else format_value("cost", tr.cost_usd))
        add(lambda: lab(cost_str, fg=theme.FG, tip=cost_tip))
        if tr.warnings:
            add(lambda: lab(f"{len(tr.warnings)} 条提示", fg=theme.WARNING,
                            tip="\n".join(str(w) for w in tr.warnings[:8])))
        # 恢复按钮
        has_cmd = bool(tr.resume_command)
        act_bg = theme.TRANSCRIPT_ACTION_BG if has_cmd else theme.CONTROL_BG
        act_hov = theme.TRANSCRIPT_ACTION_HOVER if has_cmd else theme.CONTROL_BG
        self._resume_btn._base_bg = act_bg
        self._resume_btn._hover_bg = act_hov
        self._resume_btn.configure(state="normal" if has_cmd else "disabled",
                                   bg=act_bg, fg=theme.FG_WHITE if has_cmd else theme.MUTED)
        self._resume_menu_btn._base_bg = act_bg
        self._resume_menu_btn._hover_bg = act_hov
        self._resume_menu_btn.configure(bg=act_bg)
        self._set_btn_state(self._resume_menu_btn, has_cmd)
        if not has_cmd:
            why = ("WorkBuddy 暂无恢复命令" if tr.source == "workbuddy"
                   else "此会话无法恢复（缺少会话 ID 或数据源不支持）")
            self._reason_lbl.configure(text=why)
            self._reason_lbl.pack(fill="x", padx=theme.PAD_L, pady=(0, theme.PAD_S))

    def _set_nav_state(self) -> None:
        total = max(1, len(self._refs))
        cur = min(total, max(1, self._idx + 1)) if self._refs else 0
        self._nav_lbl.configure(text=f"{cur}/{total}" if self._refs else "0/0")
        if len(self._refs) <= 1:
            self._nav_box.pack_forget()
        elif not self._nav_box.winfo_manager():
            self._nav_box.pack(side="right", padx=(theme.PAD_M, 0), after=self._resume_box)
        for b, ok in ((self._prev_btn, self._idx > 0),
                      (self._next_btn, self._idx < len(self._refs) - 1)):
            self._set_btn_state(b, ok)
        ready = self._state == "ready"
        for b in (self._export_btn, self._find_btn, self._opt_btn, self._more_btn,
                  self._expand_btn, self._collapse_btn):
            self._set_btn_state(b, ready)
        if not ready:
            self._resume_btn.configure(state="disabled")
            self._set_btn_state(self._resume_menu_btn, False)

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
        if hasattr(self, "_view_seg") and self._view_seg is not None:
            self._view_seg.set(self._view, notify=False)
        for k, p in self._pills.items():
            on = k == self._view
            p.set_state(fill=theme.CONTROL_BG if on else theme.PANEL,
                        fg=theme.FG_WHITE if on else theme.MUTED)

    def _set_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        self._refresh_pills()
        if self._state == "ready":
            self._rerender_all()

    def _refresh_opt_btn(self) -> None:
        try:
            obg = theme.CONTROL_BG if (self._expand_all or self._show_injected) else theme.PANEL_2
            self._opt_btn._base_bg = obg
            self._opt_btn.configure(bg=obg)
        except tk.TclError:
            pass

    def _set_expand_all(self, v: bool) -> None:
        self._expand_all = v
        self._turn_ov.clear()
        if not v:
            self._step_ov.clear()
        self._save_pref(PREF_EXPAND, v)
        self._refresh_opt_btn()
        if self._state == "ready":
            self._rerender_all()

    def _set_show_injected(self, v: bool) -> None:
        self._show_injected = v
        self._save_pref(PREF_INJECTED, v)
        self._refresh_opt_btn()
        if self._state == "ready":
            self._rerender_all()

    def _toggle_outline(self) -> None:
        self._outline_on = not self._outline_on
        self._save_pref(PREF_OUTLINE, self._outline_on)
        try:
            obg = theme.CONTROL_BG if self._outline_on else theme.PANEL_2
            self._outline_btn._base_bg = obg
            self._outline_btn.configure(bg=obg)
        except tk.TclError:
            pass
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
        self._act_inline = set()
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
        self._path_cache.clear()
        self._telemetries = []
        self._file_changes = []
        self._chg_paths.clear()
        self._marker_rail.clear()
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
                self._act_inline.discard(n)
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
        self._act_inline = set()
        self._rows_cache = None
        self._row_total = 0
        self._restore_turn = restore_turn
        self._render_bpx = self._bubble_px()
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
        if self._render_pos == 0 and self._view == T.VIEW_CHANGES and self._file_changes:
            self._render_changes_summary()
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
            div_n = max(40, (self._cw - theme.PAD_S) // self._rule_w)
            self._ins(" " * div_n + "\n", "divider")
        head_done = False
        eff = self._eff_search()
        step_hits = eff.step_ids if eff is not None else ()
        hidden_fail = 0
        for ri, row in enumerate(rows):
            k = row.kind
            if k != "step" or row.step.id in step_hits:
                self._mark(f"rw:{row.key}")
            if k == "step" and (self._view == T.VIEW_CHANGES or row.step.id in step_hits):
                self._mark(f"st:{row.step.id}")
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
        tel = self._telemetries[turn.index] if turn.index < len(self._telemetries) else None
        ts = None
        if turn.steps:
            mi = turn.steps[0].message_index if hasattr(turn.steps[0], "message_index") else None
            if mi is not None and 0 <= mi < len(tr.messages):
                ts = tr.messages[mi].ts
        if ts is None and turn.final is not None:
            ts = turn.final.ts
        self._ins("\n", "ai_gap_top")
        if icon is not None:
            self._txt.image_create("cur", image=icon, padx=0)
            for tg in (tn, "ai_head"):
                self._txt.tag_add(tg, "cur-1c", "cur")
        name = T._APP_NAMES.get(tr.source, "助手")
        self._ins(("  " if icon is not None else "") + name, "ai_head", "ai_name")
        tm = T.format_message_time(ts)
        if tm:
            self._ins("  " + tm, "ai_head", "ai_meta")
        model = (turn.final.model if turn.final and turn.final.model
                 else (tel.model if tel and tel.model else None))
        if model:
            self._ins("  ·  ", "ai_head", "ai_meta", "ui_chrome")
            self._ins(model, "ai_head", "ai_model", "ui_chrome")
        if tel is not None:
            tok_txt = S.format_tokens(tel.total_tokens) if tel.total_tokens else ""
            cost_txt = _fmt_turn_cost(tel.cost_usd)
            if tok_txt or cost_txt:
                self._ins("  ·  ", "ai_head", "ai_meta", "ui_chrome")
                tel_label = f"{tok_txt} tok" if tok_txt else cost_txt
                if tok_txt and cost_txt:
                    tel_label = f"{tok_txt} tok ({cost_txt})"
                if tel.is_hotspot:
                    tel_label = "▲ " + tel_label
                tel_tags = ("ai_head", "ai_model", "tone_warning", "ui_chrome") if tel.is_hotspot \
                    else ("ai_head", "ai_model", "ui_chrome")
                self._act(tel_label, tel_tags,
                          lambda tl=tel: self._show_turn_telemetry(tl), inline=True)
            if tel.added or tel.removed:
                self._ins("  ·  ", "ai_head", "ai_meta", "ui_chrome")
                if tel.added:
                    self._ins(f"+{tel.added}", "ai_head", "ai_loc_add", "ui_chrome")
                    if tel.removed:
                        self._ins(" ", "ai_head", "ai_meta", "ui_chrome")
                if tel.removed:
                    self._ins(f"−{tel.removed}", "ai_head", "ai_loc_del", "ui_chrome")
        if turn.final is not None and turn.final.text.strip():
            raw_md = turn.final.text
            self._ins("\t", "ai_head", "ui_chrome")
            self._copy_act("复制回复", ("ai_head", "ai_meta", "ui_chrome"),
                           raw_md, "已复制本轮回复")
        self._ins("\n", "ai_head")
        self._ins("\n", "ai_gap_bot")

    def _show_turn_telemetry(self, tel: T.TurnTelemetry) -> None:
        parts = [f"第 {tel.turn_index + 1} 轮"]
        if tel.input_tokens or tel.output_tokens:
            parts.append(f"输入 {S.format_tokens(tel.input_tokens)}")
            parts.append(f"输出 {S.format_tokens(tel.output_tokens)}")
        if tel.cache_read_tokens:
            parts.append(f"缓存读 {S.format_tokens(tel.cache_read_tokens)}")
        if tel.cache_write_tokens:
            parts.append(f"缓存写 {S.format_tokens(tel.cache_write_tokens)}")
        c_str = _fmt_turn_cost(tel.cost_usd)
        if c_str:
            parts.append(f"估算 {c_str}")
        if tel.is_hotspot:
            parts.append("热点轮次")
        self._flash_status(" · ".join(parts),
                           fg=theme.WARNING if tel.is_hotspot else theme.FG)

    def _render_changes_summary(self) -> None:
        """在「改动」视图顶部绘出交互式「涉及文件汇总」卡片。"""
        entries = self._file_changes
        if not entries:
            return
        self._cur_rt = -2
        self._ctx = ("chg:summary", "ui_chrome")
        self._any_rows = True
        total_add = sum(e.added for e in entries)
        total_rem = sum(e.removed for e in entries)
        total_edits = sum(e.edit_count for e in entries)
        rework_files = sum(1 for e in entries if e.edit_count >= 3)
        self._ins("\n", "chg_gap")
        self._ins(f"  涉及文件汇总 · {len(entries)} 个文件", "chg_head")
        self._ins("\t", "chg_head")
        self._ins(f"+{total_add}", "chg_head", "ai_loc_add")
        self._ins("  ", "chg_head")
        self._ins(f"−{total_rem}", "chg_head", "ai_loc_del")
        self._ins(f"  ·  共 {total_edits} 次修改", "chg_head", "ai_meta")
        if rework_files:
            self._ins(f"（{rework_files} 个文件多次返工）", "chg_head", "tone_warning")
        self._ins("  \n", "chg_head")
        sep_n = max(40, (self._cw - IND - theme.PAD_M) // self._rule_w)
        self._ins(" " * sep_n + "\n", "chg_sep")
        fold_limit = 12
        show_all = "chg:all" in self._open_text
        shown = entries if show_all else entries[:fold_limit]
        cwd = self._tr.cwd if self._tr else None
        for idx, ent in enumerate(shown):
            short = S.shorten_path(ent.path, cwd)
            resolved = ent.path
            if cwd and not Path(ent.path).is_absolute() and not str(cwd).startswith(("\\\\", "//")):
                try:
                    cand = Path(cwd) / ent.path
                    if cand.exists():
                        resolved = str(cand)
                except OSError:
                    pass
            row_tag = f"chgf:{idx}"
            self._chg_paths[row_tag] = (ent.path, resolved)

            def draw_file(e=ent, sh=short, rt=row_tag) -> None:
                self._ins("  " + sh, "chg_row", "chg_path", rt)
                if e.edit_count >= 3:
                    self._ins(f"  返工 {e.edit_count} 次", "chg_row", "tone_warning", rt)
                elif e.edit_count > 1:
                    self._ins(f"  {e.edit_count} 次修改", "chg_row", "ai_meta", rt)
                self._ins("\t", "chg_row", rt)
                if e.added:
                    self._ins(f"+{e.added}", "chg_row", "ai_loc_add", rt)
                    if e.removed:
                        self._ins(" ", "chg_row", rt)
                if e.removed:
                    self._ins(f"−{e.removed}", "chg_row", "ai_loc_del", rt)
                if not e.added and not e.removed:
                    self._ins("0", "chg_row", "ai_meta", rt)
                self._ins(f"  ·  第 {e.first_turn + 1} 轮  \n", "chg_row", "ai_meta", rt)

            self._act_block(draw_file, lambda ti=ent.first_turn, sid=ent.first_step_id:
                            self._jump_to_change_step(ti, sid))
        if len(entries) > fold_limit:
            rem = len(entries) - fold_limit
            label = "  ▴ 收起文件列表" if show_all else f"  ▾ 展开全部 {len(entries)} 个文件（还有 {rem} 个）"
            self._act(label, ("chg_more",), self._toggle_changes_all,
                      trailing="\n", trailing_tags=("chg_more",), inline=True)
        self._ins("\n", "chg_gap")
        self._ctx = ()

    def _toggle_changes_all(self) -> None:
        if "chg:all" in self._open_text:
            self._open_text.discard("chg:all")
        else:
            self._open_text.add("chg:all")
        if self._state == "ready":
            self._rerender_all()

    def _jump_to_change_step(self, turn_idx: int, step_id: str) -> None:
        if not (0 <= turn_idx < len(self._turns)):
            return
        self._ensure_rendered(turn_idx)
        if not self._step_ov.get(step_id):
            self._step_ov[step_id] = True
            self._rerender_turn(turn_idx)
        mark = f"st:{step_id}"
        if mark in self._txt.mark_names():
            self._scroll_to_mark(mark, fallback_turn=turn_idx, flash=True)
        else:
            self._goto_turn(turn_idx, flash=True)
        self._lock_outline_to_turn(turn_idx)

    def _msg_text_of(self, mi: int) -> str:
        m = self._tr.messages[mi]
        return T.message_text(m)

    def _render_question(self, turn, row) -> None:
        q = turn.question
        tm = T.format_message_time(q.ts)
        head_prefix = f"你 · {tm}" if tm else "你"
        self._ins("\n", "q_gap_top")
        self._ins(head_prefix, "q_meta", "ui_chrome")
        if q.text and q.text.strip():
            raw_q = q.text
            self._ins(" · ", "q_meta", "ui_chrome")
            self._copy_act("复制", ("q_meta", "q_act", "ui_chrome"),
                           raw_q, "已复制提问")
        self._ins("\n", "q_meta")
        text = q.text.strip("\n")
        key = f"q:{turn.key}"
        folded = False
        if len(text) > QUESTION_FOLD_CHARS and not (row.force_expanded or key in self._open_text):
            text, folded = text[:QUESTION_FOLD_CHARS], True
        pad_str = "   "
        pad_x = max(theme.PAD_M, self._bfont.measure(pad_str))
        lines = wrap_text(self._bfont, text or " ", self._render_bpx - 2 * pad_x)
        text_w = max(self._bfont.measure(ln) for ln in lines)
        box_w = min(self._render_bpx, max(48, text_w + 2 * pad_x))
        box_right = max(240, self._cw - theme.PAD_S)
        box_left = max(IND, box_right - box_w)
        self._qb_seq += 1
        qt = f"qb:{self._qb_seq}"
        self._txt.tag_configure(qt, lmargin1=box_left, lmargin2=box_left,
                                rmargin=theme.PAD_S, lmargincolor=theme.BG,
                                rmargincolor=theme.BG, wrap="none")
        self._ins(" \n", qt, "q_pad")
        for ln in lines:
            self._ins(pad_str + ln + "\n", qt, "q_line", "q_bub")
        self._ins(" \n", qt, "q_pad")
        if folded:
            n = len(q.text)
            self._act(f"展开全文（共 {n} 字）", ("q_more", "ui_chrome"), lambda k=key, ti=turn.index:
                      self._toggle_text(k, ti), trailing="\n", trailing_tags=("q_more", "ui_chrome"),
                      inline=True)
        for i, im in enumerate(q.images):
            self._ins(f"[{im.alt or S.t('image.alt', index=i + 1)}]\n", "q_meta")

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
        parts = text.split(" · ")
        lead = " · ".join(parts[:2]) if len(parts) >= 2 else text
        rest = (" · " + " · ".join(parts[2:])) if len(parts) > 2 else ""

        def draw() -> None:
            self._ins("▾ " if row.expanded else "▸ ", "tl_head")
            self._ins(lead, "tl_head", "tl_bold")
            if err and err in rest:
                a, b = rest.split(err, 1)
                self._ins(a, "tl_head")
                self._ins(err, "tl_head", "tl_err")
                self._ins(b + "\n", "tl_head")
            else:
                self._ins(rest + "\n", "tl_head")

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
        # Windows GDI 下 U+23FA「⏺」会回退到 Segoe UI Emoji 绘出深灰键帽方框，统一规整为纯几何圆点
        if glyph == "⏺":
            glyph = "●"
        depth = 1 if row.depth else 0
        font_tag = "step_mono" if mono else "step_ui"
        base = (f"step_d{depth}",)
        tone_tag = f"tone_{tone}"
        lm = IND + depth * theme.PAD_L * 2
        lead_px = self._sfont.measure("│ ") + self._bfont.measure(glyph + " ")
        meta_px = (self._sfont.measure("   " + meta) + theme.PAD_M) if meta else 0
        avail_px = max(120, self._cw - lm - theme.PAD_M - lead_px - meta_px)
        t_font = self._mfont if mono else self._bfont
        raw_title = title.replace("\n", " ")
        title_budget = int(avail_px * 0.68) if (detail and t_font.measure(raw_title) > avail_px * 0.68) else avail_px
        clipped = _fit_px(t_font, raw_title, title_budget)
        used_px = t_font.measure(clipped)
        det_str = ""
        if detail:
            rem_px = avail_px - used_px - self._sfont.measure("  ")
            if rem_px >= 36:
                det_str = _fit_px(self._sfont, detail.replace("\n", " "), rem_px)

        def line() -> None:
            self._ins("│ ", *base, "step_rail")
            self._ins(glyph + " ", *base, tone_tag)
            if not mono and " " in clipped and step.kind in ("tool", "merged"):
                verb, target = clipped.split(" ", 1)
                self._ins(verb + " ", *base, "step_verb")
                self._ins(target, *base, "step_mono")
            else:
                self._ins(clipped, *base, font_tag)
            if det_str:
                self._ins("  " + det_str, *base, "step_detail")
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
            self._md(step.text, note=True, turn_key=f"{turn.key}:{step.id}", turn_idx=turn.index)
            return
        if step.kind == "thinking":
            b = step.block
            th_key = ("think", step.id)
            full_think = self._content.get(th_key)
            pending_think = th_key in self._content_pending
            raw_body = full_think if isinstance(full_think, str) else (b.text or "")
            text = (b.summary + "\n" if b.summary else "") + raw_body
            lines = text.strip("\n").split("\n")
            show_all = th_key in self._show_all
            limit = CODE_MAX_LINES if show_all else 60
            self._dt_lines(lines[:limit], "dt_text")
            if pending_think:
                self._ins("正在读取完整思考…\n", "dt_label")
            elif not show_all and (len(lines) > 60 or b.full is not None):
                more = len(lines) - 60
                lbl = f"… 展开完整思考（还有 {more} 行）" if more > 0 else "… 展开完整思考"
                self._act(lbl, ("dt_more", "ui_chrome"),
                          lambda s=step, blk=b, ti=turn.index: self._show_all_thinking(s, blk, ti),
                          trailing="\n", trailing_tags=("dt_more", "ui_chrome"), inline=True)
            elif show_all and (len(lines) > 60 or b.full is not None):
                self._act("▴ 收起思考", ("dt_more", "ui_chrome"),
                          lambda s=step, ti=turn.index: self._collapse_thinking(s, ti),
                          trailing="\n", trailing_tags=("dt_more", "ui_chrome"), inline=True)
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
            inp_key = ("inp", call.id)
            full_inp = self._content.get(inp_key)
            pending_inp = inp_key in self._content_pending
            trunc_inp = (not isinstance(full_inp, str)) and (
                len(call.input_preview or "") < call.input_total_len and call.input_full is not None
            )
            if call.kind == S.KIND_SHELL:
                cmd = self._input_field(call, "command", full_raw=full_inp)
                if cmd and cmd.strip() != call.title.strip():
                    self._ins("命令\n", "dt_label")
                    self._code_lines(("$ " + cmd).split("\n"), "dt_code")
                if pending_inp:
                    self._ins("正在读取完整参数…\n", "dt_label")
                elif trunc_inp:
                    self._act("… 展开完整命令参数", ("dt_more", "ui_chrome"),
                              lambda c=call, ti=turn.index: self._show_all_input(c, ti),
                              trailing="\n", trailing_tags=("dt_more", "ui_chrome"), inline=True)
            if call.diff or call.kind in (S.KIND_EDIT, S.KIND_WRITE):
                self._render_diff(turn, call)
            elif call.kind in (S.KIND_MCP, S.KIND_OTHER, S.KIND_AGENT, S.KIND_WEB, S.KIND_ASK,
                               S.KIND_TODO) or (res is None and call.kind not in
                                                 (S.KIND_SHELL, S.KIND_READ, S.KIND_SEARCH)):
                args = self._pretty_args(call, full_raw=full_inp)
                if args:
                    self._ins("参数\n", "dt_label")
                    self._code_lines(args.split("\n"), "dt_code")
                if pending_inp:
                    self._ins("正在读取完整参数…\n", "dt_label")
                elif trunc_inp:
                    self._act("… 展开完整参数", ("dt_more", "ui_chrome"),
                              lambda c=call, ti=turn.index: self._show_all_input(c, ti),
                              trailing="\n", trailing_tags=("dt_more", "ui_chrome"), inline=True)
                elif inp_key in self._show_all and isinstance(full_inp, str):
                    self._act("▴ 收起参数", ("dt_more", "ui_chrome"),
                              lambda c=call, ti=turn.index: self._collapse_input(c, ti),
                              trailing="\n", trailing_tags=("dt_more", "ui_chrome"), inline=True)
        if res is not None:
            self._render_output(turn, row, step, res)
        if step.result is not None and step.result.images:
            for im in step.result.images:
                kb = max(1, (im.size or 0) // 1024)
                self._ins(f"[图片 · {im.media_type} · {kb}KB]\n", "dt_label")

    def _show_all_thinking(self, step, block, turn_idx: int) -> None:
        th_key = ("think", step.id)
        self._show_all.add(th_key)
        if block.full is not None:
            self._load_ref(th_key, turn_idx, lambda: T.read_content(block.full, PAGE_CHARS),
                           block.full.kind in _FAST_REFS)
        self._rerender_turn(turn_idx)

    def _collapse_thinking(self, step, turn_idx: int) -> None:
        self._show_all.discard(("think", step.id))
        self._rerender_turn(turn_idx)

    def _show_all_input(self, call, turn_idx: int) -> None:
        inp_key = ("inp", call.id)
        self._show_all.add(inp_key)
        if call.input_full is not None:
            self._load_ref(inp_key, turn_idx, lambda: T.read_content(call.input_full, PAGE_CHARS),
                           call.input_full.kind in _FAST_REFS)
        self._rerender_turn(turn_idx)

    def _collapse_input(self, call, turn_idx: int) -> None:
        inp_key = ("inp", call.id)
        self._show_all.discard(inp_key)
        self._content.pop(inp_key, None)
        self._rerender_turn(turn_idx)

    def _code_lines(self, lines, tag: str) -> None:
        extra = 0
        if len(lines) > CODE_MAX_LINES:
            extra, lines = len(lines) - CODE_MAX_LINES, lines[:CODE_MAX_LINES]
        for ln in lines:
            self._ins(" " + (ln or " ") + " \n", tag)
        if extra:
            self._ins(f"… 还有 {extra} 行\n", "dt_label")

    def _input_field(self, call, key: str, *, full_raw: str | None = None):
        raw = full_raw if isinstance(full_raw, str) else None
        if raw is None:
            if len(call.input_preview) < call.input_total_len:
                return None
            raw = call.input_preview
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        v = obj.get(key) if isinstance(obj, dict) else None
        return v if isinstance(v, str) else None

    def _pretty_args(self, call, *, full_raw: str | None = None) -> str:
        raw = full_raw if isinstance(full_raw, str) else (call.input_preview or "")
        if not raw:
            return ""
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except ValueError:
            is_trunc = (not isinstance(full_raw, str)) and len(raw) < call.input_total_len
            return raw + ("\n…（参数已截断）" if is_trunc else "")

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

    def _render_diff_file_head(self, path: str, file_lines: list, raw_diff: str) -> None:
        self._ins(S.shorten_path(path, self._tr.cwd), "dt_file")
        new_code = T.extract_diff_new_code(file_lines)
        self._ins("\t", "dt_file", "ui_chrome")
        if new_code.strip():
            self._copy_act("复制新代码", ("dt_file", "dt_act", "ui_chrome"),
                           new_code, "已复制改动后代码")
            self._ins(" · ", "dt_file", "dt_act", "ui_chrome")
        self._copy_act("复制 Diff  ", ("dt_file", "dt_act", "ui_chrome"),
                       raw_diff, "已复制 Diff")
        self._ins("\n", "dt_file")

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
        # 每个文件的 +a −r（多文件先列清单）与按文件切分的行集
        sums, cur = {}, None
        file_blocks: dict[str, list] = {}
        for ln in lines:
            if ln.type == "file":
                cur = ln.path
                sums.setdefault(cur, [0, 0])
                file_blocks.setdefault(cur, [])
            elif cur is not None:
                file_blocks[cur].append(ln)
                if ln.type == "add":
                    sums[cur][0] += 1
                elif ln.type == "del":
                    sums[cur][1] += 1
        if len(files) > 1 or len(sums) > 1:
            self._ins("改动\n", "dt_label")
            for p, (a, r) in sums.items():
                self._ins(f"└ {S.shorten_path(p, self._tr.cwd)} (+{a} −{r})\n", "dt_file")
        if files and not any(ln.type == "file" for ln in lines):
            self._render_diff_file_head(files[0]["path"], lines, text)
        shown, extra = 0, 0
        for ln in lines:
            if shown >= DIFF_MAX_LINES and ln.type in ("add", "del", "context"):
                extra += 1
                continue
            if ln.type == "file":
                self._render_diff_file_head(ln.path, file_blocks.get(ln.path, lines), text)
            elif ln.type == "gap":
                self._ins("⋯\n", "dt_gap")
            else:
                shown += 1
                on = "" if ln.old_no is None else str(ln.old_no)
                nn = "" if ln.new_no is None else str(ln.new_no)
                tag = {"add": "dt_add", "del": "dt_del"}.get(ln.type, "dt_ctx")
                self._ins(f"{on:>4} {nn:>4} ", tag, "dt_ln", "ui_chrome")
                sign = {"add": "+", "del": "-"}.get(ln.type, " ")
                self._ins(sign, tag,
                          {"add": "dt_sign_add", "del": "dt_sign_del"}.get(ln.type, "dt_ln"),
                          "ui_chrome")
                self._ins(" ", tag, "ui_chrome")
                self._ins(ln.text + "\n", tag)
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
        self._md(turn.final.text, turn_key=turn.key, turn_idx=turn.index)

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
    def _md(self, text: str, note: bool = False, *,
            turn_key: str = "", turn_idx: int | None = None) -> None:
        sfx = "_n" if note else ""
        base = DETAIL_IND if sfx else IND
        blocks = MD.parse_blocks(text)
        for bi, b in enumerate(blocks):
            k = b[0]
            if k == "code":
                lang, body = b[1], b[2]
                lt = f"md_lang{sfx}"
                self._ins("  " + (lang or "text"), lt)
                self._ins("\t", lt, "ui_chrome")
                self._copy_act("复制  ", (lt, "md_muted", "ui_chrome"),
                               body, "已复制代码")
                self._ins("\n", lt)
                self._md_code_block(body, lang, sfx,
                                    cb_key=f"cb:{turn_key}:{bi}" if turn_key else "",
                                    turn_idx=turn_idx)
                self._ins("\n", "md_gap")
            elif k == "h":
                lv = b[1]
                if bi > 0:
                    self._ins("\n", "md_gap_h_top")
                self._md_inline(b[2], f"md_h{lv}{sfx}", base_x=base,
                                font=self._hfonts.get(lv, self._bfont_bold))
                self._ins("\n", "md_gap_h_bot")
            elif k == "li":
                dp, marker, body_text = b[1], b[2], b[3]
                is_bullet = marker == "•"
                tag = f"md_li{dp}{sfx}" if is_bullet else f"md_oli{dp}{sfx}"
                m_tag = "md_muted" if is_bullet else "md_li_num"
                lead = marker + ("  " if is_bullet else " ")
                lm2 = self._li_lmargin2.get(tag, base + theme.PAD_L * 2)
                self._md_inline(body_text, tag, prefix=(lead, m_tag),
                                cont_tag=f"{tag}_cont", base_x=lm2)
                self._ins("\n", "md_gap_li")
            elif k == "quote":
                tag = f"md_quote{sfx}"
                q_x = base + theme.PAD_M + self._bfont.measure("▎ ")
                self._md_inline(b[1], tag, prefix=("▎ ", "md_quote_bar"),
                                cont_prefix=("▎ ", "md_quote_bar"), base_x=q_x)
                self._ins("\n", "md_gap_li")
            elif k == "hr":
                hr_n = max(30, (self._cw - base - theme.PAD_M) // self._rule_w)
                self._ins(" " * hr_n + "\n", f"md_hr{sfx}")
            elif k == "table":
                self._table(b[1], sfx)
                self._ins("\n", "md_gap")
            else:
                self._md_inline(b[1], f"md_p{sfx}", base_x=base)
                self._ins("\n", "md_gap_p")

    def _md_code_block(self, body: str, lang: str, sfx: str, *,
                       cb_key: str, turn_idx: int | None) -> None:
        lines = body.split("\n")
        total = len(lines)
        expanded = bool(cb_key and cb_key in self._open_text)
        can_toggle = bool(cb_key and turn_idx is not None)
        limit = CODE_MAX_LINES if expanded else CODE_FOLD_LINES
        shown = lines[:limit]
        tag = f"md_codeblock{sfx}"
        more_tag = f"md_code_more{sfx}"
        for ln in shown:
            self._ins("\u00a0", tag)
            if not ln:
                self._ins(" ", tag)
            else:
                for tok, syn in MD.highlight_code_line(ln, lang):
                    if tok:
                        if syn:
                            self._ins(tok, tag, syn)
                        else:
                            self._ins(tok, tag)
            self._ins("\u00a0\n", tag)
        hidden = total - len(shown)
        if hidden > 0:
            if can_toggle and not expanded:
                self._act(f"▾ 展开全部代码（共 {min(total, CODE_MAX_LINES)} 行）",
                          (more_tag, "ui_chrome"),
                          lambda k=cb_key, ti=turn_idx: self._toggle_text(k, ti),
                          trailing="\n", trailing_tags=(more_tag, "ui_chrome"), inline=True)
            else:
                self._ins(f"… 还有 {hidden} 行\n", "dt_label")
        elif expanded and can_toggle and total > CODE_FOLD_LINES:
            self._act("▴ 收起代码", (more_tag, "ui_chrome"),
                      lambda k=cb_key, ti=turn_idx: self._toggle_text(k, ti),
                      trailing="\n", trailing_tags=(more_tag, "ui_chrome"), inline=True)

    def _resolve_path_chip(self, seg: str) -> tuple[str, bool] | None:
        """若行内代码片段像文件路径，返回 ``(目标路径, 本地是否存在)``；跳过 UNC 网络路径。"""
        if seg in self._path_cache:
            return self._path_cache[seg]
        if not MD.looks_like_path(seg):
            self._path_cache[seg] = None
            return None
        base, _ = MD.split_path_loc(seg)
        if base.startswith(("\\\\", "//")):
            self._path_cache[seg] = None
            return None
        cwd = self._tr.cwd if self._tr else None
        resolved = base
        exists = False
        try:
            p = Path(base)
            if p.is_absolute():
                exists = p.exists()
                resolved = str(p)
            elif cwd and not str(cwd).startswith(("\\\\", "//")):
                cand = Path(cwd) / p
                if cand.exists():
                    exists = True
                    resolved = str(cand)
        except OSError:
            exists = False
        res = (resolved, exists)
        self._path_cache[seg] = res
        return res

    def _enrich_md_segs(self, rich_segs: list[tuple[str, tuple, str | None]], *,
                        in_table: bool = False) -> list[tuple[str, tuple, tuple | None]]:
        """把 ``MD.inline_rich`` 转为带交互元数据的三元组 ``(text, flags, act_meta)``。"""
        out: list[tuple[str, tuple, tuple | None]] = []
        code_tag = "md_tbl_code" if in_table else "md_code"
        for seg, flags, url in rich_segs:
            if not seg:
                continue
            if "md_code" in flags:
                fl = tuple(code_tag if f == "md_code" else f for f in flags)
                pr = self._resolve_path_chip(seg)
                if pr is not None:
                    resolved, exists = pr
                    if exists:
                        fl = fl + ("md_code_path",)
                    out.append((seg, fl, ("path", seg, resolved, exists)))
                else:
                    out.append((seg, fl, None))
            elif url and "md_link" in flags:
                out.append((seg, flags, ("url", url)))
            elif url and "md_muted" in flags:
                out.append((seg, flags, ("url", url)))
            else:
                out.append((seg, flags, None))
        return out

    def _measure_rich_segs(self, segs: list[tuple[str, tuple, tuple | None]],
                           default_font: tkfont.Font) -> int:
        total = 0
        for seg, flags, _ in segs:
            if "md_code" in flags or "md_tbl_code" in flags:
                total += self._cfont.measure(f"\u2009{seg}\u2009")
            else:
                f = self._bfont_bold if "md_bold" in flags else default_font
                clean = seg.replace("\ufe0f", "")
                total += f.measure(clean)
        return total

    def _emit_rich_piece(self, seg_txt: str, flags: tuple, meta: tuple | None,
                         *base_tags: str) -> None:
        if meta is None:
            self._ins(seg_txt, *base_tags, *flags)
            return
        kind = meta[0]
        if kind == "url":
            url = meta[1]
            self._act(seg_txt, base_tags + flags,
                      lambda u=url: self._open_url_menu(u), inline=True)
        elif kind == "path":
            raw, resolved, _exists = meta[1], meta[2], meta[3]
            self._act(seg_txt, base_tags + flags,
                      lambda r=raw, p=resolved: self._open_path_menu(r, p), inline=True)
        else:
            self._ins(seg_txt, *base_tags, *flags)

    def _table(self, rows, sfx: str) -> None:
        """富格式表格：像素级量宽 + 专属 tab stop 对齐；仅表头下绘 1px 细线，表体靠
        斑马纹与独立上下内边距行分界；单元格内完整保留行内代码胶囊、粗体、链接与彩色状态符。"""
        if not rows:
            return
        base = DETAIL_IND if sfx else IND
        x0 = base + theme.PAD_S
        gap = theme.PAD_L * 2
        pad_l = "  "
        pad_l_px = self._bfont.measure(pad_l)
        cols = max(len(r) for r in rows)
        norm = [list(r) + [""] * (cols - len(r)) for r in rows]
        rich_grid = [
            [self._enrich_md_segs(
                MD.inline_rich(c.strip(), ("md_bold",) if ri == 0 else ()),
                in_table=True)
             for c in r]
            for ri, r in enumerate(norm)
        ]
        avail = max(200, self._cw - x0 - theme.PAD_M - pad_l_px - theme.PAD_M)
        min_col = max(36, self._bfont_bold.measure("0000"))
        max_col = max(min_col, avail * 2 // 5)
        nat = [
            max(self._bfont_bold.measure("00"),
                max(self._measure_rich_segs(
                    rich_grid[ri][ci],
                    self._bfont_bold if ri == 0 else self._bfont)
                    for ri in range(len(norm))))
            for ci in range(cols)
        ]
        widths = [min(max_col, w) for w in nat]
        budget = max(cols * min_col, avail - gap * (cols - 1))
        rem = budget - sum(widths)
        if rem > 0:
            for ci in sorted(range(cols), key=lambda k: nat[k] - widths[k], reverse=True):
                need = nat[ci] - widths[ci]
                if need <= 0 or rem <= 0:
                    break
                give = min(need, rem)
                widths[ci] += give
                rem -= give
        step = max(2, self._bfont.measure("0") // 2)
        while sum(widths) > budget:
            ci = max(range(cols), key=lambda k: widths[k])
            if widths[ci] <= min_col:
                break
            widths[ci] = max(min_col, widths[ci] - step)

        stops, x = [], x0 + pad_l_px
        for w in widths[:-1]:
            x += w + gap
            stops.append(x)
        table_w = pad_l_px + sum(widths) + gap * max(0, cols - 1) + theme.PAD_M
        rm = max(theme.PAD_M, self._cw - (x0 + table_w))
        self._tbl_seq += 1
        tt = f"tbl{self._tbl_seq}"
        self._txt.tag_configure(tt, lmargin1=x0, lmargin2=x0, rmargin=rm,
                                lmargincolor=theme.BG, rmargincolor=theme.BG,
                                tabs=tuple(stops) or "", wrap="none")
        rule_n = max(1, table_w // self._rule_w)

        for ri, row_segs in enumerate(rich_grid):
            row_font = self._bfont_bold if ri == 0 else self._bfont
            wrapped_cols = [
                self._wrap_md_segments_rich(row_segs[ci], widths[ci], row_font)
                for ci in range(cols)
            ]
            h = max(1, max(len(vc) for vc in wrapped_cols))
            if ri == 0:
                rtag, ptag, itag = "md_th", "md_th_pad", "md_th_inner"
            elif ri % 2 == 1:
                rtag, ptag, itag = "md_td", "md_td_pad", "md_td_inner"
            else:
                rtag, ptag, itag = "md_td_alt", "md_td_alt_pad", "md_td_alt_inner"

            self._ins(" \n", tt, ptag)
            for li in range(h):
                if li > 0:
                    self._ins(" \n", tt, itag)
                self._ins(pad_l, tt, rtag)
                for ci in range(cols):
                    if ci > 0:
                        self._ins("\t", tt, rtag)
                    if li < len(wrapped_cols[ci]):
                        for seg_txt, seg_fl, seg_meta in wrapped_cols[ci][li]:
                            self._emit_rich_piece(seg_txt, seg_fl, seg_meta, tt, rtag)
                self._ins("\n", tt, rtag)
            self._ins(" \n", tt, ptag)
            if ri == 0:
                self._ins(" " * rule_n + "\n", tt, "md_trule")

    def _wrap_md_segments_rich(
        self, segs: list[tuple[str, tuple, tuple | None]], avail_w: int,
        default_font: tkfont.Font,
    ) -> list[list[tuple[str, tuple, tuple | None]]]:
        """按像素宽将富片段预折行为多条视觉行：
        - 行内代码（md_code / md_tbl_code）作为原子块，不半词劈断；
        - 西文单词 / URL / 路径整词换行；
        - 严格遵守中文避头尾（Kinsoku）禁则：右标点不落行首，左括号不孤留行尾；
        - 自动将 ✅ / ☑ / ⚠ / ❌ 规整为矢量几何状态符并赋予语义色。"""
        avail_w = max(32, avail_w)
        # 1) 切分为基础 piece: (text, flags, meta, px, is_space, is_code)
        pieces: list[tuple[str, tuple, tuple | None, int, bool, bool]] = []
        for seg, flags, meta in segs:
            if not seg:
                continue
            if "md_code" in flags or "md_tbl_code" in flags:
                chip = f"\u2009{seg}\u2009"
                pieces.append((chip, flags, meta, self._cfont.measure(chip), False, True))
                continue
            f = self._bfont_bold if "md_bold" in flags else default_font
            clean_seg = seg.replace("\ufe0f", "")
            for tok in re.findall(r"[A-Za-z0-9_./:\\-]+|\s+|.", clean_seg):
                st = MD.STATUS_GLYPHS.get(tok)
                if st is not None:
                    g_ch, g_tone = st
                    t_fl = flags + (g_tone, "md_bold") if "md_bold" not in flags else flags + (g_tone,)
                    pieces.append((g_ch, t_fl, meta, self._bfont_bold.measure(g_ch), False, False))
                else:
                    pieces.append((tok, flags, meta, f.measure(tok), tok.isspace(), False))

        # 2) 避头尾（Kinsoku）粘合：生成不可拆分的复合原子单元 [(pieces_list, total_w, is_space)]
        max_bind = int(avail_w * 0.65)
        units: list[tuple[list[tuple[str, tuple, tuple | None, int, bool, bool]], int, bool]] = []
        open_buf: list[tuple[str, tuple, tuple | None, int, bool, bool]] = []
        open_w = 0
        for pc in pieces:
            txt, _fl, _mt, pw, is_sp, is_cd = pc
            if is_sp:
                if open_buf:
                    units.append((open_buf, open_w, False))
                    open_buf, open_w = [], 0
                units.append(([pc], pw, True))
                continue
            if (not is_cd) and all(ch in MD.KINSOKU_NO_END for ch in txt) and open_w + pw <= max_bind:
                open_buf.append(pc)
                open_w += pw
                continue
            if open_buf and open_w + pw <= max_bind:
                grp = open_buf + [pc]
                gw = open_w + pw
                open_buf, open_w = [], 0
            else:
                if open_buf:
                    units.append((open_buf, open_w, False))
                    open_buf, open_w = [], 0
                grp, gw = [pc], pw
            # 后置禁则标点向前一个非空单元归并
            if ((not is_cd) and all(ch in MD.KINSOKU_NO_START for ch in txt)
                    and units and not units[-1][2]
                    and units[-1][1] + gw <= max_bind):
                prev_grp, prev_w, _ = units[-1]
                units[-1] = (prev_grp + grp, prev_w + gw, False)
            else:
                units.append((grp, gw, False))
        if open_buf:
            units.append((open_buf, open_w, False))

        # 3) 按像素预算折行
        lines: list[list[tuple[str, tuple, tuple | None]]] = []
        cur: list[tuple[str, tuple, tuple | None]] = []
        cur_w = 0

        def push(s: str, fl: tuple, mt: tuple | None) -> None:
            if not s:
                return
            if cur and cur[-1][1] == fl and cur[-1][2] == mt:
                cur[-1] = (cur[-1][0] + s, fl, mt)
            else:
                cur.append((s, fl, mt))

        def flush() -> None:
            nonlocal cur, cur_w
            lines.append(cur)
            cur = []
            cur_w = 0

        for grp, gw, is_sp in units:
            if is_sp:
                if not cur:
                    continue
                if cur_w + gw <= avail_w:
                    for s, fl, mt, *_ in grp:
                        push(s, fl, mt)
                    cur_w += gw
                else:
                    flush()
                continue
            if cur_w + gw <= avail_w:
                for s, fl, mt, *_ in grp:
                    push(s, fl, mt)
                cur_w += gw
            elif cur and gw <= avail_w:
                flush()
                for s, fl, mt, *_ in grp:
                    push(s, fl, mt)
                cur_w = gw
            else:
                # 单个超宽复合单元（如超长 URL 或超长代码串）按片段/字符安全拆分
                for s, fl, mt, pw, _sp, is_cd in grp:
                    if cur_w + pw <= avail_w:
                        push(s, fl, mt)
                        cur_w += pw
                    elif cur and pw <= avail_w:
                        flush()
                        push(s, fl, mt)
                        cur_w = pw
                    elif is_cd:
                        inner = s.strip("\u2009")
                        buf = "\u2009"
                        for ch in inner:
                            cand = buf + ch + "\u2009"
                            if cur and cur_w + self._cfont.measure(cand) > avail_w:
                                if len(buf) > 1:
                                    push(buf + "\u2009", fl, mt)
                                flush()
                                buf = "\u2009" + ch
                            else:
                                buf += ch
                        if len(buf) > 1:
                            tail = buf + "\u2009"
                            push(tail, fl, mt)
                            cur_w += self._cfont.measure(tail)
                    else:
                        f = self._bfont_bold if "md_bold" in fl else default_font
                        for ch in s:
                            cw = f.measure(ch)
                            if cur and cur_w + cw > avail_w:
                                flush()
                            push(ch, fl, mt)
                            cur_w += cw

        if cur or not lines:
            lines.append(cur)
        return lines

    def _wrap_md_segments(self, segs: list[tuple[str, tuple]], avail_w: int,
                          default_font: tkfont.Font) -> list[list[tuple[str, tuple]]]:
        """兼容二元组 ``(text, flags)`` 的预折行入口（单测与纯排演调用）。"""
        rich_in = [(s, fl, None) for s, fl in segs]
        vlines = self._wrap_md_segments_rich(rich_in, avail_w, default_font)
        return [[(s, fl) for s, fl, _ in vl] for vl in vlines]

    def _md_inline(self, text: str, line_tag: str, *,
                   prefix: tuple[str, str] | None = None,
                   cont_prefix: tuple[str, str] | None = None,
                   cont_tag: str | None = None,
                   base_x: int = IND,
                   font: tkfont.Font | None = None) -> None:
        rich_segs = self._enrich_md_segs(MD.inline_rich(text), in_table=False)
        avail_w = max(140, self._cw - base_x - theme.PAD_M)
        vlines = self._wrap_md_segments_rich(rich_segs, avail_w, font or self._bfont)
        for li_idx, vline in enumerate(vlines):
            if li_idx > 0:
                self._ins("\n", "md_wrap_gap")
            t_line = line_tag if li_idx == 0 else (cont_tag or line_tag)
            pf = prefix if li_idx == 0 else cont_prefix
            if pf is not None:
                self._ins(pf[0], t_line, pf[1])
            for seg_txt, flags, meta in vline:
                self._emit_rich_piece(seg_txt, flags, meta, t_line)
            self._ins("\n", t_line)

    # -- 可点击片段 --------------------------------------------------------------
    def _new_act(self, fn, *, inline: bool = False) -> str:
        """登记一个可点击片段：在 cur 处放 ``a:<n>`` mark（左重力，停在片段起点）。"""
        self._act_seq += 1
        name = f"a:{self._act_seq}"
        self._acts[name] = fn
        if inline:
            self._act_inline.add(name)
        self._mark(name)
        return name

    def _act(self, text: str, tags: tuple, fn, *, trailing: str = "",
             trailing_tags: tuple = (), inline: bool = False) -> str:
        name = self._new_act(fn, inline=inline)
        self._ins(text, *tags, "act")
        if inline:
            self._mark(f"ae:{name[2:]}")
        if trailing:
            self._ins(trailing, *trailing_tags, *(() if inline else ("act",)))
        return name

    def _flash_act_ok(self, name: str) -> None:
        t = self._txt
        end_mark = f"ae:{name[2:]}"
        try:
            if name in t.mark_names() and end_mark in t.mark_names():
                start, end = t.index(name), t.index(end_mark)
                t.tag_add("act_ok", start, end)
                self._win.after(
                    1100,
                    lambda s=start, e=end: (
                        t.tag_remove("act_ok", s, e) if not self._closed else None
                    ),
                )
        except tk.TclError:
            pass

    def _copy_act(self, label: str, tags: tuple, payload: str, ok_msg: str) -> str:
        box: list[str] = []

        def _do(p: str = payload, msg: str = ok_msg) -> None:
            self._copy_text(p, msg)
            if box:
                self._flash_act_ok(box[0])

        name = self._act(label, tags, _do, inline=True)
        box.append(name)
        return name

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
        for htag in ("row_hover", "act_hover", "chip_hover"):
            t.tag_remove(htag, "1.0", "end")
        self._hover_act = name
        if name:
            try:
                start = t.index(name)
                if name in self._act_inline:
                    end_mark = f"ae:{name[2:]}"
                    end = t.index(end_mark) if end_mark in t.mark_names() else f"{start} lineend"
                    tnames = t.tag_names(start)
                    htag = "chip_hover" if ("md_code" in tnames or "md_tbl_code" in tnames) else "act_hover"
                    t.tag_add(htag, start, end)
                else:
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
        self._marker_rail.set_viewport(float(first), float(last))
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
        try:
            self._txt.tk.call(self._txt._w, "sync")
        except tk.TclError:
            pass
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
            for y_off in (0, theme.PAD_L + 4, theme.PAD_L * 2 + 8):
                for name in t.tag_names(t.index(f"@0,{y_off}")):
                    if name.startswith("tn:"):
                        return int(name[3:])
        except (tk.TclError, ValueError):
            pass
        return None

    def _on_scrolled(self) -> None:
        self._scroll_after = None
        if self._closed or self._state != "ready":
            return
        if self._txt.tag_ranges("sel"):
            self._on_selection_changed()
        if not self._outline_on:
            return
        if time.monotonic() < self._outline_lock_until:
            return
        n = self._top_turn()
        if n is None or n == self._cur_turn or not self._entries:
            return
        self._cur_turn = n
        keys = [e.turn_index for e in self._entries]
        i = bisect.bisect_right(keys, n) - 1
        self._outline.set_current(max(0, i))

    def _first_content_line(self, start_idx: str, end_idx: str) -> str:
        """跳过轮首 divider 与空白间距行，返回本轮首个真实内容行起点。"""
        t = self._txt
        cur = start_idx
        for _ in range(5):
            tnames = t.tag_names(cur)
            if any(g in tnames for g in ("divider", "q_gap_top", "ai_gap_top")):
                nxt = t.index(f"{cur} lineend+1c")
                if t.compare(nxt, "<", end_idx):
                    cur = nxt
                    continue
            break
        return cur

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
        target = self._first_content_line(str(rng[0]), str(rng[-1]))
        try:
            self._txt.tk.call(self._txt._w, "sync")
        except tk.TclError:
            pass
        self._txt.yview(target)
        self._cur_turn = -1
        self._on_scrolled()
        return True

    def _flash_range(self, a: str, b: str, *, tag: str = "flash") -> None:
        t = self._txt
        for tg in ("flash", "err_flash"):
            t.tag_remove(tg, "1.0", "end")
        t.tag_add(tag, a, b)
        if self._flash_after is not None:
            try:
                self._win.after_cancel(self._flash_after)
            except tk.TclError:
                pass
        self._flash_after = self._win.after(FLASH_MS, lambda: self._clear_flash())

    def _clear_flash(self) -> None:
        self._flash_after = None
        try:
            for tg in ("flash", "err_flash"):
                self._txt.tag_remove(tg, "1.0", "end")
        except tk.TclError:
            pass

    def _lock_outline_to_turn(self, turn_idx: int) -> None:
        self._outline_lock_until = time.monotonic() + 0.35
        self._cur_turn = turn_idx
        if self._entries:
            keys = [e.turn_index for e in self._entries]
            i = bisect.bisect_right(keys, turn_idx) - 1
            self._outline.set_current(max(0, i))

    def _on_outline_you(self, ent) -> None:
        turn = self._turns[ent.turn_index]
        self._ensure_rendered(ent.turn_index)
        self._scroll_to_mark(f"rw:{turn.key}:question", fallback_turn=ent.turn_index, flash=False)
        self._lock_outline_to_turn(ent.turn_index)

    def _on_outline_ai(self, ent) -> None:
        turn = self._turns[ent.turn_index]
        self._ensure_rendered(ent.turn_index)
        t = self._txt
        for name in (f"rw:{turn.key}:final", f"ah:{turn.key}"):
            if name in t.mark_names():
                self._scroll_to_mark(name, fallback_turn=ent.turn_index, flash=False)
                self._lock_outline_to_turn(ent.turn_index)
                return
        self._goto_turn(ent.turn_index, flash=False)
        self._lock_outline_to_turn(ent.turn_index)

    def _on_outline_err(self, ent, pos: int, total: int) -> None:
        ti = ent.turn_index
        self._ensure_rendered(ti)
        t = self._txt
        rng = t.tag_ranges(f"tn:{ti}")
        err_line = None
        if rng:
            s_idx, e_idx = str(rng[0]), str(rng[-1])
            for etag in ("tone_danger", "ev_danger", "tl_err"):
                er = t.tag_nextrange(etag, s_idx, e_idx)
                if er:
                    err_line = str(er[0])
                    break
        if err_line is not None:
            self._scroll_center(err_line)
            self._flash_range(f"{err_line} linestart", f"{err_line} lineend+1c", tag="err_flash")
        else:
            self._goto_turn(ti, flash=False)
        self._lock_outline_to_turn(ti)
        self._flash_status(f"异常轮次 {pos}/{total}", fg=theme.MUTED)

    def _open_url_menu(self, url: str) -> None:
        if not url:
            return
        m = FlatMenu(self._win)
        m.add_command(label="复制链接地址",
                      command=lambda u=url: self._copy_text(u, "已复制链接"),
                      image=ui_icon(self._win, "copy"), compound="left")

        def _open_browser(u: str = url) -> None:
            import webbrowser
            try:
                webbrowser.open(u)
            except Exception:
                self._flash_status("打开链接失败", error=True)

        m.add_command(label="在默认浏览器中打开", command=_open_browser,
                      image=ui_icon(self._win, "export"), compound="left")
        m.tk_popup(self._win.winfo_pointerx(), self._win.winfo_pointery())

    def _open_path_menu(self, raw: str, resolved: str, event=None) -> None:
        target = resolved or raw
        if not target:
            return
        exists = False
        try:
            if not target.startswith(("\\\\", "//")):
                exists = Path(target).exists()
        except OSError:
            exists = False
        m = FlatMenu(self._win)
        m.add_command(label="复制路径",
                      command=lambda p=target: self._copy_text(p, "已复制路径"),
                      image=ui_icon(self._win, "copy"), compound="left")
        m.add_command(label=f"在{FILE_MANAGER_NAME}中定位",
                      command=(lambda p=target: open_in_file_manager(p)) if exists else None,
                      state="normal" if exists else "disabled",
                      image=ui_icon(self._win, "folder"), compound="left")
        x = event.x_root if event is not None else self._win.winfo_pointerx()
        y = event.y_root if event is not None else self._win.winfo_pointery()
        m.tk_popup(x, y)

    def _scroll_to_mark(self, mark: str, *, fallback_turn: int, flash: bool = False) -> None:
        t = self._txt
        if mark not in t.mark_names():
            self._goto_turn(fallback_turn, flash=flash)
            return
        idx = t.index(mark)
        rng = t.tag_ranges(f"tn:{fallback_turn}")
        if rng and mark.endswith(":question"):
            idx = self._first_content_line(idx, str(rng[-1]))
        try:
            t.tk.call(t._w, "sync")
        except tk.TclError:
            pass
        t.yview(idx)
        if flash:
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
                # 视口顶部距本轮起点 ≤ 3 行（含分轮线与顶部间距行）就算「已在本轮开头」，再按 k 去上一轮
                if rng and (int(self._txt.index("@0,0").split(".")[0])
                            - int(str(rng[0]).split(".")[0])) <= 3:
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
        try:
            self._find_btn._base_bg = theme.CONTROL_BG
            self._find_btn.configure(bg=theme.CONTROL_BG)
        except tk.TclError:
            pass
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")

    def _close_find(self) -> None:
        self._findbar.pack_forget()
        try:
            self._find_btn._base_bg = theme.PANEL_2
            self._find_btn.configure(bg=theme.PANEL_2)
        except tk.TclError:
            pass
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
        self._refresh_marker_rail()
        if new is not None and new.total:
            self._goto_hit(0)

    def _highlight(self, start, end) -> None:
        q = self._query
        if not q:
            return
        t = self._txt
        idx, n = start, 0
        pat = r"\n*".join(re.escape(ch) for ch in q) if len(q) > 1 else re.escape(q)
        while n < 5000:
            pos = t.search(pat, idx, stopindex=end, nocase=True, regexp=True, count=self._cnt_var)
            if not pos:
                break
            c = max(1, self._cnt_var.get())
            e = f"{pos}+{c}c"
            if "ui_chrome" not in t.tag_names(pos):
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

    # ------------------------------------------------------------------ 文本选区与右键菜单
    def _on_selection_changed(self, _e=None) -> None:
        """同步浏览器式紧致选区 ``sel_vis``：
        1) 绝不选中每行末尾的 ``\\n``，消除整屏右侧蓝墙；
        2) 跳过所有空白间距行（``_GAP_TAGS``），消除左侧缩进处黑白相间的梳齿条纹；
        3) 自动剔除 ``ui_chrome``（如「复制」「复制回复」按钮），选区纯粹干净。"""
        t = self._txt
        t.tag_remove("sel_vis", "1.0", "end")
        rng = t.tag_ranges("sel")
        if not rng:
            return
        s_first, s_last = str(rng[0]), str(rng[1])
        ln_start = int(s_first.split(".")[0])
        ln_end = int(s_last.split(".")[0])
        if s_last.endswith(".0") and ln_end > ln_start:
            ln_end -= 1
        if ln_end - ln_start > 800:
            top_ln = int(t.index("@0,0").split(".")[0])
            bot_ln = int(t.index(f"@0,{max(200, t.winfo_height())}").split(".")[0])
            lo = max(ln_start, top_ln - 60)
            hi = min(ln_end, bot_ln + 60)
        else:
            lo, hi = ln_start, ln_end
        for ln in range(lo, hi + 1):
            tnames = set(t.tag_names(f"{ln}.0"))
            if tnames & _GAP_TAGS:
                continue
            c0 = s_first if ln == ln_start else f"{ln}.0"
            c1 = s_last if (ln == ln_end and not s_last.endswith(".0")) else f"{ln}.end"
            ln_end_idx = t.index(f"{ln}.end")
            if t.compare(c1, ">", ln_end_idx):
                c1 = ln_end_idx
            if "q_line" in tnames and t.compare(c0, "<", f"{ln}.3"):
                c0 = f"{ln}.3"
            elif "md_codeblock" in tnames or "md_codeblock_n" in tnames:
                if t.compare(c0, "<", f"{ln}.1"):
                    c0 = f"{ln}.1"
                cb_end = t.index(f"{ln}.end-1c")
                if t.compare(c1, ">", cb_end):
                    c1 = cb_end
            if t.compare(c0, "<", c1):
                t.tag_add("sel_vis", c0, c1)
        idx = f"{lo}.0"
        stop = f"{hi}.end"
        while True:
            cr = t.tag_nextrange("ui_chrome", idx, stop)
            if not cr:
                break
            t.tag_remove("sel_vis", cr[0], cr[1])
            idx = str(cr[1])

    def _clean_selected_text(self) -> str:
        """提取纯净选区文本：剔除 UI 按钮、间距空行、气泡/代码缩进占位，并将同段软折行无缝拼回。"""
        t = self._txt
        rng = t.tag_ranges("sel")
        if not rng:
            return ""
        s_first, s_last = str(rng[0]), str(rng[1])
        ln_start = int(s_first.split(".")[0])
        ln_end = int(s_last.split(".")[0])
        if s_last.endswith(".0") and ln_end > ln_start:
            ln_end -= 1
        out_parts: list[str] = []
        for ln in range(ln_start, ln_end + 1):
            tnames = set(t.tag_names(f"{ln}.0"))
            if "md_wrap_gap" in tnames:
                if out_parts and out_parts[-1] == "\n":
                    out_parts.pop()
                continue
            if tnames & _GAP_TAGS:
                continue
            c0 = s_first if ln == ln_start else f"{ln}.0"
            c1 = s_last if (ln == ln_end and not s_last.endswith(".0")) else f"{ln}.end"
            ln_end_idx = t.index(f"{ln}.end")
            if t.compare(c1, ">", ln_end_idx):
                c1 = ln_end_idx
            if "q_line" in tnames and t.compare(c0, "<", f"{ln}.3"):
                c0 = f"{ln}.3"
            elif "md_codeblock" in tnames or "md_codeblock_n" in tnames:
                if t.compare(c0, "<", f"{ln}.1"):
                    c0 = f"{ln}.1"
                cb_end = t.index(f"{ln}.end-1c")
                if t.compare(c1, ">", cb_end):
                    c1 = cb_end
            if t.compare(c0, "<", c1):
                cur = c0
                line_buf: list[str] = []
                while t.compare(cur, "<", c1):
                    cr = t.tag_nextrange("ui_chrome", cur, c1)
                    if not cr:
                        line_buf.append(t.get(cur, c1))
                        break
                    if t.compare(cur, "<", cr[0]):
                        line_buf.append(t.get(cur, cr[0]))
                    cur = str(cr[1])
                piece = "".join(line_buf).replace("\u2009", "")
                is_code_or_diff = bool(tnames & {
                    "md_codeblock", "md_codeblock_n", "dt_add", "dt_del", "dt_ctx", "dt_code",
                })
                if piece or is_code_or_diff:
                    out_parts.append(piece)
                    if ln < ln_end:
                        out_parts.append("\n")
        return "".join(out_parts).strip("\n")

    def _on_copy_event(self, _e=None):
        if self._txt.tag_ranges("sel"):
            self._copy_selection()
            return "break"
        return None

    def _on_text_context(self, e) -> None:
        t = self._txt
        idx = t.index(f"@{e.x},{e.y}")
        tnames = t.tag_names(idx)
        for name in tnames:
            if name.startswith("chgf:") and name in self._chg_paths:
                raw_p, res_p = self._chg_paths[name]
                self._open_path_menu(raw_p, res_p, e)
                return
        turn_idx = None
        for name in tnames:
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
            txt = self._clean_selected_text()
            if txt:
                self._copy_text(txt, "已复制所选")
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
