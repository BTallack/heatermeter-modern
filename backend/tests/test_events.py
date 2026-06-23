"""Tests for auto timeline events: store roundtrip + service edge detection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


# -- store ------------------------------------------------------------------

def test_store_events_roundtrip_and_filtering():
    st = Store(":memory:")
    st.add_event(100.0, "lid_open", session_id=1, label="Lid open")
    st.add_event(200.0, "setpoint", session_id=1, label="Set 250°", value=250.0)
    st.add_event(300.0, "stall_start", session_id=2, channel="food1")
    all_ev = st.list_events()
    assert [e["kind"] for e in all_ev] == ["lid_open", "setpoint", "stall_start"]
    assert st.list_events(session_id=1)[-1]["value"] == 250.0
    assert [e["kind"] for e in st.list_events(since=250)] == ["stall_start"]
    # delete_session removes its events
    st.delete_session(1)
    assert [e["session_id"] for e in st.list_events()] == [2]


# -- service edge detection (real read path) ---------------------------------

def _svc():
    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    return svc, tt


def _hmsu(sp="225", pit="198", lid="0"):
    # HMSU fields: SetPoint,Pit,Food1,Food2,Ambient,OutputPct,OutputAvg,
    #              LidOpenCountdown,FanPct,ServoPct,PidMode
    return protocol.frame(f"HMSU,{sp},{pit},140,,,0,0,{lid},0,0,4")


def test_lid_and_setpoint_edges_recorded():
    svc, tt = _svc()
    svc._on_line(_hmsu(sp="225", lid="0"))      # baseline; no events yet
    kinds = [e["kind"] for e in svc.store.list_events()]
    assert "setpoint" not in kinds and "lid_open" not in kinds

    tt[0] += 1
    svc._on_line(_hmsu(sp="225", lid="120"))    # lid opens
    tt[0] += 1
    svc._on_line(_hmsu(sp="225", lid="0"))      # lid closes
    tt[0] += 1
    svc._on_line(_hmsu(sp="250", lid="0"))      # setpoint change

    kinds = [e["kind"] for e in svc.store.list_events()]
    assert kinds.count("lid_open") == 1
    assert kinds.count("lid_closed") == 1
    assert kinds.count("setpoint") == 1
    sp_ev = [e for e in svc.store.list_events() if e["kind"] == "setpoint"][0]
    assert sp_ev["value"] == 250.0 and "250" in sp_ev["label"]
    # No repeats while values hold steady.
    tt[0] += 1
    svc._on_line(_hmsu(sp="250", lid="0"))
    assert len(svc.store.list_events()) == 3


def test_alarm_edge_records_target_event():
    svc, tt = _svc()
    svc._on_line(_hmsu())
    # Food 1 high alarm rings (firmware suffixes the value with H).
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203H,-1,-1,-1,-1"))
    tt[0] += 1
    svc._on_line(_hmsu())   # alarms are checked on the HMSU tick
    evs = [e for e in svc.store.list_events() if e["kind"] == "target"]
    assert evs and evs[0]["channel"] == "food1"
    assert "reached target" in evs[0]["label"]


def test_food_target_change_records_event():
    svc, tt = _svc()
    svc._on_line(_hmsu())                                  # baseline; targets observed
    # Set Food 1 target to 203 (non-ringing high alarm = a cook target).
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
    tt[0] += 1
    svc._on_line(_hmsu())                                  # tick -> "target set"
    evs = [e for e in svc.store.list_events() if e["kind"] == "food_target"]
    assert evs and evs[0]["channel"] == "food1" and evs[0]["value"] == 203.0
    assert "203" in evs[0]["label"]
    # Change it -> another marker.
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,195,-1,-1,-1,-1"))
    tt[0] += 1
    svc._on_line(_hmsu())
    evs = [e for e in svc.store.list_events() if e["kind"] == "food_target"]
    assert len(evs) == 2 and evs[-1]["value"] == 195.0
    # Steady -> no repeat.
    tt[0] += 1
    svc._on_line(_hmsu())
    evs = [e for e in svc.store.list_events() if e["kind"] == "food_target"]
    assert len(evs) == 2


def test_probe_event_lands_on_timeline():
    svc, tt = _svc()
    svc.save_probewatch({"dropout_secs": 5, "stall_enabled": False})
    svc._on_line(_hmsu())                       # food1 = 140, present
    tt[0] += 3
    svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
    tt[0] += 9
    svc._on_line(protocol.frame("HMSU,225,198,,,,0,0,0,0,0,4"))
    kinds = [e["kind"] for e in svc.store.list_events()]
    assert "disconnect" in kinds


def test_prediction_forecasts_logged_and_throttled():
    svc, tt = _svc()
    # ntfy must be "configured" for the ETA path to run; stub the notify config.
    svc.notify_effective_config = lambda: {"enabled": True, "topic": "t"}
    # Food1 target 160 (high alarm idx 3), rising steadily from 100.
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,160,-1,-1,-1,-1"))
    for i in range(40):
        tt[0] = 1000.0 + i * 60
        f1 = 100 + i * 2
        svc._on_line(protocol.frame(f"HMSU,225,220,{f1},,,0,0,0,0,0,2"))
    preds = [e for e in svc.store.list_events() if e["kind"] == "prediction"]
    # ~40 min of samples with a 10-min throttle -> a handful, not dozens.
    assert 1 <= len(preds) <= 6
    assert all(p["channel"] == "food1" and p["value"] > 1000.0 for p in preds)
    # Forecast value is a plausible done-at epoch (after the sample that made it).
    assert preds[0]["value"] > preds[0]["ts"]
