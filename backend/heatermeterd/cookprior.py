"""Cut-aware time-to-done prior (pure, unit-testable).

The physics models in predict.py extrapolate the *current* rate of rise, so
early in a long cook they cannot see the stall ahead: replayed against a real
11-hour pork shoulder, every trailing-window model said "about two hours" at
131 deg when the truth was ten. What an experienced cook knows - and this
module encodes - is the *shape* of a cook: how the fraction of the temperature
rise maps to the fraction of the total time, stall included.

A profile is a piecewise-linear curve ``(rise_fraction -> time_fraction)``.
Given how far the meat has come (start temp -> now -> target) and how long
that took, the curve says what fraction of the whole cook has elapsed, which
scales to a total and therefore a remaining time. Weight and pit temperature
never need to be known: the cook's own progress so far calibrates the
timescale. The curve is normalised to the time the meat reaches the *finish
window* (FINISH_MARGIN below target) - the last couple of degrees of a pulled
cut are probe-tender-not-a-number territory and are left to the physics
models' plateau detection.

Defaults: the "pulled" curve is taken from a real unwrapped pork shoulder at
265 F (kamado); the others are smooth no-stall approaches. :func:`learn_curve`
+ :func:`merge_curve` refine a profile from each finished cook on the user's
own cooker, so the prior gets personal with use.
"""

from __future__ import annotations

from typing import Optional

FINISH_MARGIN = 3.0        # deg below target where the prior calls it "done"
MIN_ELAPSED_SECS = 1200    # need this much history before scaling a curve
MIN_RISE_FRACTION = 0.08   # ...and this much progress
R_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]

# rise fraction -> time fraction. Time is normalised to reaching target - FINISH_MARGIN.
DEFAULT_CURVES = {
    # Unwrapped pork shoulder, 265 F, 11 h: half the rise in the first ~12% of the
    # time, then the stall (0.64-0.86 of the rise) eating ~70% of it.
    "pulled": [(0.0, 0.0), (0.25, 0.04), (0.40, 0.07), (0.54, 0.125), (0.64, 0.19),
               (0.68, 0.25), (0.72, 0.375), (0.76, 0.50), (0.78, 0.625),
               (0.81, 0.75), (0.85, 0.81), (0.86, 0.875), (0.91, 0.94), (1.0, 1.0)],
    # Ribs: a shorter, shallower stall.
    "ribs": [(0.0, 0.0), (0.3, 0.10), (0.5, 0.20), (0.65, 0.35), (0.75, 0.55),
             (0.85, 0.75), (0.93, 0.90), (1.0, 1.0)],
    # Roasts, poultry, chops, fish: no stall, an exponential approach that
    # slows steadily (target sits ~90% of the way to the cooker temperature).
    "roast": [(0.0, 0.0), (0.2, 0.09), (0.4, 0.19), (0.6, 0.32), (0.7, 0.42),
              (0.8, 0.55), (0.9, 0.72), (0.95, 0.85), (1.0, 1.0)],
}

# Uncertainty band around the prior's estimate (multipliers): stalls vary a lot.
BANDS = {"pulled": (0.7, 1.5), "ribs": (0.75, 1.4), "roast": (0.8, 1.3)}

_PULLED_WORDS = ("brisket", "shoulder", "butt", "pulled", "belly", "chuck",
                 "beef rib", "burnt")
_RIB_WORDS = ("rib",)


def profile_for(target, name: Optional[str] = None) -> str:
    """Pick a curve from the probe's name (set from the meat preset) and the
    target temperature."""
    n = (name or "").lower()
    if any(w in n for w in _PULLED_WORDS):
        return "pulled"
    if any(w in n for w in _RIB_WORDS):
        return "ribs"
    try:
        t = float(target)
    except (TypeError, ValueError):
        return "roast"
    if t >= 198:
        return "pulled"
    if t >= 190:
        return "ribs"
    return "roast"


def _interp(curve, r: float) -> float:
    """Time fraction at rise fraction *r* on a piecewise-linear curve."""
    if r <= curve[0][0]:
        return curve[0][1]
    for (r0, t0), (r1, t1) in zip(curve, curve[1:]):
        if r <= r1:
            if r1 == r0:
                return t1
            return t0 + (t1 - t0) * (r - r0) / (r1 - r0)
    return curve[-1][1]


def estimate(elapsed_secs, start_temp, temp_now, target, profile: str = "pulled",
             curve=None) -> Optional[dict]:
    """Remaining seconds until the meat reaches the finish window, from its
    progress so far. Returns ``{"eta", "eta_low", "eta_high", "profile",
    "fraction"}`` or None when there is too little progress to scale the
    curve (early minutes, probe just inserted, or already in the window)."""
    try:
        elapsed = float(elapsed_secs)
        t0 = float(start_temp)
        t = float(temp_now)
        tg = float(target)
    except (TypeError, ValueError):
        return None
    finish = tg - FINISH_MARGIN
    if elapsed < MIN_ELAPSED_SECS or finish <= t0 or t <= t0:
        return None
    r = (t - t0) / (finish - t0)
    if r < MIN_RISE_FRACTION:
        return None
    if r >= 1.0:
        return None                     # in the window: the physics owns it
    crv = curve or DEFAULT_CURVES.get(profile) or DEFAULT_CURVES["roast"]
    tau = _interp(crv, r)
    if tau <= 0.01:
        return None
    total = elapsed / tau
    eta = max(0.0, total - elapsed)
    lo, hi = BANDS.get(profile, (0.8, 1.3))
    return {"eta": eta, "eta_low": eta * lo, "eta_high": eta * hi,
            "profile": profile, "fraction": round(tau, 3)}


def learn_curve(timestamps, temps, target) -> Optional[list]:
    """Turn a finished cook's (ts, temp) trace into a curve on R_GRID: the time
    fraction at which each rise fraction was first reached, normalised to the
    finish window. None if the trace never got there or is too short."""
    pairs = [(t, v) for t, v in zip(timestamps, temps)
             if v is not None and isinstance(v, (int, float))]
    if len(pairs) < 20:
        return None
    try:
        tg = float(target)
    except (TypeError, ValueError):
        return None
    finish = tg - FINISH_MARGIN
    t_start, v_start = pairs[0]
    if v_start >= finish:
        return None
    t_done = next((t for t, v in pairs if v >= finish), None)
    if t_done is None or t_done - t_start < 1800:
        return None
    total = t_done - t_start
    out = []
    for r in R_GRID:
        level = v_start + r * (finish - v_start)
        t_hit = next((t for t, v in pairs if v >= level), t_done)
        out.append((r, round(min(1.0, (t_hit - t_start) / total), 4)))
    out[-1] = (1.0, 1.0)
    return out


def merge_curve(existing: Optional[dict], new_curve: list) -> dict:
    """Fold a learned curve into a stored profile ``{"curve", "n"}`` as a
    running mean on R_GRID (the newest cook counts as one more sample)."""
    new_on_grid = [(r, _interp(new_curve, r)) for r in R_GRID]
    if not existing or not existing.get("curve"):
        return {"curve": new_on_grid, "n": 1}
    n = int(existing.get("n") or 1)
    old = existing["curve"]
    merged = []
    for r, tn in new_on_grid:
        to = _interp([tuple(p) for p in old], r)
        merged.append((r, round((to * n + tn) / (n + 1), 4)))
    return {"curve": merged, "n": n + 1}


def curve_from_store(stored: Optional[dict], profile: str):
    """The curve to use for *profile*: the user's learned one when present."""
    if isinstance(stored, dict):
        p = stored.get(profile)
        if isinstance(p, dict) and p.get("curve"):
            return [tuple(x) for x in p["curve"]]
    return DEFAULT_CURVES.get(profile)
