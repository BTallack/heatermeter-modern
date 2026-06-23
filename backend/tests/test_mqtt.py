"""Tests for the MQTT / Home Assistant discovery bridge.

No broker required: discovery payloads are pure, and the bridge is driven with a
fake client that records publishes and can inject messages.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import mqtt


class FakeClient:
    def __init__(self):
        self.published = []      # (topic, payload, retain)
        self.subscribed = []
        self.on_connect = None
        self.on_message = None
        self.will = None

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def username_pw_set(self, u, p):
        self.creds = (u, p)

    def connect(self, host, port):
        self.conn = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    # test helper to simulate a connection completing
    def fire_connect(self):
        self.on_connect(self, None, None, 0)

    def fire_message(self, topic, payload):
        msg = type("M", (), {"topic": topic, "payload": payload})
        self.on_message(self, None, msg)


def test_discovery_configs_shape():
    cfgs = mqtt.discovery_configs(node_id="hm", version="20210203B")
    topics = [t for t, _ in cfgs]
    # 4 temp sensors + fan + lid binary + setpoint number + 3 target numbers = 10,
    # +3 intelligence (stalled, fuel_low, predicted_done) = 13,
    # +3 per-probe predicted_done sensors = 16,
    # +4 name sensors + 4 name text entities = 24.
    assert len(cfgs) == 24
    assert any("/sensor/hm/pit/config" in t for t in topics)
    assert any("/binary_sensor/hm/lid/config" in t for t in topics)
    assert any("/number/hm/setpoint/config" in t for t in topics)
    assert any("/number/hm/target_food1/config" in t for t in topics)
    assert any("/number/hm/target_ambient/config" in t for t in topics)
    # Every config shares the same device identifiers.
    for _, cfg in cfgs:
        assert cfg["device"]["identifiers"] == ["heatermeter_hm"]
        assert cfg["device"]["sw_version"] == "20210203B"
        assert cfg["unique_id"].startswith("heatermeter_hm_")


def test_temp_sensors_show_board_unit_without_conversion():
    # Temperature sensors must NOT use device_class temperature, or HA would
    # convert them to the system unit (a metric HA showing our F values as C).
    f = dict(mqtt.discovery_configs(unit="F"))
    pit_f = [cfg for t, cfg in f.items() if t.endswith("/sensor/hm/pit/config")][0]
    assert "device_class" not in pit_f
    assert pit_f["unit_of_measurement"] == "°F"
    c = dict(mqtt.discovery_configs(unit="C"))
    pit_c = [cfg for t, cfg in c.items() if t.endswith("/sensor/hm/pit/config")][0]
    assert pit_c["unit_of_measurement"] == "°C"
    setp_c = [cfg for t, cfg in c.items() if t.endswith("/setpoint/config")][0]
    assert setp_c["unit_of_measurement"] == "°C" and setp_c["max"] == 300


def test_target_entities_writable_and_in_payload():
    cfgs = dict(mqtt.discovery_configs())
    tf1 = [cfg for t, cfg in cfgs.items() if t.endswith("/target_food1/config")][0]
    assert tf1["command_topic"].endswith("/target/food1/set")
    assert tf1["value_template"] == "{{ value_json.target_food1 }}"
    p = mqtt.state_payload({"pit": 200}, {"food1": 203, "food2": None, "ambient": 165})
    assert p["target_food1"] == 203
    assert p["target_food2"] is None
    assert p["target_ambient"] == 165


def test_bridge_target_command_invokes_callback():
    got = {}
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake, node_id="hm",
                             on_target=lambda ch, v: got.update(ch=ch, v=v))
    bridge.connect()
    bridge._on_connect(fake, None, None, 0)
    assert bridge.target_command_topic("food1") in fake.subscribed
    fake.fire_message(bridge.target_command_topic("food2"), b"165")
    assert got == {"ch": "food2", "v": 165.0}


def test_setpoint_entity_is_writable():
    cfgs = mqtt.discovery_configs()
    setp = [cfg for t, cfg in cfgs if t.endswith("/setpoint/config")][0]
    assert "command_topic" in setp
    assert setp["command_topic"].endswith("/setpoint/set")


def test_state_payload_flattens_and_handles_nones():
    status = {"set_point": 225, "pit": 198.5, "food1": None, "food2": 140,
              "ambient": 72, "fan_pct": 30, "servo_pct": 0, "output_pct": 35,
              "lid_countdown": 0}
    p = mqtt.state_payload(status)
    assert p["pit"] == 198.5
    assert p["food1"] is None
    assert p["lid_open"] == "false"

    status["lid_countdown"] = 12
    assert mqtt.state_payload(status)["lid_open"] == "true"


def test_bridge_connect_publishes_discovery_and_availability():
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake, node_id="hm")
    bridge.version = "20210203B"
    bridge.connect()
    fake.fire_connect()

    # availability online + discovery for all 24 entities published retained.
    assert (bridge.availability_topic, "online", True) in fake.published
    discovery_pubs = [p for p in fake.published if "/config" in p[0]]
    assert len(discovery_pubs) == 24
    assert all(retain for _, _, retain in discovery_pubs)
    # subscribed to the setpoint command topic.
    assert bridge.setpoint_command_topic in fake.subscribed


def test_bridge_state_publish():
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake)
    bridge.publish_state({"pit": 200, "set_point": 225, "fan_pct": 40,
                          "lid_countdown": 0})
    state_pubs = [p for p in fake.published if p[0] == bridge.state_topic]
    assert len(state_pubs) == 1
    payload = json.loads(state_pubs[0][1])
    assert payload["pit"] == 200
    assert payload["fan_pct"] == 40


def test_bridge_setpoint_command_invokes_callback():
    got = {}
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake,
                             on_setpoint=lambda v: got.update(value=v))
    bridge.connect()
    fake.fire_connect()
    fake.fire_message(bridge.setpoint_command_topic, b"275")
    assert got["value"] == 275.0

    # Bad payloads are ignored, not crashing.
    fake.fire_message(bridge.setpoint_command_topic, b"not a number")
    assert got["value"] == 275.0


def test_bridge_close_marks_offline():
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake)
    bridge.close()
    assert (bridge.availability_topic, "offline", True) in fake.published


def test_make_client_real_paho_constructs():
    """Regression for the paho-mqtt 2.x constructor.

    Every other test injects a FakeClient, so _make_client() (the real paho
    path) is never exercised. paho 2.x requires an explicit CallbackAPIVersion;
    a bare mqtt.Client() raises there, which silently disabled the whole MQTT
    bridge in production. This guards that _make_client() builds a real client
    without raising. Skips cleanly when paho isn't installed.
    """
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        print("    (skipped: paho-mqtt not installed)")
        return
    bridge = mqtt.MqttBridge("localhost", username="u", password="p")
    client = bridge._make_client()  # must not raise on paho 1.x or 2.x
    assert client is not None


def test_state_payload_intelligence_extras():
    from heatermeterd.mqtt import state_payload
    p = state_payload({"pit": 225.0},
                      extras={"stalled": True, "fuel_low": False,
                              "predicted_done": "2026-06-10T16:45:00-06:00"})
    assert p["stalled"] == "true" and p["fuel_low"] == "false"
    assert p["predicted_done"].startswith("2026-06-10T16:45")
    # Defaults without extras: off/None.
    p2 = state_payload({"pit": 225.0})
    assert p2["stalled"] == "false" and p2["predicted_done"] is None


def test_per_probe_predicted_done_sensors_and_payload():
    cfgs = dict(mqtt.discovery_configs(node_id="hm"))
    # Each food channel gets its own predicted-done timestamp sensor.
    for channel in ("food1", "food2", "ambient"):
        s = [cfg for t, cfg in cfgs.items()
             if t.endswith(f"/sensor/hm/predicted_done_{channel}/config")]
        assert len(s) == 1, f"missing predicted_done sensor for {channel}"
        assert s[0]["device_class"] == "timestamp"
        assert s[0]["value_template"] == \
            f"{{{{ value_json.predicted_done_{channel} }}}}"
    # state_payload threads the per-channel map; absent channels stay None.
    p = mqtt.state_payload(
        {"pit": 225.0},
        extras={"predicted_done": "2026-06-10T16:45:00-06:00",
                "predicted_done_by": {"food1": "2026-06-10T16:45:00-06:00",
                                      "food2": "2026-06-10T18:10:00-06:00"}})
    assert p["predicted_done_food1"].startswith("2026-06-10T16:45")
    assert p["predicted_done_food2"].startswith("2026-06-10T18:10")
    assert p["predicted_done_ambient"] is None
    # No map at all -> all per-channel keys are None (HA shows "unknown").
    p2 = mqtt.state_payload({"pit": 225.0})
    assert p2["predicted_done_food1"] is None
    assert p2["predicted_done_food2"] is None


def test_name_entities_present_sensor_and_text():
    cfgs = dict(mqtt.discovery_configs(node_id="hm"))
    # Each probe gets a read-only label sensor and a writable text entity.
    for channel in ("pit", "food1", "food2", "ambient"):
        s = [cfg for t, cfg in cfgs.items()
             if t.endswith(f"/sensor/hm/name_{channel}/config")]
        assert len(s) == 1, f"missing name sensor for {channel}"
        assert s[0]["value_template"] == f"{{{{ value_json.name_{channel} }}}}"
        assert "command_topic" not in s[0]   # read-only

        t = [cfg for top, cfg in cfgs.items()
             if top.endswith(f"/text/hm/setname_{channel}/config")]
        assert len(t) == 1, f"missing name text entity for {channel}"
        assert t[0]["command_topic"].endswith(f"/name/{channel}/set")
        assert t[0]["value_template"] == f"{{{{ value_json.name_{channel} }}}}"
        assert t[0]["max"] == 13


def test_state_payload_carries_names_with_defaults():
    # No names -> stable defaults so HA templates never see blanks.
    p = mqtt.state_payload({"pit": 200})
    assert p["name_pit"] == "Pit"
    assert p["name_food1"] == "Food 1"
    assert p["name_food2"] == "Food 2"
    assert p["name_ambient"] == "Ambient"
    # Supplied names win; missing channels fall back to defaults.
    p2 = mqtt.state_payload(
        {"pit": 200},
        names={"pit": "Smoker", "food1": "Brisket", "food2": "", "ambient": None})
    assert p2["name_pit"] == "Smoker"
    assert p2["name_food1"] == "Brisket"
    assert p2["name_food2"] == "Food 2"     # blank -> default
    assert p2["name_ambient"] == "Ambient"  # None -> default


def test_bridge_name_command_invokes_callback():
    got = {}
    fake = FakeClient()
    bridge = mqtt.MqttBridge("localhost", client=fake, node_id="hm",
                             on_name=lambda ch, v: got.update(ch=ch, v=v))
    bridge.connect()
    fake.fire_connect()
    assert bridge.name_command_topic("food1") in fake.subscribed
    fake.fire_message(bridge.name_command_topic("food2"), b"Pork Butt")
    assert got == {"ch": "food2", "v": "Pork Butt"}
