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


def pick_done_at(predictions: dict, channel: str, now: float) -> Optional[float]:
    """The plan's governing done time. *predictions* is the service's
    last_predictions cache (channel -> {ts, eta, confidence, done_at, ...}).
    'auto' means dinner is ready when the LAST targeted item finishes."""
    def fresh(p):
        return (p and _num(p.get("done_at")) is not None
                and (now - (p.get("ts") or 0)) <= FRESH_SECS
                and p.get("confidence") in ("low", "medium", "high"))
    if channel != "auto":
        p = predictions.get(channel)
        return p["done_at"] if fresh(p) else None
    dones = [p["done_at"] for p in predictions.values() if fresh(p)]
    return max(dones) if dones else None


def assess(now: float, cfg: dict, predictions: dict,
           stalled_channels: Optional[set] = None,
           any_target: bool = False) -> dict:
    """Compare the plan against the live predictions.

    Returns ``{"status", "serve_ts", "ready_at", "slack_secs", "advice": [...]}``
    where each advice item is ``{"kind", "text"}`` (kinds: wrap, bump_pit,
    push_serve, hold, drop_pit)."""
    cfg = sanitize(cfg)
    stalled = stalled_channels or set()
    out = {"status": "off", "serve_ts": cfg["serve_ts"], "ready_at": None,
           "slack_secs": None, "advice": []}
    if not cfg["enabled"] or cfg["serve_ts"] <= 0:
        return out

    if now > cfg["serve_ts"] + STALE_AFTER_SECS:
        out["status"] = "stale"
        return out
    if now > cfg["serve_ts"]:
        out["status"] = "past"
        return out

    done_at = pick_done_at(predictions, cfg["channel"], now)
    if done_at is None:
        out["status"] = "no_target" if not any_target else "no_eta"
        return out

    ready_at = done_at + cfg["rest_secs"]
    slack = cfg["serve_ts"] - ready_at
    out["ready_at"] = ready_at
    out["slack_secs"] = slack

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
    elif slack > cfg["hold_window_secs"]:
        out["status"] = "early"
        early_min = int(round(slack / 60))
        out["advice"].append({"kind": "hold",
                              "text": f"Running ~{early_min} min early - plan a "
                                      "keep-warm hold after the pull."})
        out["advice"].append({"kind": "drop_pit",
                              "text": "Or drop the pit ~15° to slow down."})
    else:
        out["status"] = "on_track"
    return out
