"""Tests for the Meater-style cook-completion detector (pure, no hardware)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import cookdone, protocol
from heatermeterd.cookdone import CookDoneDetector
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


class _FakeLink:
    def __init__(self):
        self.sent = []

    def start(self, on_line, loop):
        self.on_line = on_line
        self.loop = loop

    def send(self, line):
        self.sent.append(line)

    def pause(self):
        pass

    def resume(self, on_line=None, loop=None):
        pass

    def close(self):
        pass


def _feed(det, start, step, samples, target, ambient=75.0, probe=1):
    """Feed a sequence of single-probe temps at *step* seconds apart starting at
    *start*. Returns the last update() result and the timeline end ts."""
    ts = start
    last = {"events": [], "completed": False, "done_at": None}
    for temp in samples:
        last = det.update(ts, {probe: temp}, {probe: target}, ambient)
        ts += step
    return last, ts


# -- sanitize ---------------------------------------------------------------

def test_sanitize_defaults_and_clamps():
    d = cookdone.sanitize(None)
    assert d["enabled"] is True and d["on_complete"] == "notify"
    d = cookdone.sanitize({"grace_secs": 5, "drop_margin": 9999,
                           "on_complete": "bogus"})
    assert d["grace_secs"] == 30            # clamped up to min
    assert d["drop_margin"] == 300.0        # clamped down to max
    assert d["on_complete"] == "notify"     # invalid -> default
    d = cookdone.sanitize({"on_complete": "keep_warm", "keep_warm_temp": 160})
    assert d["on_complete"] == "keep_warm" and d["keep_warm_temp"] == 160.0


# -- core: reach -> pulled -> confirmed done --------------------------------

def test_reach_then_removed_completes():
    det = CookDoneDetector({"grace_secs": 120, "drop_margin": 35,
                            "rise_delta": 15})
    # climb to target
    det.update(0, {1: 150}, {1: 203}, 75)
    det.update(10, {1: 203}, {1: 203}, 75)   # reached
    # pulled: drops toward ambient and stays there past the grace window
    res, _ = _feed(det, 20, 10, [120, 95, 80, 78, 76, 76, 76, 76, 76, 76, 76,
                                 76, 76, 76, 76], target=203, ambient=75)
    assert det.completed is True
    # done_at backdates to when it left the meat (~ first sample below arm line)
    assert det.probes[1]["done"] is True


def test_pulled_then_reinserted_same_spot_cancels():
    det = CookDoneDetector({"grace_secs": 120, "drop_margin": 35,
                            "rise_delta": 15})
    det.update(0, {1: 203}, {1: 203}, 75)            # reached
    det.update(10, {1: 150}, {1: 203}, 75)           # big drop -> armed
    det.update(20, {1: 120}, {1: 203}, 75)           # falling (min=120)
    r = det.update(30, {1: 198}, {1: 203}, 75)       # shot back up -> reinsert
    assert any(e["event"] == "repositioned" for e in r["events"])
    assert det.probes[1]["pulled_since"] is None
    assert det.completed is False


def test_moved_to_cooler_spot_cancels():
    """The key nuance: probe moved to a cooler, less-done spot of a big cut.
    It dips into air, then rises off the minimum even though it settles BELOW
    target - that rise must cancel the pull."""
    det = CookDoneDetector({"grace_secs": 120, "drop_margin": 35,
                            "rise_delta": 15})
    det.update(0, {1: 203}, {1: 203}, 75)            # reached
    det.update(10, {1: 140}, {1: 203}, 75)           # armed (below 168 arm line)
    det.update(20, {1: 100}, {1: 203}, 75)           # in air, min=100
    # reinserted into a cooler spot: settles at 175 (below target) but well up
    # off the 100 minimum -> rise_delta -> reposition.
    r = det.update(30, {1: 175}, {1: 203}, 75)
    assert any(e["event"] == "repositioned" for e in r["events"])
    assert det.completed is False
    # and it can still complete later if actually pulled
    res, _ = _feed(det, 40, 10, [175, 120, 80, 76] + [76] * 14, 203, 75)
    assert det.completed is True


def test_unplugged_probe_completes_after_grace():
    det = CookDoneDetector({"grace_secs": 60, "drop_margin": 35})
    det.update(0, {1: 203}, {1: 203}, 75)            # reached
    # probe goes blank (unplugged) and stays blank past the window
    res, _ = _feed(det, 10, 10, [None] * 9, target=203, ambient=75)
    assert det.completed is True


def test_unplug_then_reinsert_hot_cancels():
    det = CookDoneDetector({"grace_secs": 120, "drop_margin": 35})
    det.update(0, {1: 203}, {1: 203}, 75)            # reached
    det.update(10, {1: None}, {1: 203}, 75)          # unplugged -> armed
    det.update(20, {1: None}, {1: 203}, 75)
    r = det.update(30, {1: 200}, {1: 203}, 75)       # replugged into hot meat
    assert any(e["event"] == "repositioned" for e in r["events"])
    assert det.completed is False


def test_not_reached_does_not_complete():
    det = CookDoneDetector({"grace_secs": 60, "drop_margin": 35})
    # pulled early, never hit target
    res, _ = _feed(det, 0, 10, [150, 160, 120, 80, 76, 76, 76, 76], 203, 75)
    assert det.completed is False


def test_no_target_is_inert():
    det = CookDoneDetector({"grace_secs": 30})
    res, _ = _feed(det, 0, 10, [203, 80, 76, 76, 76, 76], target=-1, ambient=75)
    assert det.completed is False


def test_disabled_is_inert():
    det = CookDoneDetector({"enabled": False, "grace_secs": 30})
    res, _ = _feed(det, 0, 10, [203, 80, 76, 76, 76, 76], 203, 75)
    assert det.completed is False


# -- multi-probe gating -----------------------------------------------------

def test_second_probe_still_cooking_blocks_completion():
    det = CookDoneDetector({"grace_secs": 60, "drop_margin": 35,
                            "rise_delta": 15})
    ts = 0
    # food1 reaches + is pulled; food2 still climbing (in food, below target)
    for f1, f2 in [(203, 150), (120, 160), (80, 170), (76, 180), (76, 185),
                   (76, 190), (76, 195), (76, 198)]:
        res = det.update(ts, {1: f1, 2: f2}, {1: 203, 2: 200}, 75)
        ts += 10
    assert det.probes[1]["done"] is True
    assert det.completed is False        # food2 still cooking blocks it

    # now food2 reaches + is pulled too (drop straight toward ambient) -> completes
    for f2 in [200, 120, 80, 76, 76, 76, 76, 76]:
        res = det.update(ts, {1: 76, 2: f2}, {1: 203, 2: 200}, 75)
        ts += 10
    assert det.completed is True


def test_unused_second_probe_does_not_block():
    det = CookDoneDetector({"grace_secs": 60, "drop_margin": 35})
    ts = 0
    # food2 has a target but is never plugged in (reads None)
    for f1 in [203, 120, 80, 76, 76, 76, 76, 76]:
        res = det.update(ts, {1: f1, 2: None}, {1: 203, 2: 200}, 75)
        ts += 10
    assert det.completed is True


def test_reset_clears_state():
    det = CookDoneDetector({"grace_secs": 30})
    _feed(det, 0, 10, [203, 80, 76, 76, 76, 76], 203, 75)
    assert det.completed is True
    det.reset()
    assert det.completed is False and det.probes == {}


# -- service integration ----------------------------------------------------

def test_service_marks_cook_complete_and_shutdown():
    async def scenario():
        clock = {"t": 1000.0}
        store = Store(":memory:")
        link = _FakeLink()
        svc = HeaterMeterService(link, store, time_fn=lambda: clock["t"])
        await svc.start()
        svc.save_cookdone({"enabled": True, "grace_secs": 20, "drop_margin": 35,
                           "rise_delta": 15, "on_complete": "shutdown"})
        events = []
        svc.on_event = lambda ev: events.append(ev)

        # Food1 target = 203 via the high alarm (flat list idx 3).
        svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))

        def hmsu(food1):
            f = "" if food1 is None else str(food1)
            svc._on_line(protocol.frame(
                f"HMSU,0,200,{f},,75,0,0,0,0,0,4"))

        hmsu(203)                                   # reached target
        for temp in [120, 90, 78, 76, 76, 76, 76]:  # pulled, stays out
            clock["t"] += 5
            hmsu(temp)

        sess = store.get_session(svc.session_id)
        assert sess["completed_ts"] is not None
        assert sess["completed_reason"] == "probe removed"
        assert any(ev.get("type") == "cook_complete" for ev in events)
        # on_complete=shutdown -> the cooker was idled
        assert any("sp=O" in s for s in link.sent)
        await svc.stop()
    asyncio.run(scenario())


def test_service_reposition_does_not_complete():
    async def scenario():
        clock = {"t": 5000.0}
        store = Store(":memory:")
        svc = HeaterMeterService(_FakeLink(), store, time_fn=lambda: clock["t"])
        await svc.start()
        svc.save_cookdone({"enabled": True, "grace_secs": 20, "drop_margin": 35,
                           "rise_delta": 15})

        svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))

        def hmsu(food1):
            svc._on_line(protocol.frame(f"HMSU,0,200,{food1},,75,0,0,0,0,0,4"))

        hmsu(203)
        # dip (moved to a cooler spot) then rises back up -> reposition
        for temp in [140, 100, 175, 180]:
            clock["t"] += 5
            hmsu(temp)
        assert store.get_session(svc.session_id)["completed_ts"] is None
        await svc.stop()
    asyncio.run(scenario())


def test_service_finish_cook():
    async def scenario():
        store = Store(":memory:")
        svc = HeaterMeterService(_FakeLink(), store, time_fn=lambda: 9000.0)
        await svc.start()
        assert svc.finish_cook()["ok"] is False            # nothing active yet
        svc._on_line(protocol.frame("HMSU,0,200,,,75,0,0,0,0,0,4"))  # starts a session
        r = svc.finish_cook()
        assert r["ok"] is True
        sess = store.get_session(svc.session_id)
        assert sess["completed_ts"] is not None
        assert sess["completed_reason"] == "manual"
        await svc.stop()
    asyncio.run(scenario())


def test_service_shutdown_writes_request():
    import tempfile
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            spool = os.path.join(tmp, "spool")
            os.makedirs(spool, exist_ok=True)
            store = Store(":memory:")
            svc = HeaterMeterService(_FakeLink(), store, time_fn=lambda: 1.0)
            svc.firmware_spool = spool
            svc._shutdown_grace = 0.01
            await svc.start()
            svc._on_line(protocol.frame("HMSU,0,200,,,75,0,0,0,0,0,4"))  # open session
            sid = svc.session_id
            r = svc.shutdown_system(dryrun=True)
            assert r["ok"] is True and r["dryrun"] is True
            await asyncio.sleep(0.05)   # let the deferred trigger fire
            req = os.path.join(spool, "poweroff.request")
            assert os.path.exists(req)
            assert open(req).read() == "dryrun"
            # the current cook was closed
            assert store.get_session(sid)["ended_ts"] is not None
            await svc.stop()
    asyncio.run(scenario())


def test_second_probe_cold_early_cook_blocks_completion():
    # A second targeted probe that is plugged in but still cold (below the
    # ambient band, not yet reached) must still block completion so the fire
    # action waits for it.
    det = CookDoneDetector({"grace_secs": 30, "drop_margin": 35, "rise_delta": 15})
    ts = 0
    for f1 in [203, 120, 80, 76, 76]:
        det.update(ts, {1: f1, 2: 90}, {1: 203, 2: 200}, 75)
        ts += 10
    assert det.probes[1]["done"] is True
    assert det.completed is False        # food2 (plugged, cold) blocks it


def test_service_set_units_converts():
    async def scenario():
        svc = HeaterMeterService(_FakeLink(), Store(":memory:"), time_fn=lambda: 1.0)
        svc._fw_send_gap = 0.001
        await svc.start()
        svc._on_line(protocol.frame("HMPD,0,4.0,3.0,2.0,F"))            # units F
        svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))   # food1 high 203F
        svc._on_line(protocol.frame("HMSU,0,200,,,75,0,0,0,0,0,4"))     # cooker OFF
        svc.link.sent.clear()
        r = svc.set_units("C")
        assert r["ok"] and r["changed"]
        await asyncio.sleep(0.05)
        sent = "".join(svc.link.sent)
        assert "/set?sp=C" in sent          # off -> unit-only (don't turn it on)
        assert "95.0" in sent               # 203F -> 95.0C in the alarm re-send
        # No-op when already in that unit.
        svc._on_line(protocol.frame("HMPD,0,4.0,3.0,2.0,C"))
        assert svc.set_units("C")["changed"] is False
    asyncio.run(scenario())


def test_config_backup_restore_roundtrip():
    import tempfile
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc = HeaterMeterService(_FakeLink(), Store(":memory:"), time_fn=lambda: 1.0)
            svc.mqtt_config_path = os.path.join(tmp, "mqtt.json")
            svc.cookdone_config_path = os.path.join(tmp, "cookdone.json")
            svc.probe_presets_path = os.path.join(tmp, "pp.json")
            svc._fw_send_gap = 0.001
            await svc.start()
            svc._on_line(protocol.frame("HMPD,0,4.0,3.0,2.0,F"))
            svc._on_line(protocol.frame("HMPC,0,7.3e-4,2.1e-4,9.5e-8,5.0,3"))
            svc._on_line(protocol.frame("HMPN,Pit Temp,Brisket,,Ambient"))
            svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
            svc.reconfigure_mqtt({"enabled": False, "host": "h", "password": "secret"})
            svc.set_probe_preset_sel(0, "ad8495_ktype")
            svc.store.save_program("My Cook", [{"name": "s1", "setpoint": 250}], 1.0)

            b = svc.config_backup(include_secrets=False)
            assert b["schema"] == 1
            assert b["board"]["probe_coeffs"][0]["type"] == "3"
            assert "password" not in b["host"]["mqtt"]           # masked
            assert any(p["name"] == "My Cook" for p in b["programs"])
            assert svc.config_backup(include_secrets=True)["host"]["mqtt"]["password"] == "secret"

            # Restore onto a fresh service.
            svc2 = HeaterMeterService(_FakeLink(), Store(":memory:"), time_fn=lambda: 2.0)
            svc2.mqtt_config_path = os.path.join(tmp, "mqtt2.json")
            svc2.cookdone_config_path = os.path.join(tmp, "cookdone2.json")
            svc2.probe_presets_path = os.path.join(tmp, "pp2.json")
            svc2._fw_send_gap = 0.001
            await svc2.start()
            r = svc2.config_restore(svc.config_backup(include_secrets=True))
            assert r["ok"] and "board" in r["applied"]
            await asyncio.sleep(0.06)
            sent = "".join(svc2.link.sent)
            assert "/set?pc0=" in sent and ",3" in sent          # pit type 3
            assert "pn1=Brisket" in sent and "203" in sent       # name + alarm
            assert any(p["name"] == "My Cook" for p in svc2.store.list_programs())
            assert os.path.exists(svc2.probe_presets_path)
            await svc.stop()
            await svc2.stop()
    asyncio.run(scenario())


def test_service_cleanup_db():
    import tempfile
    from heatermeterd.protocol import Status
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(":memory:")
            svc = HeaterMeterService(_FakeLink(), store, time_fn=lambda: 1_000_000.0)
            svc.storage_config_path = os.path.join(tmp, "storage.json")
            await svc.start()
            store.insert(Status(pit=200.0), 1.0, session_id=1)            # ancient
            store.insert(Status(pit=200.0), 1_000_000.0, session_id=1)    # now
            r = svc.cleanup_db(retention_days=1)                          # cutoff now-1d
            assert r["pruned"] >= 1
            assert store.db_stats()["samples"] == 1
            assert svc.save_storage_config({"retention_days": 90})["retention_days"] == 90
            assert svc.get_storage_config()["retention_days"] == 90
            await svc.stop()
    asyncio.run(scenario())
