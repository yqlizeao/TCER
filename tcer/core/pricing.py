"""Per-model token pricing, loaded from ``data/model_pricing.json``.

The price table is sourced verbatim from cc-switch's ``seed_model_pricing()``
(vendor official list prices, ~162 models); see the JSON's ``_meta`` block for
provenance. This module resolves a Claude Code model id (``message.model``) to
its four ``$/MTok`` rates, falling back to the Anthropic list-price ``default``
for any model not in the table.

The config is hand-editable: add/adjust entries under ``models`` to extend
coverage (e.g. new providers) without touching code.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).parent.parent / "config" / "model_pricing.json"

# The four billing dimensions, in TCER's canonical key order.
_RATE_KEYS = ("input", "output", "cache_read", "cache_write")


@lru_cache(maxsize=1)
def _load() -> dict:
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _rates(entry: dict) -> dict[str, float]:
    return {k: float(entry[k]) for k in _RATE_KEYS}


def default_pricing() -> dict[str, float]:
    """Fallback rate map ($/MTok) used for unknown / mixed-model usage."""
    return _rates(_load()["default"])


def _normalize(s: str) -> str:
    """Aggressive normalization for fallback matching.

    Lowercases, then spells a version dot literally in the two common damaged
    forms providers emit — ``5p2`` → ``5.2`` (fireworks) and ``5-6`` → ``5.6``
    (providers that render ``gpt-5.6`` as ``gpt-5-6``) — before dropping
    ``-``/``_`` separators. So ``GLM-5.2``, ``glm5.2`` and ``glm-5p2`` all
    collapse to ``glm5.2``, and ``gpt-5-6-sol`` collapses onto ``gpt5.6sol``
    (== ``gpt-5.6-sol``). Without the ``5-6`` rule, ``gpt-5-6-sol`` would fall
    through to forward-prefix matching and silently bind to the shorter
    ``gpt-5`` key (wrong label AND wrong price). Only used as a last resort by
    ``_match_id`` after exact / prefix matching fails; ``_norm_index`` drops any
    bucket two distinct keys collapse onto, so this can never merge two real
    models and corrupt pricing.
    """
    s = s.lower()
    s = re.sub(r"(\d)p(\d)", r"\1.\2", s)
    s = re.sub(r"(\d)-(\d)", r"\1.\2", s)
    return s.replace("-", "").replace("_", "")


@lru_cache(maxsize=1)
def _norm_index() -> dict[str, str]:
    """Normalized-form → table key, keeping only unambiguous mappings.

    If two table keys ever normalize to the same form (a future edit could do
    it), both are dropped so a fuzzy match can never silently merge two
    distinct models and corrupt pricing.
    """
    buckets: dict[str, list[str]] = {}
    for k in _load()["models"]:
        buckets.setdefault(_normalize(k), []).append(k)
    return {n: ks[0] for n, ks in buckets.items() if len(ks) == 1}


@lru_cache(maxsize=512)
def _match_id(model: str) -> str | None:
    """Resolve a model string to a table key, with bidirectional prefix matching.

    Resolution strategies in priority order:
    1. Exact match: ``model`` is a table key.
    2. Normalized exact: lowercase, drop ``-``/``_``, and spell a version dot
       literally from both damaged forms (``5p2`` → ``5.2``, ``5-6`` → ``5.6``).
       Collapses ``glm5.2`` / ``GLM-5.2`` / ``glm-5p2`` onto ``glm-5.2``, and
       ``gpt-5-6-sol`` onto ``gpt-5.6-sol``. Tried before prefix matching so a
       damaged id doesn't forward-prefix onto a shorter key (``glm-5p2`` onto
       ``glm-5``, or ``gpt-5-6-sol`` onto ``gpt-5``).
    3. Forward prefix: ``model.startswith(mid)`` — handles date/``[1m]`` suffixes
       appended by Claude Code (e.g. ``claude-opus-4-8[1m]`` → ``claude-opus-4-8``).
    4. Reverse prefix: ``mid.startswith(model)`` — handles shortened ids written
       by JSONL (e.g. ``claude-opus-4-6`` → ``claude-opus-4-6-20260206``).
       When multiple table keys share the same prefix, the **shortest** match wins
       (closest to the caller's string, least ambiguous).

    Each strategy is tried first on the raw ``model``, then on its last ``/``
    segment (to strip a vendor path prefix like ``z-ai/`` or
    ``accounts/fireworks/models/``). Returns ``None`` if nothing in the table
    matches, or if *model* is empty.
    """
    if not model:
        return None
    models = _load()["models"]
    # Candidates: the raw id, plus its last path segment if a vendor prefixed it
    # ("z-ai/glm-5.2" -> also try "glm-5.2"). Raw is tried first to preserve the
    # long-standing [1m]/date-suffix behaviour exactly.
    candidates = [model]
    if "/" in model:
        candidates.append(model.rsplit("/", 1)[-1])
    for cand in candidates:
        # 1. Exact
        if cand in models:
            return cand
        # 2. Normalized exact (tolerant of case / missing dash / '5p2' for '5.2').
        #    Tried BEFORE prefix matching so "glm-5p2" resolves to "glm-5.2" here
        #    instead of forward-prefixing onto the shorter table key "glm-5".
        nk = _norm_index().get(_normalize(cand))
        if nk is not None:
            return nk
        # 3. Forward prefix (candidate is longer than table key)
        best_fwd: str | None = None
        for mid in models:
            if cand.startswith(mid) and (best_fwd is None or len(mid) > len(best_fwd)):
                best_fwd = mid
        if best_fwd is not None:
            return best_fwd
        # 4. Reverse prefix (table key is longer than candidate)
        best_rev: str | None = None
        for mid in models:
            if mid.startswith(cand) and (best_rev is None or len(mid) < len(best_rev)):
                best_rev = mid
        if best_rev is not None:
            return best_rev
    return None


def table_key(model: str | None) -> str | None:
    """The canonical table key for *model*, or ``None`` if it falls back to default.

    Lets callers tell priced-from-the-table apart from Anthropic-list-price
    fallback without re-running the match itself.
    """
    return _match_id(model) if model else None


@lru_cache(maxsize=512)
def normalize(model: str) -> str:
    """Return the canonical table key for *model*, or *model* itself if unknown.

    Use this to deduplicate per_model buckets (e.g. merge ``claude-opus-4-6`` into
    ``claude-opus-4-6-20260206``).  The returned string is always a valid table key
    when a match exists, so ``label()`` and ``resolve()`` will find it directly.
    """
    mid = _match_id(model)
    return mid if mid is not None else model


@lru_cache(maxsize=512)
def resolve(model: str | None) -> dict[str, float]:
    """Resolve a model id to its $/MTok rates, falling back to ``default``."""
    if model:
        mid = _match_id(model)
        if mid is not None:
            return _rates(_load()["models"][mid])
    return default_pricing()


@lru_cache(maxsize=512)
def label(model: str | None) -> str:
    """Friendly display name for a model id, tolerant of ``[1m]``/date suffixes.

    Falls back to the raw id for models not in the table, so nothing is hidden.
    """
    if not model:
        return "-"
    mid = _match_id(model)
    return _load()["models"][mid]["display_name"] if mid is not None else model


def model_count() -> int:
    return len(_load()["models"])
