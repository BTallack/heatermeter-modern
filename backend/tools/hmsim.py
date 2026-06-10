#!/usr/bin/env python3
"""Fake HeaterMeter board (CLI).

Emits a realistic ``$HMSU`` stream so the stack can be exercised without real
hardware. Two modes:

  * default: stream framed sentences to stdout (good for eyeballing / piping)
  * --pty:   create a pseudo-terminal, print its slave device path, and behave
             like a serial board on that device (also accepts /set + /config).

The thermal model lives in :mod:`heatermeterd.sim`; this is just the CLI.

Examples:
    python3 tools/hmsim.py --setpoint 250
    python3 tools/hmsim.py --pty
    python3 tools/hmsim.py --duration 5
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from heatermeterd.sim import SimBoard           # noqa: E402
from heatermeterd.serial_io import LineReader   # noqa: E402


def run_stdout(board: SimBoard, interval: float, duration: float | None) -> None:
    for line in board.config_lines():
        sys.stdout.write(line)
    sys.stdout.flush()
    start = time.monotonic()
    while True:
        board.step(interval)
        sys.stdout.write(board.status_line())
        sys.stdout.flush()
        if duration is not None and (time.monotonic() - start) >= duration:
            return
        time.sleep(interval)


def run_pty(board: SimBoard, interval: float, duration: float | None) -> None:
    import tty
    master, slave = os.openpty()
    # Raw mode so the PTY behaves like a real UART (no line-buffering, no
    # \n -> \r\n translation) for whatever client connects to the slave.
    for fd in (master, slave):
        tty.setraw(fd)
    print(f"SIM ready. Connect to: {os.ttyname(slave)}", file=sys.stderr, flush=True)
    reader = LineReader()

    def send(line: str) -> None:
        os.write(master, line.encode("ascii", "replace"))

    for line in board.config_lines():
        send(line)

    start = time.monotonic()
    next_tick = time.monotonic()
    try:
        while True:
            timeout = max(0.0, next_tick - time.monotonic())
            r, _, _ = select.select([master], [], [], timeout)
            if r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                for cmd in reader.feed(data):
                    for out in board.handle_command(cmd):
                        send(out)
            if time.monotonic() >= next_tick:
                board.step(interval)
                send(board.status_line())
                next_tick += interval
            if duration is not None and (time.monotonic() - start) >= duration:
                return
    finally:
        os.close(master)
        os.close(slave)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fake HeaterMeter board")
    ap.add_argument("--pty", action="store_true",
                    help="expose a pseudo-terminal serial device")
    ap.add_argument("--setpoint", type=float, default=225.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    board = SimBoard(setpoint=args.setpoint, seed=args.seed)
    try:
        if args.pty:
            run_pty(board, args.interval, args.duration)
        else:
            run_stdout(board, args.interval, args.duration)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
