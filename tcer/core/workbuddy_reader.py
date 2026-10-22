"""WorkBuddy / CodeBuddy local-session reader for TCER.

WorkBuddy persists sessions as JSONL streams containing:
  * ``type: "session-meta"``: Session ID and host metadata.
  * ``type: "ai-title"``: AI-generated session title and working directory.
  * ``type: "message"``:
    - role == "user": User queries (may contain ``<system-reminder>`` context
      and ``<user_query>`` wrappers).
    - role == "assistant": Final model textual response.
  * ``type: "reasoning"``: Model reasoning / thinking content.
  * ``type: "function_call"``: Tool invocation (Read, Edit, Write, Grep, Glob,
    PowerShell, Bash, ToolSearch, etc.) with JSON arguments and authoritative
    step token usage under ``providerData.rawUsage``.
  * ``type: "function_call_result"``: Tool output and execution status.

This module parses WorkBuddy session JSONL files into TCER's canonical models
(``TokenUsage``, ``SessionMeta``, ``SessionLoc``, and conversation block timeline).
It provides robust project root detection so ordinary workbuddy sessions merge into
a single 'WorkBuddy' entry, while directory-based projects merge and reduce to the
directory name (e.g. 'cicd', 'vxtest', 'slg') and align with their actual code workspace.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterator

from tcer.core import loc, pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import (
    as_int as _as_int,
    is_correction as _is_correction,
    is_slash_command as _is_slash_command,
)
from tcer.core.paths import encode_hash, workbuddy_dir, workbuddy_roots, workbuddy_sessions_dir

_NO_CWD_KEY = "__workbuddy_no_cwd__"
_NO_CWD_LABEL = "WorkBuddy 无工作目录"

# WorkBuddy tool name -> TCER canonical tool name
_WORKBUDDY_TOOL_MAP = {
    "read": "Read",
    "read_file": "Read",
    "edit": "Edit",
    "edit_file": "Edit",
    "write": "Write",
    "write_file": "Write",
    "grep": "Grep",
    "grep_search": "Grep",
    "glob": "Glob",
    "bash": "Bash",
    "powershell": "Bash",
    "cmd": "Bash",
    "terminal": "Bash",
    "toolsearch": "ToolSearch",
    "deferexecutetool": "DeferExecuteTool",
    "present_files": "present_files",
}

_SYS_REMINDER_RE = re.compile(
    r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>", re.IGNORECASE
)
_USER_QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>", re.IGNORECASE)
_MSG_QUOTE_RE = re.compile(
    r'<message_quote\b[^>]*>[\s\S]*?<original_text><!\[CDATA\[([\s\S]*?)\]\]></original_text>[\s\S]*?</message_quote>',
    re.IGNORECASE,
)
_TS_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}|\d{8}|\d{4}-\d{2}-\d{2})\b"
)


def clean_user_text(raw: str) -> str:
    """Clean system reminders and unwrap user queries from WorkBuddy user input."""
    if not raw:
        return ""
    t = _SYS_REMINDER_RE.sub("", raw).strip()

    def _quote_repl(m):
        quoted = m.group(1).strip()
        lines = [f"> {line}" for line in quoted.split("\n")]
        return "\n" + "\n".join(lines) + "\n\n"

    t = _MSG_QUOTE_RE.sub(_quote_repl, t).strip()
    m = _USER_QUERY_RE.search(t)
    if m:
        t = m.group(1).strip()
    return t


def sanitize_path(p: str) -> str | None:
    """Normalize and filter raw path string for project root detection."""
    if not isinstance(p, str):
        return None
    p = p.strip(' \t\r\n"\':;,')
    p = p.replace("\\", "/")
    # Normalize /e/slg -> E:/slg
    if re.match(r"^/[a-zA-Z]/", p):
        p = p[1].upper() + ":" + p[2:]
    elif re.match(r"^[a-zA-Z]:", p):
        p = p[0].upper() + p[1:]
    elif p.startswith("/"):
        pass  # Unix absolute path
    else:
        return None

    low = p.lower()
    if "workbuddy" in low or ".workbuddy" in low or "codebuddy" in low:
        return None
    if any(low.startswith(x) for x in ["c:/windows", "c:/program files", "c:/users", "c:/temp", "c:/appdata"]):
        return None
    return p


def extract_project_dir_from_session(path: Path) -> str | None:
    """Extract authoritative business project root directory from a session file.

    Analyzes tool calls (Read/Edit/Write/Grep/Bash commands) to determine the real
    target workspace directory, bypassing temporary timestamped sandbox directories.
    """
    cwd: str | None = None
    file_paths: list[str] = []

    for obj in iter_records(path):
        if not cwd and obj.get("cwd"):
            cwd = str(obj["cwd"])
        t = obj.get("type")
        if t == "function_call":
            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = None
            if isinstance(args, dict):
                fp = args.get("file_path") or args.get("path")
                if fp and isinstance(fp, str):
                    file_paths.append(fp)
                cmd = args.get("command")
                if cmd and isinstance(cmd, str):
                    m = re.findall(r'[a-zA-Z]:[\\/][^"\'\s;|,]+|/[a-zA-Z]/[^"\'\s;|,]+', cmd)
                    file_paths.extend(m)
        elif t == "function_call_result":
            out = obj.get("output")
            if isinstance(out, dict):
                txt = out.get("text", "")
                if txt and isinstance(txt, str):
                    m = re.findall(r'[a-zA-Z]:[\\/][^"\'\s;|,]+|/[a-zA-Z]/[^"\'\s;|,]+', txt)
                    file_paths.extend(m)

    candidate_roots: list[str] = []
    for fp in file_paths:
        sp = sanitize_path(fp)
        if not sp:
            continue
        parts = sp.split("/")
        if len(parts) >= 2 and parts[0].endswith(":"):
            if (
                parts[1].lower() in ("repos", "github", "git", "projects", "workspace", "code", "dev")
                and len(parts) >= 3
            ):
                cand = parts[0] + "/" + parts[1] + "/" + parts[2]
            else:
                cand = parts[0] + "/" + parts[1]
            candidate_roots.append(cand)
        elif len(parts) >= 3 and parts[0] == "":
            if len(parts) >= 4 and parts[1].lower() in ("home", "users", "root"):
                cand = "/" + "/".join(parts[1:4])
            else:
                cand = "/" + parts[1]
            candidate_roots.append(cand)

    if candidate_roots:
        counts = Counter(candidate_roots)
        best_root = counts.most_common(1)[0][0]
        return os.path.normpath(best_root)

    if cwd:
        norm_cwd = os.path.normpath(cwd)
        if not _TS_RE.search(norm_cwd) and not any(k in norm_cwd.lower() for k in ("workbuddy", "codebuddy")):
            return norm_cwd

    return None


def derive_project_name(project_dir: str | None) -> str:
    """Derive display project name from a directory path, reduced to directory name."""
    if not project_dir:
        return "WorkBuddy"
    p = Path(project_dir)
    name = p.name or p.parent.name
    if name and not _TS_RE.search(name) and name.lower() not in ("workbuddy", "workbuddy ai", "codebuddy", "projects"):
        return name
    return "WorkBuddy"


def _collect_known_cwds() -> dict[str, str]:
    """Collect known project working directories from other sources without recursion."""
    known: dict[str, str] = {}
    try:
        from tcer.core import paths
        for src in ("claude", "codex", "omp", "pi", "antigravity"):
            for r in paths.list_project_refs(src):
                if r.cwd and Path(r.cwd).is_dir():
                    dname = Path(r.cwd).name.lower()
                    known[dname] = os.path.normpath(r.cwd)
    except Exception:
        pass
    return known


def resolve_actual_project_cwd(
    project_name: str,
    session_paths: list[Path],
    known_cwds: dict[str, str] | None = None,
) -> str | None:
    """Resolve the real codebase directory on disk for a WorkBuddy project."""
    pname_low = project_name.lower()
    if known_cwds is None:
        known_cwds = _collect_known_cwds()

    # 1. Align with known real project directories across other sources (Claude/omp/Codex)
    if pname_low in known_cwds:
        return known_cwds[pname_low]

    # 2. From session touched paths
    for sp in session_paths:
        try:
            with sp.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if obj.get("type") == "function_call":
                        args = obj.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                pass
                        if isinstance(args, dict):
                            raw_p = args.get("file_path") or args.get("path")
                            if raw_p and isinstance(raw_p, str):
                                p = Path(raw_p)
                                for cand in [p] + list(p.parents):
                                    if (
                                        cand.name.lower() == pname_low
                                        and cand.is_dir()
                                        and "workbuddy" not in str(cand).lower()
                                    ):
                                        return os.path.normpath(str(cand))
        except Exception:
            continue

    return None


def classify_workbuddy_project(p: Path, known_cwds: dict[str, str] | None = None) -> tuple[str, str, str | None]:
    """Classify a WorkBuddy session into (group_key, display_name, real_cwd).

    Rules:
    1. Sessions operating on a concrete business directory (e.g. E:\\slg) merge
       by that directory, reduced to the directory name ('slg').
    2. Project directories created by WorkBuddy (e.g. c-...-WorkBuddy AI-cicd)
       reduce to the directory name ('cicd') and resolve to actual code directory.
    3. Ordinary WorkBuddy sessions (temporary timestamped sandbox directories,
       e.g. WorkBuddy/2026-10-16-... or 2026-10-21-14-47-03) merge into a single
       'WorkBuddy' entry.
    """
    # 1. Check if operations within session targeted a real business project (e.g. E:\slg)
    detected_dir = extract_project_dir_from_session(p)
    if detected_dir:
        norm_dir = os.path.normpath(detected_dir)
        dname = Path(norm_dir).name
        # Ensure it's not a bare drive root like "C:\" or timestamped sandbox
        if dname and not _TS_RE.search(dname) and len(Path(norm_dir).parts) > 1:
            if dname.lower() not in ("workbuddy", "workbuddy ai", "codebuddy", "default", "projects"):
                return dname.lower(), dname, norm_dir

    folder_name = p.parent.name
    is_ts = bool(_TS_RE.search(folder_name))

    meta_cwd = None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(15):
                line = f.readline()
                if not line:
                    break
                obj = json.loads(line)
                if obj.get("cwd"):
                    meta_cwd = str(obj["cwd"])
                    break
    except Exception:
        pass

    if meta_cwd and _TS_RE.search(meta_cwd):
        is_ts = True

    # 2. If folder name represents a specific project directory (not timestamped sandbox)
    if not is_ts:
        parts = folder_name.replace("\\", "/").split("-")
        tail = parts[-1].strip() if parts else folder_name
        if tail.lower() not in ("workbuddy", "workbuddy ai", "codebuddy", "default", "projects"):
            actual_cwd = resolve_actual_project_cwd(tail, [p], known_cwds)
            if not actual_cwd and meta_cwd and not _TS_RE.search(meta_cwd):
                if not any(k in meta_cwd.lower() for k in ("workbuddy", "codebuddy")):
                    actual_cwd = os.path.normpath(meta_cwd)
            return tail.lower(), tail, actual_cwd

    # 3. Ordinary sessions -> merge into a single 'WorkBuddy' entry
    return "workbuddy", "WorkBuddy", None


def iter_records(path: Path) -> Iterator[dict]:
    """Yield parsed JSON records from a WorkBuddy session JSONL."""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, TypeError):
                continue


def read_session_meta(path: Path) -> SessionMeta:
    """Extract lightweight WorkBuddy session metadata."""
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    first_user_text: str | None = None
    models: set[str] = set()

    for obj in iter_records(path):
        ts = obj.get("timestamp")
        if isinstance(ts, (int, float)):
            ts = int(ts)
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

        if not session_id and obj.get("sessionId"):
            session_id = str(obj["sessionId"])
        if not cwd and obj.get("cwd"):
            cwd = str(obj["cwd"])
        if obj.get("type") == "ai-title" and obj.get("aiTitle"):
            title = str(obj["aiTitle"])

        pd = obj.get("providerData")
        if isinstance(pd, dict):
            m = pd.get("model") or pd.get("requestModelName")
            if m:
                models.add(pricing.normalize(str(m)))

        if obj.get("type") == "message" and obj.get("role") == "user" and not first_user_text:
            content = obj.get("content") or []
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            else:
                txt = ""
            clean = clean_user_text(txt)
            if clean:
                first_user_text = clean

    if not session_id:
        session_id = path.stem
    if not title:
        title = (first_user_text[:40] + "…") if first_user_text else session_id

    # Resolve real project directory if cwd is a sandbox
    _, _, real_cwd = classify_workbuddy_project(path)
    if not real_cwd:
        real_cwd = cwd

    return SessionMeta(
        session_id=session_id,
        path=path,
        cwd=real_cwd,
        title=title,
        source="workbuddy",
        is_subagent=False,
    )


def aggregate_usage(path: Path) -> TokenUsage:
    """Aggregate WorkBuddy token, tool, and conversational usage from JSONL."""
    u = TokenUsage()
    seen_mids: set[str] = set()
    turn_idx = 0
    first_user_text: str | None = None

    for obj in iter_records(path):
        t = obj.get("type")
        ts = obj.get("timestamp")
        if isinstance(ts, (int, float)):
            ts = int(ts)
            if u.started_at is None or ts < u.started_at:
                u.started_at = ts
            if u.ended_at is None or ts > u.ended_at:
                u.ended_at = ts

        # Usage from providerData
        pd = obj.get("providerData")
        if isinstance(pd, dict) and ("rawUsage" in pd or "usage" in pd):
            mid = pd.get("messageId")
            if not mid or mid not in seen_mids:
                if mid:
                    seen_mids.add(mid)
                ru = pd.get("rawUsage") or {}
                usg = pd.get("usage") or {}

                inp = _as_int(ru.get("prompt_tokens") or usg.get("inputTokens"))
                out = _as_int(ru.get("completion_tokens") or usg.get("outputTokens"))

                cached = 0
                pt_details = ru.get("prompt_tokens_details") or {}
                if pt_details.get("cached_tokens") is not None:
                    cached = _as_int(pt_details["cached_tokens"])
                elif usg.get("inputTokensDetails"):
                    cached = _as_int(usg["inputTokensDetails"][0].get("cached_tokens"))

                reasoning = 0
                ct_details = ru.get("completion_tokens_details") or {}
                if ct_details.get("reasoning_tokens") is not None:
                    reasoning = _as_int(ct_details["reasoning_tokens"])
                elif usg.get("outputTokensDetails"):
                    reasoning = _as_int(usg["outputTokensDetails"][0].get("reasoning_tokens"))

                u.input_tokens += inp
                u.output_tokens += out
                u.cache_read_input_tokens += cached
                u.reasoning_output_tokens += reasoning
                u.peak_input_tokens = max(u.peak_input_tokens, inp)

                m = pd.get("model") or pd.get("requestModelName") or "gemini-3.8-flash"
                norm_m = pricing.normalize(str(m))
                u.models.add(norm_m)
                mu = u.bucket(norm_m)
                mu.input_tokens += inp
                mu.output_tokens += out
                mu.cache_read_input_tokens += cached

                if t == "message" and obj.get("role") == "assistant":
                    u.assistant_msgs += 1

                # Timeline record
                u.turn_stats.append(TurnStat(
                    turn=turn_idx,
                    ts=ts,
                    input_tokens=inp,
                    cache_read=cached,
                    cache_write=0,
                    output_tokens=out,
                    model=norm_m,
                ))

        # Tool calls
        if t == "function_call":
            raw_name = obj.get("name") or "tool"
            canonical = _WORKBUDDY_TOOL_MAP.get(raw_name.lower(), raw_name)
            u.tool_calls[raw_name] = u.tool_calls.get(raw_name, 0) + 1

            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = None

            path_hint = ""
            if isinstance(args, dict):
                path_hint = str(args.get("file_path") or args.get("path") or "")
            u.tool_ops.append(ToolOp(turn_idx, canonical, path_hint))

        elif t == "function_call_result":
            raw_name = obj.get("name") or "tool"
            status = obj.get("status")
            out = obj.get("output")
            out_txt = ""
            if isinstance(out, dict):
                out_txt = out.get("text") or ""
            elif isinstance(out, str):
                out_txt = out
            elif isinstance(out, list):
                out_txt = "\n".join(c.get("text", "") for c in out if isinstance(c, dict))

            lowered = out_txt.strip().lower()
            is_err = (
                status != "completed"
                or lowered.startswith("error:")
                or lowered.startswith("error during")
                or lowered.startswith("failed:")
                or "traceback (most recent call last):" in lowered
            )
            if is_err:
                u.tool_errors += 1
                u.tool_errors_by_tool[raw_name] = u.tool_errors_by_tool.get(raw_name, 0) + 1

        elif t == "reasoning":
            raw_c = obj.get("rawContent") or []
            rtxt = ""
            if isinstance(raw_c, list):
                rtxt = "\n".join(c.get("text", "") for c in raw_c if isinstance(c, dict) and c.get("text"))
            if not rtxt and pd:
                rtxt = str(pd.get("reasoning", "") or "")
            if rtxt.strip():
                u.thinking_count += 1

        elif t == "message":
            role = obj.get("role")
            content = obj.get("content") or []
            txt = ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "".join(c.get("text", "") for c in content if isinstance(c, dict))

            if role == "user":
                clean = clean_user_text(txt)
                if clean:
                    turn_idx += 1
                    u.user_msgs += 1
                    u.user_message_texts.append(clean[:500])
                    if not first_user_text:
                        first_user_text = clean

    if u.started_at and u.ended_at and u.ended_at >= u.started_at:
        u.session_duration_ms = u.ended_at - u.started_at

    return u


def _loc_scan(path: Path) -> tuple[loc.SessionLoc, bool]:
    acc = loc._LocAccumulator()
    has_signal = False
    turn_idx = 0

    for obj in iter_records(path):
        t = obj.get("type")
        if t == "message" and obj.get("role") == "user":
            turn_idx += 1
        elif t == "function_call":
            name = obj.get("name")
            if name in ("Edit", "Write"):
                args = obj.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = None
                if isinstance(args, dict):
                    fp = args.get("file_path") or args.get("path")
                    if fp and loc._is_code(fp):
                        has_signal = True
                        acc.on_tool_use(name, args, turn=turn_idx)

    return acc.finish(), has_signal


def session_loc_full(path: Path) -> loc.SessionLoc:
    """Return LOC from parseable WorkBuddy Edit/Write calls."""
    return _loc_scan(path)[0]


def has_loc_signal(path: Path) -> bool:
    """True if the session contains a parseable Edit/Write call."""
    return _loc_scan(path)[1]


def read_user_messages(path: Path) -> list[str]:
    """Extract cleaned WorkBuddy user-message texts."""
    msgs: list[str] = []
    for obj in iter_records(path):
        if obj.get("type") == "message" and obj.get("role") == "user":
            content = obj.get("content") or []
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            else:
                txt = ""
            clean = clean_user_text(txt)
            if clean:
                msgs.append(clean[:500])
    return msgs


def read_conversation(path: Path) -> list[dict]:
    """Extract the full ordered conversation from a WorkBuddy session JSONL."""
    convo: list[dict] = []
    for obj in iter_records(path):
        t = obj.get("type")
        ts = obj.get("timestamp")
        if isinstance(ts, (int, float)):
            ts = int(ts)

        if t == "message":
            role = obj.get("role")
            content = obj.get("content") or []
            txt = ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if role == "user":
                clean = clean_user_text(txt)
                if clean:
                    convo.append({"role": "user", "type": "text", "text": clean, "ts": ts})
            elif role == "assistant":
                clean = txt.strip()
                if clean:
                    convo.append({"role": "assistant", "type": "text", "text": clean, "ts": ts})

        elif t == "reasoning":
            raw_c = obj.get("rawContent") or []
            rtxt = ""
            if isinstance(raw_c, list):
                rtxt = "\n".join(c.get("text", "") for c in raw_c if isinstance(c, dict) and c.get("text"))
            if not rtxt:
                pd = obj.get("providerData")
                if isinstance(pd, dict):
                    rtxt = str(pd.get("reasoning", "") or "")
            rtxt = rtxt.strip()
            if rtxt:
                convo.append({"role": "assistant", "type": "thinking", "text": rtxt, "ts": ts})

        elif t == "function_call":
            name = obj.get("name") or "tool"
            args = obj.get("arguments")
            call_id = obj.get("callId") or obj.get("id")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            convo.append({
                "role": "assistant",
                "type": "tool_use",
                "name": name,
                "input": args,
                "id": call_id,
                "ts": ts,
            })

        elif t == "function_call_result":
            name = obj.get("name") or "tool"
            call_id = obj.get("callId") or obj.get("id")
            status = obj.get("status")
            out = obj.get("output")
            out_txt = ""
            if isinstance(out, dict):
                out_txt = out.get("text") or ""
            elif isinstance(out, str):
                out_txt = out
            elif isinstance(out, list):
                out_txt = "\n".join(c.get("text", "") for c in out if isinstance(c, dict))

            lowered = out_txt.strip().lower()
            is_err = (
                status != "completed"
                or lowered.startswith("error:")
                or lowered.startswith("error during")
                or lowered.startswith("failed:")
                or "traceback (most recent call last):" in lowered
            )
            convo.append({
                "role": "tool",
                "type": "tool_result",
                "name": name,
                "tool_use_id": call_id,
                "is_error": is_err,
                "text": out_txt,
                "ts": ts,
            })

    return convo


def parse_session_file(
    path: Path,
    *,
    user: str | None = None,
    project: str | None = None,
) -> dict:
    """Parse a single WorkBuddy session file into a full report dictionary."""
    from tcer.core import export, metrics

    p = Path(path)
    meta = read_session_meta(p)
    u = aggregate_usage(p)
    sloc, _ = _loc_scan(p)
    convo = read_conversation(p)

    _, disp, _ = classify_workbuddy_project(p)
    proj_name = project or disp

    net_loc = sloc.added - sloc.deleted
    rep = metrics.compute(
        meta,
        u,
        net_loc,
        task_type="code_creation" if net_loc > 0 else "chat",
        code_added=sloc.added,
        code_deleted=sloc.deleted,
        code_reworked=sloc.rework_deleted,
        high_churn_files=sloc.high_churn_files,
    )

    row = export.report_row_dict(rep)
    row["project"] = proj_name
    if user:
        row["user"] = user
        row["person"] = user
    row["source"] = "workbuddy"
    row["conversation"] = convo
    row["user_messages"] = u.user_message_texts
    return row


def _is_workbuddy_session_file(p: Path) -> bool:
    """Validate that p is a real WorkBuddy conversation session file, not a log."""
    low = p.as_posix().lower()
    if any(k in low for k in ("/logs/", "/audit/", "/spool/", "/sandbox/", "/traces/")):
        return False
    if any(k in p.name.lower() for k in ("audit", "log", "history", "spool", "trace")):
        return False
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(5):
                line = f.readline().strip()
                if not line:
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    return False
                t = obj.get("type")
                r = obj.get("role")
                if (
                    t in ("session-meta", "ai-title", "message", "reasoning", "function_call", "file-history-snapshot")
                    or r in ("user", "assistant")
                ):
                    return True
                return False
    except Exception:
        return False
    return False


def discover_sessions(base_dir: Path | None = None) -> list[Path]:
    """Recursively collect WorkBuddy session JSONL files across all candidate roots."""
    roots: list[Path] = []
    if base_dir:
        roots.append(Path(base_dir))
    else:
        for r in workbuddy_roots():
            proj_sub = r / "projects"
            if proj_sub.is_dir():
                roots.append(proj_sub)
            sess_sub = r / "sessions"
            if sess_sub.is_dir():
                roots.append(sess_sub)
            if os.environ.get("WORKBUDDY_DIR") or os.environ.get("CODEBUDDY_DIR"):
                roots.append(r)

    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*.jsonl"):
                if not _is_workbuddy_session_file(p):
                    continue
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    out.append(p)
        except OSError:
            continue

    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def list_project_refs(base_dir: Path | None = None) -> list[ProjectRef]:
    """Group WorkBuddy sessions into project refs by directory-level aggregation.

    Ordinary workbuddy sessions are merged into a single 'WorkBuddy' entry.
    Other sessions are merged by directory and reduced to the directory name.
    """
    sessions = discover_sessions(base_dir)
    groups: dict[tuple[str, str], list[Path]] = {}
    cwd_by_group: dict[tuple[str, str], str | None] = {}
    known_cwds = _collect_known_cwds()

    for p in sessions:
        key, disp, real_cwd = classify_workbuddy_project(p, known_cwds)
        groups.setdefault((key, disp), []).append(p)
        if real_cwd and (key, disp) not in cwd_by_group:
            cwd_by_group[(key, disp)] = real_cwd

    refs: list[ProjectRef] = []
    for (key, disp), paths in groups.items():
        cwd = cwd_by_group.get((key, disp))
        refs.append(ProjectRef(
            source="workbuddy",
            key=key,
            display_name=disp,
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(paths, key=lambda f: f.stat().st_mtime)),
        ))

    # Keep WorkBuddy general entry first if present, then alphabetical
    def _sort_key(r: ProjectRef):
        return (0 if r.display_name.lower() == "workbuddy" else 1, r.display_name.lower())

    return sorted(refs, key=_sort_key)


def resolve_project(project: str | ProjectRef) -> ProjectRef | None:
    """Resolve a project key, display name, or cwd substring to a WorkBuddy project ref."""
    if isinstance(project, ProjectRef):
        return project
    refs = list_project_refs()
    for ref in refs:
        if ref.key == project or ref.display_name.lower() == project.lower():
            return ref
    needle = project.lower()
    matches = [
        r for r in refs
        if needle in r.key.lower()
        or needle in r.display_name.lower()
        or (r.cwd and needle in r.cwd.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def sessions_for_project(project: str | ProjectRef) -> list[Path]:
    """Return the session files for a WorkBuddy project."""
    if isinstance(project, ProjectRef):
        return list(project.session_paths)
    ref = resolve_project(project)
    return list(ref.session_paths) if ref else []
