"""TCER web backend — pure-stdlib HTTP server.

Endpoints
---------
POST /api/login              {username, password}              -> {token}
POST /api/upload             (Bearer) upload payload            -> {inserted}
GET  /api/filters            (Bearer)                           -> {persons, projects, models}
GET  /api/overview           (Bearer) ?metric=&...              -> {totals, series}
GET  /api/detail             (Bearer) ?dimension=&...           -> {dimension, rows}
GET  /api/aliases            (Bearer) ?kind=project|model|person -> {aliases}
POST /api/aliases            (Bearer) {kind, raw, canonical}    -> {ok}
GET  /api/sessions           (Bearer) ?filters...               -> {sessions, total}
GET  /api/session            (Bearer) ?id=                      -> {session detail}
GET  /api/health                                                -> {ok:true}

Static frontend is served from ``../frontend`` for any non-/api path.

Run:
    python -m web.backend.server            # from repo root
    python web/backend/server.py            # direct
Env:
    TCER_WEB_HOST (default 127.0.0.1)
    TCER_WEB_PORT (default 8899)
    TCER_WEB_SECRET  (token signing key; random if unset)
    TCER_WEB_DB      (sqlite path)
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Allow running as a script (python web/backend/server.py) or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import db  # noqa: E402

_FRONTEND_DIR = (Path(__file__).resolve().parent.parent / "frontend").resolve()
_MAX_BODY = 64 * 1024 * 1024  # 64 MiB upload cap

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TCERWeb/0.2"

    # -- helpers ----------------------------------------------------------- #
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return None

    def _auth_user(self) -> str | None:
        token = auth.bearer_from_header(self.headers.get("Authorization"))
        return auth.verify_token(token) if token else None

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- routing ----------------------------------------------------------- #
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/login":
            self._h_login()
        elif route == "/api/upload":
            self._h_upload()
        elif route == "/api/aliases":
            self._h_set_alias()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        if route == "/api/health":
            self._send_json({"ok": True})
        elif route == "/api/filters":
            self._guard(self._h_filters)
        elif route == "/api/overview":
            self._guard(lambda: self._h_overview(qs))
        elif route == "/api/detail":
            self._guard(lambda: self._h_detail(qs))
        elif route == "/api/aliases":
            self._guard(lambda: self._h_get_aliases(qs))
        elif route == "/api/sessions":
            self._guard(lambda: self._h_sessions(qs))
        elif route == "/api/session":
            self._guard(lambda: self._h_session(qs))
        elif route.startswith("/api/"):
            self._send_json({"error": "not found"}, 404)
        else:
            self._serve_static(route)

    def _guard(self, fn) -> None:
        """Run an authenticated handler, 401-ing if no valid token."""
        if not self._auth_user():
            self._send_json({"error": "unauthorized"}, 401)
            return
        fn()

    # -- query-string parsing --------------------------------------------- #
    @staticmethod
    def _multi(qs: dict, key: str) -> list[str] | None:
        vals = qs.get(key)
        if not vals:
            return None
        out: list[str] = []
        for v in vals:
            out.extend(x for x in v.split(",") if x)
        return out or None

    @staticmethod
    def _one(qs: dict, key: str, default=None):
        return qs.get(key, [default])[0]

    def _common_filters(self, qs: dict) -> dict:
        start = self._one(qs, "start")
        end = self._one(qs, "end")
        return {
            "persons": self._multi(qs, "persons"),
            "projects": self._multi(qs, "projects"),
            "models": self._multi(qs, "models"),
            "start_ts": int(start) if start else None,
            "end_ts": int(end) if end else None,
        }

    # -- handlers ---------------------------------------------------------- #
    def _h_login(self) -> None:
        data = self._read_json()
        if not data or "username" not in data or "password" not in data:
            self._send_json({"error": "username and password required"}, 400)
            return
        if db.verify_user(str(data["username"]), str(data["password"])):
            self._send_json({"token": auth.issue_token(str(data["username"]))})
        else:
            self._send_json({"error": "invalid credentials"}, 401)

    def _h_upload(self) -> None:
        user = self._auth_user()
        if not user:
            self._send_json({"error": "unauthorized"}, 401)
            return
        data = self._read_json()
        if data is None:
            self._send_json({"error": "invalid or too-large body"}, 400)
            return
        anonymous = bool(data.get("anonymous"))
        person = None if anonymous else (data.get("user") or user)
        project = data.get("project")
        aggregate = data.get("aggregate")
        sessions = data.get("sessions") if data.get("detail") else None
        generated_at = data.get("generated_at")
        try:
            n = db.insert_records(
                uploaded_by=user, person=person, project=project,
                aggregate=aggregate, sessions=sessions,
                generated_at=int(generated_at) if generated_at else None,
            )
        except Exception as e:  # malformed payload shouldn't crash the server
            self._send_json({"error": f"insert failed: {e}"}, 400)
            return
        self._send_json({"inserted": n})

    def _h_filters(self) -> None:
        self._send_json(db.distinct_values())

    def _h_overview(self, qs: dict) -> None:
        f = self._common_filters(qs)
        metric = self._one(qs, "metric", "tcer")
        try:
            self._send_json(db.overview(metric=metric, **f))
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _h_detail(self, qs: dict) -> None:
        f = self._common_filters(qs)
        dimension = self._one(qs, "dimension", "project")
        try:
            self._send_json(db.aggregate_by(dimension=dimension, **f))
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _h_get_aliases(self, qs: dict) -> None:
        kind = self._one(qs, "kind", "project")
        try:
            self._send_json({"kind": kind, "aliases": db.get_aliases(kind)})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _h_set_alias(self) -> None:
        if not self._auth_user():
            self._send_json({"error": "unauthorized"}, 401)
            return
        data = self._read_json()
        if not data or "kind" not in data or "raw" not in data:
            self._send_json({"error": "kind and raw required"}, 400)
            return
        try:
            db.set_alias(str(data["kind"]), str(data["raw"]),
                         data.get("canonical"))
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        self._send_json({"ok": True})

    def _h_sessions(self, qs: dict) -> None:
        f = self._common_filters(qs)
        self._send_json(db.sessions_list(**f))

    def _h_session(self, qs: dict) -> None:
        sid = self._one(qs, "id")
        if not sid:
            self._send_json({"error": "id required"}, 400)
            return
        try:
            detail = db.session_detail(int(sid))
        except (TypeError, ValueError):
            self._send_json({"error": "invalid id"}, 400)
            return
        if detail is None:
            self._send_json({"error": "not found"}, 404)
            return
        self._send_json(detail)

    # -- static ------------------------------------------------------------ #
    def _serve_static(self, route: str) -> None:
        rel = route.lstrip("/") or "index.html"
        target = (_FRONTEND_DIR / rel).resolve()
        if not str(target).startswith(str(_FRONTEND_DIR)) or not target.is_file():
            # SPA-ish fallback to index.html
            target = _FRONTEND_DIR / "index.html"
            if not target.is_file():
                self._send_json({"error": "not found"}, 404)
                return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    db.init_db()
    # Bootstrap a default admin/admin account on an empty DB for first-run.
    if db.user_count() == 0:
        db.create_user("admin", "admin")
        sys.stderr.write("[tcer-web] created default user admin/admin — change it!\n")
    host = os.environ.get("TCER_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("TCER_WEB_PORT", "8899"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"[tcer-web] serving on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()