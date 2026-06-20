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

_DATA_PATH = Path(__file__).parent / "data" / "model_pricing.json"

# The four billing dimensions, in TCER's canonical key order.
RATE_KEYS = ("input", "output", "cache_read", "cache_write")


@lru_cache(maxsize=1)
def _load() -> dict:
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _rates(entry: dict) -> dict[str, float]:
    return {k: float(entry[k]) for k in RATE_KEYS}


def default_pricing() -> dict[str, float]:
    """Fallback rate map ($/MTok) used for unknown / mixed-model usage."""
    return _rates(_load()["default"])


@lru_cache(maxsize=512)
def _match_id(model: str) -> str | None:
    """Resolve a model string to a table key: exact id, else longest-prefix id.

    Claude Code appends dated / ``[1m]`` suffixes to the base id (e.g.
    ``claude-opus-4-8[1m]``); a prefix match maps those onto the base entry.
    Returns None if nothing in the table matches.
    """
    models = _load()["models"]
    if model in models:
        return model
    best_id: str | None = None
    for mid in models:
        if model.startswith(mid) and (best_id is None or len(mid) > len(best_id)):
            best_id = mid
    return best_id


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
