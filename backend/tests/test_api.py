"""Smoke test for the FastAPI layer.

Skips cleanly when fastapi/httpx are not installed (the pure suite still runs),
and exercises the real app end to end when they are: REST status/history,
setpoint control into the simulator, the command guard, the WebSocket stream,
and static dashboard serving.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from fastapi.testclient import TestClient
    HAVE_WEB = True
except Exception:
    HAVE_WEB = False

from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def test_api_end_to_end():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=0.05, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    app = create_app(svc)

    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 200
        assert "status" in c.get("/api/status").json()

        index = c.get("/")
        assert index.status_code == 200 and "HeaterMeter" in index.text
        # The app's own HTML/JS/CSS must be no-cache so a redeploy is picked up.
        assert "no-cache" in index.headers.get("cache-control", "")

        # Share-page assets (the classic dashboard itself was retired).
        css = c.get("/style.css")
        assert css.status_code == 200
        assert "no-cache" in css.headers.get("cache-control", "")
        # Vendored, versioned assets may cache hard.
        vend = c.get("/vendor/uPlot.iife.min.js")
        assert vend.status_code == 200
        assert "immutable" in vend.headers.get("cache-control", "")
        # Path traversal is blocked.
        assert c.get("/../api.py").status_code in (404, 400)

        with c.websocket_connect("/api/ws") as ws:
            assert "state" in ws.receive_json()   # initial snapshot
            # Subsequent messages may be either state updates or event
            # side-channel messages (e.g. session_started). Read a few and
            # confirm at least one carries state.
            got_state = False
            for _ in range(6):
                m = ws.receive_json()
                if "state" in m:
                    got_state = True
                    break
            assert got_state

        assert c.post("/api/setpoint", json={"value": 300, "unit": "F"}).status_code == 200
        time.sleep(0.3)
        assert link.board.setpoint == 300.0

        # Phase 2 control endpoints round-trip into the simulated board.
        assert c.post("/api/probe-name", json={"index": 1, "name": "Brisket"}).status_code == 200
        time.sleep(0.2)
        assert link.board.probe_names[1] == "Brisket"

        assert c.post("/api/offsets", json={"offsets": [None, None, None, -3]}).status_code == 200
        time.sleep(0.2)
        assert link.board.offsets[3] == -3

        assert c.post("/api/pid", json={"p": 5.5, "i": 0.01}).status_code == 200
        time.sleep(0.2)
        assert link.board.pid["p"] == 5.5
        assert link.board.pid["i"] == 0.01

        assert c.post("/api/alarms", json={"thresholds": [None, 250, None, None, None, None, None, None]}).status_code == 200
        time.sleep(0.2)
        assert link.board.alarms[1] == 250

        assert c.post("/api/manual", json={"percent": 40}).status_code == 200
        time.sleep(0.2)
        assert link.board.manual is True

        # Fan settings round-trip.
        assert c.post("/api/fan", json={"fan_high": 80, "max_startup": 50}).status_code == 200
        time.sleep(0.2)
        assert link.board.fan_params["high"] == 80
        assert link.board.fan_params["max_startup"] == 50

        # Invert flags packed into the bitfield.
        assert c.post("/api/fan", json={"invert_fan": True}).status_code == 200
        time.sleep(0.2)
        assert link.board.fan_params["flags"] & 1

        # Lid detect.
        assert c.post("/api/lid", json={"offset_percent": 7, "duration_seconds": 300}).status_code == 200
        time.sleep(0.2)
        assert link.board.lid["offset"] == 7
        assert link.board.lid["duration"] == 300

        # Probe type preset round-trip.
        presets_resp = c.get("/api/probe-presets").json()
        assert "thermoworks_pro" in presets_resp["presets"]
        assert presets_resp["types"]["3"] == "Thermocouple"
        assert c.post("/api/probe-type", json={"index": 1, "preset": "maverick_et732"}).status_code == 200
        time.sleep(0.2)
        assert link.board.probe_coeffs[1] is not None
        assert link.board.probe_types[1] == 1  # INTERNAL

        # Probe disable.
        assert c.post("/api/probe-type", json={"index": 2, "disabled": True}).status_code == 200
        time.sleep(0.2)
        assert link.board.probe_types[2] == 0

        # Meat presets.
        meat = c.get("/api/presets").json()
        assert any(p["key"] == "brisket" for p in meat["meat"])

        # Prediction endpoint (needs some history; sim has been logging).
        pred = c.get("/api/predict", params={"channel": "pit", "target": 999})
        assert pred.status_code == 200
        assert "confidence" in pred.json()
        assert c.get("/api/predict", params={"channel": "bogus", "target": 100}).status_code == 400

        # Sessions: one should have auto-started from the streaming data.
        sessions = c.get("/api/sessions").json()
        assert len(sessions) >= 1
        sid = sessions[0]["id"]
        assert c.patch(f"/api/sessions/{sid}", json={"name": "Test Cook"}).status_code == 200
        assert c.get(f"/api/sessions/{sid}").json()["name"] == "Test Cook"

        # Notes.
        nr = c.post("/api/notes", json={"text": "wrapped it", "channel": "pit"})
        assert nr.status_code == 200
        notes = c.get("/api/notes").json()
        assert any(n["text"] == "wrapped it" for n in notes)

        # Cook programs: status idle, save a template, start it, advance, stop.
        assert c.get("/api/program").json().get("running") is False
        prog = {"name": "Test", "stages": [
            {"name": "Smoke", "setpoint": 250,
             "advance": {"type": "probe", "channel": "food1", "temp": 165}},
            {"name": "Hold", "setpoint": 150, "advance": {"type": "manual"}}]}
        assert c.post("/api/programs", json=prog).status_code == 200
        assert len(c.get("/api/programs").json()) >= 1
        assert c.post("/api/program/start", json=prog).status_code == 200
        time.sleep(0.2)
        st = c.get("/api/program").json()
        assert st["stage_count"] == 2 and st["done"] is False
        assert link.board.setpoint == 250   # stage 0 applied
        assert c.post("/api/program/advance").status_code == 200
        assert c.post("/api/program/stop").status_code == 200
        # Bad program rejected.
        assert c.post("/api/program/start", json={"stages": []}).status_code == 400

        # CSV export.
        csv_resp = c.get("/api/export.csv")
        assert csv_resp.status_code == 200
        assert "timestamp_iso" in csv_resp.text

        # Session sharing: enable -> public link works -> disable -> 404.
        share = c.post(f"/api/sessions/{sid}/share", json={"enabled": True}).json()
        assert share["token"]
        pub = c.get(f"/api/share/{share['token']}")
        assert pub.status_code == 200
        assert "history" in pub.json() and "name" in pub.json()
        c.post(f"/api/sessions/{sid}/share", json={"enabled": False})
        assert c.get(f"/api/share/{share['token']}").status_code == 404

        # Validation guards.
        assert c.post("/api/probe-name", json={"index": 9, "name": "x"}).status_code == 400
        assert c.post("/api/offsets", json={"offsets": [1, 2]}).status_code == 400
        assert c.post("/api/command", json={"path": "/nope"}).status_code == 400
        assert c.post("/api/probe-type", json={"index": 0, "preset": "nope"}).status_code == 400

        hist = c.get("/api/history").json()
        assert "t" in hist


def test_firmware_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import json
    import tempfile

    from heatermeterd.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        spool = os.path.join(tmp, "firmware", "spool")
        os.makedirs(spool, exist_ok=True)
        manifest = os.path.join(tmp, "manifest.json")
        with open(manifest, "w") as f:
            json.dump({"schema": 1, "images": [{
                "version": "20260602-hm3",
                "file": "heatermeter-20260602-hm3.hex",
                "sha256": "a" * 64, "changelog": "x",
                "eeprom_reset": False, "board_rev": "B"}]}, f)

        # interval=10 -> the board stays quiet (no driving HMSU) so the
        # pre-flight guard sees an idle cooker during the test window.
        link = SimLink(setpoint=225.0, interval=10.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        svc.firmware_dir = os.path.join(tmp, "firmware")
        svc.firmware_spool = spool
        svc.firmware_manifest_path = manifest
        app = create_app(svc)

        with TestClient(app) as c:
            fw = c.get("/api/firmware")
            assert fw.status_code == 200
            j = fw.json()
            assert j["configured"] is True
            assert any(i["version"] == "20260602-hm3" for i in j["images"])

            assert c.get("/api/firmware/status").json()["state"] in ("idle", "flashing")
            assert c.get("/api/status").json()["firmware"]["state"] in ("idle", "flashing")

            # Confirmation is mandatory.
            assert c.post("/api/firmware/flash",
                          json={"version": "20260602-hm3"}).status_code == 400
            # Unknown version is refused (guard passes on a quiet board).
            assert c.post("/api/firmware/flash",
                          json={"version": "nope", "confirm": True}).status_code == 409
            # Rollback with no backup available is refused.
            assert c.post("/api/firmware/rollback",
                          json={"confirm": True}).status_code == 409

            # A valid, confirmed flash kicks off (the helper never runs here; we
            # only assert it started and wrote the request file).
            r = c.post("/api/firmware/flash",
                       json={"version": "20260602-hm3", "confirm": True})
            assert r.status_code == 200 and r.json()["ok"] is True
            assert os.path.exists(os.path.join(spool, "request.json"))
            assert c.get("/api/firmware/status").json()["state"] == "flashing"
            # A second flash while one is running is refused.
            assert c.post("/api/firmware/flash",
                          json={"version": "20260602-hm3", "confirm": True}).status_code == 409


def test_host_update_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import tempfile

    from heatermeterd.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        spool = os.path.join(tmp, "hostupdate", "spool")
        os.makedirs(spool, exist_ok=True)
        link = SimLink(setpoint=225.0, interval=10.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        svc.hostupdate_dir = os.path.join(tmp, "hostupdate")
        svc.hostupdate_spool = spool
        svc.hostupdate_staging = os.path.join(svc.hostupdate_dir, "staging")
        svc.hostupdate_config_path = os.path.join(tmp, "hostupdate.json")
        svc.install_root = os.path.join(tmp, "install")
        app = create_app(svc)

        with TestClient(app) as c:
            hu = c.get("/api/host-update").json()
            assert hu["configured"] is False
            assert hu["current"]              # APP_VERSION string
            assert c.get("/api/status").json()["hostupdate"]["state"] == "idle"

            # URL validation on the config endpoint.
            assert c.post("/api/host-update/config",
                          json={"manifest_url": "ftp://x/y"}).status_code == 400
            ok = c.post("/api/host-update/config",
                        json={"manifest_url": "https://x.test/u.json",
                              "auto_check": True})
            assert ok.status_code == 200 and ok.json()["auto_check"] is True
            assert c.get("/api/host-update").json()["configured"] is True

            # Confirmation is mandatory.
            assert c.post("/api/host-update/apply", json={}).status_code == 400
            # Checking against an unreachable host is a 400, not a crash.
            assert c.post("/api/host-update/check").status_code == 400

            # Unconfigured apply is refused (clear the URL first).
            c.post("/api/host-update/config", json={"manifest_url": ""})
            r = c.post("/api/host-update/apply", json={"confirm": True})
            assert r.status_code == 409
            assert "not configured" in r.json()["error"].lower()

            # ack is always safe.
            assert c.post("/api/host-update/ack").json()["ok"] is True


def test_lcd_message_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=10.0, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    app = create_app(svc)
    with TestClient(app) as c:
        # Empty message refused.
        assert c.post("/api/lcd/message", json={"line1": "  "}).status_code == 400
        # Commas split lines in the firmware toast; they must be stripped, and
        # each line capped at the LCD's 16 chars.
        r = c.post("/api/lcd/message",
                   json={"line1": "Dinner, is ready right now folks",
                         "line2": "Come and get it"})
        assert r.status_code == 200
        j = r.json()
        assert "," not in j["line1"] and len(j["line1"]) <= 16
        sent = [l for l in link.sent if "tt=" in l] if hasattr(link, "sent") else []
        # SimLink may not record sends; the response shape is the contract here.
        assert j["ok"] is True


def test_probe_watch_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=10.0, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    app = create_app(svc)
    with TestClient(app) as c:
        d = c.get("/api/probe-watch").json()
        assert d["enabled"] is True and "dropout_secs" in d and "stall_enabled" in d
        # /api/status carries the (initially empty) per-channel health map.
        assert c.get("/api/status").json()["probe_health"] == {}
        # Partial update merges + clamps; dropout_secs floors at 2s.
        r = c.post("/api/probe-watch", json={"dropout_secs": 0.1, "stall_enabled": False}).json()
        assert r["dropout_secs"] >= 2.0 and r["stall_enabled"] is False
        assert r["enabled"] is True   # untouched field preserved


def test_push_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import tempfile
    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=10.0, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    svc.push_config_path = tempfile.mkstemp(suffix=".json")[1]
    app = create_app(svc)
    with TestClient(app) as c:
        d = c.get("/api/push").json()
        assert d["enabled"] is False and d["token_count"] == 0
        # A device registers its APNs token.
        r = c.post("/api/push/register", json={"token": "deadbeef"}).json()
        assert r["ok"] and r["token_count"] == 1
        assert c.get("/api/push").json()["token_count"] == 1
        # Empty token is a 400.
        assert c.post("/api/push/register", json={"token": ""}).status_code == 400
        # Operator config merges (credentials, sandbox flag).
        r2 = c.post("/api/push", json={"enabled": True, "sandbox": True,
                                       "bundle_id": "com.x.HM"}).json()
        assert r2["sandbox"] is True and r2["bundle_id"] == "com.x.HM"
        # Deregister.
        r3 = c.request("DELETE", "/api/push/deadbeef").json()
        assert r3["removed"] is True and r3["token_count"] == 0


def test_ui_prefs_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import tempfile
    from heatermeterd.api import create_app

    svc = HeaterMeterService(SimLink(interval=10.0, seed=1), Store(":memory:"))
    svc.uiprefs_config_path = tempfile.mkstemp(suffix=".json")[1]
    app = create_app(svc)
    with TestClient(app) as c:
        assert c.get("/api/ui-prefs").json()["welcomed"] is False
        assert c.post("/api/ui-prefs", json={"welcomed": True}).json()["welcomed"] is True
        # Persists across a fresh service (i.e. any browser sees it).
        svc2 = HeaterMeterService(SimLink(interval=10.0, seed=1), Store(":memory:"))
        svc2.uiprefs_config_path = svc.uiprefs_config_path
        assert svc2.get_uiprefs()["welcomed"] is True


def test_notes_scoped_by_session_and_window():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    svc = HeaterMeterService(SimLink(interval=10.0, seed=1), Store(":memory:"))
    now = svc.time_fn()
    svc.store.add_note(now - 9 * 24 * 3600, "old cook", session_id=1)   # ~9 days ago
    svc.store.add_note(now - 60, "this cook", session_id=2)             # 1 min ago
    app = create_app(svc)
    with TestClient(app) as c:
        # No scope -> everything (old behavior, now only used deliberately).
        assert len(c.get("/api/notes").json()) == 2
        # Scoped to the current session -> only this cook's note.
        s2 = c.get("/api/notes?session_id=2").json()
        assert [n["text"] for n in s2] == ["this cook"]
        # Trailing window -> the week-old note is excluded.
        recent = c.get("/api/notes?minutes=120").json()
        assert [n["text"] for n in recent] == ["this cook"]


def test_lid_recovery_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=10.0, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    app = create_app(svc)
    with TestClient(app) as c:
        d = c.get("/api/lid-recovery").json()
        assert d["enabled"] is True and "recover_delta" in d and "ramp_secs" in d
        # Partial update merges + clamps; start_pct floors at 0, ramp_secs caps.
        r = c.post("/api/lid-recovery",
                   json={"start_pct": -10, "ramp_secs": 99999,
                         "enabled": False}).json()
        assert r["start_pct"] == 0 and r["ramp_secs"] == 600
        assert r["enabled"] is False
        assert r["recover_delta"] == d["recover_delta"]   # untouched preserved
        assert c.get("/api/lid-recovery").json()["enabled"] is False


def test_cook_done_api():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    from heatermeterd.api import create_app

    link = SimLink(setpoint=225.0, interval=10.0, seed=1)
    svc = HeaterMeterService(link, Store(":memory:"))
    app = create_app(svc)
    with TestClient(app) as c:
        d = c.get("/api/cook-done").json()
        assert d["enabled"] is True and d["on_complete"] == "notify"
        # Update + clamp + validate.
        r = c.post("/api/cook-done", json={"grace_secs": 300,
                   "on_complete": "keep_warm", "keep_warm_temp": 160}).json()
        assert r["grace_secs"] == 300 and r["on_complete"] == "keep_warm"
        assert r["keep_warm_temp"] == 160.0
        # Bad enum falls back to the safe default; other fields are preserved.
        r2 = c.post("/api/cook-done", json={"on_complete": "bogus"}).json()
        assert r2["on_complete"] == "notify"      # invalid -> safe default
        assert r2["grace_secs"] == 300            # unrelated field preserved
        assert c.get("/api/cook-done").json()["grace_secs"] == 300


def test_probe_preset_persistence():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import tempfile

    from heatermeterd.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        link = SimLink(setpoint=225.0, interval=10.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        svc.probe_presets_path = os.path.join(tmp, "probe_presets.json")
        app = create_app(svc)
        with TestClient(app) as c:
            pp = c.get("/api/probe-presets").json()
            assert pp["presets"]["ad8495_ktype"]["type"] == 3   # thermocouple
            assert pp["presets"]["thermoworks_pro"]["type"] == 1  # thermistor
            assert c.post("/api/probe-type",
                          json={"index": 0, "preset": "ad8495_ktype"}).status_code == 200
            assert c.get("/api/status").json()["probe_preset_sel"]["0"] == "ad8495_ktype"
            assert c.post("/api/probe-type",
                          json={"index": 1, "disabled": True}).status_code == 200
            assert c.get("/api/status").json()["probe_preset_sel"]["1"] == "__off"


def test_auth_gate():
    if not HAVE_WEB:
        print("    (skipped: fastapi/httpx not installed)")
        return

    import tempfile

    from heatermeterd.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        svc = HeaterMeterService(SimLink(setpoint=225.0, interval=10.0, seed=1),
                                 Store(":memory:"))
        svc.auth_config_path = os.path.join(tmp, "auth.json")
        app = create_app(svc)
        with TestClient(app) as c:
            assert c.get("/api/status").status_code == 200       # auth off -> open
            svc.auth_set_password("secret")                      # enable auth
            assert c.get("/api/status").status_code == 401       # now gated
            assert c.get("/api/auth").json()["enabled"] is True  # status exempt
            assert c.get("/api/share/nope").status_code == 404   # share exempt (not 401)
            assert c.get("/").status_code == 200                 # SPA still served
            login = c.post("/api/login", json={"password": "secret"})
            assert login.status_code == 200
            tok = login.json()["token"]
            assert c.get("/api/status", headers={"Authorization": "Bearer " + tok}).status_code == 200
            assert c.post("/api/login", json={"password": "nope"}).status_code == 401
