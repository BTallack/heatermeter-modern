"""Integration test of the service with the in-process simulated link.

Exercises links + service + state + store + protocol together, with no web
dependencies and no hardware.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


def test_service_rejects_bad_checksum_lines():
    # A corrupted/merged line (bad checksum) must NOT corrupt state. This guards
    # the real-hardware bug where a garbled $HMPN shifted probe-name fields.
    async def scenario():
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=10.0, seed=1)  # slow: no auto traffic
        svc = HeaterMeterService(link, store)
        await svc.start()

        # Seed a known-good probe-name sentence.
        good = protocol.frame("HMPN,Pit,Food1,Food2,Ambient")
        svc._on_line(good)
        assert svc.state.probe_names == ["Pit", "Food1", "Food2", "Ambient"]

        # A corrupted line with a wrong checksum must be ignored.
        corrupt = "$HMPN,GARBAGE,X,Y,Z*00"
        svc._on_line(corrupt)
        assert svc.state.probe_names == ["Pit", "Food1", "Food2", "Ambient"]
        assert svc.bad_checksums == 1

        # A good HMSU still flows through and is stored.
        svc._on_line(protocol.frame("HMSU,225,198,,,,30,30,0,30,0"))
        assert store.count() == 1

        await svc.stop()

    asyncio.run(scenario())


def test_service_streams_stores_and_controls():
    async def scenario():
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=0.05, seed=1)
        svc = HeaterMeterService(link, store)
        q = svc.subscribe()
        await svc.start()

        # Collect a handful of messages; some may be event side-channel messages
        # (e.g. session_started), so gather generously then filter for state.
        msgs = [await asyncio.wait_for(q.get(), timeout=3) for _ in range(6)]

        # Control path: change the setpoint and confirm the sim accepts it.
        await svc.send_command(protocol.set_setpoint(300))
        await asyncio.sleep(0.2)

        await svc.stop()
        return msgs, store, link, svc

    msgs, store, link, svc = asyncio.run(scenario())

    state_msgs = [m for m in msgs if "state" in m]
    assert len(state_msgs) >= 3
    assert all("ts" in m for m in state_msgs)
    assert state_msgs[0]["state"]["status"]["pit"] is not None
    assert store.count() >= 3
    assert link.board.setpoint == 300.0
    # A session was auto-started and tagged onto samples.
    assert svc.session_id is not None


def test_session_auto_start_and_idle_close():
    # Drive _on_line directly with controlled timestamps to exercise the
    # auto-start-on-data / auto-close-after-idle session lifecycle.
    async def scenario():
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=100.0, seed=1)  # effectively silent
        clock = {"t": 1000.0}
        svc = HeaterMeterService(link, store, time_fn=lambda: clock["t"],
                                 idle_gap=60.0)
        await svc.start()

        svc._on_line(protocol.frame("HMSU,225,100,,,,30,30,0,30,0"))
        first = svc.session_id
        assert first is not None

        clock["t"] = 1010.0
        svc._on_line(protocol.frame("HMSU,225,101,,,,30,30,0,30,0"))
        assert svc.session_id == first  # within idle gap -> same session

        clock["t"] = 1010.0 + 120.0     # exceed idle gap
        svc._on_line(protocol.frame("HMSU,225,102,,,,30,30,0,30,0"))
        assert svc.session_id != first  # new session

        await svc.stop()
        return store, first, svc.session_id

    store, first, second = asyncio.run(scenario())
    sessions = store.list_sessions()
    assert len(sessions) == 2
    # First session got closed (has ended_ts).
    closed = store.get_session(first)
    assert closed["ended_ts"] is not None


def test_cooker_profiles_roundtrip():
    import json as _json
    import tempfile

    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.store import Store
    from heatermeterd.service import HeaterMeterService

    with tempfile.TemporaryDirectory() as tmp:
        svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
        svc.profiles_path = os.path.join(tmp, "profiles.json")
        sent = []
        svc.link.send = lambda line: sent.append(line)
        # Seed live tuning state from board sentences.
        svc._on_line(protocol.frame("HMPD,4.0,3.0,0.01,5.0,F"))
        svc._on_line(protocol.frame("HMFN,10,80,130,225,2,50,0,100"))
        svc._on_line(protocol.frame("HMLD,6,240"))

        # Empty to start; save the current tuning as "Kamado".
        assert svc.get_profiles()["profiles"] == []
        r = svc.save_profile("Kamado")
        assert r["ok"] and r["active"] == "Kamado"
        on_disk = _json.load(open(svc.profiles_path))
        assert on_disk["profiles"][0]["pid"]["p"] == "3.0"

        # Saving under the same name replaces, not duplicates.
        svc.save_profile("Kamado")
        assert len(svc.get_profiles()["profiles"]) == 1

        # Apply sends the tuning back to the board (paced sender, first cmd now).
        sent.clear()
        r = svc.apply_profile("Kamado")
        assert r["ok"]
        assert any("pidb=" in s or "pidp=" in s for s in sent)

        # Unknown apply/delete refused; delete clears active.
        assert not svc.apply_profile("Nope")["ok"]
        assert svc.delete_profile("Kamado")["ok"]
        assert svc.get_profiles() == {"profiles": [], "active": None}
        assert not svc.delete_profile("Kamado")["ok"]
