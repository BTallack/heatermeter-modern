"""Pit-stability score for a cook (pure, unit-testable).

How steadily did the controller hold the pit, judged only over the time it
was *supposed* to be holding: after the pit first reaches the setpoint (per
setpoint segment - a bump starts a fresh climb), and excluding the firmware's
lid-open windows plus a short grace after each. The initial climb and the
lid opens are the cook, not the controller, so they are scored separately
(overshoot, lid recovery) rather than penalised as "off target".

Metrics (board unit; defaults assume Fahrenheit):
* in_band_pct  - share of held time within +/-BAND of the setpoint
* mae          - mean |pit - set| over held time
* overshoot    - largest excursion above the setpoint (lid windows excluded)
* lid_recovery - mean seconds from a lid window closing to the pit re-entering
                 the band (only lid windows that recovered count)
* output_avg   - mean PID output over held time (fuel/air effort)

The 0-100 score is deliberately simple and explainable: 60% in-band share,
25% MAE (0 deg = full marks, 20 deg = none), 15% lid recovery (<=5 min = full,
>=30 min = none; no lid opens = full). :func:`compare` ranks a cook's score
against the user's own history.
"""

from __future__ import annotations

from typing import Optional

BAND = 10.0            # deg either side of set that counts as "holding"
AT_TEMP_EPSILON = 5.0  # reaching within this of set = the climb is over
REARM_DELTA = 10.0     # a setpoint change this big starts a new climb
LID_GRACE_SECS = 300   # after a lid window: still the cook's fault, not the PID's
MIN_HELD_SECS = 600    # fewer held seconds than this = no meaningful score


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def score_cook(columns: dict) -> Optional[dict]:
    """Score one cook from the store's history columns
    (``{"t", "set_point", "pit", "lid_countdown", "output_pct", ...}``).
    Returns None when the pit never held a setpoint long enough to judge."""
    t = columns.get("t") or []
    n = len(t)
    if n < 2:
        return None
    sps = columns.get("set_point") or []
    pits = columns.get("pit") or []
    lids = columns.get("lid_countdown") or []
    outs = columns.get("output_pct") or columns.get("fan_pct") or []

    held_secs = 0.0
    in_band_secs = 0.0
    abs_err_secs = 0.0
    out_secs = 0.0
    overshoot = 0.0
    recoveries: list = []

    sp_prev = None
    at_temp = False
    in_lid = False
    grace_until = None
    lid_closed_at = None       # waiting for the pit to re-enter the band
    setpoints: set = set()

    for k in range(n):
        sp = _num(sps[k] if k < len(sps) else None)
        pit = _num(pits[k] if k < len(pits) else None)
        lid = (_num(lids[k] if k < len(lids) else None) or 0) > 0
        out = _num(outs[k] if k < len(outs) else None)
        dt = (t[k + 1] - t[k]) if k + 1 < n else 0.0
        dt = max(0.0, min(dt, 60.0))       # a gap in the log is not held time

        if sp is None or sp <= 0 or pit is None:
            at_temp = False
            sp_prev = None
            continue
        setpoints.add(round(sp))
        if sp_prev is not None and abs(sp - sp_prev) >= REARM_DELTA:
            at_temp = False
        sp_prev = sp
        if not at_temp and pit >= sp - AT_TEMP_EPSILON:
            at_temp = True

        if lid:
            in_lid = True
            continue
        if in_lid:
            in_lid = False
            grace_until = t[k] + LID_GRACE_SECS
            lid_closed_at = t[k]
        if lid_closed_at is not None and abs(pit - sp) <= BAND:
            recoveries.append(t[k] - lid_closed_at)
            lid_closed_at = None
        in_grace = grace_until is not None and t[k] < grace_until

        overshoot = max(overshoot, pit - sp)
        if not at_temp or in_grace:
            continue                    # climb / lid recovery: not the PID's hold

        err = abs(pit - sp)
        held_secs += dt
        abs_err_secs += err * dt
        if err <= BAND:
            in_band_secs += dt
        if out is not None:
            out_secs += out * dt

    if held_secs < MIN_HELD_SECS:
        return None
    in_band_pct = 100.0 * in_band_secs / held_secs
    mae = abs_err_secs / held_secs
    lid_recovery = (sum(recoveries) / len(recoveries)) if recoveries else None

    band_part = in_band_pct / 100.0
    mae_part = max(0.0, 1.0 - mae / 20.0)
    if lid_recovery is None:
        rec_part = 1.0
    else:
        rec_part = max(0.0, min(1.0, 1.0 - (lid_recovery - 300.0) / 1500.0))
    score = round(100.0 * (0.60 * band_part + 0.25 * mae_part + 0.15 * rec_part))

    return {
        "score": int(max(0, min(100, score))),
        "held_secs": round(held_secs),
        "in_band_pct": round(in_band_pct, 1),
        "mae": round(mae, 1),
        "overshoot": round(max(0.0, overshoot), 1),
        "lid_recovery_secs": round(lid_recovery) if lid_recovery is not None else None,
        "lid_opens": len(recoveries),
        "output_avg": round(out_secs / held_secs, 1) if held_secs else None,
        "setpoints": sorted(setpoints),
    }


def compare(score: int, history: list) -> dict:
    """Rank *score* against the user's other cooks' scores (ints; the current
    cook excluded). Returns avg/best/rank text, or an empty comparison when
    there is no history yet."""
    hist = [int(s) for s in history if isinstance(s, (int, float))]
    if not hist:
        return {"cooks": 0, "avg": None, "best": None, "rank": None,
                "verdict": "first scored cook"}
    avg = sum(hist) / len(hist)
    best = max(hist)
    better = sum(1 for s in hist if s > score)
    rank = better + 1
    total = len(hist) + 1
    if score >= best:
        verdict = "your steadiest cook yet"
    elif score >= avg + 5:
        verdict = "steadier than usual"
    elif score <= avg - 5:
        verdict = "rougher than usual"
    else:
        verdict = "about your usual"
    return {"cooks": len(hist), "avg": round(avg), "best": best,
            "rank": f"{rank} of {total}", "verdict": verdict}
