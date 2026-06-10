"""Simulated HeaterMeter board model.

A tiny thermal model that produces realistic ``$HM*`` sentences so the rest of
the stack can be developed and tested without hardware. Shared by the CLI
simulator (``tools/hmsim.py``) and the in-process ``SimLink`` daemon transport.
"""

from __future__ import annotations

import random

from . import protocol


class SimBoard:
    """Pit chases the setpoint; food rises asymptotically toward it."""

    def __init__(self, setpoint: float = 225.0, ambient: float = 75.0,
                 seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.setpoint = setpoint
        self.ambient = ambient
        self.pit = ambient
        self.food1 = ambient - 35.0   # meat starts cold
        self.fan = 100.0
        self.servo = 0.0
        self.output = 100.0
        self.output_avg = 100.0
        self.manual = False
        self.t = 0.0
        # Configurable state the board would hold in EEPROM.
        self.probe_names = ["Pit", "Food1", "Food2", "Ambient"]
        self.offsets = [0, 0, 0, 0]
        self.pid = {"b": 0.0, "p": 4.0, "i": 0.020, "d": 5.0}
        self.units = "F"
        # 4 probes x (low, high); -1 disabled.
        self.alarms = [-40, -200, -40, -200, -40, -200, -40, -200]
        # Fan/servo params: low, high, servo_min, servo_max, flags, max_startup,
        # fan_active_floor, servo_active_ceil.
        self.fan_params = {
            "low": 0, "high": 100, "servo_min": 100, "servo_max": 200,
            "flags": 0, "max_startup": 100, "fan_active_floor": 0,
            "servo_active_ceil": 100,
        }
        self.lid = {"offset": 6, "duration": 240}
        # Per-probe coefficients (a, b, c, r) and type; only tracked so the
        # /set?pcN= round-trip is observable in tests.
        self.probe_coeffs = [None, None, None, None]
        self.probe_types = [1, 1, 1, 1]

    def step(self, dt: float = 1.0) -> None:
        self.t += dt
        err = self.setpoint - self.pit

        if not self.manual:
            # Lazy proportional controller, clamped 0..100.
            self.output = max(0.0, min(100.0, err * 2.5))
        self.output_avg += (self.output - self.output_avg) * 0.1

        # Pit heats with airflow, loses heat to ambient.
        heat = self.output * 0.05
        loss = (self.pit - self.ambient) * 0.004
        self.pit += (heat - loss) * dt + self.rng.uniform(-0.3, 0.3)

        # Food climbs toward (pit - stall offset), slowly.
        target_food = self.pit - 40.0
        self.food1 += (target_food - self.food1) * 0.01 * dt
        self.ambient += self.rng.uniform(-0.2, 0.2)

        self.fan = self.output
        self.servo = 0.0

    # -- sentence generation ----------------------------------------------

    def status_line(self) -> str:
        return protocol.frame(self.status_payload())

    def status_payload(self) -> str:
        def n(x: float, d: int = 1) -> str:
            return f"{x:.{d}f}"
        sp = "-0" if self.manual else f"{self.setpoint:.0f}"
        return "HMSU," + ",".join([
            sp,
            n(self.pit),
            n(self.food1),
            "U",                      # food2 unplugged (real firmware uses "U")
            n(self.ambient),
            f"{self.output:.0f}",
            f"{self.output_avg:.0f}",
            "0",                      # lid open countdown
            f"{self.fan:.0f}",
            f"{self.servo:.0f}",
        ])

    def config_lines(self) -> list[str]:
        pn = "HMPN," + ",".join(self.probe_names)
        pd = "HMPD,0,%s,%s,%s,%s" % (self.pid["p"], self.pid["i"], self.pid["d"],
                                     self.units)
        po = "HMPO," + ",".join(str(o) for o in self.offsets)
        al = "HMAL," + ",".join(str(a) for a in self.alarms)
        fp = self.fan_params
        fn = "HMFN,%s,%s,%s,%s,%s,%s,%s,%s" % (
            fp["low"], fp["high"], fp["servo_min"], fp["servo_max"],
            fp["flags"], fp["max_startup"], fp["fan_active_floor"],
            fp["servo_active_ceil"])
        ld = "HMLD,%s,%s" % (self.lid["offset"], self.lid["duration"])
        return [
            protocol.frame("UCID,HeaterMeter,20210202B"),
            protocol.frame(pn),
            protocol.frame(pd),
            protocol.frame(po),
            protocol.frame(fn),
            protocol.frame(ld),
            protocol.frame(al),
        ]

    def handle_command(self, line: str) -> list[str]:
        """React to an inbound command. Returns any lines to send back."""
        line = line.strip()
        if line.startswith("/config"):
            return self.config_lines()
        if line.startswith("/set?sp="):
            val = line.split("=", 1)[1]
            unit = val[-1] if val and val[-1].isalpha() else ""
            num = val[:-1] if unit else val
            # A leading '-' means manual output mode (including "-0" = 0%), which
            # float() alone can't distinguish since float("-0") == 0.0. Match the
            # firmware: any negative-signed setpoint switches to manual.
            is_manual = num.strip().startswith("-")
            try:
                n = float(num)
            except ValueError:
                return []
            if is_manual:
                self.manual = True
                self.output = -n
            else:
                self.manual = False
                self.setpoint = n
        elif line.startswith("/set?pn"):
            # /set?pnN=Name
            try:
                idx = int(line[len("/set?pn")])
                name = line.split("=", 1)[1]
                self.probe_names[idx] = name
            except (ValueError, IndexError):
                pass
        elif line.startswith("/set?po="):
            vals = line.split("=", 1)[1].split(",")
            for i, v in enumerate(vals[:4]):
                if v.strip() != "":
                    try:
                        self.offsets[i] = int(float(v))
                    except ValueError:
                        pass
        elif line.startswith("/set?pid"):
            # /set?pidX=value
            try:
                param = line[len("/set?pid")]
                value = float(line.split("=", 1)[1])
                if param in self.pid:
                    self.pid[param] = value
            except (ValueError, IndexError):
                pass
        elif line.startswith("/set?al="):
            vals = line.split("=", 1)[1].split(",")
            for i, v in enumerate(vals[:len(self.alarms)]):
                if v.strip() != "":
                    try:
                        self.alarms[i] = int(float(v))
                    except ValueError:
                        pass
        elif line.startswith("/set?fn="):
            vals = line.split("=", 1)[1].split(",")
            keys = ["low", "high", "servo_min", "servo_max", "flags",
                    "max_startup", "fan_active_floor", "servo_active_ceil"]
            for i, v in enumerate(vals[:len(keys)]):
                if v.strip() != "":
                    try:
                        self.fan_params[keys[i]] = int(float(v))
                    except ValueError:
                        pass
        elif line.startswith("/set?ld="):
            vals = line.split("=", 1)[1].split(",")
            try:
                if len(vals) > 0 and vals[0].strip() != "":
                    self.lid["offset"] = int(float(vals[0]))
                if len(vals) > 1 and vals[1].strip() != "":
                    self.lid["duration"] = int(float(vals[1]))
            except ValueError:
                pass
        elif line.startswith("/set?pc"):
            # /set?pcN=A,B,C,R,TRM
            try:
                idx = int(line[len("/set?pc")])
                vals = line.split("=", 1)[1].split(",")
                if len(vals) >= 4 and all(v.strip() for v in vals[:4]):
                    self.probe_coeffs[idx] = [float(x) for x in vals[:4]]
                if len(vals) >= 5 and vals[4].strip() != "":
                    self.probe_types[idx] = int(float(vals[4]))
            except (ValueError, IndexError):
                pass
        return []
