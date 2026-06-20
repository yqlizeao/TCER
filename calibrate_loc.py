"""Calibration harness for the LOC layer (investigation tool, NOT part of tcer).

Quantifies how much the Write-over-preexisting-file bug (F1) inflates net_loc —
and therefore TCER. tcer's ``loc.session_loc`` assumes ``old=0`` for any file a
session hasn't touched yet, so a ``Write`` that overwrites an *existing* file
counts the whole new content as added and never records the deletion. This script
replays each session's file-mutating tool calls TWICE in lockstep:

  - tcer path : the EXACT production logic (file_lines starts empty) — same code
                path as ``loc.session_loc`` via ``loc._delta_for_tool``.
  - git path  : same logic, but on first ``Write`` touch of a file, seeds its
                real prior line count from git (the file's size in the last commit
                before the session started) — the ground truth tcer can't see.

The gap between the two is the F1 inflation. Edit / MultiEdit / NotebookEdit only
use line *deltas*, so they agree between the two paths — the entire gap is
attributable to ``Write`` on pre-existing files.

git is used ONLY here, for ground truth. tcer itself stays git-free.

Run from the repo root:

    python calibrate_loc.py --project TCER
    python calibrate_loc.py --project TCER --no-subagents
    python calibrate_loc.py --project TCER --session <substring>
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Importable as a loose script: put tcer's src/ on the path.
sys.path.insert(0, str(Path(__file__).parent / "tcer" / "src"))
from tcer import loc, reader  # noqa: E402
from tcer.paths import resolve_project  # noqa: E402

_EDIT_TOOLS = loc._EDIT_TOOLS  # reuse production tool set so we stay in sync


def git_prior_lines(fp: str, started_at_ms: int, cwd: str,
                    cache: dict[str, int]) -> int:
    """Lines of ``fp`` as it existed in git just before the session started.

    Returns 0 when the file wasn't committed before the session (new / untracked /
    outside the repo) — in that case tcer's old=0 is correct and there is nothing
    to calibrate. Conservative: only counts committed state, so uncommitted edits
    from an earlier not-yet-committed session make this *underestimate* prior size
    (and thus underestimate F1 inflation).
    """
    if fp in cache:
        return cache[fp]
    cache[fp] = 0  # default until proven otherwise
    try:
        rel = os.path.relpath(fp, cwd).replace("\\", "/")
    except ValueError:
        return 0
    if rel.startswith(".."):
        return 0  # file outside the repo — can't ground-truth
    iso = datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    try:
        commit = subprocess.run(
            ["git", "rev-list", "-1", "--before", iso, "HEAD", "--", rel],
            cwd=cwd, capture_output=True, text=True,
        ).stdout.strip()
        if not commit:
            return 0  # file not yet committed before this session
        blob = subprocess.run(
            ["git", "show", f"{commit}:{rel}"],
            cwd=cwd, capture_output=True,
        ).stdout.decode("utf-8", "replace")
    except (FileNotFoundError, OSError):
        return 0  # git not installed / not a git repo
    prior = len(blob.splitlines())
    cache[fp] = prior
    return prior


def calibrate_session(path: Path, cwd: str | None) -> dict:
    """Replay one session's tool calls under both the tcer and git assumptions.

    Ground-truth prior size is looked up at each Write's OWN timestamp (not the
    session's first-message time) — the most precise comparison point, and robust
    to sessions whose assistant turns all have usage=0 (which makes
    ``aggregate_usage.started_at`` None).
    """
    tcer_fl: dict[str, int] = {}
    git_fl: dict[str, int] = {}
    tcer_a = tcer_d = git_a = git_d = 0
    writes_total = writes_preexisting = 0
    cache: dict[str, int] = {}

    for obj in reader.iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        ts_ms = reader.parse_timestamp_ms(obj.get("timestamp"))
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
            if not fp or not loc._is_code(fp):
                continue
            # Seed git ground truth on first Write touch (Edit doesn't need it —
            # it only uses the new_string/old_string delta, not absolute size).
            if name == "Write":
                writes_total += 1
                if fp not in git_fl:
                    prior = (git_prior_lines(fp, ts_ms, cwd, cache)
                             if cwd and ts_ms else 0)
                    git_fl[fp] = prior
                    if prior > 0:
                        writes_preexisting += 1
            a1, d1 = loc._delta_for_tool(name, inp, tcer_fl, fp)  # production path
            tcer_a += a1
            tcer_d += d1
            a2, d2 = loc._delta_for_tool(name, inp, git_fl, fp)    # ground-truth path
            git_a += a2
            git_d += d2

    return {
        "tcer_a": tcer_a, "tcer_d": tcer_d,
        "git_a": git_a, "git_d": git_d,
        "writes_total": writes_total, "writes_preexisting": writes_preexisting,
    }


def _fmt(n: int) -> str:
    return f"{n:+d}" if n else "0"


def _pct(infl: int, base: int) -> str:
    return f"{infl * 100 / base:+.0f}%" if base else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Calibrate tcer net_loc against git ground truth (F1).")
    ap.add_argument("--project", required=True)
    ap.add_argument("--session", default=None, help="substring match on session id")
    ap.add_argument("--no-subagents", action="store_true")
    args = ap.parse_args()

    proj = resolve_project(args.project)
    if proj is None:
        sys.exit(f"project '{args.project}' not found under ~/.claude/projects")
    files = reader.discover_jsonl(proj.name)
    if args.no_subagents:
        files = [f for f in files if not reader.is_subagent(f)]

    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(reader.parent_session_id(f), []).append(f)
    if args.session:
        groups = {k: v for k, v in groups.items() if args.session in k}
        if not groups:
            sys.exit(f"no session matches '{args.session}'")

    print(f"Project: {proj.name}   sessions: {len(groups)}   "
          f"{'(subagents excluded)' if args.no_subagents else '(subagents folded into parent)'}")
    print("-" * 96)
    print(f"{'session':<36} {'writes':>7} {'pre':>5} "
          f"{'tcer net':>9} {'git net':>9} {'inflation':>10} {'%':>6}")
    print("-" * 96)

    tot = {k: 0 for k in ("tcer_a", "tcer_d", "git_a", "git_d",
                          "writes_total", "writes_preexisting")}
    for key in sorted(groups):
        cwd = None
        # cwd comes from the group's main file; reuse across its subagents.
        main = next((f for f in groups[key] if not reader.is_subagent(f)), None)
        if main:
            cwd = reader.read_session_meta(main).cwd
        r = {k: 0 for k in tot}
        for f in groups[key]:
            sr = calibrate_session(f, cwd)
            for k in tot:
                r[k] += sr[k]
        tcer_net = r["tcer_a"] - r["tcer_d"]
        git_net = r["git_a"] - r["git_d"]
        infl = tcer_net - git_net
        print(f"{key[:34]:<36} {r['writes_total']:>7} {r['writes_preexisting']:>5} "
              f"{_fmt(tcer_net):>9} {_fmt(git_net):>9} {_fmt(infl):>10} {_pct(infl, git_net):>6}")
        for k in tot:
            tot[k] += r[k]

    print("-" * 96)
    tcer_net = tot["tcer_a"] - tot["tcer_d"]
    git_net = tot["git_a"] - tot["git_d"]
    infl = tcer_net - git_net
    print(f"{'AGGREGATE':<36} {tot['writes_total']:>7} {tot['writes_preexisting']:>5} "
          f"{_fmt(tcer_net):>9} {_fmt(git_net):>9} {_fmt(infl):>10} {_pct(infl, git_net):>6}")
    print()
    print(f"tcer net_loc       = {tcer_net:+d}")
    print(f"git-calibrated net = {git_net:+d}")
    print(f"F1 inflation       = {infl:+d}  ({_pct(infl, git_net)} of git net)")
    print(f"pre-existing Writes = {tot['writes_preexisting']} / "
          f"{tot['writes_total']} total Writes")


if __name__ == "__main__":
    main()
