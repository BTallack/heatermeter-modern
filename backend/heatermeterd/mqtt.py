"""MQTT bridge with Home Assistant auto-discovery.

This is the headline integration feature: with one MQTT broker configured, the
HeaterMeter shows up in Home Assistant automatically - pit/food/ambient temps,
setpoint, fan %, and lid state as entities, with the setpoint controllable from
HA. None of the commercial controllers ship this; the HA community reverse-
engineers them. We just provide it, locally, no cloud.

Design:
* The publish/discovery payload construction is PURE (no network), so it is
  unit-tested without a broker. See :func:`discovery_configs` and
  :func:`state_payload`.
* :class:`MqttBridge` wraps a paho-mqtt client (imported lazily). It is fed the
  same status dicts the WebSocket broadcasts, and it subscribes to a command
  topic so HA can set the pit setpoint. A client can be injected for testing.
* Entirely optional: if no broker host is configured, the bridge is never
  created and nothing changes. paho-mqtt is an optional dependency.

HA discovery follows the standard `<discovery_prefix>/<component>/<node>/<obj>/config`
convention with a shared `device` block so all entities group under one device.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_BASE_TOPIC = "heatermeter"

# Sensor entities: (object_id, name, unit, device_class, state field in status).
# unit None => no unit (e.g. percentages use the literal "%").
_TEMP_SENSORS = [
    ("pit", "Pit", "pit"),
    ("food1", "Food 1", "food1"),
    ("food2", "Food 2", "food2"),
    ("ambient", "Ambient", "ambient"),
]


def _device_block(node_id: str, version: Optional[str]) -> dict:
    return {
        "identifiers": [f"heatermeter_{node_id}"],
        "name": "HeaterMeter",
        "manufacturer": "HeaterMeter (open source)",
        "model": "HeaterMeter",
        "sw_version": version or "unknown",
    }


# Number-entity ranges per unit: setpoint (sp) and probe target (tg) as
# (min, max, step). The board reports/cooks in its own unit, so HA mirrors it.
def _ranges(unit: str) -> dict:
    if unit == "C":
        return {"sp": (40, 300, 1), "tg": (0, 180, 1)}
    return {"sp": (100, 500, 5), "tg": (32, 350, 1)}


# Food probes exposed as writable target-temp numbers in HA.
_TARGET_PROBES = [("food1", "Food 1"), ("food2", "Food 2"), ("ambient", "Ambient")]

# All four probes get a name entity: a read-only sensor (the name configured in
# HeaterMeter, handy in HA templates) plus a writable text input that renames
# the probe on the board. The fixed Pit/Food 1/Food 2 temperature sensors above
# keep their stable names; these expose the user-chosen labels separately.
_NAME_PROBES = [("pit", "Pit"), ("food1", "Food 1"),
                ("food2", "Food 2"), ("ambient", "Ambient")]


def discovery_configs(node_id: str = "hm", version: Optional[str] = None,
                      base_topic: str = DEFAULT_BASE_TOPIC,
                      discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
                      unit: str = "F") -> list[tuple[str, dict]]:
    """Return a list of (topic, config_dict) for HA MQTT discovery.

    Pure: no I/O. Each entry should be published retained to its topic. *unit* is
    the board's temperature unit ('F'/'C'); entities mirror it verbatim.
    """
    udeg = f"°{'C' if unit == 'C' else 'F'}"
    rng = _ranges(unit)
    state_topic = f"{base_topic}/{node_id}/state"
    avail_topic = f"{base_topic}/{node_id}/availability"
    dev = _device_block(node_id, version)
    out: list[tuple[str, dict]] = []

    def base(obj_id):
        return {
            "state_topic": state_topic,
            "availability_topic": avail_topic,
            "device": dev,
            "unique_id": f"heatermeter_{node_id}_{obj_id}",
        }

    # Temperature sensors. We deliberately do NOT set device_class=temperature:
    # HA force-converts temperature entities to the system unit (so a metric HA
    # would show our Fahrenheit values in Celsius). Without device_class HA shows
    # the value verbatim with our unit label; state_class keeps history/stats.
    for obj_id, name, field in _TEMP_SENSORS:
        cfg = base(obj_id)
        cfg.update({
            "name": name,
            "unit_of_measurement": udeg,
            "icon": "mdi:thermometer",
            "value_template": f"{{{{ value_json.{field} }}}}",
            "state_class": "measurement",
        })
        out.append((f"{discovery_prefix}/sensor/{node_id}/{obj_id}/config", cfg))

    # Fan output % sensor.
    fan = base("fan")
    fan.update({
        "name": "Fan Output",
        "unit_of_measurement": "%",
        "icon": "mdi:fan",
        "value_template": "{{ value_json.fan_pct }}",
        "state_class": "measurement",
    })
    out.append((f"{discovery_prefix}/sensor/{node_id}/fan/config", fan))

    # Lid-open binary sensor.
    lid = base("lid")
    lid.update({
        "name": "Lid Open",
        "device_class": "opening",
        "value_template": "{{ value_json.lid_open }}",
        "payload_on": "true",
        "payload_off": "false",
    })
    out.append((f"{discovery_prefix}/binary_sensor/{node_id}/lid/config", lid))

    # Cook intelligence: stall, fuel, and the predicted-done clock. These are
    # what make HA automations like "announce when the brisket stalls" or
    # "flash the porch light when fuel runs low" one-liners.
    stall = base("stalled")
    stall.update({
        "name": "Cook Stalled",
        "value_template": "{{ value_json.stalled }}",
        "payload_on": "true", "payload_off": "false",
        "icon": "mdi:chart-bell-curve",
    })
    out.append((f"{discovery_prefix}/binary_sensor/{node_id}/stalled/config", stall))

    fuel_low = base("fuel_low")
    fuel_low.update({
        "name": "Fuel Low",
        "device_class": "problem",
        "value_template": "{{ value_json.fuel_low }}",
        "payload_on": "true", "payload_off": "false",
        "icon": "mdi:fire-alert",
    })
    out.append((f"{discovery_prefix}/binary_sensor/{node_id}/fuel_low/config", fuel_low))

    pdone = base("predicted_done")
    pdone.update({
        "name": "Predicted Done",
        "device_class": "timestamp",
        "value_template": "{{ value_json.predicted_done }}",
        "icon": "mdi:clock-check-outline",
    })
    out.append((f"{discovery_prefix}/sensor/{node_id}/predicted_done/config", pdone))

    # Per-probe predicted-done clocks, so HA can show each food probe's ETA (the
    # aggregate above is just the soonest of these). Mirrors the target numbers.
    for channel, label in _TARGET_PROBES:
        pd = base(f"predicted_done_{channel}")
        pd.update({
            "name": f"{label} Predicted Done",
            "device_class": "timestamp",
            "value_template": f"{{{{ value_json.predicted_done_{channel} }}}}",
            "icon": "mdi:clock-check-outline",
        })
        out.append((f"{discovery_prefix}/sensor/{node_id}/predicted_done_{channel}/config", pd))

    # Setpoint as a HA number entity (read + write).
    smin, smax, sstep = rng["sp"]
    setp = base("setpoint")
    setp.update({
        "name": "Setpoint",
        "command_topic": f"{base_topic}/{node_id}/setpoint/set",
        "value_template": "{{ value_json.set_point }}",
        "unit_of_measurement": udeg,
        "min": smin, "max": smax, "step": sstep,
        "mode": "box",
        "icon": "mdi:thermometer",
    })
    out.append((f"{discovery_prefix}/number/{node_id}/setpoint/config", setp))

    # Per-probe target temperatures as writable HA number entities. Writing one
    # sets that probe's high alarm (its cook target). An empty/None value means
    # no target set.
    tmin, tmax, tstep = rng["tg"]
    for channel, name in _TARGET_PROBES:
        tgt = base(f"target_{channel}")
        tgt.update({
            "name": f"{name} Target",
            "command_topic": f"{base_topic}/{node_id}/target/{channel}/set",
            "value_template": f"{{{{ value_json.target_{channel} }}}}",
            "unit_of_measurement": udeg,
            "min": tmin, "max": tmax, "step": tstep,
            "mode": "box",
            "icon": "mdi:thermometer-alert",
        })
        out.append((f"{discovery_prefix}/number/{node_id}/target_{channel}/config", tgt))

    # Per-probe names: a read-only sensor (the label set in HeaterMeter, for use
    # in HA templates) and a writable text input that renames the probe on the
    # board. The temperature sensors above keep their fixed Pit/Food 1/Food 2
    # names so history is stable; these carry the user's chosen labels.
    for channel, default in _NAME_PROBES:
        nsensor = base(f"name_{channel}")
        nsensor.update({
            "name": f"{default} Label",
            "value_template": f"{{{{ value_json.name_{channel} }}}}",
            "icon": "mdi:tag-text-outline",
        })
        out.append((f"{discovery_prefix}/sensor/{node_id}/name_{channel}/config",
                    nsensor))

        ntext = base(f"setname_{channel}")
        ntext.update({
            "name": f"{default} Name",
            "command_topic": f"{base_topic}/{node_id}/name/{channel}/set",
            "value_template": f"{{{{ value_json.name_{channel} }}}}",
            "max": 13,                 # board EEPROM caps probe names at 13 chars
            "icon": "mdi:rename-box",
        })
        out.append((f"{discovery_prefix}/text/{node_id}/setname_{channel}/config",
                    ntext))

    return out


def state_payload(status: dict, targets: Optional[dict] = None,
                  extras: Optional[dict] = None,
                  names: Optional[dict] = None) -> dict:
    """Flatten a status dict into the JSON HA reads via value_template.

    *targets* maps food1/food2/ambient -> target temperature (or None).
    *extras* carries the cook-intelligence fields: ``stalled`` (bool),
    ``fuel_low`` (bool), ``predicted_done`` (soonest ISO-8601 timestamp or None),
    and ``predicted_done_by`` (per-channel ISO-8601 map).
    *names* maps pit/food1/food2/ambient -> the probe label set on the board."""
    def num(v):
        return v if isinstance(v, (int, float)) else None
    lid = status.get("lid_countdown") or 0
    targets = targets or {}
    extras = extras or {}
    names = names or {}
    pdone_by = extras.get("predicted_done_by") or {}
    return {
        "set_point": num(status.get("set_point")),
        "pit": num(status.get("pit")),
        "food1": num(status.get("food1")),
        "food2": num(status.get("food2")),
        "ambient": num(status.get("ambient")),
        "fan_pct": num(status.get("fan_pct")),
        "servo_pct": num(status.get("servo_pct")),
        "output_pct": num(status.get("output_pct")),
        "lid_open": "true" if (lid and lid > 0) else "false",
        "target_food1": num(targets.get("food1")),
        "target_food2": num(targets.get("food2")),
        "target_ambient": num(targets.get("ambient")),
        "stalled": "true" if extras.get("stalled") else "false",
        "fuel_low": "true" if extras.get("fuel_low") else "false",
        "predicted_done": extras.get("predicted_done"),
        "predicted_done_food1": pdone_by.get("food1"),
        "predicted_done_food2": pdone_by.get("food2"),
        "predicted_done_ambient": pdone_by.get("ambient"),
        "name_pit": names.get("pit") or "Pit",
        "name_food1": names.get("food1") or "Food 1",
        "name_food2": names.get("food2") or "Food 2",
        "name_ambient": names.get("ambient") or "Ambient",
    }


class MqttBridge:
    """Publishes HeaterMeter state to MQTT and accepts setpoint commands.

    *on_setpoint* is called with a float when HA writes the setpoint number.
    *client* may be injected (a paho-mqtt Client or a compatible fake) for tests;
    otherwise one is created lazily from paho.
    """

    def __init__(self, host: str, port: int = 1883,
                 username: Optional[str] = None, password: Optional[str] = None,
                 node_id: str = "hm", base_topic: str = DEFAULT_BASE_TOPIC,
                 discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
                 on_setpoint: Optional[Callable[[float], None]] = None,
                 on_target: Optional[Callable[[str, float], None]] = None,
                 on_name: Optional[Callable[[str, str], None]] = None,
                 unit: str = "F", client=None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.node_id = node_id
        self.base_topic = base_topic
        self.discovery_prefix = discovery_prefix
        self.on_setpoint = on_setpoint
        self.on_target = on_target           # (channel, value) when HA writes a target
        self.on_name = on_name               # (channel, text) when HA renames a probe
        self.unit = unit or "F"
        self._client = client
        self._connected = False
        self.last_error: Optional[str] = None
        self.version: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def state_topic(self) -> str:
        return f"{self.base_topic}/{self.node_id}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.base_topic}/{self.node_id}/availability"

    @property
    def setpoint_command_topic(self) -> str:
        return f"{self.base_topic}/{self.node_id}/setpoint/set"

    def target_command_topic(self, channel: str) -> str:
        return f"{self.base_topic}/{self.node_id}/target/{channel}/set"

    def name_command_topic(self, channel: str) -> str:
        return f"{self.base_topic}/{self.node_id}/name/{channel}/set"

    def _make_client(self):
        import paho.mqtt.client as mqtt  # lazy
        # paho-mqtt 2.x requires an explicit callback API version; bare
        # mqtt.Client() raises on 2.x. Our callbacks already use the v2
        # signatures (reason_code + properties), so request VERSION2 and fall
        # back to the 1.x constructor on older paho.
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            client = mqtt.Client()
        if self.username:
            client.username_pw_set(self.username, self.password)
        # Last will: mark offline if we drop.
        client.will_set(self.availability_topic, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    def connect(self) -> None:
        if self._client is None:
            self._client = self._make_client()
        # Wire callbacks onto whatever client we have, including an injected
        # fake (which skips _make_client). The bridge owns the protocol logic;
        # the client is just transport.
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        # Injected fake clients may not need a real connect.
        connect = getattr(self._client, "connect", None)
        if connect:
            connect(self.host, self.port)
        loop_start = getattr(self._client, "loop_start", None)
        if loop_start:
            loop_start()

    # paho callbacks ------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # rc is a ReasonCode on paho 2.x (has .is_failure) or an int on 1.x
        # (0 == success). A failed connect (e.g. bad auth) still fires this.
        is_failure = getattr(rc, "is_failure", None)
        ok = (not is_failure) if is_failure is not None else (rc == 0)
        self._connected = ok
        if not ok:
            self.last_error = f"broker refused connection ({rc})"
            return
        self.last_error = None
        client.subscribe(self.setpoint_command_topic)
        for channel, _name in _TARGET_PROBES:
            client.subscribe(self.target_command_topic(channel))
        for channel, _name in _NAME_PROBES:
            client.subscribe(self.name_command_topic(channel))
        client.publish(self.availability_topic, "online", retain=True)
        self.publish_discovery()

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode() if isinstance(msg.payload, bytes) else msg.payload
        except Exception:
            return
        if msg.topic == self.setpoint_command_topic and self.on_setpoint:
            try:
                self.on_setpoint(float(payload))
            except (ValueError, TypeError):
                pass
            return
        if self.on_target:
            for channel, _name in _TARGET_PROBES:
                if msg.topic == self.target_command_topic(channel):
                    try:
                        self.on_target(channel, float(payload))
                    except (ValueError, TypeError):
                        pass
                    return
        if self.on_name:
            for channel, _name in _NAME_PROBES:
                if msg.topic == self.name_command_topic(channel):
                    self.on_name(channel, str(payload))
                    return

    # publishing ----------------------------------------------------------

    def publish_discovery(self) -> None:
        for topic, cfg in discovery_configs(self.node_id, self.version,
                                            self.base_topic, self.discovery_prefix,
                                            unit=self.unit):
            self._client.publish(topic, json.dumps(cfg), retain=True)

    def publish_state(self, status: dict, version: Optional[str] = None,
                      targets: Optional[dict] = None,
                      unit: Optional[str] = None,
                      extras: Optional[dict] = None,
                      names: Optional[dict] = None) -> None:
        republish = False
        if version and version != self.version:
            self.version = version
            republish = True   # sw_version updates once we learn it
        if unit and unit != self.unit:
            self.unit = unit
            republish = True   # entity units follow the board's unit
        if republish and self._connected:
            self.publish_discovery()
        if self._client is None:
            return
        self._client.publish(
            self.state_topic,
            json.dumps(state_payload(status, targets, extras, names)))

    def publish_availability(self, online: bool) -> None:
        if self._client is None:
            return
        self._client.publish(self.availability_topic,
                             "online" if online else "offline", retain=True)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self.publish_availability(False)
            loop_stop = getattr(self._client, "loop_stop", None)
            if loop_stop:
                loop_stop()
            disconnect = getattr(self._client, "disconnect", None)
            if disconnect:
                disconnect()
        except Exception:
            pass


def test_connection(host: str, port: int = 1883, username: Optional[str] = None,
                    password: Optional[str] = None, timeout: float = 5.0) -> dict:
    """Try connecting to a broker with the given credentials and report the
    result. Blocking (waits up to *timeout*); run it off the event loop with
    asyncio.to_thread. Returns {"ok": bool, "error": str|None}. Used by the
    config UI's "Test connection" button before saving."""
    import time
    import paho.mqtt.client as mqtt
    if not host:
        return {"ok": False, "error": "no broker host"}
    try:
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            c = mqtt.Client()
        if username:
            c.username_pw_set(username, password)
        res = {"ok": False, "error": "timed out connecting"}

        def on_connect(cl, u, f, rc, props=None):
            is_failure = getattr(rc, "is_failure", None)
            ok = (not is_failure) if is_failure is not None else (rc == 0)
            res["ok"] = ok
            res["error"] = None if ok else f"broker refused connection ({rc})"

        c.on_connect = on_connect
        c.connect(host, int(port), int(timeout))
        c.loop_start()
        waited = 0.0
        while waited < timeout and res["error"] == "timed out connecting":
            time.sleep(0.1)
            waited += 0.1
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}
