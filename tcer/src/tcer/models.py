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
    assistant_msgs: int = 0  # assistant turns counted toward the totals
    empty_usage_skipped: int = 0  # assistant turns with all-zero usage (skipped)
    started_at: int | None = None  # epoch ms of first counted assistant turn
    ended_at: int | None = None  # epoch ms of last counted assistant turn

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def total(self) -> int:
        return self.total_input + self.output_tokens

    def bucket(self, model: str) -> ModelUsage:
        """Return (creating if needed) the per-model bucket for ``model``."""
        mu = self.per_model.get(model)
        if mu is None:
            mu = ModelUsage()
            self.per_model[model] = mu
        return mu

    def merge(self, other: TokenUsage) -> TokenUsage:
        """Return a new TokenUsage that is the sum of self and other (for aggregation)."""
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
            started_at=_min_ms(self.started_at, other.started_at),
            ended_at=_max_ms(self.ended_at, other.ended_at),
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
    # --- composite layer (L5), populated when loc_accumulated / task_type available ---
    loc_accumulated: int | None = None  # current codebase size (for NCPI / PSAC)
    ncpi: float | None = None  # net code production index = net_loc / loc_accumulated
    caf: float | None = None  # cache adjustment factor
    task_type: str | None = None  # one of metrics.TTAF keys
    ta_tcer: float | None = None  # task-adjusted TCER = tcer / TTAF
    psac: float | None = None  # project-stage adjustment coefficient
    tcer_phase_adj: float | None = None  # tcer * psac
    ctei: float | None = None  # composite token efficiency index
    grade: str | None = None  # CTEI rating label
    # --- quality layer (L3) ---
    code_added: int | None = None  # gross code lines added (from tool calls)
    code_deleted: int | None = None  # gross code lines deleted (from tool calls)
    subagent_count: int = 0  # number of subagent sessions folded into this one
    churn_ratio: float | None = None  # deleted / added (rework fraction)
