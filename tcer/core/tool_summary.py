"""会话记录阅读器的工具层：工具名归一化 / 标题提炼 / 步骤标题 / 合并 / 轮摘要。

纯函数、无 Tk 依赖。数据类（ToolCallBlock / ToolStep / SessionTurn …）定义在
:mod:`tcer.core.transcript`，这里按鸭子类型读取属性，仅在 :func:`merge_steps`
内部懒导入 ``MergedStep``，避免循环依赖。

Portions of this file are ported from cc-switch
(https://github.com/farion1231/cc-switch), Copyright (c) 2025 Jason Young,
licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
Sources:
  - src/components/sessions/reader/toolSummary.ts
  - src/components/sessions/reader/agentStyles.ts
  - src/components/sessions/reader/i18n.ts (中文文案)
  - src-tauri/src/session_manager/providers/blocks.rs
    (normalize_tool / refine_shell_kind / title_* / preview / line_change_counts)
  - src/components/sessions/reader/SessionEventRow.tsx (describeEvent)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# 常量与枚举（均为字符串，便于 JSON 化 / GUI 直接比较）
# --------------------------------------------------------------------------- #
KIND_SHELL = "shell"
KIND_READ = "read"
KIND_SEARCH = "search"
KIND_EDIT = "edit"
KIND_WRITE = "write"
KIND_WEB = "web"
KIND_MCP = "mcp"
KIND_AGENT = "agent"
KIND_ASK = "ask"
KIND_TODO = "todo"
KIND_OTHER = "other"
TOOL_KINDS = (KIND_SHELL, KIND_READ, KIND_SEARCH, KIND_EDIT, KIND_WRITE, KIND_WEB,
              KIND_MCP, KIND_AGENT, KIND_ASK, KIND_TODO, KIND_OTHER)

ST_SUCCESS = "success"
ST_ERROR = "error"
ST_INTERRUPTED = "interrupted"
ST_PENDING = "pending"
ST_UNKNOWN = "unknown"
TOOL_STATUSES = (ST_SUCCESS, ST_ERROR, ST_INTERRUPTED, ST_PENDING, ST_UNKNOWN)

# 事件种类（EventBlock.kind）
EV_ABORTED = "aborted"
EV_COMPACTION = "compaction"
EV_MODEL_CHANGE = "model_change"
EV_THINKING_LEVEL = "thinking_level"
EV_HOOK = "hook"
EV_PR_LINK = "pr_link"
EV_SLASH = "slash_command"
EV_INFO = "info"
EV_ERROR = "error"
EV_SUB_AGENT = "sub_agent"
EV_OTHER = "other"

PREVIEW_LINES = 12          # 工具结果预览行数
PREVIEW_CHARS = 1200        # 工具结果预览字符数
INPUT_PREVIEW_CHARS = 400   # 工具参数预览字符数
THINKING_PREVIEW_CHARS = 400
TITLE_CHARS = 200
INLINE_TEXT_MAX_CHARS = 8 * 1024   # 注入文本超过此长度只放预览 + 引用
EVENT_PREVIEW_CHARS = 400

# 旧数据从 content 兜底出来的调用 id 前缀（保留以与 cc-switch 对齐；本移植不产生）
LEGACY_CALL_PREFIX = "legacy:"

# --------------------------------------------------------------------------- #
# 文案（cc-switch i18n READER_DEFAULTS 中文）
# --------------------------------------------------------------------------- #
T = {
    "timelineSummary": "执行过程 · {steps} 步",
    "summaryCommands": "{count} 个命令",
    "summaryFiles": "改了 {count} 个文件",
    "summaryErrors": "{count} 个失败",
    "summaryDuration": "耗时 {duration}",   # cc-switch 为 ⏱ 前缀；TCER 禁 emoji 图标
    "summaryTokens": "{tokens} tok",
    "moreFailures": "还有 {count} 个失败",
    "status.success": "成功",
    "status.error": "失败",
    "status.interrupted": "已中断",
    "status.pending": "进行中",
    "status.unknown": "未知",
    "exitCode": "exit {code}",
    "lines": "{count} 行",
    "thinkingRedacted": "思考内容不可见",
    "noFinalReply": "这一轮没有最终回复",
    "turnAborted": "已中断",
    "event.compaction": "上下文已压缩",
    "event.modelChange": "模型切换为 {model}",
    "event.thinkingLevel": "思考强度：{level}",
    "event.hookError": "Hook 执行出错",
    "event.prLink": "已关联 PR #{number}",
    "event.prLinkPlain": "已关联 PR",
    "event.slashCommand": "运行了 {command}",
    "event.subAgent": "子代理 {name}",
    "merged.explored": "查看了 {files} 个文件、搜索 {searches} 次",
    "merged.mcpTimes": "调用 {server} {count} 次",
    "outlineNoReply": "没有回复",
    "outlineAborted": "已中断",
    "image.alt": "图 {index}",
    "you": "你",
    "verb.run": "运行",
    "verb.read": "读取",
    "verb.search": "搜索",
    "verb.edit": "修改",
    "verb.write": "写入",
    "verb.web": "网页",
    "verb.mcp": "MCP",
    "verb.agent": "子代理",
    "verb.ask": "提问",
    "verb.todo": "待办",
    "verb.other": "工具",
    "verb.output": "工具输出",
}


def t(key: str, **kw: Any) -> str:
    s = T.get(key, key)
    return s.format(**kw) if kw else s


# --------------------------------------------------------------------------- #
# 文本小工具（blocks.rs 移植）
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    """等价 Rust ``str::lines()``：按 ``\\n`` 切、去行尾 ``\\r``、末尾空段不计。"""
    if not text:
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def count_lines(text: str) -> int:
    if not text:
        return 0
    n = text.count("\n")
    return n if text.endswith("\n") else n + 1


@dataclass
class Preview:
    text: str
    total_len: int       # 全文字符数
    line_count: int      # 全文行数
    truncated: bool      # 预览是否短于全文（忽略末尾换行）


def preview_with(text: str, max_lines: int, max_chars: int) -> Preview:
    total_len = len(text)
    line_count = count_lines(text)
    end = total_len
    if max_lines > 0:
        if max_lines < 10**9:
            idx = -1
            for _ in range(max_lines):
                idx = text.find("\n", idx + 1)
                if idx < 0:
                    break
            if idx >= 0:
                end = idx
    else:
        end = 0
    if end > max_chars:
        end = max_chars
    truncated = bool(text[end:].rstrip("\r\n")) if end < total_len else False
    kept = text[:end].rstrip("\r\n")
    return Preview(kept, total_len, line_count, truncated)


def preview(text: str) -> Preview:
    """工具结果预览：前 12 行且不超过 1200 字符。"""
    return preview_with(text, PREVIEW_LINES, PREVIEW_CHARS)


def preview_chars(text: str, max_chars: int) -> Preview:
    """只按字符截断（参数、思考正文）。"""
    return preview_with(text, 10**9, max_chars)


def one_line_title(text: str) -> str:
    """截成单行标题：首行、≤200 字符，有后续非空行或被截断时加 ``…``。"""
    trimmed = text.strip()
    lines = _lines(trimmed)
    first = (lines[0] if lines else "").rstrip()
    more = any(ln.strip() for ln in lines[1:])
    title = first[:TITLE_CHARS]
    if more or len(first) > TITLE_CHARS:
        title += "…"
    return title


def line_change_counts(old: str, new: str) -> tuple[int, int]:
    """由替换前后的文本估算 ``(+added, -removed)``：去掉首尾相同行后剩余即增删。"""
    a, b = _lines(old), _lines(new)
    prefix = 0
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += 1
    ra, rb = a[prefix:], b[prefix:]
    suffix = 0
    for x, y in zip(reversed(ra), reversed(rb)):
        if x != y:
            break
        suffix += 1
    return len(b) - prefix - suffix, len(a) - prefix - suffix


def count_diff_lines(diff: str) -> tuple[int, int]:
    """统计 unified diff 里的增删行数，跳过 ``+++`` / ``---`` 文件头。"""
    added = removed = 0
    for line in _lines(diff):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def split_mcp_name(raw_name: str) -> tuple[str, str] | None:
    """拆 ``mcp__<server>__<tool>``；不是这种形式返回 None。"""
    if not raw_name.startswith("mcp__"):
        return None
    rest = raw_name[5:]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def refine_shell_kind(command: str) -> str:
    """Shell 命令按首个程序名细化：rg/grep/fd/find/ls 开头算搜索，其余仍是 shell。"""
    parts = command.split()
    program = parts[0] if parts else ""
    program = program.rsplit("/", 1)[-1]
    return KIND_SEARCH if program in ("rg", "grep", "fd", "find", "ls") else KIND_SHELL


# --------------------------------------------------------------------------- #
# 工具名 → kind
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NormalizedTool:
    kind: str
    server: str | None = None


_CLAUDE_KINDS = {
    "Bash": KIND_SHELL, "Read": KIND_READ,
    "Grep": KIND_SEARCH, "Glob": KIND_SEARCH, "ToolSearch": KIND_SEARCH, "ListAgents": KIND_SEARCH,
    "Edit": KIND_EDIT, "MultiEdit": KIND_EDIT, "NotebookEdit": KIND_EDIT,
    "Write": KIND_WRITE,
    "WebFetch": KIND_WEB, "WebSearch": KIND_WEB,
    "Agent": KIND_AGENT, "Task": KIND_AGENT, "SendMessage": KIND_AGENT, "TaskStop": KIND_AGENT,
    "AskUserQuestion": KIND_ASK, "TodoWrite": KIND_TODO,
}
_CODEX_KINDS = {
    "exec": KIND_SHELL, "exec_command": KIND_SHELL, "shell": KIND_SHELL, "local_shell": KIND_SHELL,
    "write_stdin": KIND_SHELL, "CommandExecution": KIND_SHELL,
    "read_file": KIND_READ,
    "apply_patch": KIND_EDIT, "FileChange": KIND_EDIT,
    "write_file": KIND_WRITE,
    "web_search": KIND_WEB, "web_search_call": KIND_WEB, "open_page": KIND_WEB, "WebSearch": KIND_WEB,
    "McpToolCall": KIND_MCP,
    "spawn_agent": KIND_AGENT, "send_message": KIND_AGENT, "agent_message": KIND_AGENT,
    "SubAgentActivity": KIND_AGENT, "create_thread": KIND_AGENT,
    "request_user_input_async": KIND_ASK, "update_plan": KIND_TODO,
}
_GEMINI_KINDS = {
    "run_shell_command": KIND_SHELL,
    "read_file": KIND_READ, "read_many_files": KIND_READ,
    "grep_search": KIND_SEARCH, "glob": KIND_SEARCH, "list_directory": KIND_SEARCH,
    "search_file_content": KIND_SEARCH,
    "replace": KIND_EDIT, "edit": KIND_EDIT, "write_file": KIND_WRITE,
    "web_fetch": KIND_WEB, "google_web_search": KIND_WEB,
    "ask_user": KIND_ASK, "write_todos": KIND_TODO,
}
_OPENCODE_KINDS = {
    "bash": KIND_SHELL, "read": KIND_READ,
    "grep": KIND_SEARCH, "glob": KIND_SEARCH, "list": KIND_SEARCH,
    "ast_grep_search": KIND_SEARCH, "codesearch": KIND_SEARCH,
    "edit": KIND_EDIT, "patch": KIND_EDIT, "write": KIND_WRITE,
    "webfetch": KIND_WEB, "websearch": KIND_WEB,
    "task": KIND_AGENT, "background_output": KIND_AGENT, "background_cancel": KIND_AGENT,
    "question": KIND_ASK, "todowrite": KIND_TODO, "todoread": KIND_TODO,
    "invalid": KIND_OTHER,
}
_PI_KINDS = {
    "bash": KIND_SHELL, "bashExecution": KIND_SHELL, "read": KIND_READ,
    "grep": KIND_SEARCH, "find": KIND_SEARCH, "ls": KIND_SEARCH,
    "edit": KIND_EDIT, "write": KIND_WRITE, "fetch": KIND_WEB,
}


def _generic_kind(raw_name: str) -> str:
    name = raw_name.lower()
    if name.startswith("web"):
        return KIND_WEB
    if any(k in name for k in ("shell", "bash", "exec", "terminal")):
        return KIND_SHELL
    if name.startswith("read"):
        return KIND_READ
    if any(k in name for k in ("grep", "glob", "search")):
        return KIND_SEARCH
    if any(k in name for k in ("edit", "patch")):
        return KIND_EDIT
    if name.startswith("write"):
        return KIND_WRITE
    return KIND_OTHER


def normalize_tool(source: str, raw_name: str, namespace: str | None = None) -> NormalizedTool:
    """原始工具名 → ``NormalizedTool(kind, server)``。

    ``source`` ∈ claude / codex / gemini / opencode / pi / generic（其余按 generic）。
    ``namespace`` 只有 Codex ``function_call.namespace`` 会传（如 ``mcp__cua_repl``）。
    """
    if namespace and namespace.startswith("mcp__"):
        return NormalizedTool(KIND_MCP, namespace[5:].rstrip("_"))
    sp = split_mcp_name(raw_name)
    if sp:
        return NormalizedTool(KIND_MCP, sp[0])
    if source == "claude":
        return NormalizedTool(_CLAUDE_KINDS.get(raw_name, KIND_OTHER))
    if source == "codex":
        k = _CODEX_KINDS.get(raw_name)
        if k is None:
            k = KIND_WEB if raw_name.startswith("web.") else KIND_OTHER
        return NormalizedTool(k)
    if source == "gemini":
        k = _GEMINI_KINDS.get(raw_name)
        if k is None:
            if "/" in raw_name:
                return NormalizedTool(KIND_MCP, raw_name.split("/", 1)[0])
            k = KIND_OTHER
        return NormalizedTool(k)
    if source == "opencode":
        k = _OPENCODE_KINDS.get(raw_name)
        if k is None:
            if "_" in raw_name:
                return NormalizedTool(KIND_MCP, raw_name.split("_", 1)[0])
            k = KIND_OTHER
        return NormalizedTool(k)
    if source == "pi":
        return NormalizedTool(_PI_KINDS.get(raw_name, KIND_OTHER))
    return NormalizedTool(_generic_kind(raw_name))


# --------------------------------------------------------------------------- #
# 标题提炼
# --------------------------------------------------------------------------- #
def title_read(path: str, offset: int | None, limit: int | None) -> tuple[str, str | None]:
    detail = None
    if offset is not None and limit is not None and limit > 0:
        detail = f":{offset}-{offset + limit - 1}"
    elif offset is not None and limit is None:
        detail = f":{offset}"
    elif offset is None and limit is not None and limit > 0:
        detail = f":1-{limit}"
    return one_line_title(path), detail


def title_search(pattern: str, path: str | None) -> str:
    if path:
        return one_line_title(f"{pattern} in {path}")
    return one_line_title(pattern)


def title_web(items: Iterable[str]) -> str:
    return one_line_title(", ".join(items))


def title_mcp(server: str, tool: str) -> str:
    return one_line_title(f"{server}.{tool}")


def title_todo(count: int) -> str:
    return f"{count} 项"


def first_string_field(inp: Any) -> str | None:
    """参数里第一个非空字符串字段（mcp / other 的 detail）。"""
    if not isinstance(inp, dict):
        return None
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return one_line_title(v)
    return None


def str_field(inp: Any, keys: Iterable[str]) -> str | None:
    """参数里第一个存在且非空的字符串字段。"""
    if not isinstance(inp, dict):
        return None
    for k in keys:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _u64_field(inp: Any, keys: Iterable[str]) -> int | None:
    if not isinstance(inp, dict):
        return None
    for k in keys:
        v = inp.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v >= 0:
            return v
        if isinstance(v, float) and v >= 0:
            return int(v)
        if isinstance(v, str):
            try:
                n = int(v.strip())
                if n >= 0:
                    return n
            except ValueError:
                pass
    return None


PATH_KEYS = ("file_path", "filePath", "path", "absolute_path", "file", "filename", "dir_path")


def title_from_input(kind: str, raw_name: str, server: str | None, inp: Any) -> tuple[str, str | None]:
    """按 kind 从参数里提炼 ``(title, detail)``（通用版，非 Claude 源共用）。"""
    def detail_of(keys):
        v = str_field(inp, keys)
        return one_line_title(v) if v else None

    if kind == KIND_SHELL:
        cmd = str_field(inp, ("command", "cmd", "script"))
        return (one_line_title(cmd) if cmd else ""), detail_of(("description",))
    if kind == KIND_READ:
        path = str_field(inp, PATH_KEYS)
        if path:
            return title_read(path, _u64_field(inp, ("offset", "start_line")),
                              _u64_field(inp, ("limit",)))
        paths = inp.get("paths") if isinstance(inp, dict) else None
        items = [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []
        return title_web(items), None
    if kind == KIND_SEARCH:
        path = str_field(inp, ("path", "dir_path", "directory", "cwd"))
        pattern = str_field(inp, ("pattern", "query", "regex", "glob", "include"))
        if pattern:
            return title_search(pattern, path), None
        return (one_line_title(path) if path else ""), None
    if kind in (KIND_EDIT, KIND_WRITE):
        path = str_field(inp, PATH_KEYS)
        return (one_line_title(path) if path else ""), None
    if kind == KIND_WEB:
        items: list[str] = []
        if isinstance(inp, dict):
            for key in ("urls", "queries"):
                v = inp.get(key)
                if isinstance(v, list):
                    items.extend(x for x in v if isinstance(x, str))
        if not items:
            one = str_field(inp, ("url", "query", "prompt"))
            if one:
                items.append(one)
        return title_web(items), None
    if kind == KIND_MCP:
        srv = server or ""
        sp = split_mcp_name(raw_name)
        if sp:
            tool = sp[1]
        elif srv and raw_name.startswith(srv):
            tool = raw_name[len(srv):].lstrip("_/.") or raw_name
        else:
            tool = raw_name
        return title_mcp(srv, tool), first_string_field(inp)
    if kind == KIND_AGENT:
        v = str_field(inp, ("description", "task_name", "prompt", "message"))
        return (one_line_title(v) if v else ""), detail_of(("subagent_type", "agent", "model"))
    if kind == KIND_ASK:
        question = None
        if isinstance(inp, dict):
            qs = inp.get("questions")
            if isinstance(qs, list) and qs:
                first = qs[0]
                if isinstance(first, dict) and isinstance(first.get("question"), str):
                    question = first["question"]
                elif isinstance(first, str):
                    question = first
            if question is None:
                question = str_field(inp, ("question", "prompt"))
        return (one_line_title(question) if question else ""), None
    if kind == KIND_TODO:
        todos = inp.get("todos") if isinstance(inp, dict) else None
        return (title_todo(len(todos)) if isinstance(todos, list) else ""), None
    return one_line_title(raw_name), first_string_field(inp)


def claude_tool_title(raw_name: str, kind: str, server: str | None, inp: Any) -> tuple[str, str, str | None]:
    """Claude 专用标题提炼：返回 ``(kind, title, detail)``（Bash 可能细化为 search）。"""
    def s(key):
        v = inp.get(key) if isinstance(inp, dict) else None
        return v if isinstance(v, str) and v.strip() else None

    if kind == KIND_MCP:
        sp = split_mcp_name(raw_name)
        srv, tool = sp if sp else (server or "", raw_name)
        return kind, title_mcp(srv, tool), first_string_field(inp)
    if raw_name == "Bash":
        cmd = s("command") or ""
        d = s("description")
        return refine_shell_kind(cmd), one_line_title(cmd), one_line_title(d) if d else None
    if raw_name == "Read":
        title, detail = title_read(s("file_path") or "", _u64_field(inp, ("offset",)),
                                   _u64_field(inp, ("limit",)))
        return kind, title, detail
    if raw_name == "Grep":
        return kind, title_search(s("pattern") or "", s("path") or s("glob")), s("output_mode")
    if raw_name == "Glob":
        return kind, title_search(s("pattern") or "", s("path")), None
    if raw_name == "ToolSearch":
        return kind, title_search(s("query") or "", None), None
    if raw_name in ("Edit", "MultiEdit", "Write"):
        return kind, one_line_title(s("file_path") or ""), None
    if raw_name == "NotebookEdit":
        return kind, one_line_title(s("notebook_path") or ""), None
    if raw_name == "WebFetch":
        return kind, title_web([s("url") or ""]), None
    if raw_name == "WebSearch":
        return kind, title_web([s("query") or ""]), None
    if raw_name in ("Agent", "Task"):
        return kind, one_line_title(s("description") or s("prompt") or ""), s("model")
    if raw_name == "AskUserQuestion":
        q = ""
        qs = inp.get("questions") if isinstance(inp, dict) else None
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            v = qs[0].get("question")
            q = v if isinstance(v, str) else ""
        return kind, one_line_title(q), None
    if raw_name == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp, dict) else None
        return kind, title_todo(len(todos) if isinstance(todos, list) else 0), None
    if kind == KIND_AGENT:
        return kind, first_string_field(inp) or one_line_title(raw_name), None
    return kind, one_line_title(raw_name), first_string_field(inp)


def diff_from_input(kind: str, inp: Any) -> dict | None:
    """只凭参数估算 Edit/Write 的改动：返回 ``{files:[{path,op,added,removed}],added,removed}``。"""
    if not isinstance(inp, dict):
        return None
    path = str_field(inp, PATH_KEYS)
    if not path:
        return None

    def pair(item):
        if not isinstance(item, dict):
            return None
        old = _str_allow_empty(item, ("old_string", "oldString", "old_str", "oldText"))
        new = _str_allow_empty(item, ("new_string", "newString", "new_str", "newText"))
        if old is None or new is None:
            return None
        return count_lines(new), count_lines(old)

    if kind == KIND_EDIT:
        edits = inp.get("edits")
        if isinstance(edits, list):
            a = r = 0
            for e in edits:
                p = pair(e)
                if p:
                    a += p[0]
                    r += p[1]
        else:
            p = pair(inp)
            if p is None:
                return None
            a, r = p
        return single_file_diff(path, "update", a, r)
    if kind == KIND_WRITE:
        content = _str_allow_empty(inp, ("content", "contents", "text"))
        if content is None:
            return None
        return single_file_diff(path, "add", count_lines(content), 0)
    return None


def _str_allow_empty(inp: dict, keys) -> str | None:
    for k in keys:
        v = inp.get(k)
        if isinstance(v, str):
            return v
    return None


def single_file_diff(path: str, op: str, added: int, removed: int) -> dict:
    return {"files": [{"path": path, "op": op, "added": added, "removed": removed}],
            "added": added, "removed": removed, "full": None}


# --------------------------------------------------------------------------- #
# 工具结果解析（Claude / Codex 共用的小解析）
# --------------------------------------------------------------------------- #
def parse_exit_code(text: str) -> int | None:
    """``Exit code N`` 开头的工具输出（Claude Bash 失败时的格式）。"""
    s = text.lstrip()
    first = s.split("\n", 1)[0].rstrip("\r")
    rest = first[7:] if first.startswith("Error: ") else first
    if not rest.startswith("Exit code "):
        return None
    try:
        return int(rest[10:].strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Agent 风格（agentStyles.ts）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentStyle:
    id: str
    assistant_glyph: str
    user_glyph: str
    step_glyphs: dict                  # status → glyph（success/error/pending/interrupted）
    result_frame: str                  # tree / box / none
    result_connector: str
    thinking_glyph: str
    thinking_label: str                # 中文
    title_format: str                  # call / verb / icon
    verbs: dict                        # kind → 动词；缺省用 raw_name
    merge_read_search_runs: bool
    merge_mcp_same_server_runs: bool
    show_diff_counts: bool = True
    show_step_cost: bool = False
    show_exit_code: str = "nonzero"    # always / nonzero
    mono_titles: bool = True


def _gl(s, e, p, i):
    return {"success": s, "error": e, "pending": p, "interrupted": i}


_GENERIC_VERBS = {
    KIND_SHELL: "verb.run", KIND_READ: "verb.read", KIND_SEARCH: "verb.search",
    KIND_EDIT: "verb.edit", KIND_WRITE: "verb.write", KIND_WEB: "verb.web",
    KIND_MCP: "verb.mcp", KIND_AGENT: "verb.agent", KIND_ASK: "verb.ask",
    KIND_TODO: "verb.todo", KIND_OTHER: "verb.other",
}

AGENT_STYLES: dict[str, AgentStyle] = {
    "claude": AgentStyle(
        "claude", "⏺", ">", _gl("⏺", "⏺", "⏺", "⏺"), "tree", "⎿", "✻", "思考中…", "call",
        {}, False, True, mono_titles=True),
    "codex": AgentStyle(
        "codex", "•", "›", _gl("•", "•", "•", "•"), "tree", "└", "•", "推理", "verb",
        {KIND_SHELL: "Ran", KIND_READ: "Read", KIND_SEARCH: "Search", KIND_EDIT: "Edited",
         KIND_WRITE: "Added", KIND_WEB: "Searched", KIND_MCP: "Called",
         KIND_AGENT: "Spawned agent", KIND_ASK: "Asked", KIND_TODO: "Updated plan"},
        True, False, mono_titles=False),
    "gemini": AgentStyle(
        "gemini", "✦", ">", _gl("✓", "x", "o", "-"), "box", "", "✦", "想法", "verb",
        {KIND_SHELL: "Shell", KIND_READ: "ReadFile", KIND_SEARCH: "Search", KIND_EDIT: "Edit",
         KIND_WRITE: "WriteFile", KIND_WEB: "WebFetch", KIND_MCP: "MCP", KIND_AGENT: "Agent",
         KIND_ASK: "Ask", KIND_TODO: "Todos"},
        False, False),
    "opencode": AgentStyle(
        "opencode", "", "", _gl("✓", "✗", "~", "✗"), "tree", "│", "", "Thought", "icon",
        {KIND_SHELL: "", KIND_READ: "Read", KIND_SEARCH: "Grep", KIND_EDIT: "Edit",
         KIND_WRITE: "Wrote", KIND_WEB: "WebFetch", KIND_AGENT: "Task", KIND_ASK: "Asked",
         KIND_TODO: "Todos"},
        False, False, show_step_cost=True),
    "pi": AgentStyle(
        "pi", "", "", _gl("●", "●", "●", "●"), "none", "", "", "Thinking…", "verb",
        {KIND_SHELL: "bash", KIND_READ: "read", KIND_SEARCH: "grep", KIND_EDIT: "edit",
         KIND_WRITE: "write", KIND_WEB: "fetch"},
        False, False),
    "generic": AgentStyle(
        "generic", "•", "", _gl("•", "•", "•", "•"), "tree", "└", "•", "思考", "verb",
        dict(_GENERIC_VERBS), False, False),
}

# 数据源 → 风格（规格 §3.3 建议映射）
SOURCE_STYLE = {
    "claude": "claude", "codex": "codex", "opencode": "opencode", "pi": "pi", "omp": "pi",
    "antigravity": "generic", "workbuddy": "generic", "codebuddy": "generic",
}


def get_style(source_or_style: str | None) -> AgentStyle:
    key = SOURCE_STYLE.get((source_or_style or "").lower(), source_or_style or "generic")
    return AGENT_STYLES.get(key, AGENT_STYLES["generic"])


# --------------------------------------------------------------------------- #
# 状态判定
# --------------------------------------------------------------------------- #
def is_failure_status(status: str) -> bool:
    """失败类状态：失败与中断都要常显。"""
    return status in (ST_ERROR, ST_INTERRUPTED)


def is_failure_step(step) -> bool:
    return step.kind in ("tool", "merged") and is_failure_status(step.status)


# --------------------------------------------------------------------------- #
# 步骤合并
# --------------------------------------------------------------------------- #
def _merge_key_of(step, style: AgentStyle) -> str:
    if step.kind != "tool" or step.status != ST_SUCCESS or not step.call:
        return ""
    kind, server = step.call.kind, step.call.server
    if style.merge_read_search_runs and kind in (KIND_READ, KIND_SEARCH):
        return "explored"
    if style.merge_mcp_same_server_runs and kind == KIND_MCP and server:
        return f"mcp:{server}"
    return ""


def merge_steps(steps: list, style: AgentStyle) -> list:
    """按风格合并连续步骤：Codex 连续成功的 read/search → explored；
    Claude 连续成功的同一 MCP 服务器调用 → mcp。只合并成功步骤且至少 2 步。"""
    from tcer.core.transcript import MergedStep

    merged: list = []
    run: list = []
    run_key = ""

    def flush():
        nonlocal run, run_key
        if len(run) >= 2:
            merged.append(MergedStep(
                id=f"{run[0].id}+merged",
                merge_kind="explored" if run_key == "explored" else "mcp",
                server=None if run_key == "explored" else run[0].call.server,
                children=run, status=ST_SUCCESS))
        else:
            merged.extend(run)
        run = []
        run_key = ""

    for step in steps:
        key = _merge_key_of(step, style)
        if key and key == run_key:
            run.append(step)
            continue
        flush()
        if key:
            run = [step]
            run_key = key
        else:
            merged.append(step)
    flush()
    return merged


# --------------------------------------------------------------------------- #
# 格式化小工具
# --------------------------------------------------------------------------- #
def _trim_num(x: float, digits: int = 1) -> str:
    s = f"{x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def format_duration(ms: float) -> str:
    """0.3s / 12s / 2m14s / 4m / 1h3m；<1s 用 ms。"""
    if ms < 1000:
        return f"{int(round(ms))}ms"
    seconds = ms / 1000
    if seconds < 10:
        return f"{_trim_num(seconds)}s"
    total = int(seconds + 0.5)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m{s}s" if s else f"{m}m"
    h = total // 3600
    m = (total % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"


def format_tokens(tokens: float) -> str:
    """980 / 28.5k / 1.2M"""
    if tokens < 1000:
        return str(int(tokens))
    if tokens < 1_000_000:
        return f"{_trim_num(tokens / 1000)}k"
    return f"{_trim_num(tokens / 1_000_000)}M"


_HOME_RE = re.compile(
    r"""(^|[\s"'(])(?:/(?:Users|home)/[^/\s"']+|[A-Za-z]:[\\/]Users[\\/][^\\/\s"']+)(?=[\\/])""")


def _root_pattern(root: str) -> str:
    out = []
    for ch in root:
        out.append(r"[\\/]" if ch in "/\\" else re.escape(ch))
    return "".join(out)


def shorten_path(text: str, project_dir: str | None = None) -> str:
    """路径缩短：project_dir 之下显示相对路径（本身→``.``），家目录下的路径 → ``~/…``。
    对整段文本替换；Windows 盘符 / 反斜杠 / 大小写不敏感。"""
    result = text
    root = (project_dir or "").rstrip("/\\")
    if root:
        pat = _root_pattern(root)
        flags = re.IGNORECASE if (len(root) > 1 and root[1:2] == ":") or "\\" in root else 0
        result = re.sub(pat + r"[\\/]", "", result, flags=flags)
        result = re.sub(pat + r"(?![\w.-])", ".", result, flags=flags)
    return _HOME_RE.sub(lambda m: m.group(1) + "~", result)


# --------------------------------------------------------------------------- #
# 步骤标题
# --------------------------------------------------------------------------- #
@dataclass
class StepTitle:
    icon: str
    verb: str
    target: str
    detail: str | None
    text: str


_RAW_NAME_VERBS = {
    "claude": {"Edit": "Update", "MultiEdit": "Update"},
    "gemini": {"google_web_search": "GoogleSearch"},
    "opencode": {"glob": "Glob", "list": "List", "websearch": "WebSearch"},
}
_OPENCODE_ICONS = {
    KIND_SHELL: "$", KIND_READ: "→", KIND_SEARCH: "✱", KIND_EDIT: "←", KIND_WRITE: "←",
    KIND_WEB: "%", KIND_MCP: "⚙", KIND_AGENT: "│", KIND_ASK: "→", KIND_TODO: "⚙", KIND_OTHER: "⚙",
}
_PATH_KINDS = (KIND_READ, KIND_SEARCH, KIND_EDIT, KIND_WRITE)


def _verb_for(call, style: AgentStyle) -> str:
    alias = _RAW_NAME_VERBS.get(style.id, {}).get(call.raw_name)
    if alias:
        return alias
    verb = style.verbs.get(call.kind)
    if verb is None:
        return call.raw_name
    return t(verb) if verb.startswith("verb.") else verb


def _codex_diff_title(call, verb: str, target: str) -> tuple[str, str]:
    files = (call.diff or {}).get("files") or []
    if not files:
        return verb, target
    ops = {f.get("op") for f in files}
    only = files[0].get("op") if len(ops) == 1 else None
    verb = "Added" if only == "add" else "Deleted" if only == "delete" else "Edited"
    return verb, (f"{len(files)} files" if len(files) > 1 else target)


def format_step_title(step, style: AgentStyle, project_dir: str | None = None) -> StepTitle:
    """步骤标题（动作 + 对象）。call 风格 ``Bash(cargo build)``；verb 风格 ``Ran cargo build``；
    icon 风格 ``$ cargo build``。无配对调用 → 「工具输出」。"""
    call = step.call
    if call is None:
        v = t("verb.output")
        return StepTitle("", v, "", None, v)
    verb = _verb_for(call, style)
    target = shorten_path(call.title, project_dir) if call.kind in _PATH_KINDS else call.title
    detail = call.detail
    if call.kind == KIND_READ and detail and detail.startswith(":"):
        target += detail
        detail = None
    if call.kind == KIND_MCP and style.title_format != "icon":
        if style.title_format == "call":
            verb = call.title
            target = detail or ""
        else:
            target = f"{call.title}({detail or ''})"
        detail = None
    if style.id == "codex" and call.kind in (KIND_EDIT, KIND_WRITE):
        verb, target = _codex_diff_title(call, verb, target)
    if target == verb:
        target = ""
    icon = ""
    if style.title_format == "call":
        text = f"{verb}({target})" if target else verb
    elif style.title_format == "icon":
        icon = "◈" if call.raw_name == "websearch" else _OPENCODE_ICONS.get(call.kind, "⚙")
        text = " ".join(p for p in (icon, verb, target) if p)
    else:
        text = f"{verb} {target}" if target else verb
    return StepTitle(icon, verb, target, detail, text)


@dataclass
class MergedTitle(StepTitle):
    count: int = 0
    reads: int = 0
    searches: int = 0
    subtitle: str = ""


def format_merged_title(step) -> MergedTitle:
    """合并步骤标题：``Explored`` / ``Called {server} {n} times``，附中文副标题。"""
    count = len(step.children)
    reads = len({c.call.title for c in step.children if c.call and c.call.kind == KIND_READ})
    searches = sum(1 for c in step.children if c.call and c.call.kind == KIND_SEARCH)
    if step.merge_kind == "explored":
        verb = "Explored"
        sub = t("merged.explored", files=reads, searches=searches)
    else:
        verb = f"Called {step.server} {count} times"
        sub = t("merged.mcpTimes", server=step.server, count=count)
    return MergedTitle("", verb, "", None, verb, count, reads, searches, sub)


# --------------------------------------------------------------------------- #
# 步骤元信息
# --------------------------------------------------------------------------- #
_STATUS_TONE = {ST_SUCCESS: "success", ST_ERROR: "danger", ST_INTERRUPTED: "warning",
                ST_PENDING: "muted", ST_UNKNOWN: "neutral"}


@dataclass
class StepMeta:
    status: str
    glyph: str
    tone: str                       # success / danger / warning / muted / neutral
    exit_code: int | None = None
    duration_ms: int | None = None
    line_count: int | None = None   # 输出行数（有 diff 计数时不给）
    diff: dict | None = None        # {added, removed, files}
    tokens: int | None = None
    cost_usd: float | None = None


def status_glyph(status: str, style: AgentStyle) -> str:
    return style.step_glyphs["success" if status == ST_UNKNOWN else status]


def format_step_meta(step, style: AgentStyle) -> StepMeta:
    """步骤一行的状态与元信息：状态符 + exit + 耗时 + 行数 / +x −y + tokens/费用。"""
    meta = StepMeta(step.status, status_glyph(step.status, style), _STATUS_TONE[step.status])
    if step.kind == "merged":
        total = sum((c.result.duration_ms or 0) for c in step.children if c.result)
        if total > 0:
            meta.duration_ms = total
        return meta
    call, result, cost = step.call, step.result, step.cost
    exit_code = result.exit_code if result else None
    if exit_code is not None and (style.show_exit_code == "always" or exit_code != 0):
        meta.exit_code = exit_code
    meta.duration_ms = result.duration_ms if result else None
    diff = call.diff if call else None
    if style.show_diff_counts and diff and diff.get("added", 0) + diff.get("removed", 0) > 0:
        meta.diff = {"added": diff["added"], "removed": diff["removed"],
                     "files": len(diff.get("files") or [])}
    elif result is not None and result.line_count > 0:
        meta.line_count = result.line_count
    if style.show_step_cost and cost is not None:
        meta.tokens = cost.tokens
        meta.cost_usd = cost.cost_usd
    return meta


def format_step_meta_text(meta: StepMeta) -> str:
    """元信息拼成一行（导出 Markdown / GUI 右侧文字共用）：
    失败状态词 · exit N · +a −r · 耗时 · tok · 费用。"""
    parts: list[str] = []
    if is_failure_status(meta.status):
        parts.append(t(f"status.{meta.status}"))
    if meta.exit_code is not None:
        parts.append(t("exitCode", code=meta.exit_code))
    if meta.diff:
        parts.append(f"+{meta.diff['added']} −{meta.diff['removed']}")
    if meta.duration_ms:
        parts.append(format_duration(meta.duration_ms))
    if meta.tokens is not None:
        parts.append(t("summaryTokens", tokens=format_tokens(meta.tokens)))
    if meta.cost_usd is not None:
        parts.append(f"${meta.cost_usd:.4f}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# 事件描述（describeEvent）
# --------------------------------------------------------------------------- #
@dataclass
class EventDisplay:
    label: str
    detail: str | None
    tone: str            # muted / danger


_PR_NUM = re.compile(r"#(\d+)")
_PR_URL_NUM = re.compile(r"/pull/(\d+)")
_SUBAGENT_RE = re.compile(r"^([^:\s]{1,40}):\s*([\s\S]*)$")


def describe_event(block, body: str | None = None) -> EventDisplay:
    text = (block.text or "").strip()
    extra = (body or "").strip()

    def join(*parts):
        s = "\n\n".join(p for p in parts if p)
        return s or None

    k = block.kind
    if k == EV_ABORTED:
        return EventDisplay(t("turnAborted"), None, "muted")
    if k == EV_COMPACTION:
        return EventDisplay(t("event.compaction"), join(text, extra), "muted")
    if k == EV_MODEL_CHANGE:
        return EventDisplay(t("event.modelChange", model=text), None, "muted")
    if k == EV_THINKING_LEVEL:
        return EventDisplay(t("event.thinkingLevel", level=text), None, "muted")
    if k == EV_HOOK:
        return EventDisplay(t("event.hookError"), text or None, "danger")
    if k == EV_PR_LINK:
        m = _PR_NUM.search(block.text or "") or _PR_URL_NUM.search(block.url or "")
        label = t("event.prLink", number=m.group(1)) if m else t("event.prLinkPlain")
        return EventDisplay(label, None, "muted")
    if k == EV_SLASH:
        return EventDisplay(t("event.slashCommand", command=text), extra or None, "muted")
    if k == EV_SUB_AGENT:
        m = _SUBAGENT_RE.match(text)
        name, rest = (m.group(1), m.group(2)) if m else (text, "")
        return EventDisplay(t("event.subAgent", name=name), join(rest, extra), "muted")
    if k == EV_ERROR:
        return EventDisplay(text, extra or None, "danger")
    return EventDisplay(text, extra or None, "muted")


# --------------------------------------------------------------------------- #
# 轮摘要
# --------------------------------------------------------------------------- #
@dataclass
class TurnSummary:
    step_count: int = 0            # 工具步骤数（合并步骤按子步骤计）
    command_count: int = 0
    files_changed: int = 0         # 成功改动的不同文件数
    error_count: int = 0           # 失败 + 中断
    thinking_count: int = 0
    duration_ms: int | None = None
    kind_counts: dict = field(default_factory=dict)
    tokens: int | None = None      # OpenCode step-finish 合计
    cost_usd: float | None = None


def tool_steps_of(turn) -> list:
    """轮内全部工具步骤（展开合并步骤）。"""
    out = []
    for s in turn.steps:
        if s.kind == "merged":
            out.extend(s.children)
        elif s.kind == "tool":
            out.append(s)
    return out


def _sum_defined(values) -> Any:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def summarize_turn(turn) -> TurnSummary:
    """折叠摘要行数据：步骤数 / 命令数 / 改文件数 / 失败数 / 耗时。"""
    tools = tool_steps_of(turn)
    kind_counts: dict[str, int] = {}
    files: set[str] = set()
    for s in tools:
        kind = s.call.kind if s.call else KIND_OTHER
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        call = s.call
        if call is None or is_failure_status(s.status):
            continue
        if call.diff:
            files.update(f["path"] for f in call.diff.get("files") or [])
        elif kind in (KIND_EDIT, KIND_WRITE):
            files.add(call.title)
    start = turn.question.ts if turn.question and turn.question.ts is not None else turn.ts
    span = (turn.end_ts - start) if start is not None and turn.end_ts is not None else 0
    step_dur = sum((s.result.duration_ms or 0) for s in tools if s.result)
    duration = span if span > 0 else (step_dur if step_dur > 0 else None)
    return TurnSummary(
        step_count=len(tools),
        command_count=kind_counts.get(KIND_SHELL, 0),
        files_changed=len(files),
        error_count=sum(1 for s in tools if is_failure_status(s.status)),
        thinking_count=sum(1 for s in turn.steps if s.kind == "thinking"),
        duration_ms=duration,
        kind_counts=kind_counts,
        tokens=_sum_defined(c.tokens for c in turn.step_costs),
        cost_usd=_sum_defined(c.cost_usd for c in turn.step_costs),
    )


def format_turn_summary(summary: TurnSummary, *, hidden_failures: int = 0,
                        no_final: bool = False, aborted: bool = False,
                        steps: int | None = None) -> str:
    """折叠摘要行文案：``执行过程 · N 步 · M 个命令 · 改了 K 个文件 · F 个失败 · 耗时 2m14s``。

    ``steps`` 缺省用 ``summary.step_count``（目录与摘要行统一口径）。"""
    parts = [t("timelineSummary", steps=summary.step_count if steps is None else steps)]
    if summary.command_count:
        parts.append(t("summaryCommands", count=summary.command_count))
    if summary.files_changed:
        parts.append(t("summaryFiles", count=summary.files_changed))
    if summary.error_count:
        parts.append(t("summaryErrors", count=summary.error_count))
    if summary.duration_ms:
        parts.append(t("summaryDuration", duration=format_duration(summary.duration_ms)))
    if summary.tokens:
        parts.append(t("summaryTokens", tokens=format_tokens(summary.tokens)))
    if summary.cost_usd:
        parts.append(f"${summary.cost_usd:.4f}")
    if hidden_failures > 0:
        parts.append(t("moreFailures", count=hidden_failures))
    if no_final and not aborted:
        parts.append(t("noFinalReply"))
    return " · ".join(parts)


def dumps_compact(obj: Any) -> str:
    """紧凑 JSON（参数预览用，与 serde_json::to_string 等价：无空白、保留非 ASCII）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
