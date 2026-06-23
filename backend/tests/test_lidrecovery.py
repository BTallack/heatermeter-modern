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


def types_of(events):
    return [a["type"] for _, acts in events for a in acts]


def test_sanitize_clamps_and_defaults():
    d = lidrecovery.sanitize(None)
    assert d["enabled"] is True
    assert d["start_pct"] == 15 and d["ramp_secs"] == 60
    d2 = lidrecovery.sanitize({"enabled": False, "recover_delta": 999,
                               "start_pct": -5, "ramp_secs": 99999,
                               "step_pct": 0})
    assert d2["enabled"] is False
    assert d2["recover_delta"] == 50.0     # clamped high
    assert d2["start_pct"] == 0            # clamped low
    assert d2["ramp_secs"] == 600          # clamped high
    assert d2["step_pct"] == 1             # clamped low


def test_disabled_does_nothing():
    det = lidrecovery.LidRecovery({"enabled": False})
    events = drive(det, [
        (0, 240, 225, 225), (1, 239, 215, 225), (10, 230, 205, 225),
        (11, 229, 215, 225),  # would normally recover
    ])
    assert events == []
    assert det.state == "idle"


def test_recovery_cancels_lid_and_starts_gentle_ramp():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "start_pct": 15,
                                   "ramp_secs": 60, "min_armed_secs": 5})
    # Lid opens at t=0 (sp 225, pit 225); pit dives while open, bottoms at 205
    # around t=8, then climbs as the lid is closed.
    samples = [
        (0, 240, 225, 225),
        (2, 238, 218, 225),
        (4, 236, 211, 225),
        (6, 234, 207, 225),
        (8, 232, 205, 225),   # the low
        (10, 230, 207, 225),  # +2 off min, below recover_delta
        (12, 228, 210, 225),  # +5 off min -> recovery fires (armed >= 5s)
    ]
    events = drive(det, samples)
    # First action burst is at t=12: cancel_lid then a gentle manual start.
    assert events[0][0] == 12
    kinds = [a["type"] for a in events[0][1]]
    assert kinds == ["cancel_lid", "manual"]
    start = [a for a in events[0][1] if a["type"] == "manual"][0]
    assert start["pct"] == 15   # gentle start
    assert det.state == "ramping"


def test_ramp_climbs_to_full_then_resumes_pid():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "start_pct": 20,
                                   "ramp_secs": 60, "min_armed_secs": 5,
                                   "step_pct": 10})
    samples = [(0, 240, 225, 225), (6, 234, 205, 225), (12, 228, 210, 225)]
    # Drive the dip + recovery, then keep the lid closed (lid=0) with the pit
    # still climbing but below setpoint so the ramp runs its course.
    for t in range(14, 80, 2):
        samples.append((t, 0, 211, 225))
    events = drive(det, samples)
    kinds = types_of(events)
    assert kinds[0] == "cancel_lid"
    manual_pcts = [a["pct"] for _, acts in events for a in acts
                   if a["type"] == "manual"]
    # Ramp is monotonic non-decreasing, starts gentle, ends at full.
    assert manual_pcts[0] == 20
    assert manual_pcts == sorted(manual_pcts)
    assert manual_pcts[-1] == 100
    # Final action hands control back to PID at the original setpoint.
    assert kinds[-1] == "resume_auto"
    last = [a for _, acts in events for a in acts if a["type"] == "resume_auto"][-1]
    assert last["setpoint"] == 225
    assert det.state == "idle"


def test_resume_pid_early_when_pit_reaches_setpoint():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "start_pct": 20,
                                   "ramp_secs": 60, "min_armed_secs": 5})
    samples = [(0, 240, 225, 225), (6, 234, 205, 225), (12, 228, 210, 225)]
    # Pit shoots back to setpoint mid-ramp -> hand to PID immediately.
    samples.append((20, 0, 225, 225))
    events = drive(det, samples)
    kinds = types_of(events)
    assert kinds[-1] == "resume_auto"
    assert det.state == "idle"


def test_recovery_above_setpoint_skips_ramp():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Lid barely cracked: pit dips a touch but is already back above setpoint
    # by the time recovery is detected -> no fan push, straight to PID.
    samples = [
        (0, 240, 226, 220),
        (6, 234, 222, 220),   # low 222
        (12, 228, 227, 220),  # +5 off min, and >= setpoint
    ]
    events = drive(det, samples)
    kinds = types_of(events)
    assert kinds == ["cancel_lid", "resume_auto"]
    assert det.state == "idle"


def test_no_recovery_lets_firmware_timer_run():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Lid stays open the whole time (pit keeps falling), then the firmware
    # countdown ends (lid=0) before any recovery. We must do nothing.
    samples = [(t, 240 - t, 225 - t * 2, 225) for t in range(0, 30, 2)]
    samples.append((30, 0, 165, 225))   # firmware timer expired
    events = drive(det, samples)
    assert events == []
    assert det.state == "idle"


def test_brief_blip_below_min_armed_secs_ignored():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # A rise happens within the dwell window -> ignored (could be sensor noise).
    samples = [
        (0, 240, 225, 225),
        (1, 239, 210, 225),   # low
        (3, 237, 220, 225),   # +10 but only 3s armed -> ignored
    ]
    events = drive(det, samples)
    assert events == []
    assert det.state == "armed"


def test_manual_mode_setpoint_not_touched():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "min_armed_secs": 5})
    # Negative/zero setpoint == manual fan mode; lid recovery must stay out.
    samples = [(0, 240, 225, -50), (6, 234, 205, -50), (12, 228, 210, -50)]
    events = drive(det, samples)
    assert events == []
    assert det.state == "idle"


def test_second_lid_open_during_ramp_rearms():
    det = lidrecovery.LidRecovery({"recover_delta": 4.0, "start_pct": 20,
                                   "ramp_secs": 60, "min_armed_secs": 5})
    samples = [(0, 240, 225, 225), (6, 234, 205, 225), (12, 228, 210, 225)]
    events = drive(det, samples)
    assert det.state == "ramping"
    # Lid opens again mid-ramp -> abandon ramp, re-arm on the new dip.
    r = det.update(16, 240, 208, 225)
    assert r["actions"] == []
    assert det.state == "armed"
