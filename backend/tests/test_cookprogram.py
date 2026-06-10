"""Tests for multi-stage cook programs (pure logic + live runner)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import cookprogram, presets
from heatermeterd.cookprogram import (validate_program, ProgramState,
                                      ProgramError, CookProgramRunner)
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


# -- validation -------------------------------------------------------------

def test_program_presets_all_validate():
    """Every bundled program preset must be a runnable, valid program."""
    progs = presets.all_presets()["program"]
    assert progs, "expected at least one program preset"
    keys = set()
    for p in progs:
        assert p["key"] not in keys, f"duplicate preset key {p['key']}"
        keys.add(p["key"])
        assert p.get("label") and p.get("stages"), p
        # Should not raise - each preset is a valid cook program.
        stages = validate_program(p["stages"])
        assert len(stages) == len(p["stages"])
    # Spot-check a known multi-stage one.
    brisket = presets.get_program_preset("brisket_low_slow")
    assert brisket and len(brisket["stages"]) == 3


def test_validate_ok():
    stages = validate_program([
        {"name": "Smoke", "setpoint": 250,
         "advance": {"type": "probe", "channel": "food1", "temp": 165}},
        {"name": "Keep warm", "setpoint": 150, "advance": {"type": "manual"}},
    ])
    assert len(stages) == 2
    assert stages[0]["setpoint"] == 250.0
    assert stages[0]["advance"]["channel"] == "food1"


def test_validate_shutdown_stage():
    stages = validate_program([{"name": "Off", "setpoint": "off",
                                "advance": {"type": "manual"}}])
    assert stages[0]["shutdown"] is True
    assert stages[0]["setpoint"] is None


def test_validate_time_stage():
    stages = validate_program([{"name": "Sear", "setpoint": 450,
                                "advance": {"type": "time", "seconds": 600}}])
    assert stages[0]["advance"]["seconds"] == 600


def test_validate_rejects_empty():
    for bad in [[], None, "x"]:
        try:
            validate_program(bad)
        except ProgramError:
            pass
        else:
            raise AssertionError(f"expected ProgramError for {bad!r}")


def test_validate_rejects_bad_channel():
    try:
        validate_program([{"setpoint": 250,
                           "advance": {"type": "probe", "channel": "nope", "temp": 1}}])
    except ProgramError:
        pass
    else:
        raise AssertionError("expected ProgramError")


# -- pure state machine -----------------------------------------------------

def test_state_advances_on_probe():
    stages = validate_program([
        {"name": "A", "setpoint": 250, "advance": {"type": "probe", "channel": "food1", "temp": 165}},
        {"name": "B", "setpoint": 150, "advance": {"type": "manual"}},
    ])
    s = ProgramState(stages=stages)
    s.start(0)
    assert s.current["name"] == "A"
    assert not s.should_advance(10, {"food1": 100})   # below target
    assert s.should_advance(10, {"food1": 170})       # at/above target
    s.advance(10)
    assert s.current["name"] == "B"
    # Manual stage never auto-advances.
    assert not s.should_advance(99999, {"food1": 999})


def test_state_advances_on_time():
    stages = validate_program([
        {"name": "Hold", "setpoint": 250, "advance": {"type": "time", "seconds": 60}},
        {"name": "Next", "setpoint": 150, "advance": {"type": "manual"}},
    ])
    s = ProgramState(stages=stages)
    s.start(1000)
    assert not s.should_advance(1030, {})   # 30s < 60s
    assert s.should_advance(1061, {})       # 61s >= 60s


def test_state_completes():
    stages = validate_program([
        {"name": "Only", "setpoint": 250, "advance": {"type": "time", "seconds": 10}},
    ])
    s = ProgramState(stages=stages)
    s.start(0)
    assert s.advance(11) is None
    assert s.done is True
    assert s.current is None


# -- live runner against the simulated board --------------------------------

def test_runner_drives_setpoint_through_stages():
    async def scenario():
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)  # silent sim
        svc = HeaterMeterService(link, Store(":memory:"))
        await svc.start()

        stages = validate_program([
            {"name": "Smoke", "setpoint": 250,
             "advance": {"type": "probe", "channel": "food1", "temp": 165}},
            {"name": "Finish", "setpoint": 275,
             "advance": {"type": "probe", "channel": "food1", "temp": 203}},
            {"name": "Keep warm", "setpoint": 150, "advance": {"type": "manual"}},
        ])
        svc.start_program(stages, name="Brisket")
        await asyncio.sleep(0.1)
        assert link.board.setpoint == 250   # stage 0 applied

        # Food1 reaches 165 -> advance to stage 1 (275).
        svc.program.on_sample(svc.time_fn(), {"food1": 170})
        await asyncio.sleep(0.1)
        assert link.board.setpoint == 275
        assert svc.program.state.current["name"] == "Finish"

        # Food1 reaches 203 -> advance to keep-warm (150).
        svc.program.on_sample(svc.time_fn(), {"food1": 205})
        await asyncio.sleep(0.1)
        assert link.board.setpoint == 150
        assert svc.program.state.current["name"] == "Keep warm"

        # Manual advance from keep-warm -> done.
        assert svc.advance_program() is True
        assert svc.program.state.done is True

        await svc.stop()

    asyncio.run(scenario())


def test_runner_shutdown_stage_sets_manual_zero():
    async def scenario():
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)
        svc = HeaterMeterService(link, Store(":memory:"))
        await svc.start()
        stages = validate_program([
            {"name": "Cook", "setpoint": 250, "advance": {"type": "time", "seconds": 1}},
            {"name": "Shutdown", "setpoint": "off", "advance": {"type": "manual"}},
        ])
        svc.start_program(stages)
        await asyncio.sleep(0.1)
        t0 = svc.time_fn()
        svc.program.on_sample(t0 + 2, {})   # time elapsed -> shutdown stage
        await asyncio.sleep(0.1)
        assert link.board.manual is True
        assert link.board.output == 0
        await svc.stop()

    asyncio.run(scenario())


def test_program_persistence():
    s = Store(":memory:")
    stages = validate_program([{"name": "X", "setpoint": 250,
                                "advance": {"type": "manual"}}])
    pid = s.save_program("My Brisket", stages, 1000.0)
    progs = s.list_programs()
    assert len(progs) == 1
    assert progs[0]["name"] == "My Brisket"
    assert progs[0]["stages"][0]["setpoint"] == 250
    s.delete_program(pid)
    assert s.list_programs() == []
