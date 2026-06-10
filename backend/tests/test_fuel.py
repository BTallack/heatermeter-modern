"""Tests for the charcoal/fuel monitor (synthetic blower-duty curves)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import fuel
from heatermeterd.fuel import FuelMonitor


def test_sanitize_clamps():
    d = fuel.sanitize({"window_secs": 10, "alert_duty": 5})
    assert d["window_secs"] >= 300
    assert d["alert_duty"] >= 20


def test_healthy_fire_no_alert_no_trend():
    m = FuelMonitor({"window_secs": 1200})
    for i in range(0, 2400, 2):
        assert m.update(i, 35 + (i % 7) * 0.5, 225, 225) == []
    s = m.status()
    assert s["alerted"] is False
    assert s["depleting"] is False


def test_depleting_trend_estimates_horizon():
    m = FuelMonitor({"window_secs": 1200, "trend_min_per_hr": 8})
    # Duty climbs 30 -> 60 over 40 minutes (= 45%/hour): clearly depleting.
    for i in range(0, 2400, 2):
        duty = 30 + i / 2400 * 30
        m.update(i, duty, 225, 225)
    s = m.status()
    assert s["depleting"] is True
    assert s["trend_per_hr"] > 8
    assert s["est_secs_to_max"] is not None and 0 < s["est_secs_to_max"] < 12 * 3600


def test_sustained_high_duty_alert_and_recovery():
    m = FuelMonitor({"alert_duty": 85, "alert_hold_secs": 300})
    events = []
    for i in range(0, 280, 2):                    # high but not long enough
        events += m.update(i, 92, 225, 225)
    assert events == []
    for i in range(280, 320, 2):                  # crosses the 300s hold
        events += m.update(i, 92, 225, 225)
    assert [e["type"] for e in events] == ["fuel_low"]
    # No repeat while still high.
    for i in range(320, 400, 2):
        events += m.update(i, 95, 225, 225)
    assert len(events) == 1
    # Recovery (fresh fuel added: duty collapses).
    for i in range(400, 420, 2):
        events += m.update(i, 40, 225, 225)
    assert [e["type"] for e in events] == ["fuel_low", "fuel_ok"]


def test_samples_excluded_off_setpoint_and_lid():
    m = FuelMonitor({"alert_duty": 85, "alert_hold_secs": 10})
    # Startup: pit far below setpoint -> blower pegged is NORMAL, no alert.
    for i in range(0, 120, 2):
        assert m.update(i, 100, 100, 225) == []
    # Lid open: duty spike is normal too.
    for i in range(120, 240, 2):
        assert m.update(i, 100, 224, 225, lid_open=True) == []
    assert m.status()["alerted"] is False


def test_disabled_is_silent():
    m = FuelMonitor({"enabled": False, "alert_duty": 50, "alert_hold_secs": 1})
    for i in range(0, 100, 2):
        assert m.update(i, 100, 225, 225) == []


def test_service_integration_fuel_status_and_alert():
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc._fuel.set_config({"alert_duty": 85, "alert_hold_secs": 4})
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    events = []
    orig = svc._emit
    svc._emit = lambda e: (events.append(e), orig(e))
    # Pit at setpoint, fan pegged at 95% long enough to alert.
    for _ in range(5):
        svc._on_line(protocol.frame("HMSU,225,224,,,,95,95,0,95,0,2"))
        tt[0] += 2.0
    fuel_events = [e for e in events if e.get("type") == "fuel"]
    assert fuel_events and fuel_events[0]["kind"] == "fuel_low"
    assert "fuel_low" in [e["kind"] for e in svc.store.list_events()]
    assert svc.fuel_status()["alerted"] is True
