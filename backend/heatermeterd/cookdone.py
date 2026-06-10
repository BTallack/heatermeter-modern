"""Meater-style automatic cook-completion detection (pure, unit-testable).

A food probe's cook is considered done once it has *reached its target* and is
then *removed* - a sustained drop toward ambient - for a confirmation window.

The nuance: a probe moved to a different spot of a large cut (to check doneness
elsewhere) must NOT end the cook, even though it dips. The discriminator is the
direction of travel: a probe left in air keeps falling toward ambient and never
recovers, whereas a reinserted probe RISES off its air-cooled minimum - even when
reinserted into a cooler, less-done spot. So a big drop only *arms* a pull
candidate; a subsequent rise cancels it.

The whole cook completes when every food probe that is in use has been pulled and
confirmed: at least one probe reached its target and was confirmed-removed, and
no probe is still actively cooking (in food, climbing toward an unreached
target).

Temperatures are in whatever unit the board reports; the margin defaults assume
Fahrenheit (the only deployed unit). All thresholds are configurable.
"""

from __future__ import annotations

from typing import Optional

# Probe indexes that carry food (pit=0 and ambient=3 are excluded).
FOOD_PROBES = (1, 2)

# Used to judge "near ambient" / "still in food" when no ambient probe is read.
FALLBACK_AMBIENT = 80.0

DEFAULTS = {
    "enabled": True,
    "grace_secs": 180,        # probe must stay out this long to confirm "done"
    "drop_margin": 35.0,      # degrees below target that arms a pull candidate
    "rise_delta": 15.0,       # rise off the post-pull minimum that = reinsertion
    "ambient_band": 50.0,     # "near ambient" / "in food" boundary above ambient
    "on_complete": "notify",  # notify | shutdown | keep_warm
    "keep_warm_temp": 150.0,  # used only when on_complete == "keep_warm"
}

_ON_COMPLETE = ("notify", "shutdown", "keep_warm")


def sanitize(cfg: Optional[dict]) -> dict:
    """Merge *cfg* over the defaults, coercing and clamping every field."""
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        for k in ("drop_margin", "rise_delta", "ambient_band", "keep_warm_temp"):
            try:
                d[k] = float(cfg[k])
            except (KeyError, TypeError, ValueError):
                pass
        try:
            d["grace_secs"] = int(float(cfg["grace_secs"]))
        except (KeyError, TypeError, ValueError):
            pass
        if cfg.get("on_complete") in _ON_COMPLETE:
            d["on_complete"] = cfg["on_complete"]
    d["grace_secs"] = max(30, min(3600, int(d["grace_secs"])))
    d["drop_margin"] = max(5.0, min(300.0, d["drop_margin"]))
    d["rise_delta"] = max(2.0, min(150.0, d["rise_delta"]))
    d["ambient_band"] = max(5.0, min(200.0, d["ambient_band"]))
    d["keep_warm_temp"] = max(0.0, min(600.0, d["keep_warm_temp"]))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


class CookDoneDetector:
    """Stateful per-cook detector. Feed it one sample per status update via
    :meth:`update`; it returns events and signals when the cook newly completes.
    Reset it (:meth:`reset`) at the start of each cook/session."""

    def __init__(self, cfg: Optional[dict] = None) -> None:
        self.cfg = sanitize(cfg)
        self.probes: dict[int, dict] = {}
        self.completed = False

    def set_config(self, cfg: dict) -> None:
        self.cfg = sanitize(cfg)

    def reset(self) -> None:
        self.probes.clear()
        self.completed = False

    def mark_already_complete(self) -> None:
        """Used on daemon restart when the resumed session was already completed,
        so we do not re-notify."""
        self.completed = True

    def _ps(self, idx: int) -> dict:
        return self.probes.setdefault(idx, {
            "reached": False, "pulled_since": None, "pull_min": None,
            "done": False, "done_since": None})

    def update(self, ts: float, temps: dict, targets: dict,
               ambient=None) -> dict:
        """Ingest one sample. *temps*/*targets* map probe index -> value (a
        missing or None temp means the probe is not reading). Returns
        ``{"events": [...], "completed": bool, "done_at": float|None}``."""
        if not self.cfg["enabled"]:
            return {"events": [], "completed": False, "done_at": None}

        amb = _num(ambient)
        amb_ref = amb if amb is not None else FALLBACK_AMBIENT
        drop = self.cfg["drop_margin"]
        rise = self.cfg["rise_delta"]
        band = self.cfg["ambient_band"]
        grace = self.cfg["grace_secs"]

        participating = [i for i in FOOD_PROBES
                         if _num(targets.get(i)) is not None and targets[i] > 0]
        events = []

        for idx in participating:
            target = float(targets[idx])
            temp = _num(temps.get(idx))
            ps = self._ps(idx)
            if ps["done"]:
                continue

            if temp is not None and temp >= target:
                ps["reached"] = True
            if not ps["reached"]:
                continue

            arm_line = target - drop
            if ps["pulled_since"] is None:
                # Arm a pull candidate on a big drop toward ambient (or a probe
                # that went blank after being in food).
                near_ambient = (temp is None) or (temp <= amb_ref + band)
                armed = (temp is None) or (temp <= arm_line)
                if armed and near_ambient:
                    ps["pulled_since"] = ts
                    ps["pull_min"] = temp
            else:
                if temp is not None:
                    ps["pull_min"] = (temp if ps["pull_min"] is None
                                      else min(ps["pull_min"], temp))
                    reinserted = (
                        (ps["pull_min"] is not None
                         and (temp - ps["pull_min"]) >= rise)  # rose off the min
                        or temp > arm_line)                    # back above the arm line
                    if reinserted:
                        ps["pulled_since"] = None
                        ps["pull_min"] = None
                        events.append({"probe": idx, "event": "repositioned"})
                        continue
                if (ts - ps["pulled_since"]) >= grace:
                    ps["done"] = True
                    ps["done_since"] = ps["pulled_since"]
                    events.append({"probe": idx, "event": "done",
                                   "since": ps["pulled_since"]})

        # Completion: at least one targeted probe reached + done, and NO other
        # targeted probe is still in use. A probe is "in use" if it reached its
        # target at some point OR is currently plugged in and reading - so a
        # second probe that is still early in its cook (cold, below the ambient
        # band) still blocks completion, and the keep-warm/shutdown action only
        # runs once every in-use probe target is complete. Only a targeted but
        # unplugged probe that never reached is treated as unused.
        reached_done = [i for i in participating if self._ps(i)["done"]]
        blockers = []
        for i in participating:
            ps = self._ps(i)
            if ps["done"]:
                continue
            if ps["reached"] or _num(temps.get(i)) is not None:
                blockers.append(i)

        newly_completed = False
        done_at = None
        if reached_done and not blockers and not self.completed:
            self.completed = True
            newly_completed = True
            done_at = max(self._ps(i)["done_since"] for i in reached_done)

        return {"events": events, "completed": newly_completed, "done_at": done_at}
