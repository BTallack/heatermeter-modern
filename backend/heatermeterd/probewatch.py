"""Probe health + stall watchers over the 1 Hz status stream (pure, testable).

Surfaces trust- and coaching-relevant EVENTS the service turns into push
notifications, WebSocket events, and timeline markers:

- ``disconnect`` - a probe that *was* reading suddenly reads nothing for a
  sustained window (yanked, cable failed, or out of range). A pit dropout during
  an active cook is ``critical``; a targeted food probe is ``warning``; anything
  else is ``info``. Crucially, a probe that was *never* connected does not alarm
  (so unused channels stay quiet).
- ``reconnect`` - a previously-dropped probe is reading again.
- ``fault`` - a reading wildly outside any plausible cooking range (open-circuit
  garbage), debounced so it fires once per episode.
- ``stall_start`` / ``stall_end`` - a food probe entering / leaving the classic
  low-and-slow plateau (the "stall"), detected from its rate of rise inside the
  stall temperature band. Conservative by design to avoid false positives.

Everything here is deterministic and unit-testable: feed it ``update(ts, temps,
...)`` once per status sample and it returns a (possibly empty) list of event
dicts. No I/O, no notifications - the service owns those side effects.

Temperatures are in whatever unit the board reports; defaults assume Fahrenheit
(the only deployed unit).
"""

from __future__ import annotations

from collections import deque
from typing import Optional

# Channel index <-> name. pit=0, food1=1, food2=2, ambient=3 (matches the rest
# of the codebase). The watcher works in channel *names* so callers pass a plain
# {name: value} temps dict.
CHANNELS = ("pit", "food1", "food2", "ambient")
FOOD_CHANNELS = ("food1", "food2", "ambient")  # ambient only when used as food

DEFAULTS = {
    "enabled": True,            # master switch for disconnect/fault watching
    "dropout_secs": 20.0,       # sustained missing time before a disconnect fires
    "implausible_low": -40.0,   # below this = sensor fault (open circuit etc.)
    "implausible_high": 1000.0,  # above this = sensor fault
    "stall_enabled": True,      # master switch for stall detection
    "stall_low": 150.0,         # stall band lower bound
    "stall_high": 180.0,        # stall band upper bound
    "stall_window_secs": 600.0,  # trailing window for the rate-of-rise estimate
    "stall_enter_rate": 6.0,    # deg/hr at/under which (in band) = stalled
    "stall_exit_rate": 14.0,    # deg/hr at/over which a stalled probe has broken out
}

_FLOAT_KEYS = ("dropout_secs", "implausible_low", "implausible_high",
               "stall_low", "stall_high", "stall_window_secs",
               "stall_enter_rate", "stall_exit_rate")


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over the defaults, coercing/clamping each field."""
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        for k in ("enabled", "stall_enabled"):
            if k in cfg:
                d[k] = bool(cfg[k])
        for k in _FLOAT_KEYS:
            try:
                d[k] = float(cfg[k])
            except (KeyError, TypeError, ValueError):
                pass
    # Sanity clamps so a bad config can't disable detection or invert bands.
    d["dropout_secs"] = max(2.0, d["dropout_secs"])
    d["stall_window_secs"] = max(60.0, d["stall_window_secs"])
    if d["stall_high"] < d["stall_low"]:
        d["stall_low"], d["stall_high"] = d["stall_high"], d["stall_low"]
    return d


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN never equals itself; treat as no-reading.
    return f if f == f else None


class _ChannelState:
    __slots__ = ("seen", "present", "missing_since", "last_val", "fault",
                 "samples", "stalled")

    def __init__(self):
        self.seen = False           # has ever had a valid reading
        self.present = False        # currently reading a valid value
        self.missing_since = None   # ts of first consecutive missing sample
        self.last_val = None
        self.fault = False          # implausible-reading episode in progress
        self.samples = deque()      # (ts, val) within the stall window
        self.stalled = False


class ProbeWatch:
    """Stateful watcher. Construct once; call :meth:`update` per status sample."""

    def __init__(self, config: Optional[dict] = None):
        self.cfg = sanitize(config)
        self._ch = {name: _ChannelState() for name in CHANNELS}

    def set_config(self, config: Optional[dict]) -> None:
        self.cfg = sanitize(config)

    def reset(self) -> None:
        """Forget all per-channel history (e.g. on a new cook session)."""
        self._ch = {name: _ChannelState() for name in CHANNELS}

    def update(self, ts: float, temps: dict, *, pid_mode=None,
               targets: Optional[dict] = None) -> list:
        """Ingest one status sample. *temps* maps channel name -> value (or
        None/blank for a missing probe). *targets* maps channel name -> bool
        (has an active food target), used only to rank disconnect severity.
        Returns a list of event dicts."""
        targets = targets or {}
        events = []
        cooking = pid_mode in (0, 1, 2)  # startup / recovering / at-temp

        for name in CHANNELS:
            st = self._ch[name]
            val = _num(temps.get(name))

            if val is None:
                # No reading this sample.
                if self.cfg["enabled"] and st.seen and st.present:
                    if st.missing_since is None:
                        st.missing_since = ts
                    elif (ts - st.missing_since) >= self.cfg["dropout_secs"]:
                        st.present = False
                        events.append(self._disconnect_event(
                            name, ts, cooking, bool(targets.get(name))))
                # A dropped probe can't be stalling.
                st.samples.clear()
                continue

            # Valid reading.
            reconnected = st.seen and not st.present
            st.seen = True
            st.present = True
            st.missing_since = None
            st.last_val = val
            if reconnected:
                st.stalled = False
                events.append({
                    "type": "reconnect", "channel": name, "severity": "info",
                    "ts": ts, "value": val,
                    "message": f"{_label(name)} probe reconnected.",
                })

            # Implausible-reading fault (debounced once per episode).
            if val < self.cfg["implausible_low"] or val > self.cfg["implausible_high"]:
                if not st.fault:
                    st.fault = True
                    events.append({
                        "type": "fault", "channel": name, "severity": "warning",
                        "ts": ts, "value": val,
                        "message": (f"{_label(name)} reading looks wrong "
                                    f"({val:g}°). Check the probe/type."),
                    })
                st.samples.clear()
                continue
            st.fault = False

            # Stall detection (food channels only).
            if self.cfg["stall_enabled"] and name in FOOD_CHANNELS:
                ev = self._stall_step(name, st, ts, val)
                if ev:
                    events.append(ev)
            else:
                st.samples.clear()

        return events

    # -- helpers ----------------------------------------------------------

    def _disconnect_event(self, name, ts, cooking, has_target):
        if name == "pit":
            sev = "critical" if cooking else "warning"
            msg = ("Pit probe disconnected"
                   + (" mid-cook - the controller has lost its temperature input."
                      if cooking else "."))
        elif has_target:
            sev = "warning"
            msg = f"{_label(name)} probe disconnected before reaching its target."
        else:
            sev = "info"
            msg = f"{_label(name)} probe disconnected."
        return {"type": "disconnect", "channel": name, "severity": sev,
                "ts": ts, "message": msg}

    def _stall_step(self, name, st, ts, val):
        """Maintain the trailing window and emit a stall_start/stall_end edge."""
        st.samples.append((ts, val))
        win = self.cfg["stall_window_secs"]
        while st.samples and (ts - st.samples[0][0]) > win:
            st.samples.popleft()
        # Need a full window of data before trusting a rate.
        if len(st.samples) < 2 or (ts - st.samples[0][0]) < win * 0.8:
            return None
        t0, v0 = st.samples[0]
        dt_hr = (ts - t0) / 3600.0
        if dt_hr <= 0:
            return None
        rate = (val - v0) / dt_hr  # deg per hour
        in_band = self.cfg["stall_low"] <= val <= self.cfg["stall_high"]

        if not st.stalled:
            if in_band and rate <= self.cfg["stall_enter_rate"]:
                st.stalled = True
                return {"type": "stall_start", "channel": name, "severity": "info",
                        "ts": ts, "value": val, "rate": rate,
                        "message": (f"{_label(name)} has hit the stall at "
                                    f"{val:g}°. This can last a while - "
                                    f"consider wrapping.")}
        else:
            if rate >= self.cfg["stall_exit_rate"] or val > self.cfg["stall_high"]:
                st.stalled = False
                return {"type": "stall_end", "channel": name, "severity": "info",
                        "ts": ts, "value": val, "rate": rate,
                        "message": f"{_label(name)} is climbing again "
                                   f"({val:g}°). The stall has broken."}
        return None


def _label(name: str) -> str:
    return {"pit": "Pit", "food1": "Food 1", "food2": "Food 2",
            "ambient": "Ambient"}.get(name, name)
