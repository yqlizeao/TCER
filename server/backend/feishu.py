"""Feishu (Lark) OAuth 2.0 login — opt-in networking, pure stdlib.

Configured entirely via environment; when unset the whole feature is dormant and
the server makes **zero** outbound calls (same opt-in posture as the upload
feature). Env:

    TCER_FEISHU_APP_ID       Feishu app id     (client_id)
    TCER_FEISHU_APP_SECRET   Feishu app secret (client_secret)
    TCER_FEISHU_REDIRECT_URI optional; the callback URL registered in the Feishu
                             console. If unset it is derived from the request
                             Host header as "<scheme>://<host>/api/auth/feishu/callback".
    TCER_LOGIN_MODE          password (default) | feishu | both

Flow (standard authorization-code OAuth 2.0):
    1. /api/auth/feishu/start   -> 302 to authorize_url (carries a signed state)
    2. Feishu -> /api/auth/feishu/callback?code=&state=
    3. exchange_code(code)      -> user_access_token
    4. fetch_user_info(token)   -> {open_id, name, avatar_url}

Endpoints follow Feishu's open-platform OAuth v2 (token) + authen v1 (user_info).
Network calls use urllib; failures raise FeishuError with a readable message.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Feishu 国内站默认域名；如需国际站(Lark)可改这几个基址。
_AUTH_BASE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_USERINFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

_TIMEOUT = 10  # seconds — bound the opt-in outbound calls


class FeishuError(Exception):
    """Any failure talking to Feishu (network / non-zero code / bad payload)."""


def app_id() -> str | None:
    return (os.environ.get("TCER_FEISHU_APP_ID") or "").strip() or None


def app_secret() -> str | None:
    return (os.environ.get("TCER_FEISHU_APP_SECRET") or "").strip() or None


def enabled() -> bool:
    """True only when both credentials are present (else fully dormant)."""
    return bool(app_id() and app_secret())


def login_mode() -> str:
    """Login-method switch: password (default) | feishu | both.

    ``feishu`` disables account/password login entirely, leaving only Feishu.
    """
    mode = (os.environ.get("TCER_LOGIN_MODE") or "password").strip().lower()
    return mode if mode in ("password", "feishu", "both") else "password"


def password_enabled() -> bool:
    return login_mode() in ("password", "both")


def feishu_login_enabled() -> bool:
    """Feishu offered to users only when configured AND allowed by the mode."""
    return enabled() and login_mode() in ("feishu", "both")


def redirect_uri(host: str | None) -> str:
    """The OAuth callback URL. Env override wins; else derived from Host."""
    override = (os.environ.get("TCER_FEISHU_REDIRECT_URI") or "").strip()
    if override:
        return override
    host = (host or "127.0.0.1:8890").strip()
    # localhost stays http; anything else assumed to be fronted by TLS.
    local = host.split(":")[0] in ("127.0.0.1", "localhost", "0.0.0.0")
    scheme = "http" if local else "https"
    return f"{scheme}://{host}/api/auth/feishu/callback"


def authorize_url(state: str, redirect: str) -> str:
    """Build the Feishu consent URL the browser is redirected to."""
    q = urllib.parse.urlencode({
        "client_id": app_id() or "",
        "redirect_uri": redirect,
        "response_type": "code",
        "state": state,
    })
    return f"{_AUTH_BASE}?{q}"


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:  # Feishu returns JSON error bodies too
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            raise FeishuError(f"HTTP {e.code} from {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FeishuError(f"网络请求失败：{e}")


def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            raise FeishuError(f"HTTP {e.code} from {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FeishuError(f"网络请求失败：{e}")


def exchange_code(code: str, redirect: str) -> str:
    """Trade an authorization ``code`` for a user_access_token."""
    data = _post_json(_TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": app_id() or "",
        "client_secret": app_secret() or "",
        "code": code,
        "redirect_uri": redirect,
    })
    token = data.get("access_token")
    if not token:
        # v2 puts the token at top level; guard against a nested/error shape.
        msg = (data.get("error_description") or data.get("error")
               or data.get("msg") or str(data))
        raise FeishuError(f"换取 token 失败：{msg}")
    return str(token)


def fetch_user_info(user_access_token: str) -> dict:
    """Fetch the logged-in user's profile. Returns {open_id, name, avatar_url}."""
    data = _get_json(_USERINFO_URL, {"Authorization": f"Bearer {user_access_token}"})
    if int(data.get("code", -1)) != 0:
        raise FeishuError(f"获取用户信息失败：{data.get('msg') or data}")
    d = data.get("data") or {}
    open_id = d.get("open_id") or d.get("union_id") or d.get("user_id")
    if not open_id:
        raise FeishuError("用户信息缺少 open_id")
    return {
        "open_id": str(open_id),
        "name": d.get("name") or d.get("en_name") or "飞书用户",
        "avatar_url": (d.get("avatar_url") or d.get("avatar_big")
                       or d.get("avatar_thumb") or ""),
    }