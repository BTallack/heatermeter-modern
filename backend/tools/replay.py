#!/usr/bin/env python3
"""Replay a captured log through the parser.

Useful once you have a real capture from ``hmmonitor.py --capture``: it streams
the recorded lines through :mod:`heatermeterd.protocol` and reports how cleanly
they parse, which is how we verify the protocol module against real hardware
output without the hardware present.

Example:
    python3 tools/replay.py cook-2026.log
    python3 tools/replay.py cook-2026.log --state   # show final decoded state
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from heatermeterd import protocol         # noqa: E402
from heatermeterd.state import HeaterMeterState  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay a HeaterMeter capture log")
    ap.add_argument("logfile")
    ap.add_argument("--state", action="store_true",
                    help="print the final decoded state as JSON")
    args = ap.parse_args(argv)

    state = HeaterMeterState()
    counts: dict[str, int] = {}
    good = bad = skipped = 0

    with open(args.logfile) as fh:
        for line in fh:
            s = protocol.parse(line)
            if s is None:
                skipped += 1
                continue
            counts[s.type] = counts.get(s.type, 0) + 1
            if s.checksum is not None and not s.checksum_ok:
                bad += 1
            else:
                good += 1
            state.ingest(s)

    print(f"parsed ok: {good}   bad checksum: {bad}   non-sentence lines: {skipped}")
    print("sentence types seen:")
    for typ in sorted(counts):
        print(f"  {typ}: {counts[typ]}")

    if args.state:
        print("\nfinal state:")
        print(json.dumps(state.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
