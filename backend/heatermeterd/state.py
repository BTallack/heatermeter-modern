"""In-memory model of the controller's current state.

Sentences from the firmware are fed in via :meth:`HeaterMeterState.ingest` and
the latest values for each kind are retained. :meth:`to_dict` produces a plain
dictionary suitable for JSON serialisation over the API.

This is intentionally tolerant: unknown sentence types are stashed by raw type
so nothing is lost while the protocol is still being characterised against a
live capture.
"""

from __future__ import annotations

from typing import Optional

from . import protocol
from .protocol import Sentence, Status


class HeaterMeterState:
    def __init__(self) -> None:
        self.status: Status = Status()
        self.device_name: Optional[str] = None
        self.version: Optional[str] = None
        self.probe_names: list[str] = ["", "", "", ""]
        self.probe_offsets: list[str] = ["", "", "", ""]
        self.pid: dict = {}          # {p, i, d, units}
        self.fan: dict = {}          # fan/servo params
        self.display: dict = {}      # backlight / home mode / leds
        self.lid_detect: dict = {}   # offset percent / duration
        self.alarms: list[str] = []  # raw alarm thresholds
        self.rf: dict = {}           # last RF status / mapping
        self.rf_sources: list = []   # decoded $HMRF transmitters
        self.pid_internals: dict = {}  # $HMPS component breakdown (when enabled)
        self.adc_noise: list = []    # $HMAR per-probe ADC range/noise
        self.log: list[str] = []     # recent $HMLG messages (bounded)
        self.last_update: Optional[float] = None
        self.other: dict = {}        # any unrecognised sentence, by type
        # Per-probe coefficients + type from $HMPC: index -> {a,b,c,r,type}.
        self.probe_coeffs: dict = {}
        self.config_received: bool = False  # set once we've seen an $HMFN

    # -- ingestion ---------------------------------------------------------

    def ingest(self, sentence: Sentence, ts: Optional[float] = None) -> None:
        """Update state from one parsed sentence. *ts* is an optional caller
        supplied timestamp (kept out of here so the model stays pure/testable)."""
        t = sentence.type
        f = sentence.fields

        if t == "HMSU":
            self.status = Status.from_sentence(sentence)
            if ts is not None:
                self.last_update = ts
        elif t == "UCID":
            # $UCID,HeaterMeter,<version+boardrev>
            self.device_name = f[0] if len(f) > 0 else None
            self.version = f[1] if len(f) > 1 else None
        elif t == "HMPN":
            # Sanitise so a corrupt name (e.g. a leaked command merged in over
            # the unchecksummed command channel) self-heals to the default
            # rather than sticking and being re-sent.
            self.probe_names = [protocol.clean_probe_name(x)
                                for x in (f + ["", "", "", ""])[:4]]
        elif t == "HMPO":
            self.probe_offsets = (f + ["", "", "", ""])[:4]
        elif t == "HMPD":
            # $HMPD,0,PidP,PidI,PidD,Units
            self.pid = {
                "p": f[1] if len(f) > 1 else None,
                "i": f[2] if len(f) > 2 else None,
                "d": f[3] if len(f) > 3 else None,
                "units": f[4] if len(f) > 4 else None,
            }
        elif t == "HMFN":
            keys = ["low", "high", "servo_min", "servo_max", "flags",
                    "max_startup", "fan_active_floor", "servo_active_ceil"]
            self.fan = {k: (f[i] if i < len(f) else None) for i, k in enumerate(keys)}
            self.config_received = True
        elif t == "HMPC":
            # $HMPC,idx,A,B,C,R,type
            try:
                idx = int(f[0])
            except (ValueError, IndexError):
                idx = None
            if idx is not None:
                self.probe_coeffs[idx] = {
                    "a": f[1] if len(f) > 1 else None,
                    "b": f[2] if len(f) > 2 else None,
                    "c": f[3] if len(f) > 3 else None,
                    "r": f[4] if len(f) > 4 else None,
                    "type": f[5] if len(f) > 5 else None,
                }
        elif t == "HMLB":
            # $HMLB,Backlight,HomeMode,LED0,LED1,LED2,LED3
            self.display = {
                "raw": list(f),
                "backlight": f[0] if len(f) > 0 else None,
                "home_mode": f[1] if len(f) > 1 else None,
                "leds": list(f[2:6]),
            }
        elif t == "HMLD":
            self.lid_detect = {
                "offset_percent": f[0] if len(f) > 0 else None,
                "duration": f[1] if len(f) > 1 else None,
            }
        elif t == "HMAL":
            self.alarms = list(f)
        elif t == "HMRF":
            # $HMRF,255,0,CrcOk[,NodeId,Flags,Rssi ...] - first triplet is the
            # receiver, then one triplet per active transmitter.
            self.rf["HMRF"] = list(f)
            srcs = []
            i = 3
            while i + 2 < len(f) + 1 and i + 2 <= len(f):
                try:
                    node = int(f[i]); flags = int(f[i + 1]); rssi = int(f[i + 2])
                except (ValueError, IndexError):
                    break
                srcs.append({
                    "node": node,
                    "low_battery": bool(flags & 1),
                    "recent_reset": bool(flags & 2),
                    "native": bool(flags & 4),
                    "rssi": rssi,
                })
                i += 3
            self.rf_sources = srcs
        elif t == "HMRM":
            self.rf["HMRM"] = list(f)
        elif t == "HMPS":
            # $HMPS,cPidB,cPidP,cPidI,cPidD,tempD (sum of cPid* = output %)
            keys = ["b", "p", "i", "d", "temp_d"]
            self.pid_internals = {k: (f[j] if j < len(f) else None)
                                  for j, k in enumerate(keys)}
        elif t == "HMAR":
            self.adc_noise = list(f)
        elif t == "HMLG":
            self.log.append(",".join(f))
            del self.log[:-50]  # keep last 50
        else:
            self.other[t] = list(f)

    def ingest_line(self, line: str, ts: Optional[float] = None) -> Optional[Sentence]:
        """Parse a raw line and ingest it. Returns the parsed sentence (or
        ``None`` if the line was not a sentence)."""
        s = protocol.parse(line)
        if s is not None:
            self.ingest(s, ts=ts)
        return s

    # -- output ------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "device_name": self.device_name,
            "version": self.version,
            "status": self.status.to_dict(),
            "probe_names": self.probe_names,
            "probe_offsets": self.probe_offsets,
            "pid": self.pid,
            "fan": self.fan,
            "display": self.display,
            "lid_detect": self.lid_detect,
            "alarms": self.alarms,
            "probe_coeffs": self.probe_coeffs,
            "rf": self.rf,
            "rf_sources": self.rf_sources,
            "pid_internals": self.pid_internals,
            "adc_noise": self.adc_noise,
            "log": self.log[-10:],
            "last_update": self.last_update,
        }
