"""Tests for the power-up policy: resume a blipped cook, idle a stale setpoint."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import powerup, protocol
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store

NOW = 100_000.0


def test_sanitize_defaults_and_clamps():
    d = powerup.sanitize(None)
    assert d == {"enabled": True, "blip_secs": 600, "cold_below": 150.0}
    d2 = powerup.sanitize({"blip_secs": -5, "cold_below": 5, "enabled": False})
    assert d2["blip_secs"] == 0 and d2["cold_below"] == 50.0 and d2["enabled"] is False


def test_board_off_or_manual_is_none():
    assert powerup.decide(NOW, NOW - 5, 265, 0, 62) == "none"      # off
    assert powerup.decide(NOW, NOW - 5, 265, None, 62) == "none"   # no status
    assert powerup.decide(NOW, NOW - 5, 265, -30, 62) == "none"    # manual
    assert powerup.decide(NOW, NOW - 5, 265, 265, 62,
                          cfg={"enabled": False}) == "none"        # guard off


def test_short_gap_with_active_cook_resumes():
    # 5-minute blip, cook was running at 265, board back at 265 into a warm pit.
    assert powerup.decide(NOW, NOW - 300, 265, 265, 240) == "resume"
    # Even a cold-reading pit resumes when the gap is short (probe may still be
    # settling) - the blip rule wins.
    assert powerup.decide(NOW, NOW - 300, 265, 265, 90) == "resume"


def test_long_gap_cold_pit_idles():
    # The 2026-09-14 case: last sample days ago, board boots into 600 from
    # EEPROM, pit at room temp.
    assert powerup.decide(NOW, NOW - 3 * 86400, 600, 600, 62) == "idle"
    # No history at all (fresh install) -> also idle.
    assert powerup.decide(NOW, None, None, 600, 62) == "idle"
    # Short gap but the cook was NOT active before -> idle (a stray setpoint).
    assert powerup.decide(NOW, NOW - 60, 0, 250, 70) == "idle"


def test_hot_pit_or_at_temp_never_idled():
    # Pi-only reboot after an hour offline while the board kept cooking.
    assert powerup.decide(NOW, NOW - 3600, 265, 265, 262) == "resume"
    # Fork firmware says "at temp" -> the board never rebooted.
    assert powerup.decide(NOW, NOW - 3600, 265, 265, 100,
                          pid_mode=powerup.PIDMODE_NORMAL) == "resume"


def _svc(store):
    svc = HeaterMeterService(SimLink(interval=10.0), store)
    tt = [NOW]
    svc.time_fn = lambda: tt[0]
    return svc, tt


def test_service_idles_stale_setpoint_once():
    import asyncio

    async def scenario():
        store = Store(":memory:")
        # History: a cook days ago at 265, then nothing.
        sid = store.start_session(NOW - 3 * 86400)
        store.insert(protocol.Status(set_point=265, pit=250), NOW - 3 * 86400 + 60,
                     session_id=sid)
        store.close_session(sid, NOW - 3 * 86400 + 60)
        svc, tt = _svc(store)
        sent = []
        svc.link.send = lambda line: sent.append(line)
        pushes = []
        svc._push = lambda title, *a, **k: pushes.append(title)
        await svc.start()
        assert svc._powerup_pending

        # Board boots into 600 from EEPROM, pit cold, STARTUP mode.
        svc._on_line(protocol.frame("HMSU,600,62,,,,100,100,0,50,0,0"))
        assert svc._powerup_pending is False
        assert any("sp=O" in s for s in sent)                 # idled the board
        kinds = [e["kind"] for e in svc.store.list_events()]
        assert kinds.count("powerup_idle") == 1
        assert "HeaterMeter powered on" in pushes

        # Decided once: later setpoints are the user's business.
        sent.clear()
        tt[0] += 30
        svc._on_line(protocol.frame("HMSU,250,62,,,,100,100,0,50,0,0"))
        assert not any("sp=O" in s for s in sent)
        await svc.stop()

    asyncio.run(scenario())


def test_service_resumes_after_blip():
    import asyncio

    async def scenario():
        store = Store(":memory:")
        # A cook was running at 265 five minutes ago.
        sid = store.start_session(NOW - 3600)
        store.insert(protocol.Status(set_point=265, pit=262), NOW - 300,
                     session_id=sid)
        svc, tt = _svc(store)
        sent = []
        svc.link.send = lambda line: sent.append(line)
        pushes = []
        svc._push = lambda title, *a, **k: pushes.append(title)
        await svc.start()

        svc._on_line(protocol.frame("HMSU,265,240,,,,60,60,0,60,0,0"))
        assert not any("sp=O" in s for s in sent)             # left running
        kinds = [e["kind"] for e in svc.store.list_events()]
        assert "powerup_resume" in kinds and "powerup_idle" not in kinds
        assert "Cook resumed" in pushes
        await svc.stop()

    asyncio.run(scenario())
