"""Persistence for the GUI 上传 dialog's options.

Reads/writes a small JSON file next to the Claude config dir
(``<CLAUDE_CONFIG_DIR or ~/.claude>/tcer_upload.json``), mirroring the atomic
write pattern used by ``metrics.save_baselines``.

Stored keys: server_url, username, password (optionally, obfuscated — NOT
encrypted), anonymous, last_project, detail, auto_upload, interval_min,
remember_password.

The password, when the user opts to remember it, is stored base64-obfuscated
only — this is deterrence against shoulder-surfing a plaintext file, not real
protection. The UI notes this. When ``remember_password`` is false the password
is never written.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile

from tcer.core.paths import _claude_dir

_PREFS_PATH = _claude_dir() / "tcer_upload.json"

_DEFAULTS: dict = {
    "server_url": "http://127.0.0.1:8899",
    "username": "",
    "password": "",
    "remember_password": False,
    "anonymous": False,
    "last_projects": [],   # list of selected project keys (multi-select)
    "all_sessions": False,  # upload every session (detail), not just aggregate
    "detail": False,
    "auto_upload": False,
    "interval_min": 30,
}


def _obfuscate(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _deobfuscate(text: str) -> str:
    try:
        return base64.b64decode(text.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def load() -> dict:
    """Return the stored prefs merged over defaults (never raises)."""
    prefs = dict(_DEFAULTS)
    try:
        with _PREFS_PATH.open("r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return prefs
    if not isinstance(stored, dict):
        return prefs
    prefs.update({k: stored[k] for k in _DEFAULTS if k in stored})
    # Back-compat: an earlier version stored a single ``last_project`` scalar.
    if not prefs["last_projects"] and stored.get("last_project"):
        prefs["last_projects"] = [stored["last_project"]]
    if not isinstance(prefs["last_projects"], list):
        prefs["last_projects"] = []
    # Password is stored obfuscated under a separate key so a plaintext
    # "password" is never persisted by accident.
    if prefs.get("remember_password") and stored.get("password_obf"):
        prefs["password"] = _deobfuscate(str(stored["password_obf"]))
    else:
        prefs["password"] = ""
    return prefs


def save(prefs: dict) -> None:
    """Atomically write prefs. Password only persisted if remember_password."""
    out = {k: prefs.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
    remember = bool(out.get("remember_password"))
    pwd = str(out.pop("password", "") or "")
    if remember and pwd:
        out["password_obf"] = _obfuscate(pwd)
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_PREFS_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(_PREFS_PATH))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise