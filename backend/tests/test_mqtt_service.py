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
