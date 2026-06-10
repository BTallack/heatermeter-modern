#!/usr/bin/env python3
"""Send a single command line to a HeaterMeter device.

Examples:
    python3 tools/hmsend.py /dev/serial0 "/set?sp=250F"
    python3 tools/hmsend.py /dev/ttys012 "/config"

You can also pass a high-level shorthand instead of a raw path:
    python3 tools/hmsend.py /dev/serial0 --setpoint 250
    python3 tools/hmsend.py /dev/serial0 --config
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from heatermeterd import protocol               # noqa: E402
from heatermeterd.serial_io import write_command  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send a command to a HeaterMeter")
    ap.add_argument("device", help="serial port or PTY path")
    ap.add_argument("raw", nargs="?", help="a raw command line, e.g. /set?sp=250F")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--setpoint", type=float, help="shortcut for /set?sp=<n>F")
    ap.add_argument("--unit", default="F")
    ap.add_argument("--config", action="store_true", help="shortcut for /config")
    args = ap.parse_args(argv)

    if args.config:
        line = protocol.request_config()
    elif args.setpoint is not None:
        line = protocol.set_setpoint(args.setpoint, args.unit)
    elif args.raw:
        line = protocol.command(args.raw)
    else:
        ap.error("provide a raw command, --setpoint, or --config")

    write_command(args.device, line, baud=args.baud)
    sys.stderr.write(f"sent: {line!r}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
