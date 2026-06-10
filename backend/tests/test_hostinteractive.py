"""Tests for the host-interactive ($HMHI / Net Info) responder."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import hostinteractive as hi
from heatermeterd import protocol
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def test_netinfo_title_then_ip_then_hostname():
    # Start at title (opaque 0).
    o, l1, l2 = hi.netinfo_screen(0, hi.BUTTON_ENTER, "192.168.3.164", "BTHeaterMeter")
    assert o == 0
    assert "Network" in l1 and "Information" in l2

    # DOWN -> IP screen.
    o, l1, l2 = hi.netinfo_screen(0, hi.BUTTON_DOWN, "192.168.3.164", "BTHeaterMeter")
    assert o == 1
    assert l1 == "Network Address"
    assert l2 == "192.168.3.164"

    # DOWN again -> hostname.
    o, l1, l2 = hi.netinfo_screen(1, hi.BUTTON_DOWN, "192.168.3.164", "BTHeaterMeter")
    assert o == 2
    assert l1 == "Hostname"
    assert l2 == "BTHeaterMeter"

    # DOWN at the last screen stays put.
    o, _, _ = hi.netinfo_screen(2, hi.BUTTON_DOWN, "x", "y")
    assert o == 2

    # UP from IP -> back to title.
    o, _, _ = hi.netinfo_screen(1, hi.BUTTON_UP, "x", "y")
    assert o == 0


def test_netinfo_unknown_ip():
    o, l1, l2 = hi.netinfo_screen(1, hi.BUTTON_ENTER, None, None)
    assert l2 == "Unknown"


def test_netinfo_clamps_bad_opaque():
    o, _, _ = hi.netinfo_screen(99, hi.BUTTON_ENTER, "x", "y")
    assert o == hi._LAST
    o, _, _ = hi.netinfo_screen("garbage", hi.BUTTON_ENTER, "x", "y")
    assert o == 0


def test_reply_command_format():
    cmd = protocol.host_interactive_reply(1, "Network Address", "192.168.3.164")
    assert cmd == "/set?hi=1,Network Address,192.168.3.164\n"


def test_service_answers_hmhi():
    # Feed a $HMHI request through the service and confirm it sends a /set?hi reply
    # into the simulated board within the same tick.
    async def scenario():
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        await svc.start()
        svc.host_ip = "192.168.3.164"
        svc.host_hostname = "BTHeaterMeter"

        # Capture what the service sends back to the board.
        sent = []
        orig_send = link.send
        link.send = lambda line: (sent.append(line), orig_send(line))[1]

        # Board asks for Net Info, IP screen (opaque already 1), DOWN button.
        svc._on_line(protocol.frame("HMHI,1,0,8"))

        replies = [s for s in sent if s.startswith("/set?hi=")]
        assert replies, f"no /set?hi reply sent; got {sent}"
        # opaque advanced to hostname (2) on DOWN from IP screen (1).
        assert replies[-1].startswith("/set?hi=2,Hostname,BTHeaterMeter")

        await svc.stop()

    asyncio.run(scenario())
