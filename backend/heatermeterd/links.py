"""Transport links between the service and the board.

A "link" produces lines (scheduled onto the asyncio loop via
``loop.call_soon_threadsafe``) and accepts command lines to send. Two
implementations share one interface:

* :class:`SerialLink` - a real (or PTY) serial device, read in a background
  thread. Uses pyserial when available, otherwise a raw file descriptor.
* :class:`SimLink` - an in-process simulated board, so the whole daemon can run
  with ``--sim`` and no hardware.
"""

from __future__ import annotations

import os
import select
import threading
from typing import Callable

from .serial_io import LineReader, _try_open_pyserial
from .sim import SimBoard

OnLine = Callable[[str], None]


class SerialLink:
    def __init__(self, path: str, baud: int = 38400) -> None:
        self.path = path
        self.baud = baud
        self._ser = None
        self._fd = None
        self._thread = None
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        self._on_line: OnLine | None = None
        self._loop = None
        # When True (set by the service once the board reports an hm4+ firmware),
        # every outbound command line gets the *XX XOR checksum the firmware
        # validates, so garbled/merged commands are dropped instead of executed.
        self.cmd_checksum = False

    def _open(self) -> None:
        self._ser = _try_open_pyserial(self.path, self.baud)
        if self._ser is None:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)

    def _start_reader(self, on_line: OnLine, loop) -> None:
        self._on_line = on_line
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run, args=(on_line, loop), daemon=True
        )
        self._thread.start()

    def start(self, on_line: OnLine, loop) -> None:
        self._stop.clear()
        self._open()
        self._start_reader(on_line, loop)

    def _run(self, on_line: OnLine, loop) -> None:
        reader = LineReader()
        while not self._stop.is_set():
            try:
                if self._ser is not None:
                    # pyserial is opened with timeout=1, so read() returns at
                    # least once a second and the _stop check is reached.
                    waiting = getattr(self._ser, "in_waiting", 0) or 1
                    data = self._ser.read(waiting)
                else:
                    # Raw fd: poll with a short timeout so _stop is honored
                    # promptly even when the device is silent, and even on
                    # platforms where closing the fd does not interrupt a
                    # blocked read (macOS). This is what makes pause()/close()
                    # reliable without relying on close-interrupts-read.
                    ready, _, _ = select.select([self._fd], [], [], 0.2)
                    if not ready:
                        continue
                    data = os.read(self._fd, 4096)
            except (OSError, ValueError):
                break
            if not data:
                if self._ser is None:
                    break  # raw fd EOF
                continue
            for line in reader.feed(data):
                loop.call_soon_threadsafe(on_line, line)

    def send(self, line: str) -> None:
        if self.cmd_checksum and line.startswith("/"):
            from . import protocol
            line = protocol.append_cmd_checksum(line)
        data = line.encode("ascii", "replace")
        with self._wlock:
            if self._ser is not None:
                self._ser.write(data)
                self._ser.flush()
            elif self._fd is not None:
                os.write(self._fd, data)

    def _shutdown_io(self) -> None:
        """Stop the reader thread and close the OS handle, leaving the config
        (path, baud, stored on_line/loop) intact so :meth:`resume` can reopen.

        Closes the handle first (which unblocks a reader parked in ``read`` with
        an OSError it already handles), joins, then nulls the references, so the
        reader never dereferences a half-cleared fd."""
        self._stop.set()
        ser, fd, thread = self._ser, self._fd, self._thread
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2)
        self._ser = None
        self._fd = None
        self._thread = None

    def pause(self) -> None:
        """Stop reading and release the serial handle, remembering how to
        resume. Used around an external flash that resets the board, so the
        daemon does not ingest the reset garbage or a half-line spanning the
        reboot. The UART does not conflict with SPI, so this is about clean
        ingestion, not freeing a contended pin."""
        self._shutdown_io()

    def resume(self, on_line: OnLine | None = None, loop=None) -> None:
        """Reopen the serial handle and restart the reader. Reopening discards
        any buffered garbage accumulated while paused."""
        on_line = on_line or self._on_line
        loop = loop or self._loop
        if on_line is None or loop is None:
            raise RuntimeError("resume() called before start()")
        self._stop = threading.Event()
        self._open()
        self._start_reader(on_line, loop)

    def close(self) -> None:
        self._shutdown_io()


class SimLink:
    def __init__(self, setpoint: float = 225.0, interval: float = 1.0,
                 seed: int | None = None) -> None:
        self.board = SimBoard(setpoint=setpoint, seed=seed)
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._loop = None
        self._on_line: OnLine | None = None
        # Interface parity with SerialLink (the sim board reports a pre-hm4
        # version, so the service never actually enables this).
        self.cmd_checksum = False

    def start(self, on_line: OnLine, loop) -> None:
        self._loop = loop
        self._on_line = on_line
        self._thread = threading.Thread(target=self._run, args=(on_line, loop),
                                        daemon=True)
        self._thread.start()

    def _run(self, on_line: OnLine, loop) -> None:
        with self._lock:
            config = self.board.config_lines()
        for line in config:
            loop.call_soon_threadsafe(on_line, line)
        while not self._stop.wait(self.interval):
            with self._lock:
                self.board.step(self.interval)
                line = self.board.status_line()
            loop.call_soon_threadsafe(on_line, line)

    def send(self, line: str) -> None:
        with self._lock:
            responses = self.board.handle_command(line)
        if self._loop is not None and self._on_line is not None:
            for out in responses:
                self._loop.call_soon_threadsafe(self._on_line, out)

    def pause(self) -> None:
        """Stop the simulated feed (mirror of SerialLink so the service can call
        pause/resume uniformly regardless of link type)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def resume(self, on_line: OnLine | None = None, loop=None) -> None:
        on_line = on_line or self._on_line
        loop = loop or self._loop
        if on_line is None or loop is None:
            raise RuntimeError("resume() called before start()")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(on_line, loop),
                                        daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
