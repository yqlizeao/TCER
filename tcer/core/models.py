"""Data classes used across the TCER pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModelUsage:
    """The four billing-relevant token counts for a single model within a session.

    Lightweight per-model bucket so mixed-model sessions can be priced exactly
    (each model at its own rate) instead of falling back to a single rate.
    """

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    def add(self, i: int, cw: int, cr: int, o: int) -> None:
        self.input_tokens += i
        self.cache_creation_input_tokens += cw
        self.cache_read_input_tokens += cr
        self.output_tokens += o


@dataclass(frozen=True)
class ToolOp:
    """One tool call recorded with its turn position for temporal analysis."""
    turn: int       # assistant message sequence (0-based)
    tool: str       # "Read" / "Write" / "Edit" / "Grep" / "Glob" / …
    path: str       # file_path from tool input ("" if unavailable)


@dataclass
class TokenUsage:
    """Accumulated token usage for one session (or an aggregate of sessions).

    Mirrors the four billing-relevant fields from ``message.usage`` plus a few
    counters that make reports self-describing. Populated by ``reader``.

    ``per_model`` breaks the same totals down by model id (key ``""`` for turns
    with no model recorded), so cost can be computed per model and summed —
    accurate even when a session mixed several models. ``merge`` keeps it in sync
    with the scalar totals across subagent-folding and session aggregation.
    """

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0  # cache writes, $3.75/MTok
    cache_read_input_tokens: int = 0  # cache reads, $0.30/MTok
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)
    per_model: dict[str, ModelUsage] = field(default_factory=dict)
    assistant_msgs: int = 0  # total assistant turns (incl. zero-usage stubs)
    empty_usage_skipped: int = 0  # assistant turns with all-zero usage
    started_at: int | None = None  # epoch ms of first counted assistant turn
    ended_at: int | None = None  # epoch ms of last counted assistant turn
    tool_calls: dict[str, int] = field(default_factory=dict)  # tool_name → call count
    session_duration_ms: int | None = None  # ended_at - started_at (computed property in practice)
    # --- new extraction fields ---
    user_msgs: int = 0  # count of type=="user" lines
    tool_errors: int = 0  # count of tool_result with is_error=true
    tool_errors_by_tool: dict[str, int] = field(default_factory=dict)  # tool_name → error count
    thinking_count: int = 0  # count of thinking content blocks
    tool_ops: list[ToolOp] = field(default_factory=list)  # ordered tool calls for temporal analysis
    user_message_texts: list[str] = field(default_factory=list)  # extracted user message text

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def total(self) -> int:
        return self.total_input + self.output_tokens

    @property
    def effective_turns(self) -> int:
        """Assistant turns that contributed actual tokens (excl. zero-usage stubs).

        Since zero-usage stubs are no longer counted in ``assistant_msgs``,
        this is simply ``assistant_msgs``.
        """
        return self.assistant_msgs

    def bucket(self, model: str) -> ModelUsage:
        """Return (creating if needed) the per-model bucket for ``model``."""
        mu = self.per_model.get(model)
        if mu is None:
            mu = ModelUsage()
            self.per_model[model] = mu
        return mu

    def merge(self, other: TokenUsage) -> TokenUsage:
        """Return a new TokenUsage that is the sum of self and other (for aggregation)."""
        # Merge tool_calls dicts
        merged_tools: dict[str, int] = {}
        for tool, count in self.tool_calls.items():
            merged_tools[tool] = count
        for tool, count in other.tool_calls.items():
            merged_tools[tool] = merged_tools.get(tool, 0) + count

        merged_start = _min_ms(self.started_at, other.started_at)
        merged_end = _max_ms(self.ended_at, other.ended_at)
        merged_duration = (merged_end - merged_start) if (merged_start and merged_end) else None

        # Rebase other's tool_ops turns so they continue after self's last turn
        self_max_turn = max((op.turn for op in self.tool_ops), default=-1)
        rebased_other_ops = [
            ToolOp(op.turn + self_max_turn + 1, op.tool, op.path)
            for op in other.tool_ops
        ]

        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            models=self.models | other.models,
            per_model=_merge_per_model(self.per_model, other.per_model),
            assistant_msgs=self.assistant_msgs + other.assistant_msgs,
            empty_usage_skipped=self.empty_usage_skipped + other.empty_usage_skipped,
            started_at=merged_start,
            ended_at=merged_end,
            tool_calls=merged_tools,
            session_duration_ms=merged_duration,
            user_msgs=self.user_msgs + other.user_msgs,
            tool_errors=self.tool_errors + other.tool_errors,
            tool_errors_by_tool=_merge_dicts(self.tool_errors_by_tool, other.tool_errors_by_tool),
            thinking_count=self.thinking_count + other.thinking_count,
            tool_ops=self.tool_ops + rebased_other_ops,
            user_message_texts=self.user_message_texts + other.user_message_texts,
        )


def _merge_per_model(
    a: dict[str, ModelUsage], b: dict[str, ModelUsage]
) -> dict[str, ModelUsage]:
    """Sum two per-model maps key by key into a fresh dict."""
    out: dict[str, ModelUsage] = {}
    for src in (a, b):
        for model, mu in src.items():
            dst = out.get(model)
            if dst is None:
                dst = ModelUsage()
                out[model] = dst
            dst.add(mu.input_tokens, mu.cache_creation_input_tokens,
                    mu.cache_read_input_tokens, mu.output_tokens)
    return out


def _merge_dicts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Sum two int-valued dicts key by key."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


def _min_ms(a: int | None, b: int | None) -> int | None:
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def _max_ms(a: int | None, b: int | None) -> int | None:
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


@dataclass
class SessionMeta:
    """Lightweight metadata for a session file (for list views)."""

    session_id: str | None
    cwd: str | None
    title: str | None
    path: Path
    is_subagent: bool
    entrypoint: str | None = None  # "claude-vscode" / "claude-cli" / etc.


@dataclass
class SessionReport:
    """A fully computed per-session report."""

    meta: SessionMeta
    usage: TokenUsage
    chr: float | None  # cache hit ratio, 0..1
    io_ratio: float | None
    cost: float  # USD at list price
    cost_per_mt: float | None  # $/Mt
    net_loc: int | None
    tcer: float | None  # LOC/Mt
    cpe: float | None  # $ per 1000 LOC
    # --- 综合评分 (G6), populated when loc_accumulated / task_type available ---
    loc_accumulated: int | None = None  # current codebase size (for NCPI / PSAC)
    ncpi: float | None = None  # net code production index = net_loc / loc_accumulated
    caf: float | None = None  # cache adjustment factor
    task_type: str | None = None  # one of metrics.TASK_CATEGORIES keys
    task_category: str | None = None  # one of metrics.TASK_CATEGORIES keys
    ttaf: float | None = None  # task type adjustment factor
    ntcer: float | None = None  # normalized TCER = tcer / TTAF
    ta_tcer: float | None = None  # backward compat alias for ntcer
    psac: float | None = None  # project-stage adjustment coefficient
    tcer_phase_adj: float | None = None  # tcer * psac
    ctei: float | None = None  # composite token efficiency index
    grade: str | None = None  # CTEI rating label
    # --- 代码产出与质量 (G4) ---
    code_added: int | None = None  # gross code lines added (from tool calls)
    code_deleted: int | None = None  # gross code lines deleted (from tool calls)
    code_reworked: int | None = None  # deleted lines the session itself had written
                                      # earlier (self-rework); churn = reworked / added
    subagent_count: int = 0  # number of subagent sessions folded into this one
    churn_ratio: float | None = None  # deleted / added (rework fraction)
    unseen_writes: int = 0  # Write calls whose target file hadn't been touched yet
                            # in this session (F1 exposure: prior size assumed 0)
    # --- timing metrics ---
    avg_turn_latency_sec: float | None = None  # (ended_at - started_at) / effective_turns in seconds
    session_duration_minutes: float | None = None  # session_duration_ms / 60000
    # --- tool usage pattern ---
    read_write_ratio: float | None = None  # Read / (Write + Edit)
    edit_ratio: float | None = None  # Edit / (Edit + Write)
    exploration_ratio: float | None = None  # (Grep + Glob) / total_tools
    subagent_density: float | None = None  # subagent_count / effective_turns
    # --- context efficiency ---
    cache_efficiency: float | None = None  # cache_read / cache_write (>1 means cache paid off)
    cache_write_ratio: float | None = None  # cache_write / total_input
    non_cached_input_ratio: float | None = None  # input / total_input
    # --- file-level quality ---
    high_churn_file_count: int = 0  # files edited ≥3 times
    high_churn_details: dict | None = None  # {path: count} for files edited ≥3 (for popup)
    test_net_loc: int | None = None  # net LOC in test files
    doc_net_loc: int | None = None  # net LOC in doc files
    test_loc_ratio: float | None = None  # test_net / net_loc
    doc_loc_ratio: float | None = None  # doc_net / net_loc
    # --- new quality metrics ---
    tool_error_rate: float | None = None  # tool_errors / total_tool_calls
    files_touched: int = 0  # unique file paths across Read/Write/Edit
    files_touched_details: dict | None = None  # {path: operations} for popup
    thinking_count: int = 0  # thinking content blocks
    search_edit_ratio: float | None = None  # edits / (searches + edits)
    read_before_write: float | None = None  # files read before being written/edited
