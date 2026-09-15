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


# -- bounds-aware planning -----------------------------------------------------

def pred2(done_in, high_in, conf="low", **extra):
    p = pred(done_in, conf=conf)
    p["done_at_high"] = NOW + high_in
    p.update(extra)
    return p


def test_at_risk_when_only_the_pessimistic_bound_is_late():
    # Model says ready 30 min before serve; its slow-side bound says 40 min after.
    preds = {"food1": pred2(4 * 3600 - 1800 - 900, 4 * 3600 + 2400 - 900)}
    r = serveplan.assess(NOW, cfg(), preds, any_target=True)
    assert r["status"] == "at_risk"
    assert r["slack_secs"] == 1800 and r["slack_high_secs"] == -2400
    assert [a["kind"] for a in r["advice"]] == ["bump_pit"]
    assert "40 min" in r["advice"][0]["text"]
    # Stalled adds the wrap suggestion first.
    r2 = serveplan.assess(NOW, cfg(), preds, stalled_channels={"food1"},
                          any_target=True)
    assert [a["kind"] for a in r2["advice"]] == ["wrap", "bump_pit"]


def test_late_needs_the_model_estimate_itself_to_miss():
    preds = {"food1": pred2(4 * 3600 + 600 - 900, 4 * 3600 + 3600 - 900)}
    r = serveplan.assess(NOW, cfg(), preds, any_target=True)
    assert r["status"] == "late"


def test_early_judged_on_the_pessimistic_bound():
    # Model 3h early but the slow-side bound only 20 min early -> on track,
    # not "plan a hold".
    preds = {"food1": pred2(3600, 4 * 3600 - 1200)}
    r = serveplan.assess(NOW, cfg(rest_secs=0), preds, any_target=True)
    assert r["status"] == "on_track"
    assert r["ready_at_high"] == NOW + 4 * 3600 - 1200


def test_plateau_governs_and_reads_late_with_bump_advice():
    plateau = {"ts": NOW - 10, "eta": None, "confidence": "low",
               "done_at": None, "model": "plateau", "plateau_temp": 202.4}
    # Auto channel: the plateaued probe is the latest finisher by definition,
    # even though the other probe is comfortably on track.
    r = serveplan.assess(NOW, cfg(), {"food1": pred(3600), "food2": plateau},
                         any_target=True)
    assert r["status"] == "late" and r["plateau_temp"] == 202.4
    assert r["ready_at"] is None
    assert r["advice"][0]["kind"] == "bump_pit" and "202°" in r["advice"][0]["text"]
    # A specific channel that is not the plateaued one ignores it.
    r2 = serveplan.assess(NOW, cfg(channel="food1"),
                          {"food1": pred(3600), "food2": plateau}, any_target=True)
    assert r2["status"] == "early"
    # A stale plateau is not acted on.
    old = dict(plateau, ts=NOW - 600)
    r3 = serveplan.assess(NOW, cfg(channel="food2"), {"food2": old}, any_target=True)
    assert r3["status"] == "no_eta"


def test_service_at_risk_and_plateau_transitions():
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

    def preds(eta, high=None, **extra):
        svc.last_predictions = {"food1": {
            "ts": tt[0], "eta": eta, "confidence": "low",
            "done_at": (tt[0] + eta) if eta is not None else None,
            "done_at_high": (tt[0] + high) if high is not None else None,
            **extra}}

    preds(5400, 5400)                # both bounds 30 min before serve
    svc._drive_serveplan(tt[0])
    assert pushes == []
    tt[0] += 30; preds(5400, 9000)   # estimate fine, slow side past serve
    svc._drive_serveplan(tt[0])
    assert pushes == ["Might run late for 12:00 AM"] or pushes[-1].startswith("Might run late")
    tt[0] += 30; preds(5400, 5400)
    svc._drive_serveplan(tt[0])
    assert pushes[-1].startswith("Back on track")
    tt[0] += 30; preds(None, None, model="plateau", plateau_temp=202.3)
    svc._drive_serveplan(tt[0])
    assert pushes[-1].startswith("Running late")
    labels = [e.get("label") or "" for e in svc.store.list_events()
              if e["kind"] == "serve_status"]
    assert any("Levelling off near 202" in l for l in labels)
    assert any("At risk" in l for l in labels)


def test_service_prediction_cache_carries_bounds_and_stall():
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    svc._push = lambda *a, **k: None
    # A steady 0.5 deg/min climb on food1 toward a 203 target for 70 minutes.
    for i in range(70):
        tt[0] += 60
        svc._on_line(protocol.frame(f"HMSU,265,265,{140 + i * 0.5:.1f},,,50,50,0,0,0,2"))
    svc.state.alarms = ["-1", "-1", "-1", "203", "-1", "-1", "-1", "-1"]
    svc._last_pred_refresh = 0
    svc._refresh_predictions(tt[0])
    p = svc.last_predictions["food1"]
    assert p["done_at"] is not None and p["done_at_high"] >= p["done_at"]
    assert p["model"] in ("linear", "scurve") and p["stalled"] is False
    assert p["plateau_temp"] is None
    # The watcher's stall verdict reaches the predictor.
    svc._probewatch._ch["food1"].stalled = True
    svc._last_pred_refresh = 0
    svc._refresh_predictions(tt[0] + 1)
    p = svc.last_predictions["food1"]
    assert p["stalled"] is True and p["model"] == "stall" and p["confidence"] == "low"
