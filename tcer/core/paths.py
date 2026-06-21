"""Cross-platform location of the Claude Code config directory and project hashes.

Ports cc-switch's `config::get_claude_config_dir()` plus the project-hash encoding
rule documented in CLAUDE.md (replace ``\\``, ``/``, ``.``, ``:`` with ``-``).
"""
from __future__ import annotations

import os
from pathlib import Path

# Characters Claude Code replaces with '-' when hashing a cwd into a folder name.
_HASH_REPLACE = ("\\", "/", ".", ":")


def _claude_dir() -> Path:
    """Return the Claude Code config directory (``~/.claude`` by default).

    Honors the ``CLAUDE_CONFIG_DIR`` env override if set.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def projects_dir() -> Path:
    """Return ``<claude_dir>/projects`` where per-project session JSONL lives."""
    return _claude_dir() / "projects"


def encode_hash(cwd: str | Path) -> str:
    """Encode a working-directory path into its project-hash folder name.

    Example: ``c:\\GitHub\\TCER`` -> ``c--GitHub-TCER``.
    """
    s = str(cwd)
    for ch in _HASH_REPLACE:
        s = s.replace(ch, "-")
    return s


def list_projects() -> list[Path]:
    """Return all project-hash directories under ``projects/``, sorted."""
    base = projects_dir()
    if not base.is_dir():
        return []
    return sorted(d for d in base.iterdir() if d.is_dir())


def resolve_project(project: str) -> Path | None:
    """Resolve a user-supplied project name/hash to a project directory.

    Matches in priority order: exact folder name, then case-insensitive substring
    (so ``--project TCER`` resolves ``c--GitHub-TCER``). Returns None if no match.
    """
    dirs = list_projects()
    for d in dirs:
        if d.name == project:
            return d
    needle = project.lower()
    matches = [d for d in dirs if needle in d.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Ambiguous — prefer an exact tail segment match (e.g. "TCER" == "...-TCER").
        tail_matches = [d for d in matches if d.name.lower().endswith("-" + needle)]
        if len(tail_matches) == 1:
            return tail_matches[0]
    return None
