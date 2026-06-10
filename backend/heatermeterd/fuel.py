"""Charcoal/fuel monitoring from blower effort (pure, unit-testable).

The controller holds the pit at a setpoint by pushing air. As the fuel bed
depletes, holding the same temperature takes progressively more air, so a
sustained upward trend in (smoothed) fan duty at a steady setpoint is the
signature of a dying fire - long before the temperature actually drops. No
commercial local controller surfaces this; it is a classic HeaterMeter forum
trick automated.

``FuelMonitor.update()`` ingests one status sample per second and returns alert
events; ``status()`` reports the live assessment (duty trend per hour, an
estimate of when the blower will hit its ceiling, and the alert state).

Samples are only accumulated while the measurement is meaningful: the pit must
be near the setpoint (not startup/recovery/lid-open, where duty legitimately
spikes). All thresholds configurable.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

DEFAULTS = {
    "enabled": True,
    "window_secs": 2400.0,     # trailing window for the duty trend fit (40 min)
    "near_band": 15.0,         # only sample when |pit - setpoint| <= this
    "alert_duty": 85.0,        # sustained duty at/over this fires "add fuel"
    "alert_hold_secs": 300.0,  # how long duty must hold above alert_duty
    "trend_min_per_hr": 8.0,   # %duty/hour rise considered "burning down"
}

_FLOATS = ("window_secs", "near_band", "alert_duty", "alert_hold_secs",
           "trend_min_per_hr")


def sanitize(cfg: Optional[dict]) -> dict:
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        for k in _FLOATS:
            try:
                d[k] = float(cfg[k])
            except (KeyError, TypeError, ValueError):
                pass
    d["window_secs"] = max(300.0, d["window_secs"])
    d["alert_duty"] = min(100.0, max(20.0, d["alert_duty"]))
    return d


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


class FuelMonitor:
    def __init__(self, config: Optional[dict] = None):
        self.cfg = sanitize(config)
        self.samples: deque = deque()      # (ts, duty) while near setpoint
        self.high_since: Optional[float] = None
        self.alerted = False

    def set_config(self, config: Optional[dict]) -> None:
        self.cfg = sanitize(config)

    def reset(self) -> None:
        self.samples.clear()
        self.high_since = None
        self.alerted = False

    def update(self, ts: float, fan_pct, pit, setpoint, *,
               lid_open: bool = False) -> list:
        """Ingest one sample; returns events: fuel_low / fuel_ok."""
        if not self.cfg["enabled"]:
            return []
        duty = _num(fan_pct)
        p, sp = _num(pit), _num(setpoint)
        events = []
        # Only meaningful while actively holding temp with the lid closed.
        holding = (duty is not None and p is not None and sp is not None
                   and sp > 0 and abs(p - sp) <= self.cfg["near_band"]
                   and not lid_open)
        if holding:
            self.samples.append((ts, duty))
            win = self.cfg["window_secs"]
            while self.samples and (ts - self.samples[0][0]) > win:
                self.samples.popleft()

            # Sustained-high-duty alert (the urgent signal).
            if duty >= self.cfg["alert_duty"]:
                if self.high_since is None:
                    self.high_since = ts
                elif (not self.alerted
                      and (ts - self.high_since) >= self.cfg["alert_hold_secs"]):
                    self.alerted = True
                    events.append({
                        "type": "fuel_low", "ts": ts, "duty": duty,
                        "message": ("The blower has been working near its "
                                    "limit to hold temperature. Add fuel "
                                    "soon."),
                    })
            else:
                self.high_since = None
                if self.alerted and duty < self.cfg["alert_duty"] - 15:
                    self.alerted = False
                    events.append({
                        "type": "fuel_ok", "ts": ts, "duty": duty,
                        "message": "Blower effort is back to normal.",
                    })
        else:
            self.high_since = None
        return events

    def _trend(self):
        """Least-squares slope of duty over the window, in %/hour, plus the
        current smoothed duty. None when there is not enough data."""
        n = len(self.samples)
        if n < 10:
            return None, None
        span = self.samples[-1][0] - self.samples[0][0]
        if span < self.cfg["window_secs"] * 0.5:
            return None, None
        xs = [t for t, _ in self.samples]
        ys = [d for _, d in self.samples]
        mx = sum(xs) / n
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            return None, my
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx  # %/sec
        return slope * 3600.0, my

    def status(self) -> dict:
        """Live assessment for the API: duty trend and a rough time until the
        blower ceilings out (= the fire can no longer hold temp)."""
        slope_hr, duty_avg = self._trend()
        out = {"enabled": self.cfg["enabled"], "alerted": self.alerted,
               "duty_avg": round(duty_avg, 1) if duty_avg is not None else None,
               "trend_per_hr": round(slope_hr, 1) if slope_hr is not None else None,
               "depleting": False, "est_secs_to_max": None}
        if slope_hr is not None and duty_avg is not None \
                and slope_hr >= self.cfg["trend_min_per_hr"]:
            out["depleting"] = True
            headroom = max(0.0, 98.0 - duty_avg)
            est = headroom / slope_hr * 3600.0
            # Quote only plausible horizons; > 12h of headroom is "fine".
            if est <= 12 * 3600:
                out["est_secs_to_max"] = est
        return out
