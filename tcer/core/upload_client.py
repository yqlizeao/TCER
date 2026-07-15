"""HTTP client for pushing TCER reports to the web backend.

Pure-stdlib (``urllib``) so the zero-dependency rule holds. Mirrors the
``web/backend`` contract:

    POST /api/login   {username, password}            -> {token}
    POST /api/upload  (Bearer <token>) <payload>       -> {inserted}

The payload is built from ``export.report_row_dict`` (server schema is aligned
to those field names), wrapped with the envelope documented in
``web/PLAN-client-upload.md`` §4.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any

from tcer.core import export
from tcer.core.models import SessionReport

try:
    from tcer import __version__ as _VERSION
except Exception:  # pragma: no cover
    _VERSION = "unknown"


class UploadError(Exception):
    """Raised on login / upload failure with a human-readable message."""


def _post_json(url: str, payload: dict, token: str | None = None,
               timeout: float = 30.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            pass
        raise UploadError(f"HTTP {e.code}{f': {detail}' if detail else ''}") from e
    except urllib.error.URLError as e:
        raise UploadError(f"无法连接服务器：{e.reason}") from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError as e:
        raise UploadError("服务器返回了无法解析的响应") from e


def _base(server_url: str) -> str:
    return server_url.rstrip("/")


def anon_label(user: str | None) -> str:
    """Stable pseudonym for anonymous uploads.

    Anonymous uploads still need a *consistent per-user* person label so the web
    side can group one user's anonymized rows together (rather than collapsing
    everyone into a single 未标注 bucket). We derive a deterministic short hash
    from the username so the same user always maps to the same "匿名-xxxxxx"
    handle without exposing the real name.
    """
    seed = (user or "").strip().lower()
    if not seed:
        seed = "anonymous"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"匿名-{digest}"


def login(server_url: str, username: str, password: str,
          timeout: float = 30.0) -> str:
    """Exchange credentials for a bearer token. Raises UploadError on failure."""
    data = _post_json(f"{_base(server_url)}/api/login",
                      {"username": username, "password": password},
                      timeout=timeout)
    token = data.get("token")
    if not token:
        raise UploadError("登录失败：服务器未返回令牌")
    return str(token)


def build_payload(
    *,
    aggregate: SessionReport,
    reports: list[SessionReport],
    n_sessions: int,
    project: str,
    user: str | None,
    anonymous: bool,
    detail: bool,
) -> dict[str, Any]:
    """Build the upload envelope from an analysis result.

    Per-session rows are **always** sent (when any exist) so each session lands
    as its own row on the server — that's what drives the time axis and lets the
    web side group / dedup by session-id. ``detail`` does NOT decide whether
    sessions are split; it only decides whether each session row also carries the
    turn-by-turn conversation (user messages). ``detail=False`` sends per-session
    *metrics only*; ``detail=True`` attaches the conversation too.

    The aggregate row is included for informational ``sessions_counted`` but the
    server drops it whenever session rows are present (it re-derives the
    aggregate by summing them), so it never double-counts.

    **Scrubbing vs. detail**: anonymous uploads redact local-identifying fields
    (title / path / cwd) *only when* ``detail=False``. When the user opts to
    attach the conversation (``detail=True``) they are consciously sharing the
    session content, so title / path / cwd are sent as-is — scrubbing them while
    shipping the full transcript would be pointless.
    """
    # Anonymous uploads carry a stable pseudonym (not null) so the web side can
    # still group one user's rows together instead of piling everyone into 未标注.
    person = anon_label(user) if anonymous else user
    scrub = anonymous and not detail
    agg_row = export.report_row_dict(aggregate) | {"sessions_counted": n_sessions}
    if scrub:
        _scrub_identifying(agg_row, project)
    payload: dict[str, Any] = {
        "client_version": f"tcer {_VERSION}",
        "anonymous": bool(anonymous),
        "user": person,
        "project": project,
        "detail": bool(detail),
        "generated_at": int(time.time()),
        "aggregate": agg_row,
    }
    if reports:
        sessions = []
        for r in reports:
            row = export.report_row_dict(r)
            if scrub:
                # Anonymize local-identifying fields: title (may contain a task
                # description), plus the on-disk path and cwd. Title becomes a
                # neutral "项目-会话ID" placeholder so the web still has a label.
                _scrub_identifying(row, project)
            if detail:
                # Attach the FULL turn-by-turn conversation (user input, assistant
                # replies, thinking, tool calls + their results) so the web session
                # view can replay it. ``user_messages`` is kept for backward
                # compatibility with the existing server/web fields.
                row["user_messages"] = list(r.usage.user_message_texts)
                row["conversation"] = _read_conversation(r)
            sessions.append(row)
        payload["sessions"] = sessions
    return payload


def _read_conversation(report: SessionReport) -> list[dict]:
    """Full ordered conversation for a session report (empty on any failure).

    A session may span several JSONL files (main + subagents); ``session_paths``
    holds them all, falling back to the single ``path``. Blocks are concatenated
    in file order.
    """
    from tcer.core import reader

    meta = report.meta
    paths = list(getattr(meta, "session_paths", ()) or ())
    if not paths and getattr(meta, "path", None) is not None:
        paths = [meta.path]
    convo: list[dict] = []
    for p in paths:
        try:
            convo.extend(reader.read_conversation(p))
        except Exception:  # noqa: BLE001 — a bad file must not abort the upload
            continue
    return convo


def _scrub_identifying(row: dict, project: str) -> None:
    """Redact local-identifying fields from a report row for anonymous uploads.

    Replaces ``title`` with a neutral ``项目-会话ID`` placeholder and clears the
    on-disk ``path`` / ``cwd``. Mutates ``row`` in place.
    """
    sid = row.get("session_id") or "session"
    row["title"] = f"{project}-{sid}"
    row["path"] = ""
    row["cwd"] = ""


def upload(server_url: str, token: str, payload: dict,
           timeout: float = 60.0) -> int:
    """POST the payload and return the number of inserted rows."""
    data = _post_json(f"{_base(server_url)}/api/upload", payload,
                      token=token, timeout=timeout)
    return int(data.get("inserted", 0))


def login_and_upload(
    *,
    server_url: str,
    username: str,
    password: str,
    aggregate: SessionReport,
    reports: list[SessionReport],
    n_sessions: int,
    project: str,
    user: str | None,
    anonymous: bool,
    detail: bool,
) -> int:
    """One-shot: login, build payload, upload. Returns inserted row count."""
    token = login(server_url, username, password)
    payload = build_payload(
        aggregate=aggregate, reports=reports, n_sessions=n_sessions,
        project=project, user=user, anonymous=anonymous, detail=detail,
    )
    return upload(server_url, token, payload)