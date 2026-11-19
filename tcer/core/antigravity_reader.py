"""Google Antigravity (CLI / desktop App / IDE) local-session reader.

Antigravity stores sessions under ``~/.gemini/antigravity-cli`` (CLI, ``agy``),
``~/.gemini/antigravity`` (desktop App 2.0) and ``~/.gemini/antigravity-ide``
(IDE) — the three surfaces share one on-disk layout — plus
``~/.config/antigravity`` (ccusage parity) and cc-switch isolated terminals
(``~/.cc-switch/agy/<provider_id>/antigravity-cli``); all roots are merged by
workspace, see :func:`tcer.core.paths.antigravity_roots`.
``~/.gemini/antigravity-backup`` is deliberately not scanned (copies of the
same sessions). Remote Agents and the AI Studio web mode run in the cloud and
leave no local files, so they are out of scope.
  * ``conversation_summaries.db``: SQLite database cataloging all conversations,
    their workspace URIs (``workspace_uris``, e.g. ``["file:///C:/GitHub/TCER"]``),
    step counts, last modified timestamps, and title/preview texts.
  * ``conversations/<conversation_id>.db``: Per-session SQLite database containing:
    - ``trajectory_meta``: Cascade / trajectory metadata.
    - ``trajectory_metadata_blob``: Raw protobuf blob containing workspace URI and project id.
    - ``gen_metadata``: LLM generation responses in protobuf format. Authoritative
      source of token counts (input, cached input, output, thinking/reasoning) and
      model names (e.g. ``gemini-3.8-flash``).
    - ``steps``: Chronological execution steps in protobuf format:
      * step_type == 14: User input (prompts).
      * step_type == 15: Assistant turns / tool calls (``write_to_file``,
        ``replace_file_content``, ``view_file``, ``run_command``, etc.).
      * step_type == 132: Tool execution results and exit codes; field 5.4 repeats the
        originating call (call_id / tool name / args, byte-identical to step 15).
      * step_type == 23: Context compaction (checkpoint summary). Its LLM usage lives
        only in step metadata field 28.2 and is NOT mirrored into ``gen_metadata``.
      * step status (field 4): 3 = done, 5 = done but payload pruned (step 15 loses
        its tool calls, step 14 keeps only the 19.3.1 copy of the prompt), 7 = failed.

This module implements a zero-dependency, pure Python protobuf wire format decoder
to parse Antigravity's SQLite databases into TCER's canonical models (``TokenUsage``,
``SessionMeta``, ``SessionLoc``, and ``ProjectRef``). Read-only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tcer.core import loc, pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import (
    as_int as _as_int,
    basename_of as _basename_of,
    is_correction as _is_correction,
    is_slash_command as _is_slash_command,
    is_windows_path as _is_windows_path,
    norm_path as _norm_path,
    normcase_path as _normcase_path,
)
from tcer.core.paths import antigravity_conversations_dir, antigravity_roots

# Antigravity tool name -> TCER canonical tool name
_ANTIGRAVITY_TOOL_MAP = {
    "view_file": "Read",
    "read_file": "Read",
    "write_to_file": "Write",
    "write_file": "Write",
    "replace_file_content": "Edit",
    "edit_file": "Edit",
    "run_command": "Bash",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "ask_question": "Ask",
    "generate_image": "Image",
    # 子代理派发（实测 2026-11：一次 step 15 可并行 invoke 多个，各自落独立
    # conversation db，由 summaries.parent_conversation_id 折叠回父会话）。
    "invoke_subagent": "Task",
    # manage_subagents（list/kill）/ schedule（定时器）是后台管理动作，照
    # manage_task 先例不作 Task，保持原名透传。
}


# --------------------------------------------------------------------------- #
# Lightweight pure-Python Protobuf wire format decoder (Zero Dependencies)
# --------------------------------------------------------------------------- #

def _parse_proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Decode raw protobuf wire format into a list of (field_num, wire_type, value).

    Supported wire types:
      0: varint (value is int)
      1: 64-bit (value is 8 bytes)
      2: length-delimited (value is bytes)
      5: 32-bit (value is 4 bytes)
    """
    pos = 0
    n = len(data)
    res = []
    while pos < n:
        shift = 0
        tag = 0
        while pos < n:
            b = data[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        if pos > n:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            res.append((field_num, 0, val))
        elif wire_type == 1:  # 64-bit
            val = data[pos:pos + 8]
            pos += 8
            res.append((field_num, 1, val))
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            val = data[pos:pos + length]
            pos += length
            res.append((field_num, 2, val))
        elif wire_type == 5:  # 32-bit
            val = data[pos:pos + 4]
            pos += 4
            res.append((field_num, 5, val))
        else:
            # Unknown wire type or stream corrupted; break to avoid infinite loop
            break
    return res


def _find_field(fields: list[tuple[int, int, int | bytes]], field_num: int):
    """Return the first value matching field_num, or None."""
    for f, _, val in fields:
        if f == field_num:
            return val
    return None


def _find_all_fields(fields: list[tuple[int, int, int | bytes]], field_num: int) -> list:
    """Return all values matching field_num."""
    return [val for f, _, val in fields if f == field_num]


def _decode_timestamp(meta_blob: bytes | None) -> float | None:
    """Extract unix timestamp (seconds as float) from google.protobuf.Timestamp blob."""
    if not meta_blob:
        return None
    fields = _parse_proto_fields(meta_blob)
    ts_blob = _find_field(fields, 1)
    if not isinstance(ts_blob, bytes):
        return None
    ts_fields = _parse_proto_fields(ts_blob)
    sec = _find_field(ts_fields, 1) or 0
    nanos = _find_field(ts_fields, 2) or 0
    if isinstance(sec, int) and isinstance(nanos, int):
        return sec + nanos / 1e9
    return None


def _format_timestamp(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Step payload decoders
# --------------------------------------------------------------------------- #

def _decode_gen_metadata(data: bytes) -> tuple[str, int, int, int, int]:
    """Return (model, uncached_input, total_output, cached_input, reasoning_tokens)."""
    fields = _parse_proto_fields(data)
    inner_blob = _find_field(fields, 1)
    if not isinstance(inner_blob, bytes):
        return ("", 0, 0, 0, 0)
    inner_fields = _parse_proto_fields(inner_blob)

    # model in field 19
    model_bytes = _find_field(inner_fields, 19)
    model = ""
    if isinstance(model_bytes, bytes):
        try:
            model = model_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass

    # usage in field 4
    usage_blob = _find_field(inner_fields, 4)
    if not isinstance(usage_blob, bytes):
        return (model, 0, 0, 0, 0)

    u_fields = _parse_proto_fields(usage_blob)
    uncached_input = _find_field(u_fields, 2) or 0
    total_output = _find_field(u_fields, 3) or 0
    cached_input = _find_field(u_fields, 5) or 0
    reasoning = _find_field(u_fields, 9) or 0

    return (
        model,
        int(uncached_input) if isinstance(uncached_input, int) else 0,
        int(total_output) if isinstance(total_output, int) else 0,
        int(cached_input) if isinstance(cached_input, int) else 0,
        int(reasoning) if isinstance(reasoning, int) else 0,
    )


def _canon_tool(tool_name: str, tool_args_json: str = "",
                mcp_servers: frozenset[str] | set[str] | None = None) -> str:
    """Map an agy tool name to the canonical name.

    MCP 工具统一成 ``mcp__<server>__<tool>``（与其余源一致，CLAUDE.md #28）：

    - ``call_mcp_tool``（CLI 通用网关，参数 ``ServerName``/``ToolName``）；
    - App 2.32 起直接以 ``mcp_<server>_<tool>`` 调用（单下划线，server 内的 ``-``
      也写成 ``_``）——server/tool 边界不可从名字本身判定，只按**已知服务器名**
      最长前缀拆分：内置 :data:`_MCP_KNOWN_SERVERS` ∪ 本会话权限条目
      ``mcp(<server>/<tool>)`` 里出现的 server（``mcp_servers``）。不读
      ``~/.gemini/config/mcp_config.json``：外部配置随时间变化，同一会话会算出
      不同结果且 file_cache key 覆盖不到。拆不开的保持原名，不猜边界。

    ``swarm``：只有 ``action=add``（派生 worker）是子代理派发 → ``Task``；
    status/kill/title 是编排管理动作，照 ``manage_subagents`` 先例保持原名。
    """
    if tool_name == "call_mcp_tool" and tool_args_json:
        try:
            a = json.loads(tool_args_json)
            srv, tl = a.get("ServerName"), a.get("ToolName")
            if isinstance(srv, str) and isinstance(tl, str) and srv and tl:
                return f"mcp__{srv}__{tl}"
        except Exception:
            pass
    elif tool_name == "swarm":
        try:
            a = json.loads(tool_args_json) if tool_args_json else {}
        except Exception:
            a = {}
        return "Task" if isinstance(a, dict) and a.get("action") == "add" else "swarm"
    elif tool_name.startswith("mcp_") and not tool_name.startswith("mcp__"):
        rest = tool_name[4:]
        servers = _MCP_KNOWN_SERVERS | frozenset(mcp_servers or ())
        for srv in sorted({s.replace("-", "_") for s in servers}, key=len, reverse=True):
            if rest.startswith(srv + "_") and len(rest) > len(srv) + 1:
                return f"mcp__{srv}__{rest[len(srv) + 1:]}"
        return tool_name
    return _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)


# 已知 Antigravity MCP 服务器（server 名内 ``-`` 写作 ``_``）。实测 2026-11-15
# App 2.32.0：官方 chrome-devtools 插件 → ``mcp_chrome_devtools_<tool>``，权限
# 条目 ``mcp(chrome_devtools/evaluate_script)`` 证实边界。新增须有同类实证。
_MCP_KNOWN_SERVERS = frozenset({"chrome_devtools"})
# 权限条目 ``mcp(<server>/<tool>)``（gen_metadata 内的请求配置副本）。
_MCP_PERM_RE = re.compile(rb"mcp\(([A-Za-z0-9_\-]+)/")


def _mcp_servers_in(blob: bytes | None) -> set[str]:
    if not blob or b"mcp(" not in blob:
        return set()
    return {m.group(1).decode("ascii") for m in _MCP_PERM_RE.finditer(blob)}


def _arg(args: dict, *keys: str) -> str:
    """First non-empty string value among ``keys`` (agy arg names differ per tool/version)."""
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _extract_user_text(payload: bytes | None) -> str:
    """Extract user prompt text from a step_type == 14 step_payload.

    字段 19 是用户输入的权威载体：19.2 正文；status 5（payload 被裁剪）时只剩
    19.3.1 副本（可重复出现，与技能/命令 chip 19.3.2 并列）。**19 存在即以它为准**
    ——为空（实测 2.32.0：长度 0）或只有 chip + 空白正文（无参数技能调用）时返回
    空串，不再兜底扫描：兜底曾从字段 5 元数据里捞出 5.12 的 UUID 当成提问
    （2026-11-15 普查 369 条中 10 条）。
    """
    if not payload:
        return ""
    fields = _parse_proto_fields(payload)
    f19 = _find_field(fields, 19)
    if isinstance(f19, bytes):
        sub_fields = _parse_proto_fields(f19)
        txt = _find_field(sub_fields, 2)
        if isinstance(txt, bytes):
            return txt.decode("utf-8", errors="ignore").strip()
        for f3 in _find_all_fields(sub_fields, 3):
            if isinstance(f3, bytes):
                t = _find_field(_parse_proto_fields(f3), 1)
                if isinstance(t, bytes):
                    s = t.decode("utf-8", errors="ignore").strip()
                    if s:
                        return s
        return ""

    # Fallback（无字段 19 的旧格式）: scan length-delimited fields for plain text.
    # 字段 5 是步骤元数据（时间戳 / step id / trajectory 引用），不含用户正文。
    for fnum, wt, val in fields:
        if fnum == 5:
            continue
        if wt == 2 and isinstance(val, bytes):
            sub = _parse_proto_fields(val)
            for _, swt, sval in sub:
                if swt == 2 and isinstance(sval, bytes):
                    try:
                        # 严格解码 + 拒控制字符：宽松解码会把 protobuf 片段当成提问正文
                        s = sval.decode("utf-8").strip()
                        if (
                            len(s) > 1
                            and not any(ord(ch) < 32 and ch not in "\n\r\t" for ch in s)
                            and not s.startswith("{")
                            and not s.startswith("type.googleapis.com")
                            and not s.startswith("file:///")
                            and not _UUID_RE.match(s)
                        ):
                            return s
                    except Exception:
                        pass
    return ""


def _extract_tool_calls(payload: bytes | None) -> list[tuple[str, str, str]]:
    """All ``(tool_name, tool_args_json, call_id)`` of a step_type == 15 payload.

    Field 20 repeats field 7 once per call when the model issues parallel tool
    calls in one turn; reading only the first undercounts tools and LOC.
    """
    if not payload:
        return []
    f20 = _find_field(_parse_proto_fields(payload), 20)
    if not isinstance(f20, bytes):
        return []
    out: list[tuple[str, str, str]] = []
    for fn, wt, f7 in _parse_proto_fields(f20):
        if fn != 7 or wt != 2 or not isinstance(f7, bytes):
            continue
        sf7 = _parse_proto_fields(f7)
        name = _bstr(_find_field(sf7, 2))
        if name:
            out.append((name, _bstr(_find_field(sf7, 3)), _bstr(_find_field(sf7, 1))))
    return out


def _extract_result_call(payload: bytes | None) -> tuple[str, str, str]:
    """``(tool_name, tool_args_json, call_id)`` echoed in a step_type == 132 payload (field 5.4).

    Survives payload pruning (status 5) that empties the originating step 15.
    """
    if not payload:
        return ("", "", "")
    f5 = _find_field(_parse_proto_fields(payload), 5)
    if not isinstance(f5, bytes):
        return ("", "", "")
    f4 = _find_field(_parse_proto_fields(f5), 4)
    if not isinstance(f4, bytes):
        return ("", "", "")
    sf = _parse_proto_fields(f4)
    return (_bstr(_find_field(sf, 2)), _bstr(_find_field(sf, 3)), _bstr(_find_field(sf, 1)))


def _usage_fields(blob) -> tuple[int, int, int, int, str]:
    """``(uncached_input, total_output, cached_input, reasoning, response_id)`` of a usage blob."""
    if not isinstance(blob, bytes):
        return (0, 0, 0, 0, "")
    uf = _parse_proto_fields(blob)

    def _i(n: int) -> int:
        v = _find_field(uf, n)
        return v if isinstance(v, int) else 0

    return (_i(2), _i(3), _i(5), _i(9), _bstr(_find_field(uf, 7)))


def _gen_response_id(data: bytes | None) -> str:
    """Response id (usage field 7) of a ``gen_metadata`` row."""
    if not data:
        return ""
    inner = _find_field(_parse_proto_fields(data), 1)
    if not isinstance(inner, bytes):
        return ""
    return _usage_fields(_find_field(_parse_proto_fields(inner), 4))[4]


def _compaction_usage(payload: bytes | None) -> tuple[int, int, int, int, str]:
    """Usage of a step_type == 23 compaction call (step metadata field 5.28.2)."""
    if not payload:
        return (0, 0, 0, 0, "")
    f5 = _find_field(_parse_proto_fields(payload), 5)
    if not isinstance(f5, bytes):
        return (0, 0, 0, 0, "")
    f28 = _find_field(_parse_proto_fields(f5), 28)
    if not isinstance(f28, bytes):
        return (0, 0, 0, 0, "")
    return _usage_fields(_find_field(_parse_proto_fields(f28), 2))


def _bstr(b) -> str:
    return b.decode("utf-8", errors="ignore") if isinstance(b, bytes) else ""


def _result_ok(status: int, exit_code: int) -> bool:
    """Tool result succeeded: status 0/3 (done) or 5 (done, payload pruned) and exit code 0."""
    return exit_code == 0 and status in (0, 3, 5)


# 命令未结束的结果状态（2026-11-15 实测 CLI）：6 = ``run_command`` 超过
# ``WaitMsBeforeAsync`` 转后台任务（``<会话id>/task-N``，真正结果稍后经 step 101
# 回传）、2 = 常驻进程（``IsDaemon``）仍在运行。不是失败，不计工具错误；也不是
# 「已成功」，LOC 回放仍只认 0/3/5。
_BACKGROUND_STATUS = frozenset({2, 6})


def _result_failed(status: int, exit_code: int) -> bool:
    """Tool result counts as a tool error: not ok, and not a still-running background command."""
    if exit_code == 0 and status in _BACKGROUND_STATUS:
        return False
    return not _result_ok(status, exit_code)


def _extract_tool_call(payload: bytes | None) -> tuple[str, str, str]:
    """Extract the first (tool_name, tool_args_json, call_id) from a step_type == 15 payload."""
    if not payload:
        return ("", "", "")
    fields = _parse_proto_fields(payload)
    f20 = _find_field(fields, 20)
    if isinstance(f20, bytes):
        sub20 = _parse_proto_fields(f20)
        f7 = _find_field(sub20, 7)
        if isinstance(f7, bytes):
            sf7 = _parse_proto_fields(f7)
            call_id_b = _find_field(sf7, 1)
            tool_name_b = _find_field(sf7, 2)
            tool_args_b = _find_field(sf7, 3)
            call_id = call_id_b.decode("utf-8", errors="ignore") if isinstance(call_id_b, bytes) else ""
            tool_name = tool_name_b.decode("utf-8", errors="ignore") if isinstance(tool_name_b, bytes) else ""
            tool_args = tool_args_b.decode("utf-8", errors="ignore") if isinstance(tool_args_b, bytes) else ""
            return (tool_name, tool_args, call_id)
    return ("", "", "")


def _extract_tool_result(payload: bytes | None) -> tuple[str, int]:
    """Extract (output_text, exit_code) from a step_type == 132 payload."""
    if not payload:
        return ("", 0)
    fields = _parse_proto_fields(payload)
    f140 = _find_field(fields, 140)
    out_text = ""
    exit_code = 0
    if isinstance(f140, bytes):
        sf140 = _parse_proto_fields(f140)
        f2 = _find_field(sf140, 2)
        if isinstance(f2, bytes):
            sf2 = _parse_proto_fields(f2)
            out_b = _find_field(sf2, 1)
            if isinstance(out_b, bytes):
                out_text = out_b.decode("utf-8", errors="ignore")
                m = re.search(r"exited with code (\d+)", out_text)
                if m:
                    exit_code = int(m.group(1))
    return (out_text, exit_code)


# --------------------------------------------------------------------------- #
# Workspace URI & Project Resolution
# --------------------------------------------------------------------------- #

def _normalize_workspace_uri(uri: str) -> str:
    """Normalize file:/// URI to a local directory path."""
    if not uri:
        return ""
    raw = uri
    if raw.startswith("file:///"):
        raw = raw[8:]
    elif raw.startswith("file://"):
        raw = raw[7:]
    unquoted = urllib.parse.unquote(raw)
    if _is_windows_path(unquoted):
        # Windows 盘符路径：在任何宿主 OS 下都按 Windows 语义规范化
        # （在 Windows 上与 Path.resolve() 结果一致；在 POSIX 上避免
        # 把 "C:/..." 当相对路径拼到 cwd 上）。盘符统一大写：App 写
        # file:///c%3A/、CLI 写 file:///C:/，同一工作区不能拆成两份。
        p = _norm_path(unquoted)
        if len(p) >= 2 and p[1] == ":":
            p = p[0].upper() + p[1:]
        return p
    p = Path(unquoted)
    try:
        # Resolve to handle drive letters and backslashes uniformly
        return str(p.resolve())
    except Exception:
        return str(p)


def _project_display_name(workspace_dir: str) -> str:
    """Return a concise display name for a workspace path."""
    if not workspace_dir:
        return "Antigravity 项目"
    # basename_of：Windows 盘符路径在任何宿主 OS 下都按 Windows 语义取
    # （在 Windows 上与 Path(workspace_dir).name 一致）。
    return _basename_of(workspace_dir) or workspace_dir


def _connect_ro(path: Path | str) -> sqlite3.Connection:
    """Open SQLite connection in immutable read-only mode."""
    p = Path(path).resolve()
    # Use URI filename for read-only connection
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


# --------------------------------------------------------------------------- #
# Project Discovery
# --------------------------------------------------------------------------- #

# 子代理折叠（2026-11 启用）：父指针 = summaries.parent_conversation_id ∪
# 子会话 db 自带的 trajectory_metadata_blob 字段 5（2026-11-17 实测两者
# 120/120 逐条一致；字段 6 = 根会话 id，4.1 = worker 名）。blob 兜底覆盖
# summaries 尚未刷入新 worker 行 / 被 GUI Language Server 刷回子集的情形。
# 缓存按 summaries 库（含 -wal）签名失效——旧版进程级快照让 TCER 运行
# 期间新派生的 Swarm worker 全部独立成「(无标题)」卡（用户截图 2026-11-17）。
_SummariesInfo = namedtuple("_SummariesInfo", "parents names killed titles cids")

# normcase(root) → (签名, summaries 快照)
_SUMMARIES_INFO: dict[str, tuple[tuple, _SummariesInfo]] = {}


def _file_sig(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _db_sig(db: Path) -> tuple:
    """SQLite 库签名：主文件 + WAL（WAL 模式下写入先落 -wal，主文件不变）。"""
    return (_file_sig(db), _file_sig(db.with_name(db.name + "-wal")))


def _summaries_info(root: Path) -> _SummariesInfo:
    """summaries 表的 (父指针, worker 名, killed 集, 标题) 一次读出，按库签名缓存。

    只缓存**确定性**结果：旧 schema 缺列等明确失败缓存空快照（列按 PRAGMA
    实际存在降级）；瞬时失败（Antigravity GUI 正在写库时的 database is
    locked 等）本次返回空、不缓存——缓存了会把「瞬时读不到」固化，数值随
    读取时机漂移（code-review F4）。
    """
    key = os.path.normcase(str(root))
    summaries_db = root / "conversation_summaries.db"
    sig = _db_sig(summaries_db)
    cached = _SUMMARIES_INFO.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    parents: dict[str, str] = {}
    names: dict[str, str] = {}
    killed: set[str] = set()
    titles: dict[str, str] = {}
    cids: set[str] = set()
    if summaries_db.is_file():
        try:
            with _connect_ro(summaries_db) as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
                # 按列名取值（旧实现按位置取，缺中间列时会错位）
                opt = [c for c in ("parent_conversation_id", "agent_name", "killed", "title") if c in cols]
                sel = ", ".join(["conversation_id", *opt])
                for row in conn.execute(f"SELECT {sel} FROM conversation_summaries"):
                    cid = row[0]
                    if not cid:
                        continue
                    cids.add(cid)
                    r = dict(zip(opt, row[1:]))
                    par = r.get("parent_conversation_id")
                    if par and par != cid:
                        parents[cid] = par
                    if r.get("agent_name"):
                        names[cid] = str(r["agent_name"])
                    if r.get("killed"):
                        killed.add(cid)
                    t = r.get("title")
                    if isinstance(t, str) and t.strip():
                        titles[cid] = t.strip()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" not in msg and "no such table" not in msg:
                return _SummariesInfo(parents, names, killed, titles, cids)  # 锁冲突等瞬时错误：不写入缓存
        except Exception:
            return _SummariesInfo(parents, names, killed, titles, cids)  # 库损坏等未知错误：同样只影响本次
    info = _SummariesInfo(parents, names, killed, titles, cids)
    _SUMMARIES_INFO[key] = (sig, info)
    return info


# normcase(db) → (签名, 父 cid 或 "")；瞬时失败不缓存
_BLOB_PARENT: dict[str, tuple[tuple, str]] = {}


def _blob_parent(db: Path) -> str | None:
    """子会话 db 自带的父指针：trajectory_metadata_blob 字段 5（主会话无此字段 → ""）。

    瞬时读失败返回 None（调用方据此不缓存索引）。
    """
    key = os.path.normcase(str(db))
    sig = _db_sig(db)
    cached = _BLOB_PARENT.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    par = ""
    try:
        with _connect_ro(db) as conn:
            row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
        if row and row[0]:
            v = _find_field(_parse_proto_fields(row[0]), 5)
            if isinstance(v, bytes):
                par = v.decode("utf-8", errors="ignore").strip()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            return None  # 瞬时锁：本次视作无父指针，不缓存
    except Exception:
        return None
    if par == db.stem:
        par = ""
    _BLOB_PARENT[key] = (sig, par)
    return par


# 全根父指针索引 [key, {cid: parent}, {parent: [cid, ...]}]
# key = 各根 summaries 库签名 + conversations 目录 mtime（NTFS/ext4 在目录内
# 增删改名条目时更新目录 mtime；SQLite 新建 -wal/-journal 同样会触发）。
# 旧版每次调用 listdir 全部根（一轮分析 ~570 次）热态多花 ~0.5s。
_PARENT_INDEX: list = [None, {}, {}]


def _parent_index_full() -> tuple[dict[str, str], dict[str, list[str]]]:
    """(cid → 父 cid, 父 cid → [子 cid])，跨全部根。

    summaries 有该会话行则以其父指针为准（2026-11-17 普查与 blob 字段 5
    逐条一致）；summaries 无该行（尚未刷入 / 被 Language Server 刷回子集 /
    根无目录库）才读 db 自带 blob 字段 5 兜底——只读这一小撮，冷态不必
    逐个打开全部会话库。任一读取瞬时失败时本次结果不缓存。
    """
    roots = antigravity_roots()
    key = tuple(
        (os.path.normcase(str(r)), _db_sig(r / "conversation_summaries.db"), _file_sig(r / "conversations"))
        for r in roots
    )
    if _PARENT_INDEX[0] == key:
        return _PARENT_INDEX[1], _PARENT_INDEX[2]
    cacheable = True
    parents: dict[str, str] = {}
    known: set[str] = set()
    for root in roots:
        info = _summaries_info(root)
        cached = _SUMMARIES_INFO.get(os.path.normcase(str(root)))
        if (root / "conversation_summaries.db").is_file() and (cached is None or cached[1] is not info):
            cacheable = False  # summaries 瞬时读失败
        parents.update(info.parents)
        known |= info.cids
    for root in roots:
        try:
            names = os.listdir(root / "conversations")
        except OSError:
            continue
        for name in names:
            if not name.endswith(".db"):
                continue
            cid = name[:-3]
            if cid in known or cid in parents:
                continue
            par = _blob_parent(root / "conversations" / name)
            if par is None:
                cacheable = False
            elif par:
                parents[cid] = par
    children: dict[str, list[str]] = {}
    for cid, par in parents.items():
        children.setdefault(par, []).append(cid)
    if cacheable:
        _PARENT_INDEX[:] = [key, parents, children]
    return parents, children


def _parent_index() -> dict[str, str]:
    """cid → 父 cid（跨全部根）。"""
    return _parent_index_full()[0]


def _summaries_parent_map(root: Path) -> dict[str, str]:
    return _summaries_info(root).parents


def session_title(cid: str) -> str:
    """客户端显示的会话标题（summaries.title，跨根任一非空即取）；无则空串。"""
    for root in antigravity_roots():
        t = _summaries_info(root).titles.get(cid)
        if t:
            return t
    return ""


def killed_subagent_cids() -> set[str]:
    """全部根 summaries 中 killed=1 的会话 id 并集（跨根同 cid 副本任一侧标记即算）。"""
    out: set[str] = set()
    for root in antigravity_roots():
        out |= _summaries_info(root).killed
    return out


def killed_subagent_count(path: Path) -> int:
    """折叠子树中被协调者移除的子代理数（Antigravity Swarm 的 killed worker）。

    被 kill 的 worker 产出被丢弃，其已消耗的 Token 是纯浪费——父会话卡上
    显示该数。跨根判定同 _is_subagent_cid：任一根标记即算。
    """
    subs = subagent_dbs(path)
    if not subs:
        return 0
    killed = killed_subagent_cids()
    return sum(1 for p in subs if p.stem in killed)


def subagent_names(path: Path) -> tuple[str, ...]:
    """折叠子树的具名 worker 名（summaries.agent_name），按子 db 顺序。

    只收真正具名的 worker（Swarm 的 @parser/@verifier 式命名）——编排会话
    一眼可辨的信号；`self`（invoke_subagent 并行子代理的通用名）与空串不算
    具名，混进名单只会制造「self、self、…」噪声。
    """
    subs = subagent_dbs(path)
    if not subs:
        return ()
    names: dict[str, str] = {}
    for root in antigravity_roots():
        names.update(_summaries_info(root).names)
    out = []
    for p in subs:
        n = names.get(p.stem)
        if n and n != "self":
            out.append(n)
    return tuple(out)


def reset_summaries_cache() -> None:
    """清空 summaries / blob 父指针缓存（测试用；生产按库签名自动失效）。"""
    _SUMMARIES_INFO.clear()
    _BLOB_PARENT.clear()
    _PARENT_INDEX[:] = [None, {}, {}]
    _SUBTREE_MEMO.clear()


reset_parent_map_cache = reset_summaries_cache  # 旧名兼容（原实现引用已删除的 _PARENT_MAPS）


def _is_subagent_cid(cid: str, index: dict[str, str] | None = None) -> bool:
    """cid 是否带 parent 指针且父 db 在任一根存在的子会话（**跨根**判定）。

    同一会话 id 跨根多副本（拷贝的 profile、summaries 被一侧刷掉）必须整体
    认定：只按副本所在根判定会一侧折叠、另一侧独立成卡 → Token/LOC 双计
    （code-review F3；旧 best-by-cid 去重的保证不能丢）。父指针来自全根
    索引（summaries ∪ blob 字段 5），天然跨根。
    """
    par = (index if index is not None else _parent_index()).get(cid)
    if not par:
        return False
    return any((r / "conversations" / f"{par}.db").is_file() for r in antigravity_roots())


def subagent_dbs(path: Path) -> list[Path]:
    """Child conversation dbs of ``path`` per the parent index (summaries ∪ blob 字段 5).

    children 从**所有根**枚举（best-by-mtime 选最新副本，与 `_workspace_sessions`
    的跨根去重语义一致——父副本在哪根不影响子会话发现，code-review F3）。
    深度优先收齐嵌套子会话（实测 depth 全为 1，递归只作防御）；父 db 已被
    清理的孤儿由调用方按「父不存在仍独立」语义处理。防环：visited 集合。
    """
    path = Path(path)
    if not path.is_file():
        return []
    by_parent = _parent_index_full()[1]
    # 子树候选按索引版本记忆（目录 mtime 已覆盖候选文件的增删）；仅跨根
    # 多副本时才逐次比较 mtime 选最新——单副本无可选，结果与逐次 stat 一致。
    # 瞬时读失败时返回的是未缓存的新索引（by_parent 不是缓存对象）→ 不走记忆
    memoable = by_parent is _PARENT_INDEX[2] and _PARENT_INDEX[0] is not None
    memo_key = (_PARENT_INDEX[0], path.stem)
    cand_lists = _SUBTREE_MEMO.get(memo_key) if memoable else None
    if cand_lists is None:
        roots = antigravity_roots()
        conv_dirs = [r / "conversations" for r in roots]
        cand_lists = []
        visited = {path.stem}
        stack = [path.stem]
        while stack:
            cur = stack.pop(0)
            for cid in by_parent.get(cur, ()):
                if cid in visited:
                    continue
                visited.add(cid)
                cands = [c / f"{cid}.db" for c in conv_dirs]
                cands = [c for c in cands if c.is_file()]
                if cands:
                    cand_lists.append(cands)
                    stack.append(cid)
        if memoable:
            if len(_SUBTREE_MEMO) > 4096:
                _SUBTREE_MEMO.clear()
            _SUBTREE_MEMO[memo_key] = cand_lists
    out = [c[0] if len(c) == 1 else max(c, key=lambda p: p.stat().st_mtime) for c in cand_lists]
    out.sort(key=lambda p: p.stem)
    return out


# (索引 key, 父 cid) → 各子会话的候选副本列表；索引重建即自然失效
_SUBTREE_MEMO: dict[tuple, list[list[Path]]] = {}


def _is_subagent_path(path: Path) -> bool:
    """True when ``path`` is a child conversation whose parent db still exists."""
    return _is_subagent_cid(Path(path).stem)


def _root_entries(root: Path) -> list[tuple[str, list[str]]]:
    """``(conversation_id, [normalized_workspace, ...])`` for one data root, newest first.

    Union of ``conversation_summaries.db`` and a direct glob of
    ``conversations/*.db`` (deduped by conversation id, catalog wins).
    The catalog alone is not reliable: when CLI and GUI share a machine,
    closing the GUI's Language Server flushes its in-memory subset back into
    the catalog and wipes unmapped CLI rows, so sessions that exist on disk
    would silently disappear if only the catalog were read. Sessions missing
    from the catalog are recovered from each session db's
    ``trajectory_metadata_blob``. Roots without a catalog (App/IDE may not
    keep one) are covered by the glob alone.
    """
    entries: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    summaries_db = root / "conversation_summaries.db"
    if summaries_db.is_file():
        try:
            with _connect_ro(summaries_db) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries "
                    "ORDER BY last_modified_time DESC"
                )
                for cid, uris_raw in cur.fetchall():
                    if not uris_raw:
                        continue
                    try:
                        uris = json.loads(uris_raw)
                    except Exception:
                        uris = [uris_raw]
                    wss: list[str] = []
                    for u in uris:
                        ws = _normalize_workspace_uri(u)
                        if ws and ws not in wss:
                            wss.append(ws)
                    if wss:
                        entries.append((cid, wss))
                        seen.add(cid)
        except Exception:
            pass

    conv_dir = root / "conversations"
    if not conv_dir.is_dir():
        return entries
    for db_file in sorted(conv_dir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True):
        if db_file.stem in seen:
            continue
        try:
            with _connect_ro(db_file) as conn:
                cur = conn.cursor()
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
                row = cur.fetchone()
                if row and row[0]:
                    # Quick extract file:///... from bytes
                    m = re.search(rb"file:///[^\x00-\x1f\x7f-\xff]+", row[0])
                    if m:
                        ws = _normalize_workspace_uri(m.group(0).decode("utf-8", errors="ignore"))
                        if ws:
                            entries.append((db_file.stem, [ws]))
                            seen.add(db_file.stem)
        except Exception:
            continue
    return entries


def _workspace_sessions() -> dict[str, list[Path]]:
    """Group session db files from every Antigravity root by normalized workspace.

    Roots = CLI + desktop App + IDE + config surfaces plus cc-switch isolated
    profiles (:func:`tcer.core.paths.antigravity_roots`). The same workspace across roots
    merges into one group. A conversation id present in several roots (copied
    profile) is counted once -- the most recently modified file wins.
    子代理会话（父指针 = summaries ∪ blob 字段 5，指向存在父 db 的）不单独成
    卡：Token/LOC 由 aggregate_usage/session_loc_full 折叠进父会话（原 #5），
    父 db 缺失的孤儿仍独立展示。
    """
    entries: list[tuple[str, list[str]]] = []
    best: dict[str, tuple[int, Path]] = {}
    index = _parent_index()
    for root in antigravity_roots():
        conv_dir = root / "conversations"
        for cid, wss in _root_entries(root):
            if _is_subagent_cid(cid, index):
                continue  # 子会话（跨根判定，防副本一侧独立成卡双计）：折叠进父
            entries.append((cid, wss))
            p = conv_dir / f"{cid}.db"
            try:
                mt = p.stat().st_mtime_ns
            except OSError:
                continue
            if not p.is_file():
                continue
            prev = best.get(cid)
            if prev is None or mt > prev[0]:
                best[cid] = (mt, p)

    # 按 normcase 合并（CLI 写 file:///C:/、App 写 file:///c%3A/，同一工作区
    # 曾因盘符大小写拆成两个项目）；分组键取首见写法（盘符已统一大写）。
    groups: dict[str, list[Path]] = {}
    canon: dict[str, str] = {}
    for cid, wss in entries:
        chosen = best.get(cid)
        for ws in wss:
            ws = canon.setdefault(_normcase_path(ws), ws)
            lst = groups.setdefault(ws, [])
            if chosen is not None and chosen[1] not in lst:
                lst.append(chosen[1])
    return groups


def list_project_refs() -> list[ProjectRef]:
    """Enumerate Antigravity projects by grouping conversations by workspace (across all roots)."""
    refs: list[ProjectRef] = []
    for ws, paths in _workspace_sessions().items():
        disp = _project_display_name(ws)
        refs.append(
            ProjectRef(
                source="antigravity",
                key=disp,
                display_name=disp,
                cwd=ws,
                path=paths[0].parent if paths else antigravity_conversations_dir(),
                session_paths=tuple(paths),
            )
        )

    return sorted(refs, key=lambda r: r.display_name.lower())



def resolve_project(project: str) -> ProjectRef | None:
    """Resolve an Antigravity project key/display substring to a project ref."""
    refs = list_project_refs()
    for ref in refs:
        if ref.key == project or ref.display_name == project:
            return ref
    needle = project.lower()
    matches = [
        r for r in refs
        if needle in r.key.lower()
        or needle in r.display_name.lower()
        or (r.cwd and needle in r.cwd.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def discover_sessions(project: str | ProjectRef) -> list[Path]:
    """Return all session .db paths belonging to the given ProjectRef or project string."""
    if isinstance(project, str):
        ref = resolve_project(project)
        if ref is None:
            return []
    else:
        ref = project

    if ref.session_paths:
        return [p for p in ref.session_paths if p.is_file()]

    return [p for p in _workspace_sessions().get(ref.cwd or "", []) if p.is_file()]




# --------------------------------------------------------------------------- #
# Metadata, Usage, and LOC Parsing
# --------------------------------------------------------------------------- #

def read_session_meta(path: Path) -> SessionMeta:
    """Read session metadata from SQLite conversation database."""
    session_id = path.stem
    title = None
    started_at = None
    cwd = None

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()

            # Read cwd from trajectory_metadata_blob
            try:
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
                row = cur.fetchone()
                if row and row[0]:
                    m = re.search(rb"file:///[^\x00-\x1f\x7f-\xff]+", row[0])
                    if m:
                        cwd = _normalize_workspace_uri(m.group(0).decode("utf-8", errors="ignore"))
            except Exception:
                pass

            # Read started_at timestamp from first step
            try:
                cur.execute("SELECT metadata, step_type, step_payload FROM steps ORDER BY idx ASC LIMIT 5")
                rows = cur.fetchall()
                for r in rows:
                    if started_at is None and r[0]:
                        ts = _decode_timestamp(r[0])
                        if ts is not None:
                            started_at = _format_timestamp(ts)
                    if title is None and r[1] == 14 and r[2]:
                        txt = _extract_user_text(r[2])
                        if txt:
                            title = txt[:80]
            except Exception:
                pass
    except Exception:
        pass

    # 客户端显示的标题（summaries.title，LLM 生成的会话名）优先；无则回退首条提问。
    title = session_title(session_id) or title

    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=path,
        is_subagent=_is_subagent_path(path),
        entrypoint="agy",
        source="antigravity",
    )


def aggregate_usage(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> TokenUsage:
    """Aggregate token usage, tool metrics, and turn timelines from an Antigravity .db.

    invoke_subagent 派生的子会话（独立 conversation db）折叠并入父会话
    （原 #5：Token 保留真实成本，不单独计 session）；merge 的逐回合字段
    rebase（turn_stats/compaction_turns/turn_net_locs）由 TokenUsage.merge
    统一处理。子会话的 step 14 实测为空，user 信号另在解析层按 is_sub 跳过
    作防御（同 omp _aggregate_single(is_subagent=True) 口径）。
    subagent_dbs 已含全部嵌套后代，逐个折叠一次即可，不再递归。
    """
    u = _aggregate_single(path, cancel_check)
    for sub in subagent_dbs(path):
        u = u.merge(_aggregate_single(sub, cancel_check, is_sub=True))
    return u


def _aggregate_single(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
    is_sub: bool = False,
) -> TokenUsage:
    """Parse one conversation db (no subagent folding)."""
    path = Path(path)
    u = TokenUsage()
    if not path.is_file():
        return u

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()

            # 1. gen_metadata: Authoritative token usages per generation
            cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC")
            gen_rows = cur.fetchall()
            decoded_gens = []
            gen_ids: set[str] = set()
            mcp_servers: set[str] = set()  # 权限条目里出现的 MCP server（拆 mcp_<srv>_<tool> 用）
            for _, data in gen_rows:
                if cancel_check:
                    cancel_check()
                if not data:
                    continue
                mcp_servers |= _mcp_servers_in(data)
                rid = _gen_response_id(data)
                if rid:
                    gen_ids.add(rid)
                model, uncached_in, total_out, cached_in, reasoning = _decode_gen_metadata(data)
                total_in = uncached_in + cached_in
                if total_in == 0 and total_out == 0:
                    # 零 usage 错误桩（如请求前 400 失败）不产生 step_type=15，
                    # 若写入 decoded_gens 会使后续 turn_stats 整体错位 1 回合。
                    u.empty_usage_skipped += 1
                    continue
                decoded_gens.append((model, uncached_in, total_out, cached_in, reasoning))

                # 口径同其余源：input_tokens 只记未命中缓存部分，缓存命中单列
                # cache_read（total_input = 二者之和）。曾把 total_in 记进 input，
                # 缓存被计两遍且按全价计费（实测 Token ×2、成本 ×7.7）。
                u.input_tokens += uncached_in
                u.output_tokens += total_out
                u.cache_read_input_tokens += cached_in
                u.reasoning_output_tokens += reasoning
                u.peak_input_tokens = max(u.peak_input_tokens, total_in)

                if model:
                    norm_m = pricing.normalize(model)
                    u.models.add(norm_m)
                    mu = u.bucket(norm_m)
                    mu.input_tokens += uncached_in
                    mu.output_tokens += total_out
                    mu.cache_read_input_tokens += cached_in

            # 2. steps: Timelines, user messages, tool calls, errors
            cur.execute(
                "SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            first_ts = None
            last_ts = None
            # turn 0-based（与其余数据源一致；llm_prompts 里程碑提取与
            # ground_dynamics_user_turns 均按 0-based 键消费 turn_stats）
            current_turn = -1
            seen_calls: set[str] = set()

            def _record_call(name: str, args_json: str) -> None:
                canon_tool = _canon_tool(name, args_json, mcp_servers)
                u.tool_calls[canon_tool] = u.tool_calls.get(canon_tool, 0) + 1
                # Extract file path hint for tool_ops
                fp_hint = ""
                if args_json:
                    try:
                        args = json.loads(args_json)
                        fp_hint = _arg(
                            args, "TargetFile", "AbsolutePath", "Path",
                            "file_path", "path",
                        )
                    except Exception:
                        pass
                u.tool_ops.append(ToolOp(max(current_turn, 0), canon_tool, fp_hint))

            for _, stype, status, meta, payload in step_rows:
                if cancel_check:
                    cancel_check()

                ts = _decode_timestamp(meta)
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                ts_ms = int(ts * 1000) if ts is not None else None

                if stype == 14:  # User input
                    # 子代理的 step 14 实测为空（派发 prompt 不落子会话）；
                    # is_sub 守卫防上游日后写入派发 prompt 被当用户提问（原 #15）。
                    if is_sub:
                        continue
                    u.user_msgs += 1
                    txt = _extract_user_text(payload)
                    if u.first_prompt_chars == 0 and txt:
                        u.first_prompt_chars = len(txt)
                    if _is_slash_command(txt):
                        u.slash_command_count += 1
                    if _is_correction(txt):
                        u.correction_msg_count += 1

                elif stype == 15:  # Assistant turn / tool call
                    current_turn += 1
                    u.assistant_msgs += 1
                    # 并行工具调用：字段 20 下多个字段 7，逐个计
                    calls = _extract_tool_calls(payload)
                    for tool_name, tool_args_json, call_id in calls:
                        if call_id:
                            seen_calls.add(call_id)
                        _record_call(tool_name, tool_args_json)

                    # Add turn stat entry
                    u.turn_stats.append(
                        TurnStat(
                            turn=current_turn,
                            ts=ts_ms,
                            tool_calls=len(calls),
                            errors=0,
                            user_turn=u.user_msgs if u.user_msgs > 0 else 1,
                        )
                    )

                elif stype == 132:  # Tool result
                    # status 5 裁剪掉了 step 15 的调用体，结果步骤字段 5.4 仍带原调用：补计
                    r_name, r_args, r_cid = _extract_result_call(payload)
                    if r_name and r_cid and r_cid not in seen_calls:
                        seen_calls.add(r_cid)
                        _record_call(r_name, r_args)
                        if u.turn_stats:
                            u.turn_stats[-1].tool_calls += 1
                    _, exit_code = _extract_tool_result(payload)
                    if _result_failed(status, exit_code):
                        u.tool_errors += 1
                        if u.turn_stats:
                            u.turn_stats[-1].errors += 1

                elif stype == 23:  # Context compaction (checkpoint summary)
                    u.compaction_count += 1
                    u.compaction_turns.append(current_turn + 1)
                    # 压缩调用的 usage 只在步骤元数据里、不进 gen_metadata（按响应 id
                    # 去重以防上游日后补写）；记录不带模型，按前一次生成的模型计价（推断）。
                    c_unc, c_out, c_cached, c_reas, c_rid = _compaction_usage(payload)
                    if (c_unc or c_out or c_cached) and not (c_rid and c_rid in gen_ids):
                        if c_rid:
                            gen_ids.add(c_rid)
                        u.input_tokens += c_unc
                        u.output_tokens += c_out
                        u.cache_read_input_tokens += c_cached
                        u.reasoning_output_tokens += c_reas
                        u.peak_input_tokens = max(u.peak_input_tokens, c_unc + c_cached)
                        prev_model = ""
                        for gi in range(min(current_turn, len(decoded_gens) - 1), -1, -1):
                            if decoded_gens[gi][0]:
                                prev_model = decoded_gens[gi][0]
                                break
                        if not prev_model:
                            prev_model = next((g[0] for g in decoded_gens if g[0]), "")
                        if prev_model:
                            mu = u.bucket(pricing.normalize(prev_model))
                            mu.input_tokens += c_unc
                            mu.output_tokens += c_out
                            mu.cache_read_input_tokens += c_cached

            # 3. Map decoded generation tokens to turn_stats
            for i, (m, u_in, t_out, c_in, _) in enumerate(decoded_gens):
                if i < len(u.turn_stats):
                    stat = u.turn_stats[i]
                    stat.input_tokens = u_in
                    stat.cache_read = c_in
                    stat.output_tokens = t_out
                    if m:
                        stat.model = pricing.normalize(m)

            # Calculate inter-turn duration
            for i in range(len(u.turn_stats) - 1):
                cur_t = u.turn_stats[i].ts
                nxt_t = u.turn_stats[i + 1].ts
                if cur_t is not None and nxt_t is not None and nxt_t >= cur_t:
                    u.turn_stats[i].duration_ms = nxt_t - cur_t

            if first_ts is not None and last_ts is not None and last_ts >= first_ts:
                u.started_at = int(first_ts * 1000)
                u.ended_at = int(last_ts * 1000)
                u.session_duration_ms = int((last_ts - first_ts) * 1000)

    except Exception:
        pass

    return u


def session_loc_full(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[loc.SessionLoc, bool]:
    """Replay file-modifying tools to compute git-free LOC and rework metrics.

    LOC 按结果裁决（CLAUDE.md #32）：编辑类调用等到 step 132 结果且成功
    （status 0/3/5）才回放；status 7 的失败编辑（实测 "Could not successfully
    apply"）不计。调用体优先取结果字段 5.4（裁剪后的 status 5 步骤只剩这份），
    缺失时按 step 15 调用的出现顺序配对（实测结果严格按调用顺序到达）。
    子代理会话经 loc.merge_session_locs 并入父会话（原 #13：合并后重算
    high_churn；与 omp scan_folded 同构，无信号子会话不产出空 LOC）。
    subagent_dbs 已含全部嵌套后代，逐个回放一次即可，不再递归。

    Returns (SessionLoc, has_loc_signal).
    """
    sloc, has_signal = _loc_single(path, cancel_check)
    subs = subagent_dbs(path)
    if not subs:
        return sloc, has_signal
    slocs = [sloc] if has_signal else []
    any_signal = has_signal
    for sub in subs:
        sl, hs = _loc_single(sub, cancel_check)
        if hs:
            slocs.append(sl)
            any_signal = True
    if not slocs:
        return loc.SessionLoc(added=0, deleted=0), False
    return loc.merge_session_locs(slocs), any_signal


def _loc_single(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[loc.SessionLoc, bool]:
    """Replay one conversation db (no subagent folding)."""
    path = Path(path)
    acc = loc._LocAccumulator()
    has_signal = False

    if not path.is_file():
        return acc.finish(), False

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            # turn 0-based（与 aggregate_usage 同口径，turn_net_locs 键空间一致）
            current_turn = -1
            pending: list[tuple[str, str, str]] = []  # (name, args, call_id) awaiting result
            for _, stype, status, payload in step_rows:
                if cancel_check:
                    cancel_check()

                if stype == 15:
                    current_turn += 1
                    pending.extend(_extract_tool_calls(payload))
                    continue
                if stype != 132:
                    continue

                tool_name, tool_args_json, call_id = _extract_result_call(payload)
                if tool_name:
                    pending = [c for c in pending if not (call_id and c[2] == call_id)]
                elif pending:
                    tool_name, tool_args_json, call_id = pending.pop(0)
                if not tool_name or status not in (0, 3, 5):
                    continue

                canon_tool = _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)
                if canon_tool not in ("Write", "Edit"):
                    continue
                try:
                    args = json.loads(tool_args_json)
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                target = _arg(args, "TargetFile", "target_file", "Path", "file_path")
                if not target:
                    continue
                has_signal = True
                if canon_tool == "Write":
                    # write_to_file: TargetFile/CodeContent; write_file: Path/Content
                    content = _arg(args, "CodeContent", "code_content", "Content", "content")
                    acc.on_tool_use(
                        "Write",
                        {"file_path": target, "content": content},
                        turn=current_turn,
                    )
                else:
                    # replace_file_content: TargetFile/TargetContent/ReplacementContent;
                    # edit_file: Path/Target/Replacement
                    old_content = _arg(args, "TargetContent", "target_content", "Target")
                    new_content = _arg(
                        args, "ReplacementContent", "replacement_content", "Replacement"
                    )
                    acc.on_tool_use(
                        "Edit",
                        {
                            "file_path": target,
                            "old_string": old_content,
                            "new_string": new_content,
                        },
                        turn=current_turn,
                    )

    except Exception:
        pass

    return acc.finish(), has_signal


def read_user_messages(path: Path) -> list[str]:
    """Extract all user prompts from the conversation."""
    return [t for t, _ in read_user_messages_timed(path)]


def read_user_messages_timed(path: Path) -> list[tuple[str, int | None]]:
    """同 :func:`read_user_messages`，每条附 step 元数据时间戳（epoch ms / None）。"""
    msgs: list[tuple[str, int | None]] = []
    if not path.is_file():
        return msgs

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT metadata, step_payload FROM steps WHERE step_type = 14 ORDER BY idx ASC")
            for meta, payload in cur.fetchall():
                txt = _extract_user_text(payload)
                if txt:
                    ts = _decode_timestamp(meta)
                    msgs.append((txt, int(ts * 1000) if ts is not None else None))
    except Exception:
        pass
    return msgs


def read_conversation(path: Path) -> list[dict]:
    """Reconstruct conversational timeline for full-transcript viewing or upload."""
    events: list[dict] = []
    if not path.is_file():
        return events

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx ASC"
            )
            for idx, stype, meta, payload in cur.fetchall():
                ts = _decode_timestamp(meta)
                iso_ts = _format_timestamp(ts)
                if stype == 14:
                    txt = _extract_user_text(payload)
                    events.append({
                        "role": "user",
                        "content": txt,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
                elif stype == 15:
                    tool_name, tool_args, call_id = _extract_tool_call(payload)
                    events.append({
                        "role": "assistant",
                        "tool_call": tool_name,
                        "tool_args": tool_args,
                        "call_id": call_id,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
                elif stype == 132:
                    out_text, exit_code = _extract_tool_result(payload)
                    events.append({
                        "role": "tool_result",
                        "content": out_text,
                        "exit_code": exit_code,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
    except Exception:
        pass
    return events
