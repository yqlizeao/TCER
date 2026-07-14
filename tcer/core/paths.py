"""Cross-platform location of the Claude Code config directory and project hashes.

Ports cc-switch's `config::get_claude_config_dir()` plus the project-hash encoding
rule documented in CLAUDE.md (replace ``\\``, ``/``, ``.``, ``:`` with ``-``).
"""
from __future__ import annotations

import os
from pathlib import Path

from tcer.core.models import ProjectRef

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
    """Return ``<claude_dir>/projects`` where per-project session JSONL lives.

    Note: this is the *canonical* root only. Use :func:`claude_config_dirs` to
    enumerate every Claude config root when sessions may live under a custom
    ``CLAUDE_CONFIG_DIR`` profile (e.g. ``~/.zclaude``).
    """
    return _claude_dir() / "projects"


def _looks_like_claude_config(d: Path) -> bool:
    """Heuristic: a directory with ``projects/<hash>/*.jsonl`` looks like a Claude config root."""
    projs = d / "projects"
    if not projs.is_dir():
        return False
    try:
        return any(projs.glob("*/*.jsonl"))
    except OSError:
        return False


# Process-lifetime cache keyed by (home, CLAUDE_CONFIG_DIR) so the parent-dir scan
# runs once per distinct config relocation. A custom profile created mid-session
# only appears after a restart (or :func:`reset_claude_roots_cache`).
_CLAUDE_ROOTS_CACHE: dict[tuple[str, str], list[Path]] = {}


def claude_config_dirs() -> list[Path]:
    """All Claude config roots visible to TCER: the canonical dir plus matching siblings.

    Claude Code is often launched with ``CLAUDE_CONFIG_DIR=%USERPROFILE%\\.zclaude``
    (or another custom name) to keep ``.claude`` clean. That env var lives in
    Claude's process, not TCER's, so TCER cannot read it directly. Instead we scan
    the canonical dir's *parent* (typically the home dir) for other directories
    whose structure matches Claude's (``projects/<hash>/*.jsonl``) and treat each
    as an additional root. Sessions for the same project hash across roots are
    merged by :func:`tcer.core.reader.discover_jsonl`; a project unique to a custom
    root simply becomes visible.
    """
    key = (str(Path.home()), os.environ.get("CLAUDE_CONFIG_DIR", ""))
    cached = _CLAUDE_ROOTS_CACHE.get(key)
    if cached is not None:
        return cached

    canonical = _claude_dir()
    parent = canonical.parent
    candidates: list[Path] = []
    try:
        candidates = [c for c in parent.iterdir() if c.is_dir()]
    except OSError:
        candidates = []
    # Always consider the canonical dir even if the parent listing missed it.
    if canonical not in candidates:
        candidates.append(canonical)

    roots: list[Path] = []
    seen: set[Path] = set()
    for cand in candidates:
        try:
            if not cand.is_dir():
                continue
            rp = cand.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        if _looks_like_claude_config(cand):
            seen.add(rp)
            roots.append(cand)
    roots.sort(key=lambda p: str(p).lower())
    _CLAUDE_ROOTS_CACHE[key] = roots
    return roots


def reset_claude_roots_cache() -> None:
    """Clear the cached Claude config-root scan (used by tests)."""
    _CLAUDE_ROOTS_CACHE.clear()


def codex_dir() -> Path:
    """Return the Codex config directory (``~/.codex`` by default).

    Honors ``CODEX_HOME`` when set, matching Codex's local-state convention.
    """
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def codex_sessions_dir() -> Path:
    """Return the root directory containing Codex session JSONL files."""
    return codex_dir() / "sessions"


def opencode_dir() -> Path:
    """Return the OpenCode data directory.

    OpenCode documents ``~/.local/share/opencode`` (also on Windows under the
    user profile). ``OPENCODE_DATA_DIR`` is accepted as a test/user override.
    """
    override = os.environ.get("OPENCODE_DATA_DIR") or os.environ.get("OPENCODE_DATA_HOME")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def grok_dir() -> Path:
    """Return the grok build CLI config directory (``~/.grok`` by default).

    Honors ``GROK_HOME`` when set, matching grok build's data-root convention.
    """
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def grok_sessions_dir() -> Path:
    """Return the root directory containing Grok session directories."""
    return grok_dir() / "sessions"


def encode_hash(cwd: str | Path) -> str:
    """Encode a working-directory path into its project-hash folder name.

    Example: ``c:\\GitHub\\TCER`` -> ``c--GitHub-TCER``.
    """
    s = str(cwd)
    for ch in _HASH_REPLACE:
        s = s.replace(ch, "-")
    return s


def list_projects() -> list[Path]:
    """Return all project-hash directories across every Claude config root, sorted.

    When multiple config roots (e.g. ``~/.claude`` and ``~/.zclaude``) hold the
    same project hash, the alphabetically-first root's directory represents it;
    :func:`tcer.core.reader.discover_jsonl` still unions session files from every
    root so no sessions are lost.
    """
    dirs: list[Path] = []
    seen_names: set[str] = set()
    for root in claude_config_dirs():
        base = root / "projects"
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for d in children:
            if d.is_dir() and d.name not in seen_names:
                seen_names.add(d.name)
                dirs.append(d)
    dirs.sort(key=lambda p: p.name.lower())
    return dirs


def list_project_refs(source: str = "all") -> list[ProjectRef]:
    """Return source-aware project refs for the GUI.

    ``source`` is one of ``"all"``, ``"claude"``, ``"codex"``, ``"opencode"``,
    or ``"grok"``. Claude refs wrap real project directories; Codex/OpenCode/
    Grok refs are grouped by session cwd/project directory.
    """
    refs: list[ProjectRef] = []
    if source in ("all", "claude"):
        refs.extend(
            ProjectRef(
                source="claude",
                key=d.name,
                display_name=d.name,
                cwd=None,
                path=d,
            )
            for d in list_projects()
        )
    if source in ("all", "codex"):
        from tcer.core import codex_reader

        refs.extend(codex_reader.list_project_refs())
    if source in ("all", "opencode"):
        from tcer.core import opencode_reader

        refs.extend(opencode_reader.list_project_refs())
    if source in ("all", "grok"):
        from tcer.core import grok_reader

        refs.extend(grok_reader.list_project_refs())
    return sorted(refs, key=lambda r: (r.source, r.display_name.lower()))


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
