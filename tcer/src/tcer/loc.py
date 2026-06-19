"""Git-free code-output measurement from Claude Code session JSONL.

Net LOC and churn are derived from the assistant's own file-mutating tool calls
(``Write`` / ``Edit`` / ``MultiEdit`` / ``NotebookEdit``) recorded in the session
JSONL — not from git. This makes measurement:

- **dependency-free** — no ``git`` binary, works on any folder;
- **per-session exact** — each session's output is attributed to that session,
  with no commit-timing / time-window guesswork;
- **faithful to generation effort** — it counts what the model actually wrote and
  rewrote (iterations included), which is the real Token→Code work, rather than
  only what was eventually committed.

Caveat: a ``Write`` that overwrites a file written in an *earlier* session can't
see that file's prior length (per-session state), so it counts the full content as
added. Within a session, overwrites are tracked exactly.

``tree_loc`` measures accumulated codebase size by scanning the working directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import reader

# File suffixes counted as "code". Deliberately includes docs (.md) and config
# (.json/.yaml/.toml) since notes and project files are real output for this repo.
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".bash", ".sql", ".vue", ".svelte", ".md", ".json", ".yaml",
    ".yml", ".toml", ".html", ".css",
}

# Directories skipped when scanning a working tree for accumulated LOC.
EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "venv", ".venv", "env", "dist", "build",
    ".idea", ".vscode", ".tox", "site-packages",
}

# Tool names that mutate files (so their token cost should produce LOC).
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _nlines(s) -> int:
    """Line count of a string (0 for empty / non-string)."""
    return len(s.splitlines()) if isinstance(s, str) else 0


def _is_code(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in CODE_SUFFIXES


def session_loc(path: Path) -> tuple[int, int]:
    """Return (added, deleted) code lines from one session's file-mutating tool calls.

    Replays Write/Edit/MultiEdit/NotebookEdit in order. Net LOC = added - deleted;
    churn = deleted / added. Only paths with a code suffix are counted.
    """
    file_lines: dict[str, int] = {}  # intra-session current line count per file
    added = deleted = 0

    for obj in reader.iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name")
            if name not in _EDIT_TOOLS:
                continue
            inp = item.get("input") or {}
            fp = inp.get("file_path") or inp.get("notebook_path") or ""
            if not _is_code(fp):
                continue
            a, d = _delta_for_tool(name, inp, file_lines, fp)
            added += a
            deleted += d
    return added, deleted


def _delta_for_tool(name: str, inp: dict, file_lines: dict[str, int], fp: str) -> tuple[int, int]:
    """(added, deleted) for a single tool call, updating intra-session file state."""
    if name == "Write":
        new = _nlines(inp.get("content"))
        old = file_lines.get(fp, 0)
        file_lines[fp] = new
        return (new - old, 0) if new >= old else (0, old - new)

    if name == "Edit":
        return _apply_edit(inp.get("new_string"), inp.get("old_string"), file_lines, fp)

    if name == "MultiEdit":
        a = d = 0
        for e in inp.get("edits", []) or []:
            if isinstance(e, dict):
                ea, ed = _apply_edit(e.get("new_string"), e.get("old_string"), file_lines, fp)
                a += ea
                d += ed
        return a, d

    if name == "NotebookEdit":
        mode = inp.get("edit_mode") or "replace"
        new = _nlines(inp.get("new_source"))
        if mode == "delete":
            return 0, new
        return new, 0  # insert / replace → count new cell lines as added

    return 0, 0


def _apply_edit(new_string, old_string, file_lines: dict[str, int], fp: str) -> tuple[int, int]:
    a, d = _nlines(new_string), _nlines(old_string)
    file_lines[fp] = max(0, file_lines.get(fp, 0) + (a - d))
    return (max(0, a - d), max(0, d - a))


def net_loc(path: Path) -> int:
    """Net code LOC for one session (added - deleted)."""
    a, d = session_loc(path)
    return a - d


def tree_loc(root: Path) -> int | None:
    """Accumulated code LOC of a working directory (recursive, code suffixes only).

    Skips ``EXCLUDE_DIRS``. Returns None if ``root`` doesn't exist. Feeds NCPI
    (net / total) and PSAC (project-phase coefficient).
    """
    if not root.is_dir():
        return None
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if Path(fn).suffix.lower() not in CODE_SUFFIXES:
                continue
            fpath = Path(dirpath) / fn
            try:
                with open(fpath, "rb") as fh:
                    total += sum(1 for _ in fh)
            except OSError:
                continue
    return total
