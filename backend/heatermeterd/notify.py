"""Push notifications via ntfy (https://ntfy.sh or a self-hosted server).

Why ntfy: the dashboard's browser notifications only work while a tab is open on
the LAN, and true web-push needs HTTPS+VAPID (the Pi serves plain HTTP). ntfy is
a dead-simple HTTP-POST topic service that the phone app subscribes to, so it
delivers away-from-home alerts with no HTTPS on our side.

:func:`send` does a blocking HTTP POST (stdlib urllib only, no new deps), so the
service calls it off the event loop via run_in_executor. Config is a plain dict
persisted by the service (data/notify.json), mirroring the MQTT config pattern.
"""

from __future__ import annotations

import urllib.error
import urllib.request

DEFAULT_SERVER = "https://ntfy.sh"


def default_config() -> dict:
    return {
        "enabled": False,
        "server": DEFAULT_SERVER,
        "topic": "",            # the ntfy topic your phone subscribes to
        "token": "",            # optional bearer token for protected topics
        "debounce_sec": 30,     # an alarm must ring this long before we push
        "repeat_min": 0,        # re-push every N minutes while ringing (0 = once)
        "dark_timeout_sec": 90, # alert if no board data for this long (0 = off)
    }


def send(config: dict, title: str, message: str, priority: str = "default",
         tags: str = "", timeout: float = 8.0) -> dict:
    """POST a notification to ntfy. Blocking; run off the event loop.

    Returns {"ok": bool, "error": str|None}.
    """
    server = (config.get("server") or DEFAULT_SERVER).rstrip("/")
    topic = (config.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "no ntfy topic configured"}
    url = f"{server}/{topic}"
    # ntfy carries metadata in headers; they must be latin-1 safe.
    headers = {"Priority": str(priority)}
    try:
        headers["Title"] = title.encode("latin-1", "replace").decode("latin-1")
    except Exception:
        headers["Title"] = "HeaterMeter"
    if tags:
        headers["Tags"] = tags
    token = (config.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            ok = 200 <= status < 300
            return {"ok": ok, "error": None if ok else f"HTTP {status}"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
