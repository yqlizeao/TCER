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

Caveat (F1): a ``Write`` that overwrites a file from an *earlier* session first
assumes prior length 0 (full content counted as added, ``unseen_writes``++). When
the tool-result line carries ``toolUseResult.originalFile`` (Claude), the prior is
corrected retroactively from **session data** via ``note_write_original`` — no
disk access. Within a session, overwrites are tracked exactly via in-memory state.

产品边界：本模块只消费会话数据，绝不读取用户的真实仓库/工作目录
（磁盘先验与 tree_loc 全树扫描已按产品定位移除）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tcer.core import reader

# --- 产出文件分类：程序员代码 / 策划文本 / 开发配置 ---------------------------
# 闸门 _is_code 取三类并集（都算有效产出，计入 net_loc / TCER）；_is_doc_file 再把
# 「策划文本」单独挑出来记入「文档行」。Office 二进制（.docx/.xlsx/.pptx）刻意
# 不在内：AI 的 Write/Edit/apply_patch 是文本行模型，无法对二进制产生行增量——
# 策划通常在 .md/.txt 起草，再自行转成 Word/Excel，那份文本产出已被计入。

# 程序员：纯代码源文件
CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".sh", ".bash", ".sql", ".vue", ".svelte", ".html", ".css",
}
# 策划/文档：可文本编辑的文档与表格数据（产生行增量，计入产出）
TEXT_SUFFIXES = {
    ".md", ".txt", ".rtf", ".rst", ".org", ".adoc", ".tex", ".csv",
}
# 开发配置（已计入产出）
CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml"}

# 产出总集（_is_code 闸门）：代码 ∪ 文本 ∪ 配置
_PRODUCTIVE_SUFFIXES = CODE_SUFFIXES | TEXT_SUFFIXES | CONFIG_SUFFIXES

# Tool names that mutate files (so their token cost should produce LOC).
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Path patterns for test files (match any of these regexes)
_TEST_PATTERNS = [
    r'/tests?/',           # /test/ or /tests/
    r'_tests?\.py$',       # foo_test.py
    r'\.test\.(ts|js|tsx|jsx)$',  # foo.test.ts
    r'/spec/',             # RSpec style
]

# Path patterns for documentation / planner-text files (feeds 「文档行」).
# 散文类文档——.csv 计入产出（在闸门里）但属数据、不算文档。
_DOC_PATTERNS = [
    r'\.(md|txt|rtf|rst|org|adoc|tex)$',
    r'/docs?/',
    r'README',
]


def _is_test_file(file_path: str) -> bool:
    """Check if file path matches test file patterns."""
    normalized = file_path.replace('\\', '/')
    return any(re.search(pat, normalized, re.IGNORECASE) for pat in _TEST_PATTERNS)


def _is_doc_file(file_path: str) -> bool:
    """Check if file path is a documentation / planner-text file."""
    normalized = file_path.replace('\\', '/')
    return any(re.search(pat, normalized, re.IGNORECASE) for pat in _DOC_PATTERNS)


def _nlines(s) -> int:
    """Line count of a string (0 for empty / non-string)."""
    return len(s.splitlines()) if isinstance(s, str) else 0


def _is_code(file_path: str) -> bool:
    """True for any countable text output — code, planner text, or config.

    Name kept for back-compat; the gate now covers planner text files
    (.txt/.rst/.org/…) too, not just programmer code. Binary Office formats
    (.docx/.xlsx) are never text-line-editable, so they stay out — no line
    deltas exist to count.
    """
    return Path(file_path).suffix.lower() in _PRODUCTIVE_SUFFIXES


@dataclass
class SessionLoc:
    """LOC breakdown for one session, plus an F1 exposure counter.

    ``unseen_writes`` counts ``Write`` calls whose target file the session hadn't
    touched yet — i.e. where the prior size was *assumed to be 0*. For a Write to a
    genuinely new file that assumption is correct; for a Write that overwrites an
    *existing* file it is wrong: the whole new content is counted as added and the
    deletion is missed (the F1 bug — see the module docstring). This count is an
    upper bound on the *residual* F1 exposure (Writes whose originalFile never
    arrived in the session data).
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


class _LocAccumulator:
    """Incremental LOC state while replaying edit tool_use blocks (single-pass)."""

    __slots__ = (
        "file_lines", "session_authored", "file_edits",
        "added", "deleted", "unseen", "rework",
        "test_added", "test_deleted", "doc_added", "doc_deleted",
        "pending_f1",
    )

    def __init__(self) -> None:
        self.file_lines: dict[str, int] = {}  # current line estimate (incl. disk seed)
        # Lines this session has authored into the file (never includes disk prior).
        self.session_authored: dict[str, int] = {}
        self.file_edits: dict[str, int] = {}
        self.added = self.deleted = self.unseen = self.rework = 0
        self.test_added = self.test_deleted = 0
        self.doc_added = self.doc_deleted = 0
        # F1 待修正：首个 Write 按 old=0 记账的 (path → 该 Write 的新行数)。
        # Claude 的 toolUseResult.originalFile 到达后经 note_write_original 修正。
        self.pending_f1: dict[str, int] = {}

    def on_tool_use(self, name: str, inp: dict) -> None:
        if name not in _EDIT_TOOLS:
            return
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        if not isinstance(fp, str) or not _is_code(fp):
            return
        self.file_edits[fp] = self.file_edits.get(fp, 0) + 1
        # First Write to a path: assume old=0 and remember it in pending_f1 —
        # a later toolUseResult.originalFile (session data, not disk) corrects it.
        if name == "Write" and fp not in self.file_lines:
            self.unseen += 1
            self.pending_f1[fp] = _nlines(inp.get("content"))
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

    def note_write_original(self, fp: str, original_text) -> None:
        """用 ``toolUseResult.originalFile``（Write 前的真实文件内容）修正 F1。

        首个 Write 记账时假定 old=0（added=全文、deleted=0、unseen+1）。结果行
        给出真实原文后：确认新文件 → 只撤销 unseen 计数；确认覆写 → 按会话内
        Write 语义重算 (new−orig / orig−new)，同步修正 test/doc 拆分。仅对仍在
        ``pending_f1`` 的路径生效（后续 Edit 的 originalFile 不会误触发）。
        """
        if not isinstance(fp, str) or fp not in self.pending_f1:
            return
        if not isinstance(original_text, str):
            return
        new = self.pending_f1.pop(fp)
        self.unseen -= 1  # 先验已知，不再是 F1 暴露
        orig = _nlines(original_text)
        if orig <= 0:
            return  # 确认新文件：old=0 假定本来就对
        ta, td = (new - orig, 0) if new >= orig else (0, orig - new)
        d_add = ta - new  # ≤ 0：撤销多计的 added
        self.added += d_add
        self.deleted += td
        if _is_test_file(fp):
            self.test_added += d_add
            self.test_deleted += td
        elif _is_doc_file(fp):
            self.doc_added += d_add
            self.doc_deleted += td

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


def session_loc_full(path: Path) -> SessionLoc:
    """Full LOC breakdown for one session (added / deleted / unseen_writes).

    Replays Write/Edit/MultiEdit/NotebookEdit in order (with originalFile F1
    correction). Net LOC = added - deleted; churn = deleted / added. Only paths
    with a code suffix are counted. Session data only — never touches disk.
    """
    acc = _LocAccumulator()
    for obj in reader.iter_messages(path):
        # Write 结果行的 originalFile → F1 修正（与 reader.scan_session 一致，
        # 保证审计交叉验证时两条路径逐字节相同）。
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict) and "originalFile" in tur:
            acc.note_write_original(tur.get("filePath"), tur.get("originalFile"))
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
