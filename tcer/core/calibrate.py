"""Git-based LOC calibration for TCER sessions.

Compares the git-free LOC statistics (from tool-call replay) against git ground
truth (from ``git log --numstat``) to quantify the F1 exposure gap caused by
``Write`` calls that overwrite existing files.

Usage:
    tcer calibrate --project TCER --code-dir .

Outputs a per-session deviation report and a global calibration factor.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tcer.core import analyze, loc, paths, reader


@dataclass
class GitCommitDelta:
    """LOC change from a single git commit."""
    commit: str
    timestamp: int  # seconds since epoch
    added: int
    deleted: int
    files: list[str]


@dataclass
class SessionCalibration:
    """Calibration result for one session."""
    session_id: str
    tcer_added: int
    tcer_deleted: int
    git_added: int
    git_deleted: int

    @property
    def added_deviation(self) -> int:
        """tcer_added - git_added (positive = tcer overestimated)."""
        return self.tcer_added - self.git_added

    @property
    def deleted_deviation(self) -> int:
        """tcer_deleted - git_deleted (negative = tcer underestimated)."""
        return self.tcer_deleted - self.git_deleted

    @property
    def net_deviation(self) -> int:
        """Net LOC deviation (tcer_net - git_net)."""
        return (self.tcer_added - self.tcer_deleted) - (self.git_added - self.git_deleted)


def _parse_numstat_line(line: str) -> tuple[int, int, str] | None:
    """Parse a single --numstat line: 'added<tab>deleted<tab>path'."""
    # Split on tab, but git might output paths with quotes or special chars
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    added_s = parts[0].strip()
    deleted_s = parts[1].strip()
    path = "\t".join(parts[2:]).strip()  # Rejoin in case path contains tabs

    # Binary files show '-' for added/deleted
    if added_s == "-" or deleted_s == "-":
        return None

    try:
        added = int(added_s)
        deleted = int(deleted_s)
    except ValueError:
        # Skip lines that can't be parsed as integers
        return None

    # Remove surrounding quotes if present (git sometimes quotes paths)
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]

    # Only count code files (same filter as loc.py)
    if not loc._is_code(path):
        return None

    return added, deleted, path


def git_commits_in_window(
    repo_root: Path,
    start_ms: int | None,
    end_ms: int | None,
) -> list[GitCommitDelta]:
    """Fetch all commits in the time window from git log --numstat.

    Returns commits newest-first (reverse chronological).
    """
    if not (repo_root / ".git").is_dir():
        return []

    # Build git log command with time filters
    cmd = ["git", "log", "--numstat", "--format=%H %ct"]
    if start_ms is not None:
        cmd.append(f"--since={start_ms // 1000}")
    if end_ms is not None:
        cmd.append(f"--until={end_ms // 1000}")

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    # Parse output
    commits: list[GitCommitDelta] = []
    current_commit: str | None = None
    current_timestamp: int | None = None
    current_files: list[str] = []
    current_added = current_deleted = 0

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        # Commit header: hash + timestamp (not indented, contains space but no tab in first field)
        if "\t" not in line and " " in line and len(line.split()[0]) == 40:
            # Save previous commit if exists
            if current_commit is not None:
                commits.append(GitCommitDelta(
                    commit=current_commit,
                    timestamp=current_timestamp or 0,
                    added=current_added,
                    deleted=current_deleted,
                    files=current_files,
                ))

            # Parse new commit header
            parts = line.split(None, 1)
            current_commit = parts[0]
            current_timestamp = int(parts[1]) if len(parts) > 1 else 0
            current_files = []
            current_added = current_deleted = 0
        elif "\t" in line:
            # numstat line (contains tabs)
            parsed = _parse_numstat_line(line)
            if parsed:
                a, d, path = parsed
                current_added += a
                current_deleted += d
                current_files.append(path)

    # Save last commit
    if current_commit is not None:
        commits.append(GitCommitDelta(
            commit=current_commit,
            timestamp=current_timestamp or 0,
            added=current_added,
            deleted=current_deleted,
            files=current_files,
        ))

    return commits


def calibrate_project(
    project_hash: str,
    code_dir: Path | None = None,
    no_subagents: bool = False,
) -> list[SessionCalibration]:
    """Calibrate all sessions in a project against git history.

    Args:
        project_hash: Project hash or fuzzy name
        code_dir: Path to git repository (default: project's cwd)
        no_subagents: Skip subagent sessions

    Returns:
        List of SessionCalibration objects, one per session
    """
    # Resolve project
    proj_path = paths.resolve_project(project_hash)
    if proj_path is None:
        return []

    # Get all session paths
    all_paths = reader.discover_jsonl(proj_path.name)
    if no_subagents:
        session_paths = [p for p in all_paths if not reader.is_subagent(p)]
    else:
        session_paths = all_paths

    if not session_paths:
        return []

    # If code_dir not specified, try to infer from first session's cwd
    if code_dir is None:
        meta = reader.read_session_meta(session_paths[0])
        if meta and meta.cwd:
            code_dir = Path(meta.cwd)
        else:
            return []

    if not code_dir.is_dir():
        return []

    # Collect per-session TCER LOC and time windows
    results: list[SessionCalibration] = []

    for session_path in session_paths:
        meta = reader.read_session_meta(session_path)
        if not meta:
            continue

        # Get TCER LOC
        sloc = loc.session_loc_full(session_path)

        # Get time window from token usage
        usage = reader.aggregate_usage(session_path)

        # Get git commits in this time window
        git_commits = git_commits_in_window(
            code_dir,
            usage.started_at,
            usage.ended_at,
        )

        # Sum git deltas
        git_added = sum(c.added for c in git_commits)
        git_deleted = sum(c.deleted for c in git_commits)

        results.append(SessionCalibration(
            session_id=meta.session_id or session_path.stem,
            tcer_added=sloc.added,
            tcer_deleted=sloc.deleted,
            git_added=git_added,
            git_deleted=git_deleted,
        ))

    return results


def format_calibration_report(calibrations: list[SessionCalibration]) -> str:
    """Format a human-readable calibration report."""
    if not calibrations:
        return "No sessions to calibrate."

    lines = [
        "TCER LOC Calibration Report",
        "=" * 80,
        "",
        "Comparing git-free LOC (tool-call replay) vs git ground truth (git log --numstat)",
        "",
        f"{'Session':<40} {'TCER +/-':<15} {'Git +/-':<15} {'Net Δ':>10}",
        "-" * 80,
    ]

    total_tcer_net = 0
    total_git_net = 0
    total_added_dev = 0
    total_deleted_dev = 0

    for cal in calibrations:
        tcer_net = cal.tcer_added - cal.tcer_deleted
        git_net = cal.git_added - cal.git_deleted
        total_tcer_net += tcer_net
        total_git_net += git_net
        total_added_dev += cal.added_deviation
        total_deleted_dev += cal.deleted_deviation

        sid = cal.session_id[:38]
        tcer_str = f"+{cal.tcer_added} -{cal.tcer_deleted}"
        git_str = f"+{cal.git_added} -{cal.git_deleted}"
        dev_str = f"{cal.net_deviation:+d}"

        lines.append(f"{sid:<40} {tcer_str:<15} {git_str:<15} {dev_str:>10}")

    lines += [
        "-" * 80,
        "",
        "Summary",
        "-------",
        f"Total TCER net LOC    : {total_tcer_net:+,d}",
        f"Total Git net LOC     : {total_git_net:+,d}",
        f"Net deviation         : {total_tcer_net - total_git_net:+,d} ({((total_tcer_net / total_git_net - 1) * 100) if total_git_net else 0:.1f}%)",
        "",
        f"Total added deviation : {total_added_dev:+,d}",
        f"Total deleted deviation: {total_deleted_dev:+,d}",
        "",
    ]

    # Calibration factor suggestion
    if total_git_net != 0:
        factor = total_tcer_net / total_git_net
        lines += [
            "Calibration Factor",
            "------------------",
            f"TCER / Git ratio      : {factor:.4f}",
            "",
            "To adjust TCER values to match git:",
            f"  adjusted_tcer = tcer / {factor:.4f}",
            f"  adjusted_net_loc = net_loc / {factor:.4f}",
            "",
        ]

    # Per-session accuracy
    accurate = sum(1 for c in calibrations if abs(c.net_deviation) <= 10)
    lines += [
        f"Sessions within ±10 LOC: {accurate}/{len(calibrations)} ({accurate/len(calibrations)*100:.1f}%)",
        "",
    ]

    return "\n".join(lines)
