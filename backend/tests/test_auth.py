"""Tests for optional single-password auth (pure logic + service)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import auth
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


# -- pure ------------------------------------------------------------------

def test_hash_verify_password():
    salt, h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", salt, h)
    assert not auth.verify_password("wrong", salt, h)
    assert not auth.verify_password("", salt, h)


def test_token_make_validate():
    secret = auth.new_secret()
    tok = auth.make_token(secret, now=1000.0)
    assert auth.valid_token(tok, secret, now=1000.0)
    assert not auth.valid_token(tok, "other-secret", now=1000.0)
    assert not auth.valid_token(tok + "x", secret, now=1000.0)      # tampered
    assert not auth.valid_token(tok, secret, now=1000.0 + 31 * 86400)  # expired
    assert not auth.valid_token("", secret)
    assert not auth.valid_token("a.b.c", secret)


# -- service ---------------------------------------------------------------

def _svc(tmp):
    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc.auth_config_path = os.path.join(tmp, "auth.json")
    return svc


def test_service_auth_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _svc(tmp)
        # Off by default: everything is "valid", login is a no-op pass.
        assert svc.auth_status() == {"enabled": False}
        assert svc.auth_valid(None) is True
        assert svc.auth_login("anything")["ok"] is True

        # Enable via set_password (returns an auto-login token).
        r = svc.auth_set_password("secret")
        assert r["ok"] and r["token"]
        assert svc.auth_status() == {"enabled": True}
        assert svc.auth_valid(r["token"]) is True
        assert svc.auth_valid("bogus") is False
        assert svc.auth_valid(None) is False

        # Login.
        assert svc.auth_login("secret")["ok"] is True
        assert svc.auth_login("nope")["ok"] is False

        # Short password rejected.
        assert svc.auth_set_password("ab")["ok"] is False
        # Change requires the current password.
        assert svc.auth_set_password("newpass", current_password="wrong")["ok"] is False
        assert svc.auth_set_password("newpass", current_password="secret")["ok"] is True
        assert svc.auth_login("newpass")["ok"] is True

        # Disable requires the current password.
        assert svc.auth_disable(current_password="wrong")["ok"] is False
        assert svc.auth_disable(current_password="newpass")["ok"] is True
        assert svc.auth_status() == {"enabled": False}

        # The on-disk file is chmod 600 (holds the hash + secret).
        svc.auth_set_password("secret2")
        assert oct(os.stat(svc.auth_config_path).st_mode & 0o777) == "0o600"
