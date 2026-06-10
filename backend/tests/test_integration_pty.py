"""End-to-end test over a real pseudo-terminal.

Proves the transport path (the same code used to talk to a real serial device)
plus the parser work together against an actual tty device, with no hardware and
no third-party packages. This is the local dev harness in miniature.
"""

import asyncio
import os
import sys
import termios
import threading
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol
from heatermeterd.links import SerialLink
from heatermeterd.serial_io import read_fd_lines
from heatermeterd.state import HeaterMeterState

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import hmsim  # noqa: E402


def test_pty_stream_parses_and_builds_state():
    master, slave = os.openpty()
    # Put both ends in raw mode so the PTY behaves like a real UART: no
    # canonical line-buffering (which can drop bytes under load and corrupt a
    # line) and no \n -> \r\n output translation. A real serial port is raw.
    for fd in (master, slave):
        tty.setraw(fd)

    board = hmsim.SimBoard(setpoint=225.0, seed=1)

    def writer():
        for line in board.config_lines():
            os.write(master, line.encode())
        for _ in range(5):
            board.step(1.0)
            os.write(master, board.status_line().encode())
            time.sleep(0.02)
        os.close(master)  # EOF stops the reader

    th = threading.Thread(target=writer)
    th.start()

    state = HeaterMeterState()
    status_count = 0
    for line in read_fd_lines(slave):
        s = protocol.parse(line)
        assert s is not None, f"unparseable line: {line!r}"
        assert s.checksum_ok, f"bad checksum on simulated line: {line!r}"
        state.ingest(s)
        if s.type == "HMSU":
            status_count += 1

    th.join()
    os.close(slave)

    assert status_count == 5
    assert state.device_name == "HeaterMeter"
    assert state.probe_names[0] == "Pit"
    assert state.status.pit is not None


def test_serial_link_pause_resume():
    """SerialLink.pause() stops ingestion; resume() restarts it with a fresh
    reader so a half-line stranded by the (simulated) board reset is not
    mis-parsed. This is the serial side of the firmware-flash window."""
    master, slave = os.openpty()
    for fd in (master, slave):
        tty.setraw(fd)
    slave_name = os.ttyname(slave)

    async def scenario():
        loop = asyncio.get_running_loop()
        got = []
        link = SerialLink(slave_name)
        link.start(lambda ln: got.append(ln), loop)

        # 1. Running: a complete line is ingested.
        os.write(master, protocol.frame("HMSU,225,198,,,,30,30,0,30,0").encode())
        await asyncio.sleep(0.15)
        assert any(l.startswith("$HMSU") for l in got)

        # 2. Paused: nothing is ingested, even though bytes are written
        #    (including a half-line, as the board would emit while resetting).
        link.pause()
        before = len(got)
        os.write(master, protocol.frame("HMSU,225,199,,,,31,31,0,31,0").encode())
        os.write(master, b"$HMSU,GARB")  # no terminator: a stranded half-line
        await asyncio.sleep(0.15)
        assert len(got) == before, "ingested while paused"

        # 3. The board reset discards in-flight serial; mirror that by flushing
        #    the tty input, then resume and confirm clean fresh lines flow and
        #    the stranded garbage never appears.
        termios.tcflush(slave, termios.TCIFLUSH)
        link.resume()
        os.write(master, protocol.frame("HMSU,225,200,,,,32,32,0,32,0").encode())
        await asyncio.sleep(0.15)
        fresh = got[before:]
        assert any("200" in l for l in fresh), "no ingestion after resume"
        assert not any("GARB" in l for l in fresh), "stranded half-line leaked"
        assert not any("199" in l for l in fresh), "paused line leaked"

        link.close()

    asyncio.run(scenario())
    os.close(master)
    os.close(slave)
