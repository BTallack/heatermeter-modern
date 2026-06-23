"""Apple Push Notification (APNs) support for a native iOS HeaterMeter app.

This is what lets a Swift app receive away-from-home alerts and drive a Live
Activity (lock-screen / Dynamic Island cook view) the same way ``notify.py``
feeds ntfy. It is split into two layers with very different testability:

* **Pure** (stdlib only, unit-tested below + in test_apns.py): the device-token
  registry, the unsigned JWT (header + claims) for APNs token auth, mapping a
  HeaterMeter alert into an ``aps`` payload, and building a Live Activity
  content-state update.
* **Adapter** (optional deps, network): signing that JWT with the team's ``.p8``
  ES256 key and POSTing over HTTP/2 to Apple. APNs *requires* HTTP/2 and ES256,
  neither of which the Python stdlib provides, so :class:`ApnsSender` lazily
  imports ``cryptography`` + ``httpx[http2]`` and degrades to a clear "not
  available" state when they are absent. Keeping the decision logic pure means
  the daemon imports and is fully testable with no extra deps and no network.

Install the sender deps on the Pi with ``pip install "heatermeterd[ios]"``
(extras: cryptography, httpx[http2]); without them the daemon still runs and the
push config simply reports unavailable.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

# Production vs. development APNs endpoints. A debug build registers a sandbox
# token; a TestFlight/App Store build registers a production token.
HOST_PROD = "https://api.push.apple.com"
HOST_SANDBOX = "https://api.sandbox.push.apple.com"

# Cap stored device tokens so a misbehaving client can't grow the file forever.
MAX_TOKENS = 20

# An APNs auth JWT is valid 20-60 min; refresh well inside that.
JWT_TTL_SECONDS = 50 * 60


def default_config() -> dict:
    return {
        "enabled": False,
        "team_id": "",        # Apple Developer Team ID (10 chars)
        "key_id": "",         # the .p8 auth key's Key ID (10 chars)
        "key_path": "",       # path to AuthKey_<KEYID>.p8 (root-readable)
        "bundle_id": "",      # the app's bundle id -> APNs topic
        "sandbox": False,     # True for debug builds (sandbox APNs host)
        "tokens": [],         # registry: [{token, platform, added_ts}]
    }


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over defaults, coercing types. Tokens are validated by the
    registry, not here, so a malformed list cannot break loading."""
    d = default_config()
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        if "sandbox" in cfg:
            d["sandbox"] = bool(cfg["sandbox"])
        for k in ("team_id", "key_id", "key_path", "bundle_id"):
            if cfg.get(k) is not None:
                d[k] = str(cfg[k]).strip()
        d["tokens"] = PushRegistry(cfg.get("tokens")).to_list()
    return d


def credentials_complete(cfg: dict) -> bool:
    """True when every operator-supplied APNs credential is present (the device
    can still have registered zero tokens)."""
    return all(cfg.get(k) for k in ("team_id", "key_id", "key_path", "bundle_id"))


# -- device-token registry (pure) -----------------------------------------

class PushRegistry:
    """The set of device tokens an app has registered, newest last. Pure list
    bookkeeping: dedupe by token, refresh metadata on re-register, and cap the
    total so the persisted file stays bounded."""

    def __init__(self, tokens=None, max_tokens: int = MAX_TOKENS) -> None:
        self.max_tokens = max_tokens
        self.tokens: list[dict] = []
        for t in (tokens or []):
            if isinstance(t, dict) and t.get("token"):
                self.tokens.append({
                    "token": str(t["token"]),
                    "platform": str(t.get("platform") or "ios"),
                    "added_ts": float(t.get("added_ts") or 0.0),
                })

    def register(self, token: str, platform: str = "ios",
                 now: float = 0.0) -> bool:
        """Add or refresh *token*. Returns True if anything changed. Re-pushes
        the token to the end (most-recent) and evicts the oldest past the cap."""
        token = (token or "").strip()
        if not token:
            return False
        self.tokens = [t for t in self.tokens if t["token"] != token]
        self.tokens.append({"token": token,
                            "platform": (platform or "ios"),
                            "added_ts": float(now)})
        if len(self.tokens) > self.max_tokens:
            self.tokens = self.tokens[-self.max_tokens:]
        return True

    def remove(self, token: str) -> bool:
        """Drop *token* (e.g. APNs reported it 410 Unregistered). Returns True
        if it was present."""
        before = len(self.tokens)
        self.tokens = [t for t in self.tokens if t["token"] != token]
        return len(self.tokens) != before

    def all_tokens(self) -> list[str]:
        return [t["token"] for t in self.tokens]

    def to_list(self) -> list[dict]:
        return [dict(t) for t in self.tokens]


# -- JWT + payload building (pure) ----------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_signing_input(key_id: str, team_id: str, now: float) -> str:
    """The APNs provider-token JWT *signing input* (``header.claims``), base64url
    encoded and ready to be ES256-signed. Pure so it is unit-testable; the
    actual signature is appended by :class:`ApnsSender`. Header alg=ES256 +
    kid=<key_id>; claims iss=<team_id> + iat=now."""
    header = {"alg": "ES256", "kid": str(key_id)}
    claims = {"iss": str(team_id), "iat": int(now)}
    seg = _b64url(json.dumps(header, separators=(",", ":")).encode())
    seg += "." + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    return seg


# ntfy-style priority -> APNs delivery priority header + interruption level.
_PRIORITY_HEADER = {"min": 5, "low": 5, "default": 10, "high": 10,
                    "urgent": 10, "max": 10}
_INTERRUPTION = {"min": "passive", "low": "passive", "default": "active",
                 "high": "time-sensitive", "urgent": "time-sensitive",
                 "max": "time-sensitive"}


def priority_header(priority: str) -> int:
    """APNs ``apns-priority`` header value for an ntfy-style priority string."""
    return _PRIORITY_HEADER.get(priority, 10)


def alert_payload(title: str, body: str, *, priority: str = "default",
                  thread_id: str = "heatermeter",
                  custom: Optional[dict] = None) -> dict:
    """Build the APNs JSON for a standard alert from the same (title, message,
    priority) a ``_push`` call carries. ``thread-id`` groups HeaterMeter
    notifications together in Notification Center."""
    aps = {
        "alert": {"title": title, "body": body},
        "sound": "default",
        "thread-id": thread_id,
        "interruption-level": _INTERRUPTION.get(priority, "active"),
    }
    payload = {"aps": aps}
    if custom:
        payload.update(custom)
    return payload


def liveactivity_content_state(status: dict,
                               predicted_done_by: Optional[dict] = None) -> dict:
    """The ActivityKit ``content-state`` for the live cook view: what the Swift
    ``ActivityAttributes.ContentState`` decodes. Only the fields a glanceable
    lock-screen / Dynamic Island view needs."""
    pdone = predicted_done_by or {}
    return {
        "pit": status.get("pit"),
        "setpoint": status.get("set_point"),
        "food1": status.get("food1"),
        "food2": status.get("food2"),
        "fanPct": status.get("fan_pct"),
        "lidOpen": bool(status.get("lid_countdown") or 0),
        "mode": status.get("pid_mode_label"),
        "predictedDoneFood1": pdone.get("food1"),
        "predictedDoneFood2": pdone.get("food2"),
    }


def liveactivity_payload(content_state: dict, *, event: str = "update",
                         now: float = 0.0, stale_secs: Optional[float] = None,
                         dismiss_ts: Optional[float] = None,
                         alert: Optional[dict] = None) -> dict:
    """Build the APNs payload for a Live Activity update or end. *event* is
    ``update`` during the cook or ``end`` when it finishes. ``stale-date`` tells
    iOS when to grey the activity if updates stop."""
    aps: dict = {
        "timestamp": int(now),
        "event": event,
        "content-state": content_state,
    }
    if stale_secs is not None:
        aps["stale-date"] = int(now + stale_secs)
    if event == "end" and dismiss_ts is not None:
        aps["dismissal-date"] = int(dismiss_ts)
    if alert:
        aps["alert"] = alert
    return {"aps": aps}


def apns_host(sandbox: bool) -> str:
    return HOST_SANDBOX if sandbox else HOST_PROD


def apns_topic(bundle_id: str, *, live_activity: bool = False) -> str:
    """The ``apns-topic`` header: the bundle id for alerts, suffixed for Live
    Activity push updates."""
    return f"{bundle_id}.push-type.liveactivity" if live_activity else bundle_id


# -- network sender (adapter; optional deps) ------------------------------

class ApnsSender:
    """Signs the provider JWT and POSTs payloads to APNs over HTTP/2.

    Requires ``cryptography`` (ES256 signing of the .p8 key) and
    ``httpx[http2]`` (HTTP/2 to Apple). Both are lazily imported; when missing,
    :meth:`available` is False and :meth:`send` returns a clear error instead of
    raising, so the daemon runs unaffected until the operator installs them.
    """

    def __init__(self, config: dict, time_fn=None) -> None:
        self.config = config
        import time as _time
        self.time_fn = time_fn or _time.time
        self._jwt: Optional[str] = None
        self._jwt_ts: float = 0.0
        self._key_pem: Optional[bytes] = None

    @staticmethod
    def available() -> bool:
        try:
            import cryptography  # noqa: F401
            import httpx  # noqa: F401
        except Exception:
            return False
        return True

    def _signed_jwt(self) -> str:
        now = self.time_fn()
        if self._jwt and (now - self._jwt_ts) < JWT_TTL_SECONDS:
            return self._jwt
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        if self._key_pem is None:
            with open(self.config["key_path"], "rb") as fh:
                self._key_pem = fh.read()
        key = serialization.load_pem_private_key(self._key_pem, password=None)
        signing_input = jwt_signing_input(
            self.config["key_id"], self.config["team_id"], now)
        der = key.sign(signing_input.encode("ascii"),
                       ec.ECDSA(hashes.SHA256()))
        # APNs wants the raw r||s (JOSE) signature, not the DER ECDSA encoding.
        r, s = utils.decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self._jwt = signing_input + "." + _b64url(raw)
        self._jwt_ts = now
        return self._jwt

    def send(self, token: str, payload: dict, *, push_type: str = "alert",
             priority: str = "default", live_activity: bool = False,
             timeout: float = 8.0) -> dict:
        """POST one payload to one device token. Returns
        ``{"ok": bool, "status": int|None, "unregistered": bool, "error": str|None}``.
        ``unregistered`` is True on a 410 so the caller can prune the token."""
        if not self.available():
            return {"ok": False, "status": None, "unregistered": False,
                    "error": "apns deps not installed (cryptography, httpx[http2])"}
        try:
            import httpx
        except Exception as e:  # pragma: no cover - guarded by available()
            return {"ok": False, "status": None, "unregistered": False,
                    "error": str(e)}
        cfg = self.config
        url = f"{apns_host(cfg['sandbox'])}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._signed_jwt()}",
            "apns-topic": apns_topic(cfg["bundle_id"], live_activity=live_activity),
            "apns-push-type": push_type,
            "apns-priority": str(priority_header(priority)),
        }
        try:
            with httpx.Client(http2=True, timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
            ok = 200 <= resp.status_code < 300
            return {"ok": ok, "status": resp.status_code,
                    "unregistered": resp.status_code == 410,
                    "error": None if ok else resp.text[:200]}
        except Exception as e:
            return {"ok": False, "status": None, "unregistered": False,
                    "error": str(e)}
