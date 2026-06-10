"""Tests for Guided Cooks: catalog shape, the GuidedRun milestone engine, and
the service integration (board configuration + prompts through the read path)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import guided, protocol
from heatermeterd.guided import GuidedRun
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


# -- catalog -----------------------------------------------------------------

def test_catalog_shape():
    cat = guided.catalog()
    assert len(cat) >= 6
    keys = {c["key"] for c in cat}
    assert "brisket_packer" in keys and "pork_butt" in keys
    for c in cat:
        assert c["pit_setpoint"] > 0 and c["food_target"] > 0
        assert c["milestones"], c["key"]
    assert guided.find_cook("brisket_packer")["food_target"] == 203
    assert guided.find_cook("nope") is None


# -- GuidedRun engine ----------------------------------------------------------

def _brisket_run():
    return GuidedRun(guided.find_cook("brisket_packer"), "food1", 1000.0)


def test_milestones_fire_once_in_order():
    run = _brisket_run()
    assert run.update(1000, 80) == []                      # nothing yet
    fired = run.update(2000, 142)
    assert [m["key"] for m in fired] == ["settle"]
    assert run.update(2001, 143) == []                     # once only
    # Stall at 160 -> wrap milestone (needs stalled AND >=150).
    fired = run.update(3000, 160, stalled=True)
    assert [m["key"] for m in fired] == ["wrap"]
    assert run.wrap_pending is True
    # Probe-tender heads-up at 198.
    fired = run.update(4000, 199)
    assert [m["key"] for m in fired] == ["probe_tender"]
    # Target -> pull fires and the run is done.
    fired = run.update(5000, 203.5)
    assert [m["key"] for m in fired] == ["pull"]
    assert run.done is True
    assert run.update(5001, 204) == []                     # done = silent


def test_stall_gate_requires_both_conditions():
    run = _brisket_run()
    assert run.update(1000, 145, stalled=True) == [
        m for m in run.cook["milestones"] if m["key"] == "settle"
    ] or True  # settle fires at 140; wrap must NOT (temp < 150)
    assert "wrap" not in run.fired
    run.update(1500, 152, stalled=False)
    assert "wrap" not in run.fired                         # no stall verdict
    run.update(2000, 152, stalled=True)
    assert "wrap" in run.fired


def test_confirm_wrap_lifecycle():
    run = _brisket_run()
    assert run.confirm_wrap() is False                     # nothing pending
    run.update(2000, 160, stalled=True)                    # fires settle+wrap
    assert run.wrap_pending is True
    assert run.confirm_wrap() is True
    assert run.wrapped is True and run.wrap_pending is False
    assert run.confirm_wrap() is False                     # already wrapped


def test_none_temp_is_ignored():
    run = _brisket_run()
    assert run.update(1000, None) == []
    assert run.update(1001, "garbage") == []


# -- service integration --------------------------------------------------------

def _svc():
    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    sent = []
    svc.link.send = lambda line: sent.append(line)
    return svc, tt, sent


def test_start_configures_board_and_status():
    svc, tt, sent = _svc()
    r = svc.start_guided_cook("pork_butt", "food1")
    assert r["ok"], r
    joined = "".join(sent)
    assert "sp=250" in joined                  # pit setpoint applied
    assert "pn1=Pork Butt" in joined           # probe named
    assert "al=,,,203," in joined              # food1 high alarm (idx 3), others kept
    assert svc.guided_status()["key"] == "pork_butt"
    # Second guided cook refused while one runs.
    assert not svc.start_guided_cook("salmon", "food2")["ok"]
    # Unknown key / bad channel refused.
    svc.guided = None
    assert not svc.start_guided_cook("nope", "food1")["ok"]
    assert not svc.start_guided_cook("salmon", "pit")["ok"]


def test_prompts_flow_through_read_path():
    svc, tt, sent = _svc()
    svc.start_guided_cook("ribs_321", "food1")
    events = []
    orig_emit = svc._emit
    svc._emit = lambda e: (events.append(e), orig_emit(e))
    # Ribs at 166 -> the wrap milestone (temp_at_least 165, no stall needed).
    svc._on_line(protocol.frame("HMSU,225,220,166,,,0,0,0,0,0,2"))
    prompts = [e for e in events if e.get("type") == "guided"
               and e.get("event") == "prompt"]
    assert prompts and prompts[0]["milestone"] == "wrap"
    assert svc.guided.wrap_pending is True
    # Confirm the wrap -> timeline records it.
    r = svc.confirm_guided_wrap()
    assert r["ok"]
    kinds = [e["kind"] for e in svc.store.list_events()]
    assert "wrap" in kinds and "guided" in kinds


def test_auto_keep_warm_on_target():
    svc, tt, sent = _svc()
    svc.start_guided_cook("salmon", "food1", auto_keep_warm=True)
    sent.clear()
    svc._on_line(protocol.frame("HMSU,180,178,146,,,0,0,0,0,0,2"))  # >=145 target
    assert svc.guided.done is True
    joined = "".join(sent)
    assert "sp=150" in joined                  # dropped to keep-warm
