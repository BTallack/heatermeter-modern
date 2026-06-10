"""Tests for the time-to-done predictor."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import predict


def test_linear_rise_eta():
    # 1 degree per minute, starting at 100, target 160 -> 60 min remaining.
    t0 = 1_000_000
    ts = [t0 + i * 60 for i in range(20)]      # 20 minutes of data
    vals = [100 + i for i in range(20)]        # 100..119, +1/min
    p = predict.predict_eta(ts, vals, target=160)
    assert p.eta_seconds is not None
    # last value 119, need 41 more deg at 1 deg/min -> ~2460 s.
    assert abs(p.eta_seconds - 2460) < 60
    assert abs(p.slope_per_min - 1.0) < 0.05
    assert p.confidence == "high"


def test_near_flat_rise_gives_no_eta():
    # A probe barely rising (well under 2 deg/hr) must NOT quote an absurd ETA.
    t0 = 0
    ts = [t0 + i * 60 for i in range(30)]            # 30 minutes
    vals = [72 + i * 0.001 for i in range(30)]       # ~0.06 deg/hr
    p = predict.predict_eta(ts, vals, target=203)
    assert p.eta_seconds is None
    assert p.confidence == "low"


def test_scurve_models_stall():
    # Synthesise meat heating toward a 275F pit via Newton's law:
    # T(t) = 275 - (275-40)*exp(-k t). Use a slow k and a shorter observation
    # window so the data stops BEFORE the target (a real in-progress cook), and
    # confirm the S-curve fit recovers the time to reach 203F closely.
    import math
    env = 275.0
    T0 = 40.0
    k = 0.0001    # per second (slow cook)
    ts = [i * 60.0 for i in range(90)]    # 90 minutes of minute samples
    vals = [env - (env - T0) * math.exp(-k * t) for t in ts]
    target = 203.0
    # Sanity: data must stop below the target for a real ETA.
    assert vals[-1] < target, f"test data overshot: {vals[-1]}"
    p = predict.predict_scurve(ts, vals, target, env_temp=env)
    assert p is not None
    assert p.model == "scurve"
    # True time to target:
    t_true = -math.log((env - target) / (env - T0)) / k
    eta_true = t_true - ts[-1]
    assert eta_true > 0
    # S-curve estimate within 10% of truth.
    assert abs(p.eta_seconds - eta_true) < eta_true * 0.10
    assert p.eta_low is not None and p.eta_high is not None
    assert p.eta_low < p.eta_seconds < p.eta_high


def test_scurve_returns_none_when_target_above_env():
    import math
    env = 200.0
    vals = [env - (env - 40) * math.exp(-0.0003 * (i * 60)) for i in range(60)]
    ts = [i * 60.0 for i in range(60)]
    # Target 210 is above the 200 cooker temp -> unreachable by this model.
    assert predict.predict_scurve(ts, vals, 210.0, env_temp=env) is None


def test_predict_dispatch_prefers_scurve_for_food():
    import math
    env = 250.0
    ts = [i * 60.0 for i in range(120)]
    vals = [env - (env - 40) * math.exp(-0.00025 * t) for t in ts]
    p = predict.predict(ts, vals, 203.0, env_temp=env)
    assert p.model == "scurve"
    # Without env, falls back to linear.
    p2 = predict.predict(ts, vals, 203.0, env_temp=None)
    assert p2.model == "linear"


def test_already_at_target():
    ts = [i * 60 for i in range(10)]
    vals = [200 + i for i in range(10)]
    p = predict.predict_eta(ts, vals, target=150)
    assert p.eta_seconds == 0.0


def test_falling_no_eta():
    ts = [i * 60 for i in range(10)]
    vals = [200 - i for i in range(10)]   # cooling
    p = predict.predict_eta(ts, vals, target=250)
    assert p.eta_seconds is None
    assert p.slope_per_min < 0


def test_too_few_points():
    p = predict.predict_eta([0, 60], [100, 101], target=200)
    assert p.eta_seconds is None
    assert p.confidence == "none"


def test_ignores_none_values():
    t0 = 0
    ts = [t0 + i * 60 for i in range(12)]
    vals = [None, None] + [100 + i for i in range(10)]  # probe plugged in late
    p = predict.predict_eta(ts, vals, target=130)
    assert p.eta_seconds is not None
    assert p.slope_per_min > 0


def test_no_target():
    p = predict.predict_eta([0, 60, 120], [100, 110, 120], target=None)
    assert p.eta_seconds is None
    assert p.confidence == "none"


def test_stalled_flag_widens_band_and_lowers_confidence():
    # A healthy climb, but the watcher says the probe is stalled: the estimate
    # keeps the model ETA as the optimistic bound, stretches the pessimistic
    # bound, drops confidence, and flags it.
    ts = [i * 60 for i in range(20)]
    vals = [100 + i for i in range(20)]
    p = predict.predict(ts, vals, target=160, stalled=True)
    assert p.stalled is True and p.model == "stall"
    assert p.confidence == "low"
    assert p.eta_seconds is not None
    assert p.eta_high > p.eta_seconds * 2

def test_stalled_with_no_estimable_eta():
    # Flat line in the stall: no honest ETA, but the flag still comes through.
    ts = [i * 60 for i in range(20)]
    vals = [160.0] * 20
    p = predict.predict(ts, vals, target=203, stalled=True)
    assert p.stalled is True and p.model == "stall"
    assert p.eta_seconds is None

def test_not_stalled_unchanged():
    ts = [i * 60 for i in range(20)]
    vals = [100 + i for i in range(20)]
    p = predict.predict(ts, vals, target=160, stalled=False)
    assert p.stalled is False and p.model in ("linear", "scurve")
