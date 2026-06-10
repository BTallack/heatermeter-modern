#!/usr/bin/env python3
"""Live serial monitor and capture tool.

Connects to a HeaterMeter device (a real serial port on the Pi, or the
simulator's PTY, or a captured log file) and prints decoded status. Doubles as
the Phase 0 capture tool: with ``--capture FILE`` it appends every raw line to a
log for later replay and protocol verification.

Examples:
    # On the Pi, watching the real board:
    python3 tools/hmmonitor.py /dev/serial0

    # Against the simulator's printed PTY path:
    python3 tools/hmmonitor.py /dev/ttys012

    # Capture a real cook to a log for offline development:
    python3 tools/hmmonitor.py /dev/serial0 --capture cook-2026.log
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from heatermeterd import protocol            # noqa: E402
from heatermeterd.serial_io import device_lines  # noqa: E402


def fmt_status(st: protocol.Status) -> str:
    def t(v):
        return f"{v:6.1f}" if isinstance(v, (int, float)) else "    --"
    return (
        f"set {t(st.set_point)}  pit {t(st.pit)}  "
        f"food1 {t(st.food1)}  food2 {t(st.food2)}  amb {t(st.ambient)}  "
        f"fan {t(st.fan_pct)}%  out {t(st.output_pct)}%"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HeaterMeter serial monitor / capture")
    ap.add_argument("device", help="serial port, PTY, or capture file path")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--capture", metavar="FILE",
                    help="append every raw line to FILE")
    ap.add_argument("--all", action="store_true",
                    help="print every sentence, not just status updates")
    args = ap.parse_args(argv)

    cap = open(args.capture, "a") if args.capture else None
    good = bad = other = 0
    try:
        for line in device_lines(args.device, baud=args.baud):
            if cap:
                cap.write(line + "\n")
                cap.flush()
            s = protocol.parse(line)
            if s is None:
                continue
            if not s.checksum_ok and s.checksum is not None:
                bad += 1
                print(f"[BAD CKSUM] {line}", file=sys.stderr)
                continue
            good += 1
            if s.type == "HMSU":
                print(fmt_status(protocol.Status.from_sentence(s)))
            else:
                other += 1
                if args.all:
                    print(f"  {s.type}: {','.join(s.fields)}")
    except KeyboardInterrupt:
        pass
    finally:
        if cap:
            cap.close()
        print(f"\n[summary] ok={good} bad_checksum={bad} non-status={other}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
