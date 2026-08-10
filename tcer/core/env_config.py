"""Environment-driven TCER server config (opt-in networking + packaged support).

The upload feature is gated on ``TCER_CLIENT_UPLOAD_URL``: the GUI only shows its
「上传…」button when a server URL is configured. This keeps the default build
fully offline — no server, no button.

Config sources, in precedence order (first non-empty wins per key):
1. Process environment variables.
2. A ``.env`` file next to the running executable (发布版 PyInstaller: exe 同目录;
   源码运行: 当前工作目录 / repo root fallback). This lets a packaged client ship
   with a co-located ``.env`` that "just works" without editing the environment.

Recognized keys:
- ``TCER_CLIENT_UPLOAD_URL``          server base URL. Unset → upload UI hidden.
- ``TCER_CLIENT_UPLOAD_AUTH_TOKEN``    long-lived API token. Set → uploads authenticate
                               as that token's user; unset → anonymous uploads.
- ``TCER_CLIENT_UPLOAD_DETAIL`` "true"/"1"/"yes" → attach per-session detail
                               (conversation). Unset/anything else → False.

Values are read once and cached; call :func:`reset_cache` in tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_KEYS = ("TCER_CLIENT_UPLOAD_URL", "TCER_CLIENT_UPLOAD_AUTH_TOKEN", "TCER_CLIENT_UPLOAD_DETAIL")

_CACHE: dict[str, str] | None = None


def _dotenv_paths() -> list[Path]:
    """Candidate ``.env`` locations, in search order.

    发布版(frozen): exe 同目录优先(便携——与打包客户端同目录的 .env 自动生效)。
    源码运行: 当前工作目录,再回退到仓库根(便于开发)。
    """
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent / ".env")
    else:
        out.append(Path.cwd() / ".env")
        # repo root: this file is tcer/core/env_config.py → parents[2] == repo root
        try:
            out.append(Path(__file__).resolve().parents[2] / ".env")
        except IndexError:
            pass
    # De-dup while preserving order.
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env file (never raises).

    Ignores blank lines and ``#`` comments; strips surrounding quotes and a
    leading ``export``. Only recognized keys are kept.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k not in _KEYS:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def _load() -> dict[str, str]:
    """Resolve config from env then .env (env wins per key)."""
    merged: dict[str, str] = {}
    # .env first (lowest precedence), then override with real env vars.
    for path in _dotenv_paths():
        if path.is_file():
            for k, v in _parse_dotenv(path).items():
                merged.setdefault(k, v)
            break  # first existing .env wins
    for k in _KEYS:
        env_v = os.environ.get(k)
        if env_v is not None and env_v != "":
            merged[k] = env_v
    return merged


def _cfg() -> dict[str, str]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def reset_cache() -> None:
    """Clear the process-level cache (tests: change env/.env then re-read)."""
    global _CACHE
    _CACHE = None


def server_url() -> str | None:
    """Configured server base URL, or None when unset (upload UI stays hidden)."""
    v = (_cfg().get("TCER_CLIENT_UPLOAD_URL") or "").strip()
    return v.rstrip("/") or None


def api_token() -> str | None:
    """Configured API token, or None (→ anonymous uploads)."""
    v = (_cfg().get("TCER_CLIENT_UPLOAD_AUTH_TOKEN") or "").strip()
    return v or None


def upload_detail() -> bool:
    """Whether to attach per-session detail. Default False when unset."""
    v = (_cfg().get("TCER_CLIENT_UPLOAD_DETAIL") or "").strip().lower()
    return v in _TRUE


def upload_enabled() -> bool:
    """True when a server URL is configured (the gate for showing the upload UI)."""
    return server_url() is not None
