"""Smart lid-open recovery (pure, unit-testable).

When the firmware detects the lid opening (a sharp pit-temp drop) it shuts the
fan off and starts a fixed countdown - ``lid_countdown`` seconds - before normal
PID control resumes. That fixed wait is conservative: if the lid was only open
for a few seconds the pit starts recovering almost immediately, yet the fan
stays idle for the whole timer and the pit craters far more than it needed to.

This detector watches the pit through the firmware's lid window and, the moment
the pit turns the corner and climbs back off its low point, cuts the wait short:
it cancels the firmware lid timer and resumes heating - gently. Rather than
slamming the fan to whatever PID wants, it ramps a manual fan output from a soft
start up to full over a short window, then hands control back to the PID. The
ramp avoids the overshoot a cold-start blast would cause while still recovering
far faster than the fixed countdown.

The detector is pure: feed it one sample per status update via :meth:`update`
and it returns the actions to take (cancel the lid timer, set a manual fan
percent, or resume PID). The service translates those into serial commands.

Temperatures are in whatever unit the board reports; the defaults assume
Fahrenheit (the only deployed unit). All thresholds are configurable.
"""

from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "enabled": True,
    "recover_delta": 4.0,   # rise (deg) off the lid-open low that signals "closed"
    "start_pct": 15,        # gentle initial fan % when heating resumes
    "ramp_secs": 60,        # ramp start_pct -> 100% over this many seconds
    "min_armed_secs": 5,    # ignore blips: track the dip at least this long first
    "step_pct": 5,          # quantise the ramp so we don't spam the serial line
}


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over the defaults, coercing and clamping every field."""
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        try:
            d["recover_delta"] = float(cfg["recover_delta"])
        except (KeyError, TypeError, ValueError):
            pass
        for k in ("start_pct", "ramp_secs", "min_armed_secs", "step_pct"):
            try:
                d[k] = int(float(cfg[k]))
            except (KeyError, TypeError, ValueError):
                pass
    d["recover_delta"] = max(1.0, min(50.0, d["recover_delta"]))
    d["start_pct"] = max(0, min(100, int(d["start_pct"])))
    d["ramp_secs"] = max(0, min(600, int(d["ramp_secs"])))
    d["min_armed_secs"] = max(0, min(120, int(d["min_armed_secs"])))
    d["step_pct"] = max(1, min(50, int(d["step_pct"])))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


class LidRecovery:
    """Stateful lid-recovery detector.

    States:
      idle    - not in a lid window (or disabled).
      armed   - firmware lid timer is running; tracking the pit's low point.
      ramping - recovery detected, lid timer cancelled, ramping the fan back up.

    Feed one sample per status update via :meth:`update`, which returns
    ``{"actions": [...], "state": str}``. Each action is one of:
      {"type": "cancel_lid"}            - cancel the firmware lid countdown
      {"type": "manual", "pct": int}    - set a manual fan output percent
      {"type": "resume_auto", "setpoint": float} - hand back to PID at setpoint
    """

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = sanitize(cfg)
        self.state = "idle"
        self._armed_ts: Optional[float] = None
        self._pit_min: Optional[float] = None
        self._ramp_ts: Optional[float] = None
        self._setpoint: Optional[float] = None
        self._last_pct: Optional[int] = None

    def set_config(self, cfg: dict) -> None:
        self.cfg = sanitize(cfg)

    def reset(self) -> None:
        self.state = "idle"
        self._armed_ts = None
        self._pit_min = None
        self._ramp_ts = None
        self._setpoint = None
        self._last_pct = None

    # -- helpers -----------------------------------------------------------

    def _go_idle(self) -> None:
        self.state = "idle"
        self._armed_ts = None
        self._pit_min = None
        self._ramp_ts = None
        self._setpoint = None
        self._last_pct = None

    def _ramp_pct(self, elapsed: float) -> int:
        """The quantised manual fan percent for *elapsed* seconds into the ramp."""
        start = self.cfg["start_pct"]
        span = self.cfg["ramp_secs"]
        if span <= 0:
            return 100
        frac = max(0.0, min(1.0, elapsed / span))
        pct = start + (100 - start) * frac
        step = self.cfg["step_pct"]
        q = int(round(pct / step) * step)
        return max(start, min(100, q))

    def status(self) -> dict:
        return {"state": self.state,
                "pit_min": self._pit_min,
                "setpoint": self._setpoint}

    # -- main update -------------------------------------------------------

    def update(self, ts: float, lid_countdown, pit, set_point) -> dict:
        """Ingest one status sample.

        *lid_countdown* is the firmware's remaining lid timer (seconds; 0/None =
        not in a lid window). *pit* and *set_point* are the current pit temp and
        active setpoint. Returns ``{"actions": [...], "state": str}``."""
        if not self.cfg["enabled"]:
            if self.state != "idle":
                self._go_idle()
            return {"actions": [], "state": self.state}

        lid = _num(lid_countdown) or 0
        pit = _num(pit)
        sp = _num(set_point)
        # Smart recovery only makes sense in PID auto mode (positive setpoint).
        auto = sp is not None and sp > 0

        if self.state == "ramping":
            return self._tick_ramp(ts, lid, pit)

        # idle or armed
        if lid > 0 and auto:
            if self.state != "armed":
                # Enter the lid window: start tracking the dip.
                self.state = "armed"
                self._armed_ts = ts
                self._pit_min = pit
                self._setpoint = sp
            elif pit is not None:
                self._pit_min = (pit if self._pit_min is None
                                 else min(self._pit_min, pit))
                self._setpoint = sp
            return self._maybe_recover(ts, pit)

        # Not in a lid window (firmware resolved it, or manual mode): stand down
        # and let the firmware's own control resume. We never fight the board.
        if self.state == "armed":
            self._go_idle()
        return {"actions": [], "state": self.state}

    def _maybe_recover(self, ts: float, pit) -> dict:
        """While armed, fire recovery once the pit climbs back off its low."""
        if pit is None or self._pit_min is None or self._armed_ts is None:
            return {"actions": [], "state": self.state}
        # Require a short dwell so we capture a real low, not the first sample.
        if (ts - self._armed_ts) < self.cfg["min_armed_secs"]:
            return {"actions": [], "state": self.state}
        if (pit - self._pit_min) < self.cfg["recover_delta"]:
            return {"actions": [], "state": self.state}

        # Recovery: the lid is back on and the pit is climbing. Cancel the
        # firmware lid timer and start the gentle fan ramp.
        sp = self._setpoint
        actions = [{"type": "cancel_lid"}]
        if pit is not None and sp is not None and pit >= sp:
            # Already at/above target - no need to push the fan; hand to PID.
            actions.append({"type": "resume_auto", "setpoint": sp})
            self._go_idle()
            return {"actions": actions, "state": self.state}

        self.state = "ramping"
        self._ramp_ts = ts
        pct = self._ramp_pct(0.0)
        self._last_pct = pct
        actions.append({"type": "manual", "pct": pct})
        return {"actions": actions, "state": self.state}

    def _tick_ramp(self, ts: float, lid, pit) -> dict:
        """Advance the ramp; hand back to PID when it completes or the pit is up."""
        # A fresh lid-open during the ramp (another open) - abandon and re-arm.
        if lid > 0:
            self.state = "armed"
            self._armed_ts = ts
            self._pit_min = pit
            self._ramp_ts = None
            self._last_pct = None
            return {"actions": [], "state": self.state}

        sp = self._setpoint
        elapsed = ts - (self._ramp_ts or ts)
        # Done when the pit has recovered to setpoint or the ramp window elapsed.
        reached = pit is not None and sp is not None and pit >= sp
        if reached or elapsed >= self.cfg["ramp_secs"]:
            actions = [{"type": "resume_auto", "setpoint": sp}] if sp else []
            self._go_idle()
            return {"actions": actions, "state": self.state}

        pct = self._ramp_pct(elapsed)
        if pct != self._last_pct:
            self._last_pct = pct
            return {"actions": [{"type": "manual", "pct": pct}], "state": self.state}
        return {"actions": [], "state": self.state}
