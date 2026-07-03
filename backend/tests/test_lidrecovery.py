"""Tests for the smart lid-open recovery detector (pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import lidrecovery


def drive(det, samples):
    """Feed (ts, lid_countdown, pit, set_point) tuples; return [(ts, actions)]."""
    out = []
    for ts, lid, pit, sp in samples:
        r = det.update(ts, lid, pit, sp)
        if r["actions"]:
            out.append((ts, r["actions"]))
    return out


def kinds(events):
    return [a["type"] for _, acts in events for a in acts]


def test_sanitize_clamps_and_ignores_retired_keys():
    d = lidrecovery.sanitize(None)
    assert d == {"enabled": True, "recover_delta": 4.0, "min_armed_secs": 5}
    # Retired ramping keys are silently ignored; real fields clamp.
    d2 = lidrecovery.sanitize({"enabled": False, "recover_delta": 999,
                               "min_armed_secs": -3, "start_pct": 20, "ramp_secs": 99})
    assert d2["enabled"] is False
    assert d2["recover_delta"] == 50.0 and d2["min_armed_secs"] == 0
    assert "start_pct" not in d2 and "ramp_secs" not in d2


def test_disabled_does_nothing():
    det = lidrecovery.LidRecovery({"enabled": False})
    events = drive(det, [
        (0, 240, 225, 225), (10, 230, 205, 225), (11, 229, 215, 225),
    ])
    assert events == [] and det.state == "idle"


def test_undershoot_recovery_cancels_lid_timer():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Leaky cooker: pit craters BELOW setpoint while the lid is open, then climbs.
    samples = [
        (0, 240, 225, 225),
        (4, 236, 211, 225),
        (8, 232, 205, 225),   # the low
        (10, 230, 207, 225),  # +2, below recover_delta
        (12, 228, 210, 225),  # +5 off the low, armed >= 5s, still < setpoint
    ]
    events = drive(det, samples)
    assert kinds(events) == ["cancel_lid"]
    assert events[0][0] == 12
    assert det.state == "idle"


def test_overshoot_recovery_stands_down():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Well-sealed cooker (kamado): opening the lid flares the coals; on close the
    # pit climbs back ABOVE the setpoint. We must NOT intervene (no cancel, no
    # fan, no setpoint re-send) - let the firmware ride it out, fan off.
    samples = [
        (0, 240, 250, 245),
        (6, 234, 235, 245),   # dipped while open
        (12, 228, 250, 245),  # climbed back past the setpoint -> overshoot
    ]
    events = drive(det, samples)
    assert events == []
    assert det.state == "idle"


def test_no_recovery_lets_firmware_timer_run():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Lid stays open, pit keeps falling; firmware countdown ends before recovery.
    samples = [(t, 240 - t, 225 - t * 2, 225) for t in range(0, 30, 2)]
    samples.append((30, 0, 165, 225))
    assert drive(det, samples) == []
    assert det.state == "idle"


def test_brief_blip_below_min_armed_secs_ignored():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    samples = [
        (0, 240, 225, 225),
        (1, 239, 210, 225),   # low
        (3, 237, 220, 225),   # +10 but only 3s armed -> ignored
    ]
    assert drive(det, samples) == []
    assert det.state == "armed"


def test_manual_mode_ignored():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    samples = [(0, 240, 225, -50), (6, 234, 205, -50), (12, 228, 210, -50)]
    assert drive(det, samples) == []
    assert det.state == "idle"


def test_firmware_resolves_lid_stands_down():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    det.update(0, 240, 225, 225)          # armed
    det.update(4, 236, 210, 225)          # tracking the dip
    r = det.update(30, 0, 214, 225)       # firmware's own timer ended (lid=0)
    assert r["actions"] == [] and det.state == "idle"
