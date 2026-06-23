"""Integration test: the service drives the MQTT bridge end to end.

Uses the in-process SimLink and a fake MQTT client so no broker is needed.
Verifies state is published per HMSU, and an HA setpoint command reaches the
board.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from heatermeterd import mqtt, protocol
from heatermeterd.links import SimLink
from heatermeterd.service import HeaterMeterService
from heatermeterd.store import Store


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.on_connect = None
        self.on_message = None

    def will_set(self, *a, **k): pass
    def username_pw_set(self, *a, **k): pass
    def connect(self, *a, **k): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, topic): self.subscribed.append(topic)
    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))
    def fire_connect(self): self.on_connect(self, None, None, 0)
    def fire_message(self, topic, payload):
        self.on_message(self, None, type("M", (), {"topic": topic, "payload": payload}))


def test_predicted_done_published_without_ntfy():
    """Regression: the MQTT 'predicted done' sensors must populate from cached
    predictions even when push notifications are NOT configured. Previously the
    cache was only filled by the ntfy ETA-push path, so with ntfy off HA showed
    'unknown' despite the web UI computing ETAs on demand."""
    import json as _json
    from heatermeterd import protocol

    async def scenario():
        clock = {"t": 5000.0}
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=30.0, seed=1)  # slow: no auto feed
        svc = HeaterMeterService(link, store, time_fn=lambda: clock["t"])

        fake = FakeClient()
        svc.mqtt = mqtt.MqttBridge("localhost", client=fake)
        await svc.start()
        fake.fire_connect()
        # Push notifications stay OFF (default). Set a food1 target (high alarm
        # at index 3) so the predictor has something to aim at.
        svc.state.alarms = [-40, -200, -40, 185, -40, -200, -40, -200]

        # Feed a clean rising food1 trend (pit steady = the cooker environment).
        food1 = 120.0
        for i in range(40):
            clock["t"] = 5000.0 + i * 30
            svc._on_line(protocol.frame(
                f"HMSU,225,225,{food1:.1f},,75,40,40,0,40,0"))
            food1 += 1.0   # ~2 deg/min, stays below the 185 target

        # The prediction cache is populated despite ntfy being disabled.
        assert "food1" in svc.last_predictions
        assert svc.last_predictions["food1"]["done_at"] is not None

        # And the latest published MQTT state carries both the aggregate and the
        # per-probe predicted-done timestamp (not None -> HA shows a time).
        state_pubs = [p for p in fake.published if p[0] == svc.mqtt.state_topic]
        payload = _json.loads(state_pubs[-1][1])
        assert payload["predicted_done"] is not None
        assert payload["predicted_done_food1"] is not None
        assert payload["predicted_done_food2"] is None   # no target on food2

        await svc.stop()

    asyncio.run(scenario())


def test_service_publishes_state_and_accepts_ha_setpoint():
    async def scenario():
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=0.05, seed=1)
        svc = HeaterMeterService(link, store)

        fake = FakeClient()
        svc.mqtt = mqtt.MqttBridge("localhost", client=fake,
                                   on_setpoint=svc.mqtt_set_setpoint)
        await svc.start()
        fake.fire_connect()  # simulate broker connect -> discovery + subscribe

        # Let a few HMSU updates flow; each should publish a state message.
        await asyncio.sleep(0.4)

        state_pubs = [p for p in fake.published if p[0] == svc.mqtt.state_topic]
        assert len(state_pubs) >= 2
        payload = json.loads(state_pubs[-1][1])
        assert "pit" in payload

        # HA writes the setpoint; it must reach the simulated board.
        fake.fire_message(svc.mqtt.setpoint_command_topic, b"305")
        await asyncio.sleep(0.2)
        assert link.board.setpoint == 305.0

        await svc.stop()
        # Closing marks availability offline.
        assert (svc.mqtt.availability_topic, "offline", True) in fake.published

    asyncio.run(scenario())


def test_service_accepts_ha_probe_rename_and_publishes_names():
    async def scenario():
        store = Store(":memory:")
        link = SimLink(setpoint=225.0, interval=0.05, seed=1)
        svc = HeaterMeterService(link, store)

        fake = FakeClient()
        svc.mqtt = mqtt.MqttBridge("localhost", client=fake,
                                   on_name=svc.mqtt_set_name)
        await svc.start()
        fake.fire_connect()

        # Names are carried in the published state once HMSU/HMPN flow.
        await asyncio.sleep(0.4)
        state_pubs = [p for p in fake.published if p[0] == svc.mqtt.state_topic]
        assert state_pubs
        payload = json.loads(state_pubs[-1][1])
        assert payload["name_pit"] == "Pit"
        assert payload["name_food1"] == "Food1"

        # HA renames food1; the cleaned name reaches the simulated board (index 1).
        fake.fire_message(svc.mqtt.name_command_topic("food1"), b"Brisket")
        await asyncio.sleep(0.2)
        assert link.board.probe_names[1] == "Brisket"

        # Ambient maps to board index 3.
        fake.fire_message(svc.mqtt.name_command_topic("ambient"), b"Grate")
        await asyncio.sleep(0.2)
        assert link.board.probe_names[3] == "Grate"

        await svc.stop()

    asyncio.run(scenario())


def test_mqtt_config_precedence_and_persistence(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    svc = HeaterMeterService(SimLink(), Store(":memory:"))
    svc.mqtt_config_path = os.path.join(d, "mqtt.json")

    # 1. No file, no env default -> disabled default.
    eff = svc.mqtt_effective_config()
    assert eff["enabled"] is False and eff["node_id"] == "hm"

    # 2. Env default seeds when no file exists.
    svc._mqtt_env_default = {"enabled": True, "host": "1.2.3.4", "port": 1883,
                             "username": "u", "password": "secret", "node_id": "hm"}
    assert svc.mqtt_effective_config()["host"] == "1.2.3.4"

    # 3. Saved file wins over the env default and round-trips.
    svc.save_mqtt_file({"enabled": True, "host": "10.0.0.5", "port": 8883,
                        "username": "ha", "password": "pw", "node_id": "pit"})
    eff = svc.mqtt_effective_config()
    assert eff["host"] == "10.0.0.5" and eff["port"] == 8883 and eff["node_id"] == "pit"
    # File is chmod 600 (password lives there).
    assert (os.stat(svc.mqtt_config_path).st_mode & 0o777) == 0o600


def test_mqtt_status_public_never_leaks_password():
    svc = HeaterMeterService(SimLink(), Store(":memory:"))
    svc._mqtt_env_default = {"enabled": True, "host": "h", "port": 1883,
                            "username": "u", "password": "topsecret", "node_id": "hm"}
    pub = svc.mqtt_status_public()
    assert "password" not in pub
    assert pub["has_password"] is True
    assert pub["host"] == "h" and pub["enabled"] is True
