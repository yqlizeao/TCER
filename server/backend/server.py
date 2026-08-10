"""TCER server backend — pure-stdlib HTTP server.

Endpoints
---------
POST /api/login              {username, password}              -> {token}
GET  /api/config                                                -> {password_login, feishu_login}
GET  /api/me                 (Bearer)                           -> {username, name, avatar_url, kind}
GET  /api/auth/feishu/start                                     -> 302 Feishu consent
GET  /api/auth/feishu/callback ?code=&state=                    -> 302 / (token in fragment)
POST /api/upload             (Bearer) upload payload            -> {inserted}
GET  /api/filters            (Bearer)                           -> {persons, projects, models}
GET  /api/overview           (Bearer) ?metric=&...              -> {totals, series}
GET  /api/detail             (Bearer) ?dimension=&...           -> {dimension, rows}
GET  /api/aliases            (Bearer) ?kind=project|model|person -> {aliases}
POST /api/aliases            (Bearer) {kind, raw, canonical}    -> {ok}
GET  /api/sessions           (Bearer) ?filters...               -> {sessions, total}
GET  /api/session            (Bearer) ?id=                      -> {session detail}
GET  /api/dimensions         (Bearer)                           -> {dimensions, metrics}
GET  /api/compare            (Bearer) ?dimension=&metric=&...   -> {cohorts, caveat}
GET  /api/insights           (Bearer) ?filters...               -> {findings, coverage}
GET  /api/tokens             (login)  list caller's API tokens   -> {tokens}
POST /api/tokens             (login)  {label} mint API token      -> {token}
DELETE /api/tokens           (login)  ?id= revoke API token       -> {ok}
GET  /api/health                                                -> {ok:true}

Static frontend is served from ``../frontend`` for any non-/api path.

Run:
    python -m server.backend.server         # from repo root
    python server/backend/server.py         # direct
Env:
    TCER_SERVER_HOST (default 127.0.0.1)
    TCER_SERVER_PORT (default 8890)
    TCER_SERVER_SECRET  (token signing key; random if unset)
    TCER_SERVER_DB      (sqlite path)
    TCER_LOGIN_MODE     (password | feishu | both; default password)
    TCER_FEISHU_APP_ID / TCER_FEISHU_APP_SECRET / TCER_FEISHU_REDIRECT_URI

Auth: an ``Authorization: Bearer <token>`` may carry either a short-lived login
token (issued by /api/login) or a long-lived API token (minted from the web UI,
stored hashed). Token management endpoints require a *login* token specifically.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

# Allow running as a script (python server/backend/server.py) or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import feishu  # noqa: E402
import diagnosis  # noqa: E402
import insights  # noqa: E402
import personas  # noqa: E402
import projectview  # noqa: E402

_FRONTEND_DIR = (Path(__file__).resolve().parent.parent / "frontend").resolve()
_MAX_BODY = 64 * 1024 * 1024  # 64 MiB upload cap

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TCERServer/0.2"

    # -- helpers ----------------------------------------------------------- #
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        if not token:
            return None
        # A bearer may be either a short-lived login token (HMAC) or a long-lived
        # API token minted from the web UI. Try the session token first, then fall
        # back to the API-token table so uploads can authenticate with either.
        user = auth.verify_token(token)
        if user:
            return user
        return db.verify_api_token(token)

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
        elif route == "/api/tokens":
            self._h_create_token()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/tokens":
            self._h_delete_token(parse_qs(urlparse(self.path).query))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        if route == "/api/health":
            self._send_json({"ok": True})
        elif route == "/api/config":
            self._h_config()
        elif route == "/api/me":
            self._guard(self._h_me)
        elif route == "/api/auth/feishu/start":
            self._h_feishu_start()
        elif route == "/api/auth/feishu/callback":
            self._h_feishu_callback(qs)
        elif route == "/api/filters":
            self._guard(self._h_filters)
        elif route == "/api/overview":
            self._guard(lambda: self._h_overview(qs))
        elif route == "/api/exec":
            self._guard(lambda: self._h_exec(qs))
        elif route == "/api/engineering":
            self._guard(lambda: self._h_engineering(qs))
        elif route == "/api/diagnosis":
            self._guard(lambda: self._h_diagnosis(qs))
        elif route == "/api/projects":
            self._guard(lambda: self._h_projects(qs))
        elif route == "/api/detail":
            self._guard(lambda: self._h_detail(qs))
        elif route == "/api/aliases":
            self._guard(lambda: self._h_get_aliases(qs))
        elif route == "/api/sessions":
            self._guard(lambda: self._h_sessions(qs))
        elif route == "/api/session":
            self._guard(lambda: self._h_session(qs))
        elif route == "/api/dimensions":
            self._guard(self._h_dimensions)
        elif route == "/api/compare":
            self._guard(lambda: self._h_compare(qs))
        elif route == "/api/insights":
            self._guard(lambda: self._h_insights(qs))
        elif route == "/api/tokens":
            self._guard(self._h_list_tokens)
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
        if not feishu.password_enabled():
            self._send_json({"error": "账号密码登录已关闭，请使用飞书登录"}, 403)
            return
        data = self._read_json()
        if not data or "username" not in data or "password" not in data:
            self._send_json({"error": "username and password required"}, 400)
            return
        if db.verify_user(str(data["username"]), str(data["password"])):
            self._send_json({"token": auth.issue_token(str(data["username"]))})
        else:
            self._send_json({"error": "invalid credentials"}, 401)

    # -- login config / identity ------------------------------------------- #
    def _h_config(self) -> None:
        """Public (no-auth) login-page config: which methods are offered."""
        self._send_json({
            "password_login": feishu.password_enabled(),
            "feishu_login": feishu.feishu_login_enabled(),
        })

    def _h_me(self) -> None:
        """Identity of the current bearer: name + avatar for the sidebar.

        Feishu users carry a real name + avatar URL; password users have no
        avatar, so the client renders their name's initial instead (avatar="").
        """
        user = self._auth_user() or ""
        if user.startswith("feishu:"):
            prof = db.get_feishu_user(user[len("feishu:"):])
            if prof:
                self._send_json({"username": user, "name": prof["name"],
                                 "avatar_url": prof["avatar_url"], "kind": "feishu"})
                return
        self._send_json({"username": user, "name": user,
                         "avatar_url": "", "kind": "password"})

    # -- Feishu OAuth ------------------------------------------------------- #
    def _h_feishu_start(self) -> None:
        if not feishu.feishu_login_enabled():
            self._send_json({"error": "飞书登录未启用"}, 404)
            return
        # A short-lived signed token doubles as the OAuth ``state`` (CSRF guard):
        # the callback re-verifies it, so no server-side state store is needed.
        state = auth.issue_token("feishu-oauth-state", ttl=600)
        redirect = feishu.redirect_uri(self.headers.get("Host"))
        self._redirect(feishu.authorize_url(state, redirect))

    def _h_feishu_callback(self, qs: dict) -> None:
        if not feishu.feishu_login_enabled():
            self._send_json({"error": "飞书登录未启用"}, 404)
            return
        code = self._one(qs, "code")
        state = self._one(qs, "state")
        # Verify the state we signed in _h_feishu_start (CSRF / replay guard).
        if not state or auth.verify_token(state) != "feishu-oauth-state":
            self._redirect("/?feishu_error=" + quote("登录校验失败，请重试"))
            return
        if not code:
            self._redirect("/?feishu_error=" + quote("未收到授权码"))
            return
        redirect = feishu.redirect_uri(self.headers.get("Host"))
        try:
            tok = feishu.exchange_code(str(code), redirect)
            info = feishu.fetch_user_info(tok)
        except feishu.FeishuError as e:
            self._redirect("/?feishu_error=" + quote(str(e)))
            return
        db.upsert_feishu_user(info["open_id"], info["name"], info["avatar_url"])
        username = db.feishu_username(info["open_id"])
        login_token = auth.issue_token(username)
        # Hand the token back to the SPA via the URL fragment (never sent to the
        # server / logs); the front-end reads it on load and stores it.
        frag = urlencode({"token": login_token, "name": info["name"]})
        self._redirect(f"/#feishu={frag}")

    def _h_upload(self) -> None:
        # Read the body first so we can decide auth from the payload: anonymous
        # uploads are accepted WITHOUT a bearer token (no account/password), so
        # the client never has to call /api/login. Non-anonymous uploads still
        # require a valid token.
        data = self._read_json()
        if data is None:
            self._send_json({"error": "invalid or too-large body"}, 400)
            return
        user = self._auth_user()
        anonymous = bool(data.get("anonymous"))
        if not user and not anonymous:
            self._send_json({"error": "unauthorized"}, 401)
            return
        # For anonymous uploads the client sends a stable pseudonym in ``user``
        # (e.g. "匿名-ab12cd34") so one user's anonymized rows still group under a
        # single person instead of collapsing into 未标注. Honor whatever the
        # client sent; fall back to the login name for non-anonymous uploads.
        person = data.get("user") or (None if anonymous else user)
        project = data.get("project")
        aggregate = data.get("aggregate")
        # Per-session rows are always stored when present so each session lands as
        # its own row (time axis + session-id dedup). ``detail`` only signals that
        # each session row additionally carries the turn-by-turn conversation; it
        # no longer decides whether sessions are stored at all.
        sessions = data.get("sessions")
        generated_at = data.get("generated_at")
        try:
            n = db.insert_records(
                uploaded_by=user or "anonymous", person=person, project=project,
                aggregate=aggregate, sessions=sessions,
                generated_at=int(generated_at) if generated_at else None,
            )
        except Exception as e:  # malformed payload shouldn't crash the server
            self._send_json({"error": f"insert failed: {e}"}, 400)
            return
        self._send_json({"inserted": n})

    def _h_filters(self) -> None:
        self._send_json(db.distinct_values())

    # -- API tokens --------------------------------------------------------- #
    def _session_user(self) -> str | None:
        """Username from a *login* (HMAC) token only — NOT an API token.

        Token management must be done from an interactive web session; allowing
        an API token to mint more API tokens would be a privilege-escalation
        path with no expiry.
        """
        token = auth.bearer_from_header(self.headers.get("Authorization"))
        return auth.verify_token(token) if token else None

    def _h_list_tokens(self) -> None:
        user = self._session_user()
        if not user:
            self._send_json({"error": "unauthorized"}, 401)
            return
        self._send_json({"tokens": db.list_api_tokens(user)})

    def _h_create_token(self) -> None:
        user = self._session_user()
        if not user:
            self._send_json({"error": "unauthorized"}, 401)
            return
        data = self._read_json() or {}
        raw = db.create_api_token(user, str(data.get("label") or "") or None)
        # The raw token is returned exactly once — it is never stored or
        # recoverable afterwards (only its hash is persisted).
        self._send_json({"token": raw})

    def _h_delete_token(self, qs: dict) -> None:
        user = self._session_user()
        if not user:
            self._send_json({"error": "unauthorized"}, 401)
            return
        tid = self._one(qs, "id")
        try:
            ok = db.delete_api_token(user, int(tid))
        except (TypeError, ValueError):
            self._send_json({"error": "invalid id"}, 400)
            return
        self._send_json({"ok": ok})

    # -- Decision Lab ------------------------------------------------------- #
    def _h_dimensions(self) -> None:
        """Comparable knobs + metrics, so the UI never hardcodes the list."""
        self._send_json({
            "dimensions": [
                {"key": d.key, "label": d.label, "multi": d.multi, "hint": d.hint}
                for d in analysis.DIMENSIONS.values()
            ],
            "metrics": [
                {"key": m.key, "label": m.label, "fmt": m.fmt,
                 "higher_is_better": m.higher_is_better,
                 "guardrail": m.guardrail, "hint": m.hint}
                for m in analysis.METRICS.values()
            ],
            "min_sessions": analysis.MIN_COHORT_SESSIONS,
        })

    def _h_compare(self, qs: dict) -> None:
        f = self._common_filters(qs)
        dimension = self._one(qs, "dimension", "model")
        metric = self._one(qs, "metric", analysis.PRIMARY_METRIC)
        rows = db.fetch_analysis_rows(**f)
        try:
            self._send_json(analysis.compare(rows, dimension, metric))
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)

    def _h_insights(self, qs: dict) -> None:
        rows = db.fetch_analysis_rows(**self._common_filters(qs))
        self._send_json(insights.generate(rows))

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

    def _h_exec(self, qs: dict) -> None:
        self._send_json(personas.executive(**self._common_filters(qs)))

    def _h_engineering(self, qs: dict) -> None:
        self._send_json(personas.engineering(**self._common_filters(qs)))

    def _h_diagnosis(self, qs: dict) -> None:
        self._send_json(diagnosis.diagnose(**self._common_filters(qs)))

    def _h_projects(self, qs: dict) -> None:
        self._send_json(projectview.project_board(**self._common_filters(qs)))

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
        sys.stderr.write("[tcer-server] created default user admin/admin — change it!\n")
    host = os.environ.get("TCER_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("TCER_SERVER_PORT", "8890"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"[tcer-server] serving on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()