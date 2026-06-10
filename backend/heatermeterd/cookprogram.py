"""Multi-stage cook programs.

A cook program is an ordered list of stages. Each stage sets the pit setpoint and
defines when to advance to the next stage. Transitions can be:

* ``time`` - advance after N seconds in this stage.
* ``probe`` - advance when a food/pit probe reaches a target temperature.
* ``manual`` - wait for the user to advance (or the program just holds here).

This subsumes "keep-warm" and "auto-shutdown": a keep-warm is just a final stage
with a low setpoint, and auto-shutdown is a final stage with setpoint 0 (manual
output 0%). Example (brisket):

    [
      {"name": "Smoke",     "setpoint": 250, "advance": {"type": "probe",
          "channel": "food1", "temp": 165}},
      {"name": "Wrap+cook", "setpoint": 250, "advance": {"type": "probe",
          "channel": "food1", "temp": 203}},
      {"name": "Keep warm", "setpoint": 150, "advance": {"type": "manual"}},
    ]

The engine is split:
* :func:`validate_program` and :class:`ProgramState` are pure (no I/O), so the
  stage-advance logic is unit-tested without hardware.
* :class:`CookProgramRunner` wires a validated program to the live service: it
  applies each stage's setpoint via the link and ticks on every status sample.

Programs are persisted in the store (a small JSON blob per saved program) so the
user can reuse "My Brisket" across cooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

VALID_CHANNELS = {"pit", "food1", "food2", "ambient"}
VALID_ADVANCE = {"time", "probe", "manual"}


class ProgramError(ValueError):
    pass


def validate_program(stages) -> list:
    """Validate and normalise a list of stage dicts. Raises ProgramError."""
    if not isinstance(stages, list) or not stages:
        raise ProgramError("a program needs at least one stage")
    out = []
    for i, st in enumerate(stages):
        if not isinstance(st, dict):
            raise ProgramError(f"stage {i} is not an object")
        name = str(st.get("name") or f"Stage {i + 1}")
        # setpoint: a number (pit target F), or null/"off" for shutdown (0% fan).
        sp = st.get("setpoint")
        shutdown = False
        if sp is None or (isinstance(sp, str) and sp.lower() in ("off", "shutdown")):
            shutdown = True
            sp = None
        else:
            try:
                sp = float(sp)
            except (ValueError, TypeError):
                raise ProgramError(f"stage {i} setpoint must be a number or 'off'")
        adv = st.get("advance") or {"type": "manual"}
        atype = adv.get("type", "manual")
        if atype not in VALID_ADVANCE:
            raise ProgramError(f"stage {i} advance type must be one of {sorted(VALID_ADVANCE)}")
        norm_adv = {"type": atype}
        if atype == "time":
            try:
                norm_adv["seconds"] = float(adv["seconds"])
            except (KeyError, ValueError, TypeError):
                raise ProgramError(f"stage {i} time advance needs 'seconds'")
            if norm_adv["seconds"] <= 0:
                raise ProgramError(f"stage {i} time advance must be positive")
        elif atype == "probe":
            ch = adv.get("channel")
            if ch not in VALID_CHANNELS:
                raise ProgramError(f"stage {i} probe channel must be one of {sorted(VALID_CHANNELS)}")
            try:
                norm_adv["temp"] = float(adv["temp"])
            except (KeyError, ValueError, TypeError):
                raise ProgramError(f"stage {i} probe advance needs 'temp'")
            norm_adv["channel"] = ch
        out.append({"name": name, "setpoint": sp, "shutdown": shutdown,
                    "advance": norm_adv})
    return out


@dataclass
class ProgramState:
    """Pure runtime state of a running program. Advanced by feeding samples."""
    stages: list
    stage_index: int = 0
    stage_started_ts: Optional[float] = None
    done: bool = False

    @property
    def current(self) -> Optional[dict]:
        if self.done or self.stage_index >= len(self.stages):
            return None
        return self.stages[self.stage_index]

    def start(self, ts: float) -> dict:
        self.stage_index = 0
        self.stage_started_ts = ts
        self.done = False
        return self.stages[0]

    def should_advance(self, ts: float, temps: dict) -> bool:
        """Return True if the current stage's advance condition is met. *temps*
        maps channel -> current temperature (or None)."""
        stage = self.current
        if stage is None:
            return False
        adv = stage["advance"]
        if adv["type"] == "time":
            if self.stage_started_ts is None:
                return False
            return (ts - self.stage_started_ts) >= adv["seconds"]
        if adv["type"] == "probe":
            v = temps.get(adv["channel"])
            return isinstance(v, (int, float)) and v >= adv["temp"]
        return False  # manual: never auto-advances

    def advance(self, ts: float) -> Optional[dict]:
        """Move to the next stage. Returns the new stage, or None if finished."""
        self.stage_index += 1
        if self.stage_index >= len(self.stages):
            self.done = True
            self.stage_started_ts = None
            return None
        self.stage_started_ts = ts
        return self.current

    def to_dict(self) -> dict:
        cur = self.current
        return {
            "stage_index": self.stage_index,
            "stage_count": len(self.stages),
            "stage_name": cur["name"] if cur else None,
            "stage_started_ts": self.stage_started_ts,
            "done": self.done,
            "stages": self.stages,
        }


class CookProgramRunner:
    """Drives a validated program against the live board via the service.

    On start it applies stage 0's setpoint. On each status sample it checks the
    current stage's advance condition and, when met, applies the next stage's
    setpoint. A ``manual`` stage holds until :meth:`advance_now` is called.
    """

    def __init__(self, service, stages: list, name: str = "") -> None:
        self.service = service
        self.name = name
        self.state = ProgramState(stages=stages)

    def start(self, ts: float) -> None:
        stage = self.state.start(ts)
        self._apply_stage(stage)
        self.service._emit({"type": "program", "event": "started",
                            "name": self.name, "program": self.state.to_dict()})
        self.service._record_event(
            ts, "stage", label=f"Stage: {stage.get('name') or 'Stage 1'}"
                               + (f" ({self.name})" if self.name else ""))

    def _apply_stage(self, stage: dict) -> None:
        from . import protocol
        if stage.get("shutdown"):
            # Shutdown stage: force manual 0% output.
            self.service.send_command_threadsafe(protocol.set_manual_output(0))
        elif stage.get("setpoint") is not None:
            self.service.send_command_threadsafe(
                protocol.set_setpoint(int(round(stage["setpoint"]))))

    def on_sample(self, ts: float, temps: dict) -> None:
        if self.state.done:
            return
        if self.state.should_advance(ts, temps):
            prev = self.state.current
            stage = self.state.advance(ts)
            if stage is None:
                self.service._emit({"type": "program", "event": "completed",
                                    "name": self.name,
                                    "program": self.state.to_dict()})
                self.service._record_event(ts, "program_done",
                                           label="Program complete")
            else:
                self._apply_stage(stage)
                self.service._emit({"type": "program", "event": "advanced",
                                    "name": self.name, "from": prev["name"],
                                    "program": self.state.to_dict()})
                self.service._record_event(
                    ts, "stage", label=f"Stage: {stage.get('name') or 'next'}")

    def advance_now(self, ts: float) -> None:
        """User-triggered advance (for manual stages)."""
        if self.state.done:
            return
        stage = self.state.advance(ts)
        if stage is None:
            self.service._emit({"type": "program", "event": "completed",
                                "name": self.name, "program": self.state.to_dict()})
            self.service._record_event(ts, "program_done",
                                       label="Program complete")
        else:
            self._apply_stage(stage)
            self.service._emit({"type": "program", "event": "advanced",
                                "name": self.name, "program": self.state.to_dict()})
            self.service._record_event(
                ts, "stage", label=f"Stage: {stage.get('name') or 'next'}")

    def status(self) -> dict:
        d = self.state.to_dict()
        d["name"] = self.name
        return d
