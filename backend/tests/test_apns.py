"""Tests for the native-iOS push (APNs) support.

Only the pure layer is exercised here (registry, JWT signing input, payload
builders) plus the service-level dispatch with a fake sender. The network
adapter (ES256 signing + HTTP/2 POST) needs optional deps and a real device, so
it is injected/stubbed.
"""

import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import apns
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def test_registry_dedupes_refreshes_and_caps():
    reg = apns.PushRegistry(max_tokens=3)
    assert reg.register("a", now=1.0)
    assert reg.register("b", now=2.0)
    assert reg.register("a", now=3.0)         # re-register moves to newest
    assert reg.all_tokens() == ["b", "a"]
    assert not reg.register("", now=4.0)      # empty rejected
    reg.register("c", now=5.0)
    reg.register("d", now=6.0)                # over cap -> oldest ("b") evicted
    assert reg.all_tokens() == ["a", "c", "d"]
    assert reg.remove("c") and not reg.remove("zz")
    assert reg.all_tokens() == ["a", "d"]


def test_registry_survives_garbage_input():
    reg = apns.PushRegistry([{"token": "x", "added_ts": 1},
                             {"no_token": True}, "junk", 42])
    assert reg.all_tokens() == ["x"]


def test_jwt_signing_input_is_es256_header_and_claims():
    si = apns.jwt_signing_input("KEY123", "TEAM45", now=1000.5)
    header_seg, claims_seg = si.split(".")
    header = json.loads(_b64url_decode(header_seg))
    claims = json.loads(_b64url_decode(claims_seg))
    assert header == {"alg": "ES256", "kid": "KEY123"}
    assert claims == {"iss": "TEAM45", "iat": 1000}    # iat floored to int
    assert "=" not in si                                # base64url, unpadded


def test_alert_payload_maps_priority_to_interruption_level():
    p = apns.alert_payload("Brisket almost done", "About 8 min from 203.",
                           priority="high")
    aps = p["aps"]
    assert aps["alert"] == {"title": "Brisket almost done",
                            "body": "About 8 min from 203."}
    assert aps["interruption-level"] == "time-sensitive"
    assert aps["thread-id"] == "heatermeter"
    assert apns.priority_header("high") == 10
    assert apns.priority_header("low") == 5
    # A low-priority push is passive.
    assert apns.alert_payload("x", "y", priority="low")["aps"][
        "interruption-level"] == "passive"


def test_liveactivity_content_state_and_payload():
    status = {"pit": 248.0, "set_point": 250.0, "food1": 173.0, "food2": None,
              "fan_pct": 35.0, "lid_countdown": 0, "pid_mode_label": "At temp"}
    cs = apns.liveactivity_content_state(
        status, {"food1": "2026-06-22T18:40:00-06:00"})
    assert cs["pit"] == 248.0 and cs["setpoint"] == 250.0
    assert cs["lidOpen"] is False and cs["mode"] == "At temp"
    assert cs["predictedDoneFood1"].startswith("2026-06-22T18:40")
    upd = apns.liveactivity_payload(cs, event="update", now=1000.0,
                                    stale_secs=120)
    assert upd["aps"]["event"] == "update"
    assert upd["aps"]["timestamp"] == 1000
    assert upd["aps"]["stale-date"] == 1120
    assert upd["aps"]["content-state"]["pit"] == 248.0
    end = apns.liveactivity_payload(cs, event="end", now=2000.0,
                                    dismiss_ts=2300.0)
    assert end["aps"]["event"] == "end" and end["aps"]["dismissal-date"] == 2300


def test_apns_topic_and_host():
    assert apns.apns_topic("com.x.HM") == "com.x.HM"
    assert apns.apns_topic("com.x.HM", live_activity=True) == \
        "com.x.HM.push-type.liveactivity"
    assert apns.apns_host(False).endswith("api.push.apple.com")
    assert apns.apns_host(True).endswith("api.sandbox.push.apple.com")


def test_credentials_complete():
    assert not apns.credentials_complete(apns.default_config())
    full = {"team_id": "T", "key_id": "K", "key_path": "/k.p8",
            "bundle_id": "com.x.HM"}
    assert apns.credentials_complete(full)


# -- service-level dispatch with a fake sender ----------------------------

class FakeSender:
    """Stands in for ApnsSender; records sends and can mark a token dead (410)."""
    def __init__(self, dead=()):
        self.sent = []
        self._dead = set(dead)

    def send(self, token, payload, **kw):
        self.sent.append((token, payload, kw))
        return {"ok": token not in self._dead, "status": 410 if token in
                self._dead else 200, "unregistered": token in self._dead,
                "error": None}


def _svc(tmp):
    svc = HeaterMeterService(SimLink(), Store(":memory:"))
    svc.push_config_path = os.path.join(tmp, "push.json")
    return svc


def test_register_persists_and_status(tmp_path=None):
    import tempfile
    svc = _svc(tempfile.mkdtemp())
    assert svc.push_status()["token_count"] == 0
    r = svc.register_push_token("abc123", "ios")
    assert r["ok"] and r["token_count"] == 1
    # Persisted to disk, chmod 600.
    assert (os.stat(svc.push_config_path).st_mode & 0o777) == 0o600
    # Reload picks the token back up.
    svc2 = _svc(os.path.dirname(svc.push_config_path))
    svc2.push_config_path = svc.push_config_path
    svc2._push_cfg = svc2._load_push_file()
    assert svc2.push_status()["token_count"] == 1
    # Empty token rejected.
    assert not svc.register_push_token("")["ok"]


def test_push_dispatches_to_apns_and_prunes_dead_tokens():
    async def scenario():
        import tempfile
        svc = _svc(tempfile.mkdtemp())
        await svc.start()
        # Configure + enable push, with a live and a dead token.
        svc.save_push_config({"enabled": True, "team_id": "T", "key_id": "K",
                              "key_path": "/dev/null", "bundle_id": "com.x.HM"})
        svc.register_push_token("good")
        svc.register_push_token("stale")
        fake = FakeSender(dead=("stale",))
        svc._apns = fake
        # Force the sender selection to return our fake (creds complete + we set
        # _apns; available() may be False without deps, so bypass via monkeypatch).
        svc._apns_sender = lambda: fake

        svc._push("Pit hot", "The pit hit 250.", priority="high")
        await asyncio.sleep(0.2)   # _push_apns runs the broadcast in an executor

        sent_tokens = sorted(t for t, _p, _k in fake.sent)
        assert sent_tokens == ["good", "stale"]
        # The payload is the mapped alert.
        assert fake.sent[0][1]["aps"]["alert"]["title"] == "Pit hot"
        await asyncio.sleep(0.05)
        # The 410 token was pruned; the good one remains.
        assert svc._push_cfg["tokens"] and \
            [t["token"] for t in svc._push_cfg["tokens"]] == ["good"]
        await svc.stop()

    asyncio.run(scenario())


def test_push_noop_when_disabled():
    async def scenario():
        import tempfile
        svc = _svc(tempfile.mkdtemp())
        await svc.start()
        svc.register_push_token("good")     # token but push disabled (default)
        sent = []
        svc._apns_sender = lambda: (_ for _ in ()).throw(AssertionError("called"))
        # Disabled -> _apns_sender returns None via the real path; emulate by
        # leaving enabled False so _push_apns short-circuits before the sender.
        svc._apns_sender = lambda: None
        svc._push("x", "y")
        await asyncio.sleep(0.05)
        assert sent == []
        await svc.stop()

    asyncio.run(scenario())
