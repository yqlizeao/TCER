"""「用户提问」口径 SSOT：哪些 user 角色记录是真人发出的提问。

口径移植自 cc-switch 会话阅读器（``session_manager/providers/claude.rs``
``push_user_texts``），并补 TCER 的 IDE 上下文规则：

- **提问** = 真人发出、非系统注入、剥除注入片段后仍有可见文字（或内联图片）的
  user 消息。
- **不是提问**：子代理派发的 prompt（只读主会话，子代理文件 / ``isSidechain``
  行一律不看）、``<system-reminder>`` / IDE 上下文 / 后台任务通知 / 本地命令输出
  等注入、斜杠命令（事件）、用户中断标记（事件）、``!cmd`` 用户命令（工具调用）、
  压缩续接摘要。

指标（``user_msgs`` / ``first_prompt_chars`` / 斜杠 / 纠正计数）、用户消息弹窗、
周报、上传、LLM 回退采样、会话阅读器全部经此判定，勿在各处另写过滤。

Portions of this file are ported from cc-switch
(https://github.com/farion1231/cc-switch), Copyright (c) 2025 Jason Young,
licensed under the MIT License. Source:
src-tauri/src/session_manager/providers/claude.rs (push_user_texts,
is_injected_text, strip_system_reminders).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 用户消息正文截断上限（弹窗/上传/周报共用；与历史口径一致）。
USER_TEXT_MAX = 500

QUESTION = "question"
SLASH = "slash"          # 斜杠命令事件（开新一轮）
ABORTED = "aborted"      # [Request interrupted by user…]
BASH = "bash"            # 用户 !cmd（<bash-input>）
BASH_OUTPUT = "bash_output"
INJECTED = "injected"    # 系统注入（默认不展示、不计数）
COMPACT = "compact"      # 压缩续接摘要
TOOL_RESULT = "tool_result"  # 仅工具结果的 user 记录
SIDECHAIN = "sidechain"  # 子代理内部行
EMPTY = "empty"


@dataclass(frozen=True)
class UserInput:
    kind: str
    text: str = ""        # question: 可见正文；slash: "name args"；bash: 命令；injected: 原文
    images: int = 0
    has_tool_result: bool = False

    @property
    def is_question(self) -> bool:
        return self.kind == QUESTION


_REMINDER_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)
# IDE 插件附带的上下文块（VS Code / JetBrains）：整块是注入，块外文字才是提问。
_IDE_BLOCK_RE = re.compile(r"<(ide_[a-z_]+)>.*?(?:</\1>|\Z)", re.S)
_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)(?:</bash-input>|\Z)", re.S)

_INJECTED_PREFIXES = (
    "<local-command-stdout>", "<local-command-stderr>", "<task-notification>",
)
_BASH_OUTPUT_PREFIXES = ("<bash-stdout>", "<bash-stderr>")
# TCER 补充：无 isCompactSummary 标记的旧版本续接摘要。
_COMPACT_PREFIX = "This session is being continued from a previous conversation"


def strip_injections(text: str) -> str:
    """剥除 ``<system-reminder>`` 与 IDE 上下文块，保留其余可见文字。"""
    if "<" not in text:
        return text.strip()
    text = _REMINDER_RE.sub("", text)
    text = _IDE_BLOCK_RE.sub("", text)
    return text.strip()


def _slash_text(first: str) -> str | None:
    m = _CMD_NAME_RE.search(first)
    if not m or not m.group(1).strip():
        return None
    name = m.group(1).strip()
    a = _CMD_ARGS_RE.search(first)
    args = a.group(1).strip() if a else ""
    return f"{name} {args}" if args else name


def classify_text_blocks(texts: list[str], *, images: int = 0,
                         has_tool_result: bool = False) -> UserInput:
    """对一条 user 记录的文字块按 cc-switch 规则分类（只看首个非空块定类别）。"""
    first = next((t.strip() for t in texts if t and t.strip()), "")
    if not first:
        if images:
            return UserInput(QUESTION, "[图片消息]", images, has_tool_result)
        return UserInput(TOOL_RESULT if has_tool_result else EMPTY,
                         has_tool_result=has_tool_result)
    if first.startswith("[Request interrupted by user"):
        return UserInput(ABORTED, first, 0, has_tool_result)
    if first.startswith("<command-"):
        slash = _slash_text(first)
        if slash is not None:
            return UserInput(SLASH, slash, 0, has_tool_result)
    if first.startswith("<bash-input>"):
        m = _BASH_INPUT_RE.search(first)
        return UserInput(BASH, (m.group(1) if m else "").strip(), 0, has_tool_result)
    if first.startswith(_BASH_OUTPUT_PREFIXES):
        return UserInput(BASH_OUTPUT, first, 0, has_tool_result)
    if "<local-command-caveat>" in first or first.startswith(_INJECTED_PREFIXES):
        return UserInput(TOOL_RESULT if has_tool_result else INJECTED,
                         "" if has_tool_result else "\n\n".join(texts),
                         0, has_tool_result)
    visible = [v for v in (strip_injections(t) for t in texts if t) if v]
    if not visible:
        if images:
            return UserInput(QUESTION, "[图片消息]", images, has_tool_result)
        if has_tool_result:
            return UserInput(TOOL_RESULT, has_tool_result=True)
        return UserInput(INJECTED, "\n\n".join(texts))
    body = "\n\n".join(visible)
    if body.startswith(_COMPACT_PREFIX):
        return UserInput(COMPACT, body, 0, has_tool_result)
    if body == "/clear" or body.startswith("/clear "):
        return UserInput(SLASH, body.lstrip("/"), 0, has_tool_result)
    return UserInput(QUESTION, body, images, has_tool_result)


def classify_claude_record(obj: dict) -> UserInput | None:
    """Claude（及同构的 WorkBuddy 以外）JSONL 行 → UserInput；非 user 行返回 None。"""
    msg = obj.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    if obj.get("isSidechain") is True:
        return UserInput(SIDECHAIN)
    content = msg.get("content")
    texts: list[str] = []
    images = 0
    has_tr = False
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for it in content:
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t == "text":
                v = it.get("text")
                if isinstance(v, str):
                    texts.append(v)
            elif t == "image":
                images += 1
            elif t == "tool_result":
                has_tr = True
    else:
        return UserInput(EMPTY)
    if obj.get("isCompactSummary") is True:
        return UserInput(COMPACT, "\n\n".join(texts), 0, has_tr)
    return classify_text_blocks(texts, images=images, has_tool_result=has_tr)


def clip(text: str, n: int = USER_TEXT_MAX) -> str:
    return text if len(text) <= n else text[:n]


# --------------------------------------------------------------------------- #
# 跨源统一入口：只读主会话（子代理派发 prompt 不是用户消息）
# --------------------------------------------------------------------------- #
def read_user_messages(source: str | None, path: Path | None,
                       session_id: str | None = None) -> list[str]:
    """读取一个会话的真人提问正文（每条 ≤ ``USER_TEXT_MAX`` 字符）。

    所有消费方（弹窗 / 周报 / 上传 / LLM 回退 / 交叉验证 / 术语考古）的唯一入口。
    **只读主会话**：Claude ``subagents/``、omp/pi ``<stem>/`` 子目录、Codex/OpenCode
    子会话都不并入——子代理的 user 消息是 AI 写给 AI 的派发 prompt。
    """
    if path is None:
        return []
    src = (source or "claude").lower()
    path = Path(path)
    if src == "codex":
        from tcer.core import codex_reader
        return codex_reader.read_user_messages(path)
    if src == "opencode":
        if not session_id:
            return []
        from tcer.core import opencode_reader
        return opencode_reader.read_user_messages(path, session_id)
    if src == "omp":
        from tcer.core import omp_reader
        return omp_reader.read_user_messages(path)
    if src == "pi":
        from tcer.core import pi_reader
        return pi_reader.read_user_messages(path)
    if src == "antigravity":
        from tcer.core import antigravity_reader
        return antigravity_reader.read_user_messages(path)
    if src in ("workbuddy", "codebuddy"):
        from tcer.core import workbuddy_reader
        return workbuddy_reader.read_user_messages(path)
    from tcer.core import reader
    try:
        _, main, _ = reader.session_artifacts(path)
    except (OSError, ValueError, IndexError):
        main = path
    if reader.is_subagent(main) or not main.is_file():
        return []
    return reader.read_user_messages(main)
