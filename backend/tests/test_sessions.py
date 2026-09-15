"""Tests for session + note storage and CSV export."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd.protocol import Status
from heatermeterd.store import Store


def _status(pit):
    return Status(set_point=225.0, pit=pit, food1=None, food2=None, ambient=72.0,
                  output_pct=30.0, fan_pct=30.0, servo_pct=0.0, lid_countdown=0)


def test_session_lifecycle():
    s = Store(":memory:")
    sid = s.start_session(1000, name="Brisket")
    assert s.open_session()["id"] == sid
    s.insert(_status(100), ts=1001, session_id=sid)
    s.insert(_status(101), ts=1002, session_id=sid)
    assert s.count(session_id=sid) == 2
    s.close_session(sid, 1003)
    assert s.open_session() is None


def test_session_list_and_search():
    s = Store(":memory:")
    a = s.start_session(1000, name="Brisket cook")
    s.close_session(a, 2000)
    b = s.start_session(3000, name="Pork ribs")
    s.close_session(b, 4000)
    alls = s.list_sessions()
    assert len(alls) == 2
    found = s.list_sessions(search="ribs")
    assert len(found) == 1 and found[0]["id"] == b


def test_session_update_and_delete():
    s = Store(":memory:")
    sid = s.start_session(1000)
    s.insert(_status(100), ts=1001, session_id=sid)
    s.update_session(sid, name="Renamed", description="great cook")
    got = s.get_session(sid)
    assert got["name"] == "Renamed"
    assert got["description"] == "great cook"
    s.delete_session(sid)
    assert s.get_session(sid) is None
    assert s.count(session_id=sid) == 0   # samples cascade-deleted


def test_history_filtered_by_session():
    s = Store(":memory:")
    a = s.start_session(0)
    s.insert(_status(10), ts=1, session_id=a)
    b = s.start_session(100)
    s.insert(_status(20), ts=101, session_id=b)
    ha = s.history_columns(session_id=a)
    assert ha["pit"] == [10]
    hb = s.history_columns(session_id=b)
    assert hb["pit"] == [20]


def test_session_sharing():
    s = Store(":memory:")
    sid = s.start_session(0, name="Shared")
    s.insert(_status(100), ts=1, session_id=sid)
    # Enable sharing with a token.
    s.set_session_share(sid, "tok123")
    got = s.session_by_share_token("tok123")
    assert got is not None and got["id"] == sid
    # Disable sharing.
    s.set_session_share(sid, None)
    assert s.session_by_share_token("tok123") is None


def test_notes():
    s = Store(":memory:")
    sid = s.start_session(0)
    n1 = s.add_note(10, "wrapped the brisket", session_id=sid, channel="pit")
    s.add_note(20, "spritzed", session_id=sid)
    notes = s.list_notes(session_id=sid)
    assert len(notes) == 2
    assert notes[0]["text"] == "wrapped the brisket"
    assert notes[0]["channel"] == "pit"
    s.delete_note(n1)
    assert len(s.list_notes(session_id=sid)) == 1


def test_export_csv():
    s = Store(":memory:")
    sid = s.start_session(0)
    s.insert(_status(100), ts=1700000000, session_id=sid)
    s.insert(_status(101), ts=1700000005, session_id=sid)
    csv_text = s.export_csv(session_id=sid)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("timestamp_iso,epoch,set_point,pit")
    assert len(lines) == 3   # header + 2 rows
    assert ",100" in lines[1] or ",100.0" in lines[1]


def test_recent_series():
    s = Store(":memory:")
    sid = s.start_session(0)
    for i in range(10):
        s.insert(_status(100 + i), ts=1000 + i, session_id=sid)
    ts, vals = s.recent_series("pit", seconds=5, now=1009)
    # window is now-5 = 1004 .. 1009 inclusive -> 6 points
    assert ts[0] >= 1004
    assert vals[-1] == 109


def test_repeat_cook_and_insights():
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.protocol import Status
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    sent = []
    svc.link.send = lambda line: sent.append(line)

    # A finished cook: ran at 250 with food1 reaching its 203 target, stalled
    # for half an hour along the way.
    sid = svc.store.start_session(1000.0, name="Brisket Day")
    for i in range(20):
        svc.store.insert(Status(set_point=250, pit=248, food1=150 + i), 1000.0 + i,
                         session_id=sid)
    svc.store.add_event(1500.0, "stall_start", session_id=sid, channel="food1")
    svc.store.add_event(1500.0 + 1800, "stall_end", session_id=sid, channel="food1")
    svc.store.add_event(2000.0, "target", session_id=sid, channel="food1",
                        label="Brisket reached target", value=203.2)
    svc.store.close_session(sid, 3000.0)

    r = svc.repeat_cook(sid)
    assert r["ok"], r
    assert r["setpoint"] == 250 and r["targets"] == {"food1": 203}
    joined = "".join(sent)
    assert "sp=250" in joined and "al=,,,203," in joined

    ins = svc.cook_insights()
    assert ins["cooks"] == 1 and ins["stalls_seen"] == 1
    assert ins["avg_stall_secs"] == 1800
    assert ins["avg_duration_secs"] is None      # never marked done: no duration
    svc.store.mark_completed(sid, 2500.0, "manual")
    assert svc.cook_insights()["avg_duration_secs"] == 1500.0

    # Unknown session refused; session with no setpoint refused.
    assert not svc.repeat_cook(999)["ok"]
    sid2 = svc.store.start_session(5000.0)
    svc.store.close_session(sid2, 5001.0)
    assert not svc.repeat_cook(sid2)["ok"]


def test_resume_active_cook_after_restart():
    """Power blip mid-cook: a session with a setpoint at the last sample is
    resumed (data stays continuous)."""
    import asyncio
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService

    store = Store(":memory:")
    sid = store.start_session(1000.0)
    store.insert(Status(set_point=225, pit=210), 1000.0, session_id=sid)
    store.insert(Status(set_point=225, pit=215), 1060.0, session_id=sid)

    svc = HeaterMeterService(SimLink(interval=10.0), store)

    async def go():
        await svc.start()
        assert svc.session_id == sid          # resumed the active cook
        assert store.open_session()["id"] == sid
        await svc.stop()
    asyncio.run(go())


def test_idle_session_not_resumed_starts_fresh():
    """Unplugged-while-idle then moved: the old session is closed (data kept)
    and the next sample starts a brand-new cook."""
    import asyncio
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService

    store = Store(":memory:")
    old = store.start_session(1000.0)
    # Last sample had no setpoint -> cooker was idle/off when power was lost.
    store.insert(Status(set_point=None, pit=72), 1000.0, session_id=old)
    store.insert(Status(set_point=None, pit=70), 1060.0, session_id=old)

    svc = HeaterMeterService(SimLink(interval=10.0), store)

    async def go():
        await svc.start()
        assert svc.session_id is None              # did NOT resume the idle one
        # No cook ever ran in it, so it is pruned outright - samples kept, untagged.
        assert store.get_session(old) is None
        assert store.count() == 2
        # Idle samples on the new grill do not open a "cook" either...
        svc._on_line(protocol.frame("HMSU,,70,,,,0,0,0,0,0,4"))
        assert svc.session_id is None and store.count() == 3
        # ...the first sample with a setpoint does.
        svc._on_line(protocol.frame("HMSU,225,210,,,,0,0,0,0,0,2"))
        assert svc.session_id is not None and svc.session_id != old
        await svc.stop()
    asyncio.run(go())


def test_prune_keeps_cooks_and_noted_sessions():
    s = Store(":memory:")
    idle = s.start_session(1000.0)
    s.insert(Status(set_point=None, pit=70), 1000.0, session_id=idle)
    s.close_session(idle, 1100.0)
    cook = s.start_session(2000.0)
    s.insert(Status(set_point=225, pit=200), 2000.0, session_id=cook)
    s.close_session(cook, 2100.0)
    noted = s.start_session(3000.0)
    s.insert(Status(set_point=None, pit=70), 3000.0, session_id=noted)
    s.add_note(3001.0, "Bench test of the new probe", session_id=noted)
    s.close_session(noted, 3100.0)
    live = s.start_session(4000.0)
    s.insert(Status(set_point=None, pit=70), 4000.0, session_id=live)
    assert s.prune_empty_sessions(keep_id=live) == [idle]
    assert {x["id"] for x in s.list_sessions()} == {cook, noted, live}
    assert s.count() == 4                         # the idle samples survive


def test_manual_fan_session_is_resumed():
    """A negative (manual-fan) setpoint counts as an active cook -> resumed."""
    import asyncio
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService

    store = Store(":memory:")
    sid = store.start_session(1000.0)
    store.insert(Status(set_point=-50, pit=200), 1000.0, session_id=sid)

    svc = HeaterMeterService(SimLink(interval=10.0), store)

    async def go():
        await svc.start()
        assert svc.session_id == sid
        await svc.stop()
    asyncio.run(go())


def test_thermometer_only_cook_opens_a_session():
    """Pit off, but a targeted food probe is plugged in: that is a cook."""
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc._push = lambda *a, **k: None
    svc._on_line(protocol.frame("HMSU,,72,150,,,0,0,0,0,0,4"))   # probe, no target
    assert svc.session_id is None
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
    svc._on_line(protocol.frame("HMSU,,72,151,,,0,0,0,0,0,4"))   # probe + target
    assert svc.session_id is not None


def test_session_activity_rule():
    s = Store(":memory:")
    done = s.start_session(1000.0)
    s.insert(Status(set_point=None, pit=70, food1=150), 1000.0, session_id=done)
    s.mark_completed(done, 1500.0, "probe removed")
    s.close_session(done, 1600.0)
    evented = s.start_session(2000.0)
    s.insert(Status(set_point=None, pit=70, food1=150), 2000.0, session_id=evented)
    s.add_event(2001.0, "target", session_id=evented, channel="food1")
    s.close_session(evented, 2100.0)
    auto = s.start_session(3000.0)
    s.insert(Status(set_point=None, pit=70), 3000.0, session_id=auto)
    s.add_event(3001.0, "food_target", session_id=auto, channel="food1")
    s.close_session(auto, 3100.0)
    assert s.prune_empty_sessions() == [auto]

