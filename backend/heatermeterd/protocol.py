"""HeaterMeter serial protocol.

Pure functions for parsing the ``$HM*`` sentences the ATmega328 firmware emits
and for building the ``/set?...`` command lines the host sends back. No I/O, no
third-party dependencies. This module is the contract between the firmware and
any host software, so it is deliberately small, tolerant, and well tested.

Reference: ``arduino/heatermeter/README.txt`` in the upstream HeaterMeter repo,
and ``PROTOCOL.md`` alongside this project.

Framing (NMEA-0183 style):
    $<TYPE>,<field>,<field>,...*<CK>\n
where TYPE is a 4-character id (2-char talker + 2-char message, e.g. "HMSU"),
fields are comma separated, and CK is a two-hex-digit XOR of every character
between the ``$`` and the ``*``. Lines end with a bare newline (ASCII 10).

Inbound commands to the firmware are plain URL-style lines and are NOT
checksummed, e.g. ``/set?sp=225F``.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, asdict
from typing import Optional

LINE_TERMINATOR = "\n"


# ---------------------------------------------------------------------------
# Checksums and framing
# ---------------------------------------------------------------------------

def compute_checksum(payload: str) -> int:
    """Return the XOR of every byte in *payload* (the chars between $ and *)."""
    cs = 0
    for ch in payload.encode("ascii", "replace"):
        cs ^= ch
    return cs


def checksum_hex(payload: str) -> str:
    """Return the two-uppercase-hex-digit checksum string for *payload*."""
    return f"{compute_checksum(payload):02X}"


def frame(payload: str) -> str:
    """Wrap a raw payload (e.g. ``"HMSU,225,..."``) into a full framed line."""
    return f"${payload}*{checksum_hex(payload)}{LINE_TERMINATOR}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class Sentence:
    """A parsed ``$HM*`` line.

    ``type`` is the 4-char id (e.g. "HMSU"). ``fields`` are the comma-separated
    values after the type. ``checksum_ok`` is True only when a checksum was
    present and matched.
    """

    type: str
    fields: list[str]
    raw: str
    checksum_ok: bool
    checksum: Optional[str] = None


def parse(line: str) -> Optional[Sentence]:
    """Parse one line into a :class:`Sentence`, or ``None`` if it is not a
    ``$``-framed sentence (blank lines, command echoes, log noise, etc.)."""
    line = line.strip("\r\n")
    if not line or not line.startswith("$"):
        return None

    body = line[1:]
    star = body.rfind("*")
    if star >= 0:
        payload = body[:star]
        provided = body[star + 1:].strip()
        ok = bool(provided) and provided.upper() == checksum_hex(payload)
    else:
        payload = body
        provided = None
        ok = False

    parts = payload.split(",")
    return Sentence(
        type=parts[0],
        fields=parts[1:],
        raw=line,
        checksum_ok=ok,
        checksum=provided,
    )


def _num(s: str, cast=float):
    """Tolerant numeric conversion. Non-numeric / empty fields -> None.

    Verified against live hardware (2026-05-31): the firmware emits ``U`` for an
    unplugged/disabled probe field, e.g.
    ``$HMSU,375,74.5,U,U,U,100,99,0,30,0*4D`` - not a blank between commas as the
    upstream README example implied. Any non-numeric token (``U``, blank, etc.)
    decodes to ``None`` via the ``except`` below, so the parser is robust to
    whatever sentinel the firmware uses."""
    s = s.strip()
    if s == "":
        return None
    try:
        return cast(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Typed view of the $HMSU state-update sentence (the 1 Hz heartbeat)
# ---------------------------------------------------------------------------

@dataclass
class Status:
    """The decoded ``$HMSU`` state update.

    Field order (from README.txt):
        SetPoint, Pit, Food1, Food2, Ambient, OutputPct, OutputMovAvg,
        LidOpenCountdown, FanPct, ServoPct[, PidMode]
    PidMode (field 11) is appended only by the Tallack fork firmware
    (20260601-hm1+); it is absent on stock firmware and decodes to ``None``.
    A blank probe field (disabled probe) decodes to ``None``.
    """

    set_point: Optional[float] = None
    pit: Optional[float] = None
    food1: Optional[float] = None
    food2: Optional[float] = None
    ambient: Optional[float] = None
    output_pct: Optional[float] = None
    output_avg: Optional[float] = None
    lid_countdown: Optional[int] = None
    fan_pct: Optional[float] = None
    servo_pct: Optional[float] = None
    pid_mode: Optional[int] = None

    @classmethod
    def from_sentence(cls, s: Sentence) -> "Status":
        f = s.fields

        def g(i: int) -> str:
            return f[i] if i < len(f) else ""

        return cls(
            set_point=_num(g(0)),
            pit=_num(g(1)),
            food1=_num(g(2)),
            food2=_num(g(3)),
            ambient=_num(g(4)),
            output_pct=_num(g(5)),
            output_avg=_num(g(6)),
            lid_countdown=_num(g(7), int),
            fan_pct=_num(g(8)),
            servo_pct=_num(g(9)),
            pid_mode=_num(g(10), int),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pid_mode_label"] = PID_MODES.get(self.pid_mode)
        return d


# PID mode codes reported in $HMSU field 11 (Tallack fork firmware). See
# grillpid.h PIDMODE_* in the firmware source.
PID_MODES = {
    0: "Starting up",
    1: "Recovering",
    2: "At temp",
    3: "Manual",
    4: "Off",
}


# Convenience names for probe indexes used across commands.
PROBE_PIT = 0
PROBE_FOOD1 = 1
PROBE_FOOD2 = 2
PROBE_AMBIENT = 3

# Probe types accepted by /set?pcN= (from firmware grillpid.h).
PROBETYPE_DISABLED = 0   # do not read
PROBETYPE_INTERNAL = 1   # wired thermistor via analogRead()
PROBETYPE_RFM12B = 2     # RFM12B wireless
PROBETYPE_TC_ANALOG = 3  # analog thermocouple (e.g. AD8495); Stein[3] is mV/C

PROBETYPE_LABELS = {
    0: "Disabled",
    1: "Thermistor",
    2: "RF Wireless",
    3: "Thermocouple",
}

# Probe presets transcribed from the firmware source (arduino/heatermeter/
# hmcore.cpp). Thermistor presets are (A, B, C, R_divider) written with type 1
# (INTERNAL); the firmware applies the Steinhart-Hart equation. The thermocouple
# preset is type 3 (TC_ANALOG): the firmware ignores A/B/C and reads the 4th
# value as the amplifier scale in mV/C (AD8495 = 5 mV/C), applying its built-in
# nonlinearity table. Each preset may declare a "type"; it defaults to INTERNAL.
PROBE_PRESETS = {
    "ad8495_ktype": {
        "label": "K-Type Thermocouple (AD8495)",
        "coeffs": [7.3431401e-4, 2.1574370e-4, 9.5156860e-8, 5.0],
        "type": PROBETYPE_TC_ANALOG,
    },
    "thermoworks_pro": {
        "label": "ThermoWorks Pro-Series",
        "coeffs": [7.3431401e-4, 2.1574370e-4, 9.5156860e-8, 1.0e4],
    },
    "maverick_et72": {
        "label": "Maverick ET-72/73",
        "coeffs": [2.4723753e-4, 2.3402251e-4, 1.3879768e-7, 1.0e4],
    },
    "maverick_et732": {
        "label": "Maverick ET-732",
        "coeffs": [5.2668241e-4, 2.0037400e-4, 2.5703090e-8, 1.0e4],
    },
    "radioshack_10k": {
        "label": "Radio Shack 10k",
        "coeffs": [8.98053228e-4, 2.49263324e-4, 2.04047542e-7, 1.0e4],
    },
    "vishay_10k": {
        "label": "Vishay 10k NTCLE203E3103FB0",
        "coeffs": [1.14061e-3, 2.32134e-4, 9.63666e-8, 1.0e4],
    },
    "epcos_100k": {
        "label": "EPCOS 100k",
        "coeffs": [7.2237825e-4, 2.1630182e-4, 9.2641029e-8, 1.0e4],
    },
    "semitec_104gt2": {
        "label": "Semitec 104GT-2",
        "coeffs": [8.1129016e-4, 2.1135575e-4, 7.1761474e-8, 1.0e4],
    },
}


# ---------------------------------------------------------------------------
# Command builders (host -> firmware). Builders emit bare command lines; the
# link layer appends the hm4+ command checksum when the firmware supports it
# (see append_cmd_checksum / supports_cmd_checksum).
# ---------------------------------------------------------------------------

def command(path: str) -> str:
    """Normalise an arbitrary command path into a full, terminated line."""
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith(LINE_TERMINATOR):
        path = path + LINE_TERMINATOR
    return path


def append_cmd_checksum(line: str) -> str:
    """Append the hm4+ command checksum: ``*XX`` where XX is the XOR of every
    byte of the line (terminator excluded), as two uppercase hex digits.

    The firmware (20260610-hm4 and later) validates and strips it, and DROPS a
    checksummed line that does not verify - so a command garbled in transit
    (e.g. two commands merged by a lost newline) is rejected instead of
    executed. Lines that already carry a checksum pass through unchanged."""
    body = line.rstrip("\r\n")
    term = line[len(body):] or LINE_TERMINATOR
    if len(body) >= 3 and body[-3] == "*":
        try:
            int(body[-2:], 16)
            return line   # already checksummed
        except ValueError:
            pass
    csum = 0
    for ch in body:
        csum ^= ord(ch) & 0xFF
    return f"{body}*{csum:02X}{term}"


def supports_cmd_checksum(version) -> bool:
    """True if the board firmware validates command checksums (>= hm4).

    The version string leads with a build date (e.g. ``20260610-hm4B``); any
    build dated 2026-06-10 or later understands the ``*XX`` suffix."""
    m = re.match(r"^(\d{8})", str(version or ""))
    return bool(m) and int(m.group(1)) >= 20260610


def _fmt(value) -> str:
    """Format a value for a command field. ``None`` -> '' (keep current)."""
    if value is None:
        return ""
    return str(value)


def set_setpoint(value, unit: str = "F") -> str:
    """Set the automatic-control setpoint. *unit* is F, C, R (resistance) or
    A (raw ADC). A negative setpoint puts the unit into manual mode."""
    return command(f"/set?sp={value}{unit}")


def set_manual_output(percent) -> str:
    """Switch to manual fan mode at *percent* output (``-0`` means 0%)."""
    return command(f"/set?sp=-{percent}")


def set_units(unit: str) -> str:
    """Set the temperature unit ('F'/'C') WITHOUT changing the setpoint. The
    firmware reads the trailing letter of a /set?sp command as the unit, and a
    bare letter leaves the setpoint untouched. NOTE: the firmware does not
    convert stored values, so the caller must re-send a converted setpoint /
    alarms when switching (see service.set_units)."""
    u = (unit or "F").upper()
    return command(f"/set?sp={u}")


def set_pid(param: str, value) -> str:
    """Tune a PID constant. *param* is one of b, p, i, d."""
    param = param.lower()
    if param not in ("b", "p", "i", "d"):
        raise ValueError(f"PID param must be b/p/i/d, got {param!r}")
    return command(f"/set?pid{param}={value}")


def clean_probe_name(name) -> str:
    """Sanitise a probe name for the serial/CSV protocol.

    The host->board command channel is not checksummed, so a dropped byte can
    merge two commands and leave a probe name like ``set?po=0.0`` (a leaked
    ``/set?po=...`` offsets command). And ``$HMPN`` is comma-delimited, so a name
    containing a comma corrupts parsing. This both rejects names that are clearly
    leaked command fragments (returning '') and strips protocol-breaking
    characters, so a corrupt name never round-trips and the host never emits one.
    """
    if not name:
        return ""
    s = str(name)
    low = s.lower().lstrip()
    if low.startswith("/") or "set?" in low:
        return ""   # a leaked command, not a real name
    for ch in ("\n", "\r", ",", "/", "?", "="):
        s = s.replace(ch, "")
    return s[:22]


def set_probe_name(index: int, name: str) -> str:
    """Set probe *index* (0=pit 1=food1 2=food2 3=ambient) name."""
    return command(f"/set?pn{index}={clean_probe_name(name)}")


def set_home_rotate(seconds: int) -> str:
    """Set the LCD home-screen probe rotation interval (seconds). Firmware
    20260602-hm3+; ignored (harmless) by older firmware."""
    return command(f"/set?hr={int(seconds)}")


def set_probe_offsets(offsets) -> str:
    """Set probe calibration offsets. Pass a 4-element iterable; ``None``
    entries are left unchanged, e.g. ``[None, None, None, -2]``."""
    return command("/set?po=" + ",".join(_fmt(o) for o in offsets))


def set_probe_coeffs(index: int, a=None, b=None, c=None, r=None, trm=None) -> str:
    """Set Steinhart-Hart coefficients (a,b,c), divider resistance (r) and
    type/RF-map (trm) for probe *index*. Blank entries are left unchanged."""
    parts = ",".join(_fmt(v) for v in (a, b, c, r, trm))
    return command(f"/set?pc{index}={parts}")


def set_probe_preset(index: int, preset_key: str) -> str:
    """Apply a named preset (see PROBE_PRESETS) to probe *index*.

    Writes the preset's four coefficients and sets the probe type. Thermistor
    presets use type INTERNAL; the AD8495 preset uses type TC_ANALOG, where the
    firmware reads the 4th value as mV/C."""
    preset = PROBE_PRESETS[preset_key]
    a, b, c, r = preset["coeffs"]
    ptype = preset.get("type", PROBETYPE_INTERNAL)
    return set_probe_coeffs(index, a, b, c, r, ptype)


def set_probe_disabled(index: int) -> str:
    """Disable probe *index* (type 0). Leaves coefficients untouched."""
    return command(f"/set?pc{index}=,,,,{PROBETYPE_DISABLED}")


def set_alarms(thresholds) -> str:
    """Set probe alarm thresholds as a flat list ``[low0, high0, low1, ...]``.
    Negative disables; 0 silences and disarms a ringing alarm. ``None`` keeps
    the current value."""
    return command("/set?al=" + ",".join(_fmt(t) for t in thresholds))


def set_fan(fan_low=None, fan_high=None, servo_min=None, servo_max=None,
            flags=None, max_startup=None, fan_active_floor=None,
            servo_active_ceil=None) -> str:
    """Set fan/servo output parameters (``/set?fn=...``)."""
    parts = (fan_low, fan_high, servo_min, servo_max, flags, max_startup,
             fan_active_floor, servo_active_ceil)
    return command("/set?fn=" + ",".join(_fmt(p) for p in parts))


def set_lid_detect(offset_percent=None, duration_seconds=None, active=None) -> str:
    """Set lid-open detection parameters (``/set?ld=...``).

    *active* is a runtime trigger: non-zero enters lid-open mode NOW, 0 cancels
    it. This is the hook for a manual "lid open" button or a host-side lid
    algorithm (the firmware's auto-detect uses *offset_percent*)."""
    parts = (offset_percent, duration_seconds, active)
    return command("/set?ld=" + ",".join(_fmt(p) for p in parts))


def lid_open_now() -> str:
    """Manually enter lid-open mode (suspends the fan)."""
    return command("/set?ld=,,1")


def lid_open_cancel() -> str:
    """Cancel lid-open mode."""
    return command("/set?ld=,,0")


# LED stimulus options (firmware ledmanager.h). The high bit (0x80) inverts.
LED_STIMULI = {
    0: "Off",
    1: "Alarm Pit Low", 2: "Alarm Pit High",
    3: "Alarm Food1 Low", 4: "Alarm Food1 High",
    5: "Alarm Food2 Low", 6: "Alarm Food2 High",
    7: "Alarm Amb Low", 8: "Alarm Amb High",
    9: "RF Receive", 10: "Lid Open", 11: "Fan On",
    12: "Pit Temp Reached", 13: "Fan Max", 14: "Any Alarm",
    15: "Startup", 16: "Recovery",
}
LED_INVERT = 0x80

# Home display modes for the LCD (firmware HomeDisplayMode). The HeaterMeter has
# a 2-line display only; the firmware's 254 "4-line" mode is intentionally
# omitted because it doesn't render correctly on this hardware.
HOME_MODES = {
    255: "Pit + rotating probe",
    0: "Big number: Pit",
    1: "Big number: Food 1",
    2: "Big number: Food 2",
    3: "Big number: Ambient",
}


def set_lcd(backlight=None, home_mode=None, leds=None) -> str:
    """Set LCD/LED config (``/set?lb=...``).

    *backlight* 0-255, *home_mode* (see HOME_MODES), *leds* a 4-element iterable
    of LED stimulus bytes (stimulus value, optionally OR'd with LED_INVERT).
    ``None`` entries are left unchanged."""
    parts = [backlight, home_mode]
    if leds:
        parts += list(leds)
    return command("/set?lb=" + ",".join(_fmt(p) for p in parts))


def set_pid_internals(enabled: bool) -> str:
    """Toggle the board emitting ``$HMPS`` PID-internals every status period
    (``/set?tp=A``)."""
    return command(f"/set?tp={1 if enabled else 0}")


def set_noise_pin(pin) -> str:
    """Request a raw ADC noise dump (``$HMND``) for an analog *pin*
    (``/set?tp=,pin``)."""
    return command(f"/set?tp=,{pin}")


def toast(line1: str, line2: str = None) -> str:
    """Show a temporary "toast" message on the LCD (``/set?tt=...``)."""
    if line2 is None:
        return command(f"/set?tt={line1}")
    return command(f"/set?tt={line1},{line2}")


def host_interactive_reply(opaque, line1: str, line2: str) -> str:
    """Reply to a board ``$HMHI`` request (``/set?hi=<opaque>,<line1>,<line2>``).

    Drives the LCD's host-interactive screens (e.g. Net Info). Must be sent
    within ~800ms of the request or the board shows "Offline"."""
    return command(f"/set?hi={opaque},{line1},{line2}")


def request_version() -> str:
    """Ask the firmware to emit its ``$UCID`` version line. Lightweight; also
    promotes the board's host-state to ONLINE (any command line does)."""
    return command("/ucid")


def request_config() -> str:
    """Ask the firmware to dump its configuration (serial-only)."""
    return command("/config")


def reboot() -> str:
    """Reboot the microcontroller (only works if wired to do so)."""
    return command("/reboot")
