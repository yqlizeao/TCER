# tcer

Token-to-Code Efficiency Ratio — offline metrics for Claude Code sessions.

`tcer` parses the JSONL session files Claude Code writes under `~/.claude/projects/`
(no API calls, no instrumentation) and computes token-efficiency metrics:

- **CHR** — cache hit ratio
- **I/O ratio** — total input / output
- **cost** — USD, priced per model (each model's tokens at its own list-price
  rate; Anthropic list price as the fallback for unknown models)
- **$/Mt** — cost per million tokens
- **TCER** — net code LOC per million tokens
- **CPE** — cost per 1000 LOC

LOC is **git-free**: net added/deleted lines come from each session's own
file-mutating tool calls (`Write` / `Edit` / `MultiEdit` / `NotebookEdit`) recorded
in the JSONL — no `git` binary, exact per-session attribution, and it counts what
the model actually wrote (iterations included). Accumulated codebase size comes
from scanning the working directory.

Composite layer (L5), per the metric framework §6:

- **NCPI** — net LOC / accumulated codebase LOC (contribution density)
- **CAF** — cache adjustment factor: `TotalInput / (input + cache_write)`
- **TTAF / TA-TCER** — task-type adjustment; `TA-TCER = TCER / TTAF`
- **PSAC** — project-stage coefficient; `TCER_phase_adj = TCER × PSAC`
- **CTEI** — composite token efficiency index + rating (优秀/良好/中等/低效/极端低效)

Quality layer (L3):

- **churn ratio** — `deleted / added` code lines (rework fraction; lower is less
  rework). Other L3 signals (cyclomatic complexity, coverage delta) need external
  tools (radon/lizard/coverage.py) and are out of scope for the stdlib-only core.

## Run (no install needed)

Pure Python ≥3.11 standard library — **nothing to install**: no `pip`, no PATH
changes, fully portable. Run the package straight from `src/` with `python -m`:

```bash
cd tcer/src
python -m tcer.gui                                        # launch the Tkinter GUI
python -m tcer.cli list                                   # show all discovered projects

python -m tcer.cli report --project TCER                  # full report for a project
python -m tcer.cli report --project TCER --session <id>   # single session (substring match)
python -m tcer.cli report --project TCER --no-subagents   # main sessions only
python -m tcer.cli report --project TCER --code-dir DIR   # dir scanned for accumulated LOC
python -m tcer.cli report --project TCER --no-loc         # token metrics only (skip LOC/TCER)
python -m tcer.cli report --project TCER --csv out.csv    # export per-session CSV
python -m tcer.cli report --project TCER --json           # JSON to stdout
python -m tcer.cli report --project TCER --task-type debug      # TTAF/TA-TCER for a debug session
python -m tcer.cli report --project TCER --baseline-tcer 80     # override CTEI baselines
python -m tcer.cli report --project TCER --chart                # per-session CTEI bar chart (colored)
python -m tcer.cli report --project TCER --chart --no-color     # chart without ANSI color
```

Running from `src/` puts the `tcer` package on the import path with zero setup —
no editable install, no console-script entry points on your PATH.

### GUI

`python -m tcer.gui` (run from `tcer/src`) opens a desktop window: a
project list on the left, a metrics report on the right (summary cards +
per-session table colored by CTEI grade + a per-session CTEI bar chart). The
task-type selector and "exclude subagent" toggle re-run the analysis live. Pure
stdlib `tkinter`; analysis runs on a background thread so the UI stays responsive.

`--project` accepts a name or hash; `TCER` resolves to the `c--GitHub-TCER` folder.
By default **subagents are folded into their parent session** (one session =
main file + its subagents): their tokens and code count toward the parent, but
they are not listed or counted as separate sessions — so the session count
matches cc-switch. Use `--no-subagents` to exclude subagent data entirely.

`--task-type` is one of `feature` (default) / `feature-ext` / `debug` / `refactor`
/ `review` / `test`. CTEI baselines default to the framework's reference dataset
medians (TCER 76.59, NCPI 0.101, CPE 8.22) so scores stay on the published scale;
override them with `--baseline-*` once you have your own accumulated data.

## Design notes

- **Parsing layer** ports cc-switch's `session_manager/providers/claude.rs` (JSONL
  discovery, `isMeta` skip, head/tail metadata sampling, timestamp normalization).
- cc-switch never reads `message.usage`; this project adds usage aggregation
  (the part token-stats does).
- **Per-model pricing** (`pricing.py` + `data/model_pricing.json`): tokens are
  priced at each model's own `$/MTok` rate (≈160 models, sourced from cc-switch's
  `seed_model_pricing()`), so mixed-model sessions are exact; the `reader` keeps
  per-model token buckets and `cost_usd` sums each at its own rate, falling back
  to the Anthropic list-price `default` for unknown models. The table is a
  hand-editable JSON — add entries to extend coverage without touching code.
- **LOC is git-free** (`loc.py`): net added/deleted come from replaying each
  session's `Write`/`Edit`/`MultiEdit`/`NotebookEdit` tool calls; accumulated
  codebase size is a working-tree scan (`tree_loc`, skipping `.git`,
  `node_modules`, `__pycache__`, …). Exact per session, needs no git.
- **Tool-call LOC vs git net**: tool-call LOC counts what the model wrote and
  rewrote during sessions (the real Token→Code work), so it's typically larger
  than what eventually lands in git — a more faithful efficiency denominator that
  doesn't depend on commit habits.
- **Cross-session overwrite caveat**: a `Write` overwriting a file written in an
  *earlier* session can't see its prior length, so it counts the full content as
  added (intra-session overwrites are tracked exactly).
- **NCPI caveat**: at the whole-project aggregate level NCPI can approach/exceed
  1.0 (cumulative net vs current size); it's most meaningful per session.
- **TTAF source**: values follow the metric framework §6.4 (refactor 0.50,
  review 0.20), the authoritative framework.
- **`--chart`**: per-session CTEI bars, sorted desc, colored by grade. Sessions
  with no measurable net code have no CPE/CTEI and are omitted. ANSI color is
  auto-disabled when stdout is not a TTY (e.g. piped).

## Scope

Done: read layer + core metrics + git-free LOC + CLI + composite layer (CTEI /
TTAF / TA-TCER / PSAC / NCPI / CAF) + L3 churn ratio + per-session CTEI bar chart
+ Tkinter GUI. The CTEI formula reproduces the framework's published per-session
scores to <0.1% (see `tests/test_metrics.py`). Planned: remaining L3 signals
(cyclomatic complexity / coverage delta) as opt-in radon/lizard/coverage.py.
