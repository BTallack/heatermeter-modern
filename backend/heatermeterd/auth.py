"""Optional single-password auth for the web UI / API (pure, testable).

Design for a single-user local appliance: one shared password, hashed with
PBKDF2. Login returns a stateless HMAC-signed bearer token (carrying an expiry)
so it survives a daemon restart without server-side session storage. Auth is
OFF by default; when off, everything here is bypassed by the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_PBKDF2_ROUNDS = 200_000
TOKEN_TTL_DAYS = 30


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for *password*."""
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    return salt, h


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not (password and salt and expected_hash):
        return False
    try:
        _, h = hash_password(password, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(h, expected_hash)


def make_token(secret: str, ttl_days: int = TOKEN_TTL_DAYS, now: float | None = None) -> str:
    """Issue an HMAC-signed bearer token of the form ``<exp>.<nonce>.<sig>``."""
    now = time.time() if now is None else now
    exp = int(now + ttl_days * 86400)
    nonce = secrets.token_hex(8)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def valid_token(token: str, secret: str, now: float | None = None) -> bool:
    if not token or not secret:
        return False
    try:
        exp_s, nonce, sig = token.split(".")
        msg = f"{exp_s}.{nonce}"
    except ValueError:
        return False
    expected = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        return int(exp_s) > (time.time() if now is None else now)
    except ValueError:
        return False


def new_secret() -> str:
    return secrets.token_hex(32)
