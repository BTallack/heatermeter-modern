"""Serial transport helpers.

Two concerns are kept separate from :mod:`heatermeterd.protocol` (which is pure):

* :class:`LineReader` turns arbitrary byte chunks into complete text lines,
  tolerating \\n, \\r\\n and \\r terminators.
* line-source generators read from either a real serial port (via pyserial,
  used on the Pi) or a raw file descriptor (a pseudo-terminal or a captured
  log file, used for hardware-free development on a laptop).

pyserial is imported lazily so that the pure parsing/dev paths work with no
third-party dependencies installed.
"""

from __future__ import annotations

import os
import sys
from typing import Iterator, Optional


class LineReader:
    """Accumulates bytes and yields complete decoded lines."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        out: list[str] = []
        self._buf.extend(data)
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = self._buf[:nl]
            del self._buf[: nl + 1]
            out.append(line.rstrip(b"\r").decode("ascii", "replace"))
        return out


def read_fd_lines(fd: int) -> Iterator[str]:
    """Yield decoded lines from a raw file descriptor until EOF.

    Works for pseudo-terminals (the simulator), pipes and plain files. On a PTY
    the master closing produces either EOF or an EIO ``OSError`` depending on
    the platform; both end the iteration cleanly.
    """
    reader = LineReader()
    while True:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        yield from reader.feed(data)


def _try_open_pyserial(path: str, baud: int):
    """Return a configured pyserial ``Serial`` instance, or ``None`` if
    pyserial is not installed."""
    try:
        import serial  # type: ignore
    except ImportError:
        return None
    return serial.Serial(path, baudrate=baud, timeout=1)


def device_lines(path: str, baud: int = 38400) -> Iterator[str]:
    """Yield decoded lines from a serial device or PTY at *path*.

    Prefers pyserial (correct baud handling on a real UART). If pyserial is not
    installed it falls back to a raw read of the descriptor, which is fine for a
    pseudo-terminal where baud is irrelevant; a warning is printed because a
    real UART needs pyserial for correct framing.
    """
    ser = _try_open_pyserial(path, baud)
    if ser is not None:
        reader = LineReader()
        try:
            while True:
                waiting = getattr(ser, "in_waiting", 0) or 1
                data = ser.read(waiting)
                if data:
                    yield from reader.feed(data)
        finally:
            ser.close()
        return

    print(
        f"[serial_io] pyserial not installed; reading {path} as a raw fd. "
        "Install pyserial for real UART use (pip install pyserial).",
        file=sys.stderr,
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    try:
        yield from read_fd_lines(fd)
    finally:
        os.close(fd)


def write_command(path: str, line: str, baud: int = 38400) -> None:
    """Write a single command *line* to the device at *path*.

    Uses pyserial if available, otherwise a raw descriptor write (PTY/file).
    """
    data = line.encode("ascii", "replace")
    ser = _try_open_pyserial(path, baud)
    if ser is not None:
        try:
            ser.write(data)
            ser.flush()
        finally:
            ser.close()
        return

    fd = os.open(path, os.O_WRONLY | os.O_NOCTTY)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
