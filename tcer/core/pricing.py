"""Per-model token pricing, loaded from ``data/model_pricing.json``.

The price table is sourced verbatim from cc-switch's ``seed_model_pricing()``
(vendor official list prices, ~160 models); see the JSON's ``_meta`` block for
provenance. This module resolves a Claude Code model id (``message.model``) to
its four ``$/MTok`` rates, falling back to the Anthropic list-price ``default``
for any model not in the table.

The config is hand-editable: add/adjust entries under ``models`` to extend
coverage (e.g. new providers) without touching code.
"""
from __future__ import annotations

import json
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


@lru_cache(maxsize=512)
def _match_id(model: str) -> str | None:
    """Resolve a model string to a table key, with bidirectional prefix matching.

    Three resolution strategies in priority order:
    1. Exact match: ``model`` is a table key.
    2. Forward prefix: ``model.startswith(mid)`` — handles date/``[1m]`` suffixes
       appended by Claude Code (e.g. ``claude-opus-4-8[1m]`` → ``claude-opus-4-8``).
    3. Reverse prefix: ``mid.startswith(model)`` — handles shortened ids written
       by JSONL (e.g. ``claude-opus-4-6`` → ``claude-opus-4-6-20260206``).
       When multiple table keys share the same prefix, the **shortest** match wins
       (closest to the caller's string, least ambiguous).

    Returns ``None`` if nothing in the table matches, or if *model* is empty.
    """
    if not model:
        return None
    models = _load()["models"]
    # 1. Exact
    if model in models:
        return model
    # 2. Forward prefix (model is longer than table key)
    best_fwd: str | None = None
    for mid in models:
        if model.startswith(mid) and (best_fwd is None or len(mid) > len(best_fwd)):
            best_fwd = mid
    if best_fwd is not None:
        return best_fwd
    # 3. Reverse prefix (table key is longer than model)
    best_rev: str | None = None
    for mid in models:
        if mid.startswith(model) and (best_rev is None or len(mid) < len(best_rev)):
            best_rev = mid
    return best_rev


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
