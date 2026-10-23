"""会话记录阅读器用的轻量 Markdown 解析（纯函数，无 Tk 依赖，可无头单测）。

只覆盖聊天回复里常见的子集：标题、列表（含任务项）、引用、分隔线、围栏代码、表格、
行内的粗体 / 斜体 / 行内码 / 删除线 / 链接 / 图片。超过 ``MARKDOWN_PARSE_LIMIT`` 的长文
退化为「纯文本 + 围栏代码」（对应 cc-switch SessionMarkdown.tsx 的 MARKDOWN_PARSE_LIMIT）。

块（tuple）：
    ("code", lang, body)          围栏代码
    ("h", level, text)            标题（1-6）
    ("li", depth, marker, text)   列表项（marker 为「•」或「1.」；任务项「[ ]」「[x]」）
    ("quote", text)               引用
    ("hr",)                       分隔线
    ("table", rows)               表格（rows: list[list[str]]，首行为表头）
    ("p", text)                   段落行
"""
from __future__ import annotations

import re
import unicodedata

MARKDOWN_PARSE_LIMIT = 65536

_FENCE_OPEN = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def parse_blocks(text: str, limit: int = MARKDOWN_PARSE_LIMIT) -> list[tuple]:
    """把 Markdown 文本切成块；空行不产生块。超长文本只识别围栏代码。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    plain = len(text) > limit
    blocks: list[tuple] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _FENCE_OPEN.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            body: list[str] = []
            i += 1
            while i < n:
                cl = lines[i].strip()
                if cl.startswith(fence[0] * len(fence)) and set(cl) <= {fence[0]}:
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            blocks.append(("code", lang, "\n".join(body)))
            continue
        if plain:
            if line.strip():
                blocks.append(("p", line))
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        hm = _HEADING.match(line)
        if hm:
            blocks.append(("h", len(hm.group(1)), hm.group(2)))
            i += 1
            continue
        if _HR.match(line):
            blocks.append(("hr",))
            i += 1
            continue
        if _is_table_line(line):
            rows: list[list[str]] = []
            while i < n and _is_table_line(lines[i]):
                if not _TABLE_SEP.match(lines[i]):
                    rows.append(_split_row(lines[i]))
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        qm = _QUOTE.match(line)
        if qm:
            blocks.append(("quote", qm.group(1)))
            i += 1
            continue
        lm = _LIST.match(line)
        if lm:
            indent = len(lm.group(1).replace("\t", "    "))
            depth = min(3, indent // 2)
            raw = lm.group(2)
            marker = "•" if raw in ("-", "*", "+") else raw.replace(")", ".")
            body_text = lm.group(3)
            tm = _TASK.match(body_text)
            if tm:
                marker = "[x]" if tm.group(1) in ("x", "X") else "[ ]"
                body_text = tm.group(2)
            blocks.append(("li", depth, marker, body_text))
            i += 1
            continue
        blocks.append(("p", line.strip() if not line.startswith("    ") else line))
        i += 1
    return blocks


# --------------------------------------------------------------------------- #
# 行内
# --------------------------------------------------------------------------- #
_INLINE = re.compile(
    r"(?P<img>!\[(?P<ia>[^\]]*)\]\([^)]*\))"
    r"|(?P<cq>`+)(?P<code>.+?)(?P=cq)"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|(?<![\w*])\*(?P<i>[^\s*](?:[^*]*?[^\s*])?)\*(?![\w*])"
    r"|~~(?P<s>.+?)~~"
    r"|\[(?P<lt>[^\]]+)\]\((?P<lu>[^)\s]+)\)"
    r"|(?P<url>https?://[^\s<>)\]]+)")


def inline_segments(text: str, flags: tuple = (), _depth: int = 0) -> list[tuple[str, tuple]]:
    """行内切片：[(文本, tag 名元组)]；tag ∈ md_bold / md_italic / md_code / md_strike / md_link / md_muted。"""
    out: list[tuple[str, tuple]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], flags))
        pos = m.end()
        if m.group("img") is not None:
            alt = m.group("ia").strip()
            out.append((f"[图片{'：' + alt if alt else ''}]", flags + ("md_muted",)))
        elif m.group("code") is not None:
            out.append((m.group("code"), flags + ("md_code",)))
        elif m.group("b") is not None:
            out.extend(inline_segments(m.group("b"), flags + ("md_bold",), _depth + 1)
                       if _depth < 3 else [(m.group("b"), flags + ("md_bold",))])
        elif m.group("i") is not None:
            out.extend(inline_segments(m.group("i"), flags + ("md_italic",), _depth + 1)
                       if _depth < 3 else [(m.group("i"), flags + ("md_italic",))])
        elif m.group("s") is not None:
            out.append((m.group("s"), flags + ("md_strike",)))
        elif m.group("lt") is not None:
            label, url = m.group("lt"), m.group("lu")
            out.append((label, flags + ("md_link",)))
            if url and url != label:
                out.append((f" ({url})", flags + ("md_muted",)))
        elif m.group("url") is not None:
            out.append((m.group("url"), flags + ("md_link",)))
    if pos < len(text):
        out.append((text[pos:], flags))
    return out


def plain_inline(text: str) -> str:
    """去掉行内 Markdown 标记的纯文本（单行摘要用：折叠态标题不该露出 ``**``）。"""
    return "".join(seg for seg, _ in inline_segments(text))


# --------------------------------------------------------------------------- #
# 表格对齐（等宽，CJK 记 2 列）
# --------------------------------------------------------------------------- #
def display_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def format_table(rows: list[list[str]]) -> list[str]:
    """表格 → 等宽文本行（列用 ``│`` 分隔，表头下一行用 ``─┼─``）。"""
    if not rows:
        return []
    cols = max(len(r) for r in rows)
    norm = [r + [""] * (cols - len(r)) for r in rows]
    widths = [min(40, max(display_width(r[c]) for r in norm)) for c in range(cols)]

    def clip(s: str, w: int) -> str:
        if display_width(s) <= w:
            return s
        out, acc = "", 0
        for ch in s:
            cw = display_width(ch)
            if acc + cw > w - 1:
                break
            out += ch
            acc += cw
        return out + "…"

    def fmt(r):
        cells = []
        for c, w in zip(r, widths):
            c = clip(c, w)
            cells.append(c + " " * (w - display_width(c)))
        return " │ ".join(cells)

    lines = [fmt(norm[0])]
    if len(norm) > 1:
        lines.append("─┼─".join("─" * (w + (0 if k in (0, cols - 1) else 0))
                                for k, w in enumerate(widths)))
        lines.extend(fmt(r) for r in norm[1:])
    return lines
