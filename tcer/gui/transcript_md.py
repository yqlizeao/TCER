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
    ("table", rows)               表格（rows: list[list[str]]，首行为表头；排版见 layout_table）
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


def inline_rich(text: str, flags: tuple = (),
                _depth: int = 0) -> list[tuple[str, tuple, str | None]]:
    """富行内切片：[(文本, tag 名元组, 链接 URL 或 None)]。"""
    out: list[tuple[str, tuple, str | None]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], flags, None))
        pos = m.end()
        if m.group("img") is not None:
            alt = m.group("ia").strip()
            out.append((f"[图片{'：' + alt if alt else ''}]", flags + ("md_muted",), None))
        elif m.group("code") is not None:
            out.append((m.group("code"), flags + ("md_code",), None))
        elif m.group("b") is not None:
            out.extend(inline_rich(m.group("b"), flags + ("md_bold",), _depth + 1)
                       if _depth < 3 else [(m.group("b"), flags + ("md_bold",), None)])
        elif m.group("i") is not None:
            out.extend(inline_rich(m.group("i"), flags + ("md_italic",), _depth + 1)
                       if _depth < 3 else [(m.group("i"), flags + ("md_italic",), None)])
        elif m.group("s") is not None:
            out.append((m.group("s"), flags + ("md_strike",), None))
        elif m.group("lt") is not None:
            label, url = m.group("lt"), m.group("lu")
            out.append((label, flags + ("md_link",), url or None))
            if url and url != label:
                out.append((f" ({url})", flags + ("md_muted",), url))
        elif m.group("url") is not None:
            u = m.group("url")
            out.append((u, flags + ("md_link",), u))
    if pos < len(text):
        out.append((text[pos:], flags, None))
    return out


def inline_segments(text: str, flags: tuple = (), _depth: int = 0) -> list[tuple[str, tuple]]:
    """行内切片：[(文本, tag 名元组)]；tag ∈ md_bold / md_italic / md_code / md_strike / md_link / md_muted。"""
    return [(s, f) for s, f, _ in inline_rich(text, flags, _depth)]


def plain_inline(text: str) -> str:
    """去掉行内 Markdown 标记的纯文本（单行摘要用：折叠态标题不该露出 ``**``）。"""
    return "".join(seg for seg, _ in inline_segments(text))


# --------------------------------------------------------------------------- #
# 表格排版（宽度口径由调用方注入：GUI 传像素量字函数，测试 / 纯文本用显示列）
# --------------------------------------------------------------------------- #
def display_width(s: str) -> int:
    """显示列宽（CJK 记 2 列）——纯文本场景的默认量宽口径。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _wrap_cell(s: str, w: int, measure) -> list[str]:
    """按 ``measure`` 量宽折行；优先在空白处断开，超长词按字符硬断。"""
    out: list[str] = []
    for para in s.split("\n"):
        cur = ""
        for word in re.split(r"(\s+)", para):
            if measure(cur + word) <= w:
                cur += word
                continue
            if word.isspace():
                out.append(cur.rstrip())
                cur = ""
                continue
            if cur.strip():
                out.append(cur.rstrip())
                cur = ""
            for ch in word:
                if cur and measure(cur + ch) > w:
                    out.append(cur)
                    cur = ""
                cur += ch
        out.append(cur.rstrip())
    return out or [""]


def layout_table(rows: list[list[str]], max_width: int, measure=display_width,
                 gap: int = 3, min_col: int | None = None,
                 max_col: int | None = None) -> tuple[list[int], list[tuple[str, list[str]]]]:
    """表格 → ``(列宽, [(种类, 各列文本)])``，种类 ``head`` / ``sep`` / ``body``。

    单元格先去行内 Markdown 标记（``**`` / 反引号不再原样露出）；列宽取内容
    自然宽（单列封顶 ``max_col``），总宽超过 ``max_width`` 时从最宽列起逐步收缩，
    单元格折行而不截断——不丢内容。表头下与各表体行之间插 ``sep`` 分隔线。
    列宽 / 间距与 ``measure`` 同单位（GUI 为像素，用 tab stop 精确对齐）。"""
    if not rows:
        return [], []
    cols = max(len(r) for r in rows)
    norm = [[plain_inline(c).strip() for c in r] + [""] * (cols - len(r)) for r in rows]
    unit = max(1, measure("0"))
    min_col = min_col if min_col is not None else 6 * unit
    max_col = max_col if max_col is not None else max(min_col, max_width * 2 // 5)
    nat = [max(unit, max(measure(r[c]) for r in norm)) for c in range(cols)]
    widths = [min(max_col, w) for w in nat]
    budget = max(cols * min_col, max_width - gap * (cols - 1))
    rem = budget - sum(widths)
    if rem > 0:
        for i in sorted(range(cols), key=lambda k: nat[k] - widths[k], reverse=True):
            need = nat[i] - widths[i]
            if need <= 0 or rem <= 0:
                break
            give = min(need, rem)
            widths[i] += give
            rem -= give
    step = max(1, unit // 2)
    while sum(widths) > budget:
        i = max(range(cols), key=lambda k: widths[k])
        if widths[i] <= min_col:
            break
        widths[i] = max(min_col, widths[i] - step)

    def phys(r) -> list[list[str]]:
        cells = [_wrap_cell(c, w, measure) for c, w in zip(r, widths)]
        h = max(len(c) for c in cells)
        return [[c[li] if li < len(c) else "" for c in cells] for li in range(h)]

    lines: list[tuple[str, list[str]]] = [("head", ln) for ln in phys(norm[0])]
    for r in norm[1:]:
        lines.append(("sep", []))
        lines.extend(("body", ln) for ln in phys(r))
    return widths, lines


# --------------------------------------------------------------------------- #
# 避头尾禁则（Kinsoku）· 状态符规整 · 路径识别 · 围栏代码轻量语法高亮
# --------------------------------------------------------------------------- #
KINSOKU_NO_START = frozenset("，。、；：！？）》」』】〉》),.;:!?]%}")
KINSOKU_NO_END = frozenset("（《「『【〈([{$¥")

# Windows Tk 对彩色/变体 Emoji 会回退为单色空心线框或灰底块；映射为纯几何矢量符并着色
STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "✅": ("✓", "tone_success"),
    "☑": ("✓", "tone_success"),
    "✔": ("✓", "tone_success"),
    "❌": ("✗", "tone_danger"),
    "❎": ("✗", "tone_danger"),
    "⚠": ("⚠", "tone_warning"),
}

_PATH_LOC_RE = re.compile(r"^(.*?)(?::(\d+(?:-\d+)?))?$")
_FILE_EXTS = frozenset({
    "py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "json", "jsonl", "md", "txt",
    "toml", "yaml", "yml", "ini", "cfg", "sh", "bash", "zsh", "ps1", "bat", "cmd",
    "rs", "go", "c", "cc", "cpp", "h", "hpp", "cs", "java", "kt", "swift", "rb", "php",
    "lua", "sql", "db", "sqlite", "html", "css", "scss", "svg", "png", "jpg", "jpeg",
    "webp", "gif", "pdf", "xml", "proto", "lock", "env", "asar", "exe", "dll",
})


def split_path_loc(s: str) -> tuple[str, str | None]:
    """拆分 ``path/to/file.py:10-25`` → ``('path/to/file.py', '10-25')``。"""
    t = s.strip()
    m = _PATH_LOC_RE.match(t)
    if not m:
        return t, None
    base, loc = m.group(1), m.group(2)
    # 避免把 Windows 盘符 C: 误拆成行号
    if len(base) == 1 and base.isalpha() and loc is not None:
        return t, None
    return base, loc


def looks_like_path(s: str) -> bool:
    """判断行内代码片段是否像本地文件/目录路径（排除 URL、版本号、函数调用、命令行参数）。"""
    t = s.strip()
    if not t or len(t) > 240 or any(ch in t for ch in (" ", "\t", "\n", "(", ")", "<", ">", "=")):
        return False
    if "://" in t or t.startswith(("-", "@", "#")):
        return False
    base, _ = split_path_loc(t)
    if re.fullmatch(r"v?\d+(?:\.\d+)+", base):
        return False
    norm = base.replace("\\", "/")
    if "/" in norm:
        parts = [p for p in norm.split("/") if p]
        if not parts:
            return False
        # 排除纯路由端点如 /v2/ 或 /models（除非有已知扩展名或盘符/相对前缀）
        last = parts[-1]
        ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
        if ext in _FILE_EXTS:
            return True
        if re.match(r"^[A-Za-z]:/", norm) or norm.startswith(("./", "../", "~/")):
            return True
        if not norm.startswith("/") and len(parts) >= 2 and any("." in p for p in parts):
            return True
        return norm.endswith("/") and not norm.startswith("/v")
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        return bool(stem) and ext.lower() in _FILE_EXTS
    return False


_KEYWORDS = frozenset({
    # Python / JS / TS / Rust / Go / C++ / Shell / SQL 高频关键字并集
    "def", "class", "return", "yield", "import", "from", "as", "if", "elif", "else",
    "for", "while", "break", "continue", "pass", "try", "except", "finally", "raise",
    "with", "async", "await", "lambda", "global", "nonlocal", "assert", "del", "in",
    "is", "not", "and", "or", "const", "let", "var", "function", "export", "default",
    "interface", "type", "enum", "implements", "extends", "new", "typeof", "instanceof",
    "switch", "case", "throw", "catch", "fn", "pub", "mod", "use", "impl", "struct",
    "trait", "mut", "match", "where", "crate", "super", "move", "package", "func",
    "defer", "go", "chan", "select", "void", "int", "float", "double", "bool", "char",
    "auto", "static", "inline", "virtual", "override", "namespace", "template",
    "typename", "include", "then", "fi", "do", "done", "esac", "echo", "local",
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "UPDATE", "DELETE", "JOIN", "LEFT",
    "INNER", "GROUP", "ORDER", "BY", "LIMIT", "CREATE", "TABLE", "ALTER", "DROP",
})
_CONSTANTS = frozenset({
    "True", "False", "None", "true", "false", "null", "undefined", "NaN", "Infinity",
    "self", "cls", "this", "Self", "Ok", "Err", "Some",
})

_SYN_RE = re.compile(
    r'(?P<str>"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)'
    r"|(?P<cmt>//.*$|#(?!!).*$|--\s.*$)"
    r"|(?P<dec>@[A-Za-z_][\w.]*)"
    r"|(?P<num>\b0[xX][0-9a-fA-F_]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
    r"|(?P<id>\b[A-Za-z_]\w*\b)(?P<call>(?=\s*\())?"
)
_JSON_RE = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])*")(?=\s*:)'
    r'|(?P<str>"(?:\\.|[^"\\])*")'
    r"|(?P<num>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b(?:true|false|null)\b)"
)


def highlight_code_line(line: str, lang: str = "") -> list[tuple[str, str | None]]:
    """零依赖单行语法着色切片：返回 ``[(片段, tag_or_None)]``，拼接后恒等于原 ``line``。"""
    if not line:
        return [("", None)]
    lg = (lang or "").strip().lower()
    if lg in ("text", "txt", "plain", "plaintext"):
        return [(line, None)]
    if lg in ("diff", "patch"):
        if line.startswith(("+++", "---")):
            return [(line, "syn_cmt")]
        if line.startswith("@@"):
            return [(line, "md_link")]
        if line.startswith("+"):
            return [(line, "dt_sign_add")]
        if line.startswith("-"):
            return [(line, "dt_sign_del")]
        return [(line, None)]
    if lg in ("json", "jsonl", "jsonc"):
        out: list[tuple[str, str | None]] = []
        pos = 0
        for m in _JSON_RE.finditer(line):
            if m.start() > pos:
                out.append((line[pos:m.start()], None))
            pos = m.end()
            if m.group("key") is not None:
                out.append((m.group("key"), "syn_kw"))
            elif m.group("str") is not None:
                out.append((m.group("str"), "syn_str"))
            else:
                out.append((m.group("num"), "syn_num"))
        if pos < len(line):
            out.append((line[pos:], None))
        return out or [(line, None)]

    out = []
    pos = 0
    for m in _SYN_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], None))
        pos = m.end()
        if m.group("str") is not None:
            out.append((m.group("str"), "syn_str"))
        elif m.group("cmt") is not None:
            out.append((m.group("cmt"), "syn_cmt"))
        elif m.group("dec") is not None:
            out.append((m.group("dec"), "syn_fn"))
        elif m.group("num") is not None:
            out.append((m.group("num"), "syn_num"))
        else:
            word = m.group("id")
            if word in _KEYWORDS:
                out.append((word, "syn_kw"))
            elif word in _CONSTANTS:
                out.append((word, "syn_num"))
            elif m.group("call") is not None:
                out.append((word, "syn_fn"))
            else:
                out.append((word, None))
    if pos < len(line):
        out.append((line[pos:], None))
    return out or [(line, None)]
