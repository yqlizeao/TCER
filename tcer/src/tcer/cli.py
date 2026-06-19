"""Command-line entry point: ``tcer list`` and ``tcer report``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyze, metrics, reader, report
from .paths import list_projects


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tcer",
        description="Token-to-Code Efficiency Ratio metrics from local Claude Code sessions.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list discovered Claude Code projects")
    sub.add_parser("gui", help="launch the Tkinter GUI")

    r = sub.add_parser("report", help="compute metrics for a project/session")
    r.add_argument("--project", required=True,
                   help="project name or hash (e.g. TCER resolves c--GitHub-TCER)")
    r.add_argument("--session", help="filter to sessions whose id contains this substring")
    r.add_argument("--no-subagents", action="store_true",
                   help="exclude subagent session files (included by default)")
    r.add_argument("--code-dir",
                   help="working directory scanned for accumulated LOC (defaults to a session's cwd)")
    r.add_argument("--no-loc", action="store_true", help="skip LOC / TCER (token metrics only)")
    r.add_argument("--task-type", choices=sorted(metrics.TTAF), default="feature",
                   help="task type for TTAF / TA-TCER (default: feature)")
    r.add_argument("--baseline-tcer", type=float, default=metrics.TCER_BASELINE,
                   help=f"CTEI TCER baseline (default {metrics.TCER_BASELINE}, report median)")
    r.add_argument("--baseline-ncpi", type=float, default=metrics.NCPI_BASELINE,
                   help=f"CTEI NCPI baseline (default {metrics.NCPI_BASELINE})")
    r.add_argument("--baseline-cpe", type=float, default=metrics.CPE_BASELINE,
                   help=f"CTEI CPE baseline (default {metrics.CPE_BASELINE}, report median)")
    r.add_argument("--json", action="store_true", help="emit JSON to stdout")
    r.add_argument("--csv", metavar="FILE", help="write per-session CSV to FILE")
    r.add_argument("--chart", action="store_true", help="render a per-session CTEI bar chart")
    r.add_argument("--no-color", action="store_true", help="disable ANSI color in the chart")
    return p


def cmd_list(args) -> int:
    projects = list_projects()
    if not projects:
        print("(no projects found under ~/.claude/projects)")
        return 0
    headers = ["project hash", "sessions", "subagents"]
    rows = []
    for d in projects:
        files = reader.discover_jsonl(d.name)
        subs = sum(1 for f in files if reader.is_subagent(f))
        rows.append([d.name, str(len(files) - subs), str(subs)])
    print(report._table(headers, rows))
    return 0


def cmd_report(args) -> int:
    try:
        result = analyze.analyze_project(
            args.project,
            session=args.session,
            no_subagents=args.no_subagents,
            code_dir=args.code_dir,
            no_loc=args.no_loc,
            task_type=args.task_type,
            baseline_tcer=args.baseline_tcer,
            baseline_ncpi=args.baseline_ncpi,
            baseline_cpe=args.baseline_cpe,
        )
    except FileNotFoundError as e:
        raise SystemExit(f"error: {e}")

    if args.json:
        print(report.to_json(result.reports, result.aggregate, result.n_sessions))
        return 0
    if args.csv:
        Path(args.csv).write_text(report.to_csv(result.reports), encoding="utf-8")
        print(f"wrote {args.csv} ({result.n_sessions} sessions)")
        return 0

    print(report.session_table(result.reports))
    print()
    print(report.aggregate_block(result.aggregate, result.code_dir, result.n_sessions))
    if args.chart:
        color = not args.no_color and sys.stdout.isatty()
        print()
        print(report.ctei_chart(result.reports, color=color))
    return 0


def cmd_gui(args) -> int:
    from . import gui
    return gui.main()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "gui":
        return cmd_gui(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
