"""Guided Cooks: pick a protein and style, and the cooker is configured and
coached through the whole cook (pure logic; the service owns side effects).

A guided cook bundles what MEATER's Guided Cook does - protein, cut, target,
rest - PLUS the thing only a controller can do: it configures the pit itself
(setpoint), watches the cook's milestones (the stall, the wrap window, the
finish), and prompts at the right moments. The service turns prompts into
WebSocket events, push notifications, LCD toasts, and timeline markers.

``GUIDED_COOKS`` is the curated offline catalog. ``GuidedRun`` is the runtime
state machine: feed it samples + the live stall verdict and it returns the
prompts that should fire. Everything is deterministic and unit-testable.
"""

from __future__ import annotations

from typing import Optional

# Milestone kinds evaluated by GuidedRun.update():
#   temp_at_least  - food temp crossed a threshold (e.g. the wrap window)
#   stall_start    - the probe watcher declared a stall
#   target_reached - food temp crossed the final target
GUIDED_COOKS = [
    {
        "key": "brisket_packer", "label": "Brisket (packer)", "category": "Beef",
        "description": "Low and slow, wrapped at the stall, finished by feel "
                       "around 203 and rested long.",
        "pit_setpoint": 250, "food_target": 203, "probe_name": "Brisket",
        "rest_secs": 3600,
        "milestones": [
            {"key": "settle", "when": {"temp_at_least": 140},
             "prompt": "Smoke is doing its work. Resist peeking; you are "
                       "about two thirds of the way to the stall."},
            {"key": "wrap", "when": {"stall_start": True, "temp_at_least": 150},
             "prompt": "The stall has arrived. Wrap in butcher paper or foil "
                       "to power through, or ride it out for more bark.",
             "wrap_point": True},
            {"key": "probe_tender", "when": {"temp_at_least": 198},
             "prompt": "Start probing for tenderness. It is done when the "
                       "probe slides in like soft butter, not at a number."},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull it, wrap in towels, and rest at least an hour. "
                       "It gets better the longer you wait."},
        ],
    },
    {
        "key": "pork_butt", "label": "Pork Butt (pulled pork)", "category": "Pork",
        "description": "Forgiving low and slow to 203 for pulling.",
        "pit_setpoint": 250, "food_target": 203, "probe_name": "Pork Butt",
        "rest_secs": 1800,
        "milestones": [
            {"key": "spritz", "when": {"temp_at_least": 140},
             "prompt": "Bark is setting. Spritz hourly from here if that is "
                       "your style."},
            {"key": "wrap", "when": {"stall_start": True, "temp_at_least": 150},
             "prompt": "Stall time. Wrap to push through, or wait it out - "
                       "pork butt forgives either choice.", "wrap_point": True},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull it and rest 30 minutes, then shred while warm."},
        ],
    },
    {
        "key": "ribs_321", "label": "Pork Ribs (3-2-1 style)", "category": "Pork",
        "description": "Spare ribs: smoke, wrap, glaze. Tracks the wrap window "
                       "by temperature.",
        "pit_setpoint": 225, "food_target": 195, "probe_name": "Ribs",
        "rest_secs": 600,
        "milestones": [
            {"key": "wrap", "when": {"temp_at_least": 165},
             "prompt": "About three hours in: wrap with butter and sugar, "
                       "meat side down.", "wrap_point": True},
            {"key": "glaze", "when": {"temp_at_least": 188},
             "prompt": "Unwrap, glaze, and let the sauce set for the last "
                       "stretch."},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Check for the bend test; rest 10 minutes and slice."},
        ],
    },
    {
        "key": "chicken_whole", "label": "Whole Chicken", "category": "Poultry",
        "description": "Hotter pit for crisp skin, breast to 160 with carryover.",
        "pit_setpoint": 375, "food_target": 160, "probe_name": "Chicken",
        "rest_secs": 900,
        "milestones": [
            {"key": "check", "when": {"temp_at_least": 140},
             "prompt": "Home stretch. If the skin needs color, this is the "
                       "time to open vents or raise heat."},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull at 160; carryover during a 15 minute rest "
                       "finishes it to a safe 165."},
        ],
    },
    {
        "key": "turkey_whole", "label": "Whole Turkey", "category": "Poultry",
        "description": "Steady 325 to 160 in the breast, rest to finish.",
        "pit_setpoint": 325, "food_target": 160, "probe_name": "Turkey",
        "rest_secs": 1800,
        "milestones": [
            {"key": "baste", "when": {"temp_at_least": 130},
             "prompt": "Optional: baste or butter the skin now for color."},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull and rest 30 minutes under loose foil before "
                       "carving."},
        ],
    },
    {
        "key": "beef_ribs", "label": "Beef Short Ribs", "category": "Beef",
        "description": "Brisket on a stick: 250 to 205, probe tender.",
        "pit_setpoint": 250, "food_target": 205, "probe_name": "Beef Ribs",
        "rest_secs": 1200,
        "milestones": [
            {"key": "wrap", "when": {"stall_start": True, "temp_at_least": 150},
             "prompt": "Stalled. Beef ribs usually ride it out unwrapped for "
                       "maximum bark, but wrap if you are short on time.",
             "wrap_point": True},
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Probe between the bones; pull when butter-tender and "
                       "rest 20 minutes."},
        ],
    },
    {
        "key": "salmon", "label": "Smoked Salmon", "category": "Seafood",
        "description": "Gentle 180 pit to 145 internal.",
        "pit_setpoint": 180, "food_target": 145, "probe_name": "Salmon",
        "rest_secs": 300,
        "milestones": [
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull at 145 - the albumin just starting to show is "
                       "your visual cue. Rest briefly and serve."},
        ],
    },
    {
        "key": "reverse_sear", "label": "Reverse-Sear Steak/Roast", "category": "Beef",
        "description": "Slow smoke to just under doneness, then sear hot off "
                       "the controller.",
        "pit_setpoint": 225, "food_target": 115, "probe_name": "Steak",
        "rest_secs": 600,
        "milestones": [
            {"key": "pull", "when": {"target_reached": True},
             "prompt": "Pull now and sear over the hottest fire you can make: "
                       "about a minute per side for a perfect medium-rare."},
        ],
    },
]


def find_cook(key: str) -> Optional[dict]:
    for c in GUIDED_COOKS:
        if c["key"] == key:
            return c
    return None


def catalog() -> list:
    """The catalog as served to the UI (no runtime fields)."""
    return [{k: c[k] for k in ("key", "label", "category", "description",
                               "pit_setpoint", "food_target", "rest_secs")}
            | {"milestones": [{"key": m["key"], "prompt": m["prompt"],
                               "wrap_point": bool(m.get("wrap_point"))}
                              for m in c["milestones"]]}
            for c in GUIDED_COOKS]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


class GuidedRun:
    """Runtime state for one guided cook on one food channel.

    Feed :meth:`update` once per status sample; it returns the milestone dicts
    that fire on this sample (each fires once). The service turns those into
    notifications. ``wrap_pending`` is set when a wrap-point milestone fires and
    clears when the user confirms the wrap (A4 closed-loop)."""

    def __init__(self, cook: dict, channel: str, started_ts: float):
        self.cook = cook
        self.channel = channel
        self.started_ts = started_ts
        self.fired: set = set()
        self.wrap_pending = False
        self.wrapped = False
        self.done = False

    def update(self, ts: float, food_temp, *, stalled: bool = False) -> list:
        if self.done:
            return []
        v = _num(food_temp)
        target = self.cook.get("food_target")
        fired = []
        for m in self.cook.get("milestones", []):
            if m["key"] in self.fired:
                continue
            w = m.get("when", {})
            ok = True
            if "temp_at_least" in w:
                ok = ok and v is not None and v >= w["temp_at_least"]
            if w.get("stall_start"):
                ok = ok and stalled
            if w.get("target_reached"):
                ok = ok and v is not None and target is not None and v >= target
            if not ok:
                continue
            self.fired.add(m["key"])
            if m.get("wrap_point"):
                self.wrap_pending = True
            if w.get("target_reached"):
                self.done = True
            fired.append(m)
        return fired

    def confirm_wrap(self) -> bool:
        """User says the meat is wrapped. Returns True if it changed state."""
        if not self.wrap_pending or self.wrapped:
            return False
        self.wrap_pending = False
        self.wrapped = True
        return True

    def status(self) -> dict:
        return {
            "key": self.cook["key"],
            "label": self.cook["label"],
            "channel": self.channel,
            "started_ts": self.started_ts,
            "pit_setpoint": self.cook.get("pit_setpoint"),
            "food_target": self.cook.get("food_target"),
            "rest_secs": self.cook.get("rest_secs", 0),
            "fired": sorted(self.fired),
            "wrap_pending": self.wrap_pending,
            "wrapped": self.wrapped,
            "done": self.done,
            "milestones": [{"key": m["key"], "prompt": m["prompt"],
                            "wrap_point": bool(m.get("wrap_point")),
                            "fired": m["key"] in self.fired}
                           for m in self.cook.get("milestones", [])],
        }
