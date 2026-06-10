"""PID auto-tuning via the relay (Astrom-Hagglund) method.

Auto-tuning a BBQ controller: drive the blower as an on/off "relay" around the
setpoint and watch the pit temperature oscillate. From the amplitude (a) and
period (Tu) of that oscillation we derive the ultimate gain Ku and ultimate
period Tu, then apply tuning rules to get Kp/Ki/Kd.

This module is split into:
* :func:`compute_gains` - PURE math (relay -> Ku/Tu -> PID), unit-tested.
* :class:`PeakDetector` - PURE oscillation analysis over a temperature series.
* :class:`AutoTuner` - the async driver that toggles the fan via the link,
  records the response, and (on success) writes the new PID constants.

Safety is paramount because this drives a real fire:
* a hard pit ceiling aborts the run if the temperature runs away;
* a wall-clock timeout aborts a stalled run;
* on abort/finish the controller is returned to automatic mode at the original
  setpoint.

The relay method needs only a few oscillation cycles, but on a slow cooker that
can still be 20-40 minutes. The tuner reports progress so the UI can show it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional


# -- pure tuning math -------------------------------------------------------

# Tuning rule sets: name -> (Kp, Ti, Td) multipliers in the standard form
# Kp = c_p * Ku ; Ti = c_i * Tu ; Td = c_d * Tu. We convert to the firmware's
# Kp/Ki/Kd where Ki = Kp/Ti and Kd = Kp*Td.
TUNING_RULES = {
    # Classic Ziegler-Nichols (more aggressive, some overshoot).
    "ziegler_nichols": (0.6, 0.5, 0.125),
    # Tyreus-Luyben (gentler, less overshoot - good default for BBQ).
    "tyreus_luyben": (0.45, 2.2, 0.1576),
    # "No overshoot" Ziegler-Nichols variant (most conservative).
    "no_overshoot": (0.2, 0.5, 0.333),
}


@dataclass
class TuneResult:
    ku: float
    tu: float
    kp: float
    ki: float
    kd: float
    rule: str
    cycles: int

    def to_dict(self) -> dict:
        return asdict(self)


def compute_gains(relay_amplitude: float, osc_amplitude: float,
                  period_s: float, rule: str = "tyreus_luyben",
                  cycles: int = 0) -> TuneResult:
    """From relay amplitude d (output %), oscillation amplitude a (degrees), and
    period Tu (seconds), compute Ku and the PID gains for *rule*.

    Ku = 4d / (pi * a)  (describing-function approximation of the relay).
    """
    if osc_amplitude <= 0 or period_s <= 0:
        raise ValueError("oscillation amplitude and period must be positive")
    ku = (4.0 * relay_amplitude) / (math.pi * osc_amplitude)
    cp, ci, cd = TUNING_RULES.get(rule, TUNING_RULES["tyreus_luyben"])
    kp = cp * ku
    ti = ci * period_s
    td = cd * period_s
    ki = kp / ti if ti > 0 else 0.0
    kd = kp * td
    return TuneResult(ku=ku, tu=period_s, kp=kp, ki=ki, kd=kd, rule=rule,
                      cycles=cycles)


class PeakDetector:
    """Detects alternating maxima/minima in a temperature series to measure the
    oscillation amplitude and period. Fed one (t, value) sample at a time."""

    def __init__(self, hysteresis: float = 0.5) -> None:
        self.hysteresis = hysteresis
        self.peaks: list[tuple[float, float]] = []   # (t, value) maxima
        self.troughs: list[tuple[float, float]] = []  # (t, value) minima
        self._rising: Optional[bool] = None
        self._last_extreme: Optional[tuple[float, float]] = None

    def add(self, t: float, v: float) -> None:
        if self._last_extreme is None:
            self._last_extreme = (t, v)
            return
        le_t, le_v = self._last_extreme
        if self._rising in (None, True):
            if v > le_v:
                self._last_extreme = (t, v)
            elif le_v - v >= self.hysteresis:
                # Was rising, now fell past hysteresis -> le was a peak.
                if self._rising:
                    self.peaks.append(self._last_extreme)
                self._rising = False
                self._last_extreme = (t, v)
        if self._rising in (None, False):
            if v < le_v:
                self._last_extreme = (t, v)
            elif v - le_v >= self.hysteresis:
                if self._rising is False:
                    self.troughs.append(self._last_extreme)
                self._rising = True
                self._last_extreme = (t, v)

    def amplitude(self) -> Optional[float]:
        """Mean peak-to-trough amplitude over matched pairs."""
        n = min(len(self.peaks), len(self.troughs))
        if n == 0:
            return None
        amps = [self.peaks[i][1] - self.troughs[i][1] for i in range(n)]
        return sum(amps) / len(amps)

    def period(self) -> Optional[float]:
        """Mean period from successive peak-to-peak times."""
        if len(self.peaks) < 2:
            return None
        diffs = [self.peaks[i + 1][0] - self.peaks[i][0]
                 for i in range(len(self.peaks) - 1)]
        return sum(diffs) / len(diffs)

    def completed_cycles(self) -> int:
        return min(len(self.peaks), len(self.troughs))


# -- live driver ------------------------------------------------------------

class AutoTuneSession:
    """Drives a relay auto-tune against the live board via the service.

    Toggles the fan between *relay_high* and *relay_low* (manual output %) as the
    pit crosses *setpoint* +/- *hysteresis*, records the resulting oscillation,
    and after *max_cycles* completed cycles computes and writes PID gains. Aborts
    on *pit_ceiling* breach or *max_seconds* timeout. All control goes through
    ``service.send_command_threadsafe`` so it is safe from the loop thread.
    """

    def __init__(self, service, setpoint: float, rule: str,
                 relay_high: float, relay_low: float, hysteresis: float,
                 max_cycles: int, max_seconds: float, pit_ceiling: float) -> None:
        self.service = service
        self.setpoint = setpoint
        self.rule = rule
        self.relay_high = relay_high
        self.relay_low = relay_low
        self.hysteresis = hysteresis
        self.max_cycles = max_cycles
        self.max_seconds = max_seconds
        self.pit_ceiling = pit_ceiling

        self.detector = PeakDetector(hysteresis=max(0.3, hysteresis * 0.5))
        self.phase = "idle"          # idle|running|done
        self.done = False
        self.result: Optional[TuneResult] = None
        self.error: Optional[str] = None
        self._start_ts: Optional[float] = None
        self._relay_state: Optional[bool] = None   # True = high output
        self._samples = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.phase = "running"
        # Put the board into manual mode at the high relay level to begin.
        self._set_relay(True, initial=True)
        self.service._emit({"type": "autotune", "phase": "running",
                            "setpoint": self.setpoint, "rule": self.rule})

    def abort(self, reason: str) -> None:
        if self.done:
            return
        self.error = reason
        self._finish(success=False)

    def _finish(self, success: bool) -> None:
        self.done = True
        self.phase = "done"
        # Always return the board to automatic control at the original setpoint.
        from . import protocol
        self.service.send_command_threadsafe(
            protocol.set_setpoint(self.setpoint))
        self.service._emit({
            "type": "autotune", "phase": "done",
            "success": success, "error": self.error,
            "result": self.result.to_dict() if self.result else None,
        })

    # -- control ----------------------------------------------------------

    def _set_relay(self, high: bool, initial: bool = False) -> None:
        if high == self._relay_state and not initial:
            return
        self._relay_state = high
        from . import protocol
        pct = self.relay_high if high else self.relay_low
        # Manual output uses a negative setpoint (-pct). -0 means 0%.
        self.service.send_command_threadsafe(protocol.set_manual_output(int(pct)))

    # -- sample ingestion (called from the loop thread) -------------------

    def on_sample(self, ts: float, pit) -> None:
        if self.done or pit is None:
            return
        if self._start_ts is None:
            self._start_ts = ts
        self._samples += 1

        # Safety: pit runaway or timeout.
        if pit >= self.pit_ceiling:
            self.abort(f"pit exceeded ceiling {self.pit_ceiling:.0f}")
            return
        if (ts - self._start_ts) > self.max_seconds:
            self.abort("timed out")
            return

        # Relay logic: above setpoint -> fan off (cool), below -> fan on (heat).
        if pit > self.setpoint + self.hysteresis:
            self._set_relay(False)
        elif pit < self.setpoint - self.hysteresis:
            self._set_relay(True)

        # Record oscillation.
        self.detector.add(ts, pit)
        if self.detector.completed_cycles() >= self.max_cycles:
            self._compute_and_finish()

    def _compute_and_finish(self) -> None:
        amp = self.detector.amplitude()
        period = self.detector.period()
        if amp is None or period is None or amp <= 0 or period <= 0:
            self.abort("could not measure a clean oscillation")
            return
        relay_amp = (self.relay_high - self.relay_low)
        try:
            self.result = compute_gains(
                relay_amplitude=relay_amp, osc_amplitude=amp, period_s=period,
                rule=self.rule, cycles=self.detector.completed_cycles())
        except ValueError as e:
            self.abort(str(e))
            return
        # Write the new PID constants to the board.
        from . import protocol
        r = self.result
        for param, val in (("p", round(r.kp, 4)), ("i", round(r.ki, 5)),
                           ("d", round(r.kd, 4))):
            self.service.send_command_threadsafe(protocol.set_pid(param, val))
        self.service.send_command_threadsafe(protocol.request_config())
        self._finish(success=True)

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "done": self.done,
            "setpoint": self.setpoint,
            "rule": self.rule,
            "cycles": self.detector.completed_cycles(),
            "max_cycles": self.max_cycles,
            "samples": self._samples,
            "amplitude": self.detector.amplitude(),
            "period": self.detector.period(),
            "error": self.error,
            "result": self.result.to_dict() if self.result else None,
        }
