"""Process-level mtime/size keyed cache for expensive per-file session scans.

Used by Claude ``scan_session`` (and optionally other readers) so reanalyze /
date-filter changes do not re-walk unchanged JSONL files. Entries invalidate
when the file's ``(mtime_ns, size)`` changes.

Not thread-safe for concurrent writers of the same key; analysis workers are
cooperative (one logical analysis at a time with cancel). Cache hits return the
**same** object reference — callers must not mutate cached payloads in place
(``TokenUsage.merge`` already returns a new instance).

Cancellation-safe by construction: cooperative cancel raises inside the
factory, so a partial scan never reaches ``_CACHE`` — callers may pass
cancellable factories and still get caching for completed scans.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Hashable, TypeVar

T = TypeVar("T")

# (resolved_path, mtime_ns, size, *extra_key) → value
_CACHE: dict[tuple, Any] = {}
# 上限须容下「全部项目总览」的工作集：44 项目 × 数百会话文件 × 每文件
# usage/loc/meta 多变体 ≈ 2000 条。512 时全项目热态总览发生 LRU 轮换
# （实测 4.3s ≈ 冷态，单项目热态被挤回 464ms），2048 覆盖后回到毫秒级。
_MAX_ENTRIES = 2048


def clear() -> None:
    """Drop all cached scan results (tests / forced refresh)."""
    _CACHE.clear()


def stats() -> dict[str, int]:
    return {"entries": len(_CACHE), "max_entries": _MAX_ENTRIES}


def _sig(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    return resolved, int(st.st_mtime_ns), int(st.st_size)


def get_or_compute(
    path: Path,
    extra: Hashable,
    factory: Callable[[], T],
) -> T:
    """Return cached value for ``(path signature, extra)`` or compute and store.

    ``extra`` distinguishes variants of the same file (e.g. with_loc flag).
    If the file is unreadable, ``factory`` is called without caching.
    """
    sig = _sig(path)
    if sig is None:
        return factory()
    key = (*sig, extra)
    hit = _CACHE.get(key)
    if hit is not None:
        # LRU: refresh insertion order so hot entries survive eviction.
        _CACHE[key] = _CACHE.pop(key)
        return hit  # type: ignore[return-value]
    value = factory()
    if len(_CACHE) >= _MAX_ENTRIES:
        # Drop the least-recently-used entry (CPython 3.7+ dict order).
        try:
            del _CACHE[next(iter(_CACHE))]
        except (StopIteration, KeyError):
            pass
    _CACHE[key] = value
    return value
