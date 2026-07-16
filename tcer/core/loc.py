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

Caveat (F1): a ``Write`` that overwrites a file from an *earlier* session used to
assume prior length 0 (full content counted as added). With ``disk_prior=True``
(default), the first Write to a path seeds the prior line count from disk when
the target is readable **and the on-disk text differs from this Write payload**
(true overwrite of something else). If disk text **equals** the Write content —
the usual case when replaying a finished session whose files still exist —
prior stays 0 so net LOC is not zeroed out (post-session disk is not a baseline).
``unseen_writes`` still counts Writes where the prior could not be resolved.
Within a session, overwrites are tracked exactly via in-memory state.

``tree_loc`` measures accumulated codebase size by scanning the working directory.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tcer.core import reader

# File suffixes counted as "code". Deliberately includes docs (.md) and config
# (.json/.yaml/.toml) since notes and project files are real output for this repo.
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".bash", ".sql", ".vue", ".svelte", ".md", ".json", ".yaml",
    ".yml", ".toml", ".html", ".css",
}

# Directories skipped when scanning a working tree for accumulated LOC.
# Build outputs, tooling caches, and vendored dependency trees — never
# hand-written source, and the big ones (Rust `target/`, `node_modules/`,
# CocoaPods `Pods/`, Xcode `DerivedData/`) can hold hundreds of thousands of
# generated files that would stall tree_loc for minutes on large repos.
EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "venv", ".venv", "env", ".tox", "site-packages",
    "target", ".gradle", "build", "dist", "DerivedData", "Pods",
    ".dart_tool", ".next", ".nuxt", ".svelte-kit", ".turbo", ".angular",
    ".parcel-cache", ".webpack", "coverage", ".cache", ".caches",
    ".idea", ".vscode",
}

# Tool names that mutate files (so their token cost should produce LOC).
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Path patterns for test files (match any of these regexes)
_TEST_PATTERNS = [
    r'/tests?/',           # /test/ or /tests/
    r'_tests?\.py$',       # foo_test.py
    r'\.test\.(ts|js|tsx|jsx)$',  # foo.test.ts
    r'/spec/',             # RSpec style
]

# Path patterns for documentation files
_DOC_PATTERNS = [
    r'\.md$',
    r'/docs?/',
    r'README',
]


def _is_test_file(file_path: str) -> bool:
    """Check if file path matches test file patterns."""
    normalized = file_path.replace('\\', '/')
    return any(re.search(pat, normalized, re.IGNORECASE) for pat in _TEST_PATTERNS)


def _is_doc_file(file_path: str) -> bool:
    """Check if file path matches documentation file patterns."""
    normalized = file_path.replace('\\', '/')
    return any(re.search(pat, normalized, re.IGNORECASE) for pat in _DOC_PATTERNS)


def _nlines(s) -> int:
    """Line count of a string (0 for empty / non-string)."""
    return len(s.splitlines()) if isinstance(s, str) else 0


def _is_code(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in CODE_SUFFIXES


@dataclass
class SessionLoc:
    """LOC breakdown for one session, plus an F1 exposure counter.

    ``unseen_writes`` counts ``Write`` calls whose target file the session hadn't
    touched yet — i.e. where the prior size was *assumed to be 0*. For a Write to a
    genuinely new file that assumption is correct; for a Write that overwrites an
    *existing* file it is wrong: the whole new content is counted as added and the
    deletion is missed (the F1 bug — see the module docstring). This count is an
    upper bound on F1 exposure, not the inflation itself; quantify the real gap
    with the GUI's「校准 LOC」feature (git ground truth) when exactness matters.
    """

    added: int
    deleted: int
    unseen_writes: int = 0
    rework_deleted: int = 0  # deleted lines that this session had itself written
                             # earlier (true self-rework); deletions of pre-existing
                             # code are excluded. Feeds the churn (返工率) metric.
    # --- file-level quality metrics ---
    high_churn_files: int = 0  # files edited ≥3 times
    test_added: int = 0
    test_deleted: int = 0
    doc_added: int = 0
    doc_deleted: int = 0
    file_edit_counts: dict[str, int] = field(default_factory=dict)  # internal: path → edit count

    def recompute_high_churn(self, threshold: int = 3) -> None:
        """Set ``high_churn_files`` from ``file_edit_counts`` (unique paths)."""
        self.high_churn_files = high_churn_from_counts(self.file_edit_counts, threshold)


def high_churn_from_counts(counts: dict[str, int], threshold: int = 3) -> int:
    """Number of distinct paths edited at least ``threshold`` times."""
    return sum(1 for c in counts.values() if c >= threshold)


def merge_session_locs(slocs: list[SessionLoc]) -> SessionLoc:
    """Sum LOC counters and merge per-path edit counts (recompute high_churn).

    Used when folding subagent files into a parent session or building a project
    aggregate. ``high_churn_files`` is derived from the *merged* edit counts so
    the same path edited in main + subagent is one file, not two.
    """
    if not slocs:
        return SessionLoc(added=0, deleted=0)
    if len(slocs) == 1:
        s = slocs[0]
        # Defensive: ensure high_churn matches counts even if caller left it stale.
        out = SessionLoc(
            added=s.added,
            deleted=s.deleted,
            unseen_writes=s.unseen_writes,
            rework_deleted=s.rework_deleted,
            high_churn_files=s.high_churn_files,
            test_added=s.test_added,
            test_deleted=s.test_deleted,
            doc_added=s.doc_added,
            doc_deleted=s.doc_deleted,
            file_edit_counts=dict(s.file_edit_counts),
        )
        out.recompute_high_churn()
        return out

    merged_counts: dict[str, int] = {}
    added = deleted = unseen = rework = 0
    test_a = test_d = doc_a = doc_d = 0
    for s in slocs:
        added += s.added
        deleted += s.deleted
        unseen += s.unseen_writes
        rework += s.rework_deleted
        test_a += s.test_added
        test_d += s.test_deleted
        doc_a += s.doc_added
        doc_d += s.doc_deleted
        for fp, cnt in s.file_edit_counts.items():
            merged_counts[fp] = merged_counts.get(fp, 0) + cnt
    out = SessionLoc(
        added=added,
        deleted=deleted,
        unseen_writes=unseen,
        rework_deleted=rework,
        test_added=test_a,
        test_deleted=test_d,
        doc_added=doc_a,
        doc_deleted=doc_d,
        file_edit_counts=merged_counts,
    )
    out.recompute_high_churn()
    return out


def _resolve_path(file_path: str, cwd: str | Path | None) -> Path | None:
    if not file_path or not isinstance(file_path, str):
        return None
    p = Path(file_path)
    if not p.is_absolute():
        if cwd is None:
            return None
        p = Path(cwd) / p
    return p


def disk_line_count(file_path: str, cwd: str | Path | None = None) -> tuple[int, bool]:
    """Best-effort on-disk line count for F1 Write baseline correction.

    Returns ``(lines, resolved)``:
    - ``resolved=True`` and ``lines=N`` — file exists and was readable;
    - ``resolved=True`` and ``lines=0`` — path resolved but file missing (new file);
    - ``resolved=False`` and ``lines=0`` — could not resolve/read (relative path
      without cwd, permission error, …). Callers should treat this as the classic
      F1 exposure (assume old=0 and count ``unseen_writes``).

    Uses current disk state (not historical). Offline, stdlib only.
    """
    text, resolved = disk_file_text(file_path, cwd)
    if not resolved:
        return 0, False
    if text is None:
        return 0, True  # missing file
    return _nlines(text), True


def disk_file_text(
    file_path: str, cwd: str | Path | None = None,
) -> tuple[str | None, bool]:
    """Read on-disk text for *file_path*.

    Returns ``(text, resolved)``:
    - ``(text, True)`` — file exists and was readable;
    - ``(None, True)`` — path resolved but file missing (new file);
    - ``(None, False)`` — could not resolve/read.
    """
    p = _resolve_path(file_path, cwd)
    if p is None:
        return None, False
    try:
        if not p.is_file():
            return None, True
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), True
    except OSError:
        return None, False


def _text_equiv(a: str, b: str) -> bool:
    """Compare tool payload vs disk, ignoring newline style only."""
    if a == b:
        return True
    return a.replace("\r\n", "\n").replace("\r", "\n") == b.replace("\r\n", "\n").replace("\r", "\n")


class _LocAccumulator:
    """Incremental LOC state while replaying edit tool_use blocks (single-pass)."""

    __slots__ = (
        "file_lines", "session_authored", "file_edits",
        "added", "deleted", "unseen", "rework",
        "test_added", "test_deleted", "doc_added", "doc_deleted",
        "cwd", "disk_prior",
    )

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        disk_prior: bool = False,
    ) -> None:
        self.file_lines: dict[str, int] = {}  # current line estimate (incl. disk seed)
        # Lines this session has authored into the file (never includes disk prior).
        self.session_authored: dict[str, int] = {}
        self.file_edits: dict[str, int] = {}
        self.added = self.deleted = self.unseen = self.rework = 0
        self.test_added = self.test_deleted = 0
        self.doc_added = self.doc_deleted = 0
        self.cwd = cwd
        self.disk_prior = disk_prior

    def on_tool_use(self, name: str, inp: dict) -> None:
        if name not in _EDIT_TOOLS:
            return
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        if not isinstance(fp, str) or not _is_code(fp):
            return
        self.file_edits[fp] = self.file_edits.get(fp, 0) + 1
        # First Write to a path: optionally seed prior from disk (F1 mitigation).
        # If on-disk text *equals* this Write payload, the usual post-session
        # case for files the model just created — do NOT seed prior (would
        # zero net LOC). Only seed when disk differs (true overwrite of other
        # content still sitting on disk, e.g. tests that leave pre-write files).
        if name == "Write" and fp not in self.file_lines:
            if self.disk_prior:
                content = inp.get("content")
                disk_text, resolved = disk_file_text(fp, self.cwd)
                if not resolved:
                    self.unseen += 1  # assume old=0
                elif disk_text is None:
                    pass  # missing file → prior 0, full add
                elif isinstance(content, str) and _text_equiv(disk_text, content):
                    pass  # post-session match → authored from empty
                else:
                    self.file_lines[fp] = _nlines(disk_text)  # different content
            else:
                self.unseen += 1
        # Self-rework only against lines this session already wrote — never the
        # disk prior seed (deleting pre-existing code is a normal edit).
        authored_before = self.session_authored.get(fp, 0)
        a, d = _delta_for_tool(name, inp, self.file_lines, fp)
        self.added += a
        self.deleted += d
        rework_part = min(d, authored_before)
        self.rework += rework_part
        if name == "Write":
            # Whole-file rewrite: session now owns the entire new content.
            self.session_authored[fp] = self.file_lines.get(fp, 0)
        else:
            # Edit/MultiEdit/Notebook: add new lines, lose only reworked deletes.
            self.session_authored[fp] = max(0, authored_before - rework_part + a)
        if _is_test_file(fp):
            self.test_added += a
            self.test_deleted += d
        elif _is_doc_file(fp):
            self.doc_added += a
            self.doc_deleted += d

    def finish(self) -> SessionLoc:
        return SessionLoc(
            added=self.added,
            deleted=self.deleted,
            unseen_writes=self.unseen,
            rework_deleted=self.rework,
            high_churn_files=high_churn_from_counts(self.file_edits),
            test_added=self.test_added,
            test_deleted=self.test_deleted,
            doc_added=self.doc_added,
            doc_deleted=self.doc_deleted,
            file_edit_counts=self.file_edits,
        )


def session_loc_full(
    path: Path,
    *,
    cwd: str | Path | None = None,
    disk_prior: bool = False,
) -> SessionLoc:
    """Full LOC breakdown for one session (added / deleted / unseen_writes).

    Replays Write/Edit/MultiEdit/NotebookEdit in order. Net LOC = added - deleted;
    churn = deleted / added. Only paths with a code suffix are counted.

    ``disk_prior``: default **False** (generation effort: count what Write wrote).
    Set True only when on-disk files still reflect *pre-Write* baselines (e.g.
    lab fixtures). Post-session analysis with disk_prior=True and intermediate
    Writes often fabricates large deletes. Relative paths need ``cwd``.
    """
    acc = _LocAccumulator(cwd=cwd, disk_prior=disk_prior)
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
            if not isinstance(name, str):
                continue
            inp = item.get("input") or {}
            if not isinstance(inp, dict):
                inp = {}
            acc.on_tool_use(name, inp)
    return acc.finish()


def session_loc(path: Path) -> tuple[int, int]:
    """``(added, deleted)`` — backward-compatible tuple view of ``session_loc_full``."""
    r = session_loc_full(path)
    return r.added, r.deleted


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
                # Text mode with universal newlines so the count matches
                # ``_nlines`` (splitlines): \r\n and lone \r are normalized to \n
                # before splitting, the same as session_loc's line accounting.
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    total += sum(1 for _ in fh)
            except OSError:
                continue
    return total
