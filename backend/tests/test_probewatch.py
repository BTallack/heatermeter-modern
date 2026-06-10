"""Unit tests for the probe-health + stall watcher (pure, no hardware)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import probewatch
from heatermeterd.probewatch import ProbeWatch


def _types(events, ch=None):
    return [e["type"] for e in events if ch is None or e["channel"] == ch]


# -- sanitize ---------------------------------------------------------------

def test_sanitize_defaults_and_clamps():
    d = probewatch.sanitize(None)
    assert d["enabled"] is True and d["stall_enabled"] is True
    d = probewatch.sanitize({"dropout_secs": 0.1, "stall_low": 200, "stall_high": 150,
                             "enabled": False})
    assert d["dropout_secs"] >= 2.0           # clamped up
    assert d["stall_low"] < d["stall_high"]   # swapped back into order
    assert d["enabled"] is False


# -- disconnect / reconnect -------------------------------------------------

def test_disconnect_only_after_seen_and_debounce():
    w = ProbeWatch({"dropout_secs": 20})
    # Never-seen channel stays quiet even though it reads None forever.
    assert w.update(0, {"food1": None}) == []
    # food1 becomes present, then drops.
    assert w.update(1, {"food1": 120.0}) == []
    assert w.update(5, {"food1": None}) == []          # missing, within debounce
    assert w.update(20, {"food1": None}) == []         # still within 20s of t=5
    ev = w.update(26, {"food1": None})                 # 21s missing -> fire
    assert _types(ev, "food1") == ["disconnect"]
    # Does not fire again while still missing.
    assert w.update(40, {"food1": None}) == []


def test_pit_dropout_critical_during_cook_else_warning():
    w = ProbeWatch({"dropout_secs": 5})
    w.update(0, {"pit": 225.0}, pid_mode=2)
    w.update(2, {"pit": None}, pid_mode=2)
    ev = w.update(10, {"pit": None}, pid_mode=2)       # cooking (at-temp)
    assert ev[0]["type"] == "disconnect" and ev[0]["severity"] == "critical"

    w2 = ProbeWatch({"dropout_secs": 5})
    w2.update(0, {"pit": 70.0}, pid_mode=4)            # Off
    w2.update(2, {"pit": None}, pid_mode=4)
    ev2 = w2.update(10, {"pit": None}, pid_mode=4)
    assert ev2[0]["severity"] == "warning"


def test_food_target_dropout_is_warning_else_info():
    w = ProbeWatch({"dropout_secs": 5})
    w.update(0, {"food1": 140.0}, targets={"food1": True})
    w.update(2, {"food1": None}, targets={"food1": True})
    ev = w.update(10, {"food1": None}, targets={"food1": True})
    assert ev[0]["severity"] == "warning"

    w2 = ProbeWatch({"dropout_secs": 5})
    w2.update(0, {"food2": 140.0})
    w2.update(2, {"food2": None})
    ev2 = w2.update(10, {"food2": None})
    assert ev2[0]["severity"] == "info"


def test_reconnect_after_drop():
    w = ProbeWatch({"dropout_secs": 5})
    w.update(0, {"food1": 100.0})
    w.update(2, {"food1": None})
    assert _types(w.update(10, {"food1": None}), "food1") == ["disconnect"]
    ev = w.update(12, {"food1": 105.0})
    assert _types(ev, "food1") == ["reconnect"]


# -- implausible-reading fault ---------------------------------------------

def test_fault_debounced_and_clears():
    w = ProbeWatch({"implausible_high": 1000})
    assert w.update(0, {"pit": 1500.0}) [0]["type"] == "fault"
    assert w.update(1, {"pit": 1600.0}) == []          # still faulted, no repeat
    assert w.update(2, {"pit": 225.0}) == []           # back in range, clears
    assert w.update(3, {"pit": 2000.0})[0]["type"] == "fault"  # new episode fires


# -- stall detection --------------------------------------------------------

def test_stall_start_and_end():
    w = ProbeWatch({"stall_window_secs": 600, "stall_enter_rate": 6,
                    "stall_exit_rate": 14, "stall_low": 150, "stall_high": 180})
    starts = ends = 0
    # Plateau at ~160 for ~25 min -> stall_start fires exactly once.
    for tt in range(0, 1500, 60):
        for e in w.update(tt, {"food1": 160.0}):
            if e["type"] == "stall_start":
                starts += 1
    assert starts == 1
    # Break out: a rapid rise should end the stall once the flat samples age out.
    base = 160.0
    for tt in range(1500, 2700, 60):
        base += 8.0  # ~ +480 deg/hr, well over the 14 deg/hr exit rate
        for e in w.update(tt, {"food1": base}):
            if e["type"] == "stall_end":
                ends += 1
    assert ends >= 1


def test_no_stall_for_fast_climb():
    w = ProbeWatch({"stall_window_secs": 600})
    seen = False
    base = 150.0
    for tt in range(0, 1400, 60):
        base += 5.0  # ~300 deg/hr, never stalls
        if "stall_start" in _types(w.update(tt, {"food1": base}), "food1"):
            seen = True
    assert not seen


def test_pit_does_not_stall():
    w = ProbeWatch({"stall_window_secs": 600})
    for tt in range(0, 1400, 60):
        ev = w.update(tt, {"pit": 160.0})
        assert "stall_start" not in _types(ev, "pit")


# -- service integration (real read path, no hardware) ----------------------

def test_service_emits_dropout_through_on_line():
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc.save_probewatch({"dropout_secs": 5, "stall_enabled": False})
    events = []
    svc._emit = lambda e: events.append(e)
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]

    # food1 reads 140, then disappears; after the debounce a disconnect fires.
    svc._on_line(protocol.frame("HMSU,225,198,140,,,0,0,0,0,0,4"))
    tt[0] = 1003.0
    svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))   # missing < 5s
    assert not any(e.get("type") == "probe_event" for e in events)
    tt[0] = 1012.0
    svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))   # > 5s -> dropout

    pe = [e for e in events if e.get("type") == "probe_event"]
    assert pe and pe[-1]["kind"] == "disconnect" and pe[-1]["channel"] == "food1"
    assert svc.probe_health.get("food1") == "disconnected"

    # And it recovers.
    tt[0] = 1015.0
    svc._on_line(protocol.frame("HMSU,225,198,141,,,0,0,0,0,0,4"))
    assert svc.probe_health.get("food1") == "ok"
