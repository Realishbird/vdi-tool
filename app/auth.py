"""Optional login for the GUI.

The tool is open until an admin password is set in the GUI. After that every page and API call
needs a session cookie from the login page, or the API key for scripts. Desktops are never asked
to log in: the first-boot config, the workload downloads and the result reports stay open, because
the desktops have no credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time

from .deploy import STATE_DIR

FILE = STATE_DIR / "auth.json"
KEY_FILE = STATE_DIR / "api-key"
SESSION_DAYS = 14
COOKIE = "vdi_session"

# Paths the desktops and the local metrics scraper use.
_OPEN = re.compile(r"^/(ci/|agent/|static/|login$|favicon\.ico$|api/(login|auth)$|api/runs/[^/]+/(go$|report/))")

_fails: dict[str, list[float]] = {}


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(FILE)


def enabled() -> bool:
    return bool(_load().get("hash"))


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    # A new signing secret ends every existing session.
    _save({"salt": salt.hex(), "hash": _hash(password, salt), "secret": secrets.token_hex(32)})
    api_key()


def disable() -> None:
    FILE.unlink(missing_ok=True)


def check_password(password: str) -> bool:
    d = _load()
    if not d.get("hash"):
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"])), d["hash"])


def throttled(client: str) -> bool:
    """Five wrong passwords from one address lock it out for a minute."""
    recent = [t for t in _fails.get(client, []) if time.time() - t < 60]
    _fails[client] = recent
    return len(recent) >= 5


def failed(client: str) -> None:
    _fails.setdefault(client, []).append(time.time())


def _sign(expires: str) -> str:
    return hmac.new(bytes.fromhex(_load().get("secret", "00")), expires.encode(), hashlib.sha256).hexdigest()


def new_session() -> str:
    """A signed expiry time, so sessions survive a restart or update of the tool."""
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(expires)}"


def valid_session(token: str | None) -> bool:
    expires, _, sig = (token or "").partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(expires))


def api_key() -> str:
    """A key for scripts, readable only by root inside the container."""
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        key = secrets.token_urlsafe(32)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key + "\n")
        os.chmod(KEY_FILE, 0o600)
        return key


def allowed(path: str, cookie: str | None, authorization: str | None, client: str | None) -> bool:
    if not enabled() or _OPEN.match(path):
        return True
    if path == "/metrics" and client in ("127.0.0.1", "::1"):
        return True
    if valid_session(cookie):
        return True
    if authorization and authorization.startswith("Bearer "):
        return hmac.compare_digest(authorization[7:].strip(), api_key())
    return False
