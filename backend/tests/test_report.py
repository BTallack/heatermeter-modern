"""Tests for the per-cook report builder (pure) and its API endpoint."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import report


def _columns(n=50):
    t0 = 1_700_000_000
    return {
        "t": [t0 + i * 60 for i in range(n)],
        "set_point": [225] * n,
        "pit": [200 + (i % 10) for i in range(n)],
        "food1": [100 + i for i in range(n)],
        "food2": [None] * n,
        "ambient": [70] * n,
        "fan_pct": [30 + (i % 5) for i in range(n)],
    }


def test_compute_stats():
    s = report.compute_stats(_columns())
    assert s["samples"] == 50
    assert s["duration"] == 49 * 60
    assert s["pit"]["min"] == 200 and s["pit"]["max"] == 209
    assert s["food1"]["max"] == 149
    assert s["food2"] is None
    assert s["fan_pct"]["min"] == 30


def test_build_svg_draws_series_and_events():
    cols = _columns()
    events = [{"ts": cols["t"][10], "kind": "setpoint", "label": "Set 250°"},
              {"ts": cols["t"][20], "kind": "stall_start"},
              {"ts": 1, "kind": "target"}]   # out of range: skipped
    svg = report.build_svg(cols, events)
    assert svg.startswith("<svg")
    assert svg.count("<polyline") == 4       # set, pit, food1, ambient (food2 empty)
    assert svg.count("<circle") == 2         # two in-range event dots
    assert "°" in svg                        # temp axis labels


def test_build_svg_empty():
    svg = report.build_svg({"t": []})
    assert "No samples" in svg


def test_report_html_structure_and_escaping():
    cols = _columns()
    session = {"id": 7, "name": "Brisket <&> Test", "started_ts": cols["t"][0],
               "completed_ts": cols["t"][-1]}
    events = [{"ts": cols["t"][5], "kind": "lid_open", "label": "Lid open"}]
    notes = [{"ts": cols["t"][8], "text": "<script>alert(1)</script> wrapped",
              "photo": "abc.jpg"}]
    page = report.build_report_html(session, cols, events, notes,
                                    probe_names=["Pit", "Point", "Flat", "Amb"])
    assert "Brisket &lt;&amp;&gt; Test" in page
    assert "<script>alert(1)</script>" not in page      # escaped
    assert "&lt;script&gt;" in page
    assert "Completed" in page                          # badge
    assert "/api/photo/abc.jpg" in page
    assert "Lid open" in page
    assert "Point" in page                  # custom probe name used
    assert "Flat" not in page               # food2 has no data -> row skipped
    assert page.count("<svg") == 1


def test_report_api_endpoint():
    try:
        from fastapi.testclient import TestClient
    except Exception:
        print("    (skipped: fastapi/httpx not installed)")
        return
    from heatermeterd.api import create_app
    from heatermeterd.links import SimLink
    from heatermeterd.service import HeaterMeterService
    from heatermeterd.store import Store

    svc = HeaterMeterService(SimLink(interval=10.0), Store(":memory:"))
    app = create_app(svc)
    with TestClient(app) as c:
        # Unknown session -> 404.
        assert c.get("/api/report/999").status_code == 404
        # Make a session with a couple of samples + an event.
        sid = svc.store.start_session(1000.0, name="Test Cook")
        from heatermeterd.protocol import Status
        svc.store.insert(Status(set_point=225, pit=200, food1=100), 1000.0,
                         session_id=sid)
        svc.store.insert(Status(set_point=225, pit=210, food1=120), 1060.0,
                         session_id=sid)
        svc.store.add_event(1030.0, "setpoint", session_id=sid, label="Set 225°")
        r = c.get(f"/api/report/{sid}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Test Cook" in r.text and "<svg" in r.text and "Set 225°" in r.text
