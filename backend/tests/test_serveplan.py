"""Tests for serve-time planning (pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import serveplan

NOW = 100_000.0


def cfg(**kw):
    base = {"enabled": True, "serve_ts": NOW + 4 * 3600, "channel": "auto",
            "rest_secs": 900, "hold_window_secs": 3600}
    base.update(kw)
    return base


def pred(done_in_secs, conf="medium", age=10):
    return {"ts": NOW - age, "eta": done_in_secs, "confidence": conf,
            "done_at": NOW + done_in_secs}


def test_sanitize_defaults_and_bounds():
    d = serveplan.sanitize(None)
    assert d["enabled"] is False and d["channel"] == "auto"
    d2 = serveplan.sanitize({"rest_secs": -5, "hold_window_secs": 10,
                             "channel": "bogus", "serve_ts": -1})
    assert d2["rest_secs"] == 0.0 and d2["hold_window_secs"] == 600.0
    assert d2["channel"] == "auto" and d2["serve_ts"] == 0.0


def test_off_and_past_and_stale():
    off = serveplan.assess(NOW, cfg(enabled=False), {})
    assert off["status"] == "off"
    past = serveplan.assess(NOW, cfg(serve_ts=NOW - 60), {})
    assert past["status"] == "past"
    stale = serveplan.assess(NOW, cfg(serve_ts=NOW - 7 * 3600), {})
    assert stale["status"] == "stale"


def test_no_target_vs_no_eta():
    r = serveplan.assess(NOW, cfg(), {}, any_target=False)
    assert r["status"] == "no_target"
    # A target exists but the prediction is unusable (no confidence).
    r2 = serveplan.assess(NOW, cfg(), {"food1": pred(3600, conf="none")},
                          any_target=True)
    assert r2["status"] == "no_eta"
    # Or the prediction is stale.
    r3 = serveplan.assess(NOW, cfg(), {"food1": pred(3600, age=600)},
                          any_target=True)
    assert r3["status"] == "no_eta"


def test_on_track_window():
    # Ready 30 min before serve (inside the hold window) -> on track.
    r = serveplan.assess(NOW, cfg(rest_secs=0),
                         {"food1": pred(4 * 3600 - 1800)}, any_target=True)
    assert r["status"] == "on_track"
    assert r["slack_secs"] == 1800
    assert r["advice"] == []


def test_late_advice_ladder_with_and_without_stall():
    # Ready 40 min AFTER serve.
    preds = {"food1": pred(4 * 3600 + 1500)}
    r = serveplan.assess(NOW, cfg(), preds, stalled_channels={"food1"},
                         any_target=True)
    assert r["status"] == "late"
    kinds = [a["kind"] for a in r["advice"]]
    assert kinds == ["wrap", "bump_pit", "push_serve"]
    assert "40 min" in r["advice"][-1]["text"]
    # No stall -> no wrap suggestion.
    r2 = serveplan.assess(NOW, cfg(), preds, stalled_channels=set(),
                          any_target=True)
    assert [a["kind"] for a in r2["advice"]] == ["bump_pit", "push_serve"]


def test_early_advice():
    # Ready 2h before serve (rest included) -> early, hold + slow-down advice.
    r = serveplan.assess(NOW, cfg(rest_secs=0),
                         {"food1": pred(2 * 3600)}, any_target=True)
    assert r["status"] == "early"
    assert [a["kind"] for a in r["advice"]] == ["hold", "drop_pit"]


def test_rest_shifts_ready_at():
    r = serveplan.assess(NOW, cfg(rest_secs=1800),
                         {"food1": pred(4 * 3600 - 1800)}, any_target=True)
    # done 30 min before serve + 30 min rest = ready exactly at serve.
    assert r["status"] == "on_track" and r["slack_secs"] == 0


def test_service_transitions_push_and_record():
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    pushes = []
    svc._push = lambda title, *a, **k: pushes.append(title)
    svc.state.alarms = ["-1", "-1", "-1", "203", "-1", "-1", "-1", "-1"]
    svc.save_serveplan({"serve_ts": 1000 + 7200, "enabled": True,
                        "rest_secs": 0})

    def preds(eta):
        svc.last_predictions = {"food1": {"ts": tt[0], "eta": eta,
                                          "confidence": "medium",
                                          "done_at": tt[0] + eta}}

    preds(5400)                      # ready 90 min before a 2h-out serve
    svc._drive_serveplan(tt[0])      # first observation: silent event only
    assert pushes == []
    tt[0] += 30; preds(9000)         # now predicted past serve -> late
    svc._drive_serveplan(tt[0])
    assert pushes and pushes[0].startswith("Running late")
    tt[0] += 30; preds(5400)         # caught back up
    svc._drive_serveplan(tt[0])
    assert any(p.startswith("Back on track") for p in pushes)
    kinds = [e["kind"] for e in svc.store.list_events()]
    assert kinds.count("serve_status") == 3

    # Finishing the cook clears the plan (it belongs to one cook).
    from heatermeterd import protocol
    svc._on_line(protocol.frame("HMSU,225,200,140,,,0,0,0,0,0,2"))  # session
    svc.save_serveplan({"serve_ts": tt[0] + 3600, "enabled": True})
    svc.finish_cook()
    assert svc._serveplan_cfg["enabled"] is False
    assert svc.serveplan_status() is None


def test_auto_channel_uses_latest_finisher():
    preds = {"food1": pred(3600), "food2": pred(2 * 3600)}
    assert serveplan.pick_done_at(preds, "auto", NOW) == NOW + 2 * 3600
    assert serveplan.pick_done_at(preds, "food1", NOW) == NOW + 3600
    # A stale/unusable prediction is excluded from auto.
    preds["food2"] = pred(2 * 3600, conf="none")
    assert serveplan.pick_done_at(preds, "auto", NOW) == NOW + 3600
