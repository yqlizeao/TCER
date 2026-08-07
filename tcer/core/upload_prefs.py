"""Persistence for the GUI 上传 dialog's remembered project selection.

Since server URL, auth, and detail options moved to environment config
(``env_config``; ``TCER_CLIENT_UPLOAD_URL`` / ``TCER_CLIENT_UPLOAD_AUTH_TOKEN`` /
``TCER_CLIENT_UPLOAD_DETAIL``), the only thing the dialog still persists is
**which projects were last selected**, so reopening the dialog pre-checks them.

Reads/writes a small JSON file under ``app_dirs.prefs_dir()`` (发布版优先 exe
同目录,回退 ``~/.tcer/``):``<prefs_dir>/tcer_upload.json``, mirroring the
atomic write pattern used by ``metrics.save_baselines``.

早期版本曾在此存 server_url / 账号 / 密码 / 选项,现已全部移除(改由环境变量驱动);
旧文件里的这些字段被静默忽略,只读取 ``last_projects``。
"""
from __future__ import annotations

import json
import os
import tempfile

from tcer.core.app_dirs import prefs_dir


def _prefs_path():
    return prefs_dir() / "tcer_upload.json"


_DEFAULTS: dict = {
    "last_projects": [],   # list of selected project keys (multi-select)
}


def load() -> dict:
    """Return the stored prefs merged over defaults (never raises)."""
    prefs = dict(_DEFAULTS)
    try:
        with _prefs_path().open("r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return prefs
    if not isinstance(stored, dict):
        return prefs
    projs = stored.get("last_projects")
    if not projs and stored.get("last_project"):  # back-compat scalar
        projs = [stored["last_project"]]
    prefs["last_projects"] = projs if isinstance(projs, list) else []
    return prefs


def save(prefs: dict) -> None:
    """Atomically persist the remembered project selection only."""
    projs = prefs.get("last_projects")
    out = {"last_projects": projs if isinstance(projs, list) else []}
    p = _prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise