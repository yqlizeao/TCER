"""S1 语料层：上传的 ``conversation`` → 按说话方分层的 utterance 序列（文档 §5.1）。

只做**纯函数**的切分与清洗；落库与信号计算在 ``pipeline``。

说话方分层（文档 E2）：
- ``user``            人的原话 = 人的理解
- ``assistant``       AI 的转述（只服务人↔AI 通道，不算到人头上）
- ``subagent_prompt`` 主代理派发给子代理的任务描述（AI↔AI 通道）
thinking / tool_result 不入库（体积大且不是沟通）；编辑类工具调用只作为位置标记
（误解信号需要知道「AI 改过文件没有」）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tcer.core.parse_util import is_slash_command

# 注入块：Claude Code 的 system-reminder / 命令回显，Codex 的 environment_context 等。
_INJECTED_BLOCK_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|local-command-stdout|"
    r"local-command-stderr|local-command-caveat|environment_context|user_instructions|"
    r"ide_selection|ide_opened_file|bash-input|bash-stdout|bash-stderr)>"
    r"[\s\S]*?</\1>",
    re.IGNORECASE,
)
_INJECTED_PREFIXES = (
    "# AGENTS.md instructions for",
    "Caveat: The messages below were generated",
    "This session is being continued from a previous conversation",
    "[Request interrupted by user",
)

# 脱敏（先脱敏再落库）。顺序：key → 邮箱 → 证件/卡号 → 手机 → 内网 IP。
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"), "[密钥]"),
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat|xox[abprs])_[A-Za-z0-9_\-]{10,}\b"), "[密钥]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[密钥]"),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S{6,}"), "[密钥]"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[证件号]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[卡号]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "[内网IP]"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "[内网IP]"),
    (re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"), "[内网IP]"),
]

_FENCE_RE = re.compile(r"```[^\n]*\n([\s\S]*?)(?:```|$)")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

MAX_UTTERANCE_CHARS = 4000

# 子代理派发工具（Claude Task/Agent；其余源多为小写 task / spawn_agent）。
SUBAGENT_TOOLS = {"task", "agent", "spawn_agent", "subagent"}
# 编辑类工具（七源常见命名，小写比较）。
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "search_replace",
              "str_replace_editor", "create_file", "ast_edit", "patch"}


def strip_injected(text: str) -> str:
    t = _INJECTED_BLOCK_RE.sub(" ", text or "")
    t = t.strip()
    for p in _INJECTED_PREFIXES:
        if t.startswith(p):
            return ""
    return t


def redact(text: str) -> str:
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


def _code_to_identifiers(code: str) -> str:
    """代码块只保留有信息量的标识符并拆 camel/snake（``useEffect`` → ``use Effect``）。"""
    seen: list[str] = []
    for m in _IDENT_RE.finditer(code):
        w = m.group(0)
        parts = [p for p in _CAMEL_RE.split(w.replace("_", " ")) if p.strip()]
        piece = " ".join(" ".join(parts).split())
        if piece and piece not in seen:
            seen.append(piece)
        if len(seen) >= 60:
            break
    return " ".join(seen)


def clean_text(text: str) -> str:
    t = strip_injected(text)
    if not t:
        return ""
    t = _FENCE_RE.sub(lambda m: " " + _code_to_identifiers(m.group(1)) + " ", t)
    t = redact(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > MAX_UTTERANCE_CHARS:
        t = t[:MAX_UTTERANCE_CHARS] + "…"
    return t


@dataclass
class Item:
    """一个清洗后的对话单元（utterance 或位置标记）。"""
    kind: str                 # user | assistant | subagent_prompt | edit
    block_idx: int
    user_turn: int
    ts: int | None = None
    text: str = ""
    tool: str = ""
    extra: dict = field(default_factory=dict)


def _ts_seconds(v) -> int | None:
    try:
        x = int(v)
    except (TypeError, ValueError):
        return None
    return x // 1000 if x > 10_000_000_000 else x


def split_conversation(blocks: list) -> list[Item]:
    """把 conversation 块切成 Item 序列。

    ``user_turn``：每遇到一段（相邻 user 文本块合并计一次）真实用户输入 +1，
    其后的 AI 输出沿用该轮号；0-based，首个用户消息之前的块记 -1。
    """
    items: list[Item] = []
    turn = -1
    prev_user = False
    for idx, b in enumerate(blocks or []):
        if not isinstance(b, dict):
            continue
        role, typ = b.get("role"), b.get("type")
        ts = _ts_seconds(b.get("ts"))
        if role == "user" and typ == "text":
            raw = b.get("text") or ""
            if is_slash_command(raw.strip()):
                prev_user = False
                continue
            t = clean_text(raw)
            if not t:
                continue
            if not prev_user:
                turn += 1
            prev_user = True
            items.append(Item("user", idx, turn, ts, t))
            continue
        prev_user = False if role != "user" else prev_user
        if role == "assistant" and typ == "text":
            t = clean_text(b.get("text") or "")
            if t:
                items.append(Item("assistant", idx, turn, ts, t))
        elif role == "assistant" and typ == "tool_use":
            name = str(b.get("name") or "")
            low = name.lower()
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            if low in SUBAGENT_TOOLS:
                prompt = inp.get("prompt") or inp.get("description") or inp.get("task") or ""
                t = clean_text(str(prompt))
                if t:
                    items.append(Item("subagent_prompt", idx, turn, ts, t, tool=name,
                                      extra={"subagent_type": inp.get("subagent_type")}))
            elif low in EDIT_TOOLS:
                path = inp.get("file_path") or inp.get("path") or ""
                items.append(Item("edit", idx, turn, ts, tool=name, extra={"path": str(path)}))
    return items
