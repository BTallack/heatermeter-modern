"""Time-to-done prediction.

Estimates how long until a probe reaches a target temperature, from its recent
history. Pure functions over (timestamps, values) so they are trivially testable.

Two models:
* :func:`predict_eta` - robust linear rate-of-rise over a trailing window
  (least-squares slope). Honest and stable; the fallback.
* :func:`predict_scurve` - stall-aware. Meat temperature approaches the cooker
  temperature exponentially (Newton's law of cooling), so the curve flattens
  near the stall. Fitting that exponential captures the slowdown a linear slope
  misses, matching FireBoard Analyze's rise/stall/finish shape.

:func:`predict` dispatches to the S-curve when a cooker (environment) temperature
is known and the model fits, else linear. Each estimate carries seconds
remaining, the current rate (deg/min), a qualitative confidence, the model used,
and an optimistic/pessimistic band (eta_low/eta_high).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Prediction:
    eta_seconds: Optional[float]   # seconds until target, or None if not estimable
    slope_per_min: Optional[float]  # fitted degrees per minute
    confidence: str                # "none" | "low" | "medium" | "high"
    target: Optional[float]
    model: str = "linear"          # "linear" | "scurve" | "stall"
    eta_low: Optional[float] = None   # optimistic bound (seconds)
    eta_high: Optional[float] = None  # pessimistic bound (seconds)
    stalled: bool = False          # probe is in a detected evaporative stall

    def to_dict(self) -> dict:
        return asdict(self)


def _linfit(xs, ys):
    """Least-squares slope+intercept of ys over xs. Returns (slope, intercept)
    or (None, None) if degenerate."""
    n = len(xs)
    if n < 2:
        return None, None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _r_squared(xs, ys, slope, intercept):
    n = len(ys)
    mean_y = sum(ys) / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        return 1.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    return 1.0 - ss_res / ss_tot


def predict_eta(timestamps, values, target,
                window_seconds: float = 900.0,
                min_points: int = 5) -> Prediction:
    """Estimate seconds until *values* reaches *target*.

    timestamps: list of epoch seconds (ascending). values: matching temps; None
    entries (probe unplugged) are skipped. window_seconds: trailing window to fit
    the slope over (default 15 min). Returns a :class:`Prediction`.
    """
    if target is None:
        return Prediction(None, None, "none", target)

    # Keep only valid (ts, value) pairs within the trailing window.
    pairs = [(t, v) for t, v in zip(timestamps, values)
             if v is not None and isinstance(v, (int, float))]
    if len(pairs) < min_points:
        return Prediction(None, None, "none", target)

    t_last, v_last = pairs[-1]
    window = [(t, v) for t, v in pairs if t >= t_last - window_seconds]
    if len(window) < min_points:
        window = pairs[-min_points:]

    xs = [t for t, _ in window]
    ys = [v for _, v in window]
    slope, intercept = _linfit(xs, ys)  # degrees per second
    if slope is None:
        return Prediction(None, None, "none", target)

    slope_per_min = slope * 60.0

    # Already at/above target.
    if v_last >= target:
        return Prediction(0.0, slope_per_min, "high", target)

    # Not rising fast enough to be meaningful. A near-zero slope (probe sitting
    # at ambient, or stalled) extrapolates to an absurd ETA - the original
    # software gated this on a minimum degrees-per-hour. We require at least
    # ~2 deg/hr of rise before we'll quote a time; below that, report no ETA
    # rather than "161511h".
    MIN_SLOPE_PER_MIN = 2.0 / 60.0   # 2 deg/hour
    if slope_per_min < MIN_SLOPE_PER_MIN:
        return Prediction(None, slope_per_min, "low", target)

    eta = (target - v_last) / slope  # seconds

    # Cap absurd projections: nothing on a BBQ takes more than ~48h, and a longer
    # estimate just means the slope is too shallow to trust.
    MAX_ETA = 48 * 3600
    if eta > MAX_ETA:
        return Prediction(None, slope_per_min, "low", target)

    # Confidence from fit quality + how long we've been observing.
    r2 = _r_squared(xs, ys, slope, intercept)
    span = xs[-1] - xs[0]
    if r2 >= 0.9 and span >= window_seconds * 0.6:
        confidence = "high"
    elif r2 >= 0.7:
        confidence = "medium"
    else:
        confidence = "low"

    # A +/-20% band around the linear estimate (the slope is noisy).
    return Prediction(eta, slope_per_min, confidence, target,
                      model="linear", eta_low=eta * 0.8, eta_high=eta * 1.2)


def predict_scurve(timestamps, values, target, env_temp,
                   window_seconds: float = 3600.0, min_points: int = 8):
    """Stall-aware time-to-target using Newton's law of cooling.

    Meat temperature approaches the cooker temperature *env_temp* exponentially:
    ``T(t) = env - (env - T0) * exp(-k*t)``. Near the stall the curve flattens as
    T nears env, so a linear slope badly under-estimates; this model captures it.

    We fit k by linear regression on ``y = ln(env - T)`` vs t (which is linear in
    t with slope -k). Then solve T(t)=target for the ETA. Returns a Prediction
    with ``model="scurve"``, or ``None`` if the data doesn't support the model
    (target at/above env, too few points, non-monotone) so the caller can fall
    back to :func:`predict_eta`.
    """
    if target is None or env_temp is None:
        return None
    # The exponential approach can only reach env_temp asymptotically; if the
    # target is above the cooker temperature it is physically unreachable by this
    # model. Leave it to the linear model (e.g. cooker still heating up).
    if target >= env_temp:
        return None

    pairs = [(t, v) for t, v in zip(timestamps, values)
             if v is not None and isinstance(v, (int, float))]
    if len(pairs) < min_points:
        return None
    t_last, v_last = pairs[-1]
    if v_last >= target:
        return Prediction(0.0, None, "high", target, model="scurve",
                          eta_low=0.0, eta_high=0.0)

    window = [(t, v) for t, v in pairs if t >= t_last - window_seconds]
    if len(window) < min_points:
        window = pairs[-min_points:]

    # Linearise: y = ln(env - T). Requires env - T > 0 for every sample.
    import math
    xs, ys = [], []
    for t, v in window:
        gap = env_temp - v
        if gap <= 0.5:        # too close to env to take a stable log
            return None
        xs.append(t)
        ys.append(math.log(gap))
    slope, intercept = _linfit(xs, ys)   # slope = -k
    if slope is None or slope >= 0:
        return None   # not approaching env (k must be > 0)
    k = -slope

    # Solve env - (env - T0)*exp(-k*(t - t0)) = target.
    # With the fitted line ln(env - T) = intercept + slope*t, at the target:
    #   ln(env - target) = intercept + slope * t_target
    t_target = (math.log(env_temp - target) - intercept) / slope
    eta = t_target - t_last
    if eta <= 0:
        return Prediction(0.0, None, "high", target, model="scurve",
                          eta_low=0.0, eta_high=0.0)

    MAX_ETA = 48 * 3600
    if eta > MAX_ETA:
        return None   # model says basically never; defer to linear gating

    r2 = _r_squared(xs, ys, slope, intercept)
    span = xs[-1] - xs[0]
    if r2 >= 0.95 and span >= window_seconds * 0.4:
        confidence = "high"
    elif r2 >= 0.85:
        confidence = "medium"
    else:
        confidence = "low"

    # Current rate of rise for display: dT/dt = k * (env - T).
    slope_per_min = k * (env_temp - v_last) * 60.0
    return Prediction(eta, slope_per_min, confidence, target, model="scurve",
                      eta_low=eta * 0.85, eta_high=eta * 1.25)


def predict(timestamps, values, target, env_temp=None,
            window_seconds: float = 900.0, stalled: bool = False):
    """Best-available prediction: prefer the stall-aware S-curve when an
    environment (cooker) temperature is known and the model fits, else fall back
    to the robust linear estimate.

    *stalled* is the live verdict from the probe watcher (probewatch.py). During
    a detected evaporative stall the rate-of-rise is near zero, so any
    extrapolation is dishonest: the curve models say "hours" with false
    precision while the real exit depends on wrapping and the cut. We keep the
    best model's estimate as the OPTIMISTIC bound, stretch the pessimistic
    bound hard, mark confidence low, and flag ``stalled`` so the UI can say
    "in the stall" instead of quoting a confident clock."""
    p = None
    if env_temp is not None:
        sc = predict_scurve(timestamps, values, target, env_temp,
                            window_seconds=max(window_seconds, 1800.0))
        if sc is not None and sc.eta_seconds is not None:
            p = sc
    if p is None:
        p = predict_eta(timestamps, values, target,
                        window_seconds=window_seconds)
    if stalled and (p.eta_seconds or 0) > 0:
        p = Prediction(p.eta_seconds, p.slope_per_min, "low", p.target,
                       model="stall", eta_low=p.eta_seconds,
                       eta_high=(p.eta_high or p.eta_seconds) * 2.5,
                       stalled=True)
    elif stalled:
        p = Prediction(None, p.slope_per_min, "low", p.target, model="stall",
                       stalled=True)
    return p
