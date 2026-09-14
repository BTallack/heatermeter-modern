"""Power-up policy (pure, unit-testable).

The AVR resumes whatever setpoint it had stored when power comes back, and it
boots into STARTUP with the blower running. After a real outage (or just being
unplugged and put away) that means the controller comes back *driving toward
the last cook's temperature* - e.g. 600F at 100% output into a cold, unlit pit.

Rule, decided once on the first status after the daemon starts:

* **Short gap** since the last stored sample (a power blip, <= ``blip_secs``)
  and a cook was active before it -> **resume**; the cook carries on.
* **Pit is hot** (>= ``cold_below``) or the board reports it is already at temp
  -> **resume**; never idle a live fire (covers a Pi-only reboot mid-cook).
* Otherwise -> **idle** the board (``/set?sp=O``) and tell the user.

Pure: the service feeds it the numbers; it returns ``resume`` / ``idle`` /
``none`` (nothing to do: guard off, or the board is already off / manual).
"""

from __future__ import annotations

from typing import Optional

DEFAULTS = {
    "enabled": True,
    "blip_secs": 600,      # <= this long since the last sample = a power blip
    "cold_below": 150.0,   # pit below this = not a live fire; safe to idle
}

PIDMODE_NORMAL = 2   # fork firmware "at temp" -> the board never rebooted


def sanitize(cfg: Optional[dict]) -> dict:
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        if "enabled" in cfg:
            d["enabled"] = bool(cfg["enabled"])
        try:
            d["blip_secs"] = int(float(cfg["blip_secs"]))
        except (KeyError, TypeError, ValueError):
            pass
        try:
            d["cold_below"] = float(cfg["cold_below"])
        except (KeyError, TypeError, ValueError):
            pass
    d["blip_secs"] = max(0, min(6 * 3600, d["blip_secs"]))
    d["cold_below"] = max(50.0, min(400.0, d["cold_below"]))
    return d


def _num(v):
    return v if isinstance(v, (int, float)) else None


def decide(now: float, last_ts, last_set_point, set_point, pit,
           pid_mode=None, cfg: Optional[dict] = None) -> str:
    """Return ``"resume"``, ``"idle"`` or ``"none"`` for the first status seen
    after start-up. *last_ts*/*last_set_point* describe the most recent stored
    sample (None when the history is empty)."""
    cfg = sanitize(cfg)
    sp = _num(set_point)
    if not cfg["enabled"] or sp is None or sp <= 0:
        return "none"                     # off / manual: nothing to protect
    gap = (now - last_ts) if _num(last_ts) is not None else float("inf")
    last_active = (_num(last_set_point) or 0) > 0
    if gap <= cfg["blip_secs"] and last_active:
        return "resume"
    p = _num(pit)
    if (p is not None and p >= cfg["cold_below"]) or pid_mode == PIDMODE_NORMAL:
        return "resume"                   # a live fire - hands off
    return "idle"
