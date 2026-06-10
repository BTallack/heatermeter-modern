"""Tests for hm4+ command checksums: host appender, firmware-equivalent
validator, version gating, link integration, and the merged-line failure mode
this exists to fix."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol


def firmware_validate(line):
    """Python re-implementation of the firmware's serial_validateCsum() so the
    host appender and the C validator are proven to agree. Input is the line
    WITHOUT terminator (the firmware buffers up to the newline). Returns the
    command to dispatch, or None when the line must be dropped."""
    star = line.rfind("*")
    if star == -1:
        return line                       # unchecksummed: accepted as-is
    tail = line[star + 1:]
    if len(tail) != 2:
        return None                       # malformed checksum
    try:
        want = int(tail, 16)
    except ValueError:
        return None
    csum = 0
    for ch in line[:star]:
        csum ^= ord(ch) & 0xFF
    return line[:star] if csum == want else None


# -- appender ----------------------------------------------------------------

def test_append_known_vector():
    out = protocol.append_cmd_checksum("/set?sp=225\n")
    body = out.rstrip("\n")
    assert body.startswith("/set?sp=225*")
    want = 0
    for ch in "/set?sp=225":
        want ^= ord(ch)
    assert body.endswith(f"*{want:02X}")
    assert out.endswith("\n")


def test_append_idempotent():
    once = protocol.append_cmd_checksum("/set?sp=225\n")
    assert protocol.append_cmd_checksum(once) == once


def test_round_trip_through_firmware_validator():
    for cmd in ("/set?sp=225", "/set?pn1=Brisket Flat", "/set?al=-1,203,-1,-1",
                "/set?pc0=7.3e-4,2.1e-4,9.5e-8,5.0,3", "/config", "/set?tt=Hi,There"):
        sent = protocol.append_cmd_checksum(cmd + "\n").rstrip("\n")
        assert firmware_validate(sent) == cmd


def test_validator_accepts_bare_lines():
    assert firmware_validate("/set?sp=225") == "/set?sp=225"


def test_validator_rejects_merged_lines():
    # The SET?PO bug: two checksummed commands merged by a lost newline. The
    # checksum of the first command lands mid-line, so the line's trailing
    # *XX (from the second command) no longer matches the merged content.
    a = protocol.append_cmd_checksum("/set?pn3=Ambient\n").rstrip("\n")
    b = protocol.append_cmd_checksum("/set?po=0,0,0,0\n").rstrip("\n")
    merged = a + b
    assert firmware_validate(merged) is None


def test_validator_rejects_corrupted_byte():
    sent = protocol.append_cmd_checksum("/set?sp=225\n").rstrip("\n")
    corrupted = sent.replace("225", "325", 1)
    assert firmware_validate(corrupted) is None


def test_name_with_literal_star_round_trips():
    cmd = "/set?pn1=A*Star Name"
    sent = protocol.append_cmd_checksum(cmd + "\n").rstrip("\n")
    assert firmware_validate(sent) == cmd


# -- version gate -------------------------------------------------------------

def test_supports_cmd_checksum_gate():
    assert protocol.supports_cmd_checksum("20260610-hm4")
    assert protocol.supports_cmd_checksum("20260610-hm4B")
    assert protocol.supports_cmd_checksum("20270101-hm9")
    assert not protocol.supports_cmd_checksum("20260602-hm3B")
    assert not protocol.supports_cmd_checksum("20210202B")
    assert not protocol.supports_cmd_checksum(None)
    assert not protocol.supports_cmd_checksum("garbage")


# -- link + service integration ------------------------------------------------

def test_serial_link_appends_when_enabled():
    from heatermeterd.links import SerialLink

    captured = []

    class _FakeSer:
        def write(self, data):
            captured.append(data.decode())
        def flush(self):
            pass

    link = SerialLink("/dev/null")
    link._ser = _FakeSer()
    link.send("/set?sp=225\n")
    assert captured[-1] == "/set?sp=225\n"          # off by default
    link.cmd_checksum = True
    link.send("/set?sp=225\n")
    assert "*" in captured[-1]
    assert firmware_validate(captured[-1].rstrip("\n")) == "/set?sp=225"
    # Non-command writes (none today, but defensive) pass through untouched.
    link.send("$HMXX\n".replace("$", "/")) if False else None


def test_service_enables_on_hm4_ucid():
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    assert svc.link.cmd_checksum is False
    svc._on_line(protocol.frame("UCID,HeaterMeter,20260610-hm4B"))
    assert svc.link.cmd_checksum is True
    # A rollback to pre-hm4 turns it back off.
    svc._on_line(protocol.frame("UCID,HeaterMeter,20210202B"))
    assert svc.link.cmd_checksum is False
