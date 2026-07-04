"""Pit Guard: context-aware pit high/low alerting (pure, unit-testable).

Replaces the classic absolute high/low pit alarms, which were context-blind:
a fixed "low" threshold rings on every lid open, during the initial climb, and
whenever the setpoint changes. This guard is:

* **Relative to the setpoint** ("30 deg below set"), so it survives setpoint
  changes untouched.
* **Armed only after the pit first reaches temp** (the fork firmware reports
  pid_mode; stock firmware falls back to "pit came within 5 deg of set"), so the
  initial climb is silent.
* **Suppressed while the lid is open and during a post-lid recovery grace**
  (until the pit re-enters the band or the grace expires), so a spritz never
  pages you.
* **Sustained, with escalation** - a dip must hold for a dwell before the
  "pit low" warning; and only "still low with the PID output pegged" for a
  further dwell escalates to the fire-dying critical. Max air with no recovery
  is the one signal that reliably means the fuel is done - and it's a signal
  only a real controller has.

The high side is the over-temp ("running hot") detector that used to live in
service.py, relocated here unchanged in behavior so the whole pit guard is one
config + one module: a sustained excursion well above the setpoint (a runaway /
stuck-open damper / kamado flare), with hysteresis re-arm. It deliberately has
no at-temp or lid gating - a flare right after a lid close is exactly when you
want the warning.

Pure: feed one sample per status update via :meth:`update`; it returns timeline
events to record. The service turns those into graph markers + pushes, and
:meth:`status` feeds the API / Home Assistant binary sensors.

Temperatures are in the board's unit (defaults assume Fahrenheit).
"""

from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "enabled": True,
    "low_band": 30.0,        # deg below set that counts as "running low"
    "low_dwell_secs": 300,   # must hold that long before the warning
    "fire_fan_pct": 90,      # PID output >= this while low = "max air"
    "fire_dwell_secs": 300,  # pegged+low this long after the warning = critical
    "lid_grace_secs": 600,   # post-lid suppression (ends early on re-entry)
    "high_margin": 40.0,     # deg over set that counts as "running hot"
    "high_dwell_secs": 120,  # must hold that long before the over-temp event
}

# Reaching within this many degrees of the setpoint counts as "at temp" when
# the firmware doesn't report pid_mode (stock firmware).
AT_TEMP_EPSILON = 5.0

# A setpoint change at least this big re-arms the low guard (a big bump up
# starts a fresh climb; alerting "low" during it would be the old bug again).
REARM_SETPOINT_DELTA = 10.0

PIDMODE_NORMAL = 2   # fork firmware's "at temp"


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over the defaults, coercing and clamping every field."""
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        for k in ("low_band", "high_margin"):
            try:
                d[k] = float(cfg[k])
            except (KeyError, TypeError, ValueError):
                pass
        for k in ("low_dwell_secs", "fire_dwell_secs", "lid_grace_secs",
                  "high_dwell_secs", "fire_fan_pct"):
            try:
                d[k] = int(float(cfg[k]))
            except (KeyError, TypeError, ValueError):
                pass
    d["low_band"] = max(10.0, min(150.0, d["low_band"]))
    d["low_dwell_secs"] = max(30, min(3600, d["low_dwell_secs"]))
    d["fire_fan_pct"] = max(50, min(100, d["fire_fan_pct"]))
    d["fire_dwell_secs"] = max(60, min(3600, d["fire_dwell_secs"]))
    d["lid_grace_secs"] = max(0, min(3600, d["lid_grace_secs"]))
    d["high_margin"] = max(10.0, min(150.0, d["high_margin"]))
    d["high_dwell_secs"] = max(30, min(3600, d["high_dwell_secs"]))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


class PitGuard:
    """Stateful pit guard. Feed one sample per status update via
    :meth:`update`; returns ``{"events": [...], "status": {...}}`` where each
    event is ``{"kind": "pit_low"|"fire_dying"|"overtemp", "label", "value"}``."""

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = sanitize(cfg)
        self._reset_all()

    def set_config(self, cfg: dict) -> None:
        self.cfg = sanitize(cfg)

    def _reset_all(self) -> None:
        self._sp_prev: Optional[float] = None
        self._at_temp_seen = False
        self._in_lid = False
        self._grace_until: Optional[float] = None
        self._low_since: Optional[float] = None
        self._low_warned = False
        self._pegged_since: Optional[float] = None
        self._fire_warned = False
        self._high_since: Optional[float] = None
        self._high_active = False

    reset = _reset_all

    def status(self) -> dict:
        """Live flags for the API + Home Assistant binary sensors."""
        return {"low": self._low_warned, "fire_dying": self._fire_warned,
                "overtemp": self._high_active, "armed": self._at_temp_seen}

    def update(self, ts: float, set_point, pit, output_pct,
               lid_countdown, pid_mode=None) -> dict:
        """Ingest one status sample. *output_pct* is the PID output demand
        (falls back to fan% in the caller); *lid_countdown* > 0 means the
        firmware's lid window is running."""
        events: list = []
        if not self.cfg["enabled"]:
            self._reset_all()
            return {"events": events, "status": self.status()}

        sp = _num(set_point)
        pit = _num(pit)
        out = _num(output_pct) or 0.0
        lid = (_num(lid_countdown) or 0) > 0

        # Off / manual mode: nothing to guard.
        if sp is None or sp <= 0:
            self._reset_all()
            return {"events": events, "status": self.status()}

        # A big setpoint bump starts a fresh climb - re-arm the low guard.
        if self._sp_prev is not None and abs(sp - self._sp_prev) >= REARM_SETPOINT_DELTA:
            self._at_temp_seen = False
            self._low_since = None
            self._pegged_since = None
            self._low_warned = False
            self._fire_warned = False
        self._sp_prev = sp

        # Arm once the pit has actually reached temp this climb.
        if pid_mode == PIDMODE_NORMAL or (pit is not None and pit >= sp - AT_TEMP_EPSILON):
            self._at_temp_seen = True

        # Lid window + post-lid grace bookkeeping.
        if lid:
            self._in_lid = True
            self._grace_until = None
            self._low_since = None
            self._pegged_since = None
        elif self._in_lid:
            self._in_lid = False
            self._grace_until = ts + self.cfg["lid_grace_secs"]
        if (self._grace_until is not None and pit is not None
                and pit >= sp - self.cfg["low_band"]):
            self._grace_until = None        # recovered - grace ends early
        in_grace = self._grace_until is not None and ts < self._grace_until

        if pit is None:
            return {"events": events, "status": self.status()}

        # -- low side (warning -> fire-dying critical) ----------------------
        suppressed = lid or in_grace or not self._at_temp_seen
        low = pit <= sp - self.cfg["low_band"]
        if suppressed or not low:
            self._low_since = None
            self._pegged_since = None
            if not low and pit >= sp - self.cfg["low_band"] / 2:
                self._low_warned = False    # back near set - re-arm
                self._fire_warned = False
        else:
            if self._low_since is None:
                self._low_since = ts
            if (not self._low_warned
                    and (ts - self._low_since) >= self.cfg["low_dwell_secs"]):
                self._low_warned = True
                events.append({"kind": "pit_low",
                               "label": f"Pit low {round(pit)}° (set {round(sp)}°)",
                               "value": float(pit)})
            if self._low_warned and out >= self.cfg["fire_fan_pct"]:
                if self._pegged_since is None:
                    self._pegged_since = ts
                if (not self._fire_warned
                        and (ts - self._pegged_since) >= self.cfg["fire_dwell_secs"]):
                    self._fire_warned = True
                    events.append({"kind": "fire_dying",
                                   "label": f"Fire dying: fan maxed, pit {round(pit)}°",
                                   "value": float(pit)})
            elif out < self.cfg["fire_fan_pct"]:
                self._pegged_since = None

        # -- high side (over-temp; relocated from service, behavior kept) ---
        if pit >= sp + self.cfg["high_margin"]:
            if self._high_since is None:
                self._high_since = ts
            elif (not self._high_active
                    and (ts - self._high_since) >= self.cfg["high_dwell_secs"]):
                self._high_active = True
                events.append({"kind": "overtemp",
                               "label": f"Running hot {round(pit)}° (set {round(sp)}°)",
                               "value": float(pit)})
        else:
            self._high_since = None
            if self._high_active and pit <= sp + self.cfg["high_margin"] / 2:
                self._high_active = False

        return {"events": events, "status": self.status()}
