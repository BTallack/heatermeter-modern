"""Tests for the in-memory state model."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import protocol
from heatermeterd.state import HeaterMeterState


def feed(state, payload):
    state.ingest(protocol.parse(protocol.frame(payload)))


def test_ingest_updates_slices():
    st = HeaterMeterState()
    feed(st, "UCID,HeaterMeter,20210202B")
    feed(st, "HMPN,Pit,Brisket,Food2,Ambient")
    feed(st, "HMPD,0,4.0,0.020,5.0,F")
    feed(st, "HMSU,225,198.5,145.0,,72.0,35,33,0,35,50")

    assert st.device_name == "HeaterMeter"
    assert st.version == "20210202B"
    assert st.probe_names == ["Pit", "Brisket", "Food2", "Ambient"]
    assert st.pid["p"] == "4.0"
    assert st.pid["units"] == "F"
    assert st.status.pit == 198.5
    assert st.status.set_point == 225.0


def test_timestamp_recorded_on_status():
    st = HeaterMeterState()
    st.ingest(protocol.parse(protocol.frame("HMSU,225,198,,,,30,30,0,30,0")), ts=123.0)
    assert st.last_update == 123.0


def test_unknown_type_stashed():
    st = HeaterMeterState()
    feed(st, "HMZZ,a,b,c")
    assert st.other["HMZZ"] == ["a", "b", "c"]


def test_log_is_bounded():
    st = HeaterMeterState()
    for i in range(70):
        feed(st, f"HMLG,message {i}")
    assert len(st.log) == 50
    assert st.log[-1] == "message 69"


def test_hmlb_decoded():
    st = HeaterMeterState()
    feed(st, "HMLB,40,255,13,10,11,0")
    assert st.display["backlight"] == "40"
    assert st.display["home_mode"] == "255"
    assert st.display["leds"] == ["13", "10", "11", "0"]


def test_hmrf_sources_decoded():
    st = HeaterMeterState()
    # receiver triplet (255,0,crc), then node 5 (low batt flag=1, rssi 3),
    # then node 9 (native flag=4, rssi 2).
    feed(st, "HMRF,255,0,200,5,1,3,9,4,2")
    assert len(st.rf_sources) == 2
    assert st.rf_sources[0] == {"node": 5, "low_battery": True,
                                "recent_reset": False, "native": False, "rssi": 3}
    assert st.rf_sources[1]["node"] == 9
    assert st.rf_sources[1]["native"] is True


def test_hmps_pid_internals():
    st = HeaterMeterState()
    feed(st, "HMPS,0,12.5,4.2,-1.0,0.3")
    assert st.pid_internals["p"] == "12.5"
    assert st.pid_internals["temp_d"] == "0.3"


def test_hmar_adc_noise():
    st = HeaterMeterState()
    feed(st, "HMAR,0,1,0,0,1,0")
    assert st.adc_noise == ["0", "1", "0", "0", "1", "0"]


def test_to_dict_serialisable():
    import json
    st = HeaterMeterState()
    feed(st, "HMSU,225,198.5,,,72,30,30,0,30,0")
    feed(st, "HMRF,255,0,200,5,1,3")
    feed(st, "HMLB,40,255,13,10,11,0")
    json.dumps(st.to_dict())  # must not raise
