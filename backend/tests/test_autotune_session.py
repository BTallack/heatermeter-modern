"""Integration test of the live AutoTuneSession driving the simulated board.

Feeds a synthetic oscillating pit temperature through the session and confirms
it toggles the relay, completes cycles, computes gains, writes them to the
(simulated) board, and returns to automatic mode.
"""

import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def test_autotune_completes_and_writes_pid():
    async def scenario():
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)  # silent sim
        svc = HeaterMeterService(link, Store(":memory:"))
        await svc.start()

        ok = svc.start_autotune(setpoint=225.0, rule="ziegler_nichols",
                                max_cycles=3, hysteresis=1.0)
        assert ok is True
        # Starting again while running is refused.
        assert svc.start_autotune(setpoint=225.0) is False

        # Feed a clean oscillation around the setpoint directly into the tuner.
        t = 0.0
        period = 60.0
        amp = 8.0
        # Enough samples for >3 cycles.
        for i in range(500):
            t = float(i) * 2.0      # 2s per sample
            pit = 225.0 + amp * math.sin(2 * math.pi * t / period)
            svc.tuner.on_sample(t, pit)
            if svc.tuner.done:
                break
            await asyncio.sleep(0)  # let the loop breathe

        assert svc.tuner.done is True
        assert svc.tuner.error is None
        r = svc.tuner.result
        assert r is not None
        assert r.kp > 0 and r.ki > 0 and r.kd > 0
        # The sim board should have received the new PID + returned to setpoint.
        await asyncio.sleep(0.1)
        assert link.board.pid["p"] == round(r.kp, 4)
        assert link.board.setpoint == 225.0

        await svc.stop()

    asyncio.run(scenario())


def test_autotune_aborts_on_pit_ceiling():
    async def scenario():
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        await svc.start()
        svc.start_autotune(setpoint=225.0, pit_ceiling=300.0)
        svc.tuner.on_sample(0.0, 250.0)
        svc.tuner.on_sample(1.0, 305.0)   # breaches ceiling
        assert svc.tuner.done is True
        assert "ceiling" in (svc.tuner.error or "")
        await svc.stop()

    asyncio.run(scenario())
