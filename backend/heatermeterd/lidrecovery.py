"""Smart lid-open recovery (pure, unit-testable).

When the firmware detects the lid opening (a sharp pit drop) it shuts the fan off
and starts a fixed countdown before normal PID resumes. That fixed wait is
conservative: if the lid was open only briefly the pit recovers almost at once,
yet the fan idles for the whole timer.

This detector watches the pit through the firmware's lid window and, once the pit
turns the corner and climbs back off its low point, decides what to do:

* **Undershoot** (a leaky cooker that cratered): cancel the firmware lid timer so
  the board's own PID resumes early instead of idling out the full countdown.
* **Overshoot** (a well-sealed cooker - kettle/kamado - where opening the lid
  flares the coals and the pit shoots *past* the setpoint on close): stand down
  and let the firmware ride the excursion out with the fan off. Adding air there
  only makes the overshoot worse, and the fan cannot remove heat anyway.

It deliberately does NOT re-send the setpoint or drive a manual fan: on this
firmware ``setSetPoint`` forces ``PIDMODE_STARTUP`` (an aggressive warm-up mode)
and mislabels the state as "Starting up". Cancelling only the lid timer lets the
board resume its own normal/recovery PID in the right mode.

Pure: feed one sample per status update via :meth:`update`; the only action it
emits is ``cancel_lid``. Temperatures are in the board's unit (defaults assume F).
"""

from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "enabled": True,
    "recover_delta": 4.0,   # rise (deg) off the lid-open low that signals "closed"
    "min_armed_secs": 5,    # ignore blips: track the dip at least this long first
}


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over the defaults, coercing and clamping. Unknown keys (e.g.
    the retired start_pct/ramp_secs from the old ramping design) are ignored."""
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        try:
            d["recover_delta"] = float(cfg["recover_delta"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            d["min_armed_secs"] = int(float(cfg["min_armed_secs"]))
        except (KeyError, TypeError, ValueError):
            pass
    d["recover_delta"] = max(1.0, min(50.0, d["recover_delta"]))
    d["min_armed_secs"] = max(0, min(120, int(d["min_armed_secs"])))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


class LidRecovery:
    """Stateful lid-recovery detector.

    States:
      idle  - not in a lid window (or disabled).
      armed - firmware lid timer is running; tracking the pit's low point.

    Feed one sample per status update via :meth:`update`, which returns
    ``{"actions": [...], "state": str}``. The only action is
    ``{"type": "cancel_lid"}`` - cut the firmware's fan-off wait short so its own
    PID resumes early (emitted only on an *undershoot* recovery).
    """

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = sanitize(cfg)
        self.state = "idle"
        self._armed_ts: Optional[float] = None
        self._pit_min: Optional[float] = None
        self._setpoint: Optional[float] = None

    def set_config(self, cfg: dict) -> None:
        self.cfg = sanitize(cfg)

    def reset(self) -> None:
        self._go_idle()

    def _go_idle(self) -> None:
        self.state = "idle"
        self._armed_ts = None
        self._pit_min = None
        self._setpoint = None

    def status(self) -> dict:
        return {"state": self.state, "pit_min": self._pit_min,
                "setpoint": self._setpoint}

    def update(self, ts: float, lid_countdown, pit, set_point) -> dict:
        """Ingest one status sample. *lid_countdown* is the firmware's remaining
        lid timer (0/None = not in a lid window)."""
        if not self.cfg["enabled"]:
            if self.state != "idle":
                self._go_idle()
            return {"actions": [], "state": self.state}

        lid = _num(lid_countdown) or 0
        pit = _num(pit)
        sp = _num(set_point)
        auto = sp is not None and sp > 0   # only meaningful in PID auto mode

        if lid > 0 and auto:
            if self.state != "armed":
                self.state = "armed"
                self._armed_ts = ts
                self._pit_min = pit
                self._setpoint = sp
            elif pit is not None:
                self._pit_min = (pit if self._pit_min is None
                                 else min(self._pit_min, pit))
                self._setpoint = sp
            return self._maybe_recover(ts, pit)

        # Not in a lid window (firmware resolved it, or manual mode): stand down.
        if self.state == "armed":
            self._go_idle()
        return {"actions": [], "state": self.state}

    def _maybe_recover(self, ts: float, pit) -> dict:
        """Fire once the pit has climbed back off its low by recover_delta."""
        if pit is None or self._pit_min is None or self._armed_ts is None:
            return {"actions": [], "state": self.state}
        if (ts - self._armed_ts) < self.cfg["min_armed_secs"]:
            return {"actions": [], "state": self.state}
        if (pit - self._pit_min) < self.cfg["recover_delta"]:
            return {"actions": [], "state": self.state}

        sp = self._setpoint
        self._go_idle()
        if sp is not None and pit >= sp:
            # Overshoot (well-sealed cooker flaring): stand down, fan stays off,
            # let the firmware ride it back down. No cancel, no setpoint re-send.
            return {"actions": [], "state": self.state}
        # Undershoot: cut the firmware's fan-off wait short so its PID resumes now.
        return {"actions": [{"type": "cancel_lid"}], "state": self.state}
