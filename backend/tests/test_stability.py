"""Tests for the pit-stability score (pure)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import stability


def cols(pit, sp=250.0, lid=None, out=None, step=10):
    n = len(pit)
    return {"t": [i * step for i in range(n)], "set_point": [sp] * n, "pit": pit,
            "lid_countdown": lid or [0] * n, "output_pct": out or [40.0] * n}


def test_perfect_hold_scores_100():
    # 20 min climb 100->250, then 2 h dead on set.
    pit = [100 + i * 1.25 for i in range(120)] + [250.0] * 720
    r = stability.score_cook(cols(pit))
    assert r["score"] == 100
    assert r["in_band_pct"] == 100.0 and r["mae"] <= 0.2   # the last in-band climb samples
    assert r["overshoot"] == 0.0 and r["lid_opens"] == 0
    assert r["held_secs"] == 720 * 10 - 10 or r["held_secs"] >= 7000
    assert r["setpoints"] == [250]


def test_climb_is_not_penalised_but_overshoot_is_reported():
    pit = [100 + i * 1.25 for i in range(120)] + [265.0] * 30 + [250.0] * 720
    r = stability.score_cook(cols(pit))
    assert r["overshoot"] == 15.0
    assert r["in_band_pct"] < 100.0
    pit2 = [100 + i * 1.5 for i in range(120)] + [250.0] * 720   # climbs through to 278
    r2 = stability.score_cook(cols(pit2))
    assert r2["overshoot"] > 20


def test_wobble_lowers_score_and_mae():
    import math
    pit = [100 + i * 1.25 for i in range(120)] + [250 + 15 * math.sin(i / 10) for i in range(720)]
    r = stability.score_cook(cols(pit))
    assert 40 < r["score"] < 95
    assert r["in_band_pct"] < 80
    assert 5 < r["mae"] < 12


def test_lid_window_and_grace_are_excluded_and_recovery_measured():
    n = 120 + 720
    pit = [100 + i * 1.25 for i in range(120)] + [250.0] * 720
    lid = [0] * n
    # Lid open for 60 s at t=3000 (k=300), pit crashes to 180 and recovers over 4 min.
    for k in range(300, 306):
        lid[k] = 60 - (k - 300) * 10
        pit[k] = 180.0
    for k in range(306, 330):
        pit[k] = 180 + (k - 306) * 3          # back inside the band (>=240) by k~326
    r = stability.score_cook(cols(pit, lid=lid))
    assert r["lid_opens"] == 1
    assert 150 <= r["lid_recovery_secs"] <= 260
    assert r["in_band_pct"] == 100.0          # the dip fell inside lid + grace
    assert r["score"] == 100


def test_slow_lid_recovery_costs_points():
    n = 120 + 1440
    pit = [100 + i * 1.25 for i in range(120)] + [250.0] * 1440
    lid = [0] * n
    for k in range(300, 306):
        lid[k] = 60 - (k - 300) * 10
    for k in range(306, 306 + 240):         # 40 min at 200 before recovering
        pit[k] = 200.0
    r = stability.score_cook(cols(pit, lid=lid))
    assert r["lid_recovery_secs"] >= 1800
    assert r["score"] < 100
    assert r["in_band_pct"] < 100.0          # beyond the grace it counts against the hold


def test_setpoint_bump_restarts_the_climb():
    pit = [250.0] * 360 + [250 + i * 0.5 for i in range(100)] + [300.0] * 360
    sp = [250.0] * 360 + [300.0] * 460
    c = cols(pit)
    c["set_point"] = sp
    r = stability.score_cook(c)
    assert r["setpoints"] == [250, 300]
    assert r["in_band_pct"] == 100.0         # the climb to 300 is not "off target"


def test_no_score_without_a_held_setpoint():
    assert stability.score_cook(cols([100.0] * 50)) is None            # never reached
    c = cols([250.0] * 50)
    c["set_point"] = [None] * 50
    assert stability.score_cook(c) is None                             # board off
    assert stability.score_cook({"t": [], "set_point": [], "pit": []}) is None


def test_log_gaps_do_not_count_as_held_time():
    c = cols([250.0] * 600)
    c["t"] = [i * 10 for i in range(300)] + [100000 + i * 10 for i in range(300)]
    r = stability.score_cook(c)
    assert 5900 <= r["held_secs"] <= 6060


def test_dead_fire_is_excluded_not_penalised():
    # 2 h steady hold, then the fuel runs out: the pit sinks to 120 with the
    # setpoint still 250 for 6 h. The hold is judged on the 2 h; the collapse
    # is reported as fire-out time.
    pit = [250.0] * 720 + [250 - i * 2 for i in range(65)] + [120.0] * 2160
    r = stability.score_cook(cols(pit))
    assert r["score"] >= 85          # the half hour of collapse still costs a little
    assert r["fire_out_secs"] >= 5 * 3600
    assert r["held_secs"] < 3 * 3600


def test_brief_deep_dip_that_recovers_counts_against_the_hold():
    pit = [250.0] * 720 + [140.0] * 60 + [250.0] * 720
    r = stability.score_cook(cols(pit))
    assert r["fire_out_secs"] == 0
    assert r["in_band_pct"] < 100.0


def test_setpoint_drop_is_a_transition_until_settled():
    # 250 hold, then the user drops to 200: the 30 min of cooling toward 200
    # are not "off target", and the earlier 250 is not an overshoot of 200.
    pit = [250.0] * 720 + [250 - i * 0.28 for i in range(180)] + [200.0] * 720
    sp = [250.0] * 720 + [200.0] * 900
    c = cols(pit)
    c["set_point"] = sp
    r = stability.score_cook(c)
    assert r["in_band_pct"] == 100.0
    assert r["overshoot"] == 0.0


def test_short_hold_is_not_scored():
    pit = [100 + i * 1.25 for i in range(120)] + [250.0] * 120   # 20 min hold
    assert stability.score_cook(cols(pit)) is None


def test_compare_ranks_against_history():
    assert stability.compare(80, [])["verdict"] == "first scored cook"
    r = stability.compare(90, [70, 85, 60])
    assert r["rank"] == "1 of 4" and r["best"] == 85 and r["avg"] == 72
    assert r["verdict"] == "your steadiest cook yet"
    assert stability.compare(50, [70, 85, 60])["verdict"] == "rougher than usual"
    assert stability.compare(73, [70, 75, 72])["verdict"] == "about your usual"
    assert stability.compare(82, [70, 85, 72])["verdict"] == "steadier than usual"


def test_service_session_stability_scores_and_caches():
    import json
    import tempfile
    from heatermeterd import protocol
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    svc.stability_cache_path = os.path.join(tempfile.mkdtemp(), "stability.json")
    tt = [1000.0]
    svc.time_fn = lambda: tt[0]
    svc._push = lambda *a, **k: None
    # A 10-minute climb, then 30 minutes dead on 250.
    for i in range(60):
        tt[0] += 10
        svc._on_line(protocol.frame(f"HMSU,250,{100 + i * 2.5:.1f},,,,100,100,0,50,0,0"))
    for _ in range(180):
        tt[0] += 10
        svc._on_line(protocol.frame("HMSU,250,250,,,,40,40,0,20,0,2"))
    sid = svc.session_id
    assert sid
    r = svc.session_stability(sid)
    assert r["live"] is True
    assert r["stability"]["score"] == 100 and r["stability"]["setpoints"] == [250]
    assert r["compare"]["verdict"] == "first scored cook"
    # Live results are memoised for a minute.
    assert svc.session_stability(sid)["stability"] is r["stability"]

    svc.finish_cook()
    r2 = svc.session_stability(sid)
    assert r2["live"] is False and r2["stability"]["score"] == 100
    cache = json.load(open(svc.stability_cache_path))
    assert cache[str(sid)]["score"] == 100
    assert svc.session_stability(999999) is None


def test_report_renders_stability_line():
    from heatermeterd import report
    n = 3
    cols = {"t": [0, 60, 120], "set_point": [250.0] * n, "pit": [250.0, 251.0, 249.0],
            "food1": [None] * n, "food2": [None] * n, "ambient": [None] * n,
            "output_pct": [40.0] * n, "fan_pct": [40.0] * n, "servo_pct": [0.0] * n,
            "lid_countdown": [0] * n}
    stab = {"stability": {"score": 84, "in_band_pct": 96.0, "mae": 3.2,
                          "lid_recovery_secs": 240},
            "compare": {"cooks": 4, "avg": 71, "best": 88, "rank": "2 of 5",
                        "verdict": "steadier than usual"}}
    page = report.build_report_html({"id": 1, "name": "Test", "started_ts": 0, "ended_ts": 120},
                                    cols, [], [], insights={"stability": stab})
    assert "Pit steadiness <b>84</b>/100" in page
    assert "lid recovery 4 min" in page and "2 of 5" in page
    page2 = report.build_report_html({"id": 1, "started_ts": 0}, cols, [], [])
    assert "Pit steadiness" not in page2
