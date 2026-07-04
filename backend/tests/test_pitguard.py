"""Tests for the Pit Guard (context-aware pit low/high alerting, pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import pitguard

CFG = {"enabled": True, "low_band": 30.0, "low_dwell_secs": 300,
       "fire_fan_pct": 90, "fire_dwell_secs": 300, "lid_grace_secs": 600,
       "high_margin": 40.0, "high_dwell_secs": 120}


def drive(g, samples):
    """samples: (ts, sp, pit, out, lid[, mode]). Returns [(ts, kind), ...]."""
    fired = []
    for s in samples:
        ts, sp, pit, out, lid = s[:5]
        mode = s[5] if len(s) > 5 else None
        for ev in g.update(ts, sp, pit, out, lid, mode)["events"]:
            fired.append((ts, ev["kind"]))
    return fired


def test_sanitize_defaults_and_clamps():
    d = pitguard.sanitize(None)
    assert d["low_band"] == 30.0 and d["high_margin"] == 40.0
    d2 = pitguard.sanitize({"low_band": 1, "fire_fan_pct": 200,
                            "high_dwell_secs": 1, "enabled": False})
    assert d2["low_band"] == 10.0 and d2["fire_fan_pct"] == 100
    assert d2["high_dwell_secs"] == 30 and d2["enabled"] is False


def test_quiet_during_initial_climb():
    g = pitguard.PitGuard(CFG)
    # Cold start: pit far below set for a long time -> no alarms (not armed).
    samples = [(t, 250, 80 + t / 60, 100, 0) for t in range(0, 3600, 30)]
    fired = drive(g, samples)
    assert [k for _, k in fired if k in ("pit_low", "fire_dying")] == []


def test_arms_via_pid_mode_or_proximity():
    g = pitguard.PitGuard(CFG)
    g.update(0, 250, 100, 50, 0, pitguard.PIDMODE_NORMAL)   # fork firmware says at temp
    assert g.status()["armed"]
    g2 = pitguard.PitGuard(CFG)
    g2.update(0, 250, 247, 30, 0, None)                     # stock: within epsilon
    assert g2.status()["armed"]


def test_lid_open_and_grace_suppress_low():
    g = pitguard.PitGuard(CFG)
    g.update(0, 265, 265, 20, 0)             # at temp -> armed
    # Lid opens; pit craters far below the band. Then lid closes; pit recovers
    # slowly through the grace window. No pit_low at any point.
    samples = [(t, 265, 180, 0, 30) for t in range(10, 40, 10)]          # open
    samples += [(t, 265, 180 + (t - 40) / 6, 5, 0) for t in range(40, 520, 30)]  # grace climb
    fired = drive(g, samples)
    assert fired == []


def test_low_warning_after_dwell_then_fire_dying_escalation():
    g = pitguard.PitGuard(CFG)
    g.update(0, 265, 265, 20, 0)             # armed
    fired = []
    # Fire fades: pit sinks below 235 (set-30) and stays; PID output pegs.
    t = 10
    for _ in range(40):                       # 20 min of low + pegged output
        for ev in g.update(t, 265, 220, 100, 0)["events"]:
            fired.append((t, ev["kind"]))
        t += 30
    kinds = [k for _, k in fired]
    assert kinds == ["pit_low", "fire_dying"]
    warn_ts = fired[0][0]; crit_ts = fired[1][0]
    assert warn_ts - 10 >= 300               # warning waited out the dwell
    assert crit_ts - warn_ts >= 300          # escalation waited its own dwell
    st = g.status()
    assert st["low"] and st["fire_dying"]


def test_low_without_pegged_fan_never_escalates():
    g = pitguard.PitGuard(CFG)
    g.update(0, 265, 265, 20, 0)
    fired = drive(g, [(t, 265, 225, 40, 0) for t in range(10, 1800, 30)])
    assert [k for _, k in fired] == ["pit_low"]   # warning only; fan not maxed


def test_recovery_rearms_low_warning():
    g = pitguard.PitGuard(CFG)
    g.update(0, 265, 265, 20, 0)
    drive(g, [(t, 265, 220, 50, 0) for t in range(10, 400, 30)])   # -> pit_low
    assert g.status()["low"]
    g.update(500, 265, 262, 20, 0)            # back near set -> re-arm
    assert not g.status()["low"]
    fired = drive(g, [(t, 265, 220, 50, 0) for t in range(600, 1000, 30)])
    assert [k for _, k in fired] == ["pit_low"]   # a fresh episode fires again


def test_setpoint_bump_rearms_climb():
    g = pitguard.PitGuard(CFG)
    g.update(0, 225, 225, 20, 0)              # at temp at 225
    # Big bump to 350: pit is now 125 "below set" but that's a fresh climb.
    fired = drive(g, [(t, 350, 225 + t / 10, 100, 0) for t in range(10, 900, 30)])
    assert [k for _, k in fired if k == "pit_low"] == []


def test_overtemp_parity_with_old_detector():
    g = pitguard.PitGuard(CFG)
    fired = []
    fired += drive(g, [(0, 225, 225, 0, 0), (1, 225, 285, 0, 0)])
    assert fired == []                        # timer started, not elapsed
    fired += drive(g, [(131, 225, 288, 0, 0)])
    assert [k for _, k in fired] == ["overtemp"]
    fired += drive(g, [(151, 225, 290, 0, 0)])
    assert len(fired) == 1                    # no repeat while hot
    # Settle within half margin -> re-arm; fresh excursion fires again.
    fired += drive(g, [(200, 225, 230, 0, 0), (201, 225, 285, 0, 0),
                       (331, 225, 285, 0, 0)])
    assert [k for _, k in fired] == ["overtemp", "overtemp"]


def test_last_nights_kamado_flare_still_flags_overtemp():
    # Replay of the 07-02 shape: set 265, lid open dip to 180, close, flare to
    # 310+, sustained. Low side must stay quiet (lid + grace), high side fires.
    g = pitguard.PitGuard(CFG)
    g.update(0, 265, 265, 5, 0)               # cruising -> armed
    samples = [(20, 265, 240, 0, 30), (40, 265, 180, 0, 20)]      # lid open
    samples += [(60, 265, 291, 7, 0), (120, 265, 307, 7, 0),
                (200, 265, 311, 7, 0), (260, 265, 319, 0, 0)]     # flare
    fired = drive(g, samples)
    assert [k for _, k in fired] == ["overtemp"]


def test_off_and_manual_are_silent():
    g = pitguard.PitGuard(CFG)
    fired = drive(g, [(t, 0, 200, 100, 0) for t in range(0, 1200, 60)])
    fired += drive(g, [(t, -50, 200, 100, 0) for t in range(1200, 2400, 60)])
    assert fired == []


def test_disabled_resets_and_stays_quiet():
    g = pitguard.PitGuard({**CFG, "enabled": False})
    fired = drive(g, [(t, 265, 200, 100, 0) for t in range(0, 3600, 30)])
    assert fired == [] and g.status() == {"low": False, "fire_dying": False,
                                          "overtemp": False, "armed": False}
