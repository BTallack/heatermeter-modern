"""Serve-time planning (pure, unit-testable).

"I want to eat at 6:00." The cook's one question that no thermometer answers.
Given the stall-aware predictions the daemon already maintains, this module
continuously compares *when the food will actually be ready* (predicted done +
rest) against the serve time and says whether you're on track - and if not,
what to do about it. Because HeaterMeter controls the pit, the advice is
actionable: bump the pit, wrap now, drop to a hold.

Status bands (slack = serve_time - ready_at; positive = ready early):
  early    - ready more than `early_secs` before serve: plan a hold or slow down.
  on_track - ready inside the comfortable window before serve.
  late     - ready after the serve time: make up time or push dinner.
  no_eta   - a plan is set but there's no usable prediction yet (early in the
             cook, no target set, or low confidence).
  past     - the serve time has passed.

The advice is deliberately qualitative (wrap / bump / hold / push) grounded in
the live numbers - not fake minute-precision. Pure: the service feeds it the
prediction cache + stall flags; it returns the assessment dict the API/UI show.
"""

from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "enabled": False,
    "serve_ts": 0.0,        # epoch seconds of the serve time
    "channel": "auto",      # auto = latest-finishing targeted probe
    "rest_secs": 900,       # rest after the pull before it's ready to eat
    "hold_window_secs": 3600,   # ready this far before serve still = on track
}

_CHANNELS = ("auto", "food1", "food2", "ambient")

# Predictions older than this are not trusted for planning.
FRESH_SECS = 120.0

# Auto-disable a plan whose serve time is long past (a stale leftover plan
# must not haunt the next cook).
STALE_AFTER_SECS = 6 * 3600.0


def sanitize(cfg: Optional[dict]) -> dict:
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        for k in ("serve_ts", "rest_secs", "hold_window_secs"):
            try:
                d[k] = float(cfg[k])
            except (KeyError, TypeError, ValueError):
                pass
        if cfg.get("channel") in _CHANNELS:
            d["channel"] = cfg["channel"]
    d["serve_ts"] = max(0.0, d["serve_ts"])
    d["rest_secs"] = max(0.0, min(4 * 3600.0, d["rest_secs"]))
    d["hold_window_secs"] = max(600.0, min(6 * 3600.0, d["hold_window_secs"]))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fresh(p: Optional[dict], now: float) -> bool:
    """A prediction the plan may act on: recent, with a usable confidence, and
    carrying either a done time or a detected plateau."""
    return bool(p and (now - (p.get("ts") or 0)) <= FRESH_SECS
                and p.get("confidence") in ("low", "medium", "high")
                and (_num(p.get("done_at")) is not None
                     or p.get("model") == "plateau"))


def pick_bounds(predictions: dict, channel: str, now: float) -> Optional[dict]:
    """The plan's governing done-time bounds ``{"lo", "hi", "plateau_temp"}``.
    *lo* is the model estimate, *hi* its pessimistic bound (equal when the
    model gave none). *predictions* is the service's last_predictions cache
    (channel -> {ts, eta, confidence, done_at, done_at_high, model, ...}).
    'auto' means dinner is ready when the LAST targeted item finishes - and a
    probe levelling off below its target never finishes, so it governs and
    reports its plateau temperature instead of a time."""
    cands = [predictions.get(channel)] if channel != "auto" else list(predictions.values())
    cands = [p for p in cands if _fresh(p, now)]
    if not cands:
        return None
    plateaus = [p for p in cands if p.get("model") == "plateau"]
    if plateaus:
        return {"lo": None, "hi": None,
                "plateau_temp": _num(plateaus[0].get("plateau_temp"))}
    lo = max(p["done_at"] for p in cands)
    hi = max((_num(p.get("done_at_high")) or p["done_at"]) for p in cands)
    return {"lo": lo, "hi": max(hi, lo), "plateau_temp": None}


def pick_done_at(predictions: dict, channel: str, now: float) -> Optional[float]:
    """The plan's governing done time (the model estimate), or None."""
    b = pick_bounds(predictions, channel, now)
    return b["lo"] if b else None


def assess(now: float, cfg: dict, predictions: dict,
           stalled_channels: Optional[set] = None,
           any_target: bool = False) -> dict:
    """Compare the plan against the live predictions.

    Returns ``{"status", "serve_ts", "ready_at", "ready_at_high", "slack_secs",
    "slack_high_secs", "plateau_temp", "advice": [...]}`` where each advice
    item is ``{"kind", "text"}`` (kinds: wrap, bump_pit, push_serve, hold,
    drop_pit). Statuses: late (even the model estimate misses dinner),
    at_risk (the estimate makes it but its pessimistic bound doesn't), early,
    on_track, plus off/stale/past/no_target/no_eta."""
    cfg = sanitize(cfg)
    stalled = stalled_channels or set()
    out = {"status": "off", "serve_ts": cfg["serve_ts"], "ready_at": None,
           "ready_at_high": None, "slack_secs": None, "slack_high_secs": None,
           "plateau_temp": None, "advice": []}
    if not cfg["enabled"] or cfg["serve_ts"] <= 0:
        return out

    if now > cfg["serve_ts"] + STALE_AFTER_SECS:
        out["status"] = "stale"
        return out
    if now > cfg["serve_ts"]:
        out["status"] = "past"
        return out

    b = pick_bounds(predictions, cfg["channel"], now)
    if b is None:
        out["status"] = "no_target" if not any_target else "no_eta"
        return out
    if b["lo"] is None:
        # Levelling off below the target: it will not finish at this pit
        # temperature, so it is late however far off dinner is.
        pt = b["plateau_temp"]
        out["status"] = "late"
        out["plateau_temp"] = pt
        where = f"near {round(pt)}°" if pt is not None else "below its target"
        out["advice"] = [
            {"kind": "bump_pit",
             "text": f"Levelling off {where} - raise the pit 15–25° to finish."},
            {"kind": "push_serve", "text": "Or plan on serving later."},
        ]
        return out

    ready_lo = b["lo"] + cfg["rest_secs"]
    ready_hi = b["hi"] + cfg["rest_secs"]
    slack = cfg["serve_ts"] - ready_lo
    slack_hi = cfg["serve_ts"] - ready_hi
    out["ready_at"] = ready_lo
    out["ready_at_high"] = ready_hi
    out["slack_secs"] = slack
    out["slack_high_secs"] = slack_hi

    late_min = int(round(-slack / 60))
    if slack < 0:
        out["status"] = "late"
        # Wrap is the strongest lever, offered while something is stalled (or
        # simply mid-cook); then heat; then honesty.
        if stalled:
            out["advice"].append({"kind": "wrap",
                                  "text": "Wrap now to power through the stall."})
        out["advice"].append({"kind": "bump_pit",
                              "text": "Raise the pit 15–25° to claw back time."})
        out["advice"].append({"kind": "push_serve",
                              "text": f"Or push dinner ~{max(5, late_min)} min."})
    elif slack_hi < 0:
        out["status"] = "at_risk"
        risk_min = int(round(-slack_hi / 60))
        if stalled:
            out["advice"].append({"kind": "wrap",
                                  "text": "Wrap now to be safe - a long stall would make this late."})
        out["advice"].append({"kind": "bump_pit",
                              "text": f"If the slowdown continues, ready could be ~{max(5, risk_min)} min late. "
                                      "Bump the pit 10–15° to stay ahead."})
    elif slack_hi > cfg["hold_window_secs"]:
        out["status"] = "early"
        early_min = int(round(slack_hi / 60))
        out["advice"].append({"kind": "hold",
                              "text": f"Running ~{early_min} min early - plan a "
                                      "keep-warm hold after the pull."})
        out["advice"].append({"kind": "drop_pit",
                              "text": "Or drop the pit ~15° to slow down."})
    else:
        out["status"] = "on_track"
    return out
