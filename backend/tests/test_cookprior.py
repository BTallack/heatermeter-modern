"""Tests for the cut-aware time-to-done prior (pure)."""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import cookprior


def test_profile_selection():
    assert cookprior.profile_for(203, "Pork Shoulder") == "pulled"
    assert cookprior.profile_for(203, "Brisket") == "pulled"
    assert cookprior.profile_for(203, None) == "pulled"
    assert cookprior.profile_for(195, "Pork Ribs") == "ribs"
    assert cookprior.profile_for(195, None) == "ribs"
    assert cookprior.profile_for(165, "Chicken") == "roast"
    assert cookprior.profile_for(135, "Food 1") == "roast"
    assert cookprior.profile_for(None, None) == "roast"


def test_curves_are_monotone_and_normalised():
    for name, crv in cookprior.DEFAULT_CURVES.items():
        assert crv[0] == (0.0, 0.0) and crv[-1] == (1.0, 1.0), name
        rs = [r for r, _ in crv]
        ts = [t for _, t in crv]
        assert rs == sorted(rs) and ts == sorted(ts), name


def test_estimate_scales_the_curve_from_progress():
    # A cook that follows the pulled curve exactly: 10 h to the finish window
    # from 45 deg. At the point where the curve says 25% of the time has
    # elapsed (rise fraction 0.68), 2.5 h in -> 7.5 h to go.
    t0, target = 45.0, 203.0
    finish = target - cookprior.FINISH_MARGIN
    temp = t0 + 0.68 * (finish - t0)
    r = cookprior.estimate(2.5 * 3600, t0, temp, target, "pulled")
    assert r is not None and r["profile"] == "pulled"
    assert abs(r["eta"] - 7.5 * 3600) < 60
    assert r["eta_low"] < r["eta"] < r["eta_high"]
    assert abs(r["fraction"] - 0.25) < 0.01


def test_estimate_replays_the_real_pork_shoulder():
    # Numbers from the 07-02 cook (probe in at 12:21, ~45 deg; 199 deg at 20:21):
    # at 13:21 the meat was 131, the models said ~2 h; the truth was ~7 h to the
    # finish window. The prior lands within its band.
    r = cookprior.estimate(3600, 45.0, 131.0, 203.0, "pulled")
    truth = 7 * 3600
    assert r["eta_low"] <= truth <= r["eta_high"], r
    assert 5 * 3600 < r["eta"] < 9 * 3600
    # 16:21, 164.5 deg, 4 h in: truth 4 h to the window.
    r2 = cookprior.estimate(4 * 3600, 45.0, 164.5, 203.0, "pulled")
    assert r2["eta_low"] <= 4 * 3600 <= r2["eta_high"], r2
    # 19:51, 189.3 deg, 7.5 h in: truth 30 min.
    r3 = cookprior.estimate(7.5 * 3600, 45.0, 189.3, 203.0, "pulled")
    assert r3["eta"] < 1.5 * 3600


def test_estimate_refuses_without_enough_progress():
    assert cookprior.estimate(600, 45, 90, 203, "pulled") is None        # too soon
    assert cookprior.estimate(3600, 45, 50, 203, "pulled") is None       # no rise
    assert cookprior.estimate(3600, 45, 201, 203, "pulled") is None      # in the window
    assert cookprior.estimate(3600, 200, 201, 203, "pulled") is None     # started inside
    assert cookprior.estimate(None, 45, 120, 203) is None


def test_learn_curve_and_merge():
    # Synthesise a cook that follows the ribs curve over 5 h.
    crv = cookprior.DEFAULT_CURVES["ribs"]
    t0, target = 40.0, 195.0
    finish = target - cookprior.FINISH_MARGIN
    ts, temps = [], []
    total = 5 * 3600
    for k in range(0, 301):
        tau = k / 300
        # invert the curve: rise fraction at time fraction tau
        r = 0.0
        for (r0, tt0), (r1, tt1) in zip(crv, crv[1:]):
            if tau <= tt1:
                r = r0 + (r1 - r0) * (tau - tt0) / (tt1 - tt0) if tt1 > tt0 else r1
                break
        ts.append(t0 * 0 + k * total / 300)
        temps.append(t0 + r * (finish - t0) + (0.5 if k == 300 else 0))
    learned = cookprior.learn_curve(ts, temps, target)
    assert learned is not None and learned[0] == (0.0, 0.0) and learned[-1] == (1.0, 1.0)
    for r, tau in learned:
        assert abs(tau - cookprior._interp(crv, r)) < 0.03, (r, tau)
    merged = cookprior.merge_curve(None, learned)
    assert merged["n"] == 1
    merged2 = cookprior.merge_curve(merged, cookprior.DEFAULT_CURVES["roast"])
    assert merged2["n"] == 2
    mid = dict(merged2["curve"])[0.6]
    assert min(cookprior._interp(crv, 0.6), 0.32) <= mid <= max(cookprior._interp(crv, 0.6), 0.32)
    assert cookprior.curve_from_store({"ribs": merged2}, "ribs") == [tuple(p) for p in merged2["curve"]]
    assert cookprior.curve_from_store({}, "ribs") == crv


def test_learn_curve_rejects_unfinished_or_short():
    ts = [i * 60 for i in range(100)]
    assert cookprior.learn_curve(ts, [100 + i * 0.5 for i in range(100)], 203) is None
    assert cookprior.learn_curve(ts[:10], [40 + i * 20 for i in range(10)], 203) is None


# -- service wiring -------------------------------------------------------------

def _cook_service(tmpdir):
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store
    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc.cookprior_path = os.path.join(tmpdir, "cookprior.json")
    svc._push = lambda *a, **k: None
    tt = [1_000_000.0]
    svc.time_fn = lambda: tt[0]
    return svc, tt


def test_service_anchors_probe_start_and_blends_prior():
    import tempfile
    from heatermeterd import protocol
    svc, tt = _cook_service(tempfile.mkdtemp())
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
    # Probe goes in at 45 deg; two hours of a slowing climb to 131 deg.
    for i in range(120):
        tt[0] += 60
        svc._on_line(protocol.frame(f"HMSU,265,265,{45 + 86 * (i / 119) ** 0.7:.1f},,,50,50,0,25,0,2"))
    assert svc._food_start["food1"][1] == 45.0
    svc._last_pred_refresh = 0
    svc._refresh_predictions(tt[0])
    p = svc.last_predictions["food1"]
    assert p["prior_eta"] is not None and p["prior_eta"] > 3 * 3600
    assert p["eta"] > 2 * 3600                       # the prior pulled it well past the physics
    assert p["model"] in ("linear", "scurve", "prior")
    # A probe moved to a fresh, cold piece re-anchors.
    tt[0] += 60
    svc._on_line(protocol.frame("HMSU,265,265,36,,,50,50,0,25,0,2"))
    svc._prior_for("food1", 203.0, tt[0])
    assert svc._food_start["food1"][1] == 36.0


def test_service_anchors_on_the_probes_first_reading_not_the_target():
    import tempfile
    from heatermeterd import protocol
    svc, tt = _cook_service(tempfile.mkdtemp())
    # Two hours of climbing from 45 with NO target set yet...
    for i in range(120):
        tt[0] += 60
        svc._on_line(protocol.frame(f"HMSU,265,265,{45 + 86 * (i / 119) ** 0.7:.1f},,,50,50,0,25,0,2"))
    assert "food1" not in svc._food_start
    # ...then the target arrives: the anchor is the first reading of the cook.
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
    tt[0] += 60
    svc._on_line(protocol.frame("HMSU,265,265,131.5,,,50,50,0,25,0,2"))
    start_ts, start_temp = svc._food_start["food1"]
    assert start_temp == 45.0 and tt[0] - start_ts > 7000
    assert svc.last_predictions["food1"]["prior_eta"] is not None


def test_service_learns_shape_when_target_reached():
    import json
    import tempfile
    from heatermeterd import protocol, cookprior
    svc, tt = _cook_service(tempfile.mkdtemp())
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,195,-1,-1,-1,-1"))
    crv = cookprior.DEFAULT_CURVES["ribs"]
    # Four hours following the ribs curve from 40 to 195.
    for k in range(0, 241):
        tau = k / 240
        r = 0.0
        for (r0, t0), (r1, t1) in zip(crv, crv[1:]):
            if tau <= t1:
                r = r0 + (r1 - r0) * (tau - t0) / (t1 - t0) if t1 > t0 else r1
                break
        temp = 40 + r * (195 - 3 - 40) + (4 if k == 240 else 0)
        tt[0] += 60
        svc._on_line(protocol.frame(f"HMSU,250,250,{temp:.1f},,,40,40,0,20,0,2"))
    # The alarm rings (firmware suffixes H) -> target event -> learning.
    svc._on_line(protocol.frame("HMAL,-1,-1,-1,195H,-1,-1,-1,-1"))
    svc._check_alarms(tt[0])
    learned = svc._cookprior_curves.get("ribs")
    assert learned and learned["n"] == 1
    on_disk = json.load(open(svc.cookprior_path))
    assert on_disk["ribs"]["n"] == 1
    for r, tau in learned["curve"]:
        assert abs(tau - cookprior._interp(crv, r)) < 0.05, (r, tau)
    assert svc.cook_insights()["learned_shapes"] == {"ribs": 1}


def test_live_activity_push_and_end():
    import asyncio
    import tempfile
    from heatermeterd import protocol
    from tests.test_apns import FakeSender

    async def scenario():
        svc, tt = _cook_service(tempfile.mkdtemp())
        svc.push_config_path = os.path.join(tempfile.mkdtemp(), "push.json")
        await svc.start()
        svc.save_push_config({"enabled": True, "team_id": "T", "key_id": "K",
                              "key_path": "/dev/null", "bundle_id": "dev.tallack.heatermeter"})
        svc.register_push_token("devtok")
        svc.register_push_token("acttok", kind="liveactivity")
        fake = FakeSender()
        svc._apns_sender = lambda: fake
        svc._on_line(protocol.frame("HMAL,-1,-1,-1,203,-1,-1,-1,-1"))
        for i in range(40):
            tt[0] += 60
            svc._on_line(protocol.frame(f"HMSU,250,248,{120 + i:.0f},,,40,40,0,20,0,2"))
        svc._last_pred_refresh = 0
        svc._refresh_predictions(tt[0])
        await asyncio.sleep(0.2)
        la = [x for x in fake.sent if x[2].get("live_activity")]
        assert la and all(x[0] == "acttok" for x in la)
        aps = la[-1][1]["aps"]                      # the latest update
        assert aps["event"] == "update" and aps["content-state"]["pit"] == 248.0
        assert aps["content-state"]["food1"] == 159.0
        assert "stale-date" in aps
        # Throttled: a second refresh 30 s later does not push again.
        n = len(la)
        svc._last_pred_refresh = 0
        svc._refresh_predictions(tt[0] + 30)
        await asyncio.sleep(0.1)
        assert len([x for x in fake.sent if x[2].get("live_activity")]) == n
        # Finishing the cook ends the activity, high priority, with a dismissal date.
        svc.finish_cook()
        await asyncio.sleep(0.2)
        ends = [x for x in fake.sent if x[2].get("live_activity") and x[1]["aps"]["event"] == "end"]
        assert ends and "dismissal-date" in ends[0][1]["aps"] and ends[0][2]["priority"] == "high"
        # Alerts never went to the activity token.
        assert all(x[0] != "acttok" for x in fake.sent if not x[2].get("live_activity"))
        await svc.stop()

    asyncio.run(scenario())
