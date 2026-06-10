"""Host-interactive ($HMHI) responder - drives the HeaterMeter LCD "Net Info".

The board's LCD has a host-driven "Net Info" screen. When the user navigates to
it, the firmware emits ``$HMHI,<opaque>,<topic>,<button>`` and waits up to 800ms
for the host to reply with ``/set?hi=<opaque>,<line1>,<line2>``. If we don't
answer in time, the LCD shows "Offline". This is exactly what the user saw.

This module computes those replies (pure, unit-tested) and detects the Pi's IP
and hostname. The original behaviour lived in ipwatch.lua; references in the
firmware are hmmenus.cpp (sendHostInteract / hostMsgReceived / menuNetInfo).

Protocol notes (verified against firmware + original daemon):
* Wire order of the request is ``HMHI,<opaque>,<topic>,<button>`` (opaque first).
* ``topic`` 0 == NETINFO. ``opaque`` is a scroll cursor the host echoes back.
* Buttons: UP=4 / DOWN=8 move the cursor; the board stores whatever opaque we
  return so it remembers its position.
* Each line maps to one 16-char LCD row (the board pads/truncates to 16).
* The board only enters this screen when it already thinks the host is ONLINE;
  any valid ``/...`` command line promotes OFFLINE->ONLINE, so a light periodic
  keepalive keeps the screen reachable.
"""

from __future__ import annotations

import socket
from typing import Optional, Tuple

# Button codes (firmware menus.h).
BUTTON_LEFT = 1
BUTTON_RIGHT = 2
BUTTON_UP = 4
BUTTON_DOWN = 8
BUTTON_TIMEOUT = 0x20
BUTTON_LEAVE = 0x40
BUTTON_ENTER = 0x80

TOPIC_NETINFO = 0

# Net Info screens (the opaque cursor value). We replace the original's
# heatermeter.com "device register" screen with the more useful hostname.
_TITLE = 0
_IPADDR = 1
_HOSTNAME = 2
_LAST = 2

LCD_WIDTH = 16


def _center(s: str, width: int = LCD_WIDTH) -> str:
    s = s[:width]
    pad = (width - len(s)) // 2
    return " " * pad + s


def netinfo_screen(opaque, button, ip: Optional[str],
                   hostname: Optional[str]) -> Tuple[int, str, str]:
    """Compute the reply for a NETINFO ``$HMHI`` request.

    Returns ``(new_opaque, line1, line2)``. UP/DOWN move the cursor within
    ``[0, _LAST]``; the cursor is echoed back so the board keeps its place.
    """
    try:
        opaque = int(opaque)
    except (ValueError, TypeError):
        opaque = 0
    opaque = max(_TITLE, min(_LAST, opaque))

    if button == BUTTON_DOWN and opaque < _LAST:
        opaque += 1
    elif button == BUTTON_UP and opaque > _TITLE:
        opaque -= 1

    if opaque == _IPADDR:
        return opaque, "Network Address", (ip or "Unknown")
    if opaque == _HOSTNAME:
        return opaque, "Hostname", (hostname or "heatermeter")
    return _TITLE, _center("Network"), _center("Information")


# -- host info detection (I/O, not pure) ------------------------------------

def detect_ip() -> Optional[str]:
    """Best-effort local IP of the interface with the default route.

    Uses the UDP-connect trick: connecting a datagram socket sets the local
    address to the routing interface without actually sending anything, so it
    works offline too (returns the LAN IP)."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def get_hostname() -> Optional[str]:
    try:
        return socket.gethostname()
    except OSError:
        return None
