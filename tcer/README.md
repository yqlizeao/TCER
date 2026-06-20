# tcer

Token-to-Code Efficiency Ratio — offline metrics for Claude Code sessions.

`tcer` parses the JSONL session files Claude Code writes under `~/.claude/projects/`
(no API calls, no instrumentation, no git dependency, no network) and computes
token-efficiency metrics:

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
python -m tcer                                        # launch the Tkinter GUI (main entry)
python -m tcer.gui                                    # same (compatibility entry)
```

Running from `src/` puts the `tcer` package on the import path with zero setup —
no editable install, no console-script entry points on your PATH.

### GUI

`python -m tcer` (run from `tcer/src`) opens a Tkinter desktop window with a
three-column layout: **project list** on the left, **session list** in the middle,
and a **right panel** showing detailed metrics. The right panel is a tabbed
notebook with three tabs:

- **五层指标** (five-layer metrics) — 41 metrics across all five layers, with
  a glossary popup featuring color-coded metric explanations.
- **综合效率指数排名** (CTEI ranking) — per-session CTEI bar chart, sorted
  descending, colored by grade.
- **趋势** (trend) — line chart showing metric trends over time.

Top bar controls: **task type** selector, **time range** filter, and
**view toggle** (project/session). Features:

- **Export** — JSON / CSV / Markdown via the menu bar.
- **Model detail popup** — per-model token usage, cost, and percentage breakdown.
- **High-churn files popup** — files edited ≥3 times with edit counts.
- **Baseline reference** — current CTEI baselines shown in L5 metrics panel.

Analysis runs on a background thread so the UI stays responsive. Pure stdlib
`tkinter` (MVC architecture, 6 modules). Purely offline — no git dependency,
no network access.

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
- **F1 exposure (Write-over-preexisting-file)**: `session_loc` assumes `old=0`
  when a `Write` first touches a file in that session — correct for new files,
  wrong for overwrites of *existing* files (the whole new content is counted as
  added; the deletion is missed). `Edit` uses only line deltas and is immune.
  Reports show an `unseen_writes` count (Quality L3 layer): the number of first
  `Write` touches, upper-bounding F1 exposure. Real-world bias depends on your
  workflow: TCER's 9 sessions show 0% (new-file `Write` + old-file `Edit`), but
  a controlled overwrite-existing-file scenario inflates net by 100 lines per
  call. **NEW**: the GUI's「校准 LOC」button compares tool-call LOC against git
  ground truth (`git log --numstat`) to quantify actual deviation per session
  and compute a global calibration factor. The core stays git-free and the
  exposure is visible via the counter.
- **NCPI caveat**: at the whole-project aggregate level NCPI can approach/exceed
  1.0 (cumulative net vs current size); it's most meaningful per session.
- **TTAF source**: values follow the metric framework §6.4 (refactor 0.50,
  review 0.20), the authoritative framework.
- **CTEI chart**: per-session CTEI bars, sorted desc, colored by grade. Sessions
  with no measurable net code have no CPE/CTEI and are omitted. Available in
  the GUI's「综合效率指数排名」tab.

## Scope

Done: read layer + core metrics + git-free LOC + composite layer (CTEI / TTAF
/ TA-TCER / PSAC / NCPI / CAF) + L3 metrics + Tkinter GUI (MVC architecture,
6 modules) + CTEI bar chart + trend chart + export (JSON/CSV/MD) + model
detail popup + high-churn files popup + baseline reference in L5. GUI-only
(CLI retired). Purely offline — no git, no network. The CTEI formula
reproduces the framework's published per-session scores to <0.1%.
