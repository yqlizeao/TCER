"""Process-level mtime/size keyed cache for expensive per-file session scans.

Used by Claude ``scan_session`` (and optionally other readers) so reanalyze /
date-filter changes do not re-walk unchanged JSONL files. Entries invalidate
when the file's ``(mtime_ns, size)`` changes.

Not thread-safe for concurrent writers of the same key; analysis workers are
cooperative (one logical analysis at a time with cancel). Cache hits return the
**same** object reference — callers must not mutate cached payloads in place
(``TokenUsage.merge`` already returns a new instance).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Hashable, TypeVar

T = TypeVar("T")

# (resolved_path, mtime_ns, size, *extra_key) → value
_CACHE: dict[tuple, Any] = {}
_MAX_ENTRIES = 512


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
        return hit  # type: ignore[return-value]
    value = factory()
    if len(_CACHE) >= _MAX_ENTRIES:
        # Drop an arbitrary oldest insertion (CPython 3.7+ dict order).
        try:
            del _CACHE[next(iter(_CACHE))]
        except (StopIteration, KeyError):
            pass
    _CACHE[key] = value
    return value
