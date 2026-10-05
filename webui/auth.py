"""Single-user login gate for the Control Center.

Enabled when WEBUI_PASSWORD_HASH is set (see ``python -m webui.auth set-password``).
Pure-ASGI middleware, so it also covers the /ws WebSocket. Sessions are HMAC-signed
cookies; no extra dependencies.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

SESSION_TTL = 12 * 3600
COOKIE = "ta_session"
PUBLIC = {"/login", "/api/login"}
_fails: dict[str, list[float]] = {}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


def _secret() -> bytes:
    s = os.environ.get("WEBUI_SECRET")
    if s:
        return s.encode()
    # fall back to a key derived from the password hash: rotating the password logs everyone out
    return hashlib.sha256(b"ta-session|" + os.environ.get("WEBUI_PASSWORD_HASH", "").encode()).digest()


def make_token(user: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": user, "exp": int(time.time()) + SESSION_TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def check_token(tok: str | None) -> bool:
    try:
        body, sig = (tok or "").rsplit(".", 1)
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        d = json.loads(base64.urlsafe_b64decode(body))
        return d["u"] == os.environ.get("WEBUI_USER", "admin") and d["exp"] > time.time()
    except Exception:
        return False


def enabled() -> bool:
    return bool(os.environ.get("WEBUI_PASSWORD_HASH"))


def _cookies(headers) -> dict:
    out = {}
    for k, v in headers:
        if k == b"cookie":
            for part in v.decode("latin1").split(";"):
                if "=" in part:
                    a, b = part.strip().split("=", 1)
                    out[a] = b
    return out


def _same_origin(headers) -> bool:
    h = {k.decode("latin1"): v.decode("latin1") for k, v in headers}
    origin = h.get("origin")
    if not origin:
        return True  # non-browser clients; the session cookie is still required
    return urlsplit(origin).netloc == h.get("host", "")


def _locked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
    return len(_fails[ip]) >= 5


class AuthMiddleware:
    def __init__(self, app, static_dir: Path):
        self.app = app
        self.login_html = (static_dir / "login.html").read_bytes()

    async def _send(self, send, status, body=b"", ctype="text/plain", extra=()):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"same-origin"), *extra]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not enabled():
            return await self.app(scope, receive, send)
        headers = scope["headers"]
        authed = check_token(_cookies(headers).get(COOKIE))

        if scope["type"] == "websocket":
            if not authed or not _same_origin(headers):
                await receive()
                return await send({"type": "websocket.close", "code": 1008})
            return await self.app(scope, receive, send)

        path, method = scope["path"], scope["method"]
        secure = dict(headers).get(b"x-forwarded-proto", b"").decode() == "https"
        if method not in ("GET", "HEAD") and not _same_origin(headers):
            return await self._send(send, 403, b"cross-origin request refused")

        if path == "/login" and method == "GET":
            return await self._send(send, 200, self.login_html, "text/html; charset=utf-8")

        if path == "/api/login" and method == "POST":
            ip = (dict(headers).get(b"cf-connecting-ip") or (scope.get("client") or ("?",))[0] or b"?")
            ip = ip.decode() if isinstance(ip, bytes) else ip
            if _locked(ip):
                return await self._send(send, 429, b"too many attempts, try again in 15 minutes")
            raw = b""
            while True:
                m = await receive()
                raw += m.get("body", b"")
                if not m.get("more_body"):
                    break
            try:
                d = json.loads(raw[:4096])
            except Exception:
                d = {}
            ok = (hmac.compare_digest(str(d.get("username", "")), os.environ.get("WEBUI_USER", "admin"))
                  & verify_password(str(d.get("password", "")), os.environ["WEBUI_PASSWORD_HASH"]))
            if not ok:
                _fails.setdefault(ip, []).append(time.time())
                return await self._send(send, 401, b"invalid credentials")
            _fails.pop(ip, None)
            cookie = f"{COOKIE}={make_token(os.environ.get('WEBUI_USER', 'admin'))}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
            if secure:
                cookie += "; Secure"
            return await self._send(send, 200, b"ok", extra=[(b"set-cookie", cookie.encode())])

        if path == "/api/logout" and method == "POST":
            return await self._send(send, 200, b"ok", extra=[(
                b"set-cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0".encode())])

        if not authed:
            if path.startswith("/api/"):
                return await self._send(send, 401, b"login required")
            return await self._send(send, 302, extra=[(b"location", b"/login")])
        return await self.app(scope, receive, send)


def _set_password():
    from dotenv import find_dotenv, set_key
    env = find_dotenv(usecwd=True) or str(Path(__file__).resolve().parent.parent / ".env")
    Path(env).touch(exist_ok=True)
    user = input("Username [admin]: ").strip() or "admin"
    pw = getpass.getpass("New password (min 12 chars): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        sys.exit("Password too short or mismatch.")
    set_key(env, "WEBUI_USER", user, quote_mode="never")
    set_key(env, "WEBUI_PASSWORD_HASH", hash_password(pw), quote_mode="never")
    print(f"Saved to {env}. Restart the server to apply.")


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        _set_password()
    else:
        sys.exit("usage: python -m webui.auth set-password")
