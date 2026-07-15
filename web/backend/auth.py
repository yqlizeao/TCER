"""Minimal stateless bearer tokens (HMAC-signed), pure stdlib.

A token is ``base64url(payload).base64url(hmac_sha256(payload))`` where payload
is ``{"u": username, "exp": epoch_s}``. No server-side session store needed;
validity is checked by recomputing the HMAC and the expiry.

The signing secret comes from ``TCER_WEB_SECRET`` if set, else a random secret
generated at process start (tokens then invalidate on restart — fine for a
first-pass single-node deploy).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_SECRET = (os.environ.get("TCER_WEB_SECRET") or secrets.token_hex(32)).encode()
_TTL_SECONDS = 12 * 3600


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(username: str, ttl: int = _TTL_SECONDS) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + ttl}).encode()
    p = _b64e(payload)
    sig = _b64e(hmac.new(_SECRET, p.encode(), hashlib.sha256).digest())
    return f"{p}.{sig}"


def verify_token(token: str) -> str | None:
    """Return the username if the token is valid and unexpired, else None."""
    try:
        p, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64e(hmac.new(_SECRET, p.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(p))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload.get("u")


def bearer_from_header(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None