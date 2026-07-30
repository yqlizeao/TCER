"""Cross-platform location of the Claude Code config directory and project hashes.

Ports cc-switch's `config::get_claude_config_dir()` plus the project-hash encoding
rule documented in CLAUDE.md (replace ``\\``, ``/``, ``.``, ``:`` with ``-``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from tcer.core.models import ProjectRef

# Characters Claude Code replaces with '-' when hashing a cwd into a folder name.
_HASH_REPLACE = ("\\", "/", ".", ":")


def project_hash_key(name: str) -> str:
    """Identity key for collapsing Claude project-hash folder names.

    On Windows the drive letter in a cwd hash may appear as ``C--…`` or ``c--…``
    depending on how the path was capitalized when Claude started — two folders
    for the same project. We casefold the hash name on win32 so the GUI / list
    shows one entry; :func:`tcer.core.reader.discover_jsonl` still unions JSONL
    from every matching casing.
    """
    return name.lower() if sys.platform == "win32" else name


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


def is_custom_claude_root(project_path: Path) -> bool:
    """True if *project_path* lives under a non-canonical Claude config root.

    Used by the GUI to flag Claude projects that come from a sibling config dir
    (e.g. ccswitch's ``~/.claude-proxy``) rather than the canonical ``~/.claude``
    (or ``$CLAUDE_CONFIG_DIR``). *project_path* is a project-hash directory of
    the form ``<root>/projects/<hash>``; its grandparent is the config root.
    Compared by directory *name* so a ``$CLAUDE_CONFIG_DIR`` override is
    respected and Windows path casing can't cause false positives.
    """
    try:
        return project_path.parent.parent.name != _claude_dir().name
    except (AttributeError, OSError):
        return False


def ref_root(ref) -> Path | None:
    """Claude 项目 ref 所属的 config root（仅对 ``source=='claude'`` 有意义）。

    优先 ``ref.config_root``；未填（旧 ref / 测试构造）时回退从 ``ref.path``
    推（``<root>/projects/<hash>`` 的祖父目录）。返回 None 表示无法定位根。
    """
    root = getattr(ref, "config_root", None)
    if root is not None:
        return root
    path = getattr(ref, "path", None)
    if path is None:
        return None
    try:
        return path.parent.parent
    except (AttributeError, OSError):
        return None


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


def omp_dir() -> Path:
    """Return the Oh My Pi (omp) config root (``~/.omp`` by default).

    omp's ``dirs.ts`` resolves the config root from ``PI_CONFIG_DIR`` (default
    ``.omp``) joined to the home directory. ``OMP_HOME`` is honored only as a
    legacy fallback (it is not a real omp variable).
    """
    cfg = os.environ.get("PI_CONFIG_DIR")
    if cfg:
        return Path.home() / cfg
    override = os.environ.get("OMP_HOME")
    if override:
        return Path(override)
    return Path.home() / ".omp"


def omp_agent_dir() -> Path:
    """Return the omp *agent* base directory (``~/.omp/agent`` by default).

    ``PI_CODING_AGENT_DIR`` relocates the whole agent base (sessions, blobs,
    ``agent.db``); otherwise it is ``<config-root>/agent``. Mirrors omp's
    ``getAgentDir``.
    """
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return Path(override)
    return omp_dir() / "agent"


def omp_sessions_dir() -> Path:
    """Return the root directory containing omp session JSONL files.

    omp stores sessions under ``<agent-dir>/sessions/<encoded-cwd>/*.jsonl``
    (``~/.omp/agent/sessions`` by default; honors ``PI_CODING_AGENT_DIR`` and
    ``PI_CONFIG_DIR``). Note: Linux XDG redirection (``$XDG_DATA_HOME/omp``) is
    not replicated - run ``omp config migrate`` first or keep the default root.
    """
    return omp_agent_dir() / "sessions"


def encode_hash(cwd: str | Path) -> str:
    """Encode a working-directory path into its project-hash folder name.

    Example: ``c:\\GitHub\\TCER`` -> ``c--GitHub-TCER``.
    """
    s = str(cwd)
    for ch in _HASH_REPLACE:
        s = s.replace(ch, "-")
    return s


def list_projects() -> list[Path]:
    """Return every project-hash directory across all Claude config roots, sorted.

    Each config root is enumerated independently (no cross-root dedup) so a
    project hash present in both ``~/.claude`` and ``~/.claude-proxy`` yields two
    entries — one per root — each scoped to its own sessions via
    :func:`tcer.core.reader.discover_jsonl` (with ``roots=``). Within a single
    root, Windows hash names that differ only by case (``C--GitHub-X`` vs
    ``c--GitHub-X``) still collapse to one entry (the folder with more
    ``*.jsonl`` sessions, tie → lexicographically first name).

    Sorted by ``(config-root name, hash name)`` so the canonical ``.claude`` root
    lists first — this gives :func:`resolve_project` a stable default when a bare
    hash matches across roots.
    """
    out: list[Path] = []
    for root in claude_config_dirs():
        base = root / "projects"
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        # 根内 casefold 折叠（本根的 C--X / c--X 合并为一）；字典声明在根循环内，
        # 使兄弟根不再互相合并——跨根同 hash 现在各成一条。
        best: dict[str, Path] = {}
        best_n: dict[str, int] = {}
        for d in children:
            if not d.is_dir():
                continue
            key = project_hash_key(d.name)
            try:
                n = sum(1 for _ in d.rglob("*.jsonl"))
            except OSError:
                n = 0
            prev = best.get(key)
            if prev is None or n > best_n[key] or (
                n == best_n[key] and d.name.lower() < prev.name.lower()
            ):
                best[key] = d
                best_n[key] = n
        out.extend(best.values())
    return sorted(out, key=lambda p: (p.parent.parent.name.lower(), p.name.lower()))


def project_has_sessions(ref: ProjectRef) -> bool:
    """True if *ref* currently has at least one session the analyzer can open.

    Used by the GUI to grey out empty projects and by ``tcer.audit`` to skip
    hollow entries in ``--all-projects`` runs. Lazy-imports readers to avoid
    import cycles with ``paths``.
    """
    if ref.source == "claude":
        from tcer.core import reader
        root = ref_root(ref)
        return bool(reader.discover_jsonl(ref.key, roots=[root] if root is not None else None))
    if ref.source in ("codex", "grok", "omp"):
        return bool(ref.session_paths)
    if ref.source == "opencode":
        from tcer.core import opencode_reader
        try:
            return bool(opencode_reader.sessions_for_project(ref))
        except Exception:  # noqa: BLE001 — treat unreadable as empty
            return False
    return False


def since_date_to_ms(date_str: str | None) -> int | None:
    """``YYYY-MM-DD`` → epoch ms at 00:00 **UTC**, or None if empty/unparseable.

    Intentionally mirrors ``analyze._parse_date_to_ms`` (UTC) so the project-level
    mtime filter and the session-level started_at filter agree on the same
    threshold for one ``since`` string. Local tz would desync the left column
    from the right panel. (paths cannot import analyze — analyze imports paths.)
    """
    if not date_str:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _max_mtime_ms(paths) -> int | None:
    """Max file mtime across *paths* as epoch ms, or None if none stat-able."""
    latest = None
    for p in paths:
        try:
            m = p.stat().st_mtime_ns // 1_000_000
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return latest


def project_latest_activity_ms(ref: ProjectRef) -> int | None:
    """Epoch ms of *ref*'s most-recent session activity (approx ≈ last write).

    Claude/codex/grok/omp: max session-file mtime. OpenCode: max(session.time_created)
    from SQLite (== authoritative started_at). Returns None when there are no
    scannable files / the project is empty / all stats failed. Lazy-imports
    readers like ``project_has_sessions`` to avoid import cycles with ``paths``.
    """
    if ref.source == "claude":
        from tcer.core import reader
        root = ref_root(ref)
        files = reader.discover_jsonl(ref.key, roots=[root] if root is not None else None)
        return _max_mtime_ms(files)
    if ref.source in ("codex", "grok", "omp"):
        return _max_mtime_ms(ref.session_paths)
    if ref.source == "opencode":
        from tcer.core import opencode_reader
        try:
            return opencode_reader.latest_activity_ms(ref)
        except Exception:  # noqa: BLE001 — unreadable DB → treat as no activity
            return None
    return None


def list_project_refs(source: str = "all") -> list[ProjectRef]:
    """Return source-aware project refs for the GUI.
    ``source`` is one of ``"all"``, ``"claude"``, ``"codex"``, ``"opencode"``,
    ``"grok"``, or ``"omp"``. Claude refs wrap real project directories;
    Codex/OpenCode/Grok/omp refs are grouped by session cwd/project directory.
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
                config_root=d.parent.parent,
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
    if source in ("all", "omp"):
        from tcer.core import omp_reader

        refs.extend(omp_reader.list_project_refs())
    return sorted(refs, key=lambda r: (r.source, r.display_name.lower()))


def resolve_project(project: str) -> Path | None:
    """Resolve a user-supplied project name/hash to a project directory.

    Matches in priority order: exact folder name, casefold-equal hash name
    (Windows drive-letter variants), then case-insensitive substring
    (so ``--project TCER`` resolves ``c--GitHub-TCER``). Returns None if no match.
    """
    dirs = list_projects()
    for d in dirs:
        if d.name == project:
            return d
    want = project_hash_key(project)
    for d in dirs:
        if project_hash_key(d.name) == want:
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
