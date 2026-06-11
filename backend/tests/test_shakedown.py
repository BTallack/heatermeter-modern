"""The shakedown cook: one scripted end-to-end pork butt, no hardware.

Drives the REAL service through a complete low-and-slow cook arc by feeding
synthetic board frames at 30-second steps: pit ramp-up, a guided cook start,
the climb, a 50-minute evaporative stall, the wrap, the breakout, target
reached, the probe pulled (cook completion), and a fuel-depletion tail. Then
asserts the whole intelligence stack reacted in order:

  guided milestones -> stall detection -> wrap confirm -> predictions logged ->
  target + pull events -> cook completed -> fuel-low alert -> printable report.

This is the closest thing to a live cook that runs in CI, and it exercises the
exact code paths a real cook will, end to end through ``_on_line``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol, report
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store

STEP = 30.0   # one synthetic frame every 30 cook-seconds


def _mk_service():
    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    tt = [1_000_000.0]
    svc.time_fn = lambda: tt[0]
    # ntfy "configured" so the ETA/notification paths run; capture the pushes.
    svc.notify_effective_config = lambda: {"enabled": True, "topic": "t"}
    pushes = []
    svc._push = lambda title, msg, **k: pushes.append((title, msg))
    sent = []
    svc.link.send = lambda line: sent.append(line)
    # Make the cook-done confirmation window fit the 30s frame cadence.
    svc.save_cookdone({"enabled": True, "grace_secs": 180,
                       "on_complete": "notify"})
    svc._fuel.set_config({"alert_duty": 85, "alert_hold_secs": 300})
    return svc, tt, pushes, sent


def _frame(svc, tt, *, sp, pit, food1, fan, mode=2, lid=0):
    f1 = "" if food1 is None else f"{food1:.1f}"
    svc._on_line(protocol.frame(
        f"HMSU,{sp},{pit:.1f},{f1},,72.0,{fan},{fan},{lid},{fan},0,{mode}"))
    tt[0] += STEP


def test_full_shakedown_cook():
    svc, tt, pushes, sent = _mk_service()

    # ---- start the guided cook (configures pit 250 + food1 target 203) ----
    r = svc.start_guided_cook("pork_butt", "food1")
    assert r["ok"], r
    # The real board echoes the configured alarms back as $HMAL; the stubbed
    # link cannot, so simulate the echo (targets drive predictions + cook-done).
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))

    # ---- phase 1: pit ramps 70->250 over 30 min; meat starts at 40 --------
    for i in range(60):
        pit = 70 + (250 - 70) * (i + 1) / 60
        _frame(svc, tt, sp=250, pit=pit, food1=40 + i * 0.5, fan=60, mode=0)

    # ---- phase 2: the climb, 70 -> 150 over ~2.2h --------------------------
    food = 70.0
    while food < 150:
        food += 0.55          # ~66 deg/hr
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=35)
    assert "spritz" in svc.guided.fired          # 140-degree milestone hit

    # ---- phase 3: the stall - 50 min pinned at ~151 ------------------------
    for i in range(100):
        food = 151 + (i % 3) * 0.1               # wobble, no net rise
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=40)
    assert svc._probewatch._ch["food1"].stalled is True
    assert "wrap" in svc.guided.fired            # stall + >=150 -> wrap prompt
    assert svc.guided.wrap_pending is True

    # ---- the user wraps it --------------------------------------------------
    assert svc.confirm_guided_wrap()["ok"]
    assert svc.guided.wrapped is True

    # ---- phase 4: breakout - wrapped meat climbs 151 -> 203 over ~2h --------
    food = 151.0
    while food < 203.5:
        food += 0.22                              # ~26 deg/hr post-wrap
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=45)
    # The board rings the high alarm at the crossing ($HMAL value suffixed H).
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203H,-1,-1,-1,-1"))
    _frame(svc, tt, sp=250, pit=249, food1=food, fan=45)
    assert svc.guided.done is True               # pull milestone fired
    assert svc._probewatch._ch["food1"].stalled is False

    # ---- phase 5: probe pulled; falls toward ambient and stays out ---------
    food = 203.0
    for _ in range(20):                           # 10 min out of the meat
        food = max(80.0, food - 12)
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=45)
    session = svc.store.list_sessions()[0]
    assert session.get("completed_ts"), "cook completion did not fire"

    # ---- phase 6: fire burns down - blower pegged for 10+ minutes ----------
    for _ in range(25):
        _frame(svc, tt, sp=250, pit=243, food1=None, fan=95)
    assert svc._fuel.alerted is True

    # ---- the ledger tells the whole story ----------------------------------
    kinds = [e["kind"] for e in svc.store.list_events()]
    for expected in ("guided", "stall_start", "wrap", "stall_end", "target",
                     "prediction", "probe_done", "cook_complete", "fuel_low"):
        assert expected in kinds, f"missing {expected} in ledger: {set(kinds)}"
    # Forecasts were throttled, not spammed (a multi-hour cook, 10-min spacing).
    assert 3 <= kinds.count("prediction") <= 60

    # Pushes covered the milestones a pitmaster cares about.
    titles = " | ".join(t for t, _ in pushes)
    assert "Pork Butt" in titles                 # guided prompts
    assert "Add fuel" in titles                  # fuel alert
    assert "Cook complete" in titles

    # ---- and the report renders with prediction accuracy -------------------
    sid = session["id"]
    cols = svc.store.history_columns(None, 2000, sid)
    evs = svc.store.list_events(session_id=sid)
    page = report.build_report_html(session, cols, evs,
                                    svc.store.list_notes(sid))
    assert "Prediction accuracy" in page
    assert "first forecast" in page
    assert "Timeline" in page and "<svg" in page


def test_shakedown_probe_check_does_not_end_cook():
    """Mid-cook doneness check: probe pulled briefly then reinserted into a
    cooler spot must NOT complete the cook (the Meater-style nuance)."""
    svc, tt, pushes, sent = _mk_service()
    svc.start_guided_cook("pork_butt", "food1")
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))  # board echo
    # Get the meat to temp.
    food = 150.0
    for _ in range(60):
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=40)
        food += 1.0   # quick scripted climb to 210
    # Pull the probe to check (drops sharply)...
    for _ in range(4):
        food -= 25
        _frame(svc, tt, sp=250, pit=249, food1=max(food, 100), fan=40)
    # ...then reinsert into a cooler part of the butt: it RISES again.
    for _ in range(20):
        food = min(195.0, max(food, 100) + 5)
        _frame(svc, tt, sp=250, pit=249, food1=food, fan=40)
    session = svc.store.list_sessions()[0]
    assert not session.get("completed_ts"), \
        "a probe check wrongly completed the cook"
