"""The service: glue between a transport link, the state model, the store, and
any number of live subscribers (WebSocket clients).

Lines arrive from the link on the event loop thread (the link schedules them
with ``call_soon_threadsafe``), so :meth:`_on_line` runs on the loop and may
safely touch state, the store, and subscriber queues.

Also owns cook-session lifecycle: a session auto-starts when status data begins
flowing and auto-closes after an idle gap, so every cook becomes a named,
searchable record (FireBoard's "Sessions" idea).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Callable, Optional

from . import auth, cookdone, firmware, fuel, guided, hostupdate, probewatch, protocol
from .cookdone import CookDoneDetector
from .fuel import FuelMonitor
from .guided import GuidedRun
from .probewatch import ProbeWatch
from .state import HeaterMeterState

# A session auto-closes after this many seconds with no status data.
SESSION_IDLE_GAP = 30 * 60

# Sentinel pushed into every subscriber queue on shutdown. A WebSocket reader
# blocks on ``await q.get()`` indefinitely; without this it never unblocks, so
# uvicorn waits the full graceful-shutdown window before killing the process,
# and the daemon keeps holding the serial port (~90s) — which blocks a restart
# or a firmware flash from re-acquiring it. The sentinel lets each reader exit
# and close its socket immediately. (Identity-compared, so it is never confused
# with a real JSON message.)
WS_SHUTDOWN = object()


class HeaterMeterService:
    def __init__(self, link, store, time_fn: Callable[[], float] = time.time,
                 idle_gap: float = SESSION_IDLE_GAP) -> None:
        self.link = link
        self.store = store
        self.time_fn = time_fn
        self.idle_gap = idle_gap
        self.state = HeaterMeterState()
        self.subscribers: set[asyncio.Queue] = set()
        self.loop = None
        self.bad_checksums = 0  # count of rejected corrupted lines (diagnostic)
        self.session_id: Optional[int] = None
        self._last_sample_ts: Optional[float] = None
        # Notification sink: a callable(event: dict) set by the app layer.
        self.on_event: Optional[Callable[[dict], None]] = None
        self._alarm_state: dict = {}  # probe index -> "low"/"high" currently ringing
        # Optional MQTT/Home Assistant bridge (built by _start_mqtt()).
        self.mqtt = None
        # Path to the persisted MQTT config JSON (set by main); None disables
        # persistence (e.g. --sim with in-memory db).
        self.mqtt_config_path: Optional[str] = None
        # Default MQTT config from env/CLI (set by main); used only when no
        # mqtt.json file exists yet.
        self._mqtt_env_default: Optional[dict] = None
        # Last-known bridge status for the config UI.
        self.mqtt_status: dict = {"connected": False, "last_error": None}
        # Push notifications (ntfy). Config persisted to data/notify.json.
        self.notify_config_path: Optional[str] = None
        self.display_config_path: Optional[str] = None
        self._display_applied = False
        # Meater-style automatic cook completion (see cookdone.py).
        self.cookdone_config_path: Optional[str] = None
        self._cookdone = CookDoneDetector()
        # Probe health + stall watcher (see probewatch.py). Emits disconnect /
        # reconnect / fault / stall events the read path turns into push + WS.
        self.probewatch_config_path: Optional[str] = None
        self._probewatch = ProbeWatch()
        # Per-channel health surfaced in /api/status for a "disconnected" badge.
        self.probe_health: dict = {}
        # Auto timeline events: edge-detector state for lid + setpoint markers.
        self._lid_open_prev = False
        self._setpoint_prev: Optional[float] = None
        # Active guided cook (one at a time). See guided.py.
        self.guided: Optional[GuidedRun] = None
        self._guided_keep_warm = False
        # Charcoal/fuel monitor (blower-effort trend). See fuel.py.
        self._fuel = FuelMonitor()
        # Cooker tuning profiles (set by main): profiles.json next to the DB.
        self.profiles_path: Optional[str] = None
        # Latest cached predictions per channel (fed by _check_eta_push).
        self.last_predictions: dict = {}
        # Which named preset is configured on each probe (the board only reports
        # type+coeffs, not the preset name), so the UI can show the actual probe.
        self.probe_presets_path: Optional[str] = None
        # Sample-history retention config (bounds DB growth over time).
        self.storage_config_path: Optional[str] = None
        # Optional single-password auth (off by default).
        self.auth_config_path: Optional[str] = None
        # Per-alarm notify bookkeeping for debounce + repeat: key -> {since,last}.
        self._alarm_notify: dict = {}
        # "Almost done" ETA push bookkeeping: channels already notified this run,
        # plus a throttle timestamp so we only predict periodically.
        self._eta_notified: set = set()
        self._last_eta_check: float = 0.0
        # True while we've flagged the board as "gone dark" (no data).
        self._device_dark = False
        # Auto-tune session (None when idle). See heatermeterd.autotune.
        self.tuner: Optional["AutoTuneSession"] = None
        # Running cook program (None when idle). See heatermeterd.cookprogram.
        self.program: Optional["CookProgramRunner"] = None
        # Host network info pushed to the LCD's Net Info screen.
        self.host_ip: Optional[str] = None
        self.host_hostname: Optional[str] = None
        # In-software firmware updater (set by main): paths + live job state.
        self.firmware_dir: Optional[str] = None        # data/firmware
        self.firmware_spool: Optional[str] = None       # data/firmware/spool (IPC)
        self.firmware_manifest_path: Optional[str] = None  # trusted root-owned
        self.firmware_job: Optional[dict] = None        # active job, or None
        self.firmware_status: dict = {"state": "idle"}  # surfaced via API + WS
        self._fw_config_snapshot: Optional[dict] = None
        self._fw_progress_consumed = 0                  # progress lines read
        self._fw_started_ts = 0.0
        self._fw_last_backup_hex: Optional[str] = None  # for one-click rollback
        # Timing knobs (overridable in tests so the flow runs deterministically).
        self._fw_poll_interval = 0.5
        self._fw_timeout = 180.0
        self._fw_resume_delay = 1.5
        self._fw_verify_delay = 6.0
        self._fw_restore_delay = 4.0
        self._fw_idle_delay = 3.0
        self._fw_send_gap = 0.12
        # In-software host-app updater (paths set by main). The daemon downloads
        # + verifies a release, then the root helper deploy/hm-update swaps the
        # code + pre-built frontend and restarts the service. Host-agnostic: the
        # release channel is whatever manifest URL the operator configures.
        self.hostupdate_dir: Optional[str] = None       # data/hostupdate
        self.hostupdate_spool: Optional[str] = None      # data/hostupdate/spool (IPC)
        self.hostupdate_staging: Optional[str] = None    # data/hostupdate/staging
        self.hostupdate_config_path: Optional[str] = None
        self.install_root: Optional[str] = None          # repo root to update
        self.hostupdate_job: Optional[dict] = None
        self.hostupdate_status: dict = {"state": "idle"}
        self._hu_available: Optional[dict] = None         # cached last check
        self._hu_last_check = 0.0
        self._hu_progress_consumed = 0
        self._hu_started_ts = 0.0
        self._hu_poll_interval = 0.5
        self._hu_timeout = 240.0
        self._shutdown_grace = 2.0   # delay before the poweroff request is written

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        # Resume an open session if the daemon restarted mid-cook within the gap.
        existing = self.store.open_session()
        if existing:
            self.session_id = existing["id"]
        # Apply the saved cook-completion config; if the resumed session was
        # already completed, suppress a duplicate completion/notification.
        self._cookdone.set_config(self._load_cookdone_file())
        self._probewatch.set_config(self._load_probewatch_file())
        if existing and existing.get("completed_ts"):
            self._cookdone.mark_already_complete()
        self.link.start(self._on_line, self.loop)
        # Ask the board to dump its config so identity, probe names/types, PID
        # constants, fan params and alarms populate without a manual poke. The
        # original linkmeterd re-requested until all segments arrived; we do the
        # same lightweight retry so a single dropped /config reply (the link is
        # noisy) does not leave the UI with empty config forever.
        self._config_attempts = 0
        self.loop.call_later(2.0, self._request_config_retry)
        # Detect the Pi's network info for the LCD Net Info screen, and start a
        # light keepalive that refreshes it and keeps the board considering us
        # online (so the Net Info screen stays reachable instead of "Offline").
        self._refresh_host_info(announce=False)
        self.loop.call_later(60.0, self._host_keepalive)
        # Optional MQTT / Home Assistant bridge. Built from the persisted config
        # (mqtt.json) if present, else the env/CLI defaults. Safe no-op if MQTT
        # is disabled or unconfigured.
        self._start_mqtt()
        # Watchdog that pushes a "device went dark" alert if the board stops
        # reporting (serial died / board crashed) during a cook.
        self.loop.call_later(15.0, self._dark_watchdog)
        # Daily storage maintenance (prune/downsample old samples per config).
        self.loop.call_later(3600.0, self._db_maintenance)
        # If we were just restarted by a host-update, surface its outcome so the
        # UI can show "Updated to vX" / the error once it reconnects.
        self._load_hostupdate_boot_result()

    # -- MQTT / Home Assistant config -------------------------------------

    @staticmethod
    def _default_mqtt_config() -> dict:
        return {"enabled": False, "host": "", "port": 1883,
                "username": "", "password": "", "node_id": "hm"}

    def _load_mqtt_file(self) -> Optional[dict]:
        if not self.mqtt_config_path or not os.path.exists(self.mqtt_config_path):
            return None
        try:
            with open(self.mqtt_config_path) as f:
                data = json.load(f)
            return {**self._default_mqtt_config(), **data}
        except Exception:
            return None

    def mqtt_effective_config(self) -> dict:
        """The config in force: the saved file wins; else the env/CLI default;
        else a disabled default."""
        cfg = self._load_mqtt_file()
        if cfg is not None:
            return cfg
        if self._mqtt_env_default:
            return {**self._default_mqtt_config(), **self._mqtt_env_default}
        return self._default_mqtt_config()

    def save_mqtt_file(self, cfg: dict) -> None:
        if not self.mqtt_config_path:
            return
        os.makedirs(os.path.dirname(self.mqtt_config_path) or ".", exist_ok=True)
        with open(self.mqtt_config_path, "w") as f:
            json.dump(cfg, f)
        try:
            os.chmod(self.mqtt_config_path, 0o600)  # password lives here
        except OSError:
            pass

    def _build_and_connect_bridge(self, cfg: dict) -> None:
        # Tear down any existing bridge first.
        if self.mqtt is not None:
            try:
                self.mqtt.close()
            except Exception:
                pass
            self.mqtt = None
        self.mqtt_status = {"connected": False, "last_error": None}
        if not (cfg.get("enabled") and cfg.get("host")):
            return  # disabled or unconfigured: leave self.mqtt None
        from .mqtt import MqttBridge
        bridge = MqttBridge(
            host=cfg["host"], port=int(cfg.get("port", 1883)),
            username=(cfg.get("username") or None),
            password=(cfg.get("password") or None),
            node_id=(cfg.get("node_id") or "hm"),
            on_setpoint=self.mqtt_set_setpoint,
            on_target=self.mqtt_set_target,
            unit=(self.state.pid.get("units") or "F"))
        self.mqtt = bridge
        try:
            bridge.connect()  # connection completes async; status updates later
        except Exception as e:
            self.mqtt_status["last_error"] = str(e)

    def _start_mqtt(self) -> None:
        """Bring MQTT up on start. If a bridge was injected directly (tests, or
        an external embedder), just connect it; otherwise build one from the
        effective config (mqtt.json or env/CLI defaults)."""
        try:
            if self.mqtt is not None:
                try:
                    self.mqtt.connect()
                except Exception as e:
                    self.mqtt_status["last_error"] = str(e)
                return
            self._build_and_connect_bridge(self.mqtt_effective_config())
        except Exception:
            pass  # a broker hiccup must never stop the controller

    def reconfigure_mqtt(self, cfg: dict) -> dict:
        """Persist a new MQTT config and reconnect the bridge live."""
        self.save_mqtt_file(cfg)
        self._build_and_connect_bridge(cfg)
        return self.mqtt_status_public()

    def mqtt_status_public(self) -> dict:
        """Config for the UI: never includes the password (only whether one is
        set), plus the live connection status."""
        cfg = self.mqtt_effective_config()
        connected = bool(self.mqtt and getattr(self.mqtt, "connected", False))
        err = self.mqtt_status.get("last_error")
        if self.mqtt is not None and getattr(self.mqtt, "last_error", None):
            err = self.mqtt.last_error
        return {
            "enabled": bool(cfg.get("enabled")),
            "host": cfg.get("host", ""),
            "port": int(cfg.get("port", 1883)),
            "username": cfg.get("username", ""),
            "node_id": cfg.get("node_id", "hm"),
            "has_password": bool(cfg.get("password")),
            "connected": connected,
            "last_error": err,
        }

    # -- Push notifications (ntfy) -----------------------------------------

    def _load_notify_file(self) -> Optional[dict]:
        from . import notify
        if not self.notify_config_path or not os.path.exists(self.notify_config_path):
            return None
        try:
            with open(self.notify_config_path) as f:
                return {**notify.default_config(), **json.load(f)}
        except Exception:
            return None

    def notify_effective_config(self) -> dict:
        from . import notify
        cfg = self._load_notify_file()
        return cfg if cfg is not None else notify.default_config()

    def save_notify_file(self, cfg: dict) -> None:
        if not self.notify_config_path:
            return
        os.makedirs(os.path.dirname(self.notify_config_path) or ".", exist_ok=True)
        with open(self.notify_config_path, "w") as f:
            json.dump(cfg, f)
        try:
            os.chmod(self.notify_config_path, 0o600)  # may hold a token
        except OSError:
            pass

    def reconfigure_notify(self, cfg: dict) -> dict:
        self.save_notify_file(cfg)
        return self.notify_status_public()

    def notify_status_public(self) -> dict:
        cfg = self.notify_effective_config()
        return {
            "enabled": bool(cfg.get("enabled")),
            "server": cfg.get("server", ""),
            "topic": cfg.get("topic", ""),
            "has_token": bool(cfg.get("token")),
            "debounce_sec": int(cfg.get("debounce_sec", 30)),
            "repeat_min": int(cfg.get("repeat_min", 0)),
            "dark_timeout_sec": int(cfg.get("dark_timeout_sec", 90)),
        }

    def _push(self, title: str, message: str, priority: str = "default",
              tags: str = "") -> None:
        """Fire a push notification off the event loop (best-effort, never raises)."""
        cfg = self.notify_effective_config()
        if not (cfg.get("enabled") and cfg.get("topic")):
            return
        from . import notify
        try:
            if self.loop is not None:
                self.loop.run_in_executor(
                    None, lambda: notify.send(cfg, title, message, priority, tags))
        except Exception:
            pass

    def _dark_watchdog(self) -> None:
        """Periodic: alert once if the board stops reporting for too long."""
        cfg = self.notify_effective_config()
        timeout = int(cfg.get("dark_timeout_sec", 0) or 0)
        if (timeout > 0 and self._last_sample_ts is not None
                and not self._device_dark
                and (self.time_fn() - self._last_sample_ts) > timeout):
            self._device_dark = True
            self._emit({"type": "device_dark", "ts": self.time_fn()})
            self._push("HeaterMeter offline",
                       "No data from the controller - check the board and serial link.",
                       priority="high", tags="rotating_light")
        if self.loop is not None:
            self.loop.call_later(15.0, self._dark_watchdog)

    def _request_config_safe(self) -> None:
        try:
            self.link.send(protocol.request_config())
        except Exception:
            pass

    def _request_config_retry(self) -> None:
        """Send /config, and keep retrying every few seconds until the board's
        config has been received (an $HMFN seen) or we give up after a cap."""
        if self.state.config_received:
            self._apply_display_config()   # push host-side display prefs once linked
            return
        self._request_config_safe()
        self._config_attempts += 1
        if self._config_attempts < 10:  # ~10 tries over ~40s, then stop
            self.loop.call_later(4.0, self._request_config_retry)

    # -- display prefs (home-screen probe rotation interval) --------------
    def get_home_rotate(self) -> int:
        secs = 5
        try:
            if self.display_config_path and os.path.exists(self.display_config_path):
                with open(self.display_config_path) as f:
                    secs = int(json.load(f).get("rotate_secs", 5))
        except Exception:
            secs = 5
        return min(60, max(1, secs))

    def set_home_rotate(self, seconds: int) -> dict:
        secs = min(60, max(1, int(seconds)))
        if self.display_config_path:
            os.makedirs(os.path.dirname(self.display_config_path) or ".", exist_ok=True)
            with open(self.display_config_path, "w") as f:
                json.dump({"rotate_secs": secs}, f)
        try:
            self.link.send(protocol.set_home_rotate(secs))
        except Exception:
            pass
        return {"rotate_secs": secs}

    # -- cook completion (Meater-style) -----------------------------------

    def _load_cookdone_file(self) -> dict:
        if self.cookdone_config_path and os.path.exists(self.cookdone_config_path):
            try:
                with open(self.cookdone_config_path) as f:
                    return cookdone.sanitize(json.load(f))
            except Exception:
                pass
        return cookdone.sanitize({})

    def get_cookdone(self) -> dict:
        """The live effective config (always current, with or without a file)."""
        return dict(self._cookdone.cfg)

    def save_cookdone(self, cfg: dict) -> dict:
        clean = cookdone.sanitize(cfg)
        if self.cookdone_config_path:
            os.makedirs(os.path.dirname(self.cookdone_config_path) or ".",
                        exist_ok=True)
            with open(self.cookdone_config_path, "w") as f:
                json.dump(clean, f)
        self._cookdone.set_config(clean)
        return clean

    # -- probe preset selection -------------------------------------------

    def get_probe_presets_sel(self) -> dict:
        """Map of probe index (str) -> the named preset last applied via the UI
        ("__off" for disabled). The board only reports type+coeffs, so this lets
        the UI show which actual probe model is configured."""
        if self.probe_presets_path and os.path.exists(self.probe_presets_path):
            try:
                with open(self.probe_presets_path) as f:
                    return {str(k): v for k, v in json.load(f).items()}
            except Exception:
                pass
        return {}

    def set_probe_preset_sel(self, index: int, key: str) -> None:
        sel = self.get_probe_presets_sel()
        sel[str(index)] = key
        if not self.probe_presets_path:
            return
        try:
            os.makedirs(os.path.dirname(self.probe_presets_path) or ".",
                        exist_ok=True)
            with open(self.probe_presets_path, "w") as f:
                json.dump(sel, f)
        except Exception:
            pass

    def _check_cook_done(self, ts: float, session_id: Optional[int]) -> None:
        if session_id is None or not self._cookdone.cfg.get("enabled"):
            return
        al = self.state.alarms or []
        targets = {}
        for probe in cookdone.FOOD_PROBES:
            idx = probe * 2 + 1
            raw = al[idx] if idx < len(al) else None
            try:
                targets[probe] = float(str(raw).rstrip("LH"))
            except (TypeError, ValueError):
                targets[probe] = None
        temps = {p: self._probe_temp(p) for p in cookdone.FOOD_PROBES}
        ambient = self._probe_temp(3)
        res = self._cookdone.update(ts, temps, targets, ambient)
        names = self.state.probe_names or ["Pit", "Food 1", "Food 2", "Ambient"]
        for ev in res.get("events", []):
            if ev["event"] == "done":
                p = ev["probe"]
                nm = names[p] if p < len(names) else f"Probe {p}"
                self._emit({"type": "cook_probe_done", "probe": p,
                            "probe_name": nm, "ts": ts})
                self._record_event(ts, "probe_done",
                                   channel=("pit", "food1", "food2", "ambient")[p],
                                   label=f"{nm} done (probe removed)")
        if res.get("completed"):
            self._on_cook_complete(session_id, res.get("done_at") or ts)

    # -- probe health + stall watching ------------------------------------

    def _load_probewatch_file(self) -> dict:
        if self.probewatch_config_path and os.path.exists(self.probewatch_config_path):
            try:
                with open(self.probewatch_config_path) as f:
                    return probewatch.sanitize(json.load(f))
            except Exception:
                pass
        return probewatch.sanitize({})

    def get_probewatch(self) -> dict:
        return dict(self._probewatch.cfg)

    def save_probewatch(self, cfg: dict) -> dict:
        clean = probewatch.sanitize(cfg)
        if self.probewatch_config_path:
            try:
                os.makedirs(os.path.dirname(self.probewatch_config_path) or ".",
                            exist_ok=True)
                with open(self.probewatch_config_path, "w") as f:
                    json.dump(clean, f)
            except Exception:
                pass
        self._probewatch.set_config(clean)
        return clean

    def _check_probe_health(self, ts: float) -> None:
        cfg = self._probewatch.cfg
        if not (cfg.get("enabled") or cfg.get("stall_enabled")):
            return
        sd = self.state.status.to_dict()
        temps = {"pit": sd.get("pit"), "food1": sd.get("food1"),
                 "food2": sd.get("food2"), "ambient": sd.get("ambient")}
        al = self.state.alarms or []

        def _has_target(idx):
            raw = al[idx] if idx < len(al) else None
            try:
                return float(str(raw).rstrip("LH")) >= 0
            except (TypeError, ValueError):
                return False

        targets = {"pit": False, "food1": _has_target(3),
                   "food2": _has_target(5), "ambient": _has_target(7)}
        for ev in self._probewatch.update(
                ts, temps, pid_mode=sd.get("pid_mode"), targets=targets):
            self._on_probe_event(ev)

    def _on_probe_event(self, ev: dict) -> None:
        typ, ch, sev = ev["type"], ev["channel"], ev.get("severity", "info")
        if typ == "disconnect":
            self.probe_health[ch] = "disconnected"
        elif typ == "reconnect":
            self.probe_health[ch] = "ok"
        elif typ == "fault":
            self.probe_health[ch] = "fault"
        # Broadcast to the UI (toast + dashboard badge) regardless of severity.
        # Outer type stays "probe_event"; the specific kind (disconnect/reconnect/
        # fault/stall_*) is carried in "kind" so the spread can't clobber it.
        self._emit({**ev, "type": "probe_event", "kind": ev["type"]})
        # Push only the actionable ones, so end-of-cook / unused-probe noise stays
        # in-app: warnings + criticals always; a stall is a low-priority heads-up.
        msg = ev.get("message", "")
        if sev == "critical":
            self._push("Probe disconnected", msg, priority="high",
                       tags="rotating_light")
        elif sev == "warning":
            title = "Sensor problem" if typ == "fault" else "Probe disconnected"
            self._push(title, msg, priority="default", tags="warning")
        elif typ == "stall_start":
            self._push("Stall", msg, priority="low", tags="hourglass_flowing_sand")
        # Health + stall edges all land on the timeline too.
        if typ in ("disconnect", "reconnect", "fault", "stall_start", "stall_end"):
            self._record_event(ev.get("ts") or self.time_fn(), typ,
                               channel=ch, label=ev.get("message", ""),
                               value=ev.get("value"))

    # -- auto timeline events ----------------------------------------------

    def _record_event(self, ts: float, kind: str, channel: Optional[str] = None,
                      label: Optional[str] = None,
                      value: Optional[float] = None) -> None:
        """Persist a timeline event for the current session and broadcast it so
        open graphs add the marker live. Best-effort, never raises."""
        try:
            self.store.add_event(ts, kind, session_id=self.session_id,
                                 channel=channel, label=label, value=value)
        except Exception:
            pass
        self._emit({"type": "timeline", "kind": kind, "ts": ts,
                    "channel": channel, "label": label, "value": value})

    def _check_timeline_edges(self, ts: float) -> None:
        """Edge-detect lid-open/closed and setpoint changes from the status."""
        st = self.state.status
        # Lid: the firmware counts lid_countdown down while the lid-open logic
        # is active; 0 means normal control.
        lid_open = bool(st.lid_countdown and st.lid_countdown > 0)
        if lid_open and not self._lid_open_prev:
            self._record_event(ts, "lid_open", label="Lid open")
        elif self._lid_open_prev and not lid_open:
            self._record_event(ts, "lid_closed", label="Lid closed")
        self._lid_open_prev = lid_open
        # Setpoint: record changes (>= 1 degree) after the first observation, so
        # connecting to an already-running board doesn't log a spurious marker.
        sp = st.set_point
        if sp is not None:
            prev = self._setpoint_prev
            if prev is not None and abs(sp - prev) >= 1.0:
                self._record_event(ts, "setpoint", label=f"Set {round(sp)}°",
                                   value=float(sp))
            self._setpoint_prev = float(sp)

    # -- guided cooks --------------------------------------------------------

    _GUIDED_CH_PROBE = {"food1": 1, "food2": 2, "ambient": 3}

    def start_guided_cook(self, key: str, channel: str,
                          auto_keep_warm: bool = False) -> dict:
        """Start a guided cook: configure the pit + probe, then coach via
        milestone prompts. One guided cook at a time."""
        cook = guided.find_cook(key)
        if cook is None:
            return {"ok": False, "error": f"Unknown guided cook {key!r}."}
        probe = self._GUIDED_CH_PROBE.get(channel)
        if probe is None:
            return {"ok": False,
                    "error": "Channel must be food1, food2, or ambient."}
        if self.guided is not None and not self.guided.done:
            return {"ok": False,
                    "error": f"A guided cook ({self.guided.cook['label']}) is "
                             "already running. Stop it first."}
        ts = self.time_fn()
        # Configure the board: pit setpoint, probe name, food target (the high
        # alarm; None entries keep every other threshold untouched).
        try:
            self.link.send(protocol.set_setpoint(int(cook["pit_setpoint"])))
            self.link.send(protocol.set_probe_name(probe, cook["probe_name"]))
            thresholds: list = [None] * 8
            thresholds[probe * 2 + 1] = cook["food_target"]
            self.link.send(protocol.set_alarms(thresholds))
        except Exception as e:
            return {"ok": False, "error": f"Could not configure the board: {e}"}
        self.guided = GuidedRun(cook, channel, ts)
        self._guided_keep_warm = bool(auto_keep_warm)
        self._record_event(ts, "guided",
                           label=f"Guided cook started: {cook['label']}")
        self._emit({"type": "guided", "event": "started",
                    "guided": self.guided.status()})
        try:
            self.link.send(protocol.toast("Guided cook", cook["label"][:16]))
        except Exception:
            pass
        return {"ok": True, "guided": self.guided.status()}

    def stop_guided_cook(self) -> dict:
        if self.guided is None:
            return {"ok": False, "error": "No guided cook is running."}
        label = self.guided.cook["label"]
        self.guided = None
        self._record_event(self.time_fn(), "guided",
                           label=f"Guided cook stopped: {label}")
        self._emit({"type": "guided", "event": "stopped"})
        return {"ok": True}

    def confirm_guided_wrap(self) -> dict:
        """The user wrapped the meat (closed-loop acknowledgement)."""
        run = self.guided
        if run is None:
            return {"ok": False, "error": "No guided cook is running."}
        if not run.confirm_wrap():
            return {"ok": False, "error": "Nothing is waiting on a wrap."}
        ts = self.time_fn()
        temp = self._probe_temp(self._GUIDED_CH_PROBE[run.channel])
        self._record_event(ts, "wrap", channel=run.channel,
                           label=f"Wrapped at {round(temp) if temp else '?'}°",
                           value=temp)
        self._emit({"type": "guided", "event": "wrapped",
                    "guided": run.status()})
        return {"ok": True, "guided": run.status()}

    def guided_status(self) -> Optional[dict]:
        return self.guided.status() if self.guided else None

    def _drive_guided(self, ts: float) -> None:
        run = self.guided
        if run is None or run.done:
            return
        probe = self._GUIDED_CH_PROBE[run.channel]
        temp = self._probe_temp(probe)
        stalled = False
        try:
            chs = self._probewatch._ch.get(run.channel)
            stalled = bool(chs and chs.stalled)
        except Exception:
            pass
        for m in run.update(ts, temp, stalled=stalled):
            prompt = m["prompt"]
            # The pull milestone carries the rest clock.
            if run.done and run.cook.get("rest_secs"):
                ready = ts + run.cook["rest_secs"]
                prompt += (" Ready to eat around "
                           + time.strftime("%I:%M %p",
                                           time.localtime(ready)).lstrip("0")
                           + ".")
            self._emit({"type": "guided", "event": "prompt",
                        "milestone": m["key"], "prompt": prompt,
                        "guided": run.status()})
            self._record_event(ts, "guided", channel=run.channel,
                               label=f"{run.cook['label']}: {m['key']}",
                               value=temp)
            self._push(run.cook["label"], prompt, priority="default",
                       tags="fork_and_knife")
            try:
                self.link.send(protocol.toast(run.cook["label"][:16],
                                              m["key"][:16]))
            except Exception:
                pass
        if run.done and self._guided_keep_warm:
            # Closed-loop: drop the pit to keep-warm the moment the food hits
            # its target (uses the cook-completion keep-warm temperature).
            kw = self._cookdone.cfg.get("keep_warm_temp", 150)
            try:
                self.link.send(protocol.set_setpoint(int(kw)))
            except Exception:
                pass
            self._record_event(ts, "setpoint",
                               label=f"Keep-warm {int(kw)}° (guided)",
                               value=float(kw))

    # -- cook insights + repeat ----------------------------------------------

    def repeat_cook(self, session_id: int) -> dict:
        """Re-apply a past cook's setup: its dominant setpoint plus any food
        targets it reached (inferred from the timeline's target events)."""
        s = self.store.get_session(session_id)
        if not s:
            return {"ok": False, "error": "Unknown cook."}
        sp = self.store.session_setpoint(session_id)
        if sp is None:
            return {"ok": False,
                    "error": "That cook has no recorded setpoint to repeat."}
        thresholds: list = [None] * 8
        targets = {}
        ch_probe = {"food1": 1, "food2": 2, "ambient": 3}
        for ev in self.store.list_events(session_id=session_id):
            if ev.get("kind") == "target" and ev.get("channel") in ch_probe \
                    and ev.get("value") is not None:
                targets[ev["channel"]] = ev["value"]
        for ch, val in targets.items():
            thresholds[ch_probe[ch] * 2 + 1] = round(float(val))
        try:
            self.link.send(protocol.set_setpoint(int(round(sp))))
            if targets:
                self.link.send(protocol.set_alarms(thresholds))
        except Exception as e:
            return {"ok": False, "error": f"Could not configure the board: {e}"}
        self._record_event(self.time_fn(), "guided",
                           label=f"Repeating cook: {s.get('name') or session_id}")
        return {"ok": True, "setpoint": round(sp),
                "targets": {k: round(float(v)) for k, v in targets.items()}}

    def cook_insights(self) -> dict:
        """Aggregate learning across completed cooks: how long your cooks run,
        how long your stalls last, totals. Cheap queries over sessions+events."""
        sessions = [s for s in self.store.list_sessions()
                    if s.get("ended_ts") or s.get("completed_ts")]
        durations = [s["ended_ts"] - s["started_ts"] for s in sessions
                     if s.get("ended_ts") and s.get("started_ts")
                     and s["ended_ts"] > s["started_ts"]]
        # Pair stall_start -> stall_end per session+channel for stall lengths.
        stalls = []
        for s in sessions:
            evs = self.store.list_events(session_id=s["id"])
            open_stall = {}
            for ev in evs:
                if ev["kind"] == "stall_start":
                    open_stall[ev.get("channel")] = ev["ts"]
                elif ev["kind"] == "stall_end" and ev.get("channel") in open_stall:
                    stalls.append(ev["ts"] - open_stall.pop(ev["channel"]))
        def _avg(xs):
            return (sum(xs) / len(xs)) if xs else None
        return {
            "cooks": len(sessions),
            "completed": sum(1 for s in sessions if s.get("completed_ts")),
            "avg_duration_secs": _avg(durations),
            "longest_secs": max(durations) if durations else None,
            "stalls_seen": len(stalls),
            "avg_stall_secs": _avg(stalls),
        }

    # -- cooker profiles -----------------------------------------------------
    #
    # A profile is a named snapshot of the control tuning for one grill (PID
    # constants + fan/servo params + lid detect), stored host-side in
    # profiles.json so switching cookers is one click instead of re-tuning.

    def _load_profiles(self) -> dict:
        d = self._load_json_file(self.profiles_path) if self.profiles_path else None
        if not isinstance(d, dict) or not isinstance(d.get("profiles"), list):
            return {"profiles": [], "active": None}
        return d

    def _save_profiles(self, d: dict) -> None:
        if not self.profiles_path:
            return
        try:
            os.makedirs(os.path.dirname(self.profiles_path) or ".", exist_ok=True)
            with open(self.profiles_path, "w") as f:
                json.dump(d, f, indent=2)
        except Exception:
            pass

    def get_profiles(self) -> dict:
        return self._load_profiles()

    def save_profile(self, name: str) -> dict:
        """Snapshot the CURRENT live tuning under *name* (replaces same name)."""
        name = str(name or "").strip()[:40]
        if not name:
            return {"ok": False, "error": "Profile name is required."}
        snap = {
            "name": name,
            "pid": dict(self.state.pid),
            "fan": dict(self.state.fan),
            "lid_detect": dict(self.state.lid_detect),
        }
        d = self._load_profiles()
        d["profiles"] = [p for p in d["profiles"] if p.get("name") != name]
        d["profiles"].append(snap)
        d["active"] = name
        self._save_profiles(d)
        return {"ok": True, **self._load_profiles()}

    def apply_profile(self, name: str) -> dict:
        """Send a saved profile's tuning to the board (paced)."""
        d = self._load_profiles()
        prof = next((p for p in d["profiles"] if p.get("name") == name), None)
        if prof is None:
            return {"ok": False, "error": f"No profile named {name!r}."}
        cmds = []
        pid = prof.get("pid") or {}
        for k in ("b", "p", "i", "d"):
            if pid.get(k) not in (None, ""):
                cmds.append(protocol.set_pid(k, pid[k]))
        fan = prof.get("fan") or {}
        if any(v not in (None, "") for v in fan.values()):
            cmds.append(protocol.set_fan(
                fan.get("low"), fan.get("high"), fan.get("servo_min"),
                fan.get("servo_max"), fan.get("flags"), fan.get("max_startup"),
                fan.get("fan_active_floor"), fan.get("servo_active_ceil")))
        lid = prof.get("lid_detect") or {}
        if lid.get("offset_percent") is not None or lid.get("duration") is not None:
            cmds.append(protocol.set_lid_detect(
                lid.get("offset_percent"), lid.get("duration")))
        if not cmds:
            return {"ok": False, "error": "That profile has nothing to apply."}
        self._fw_send_sequence(cmds)
        d["active"] = name
        self._save_profiles(d)
        self._record_event(self.time_fn(), "profile",
                           label=f"Cooker profile: {name}")
        return {"ok": True, **self._load_profiles()}

    def delete_profile(self, name: str) -> dict:
        d = self._load_profiles()
        before = len(d["profiles"])
        d["profiles"] = [p for p in d["profiles"] if p.get("name") != name]
        if len(d["profiles"]) == before:
            return {"ok": False, "error": f"No profile named {name!r}."}
        if d.get("active") == name:
            d["active"] = None
        self._save_profiles(d)
        return {"ok": True, **self._load_profiles()}

    def _check_fuel(self, ts: float) -> None:
        st = self.state.status
        for ev in self._fuel.update(ts, st.fan_pct, st.pit, st.set_point,
                                    lid_open=bool(st.lid_countdown)):
            self._emit({"type": "fuel", **{k: v for k, v in ev.items()
                                           if k != "type"},
                        "kind": ev["type"]})
            if ev["type"] == "fuel_low":
                self._record_event(ts, "fuel_low", label="Fuel running low",
                                   value=ev.get("duty"))
                self._push("Add fuel", ev["message"], priority="high",
                           tags="fire")
                try:
                    self.link.send(protocol.toast("Add fuel", "Blower at limit"))
                except Exception:
                    pass
            else:
                self._record_event(ts, "fuel_ok", label="Fuel recovered",
                                   value=ev.get("duty"))

    def fuel_status(self) -> dict:
        return self._fuel.status()

    def _mqtt_extras(self, ts: float) -> dict:
        """Cook-intelligence fields for the HA state payload: any-channel stall,
        fuel-low, and the soonest fresh predicted-done time (ISO-8601)."""
        stalled = False
        try:
            stalled = any(c.stalled for c in self._probewatch._ch.values())
        except Exception:
            pass
        done_at = None
        for p in self.last_predictions.values():
            # Only trust predictions refreshed within the last 2 minutes.
            if p.get("done_at") and (ts - p.get("ts", 0)) <= 120:
                done_at = p["done_at"] if done_at is None else min(done_at,
                                                                   p["done_at"])
        iso = None
        if done_at is not None:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(done_at))
            # strftime %z gives +HHMM; HA's timestamp class wants +HH:MM.
            if len(iso) >= 5 and iso[-5] in "+-":
                iso = iso[:-2] + ":" + iso[-2:]
        return {"stalled": stalled, "fuel_low": self._fuel.alerted,
                "predicted_done": iso}

    def send_lcd_message(self, line1: str, line2: str = "") -> dict:
        """Show a transient message on the board's LCD (the /set?tt toast).

        The LCD is 16x2 and the firmware splits the toast payload on commas, so
        commas are stripped and each line capped at 16 chars."""
        def _clean(s):
            return str(s or "").replace(",", " ").replace("\n", " ").strip()[:16]
        l1, l2 = _clean(line1), _clean(line2)
        if not l1:
            return {"ok": False, "error": "Message text is required."}
        try:
            self.link.send(protocol.toast(l1, l2 or None))
        except Exception as e:
            return {"ok": False, "error": f"Could not send: {e}"}
        return {"ok": True, "line1": l1, "line2": l2}

    def shutdown_system(self, dryrun: bool = False) -> dict:
        """Gracefully end the cook, then power off the Pi via the root helper.

        Idles the cooker (so the blower stops) and closes the current session so
        it lands cleanly in Past Cooks, then writes a poweroff request that the
        root hm-power helper acts on. *dryrun* validates the chain without
        actually shutting down."""
        if not self.firmware_spool:
            return {"ok": False,
                    "error": "System shutdown is not configured on this host."}
        try:
            self.link.send(protocol.set_setpoint("O", unit=""))   # fire off
        except Exception:
            pass
        if self.session_id is not None:
            try:
                self.store.close_session(self.session_id, self.time_fn())
            except Exception:
                pass
            self.session_id = None
        self._emit({"type": "system_shutdown", "ts": self.time_fn(),
                    "dryrun": dryrun})

        def _trigger():
            try:
                path = os.path.join(self.firmware_spool, "poweroff.request")
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    f.write("dryrun" if dryrun else "now")
                os.replace(tmp, path)
            except Exception:
                pass

        # Short grace so the idle command reaches the board and the HTTP
        # response is delivered before the Pi goes down.
        if self.loop is not None:
            self.loop.call_later(self._shutdown_grace, _trigger)
        else:
            _trigger()
        return {"ok": True, "dryrun": dryrun}

    def finish_cook(self) -> dict:
        """Manually finish the current cook (the explicit Finish Cook button)."""
        if self.session_id is None:
            return {"ok": False, "error": "No active cook to finish."}
        # Suppress the auto-detector so it does not also fire for this session.
        self._cookdone.mark_already_complete()
        self._on_cook_complete(self.session_id, self.time_fn(), reason="manual")
        return {"ok": True, "session_id": self.session_id}

    def _on_cook_complete(self, session_id: int, done_at: float,
                          reason: str = "probe removed") -> None:
        try:
            self.store.mark_completed(session_id, done_at, reason)
        except Exception:
            pass
        self._emit({"type": "cook_complete", "session_id": session_id,
                    "ts": done_at, "reason": reason})
        self._record_event(done_at, "cook_complete",
                           label="Cook complete"
                                 + (" (manual)" if reason == "manual" else ""))
        msg = ("You finished this cook." if reason == "manual"
               else "Your cook is done. The probe has been out of the food for "
                    "a few minutes.")
        self._push("Cook complete", msg, priority="high",
                   tags="white_check_mark")
        # Flash it on the LCD too, for anyone standing at the cooker.
        try:
            self.link.send(protocol.toast("Cook complete", "Food is ready"))
        except Exception:
            pass
        # Stop probe-health watching for this finished cook so pulling the probes
        # afterwards does not fire "disconnected" alerts.
        self._probewatch.reset()
        self.probe_health = {}
        action = self._cookdone.cfg.get("on_complete", "notify")
        try:
            if action == "shutdown":
                self.link.send(protocol.set_setpoint("O", unit=""))
            elif action == "keep_warm":
                self.link.send(protocol.set_setpoint(
                    self._cookdone.cfg.get("keep_warm_temp", 150)))
        except Exception:
            pass

    def _apply_display_config(self) -> None:
        """Re-send display prefs to the board (called on connect so they survive
        a board reboot)."""
        if self._display_applied:
            return
        self._display_applied = True
        try:
            self.link.send(protocol.set_home_rotate(self.get_home_rotate()))
        except Exception:
            pass

    # -- in-software firmware updater -------------------------------------
    #
    # The daemon (unprivileged) never flashes directly. It writes a request
    # file into a group-writable spool; a systemd .path unit then runs the
    # root-owned helper (deploy/hm-flash) which toggles SPI, sha-verifies the
    # image against its own trusted manifest, backs up, flashes, and restores
    # SPI. The daemon pauses serial during the flash, tails the helper's
    # progress, and on completion resumes serial, re-pulls config, and (when
    # the image resets EEPROM) restores the saved configuration to the board.

    def _fw_configured(self) -> bool:
        # Require the trusted manifest to exist so the UI hides the Firmware card
        # until install.sh has placed the manifest + helper + units on the Pi.
        return bool(self.firmware_spool and self.firmware_manifest_path
                    and os.path.exists(self.firmware_manifest_path))

    def firmware_listing(self) -> dict:
        """The data the Firmware UI needs: current board version plus the
        manifest images, each flagged whether it matches the running board."""
        out = {
            "current": self.state.version,
            "current_clean": firmware.clean_version(self.state.version),
            "configured": self._fw_configured(),
            "images": [],
            "status": self.firmware_status,
        }
        if not self._fw_configured():
            return out
        try:
            manifest = firmware.load_manifest(self.firmware_manifest_path)
        except firmware.ManifestError as e:
            out["error"] = str(e)
            return out
        for img in manifest.get("images", []):
            out["images"].append({
                "version": img.get("version"),
                "changelog": img.get("changelog", ""),
                "eeprom_reset": bool(img.get("eeprom_reset", False)),
                "board_rev": img.get("board_rev"),
                "min_compat": img.get("min_compat"),
                "installed": firmware.versions_match(
                    img.get("version", ""), self.state.version),
            })
        return out

    def _snapshot_config(self) -> dict:
        """Capture the live config so it can be restored after an EEPROM-reset
        flash. Probe0 carries the pit's thermocouple type (3 = AD8495)."""
        st = self.state
        return {
            "units": (st.pid.get("units") or "F"),
            "probe_coeffs": {int(k): dict(v) for k, v in st.probe_coeffs.items()},
            "probe_names": list(st.probe_names),
            "probe_offsets": list(st.probe_offsets),
            "alarms": list(st.alarms),
            "pid": dict(st.pid),
            "fan": dict(st.fan),
            "display": dict(st.display),
            "lid_detect": dict(st.lid_detect),
            "home_rotate": self.get_home_rotate(),
        }

    def start_firmware_flash(self, version: str, action: str = "flash") -> dict:
        """Begin a flash. Returns ``{"ok": True, "job_id": ...}`` or
        ``{"ok": False, "error": "..."}``. The actual flashing happens in the
        root helper; this only guards, snapshots, pauses serial, and triggers
        the helper by writing the request file."""
        if self.firmware_job is not None:
            return {"ok": False, "error": "A firmware update is already running."}
        if not self._fw_configured():
            return {"ok": False,
                    "error": "Firmware updating is not configured on this host."}
        guard = firmware.preflight_guard(
            self.state.status.to_dict(),
            tuner_running=self.tuner is not None,
            program_running=(self.program is not None
                             and not getattr(self.program.state, "done", True)),
        )
        if guard:
            return {"ok": False, "error": guard}

        try:
            manifest = firmware.load_manifest(self.firmware_manifest_path)
        except firmware.ManifestError as e:
            return {"ok": False, "error": f"Firmware manifest problem: {e}"}
        img = firmware.find_image(manifest, version)
        if action != "rollback" and img is None:
            return {"ok": False, "error": f"Unknown firmware version {version!r}."}

        rollback_hex = None
        if action == "rollback":
            rollback_hex = self._fw_last_backup_hex
            if not rollback_hex or not os.path.exists(rollback_hex):
                return {"ok": False,
                        "error": "No backup is available to roll back to."}

        eeprom_reset = bool(img.get("eeprom_reset", False)) if img else False
        job_id = f"{int(self.time_fn())}-{secrets.token_hex(4)}"
        self._fw_config_snapshot = self._snapshot_config()

        try:
            req = firmware.build_request(
                job_id, version, action=action, eeprom_reset=eeprom_reset,
                rollback_hex=rollback_hex)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        try:
            os.makedirs(self.firmware_spool, exist_ok=True)
        except OSError:
            pass
        # Idle the cooker before flashing so a mid-flash reset can't drive a fire.
        self._fw_safe_send(protocol.set_setpoint("O", unit=""))
        # Stop ingesting serial: the flash resets the board and emits garbage.
        try:
            self.link.pause()
        except Exception:
            pass

        self.firmware_job = {"job_id": job_id, "version": version,
                             "action": action, "eeprom_reset": eeprom_reset}
        self._fw_progress_consumed = 0
        self._fw_started_ts = self.time_fn()
        self.firmware_status = {
            "state": "flashing", "job_id": job_id, "version": version,
            "action": action, "eeprom_reset": eeprom_reset, "steps": [],
            "message": "", "read_version": "",
        }
        self._broadcast_firmware()
        # Writing request.json is the trigger (systemd .path watches it). Write
        # atomically so the helper never sees a partial file.
        self._write_json_atomic(
            os.path.join(self.firmware_spool, "request.json"), req)
        if self.loop is not None:
            self.loop.call_later(self._fw_poll_interval, self._fw_poll)
        return {"ok": True, "job_id": job_id}

    def _fw_poll(self) -> None:
        job = self.firmware_job
        if job is None:
            return
        job_id = job["job_id"]
        new_steps = self._fw_read_progress(
            os.path.join(self.firmware_spool, f"{job_id}.progress.jsonl"))
        if new_steps:
            self.firmware_status.setdefault("steps", []).extend(new_steps)
            self._broadcast_firmware()
        result = self._fw_read_result(
            os.path.join(self.firmware_spool, f"{job_id}.result.json"))
        if result is not None and result.get("job_id") == job_id:
            self._fw_finish(job, result)
            return
        if (self.time_fn() - self._fw_started_ts) > self._fw_timeout:
            self._fw_finish(job, {
                "job_id": job_id, "status": "error",
                "message": ("The flash helper did not finish in time. If the "
                            "display is blank, power-cycle the controller.")})
            return
        if self.loop is not None:
            self.loop.call_later(self._fw_poll_interval, self._fw_poll)

    def _fw_finish(self, job: dict, result: dict) -> None:
        job_id = job["job_id"]
        # Resume serial and force a fresh config pull from the rebooted board,
        # no matter whether the flash succeeded.
        try:
            self.link.resume(self._on_line, self.loop)
        except Exception:
            pass
        self.state.config_received = False
        self._display_applied = False
        self._config_attempts = 0
        if self.loop is not None:
            self.loop.call_later(self._fw_resume_delay, self._request_config_retry)
            self.loop.call_later(
                self._fw_resume_delay,
                lambda: self._fw_safe_send(protocol.request_version()))

        ok = (result.get("status") == "ok")
        action = job.get("action")

        if ok and action == "flash":
            bk = os.path.join(self.firmware_spool, f"{job_id}-backup-flash.hex")
            if os.path.exists(bk):
                self._fw_last_backup_hex = bk

        if ok and action != "backup":
            # Always restore config after a flash, not only when the manifest
            # flags an EEPROM reset: whether the magic actually changed depends
            # on the version transition (e.g. a downgrade/rollback always resets
            # it), and re-sending the saved config is idempotent when it did
            # not. The restore sequence ends by idling the cooker, which also
            # handles the board resuming its stored setpoint after the reset.
            if self.loop is not None:
                self.loop.call_later(self._fw_restore_delay, self._fw_restore_config)
        elif ok and self.loop is not None:
            # A backup (dry run) does not flash, but the programmer still resets
            # the board, which resumes its stored setpoint on reboot. Idle it.
            self.loop.call_later(
                self._fw_idle_delay,
                lambda: self._fw_safe_send(protocol.set_setpoint("O", unit="")))

        self.firmware_status["read_version"] = result.get("read_version") or ""
        self.firmware_status["message"] = result.get("message") or ""
        self.firmware_status["state"] = "success" if ok else "error"
        self.firmware_status["job_id"] = job_id
        self.firmware_job = None
        self._broadcast_firmware()

        if self.loop is not None and ok and action != "backup":
            self.loop.call_later(self._fw_verify_delay,
                                 lambda: self._fw_verify_version(job))

    def _fw_verify_version(self, job: dict) -> None:
        target = job.get("version")
        board = self.state.version
        if board:
            self.firmware_status["read_version"] = board
        if job.get("action") == "rollback":
            self.firmware_status["verified"] = bool(board)
        else:
            ok = firmware.versions_match(target, board)
            self.firmware_status["verified"] = ok
            if not ok and board:
                self.firmware_status["message"] = (
                    f"Flashed, but the board reports {board}. It may still be "
                    "rebooting; refresh in a moment.")
        self._broadcast_firmware()

    def _build_restore_commands(self, snap: dict, idle: bool = True) -> list:
        """Build the ordered command list that restores a saved config to the
        board (after an EEPROM-reset flash, or from a config backup). Units are
        set first so the absolute values that follow are interpreted correctly;
        probe coefficients go next, with probe0 carrying its thermocouple type
        (3 = AD8495) so the pit reads correctly. When *idle*, the cooker is idled
        last (used post-flash); a plain config restore leaves the setpoint."""
        cmds = []
        u = snap.get("units")
        if u in ("F", "C"):
            cmds.append(protocol.set_units(u))
        for idx in sorted(snap.get("probe_coeffs", {})):
            c = snap["probe_coeffs"][idx]
            cmds.append(protocol.set_probe_coeffs(
                idx, c.get("a"), c.get("b"), c.get("c"), c.get("r"),
                c.get("type")))
        for i, name in enumerate(snap.get("probe_names", [])[:4]):
            cmds.append(protocol.set_probe_name(i, name))
        offs = snap.get("probe_offsets")
        if offs and any(str(o) not in ("", "None") for o in offs):
            cmds.append(protocol.set_probe_offsets(offs))
        pid = snap.get("pid") or {}
        for k in ("b", "p", "i", "d"):
            if pid.get(k) not in (None, ""):
                cmds.append(protocol.set_pid(k, pid[k]))
        fan = snap.get("fan") or {}
        if any(v not in (None, "") for v in fan.values()):
            cmds.append(protocol.set_fan(
                fan.get("low"), fan.get("high"), fan.get("servo_min"),
                fan.get("servo_max"), fan.get("flags"), fan.get("max_startup"),
                fan.get("fan_active_floor"), fan.get("servo_active_ceil")))
        al = snap.get("alarms")
        if al:
            cleaned = [str(x).rstrip("LH") for x in al]
            cmds.append(protocol.set_alarms(cleaned))
        disp = snap.get("display") or {}
        if disp.get("backlight") is not None or disp.get("home_mode") is not None:
            cmds.append(protocol.set_lcd(
                disp.get("backlight"), disp.get("home_mode"),
                disp.get("leds") or None))
        lid = snap.get("lid_detect") or {}
        if lid.get("offset_percent") is not None or lid.get("duration") is not None:
            cmds.append(protocol.set_lid_detect(
                lid.get("offset_percent"), lid.get("duration")))
        cmds.append(protocol.set_home_rotate(snap.get("home_rotate", 5)))
        if idle:
            cmds.append(protocol.set_setpoint("O", unit=""))  # idle, last
        return cmds

    def _fw_restore_config(self) -> None:
        snap = self._fw_config_snapshot
        if not snap:
            self._fw_safe_send(protocol.set_setpoint("O", unit=""))
            return
        self._fw_send_sequence(self._build_restore_commands(snap))

    def _fw_send_sequence(self, cmds: list) -> None:
        """Send a list of command lines spaced out so the board keeps up."""
        if not cmds:
            return
        self._fw_safe_send(cmds[0])
        rest = cmds[1:]
        if rest and self.loop is not None:
            self.loop.call_later(self._fw_send_gap,
                                 lambda: self._fw_send_sequence(rest))

    def _fw_safe_send(self, line: str) -> None:
        try:
            self.link.send(line)
        except Exception:
            pass

    def _broadcast_firmware(self) -> None:
        self._broadcast({"event": {"type": "firmware", **self.firmware_status}})

    def _write_json_atomic(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def _fw_read_progress(self, path: str) -> list:
        """Return progress dicts not yet consumed. Only whole (newline
        terminated) lines are taken, so a mid-write tail is never mis-parsed."""
        try:
            with open(path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return []
        except Exception:
            return []
        complete = content.split("\n")[:-1]  # drop trailing partial/empty
        new = complete[self._fw_progress_consumed:]
        self._fw_progress_consumed = len(complete)
        out = []
        for ln in new:
            d = firmware.parse_progress_line(ln)
            if d:
                out.append(d)
        return out

    def _fw_read_result(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            return None  # mid-write; retry next poll
        return firmware.parse_result(d)

    # -- in-software host-app updater -------------------------------------
    #
    # Pulls a new build of this Python+Svelte app from a configurable release
    # channel (the manifest URL the operator sets), verifies it by SHA-256, and
    # applies it via the root helper deploy/hm-update which swaps the daemon code
    # + pre-built frontend and restarts the service. The daemon only downloads,
    # verifies, and writes a request; the helper restarts it, so on success this
    # process is replaced and the terminal result is surfaced on the next boot
    # from a stable result file. Host-AGNOSTIC: nothing here assumes any host.

    @staticmethod
    def _default_hostupdate_config() -> dict:
        return {"manifest_url": "", "auto_check": False}

    def _app_version(self) -> str:
        # The running app version (single source of truth in api.py). Lazy import
        # avoids a service<->api import cycle.
        try:
            from .api import APP_VERSION
            return APP_VERSION
        except Exception:
            return ""

    def get_host_update_config(self) -> dict:
        d = self._default_hostupdate_config()
        cfg = self._load_json_file(self.hostupdate_config_path)
        if isinstance(cfg, dict):
            d["manifest_url"] = str(cfg.get("manifest_url", "") or "").strip()
            d["auto_check"] = bool(cfg.get("auto_check", False))
        return d

    def save_host_update_config(self, cfg: dict) -> dict:
        url = str((cfg or {}).get("manifest_url", "") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            return {"ok": False,
                    "error": "Manifest URL must start with http:// or https://"}
        d = {"manifest_url": url,
             "auto_check": bool((cfg or {}).get("auto_check", False))}
        if self.hostupdate_config_path:
            try:
                os.makedirs(os.path.dirname(self.hostupdate_config_path) or ".",
                            exist_ok=True)
                with open(self.hostupdate_config_path, "w") as f:
                    json.dump(d, f)
            except Exception:
                pass
        return {"ok": True, **d}

    def _hu_configured(self) -> bool:
        return bool(self.hostupdate_spool
                    and self.get_host_update_config()["manifest_url"])

    def host_update_listing(self) -> dict:
        """The data the Host Software card needs: running version, whether a
        channel is configured, the saved config, the cached availability from
        the last check, and the live job status."""
        return {
            "current": self._app_version(),
            "configured": self._hu_configured(),
            "config": self.get_host_update_config(),
            "available": self._hu_available,
            "last_check": self._hu_last_check,
            "status": self.hostupdate_status,
        }

    @staticmethod
    def _http_get_text(url: str, timeout: float) -> str:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "heatermeterd"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")

    @staticmethod
    def _http_download(url: str, dest: str, timeout: float) -> None:
        import shutil
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "heatermeterd"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 65536)

    async def check_host_update(self) -> dict:
        """Fetch the configured manifest and report whether a newer build is
        offered. Caches the result so the UI can render it without re-fetching."""
        url = self.get_host_update_config()["manifest_url"]
        if not url:
            return {"ok": False, "error": "No manifest URL is configured."}
        loop = self.loop or asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, self._http_get_text, url, 20.0)
            manifest = hostupdate.parse_manifest(text)
        except hostupdate.ManifestError as e:
            return {"ok": False, "error": f"Manifest problem: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"Could not fetch the manifest: {e}"}
        cur = self._app_version()
        avail = manifest.get("version")
        info = {
            "version": avail,
            "changelog": manifest.get("changelog", ""),
            "url": manifest.get("url"),
            "sha256": str(manifest.get("sha256", "")).lower(),
            "update_available": hostupdate.is_newer(cur, avail),
        }
        self._hu_available = info
        self._hu_last_check = self.time_fn()
        return {"ok": True, "current": cur, **info}

    async def start_host_update(self, version: Optional[str] = None,
                                action: str = "update") -> dict:
        """Begin a host-app update: guard, fetch the manifest fresh, download +
        verify the artifact, then write the request the root helper acts on.
        On success the helper restarts this service, so the caller should expect
        the connection to drop; the outcome reappears after reconnect."""
        if self.hostupdate_job is not None:
            return {"ok": False, "error": "A software update is already running."}
        if action == "update" and not self._hu_configured():
            return {"ok": False,
                    "error": "Software updating is not configured on this host."}
        guard = hostupdate.preflight_guard(
            self.state.status.to_dict(),
            tuner_running=self.tuner is not None,
            program_running=(self.program is not None
                             and not getattr(self.program.state, "done", True)),
        )
        if guard:
            return {"ok": False, "error": guard}

        job_id = f"{int(self.time_fn())}-{secrets.token_hex(4)}"
        tarball = sha = None
        ver = version or ""
        if action == "update":
            chk = await self.check_host_update()
            if not chk.get("ok"):
                return chk
            avail = chk.get("version")
            if version and version != avail:
                return {"ok": False,
                        "error": f"Version {version!r} is not the build offered "
                                 f"({avail!r}). Re-check for updates."}
            ver = avail
            if not chk.get("update_available") and version is None:
                return {"ok": False, "error": "Already up to date."}
            url, sha = chk.get("url"), chk.get("sha256")
            try:
                os.makedirs(self.hostupdate_staging, exist_ok=True)
                tarball = os.path.join(self.hostupdate_staging, f"{job_id}.tar.gz")
                self.hostupdate_status = {
                    "state": "downloading", "job_id": job_id, "version": ver,
                    "action": action, "steps": [], "message": ""}
                self._broadcast_hostupdate()
                loop = self.loop or asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, self._http_download, url, tarball, 120.0)
            except Exception as e:
                self.hostupdate_status = {"state": "idle"}
                return {"ok": False, "error": f"Download failed: {e}"}
            if not hostupdate.verify_artifact(tarball, sha):
                try:
                    os.remove(tarball)
                except OSError:
                    pass
                self.hostupdate_status = {"state": "idle"}
                return {"ok": False,
                        "error": "The downloaded update failed its integrity "
                                 "check (sha256 mismatch). Nothing was changed."}

        try:
            req = hostupdate.build_request(
                job_id, ver, action=action, tarball=tarball, sha256=sha,
                install_root=self.install_root)
        except ValueError as e:
            self.hostupdate_status = {"state": "idle"}
            return {"ok": False, "error": str(e)}

        try:
            os.makedirs(self.hostupdate_spool, exist_ok=True)
        except OSError:
            pass
        # Clear any stale progress/result so the poll + boot surfacing only ever
        # reflect this job (the result file is stable across jobs by design).
        for fn in ("hostupdate.progress.jsonl", "hostupdate.result.json"):
            try:
                os.remove(os.path.join(self.hostupdate_spool, fn))
            except OSError:
                pass
        self.hostupdate_job = {"job_id": job_id, "version": ver, "action": action}
        self._hu_progress_consumed = 0
        self._hu_started_ts = self.time_fn()
        self.hostupdate_status = {
            "state": "applying", "job_id": job_id, "version": ver,
            "action": action, "steps": [], "message": ""}
        self._broadcast_hostupdate()
        # Writing request.json is the trigger (systemd .path watches it).
        self._write_json_atomic(
            os.path.join(self.hostupdate_spool, "request.json"), req)
        if self.loop is not None:
            self.loop.call_later(self._hu_poll_interval, self._hu_poll)
        return {"ok": True, "job_id": job_id}

    def _hu_poll(self) -> None:
        job = self.hostupdate_job
        if job is None:
            return
        job_id = job["job_id"]
        new_steps = self._hu_read_progress(
            os.path.join(self.hostupdate_spool, "hostupdate.progress.jsonl"))
        if new_steps:
            self.hostupdate_status.setdefault("steps", []).extend(new_steps)
            self._broadcast_hostupdate()
        result = self._hu_read_result(
            os.path.join(self.hostupdate_spool, "hostupdate.result.json"))
        if result is not None and result.get("job_id") == job_id:
            self._hu_finish(job, result)
            return
        if (self.time_fn() - self._hu_started_ts) > self._hu_timeout:
            self._hu_finish(job, {
                "job_id": job_id, "status": "error",
                "message": ("The update helper did not finish in time. The "
                            "service may still be restarting; refresh shortly.")})
            return
        if self.loop is not None:
            self.loop.call_later(self._hu_poll_interval, self._hu_poll)

    def _hu_finish(self, job: dict, result: dict) -> None:
        # On success the helper restarts the daemon, so this often does not run;
        # the boot-result path then surfaces the outcome. On a pre-restart error
        # (helper refused / failed) this runs and shows the message.
        ok = (result.get("status") == "ok")
        self.hostupdate_status["state"] = "success" if ok else "error"
        self.hostupdate_status["message"] = result.get("message") or ""
        self.hostupdate_status["version"] = (
            result.get("version") or job.get("version"))
        self.hostupdate_status["job_id"] = job["job_id"]
        self.hostupdate_job = None
        self._broadcast_hostupdate()

    def _load_hostupdate_boot_result(self) -> None:
        if not self.hostupdate_spool:
            return
        d = self._hu_read_result(
            os.path.join(self.hostupdate_spool, "hostupdate.result.json"))
        if not d or not d.get("status"):
            return
        ok = (d.get("status") == "ok")
        self.hostupdate_status = {
            "state": "success" if ok else "error",
            "job_id": d.get("job_id"),
            "version": d.get("version") or self._app_version(),
            "action": d.get("action") or "update",
            "message": d.get("message") or "",
            "steps": [],
            "current": self._app_version(),
        }

    def ack_host_update(self) -> dict:
        """Dismiss a finished update result so it stops showing after reconnect."""
        if self.hostupdate_spool:
            try:
                os.remove(
                    os.path.join(self.hostupdate_spool, "hostupdate.result.json"))
            except OSError:
                pass
        self.hostupdate_status = {"state": "idle"}
        self._broadcast_hostupdate()
        return {"ok": True}

    def _broadcast_hostupdate(self) -> None:
        self._broadcast({"event": {"type": "hostupdate", **self.hostupdate_status}})

    def _hu_read_progress(self, path: str) -> list:
        try:
            with open(path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return []
        except Exception:
            return []
        complete = content.split("\n")[:-1]  # drop trailing partial/empty
        new = complete[self._hu_progress_consumed:]
        self._hu_progress_consumed = len(complete)
        out = []
        for ln in new:
            d = hostupdate.parse_progress_line(ln)
            if d:
                out.append(d)
        return out

    def _hu_read_result(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            return None  # mid-write; retry next poll
        return hostupdate.parse_result(d)

    # -- host network info (LCD Net Info screen) ---------------------------

    def _refresh_host_info(self, announce: bool = True) -> None:
        """Re-detect the Pi's IP + hostname. If *announce* and the IP changed,
        flash it on the LCD as a toast (matches the original ipwatch behaviour)."""
        from . import hostinteractive
        new_ip = hostinteractive.detect_ip()
        self.host_hostname = hostinteractive.get_hostname()
        if announce and new_ip and new_ip != self.host_ip:
            try:
                self.link.send(protocol.toast("Network Address", new_ip))
            except Exception:
                pass
        if new_ip:
            self.host_ip = new_ip

    def _host_keepalive(self) -> None:
        """Periodic: refresh host info (announce IP changes) and nudge the board
        so it keeps us 'online' and the Net Info screen stays reachable. Any
        valid command line promotes the board OFFLINE->ONLINE; /ucid is cheap."""
        self._refresh_host_info(announce=True)
        try:
            self.link.send(protocol.request_version())
        except Exception:
            pass
        if self.loop is not None:
            self.loop.call_later(60.0, self._host_keepalive)

    def _handle_host_interactive(self, sentence) -> None:
        """Reply to a $HMHI request. Wire order: HMHI,<opaque>,<topic>,<button>."""
        from . import hostinteractive
        f = sentence.fields
        try:
            opaque = int(f[0]) if len(f) > 0 else 0
            topic = int(f[1]) if len(f) > 1 else 0
            button = int(f[2]) if len(f) > 2 else 0
        except (ValueError, TypeError):
            return
        if topic != hostinteractive.TOPIC_NETINFO:
            return  # only Net Info is implemented
        new_opaque, line1, line2 = hostinteractive.netinfo_screen(
            opaque, button, self.host_ip, self.host_hostname)
        try:
            self.link.send(protocol.host_interactive_reply(new_opaque, line1, line2))
        except Exception:
            pass

    def mqtt_set_setpoint(self, value: float) -> None:
        """Setpoint command from MQTT/Home Assistant. Called on the MQTT client
        thread, so hop onto the event loop before touching the link."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: self.link.send(protocol.set_setpoint(value)))

    # Probe index of each MQTT-exposed food target (its high alarm slot).
    _MQTT_TARGET_PROBE = {"food1": 1, "food2": 2, "ambient": 3}

    def mqtt_set_target(self, channel: str, value: float) -> None:
        """Probe-target command from Home Assistant: set that probe's high alarm
        (its cook target). Runs on the MQTT thread, so hop to the loop."""
        probe = self._MQTT_TARGET_PROBE.get(channel)
        if probe is None or self.loop is None:
            return
        al = list(self.state.alarms or [])
        while len(al) < 8:
            al.append("")
        # Preserve the other thresholds (strip any ringing L/H suffix; '' keeps).
        thresholds = []
        for raw in al[:8]:
            s = str(raw).rstrip("LH")
            thresholds.append(s if s not in ("", "None") else None)
        thresholds[probe * 2 + 1] = value
        self.loop.call_soon_threadsafe(
            lambda: self.link.send(protocol.set_alarms(thresholds)))

    def set_units(self, new_unit: str) -> dict:
        """Switch the board's temperature unit (F/C). The firmware does not
        convert stored values, so convert and re-send the setpoint (absolute),
        alarm thresholds (absolute) and probe offsets (deltas) in the new unit.
        If the cooker is off we only change the unit, so we don't accidentally
        turn it on."""
        new_unit = (new_unit or "F").upper()
        if new_unit not in ("F", "C"):
            return {"ok": False, "error": "Unit must be F or C."}
        old = (self.state.pid.get("units") or "F").upper()
        if old == new_unit:
            return {"ok": True, "unit": new_unit, "changed": False}

        def conv_abs(v):
            return (v - 32) * 5.0 / 9.0 if new_unit == "C" else v * 9.0 / 5.0 + 32

        def conv_delta(v):
            return v * 5.0 / 9.0 if new_unit == "C" else v * 9.0 / 5.0

        cmds = []
        # Setpoint: re-send converted only if actively driving; else unit-only.
        sp = self.state.status.set_point
        mode = self.state.status.pid_mode
        if isinstance(sp, (int, float)) and sp > 0 and mode not in (4, None):
            cmds.append(protocol.set_setpoint(round(conv_abs(sp)), new_unit))
        else:
            cmds.append(protocol.set_units(new_unit))
        # Alarm thresholds (absolute); -1 disabled stays -1, blank keeps.
        al = self.state.alarms or []
        if al:
            out = []
            for raw in (list(al) + [""] * 8)[:8]:
                s = str(raw).rstrip("LH")
                try:
                    v = float(s)
                except (ValueError, TypeError):
                    out.append(None)
                    continue
                out.append(round(conv_abs(v), 1) if v >= 0 else int(v))
            cmds.append(protocol.set_alarms(out))
        # Probe offsets (temperature deltas).
        offs = self.state.probe_offsets or []
        if any(str(o) not in ("", "None", "0", "0.0") for o in offs):
            out = []
            for o in (list(offs) + [""] * 4)[:4]:
                try:
                    out.append(round(conv_delta(float(o)), 1))
                except (ValueError, TypeError):
                    out.append(None)
            cmds.append(protocol.set_probe_offsets(out))
        cmds.append(protocol.request_config())  # re-read so state reflects it

        if self.loop is not None:
            self._fw_send_sequence(cmds)   # spaced, to avoid command merges
        else:
            for c in cmds:
                self._fw_safe_send(c)
        return {"ok": True, "unit": new_unit, "changed": True}

    # -- storage retention -------------------------------------------------

    def get_storage_config(self) -> dict:
        d = {"retention_days": 0, "downsample_days": 0}
        cfg = self._load_json_file(self.storage_config_path)
        if isinstance(cfg, dict):
            try:
                d["retention_days"] = max(0, int(cfg.get("retention_days", 0)))
                d["downsample_days"] = max(0, int(cfg.get("downsample_days", 0)))
            except (TypeError, ValueError):
                pass
        return d

    def save_storage_config(self, cfg: dict) -> dict:
        d = {
            "retention_days": max(0, min(3650, int(cfg.get("retention_days", 0) or 0))),
            "downsample_days": max(0, min(3650, int(cfg.get("downsample_days", 0) or 0))),
        }
        if self.storage_config_path:
            try:
                os.makedirs(os.path.dirname(self.storage_config_path) or ".",
                            exist_ok=True)
                with open(self.storage_config_path, "w") as f:
                    json.dump(d, f)
            except Exception:
                pass
        return d

    def db_stats(self) -> dict:
        s = self.store.db_stats()
        s.update(self.get_storage_config())
        return s

    def cleanup_db(self, retention_days=None, downsample_days=None) -> dict:
        """Downsample old samples and/or prune very old ones, then vacuum. With
        no args, uses the saved retention config. Safe to run in a worker thread
        (the store connection is check_same_thread=False + lock-guarded)."""
        cfg = self.get_storage_config()
        rd = cfg["retention_days"] if retention_days is None else max(0, int(retention_days))
        dd = cfg["downsample_days"] if downsample_days is None else max(0, int(downsample_days))
        now = self.time_fn()
        thinned = pruned = 0
        if dd > 0:
            thinned = self.store.downsample_before(now - dd * 86400, 60)
        if rd > 0:
            pruned = self.store.prune_samples_before(now - rd * 86400)
        if pruned or thinned:
            try:
                self.store.vacuum()
            except Exception:
                pass
        stats = self.db_stats()
        stats["pruned"] = pruned
        stats["downsampled"] = thinned
        return stats

    def _db_maintenance(self) -> None:
        cfg = self.get_storage_config()
        if (cfg["retention_days"] > 0 or cfg["downsample_days"] > 0) \
                and self.loop is not None:
            self.loop.run_in_executor(None, self.cleanup_db)
        if self.loop is not None:
            self.loop.call_later(24 * 3600, self._db_maintenance)

    # -- optional auth -----------------------------------------------------

    def auth_config(self) -> dict:
        cfg = self._load_json_file(self.auth_config_path) or {}
        return {
            "enabled": bool(cfg.get("enabled")),
            "salt": cfg.get("salt", ""),
            "password_hash": cfg.get("password_hash", ""),
            "secret": cfg.get("secret", ""),
        }

    def _save_auth(self, cfg: dict) -> None:
        if not self.auth_config_path:
            return
        try:
            os.makedirs(os.path.dirname(self.auth_config_path) or ".", exist_ok=True)
            with open(self.auth_config_path, "w") as f:
                json.dump(cfg, f)
            os.chmod(self.auth_config_path, 0o600)   # holds the password hash + secret
        except Exception:
            pass

    def auth_status(self) -> dict:
        """Public (no secrets): whether auth is on."""
        return {"enabled": self.auth_config()["enabled"]}

    def auth_valid(self, token) -> bool:
        cfg = self.auth_config()
        if not cfg["enabled"]:
            return True
        return auth.valid_token(token or "", cfg["secret"])

    def auth_login(self, password: str) -> dict:
        cfg = self.auth_config()
        if not cfg["enabled"]:
            return {"ok": True, "token": None}
        if auth.verify_password(password or "", cfg["salt"], cfg["password_hash"]):
            return {"ok": True, "token": auth.make_token(cfg["secret"])}
        return {"ok": False, "error": "Incorrect password."}

    def auth_set_password(self, new_password: str, current_password=None) -> dict:
        cfg = self.auth_config()
        if cfg["enabled"] and not auth.verify_password(
                current_password or "", cfg["salt"], cfg["password_hash"]):
            return {"ok": False, "error": "Current password is incorrect."}
        if not new_password or len(new_password) < 4:
            return {"ok": False, "error": "Password must be at least 4 characters."}
        salt, h = auth.hash_password(new_password)
        secret = cfg["secret"] or auth.new_secret()
        self._save_auth({"enabled": True, "salt": salt,
                         "password_hash": h, "secret": secret})
        return {"ok": True, "token": auth.make_token(secret)}

    def auth_disable(self, current_password=None) -> dict:
        cfg = self.auth_config()
        if cfg["enabled"] and not auth.verify_password(
                current_password or "", cfg["salt"], cfg["password_hash"]):
            return {"ok": False, "error": "Password is incorrect."}
        self._save_auth({"enabled": False})
        return {"ok": True}

    # -- config backup / restore ------------------------------------------

    @staticmethod
    def _load_json_file(path):
        try:
            if path and os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def config_backup(self, include_secrets: bool = False) -> dict:
        """Snapshot the full configuration (board + host files + saved cook
        programs) as one JSON-able dict, for download/migration. Secrets (MQTT
        password, ntfy token) are omitted unless *include_secrets*."""
        host = {}
        mqtt = self._load_json_file(self.mqtt_config_path)
        if mqtt is not None:
            mqtt = dict(mqtt)
            if not include_secrets:
                mqtt.pop("password", None)
            host["mqtt"] = mqtt
        notify = self._load_json_file(self.notify_config_path)
        if notify is not None:
            notify = dict(notify)
            if not include_secrets:
                notify.pop("token", None)
            host["notify"] = notify
        for key, path in (("display", self.display_config_path),
                          ("cookdone", self.cookdone_config_path),
                          ("probe_presets", self.probe_presets_path)):
            v = self._load_json_file(path)
            if v is not None:
                host[key] = v
        try:
            programs = [{"name": p["name"], "stages": p["stages"]}
                        for p in self.store.list_programs()]
        except Exception:
            programs = []
        return {
            "schema": 1,
            "created": self.time_fn(),
            "board_version": self.state.version,
            "board": self._snapshot_config(),
            "host": host,
            "programs": programs,
            "has_secrets": include_secrets,
        }

    def config_restore(self, data: dict) -> dict:
        """Apply a backup produced by config_backup(). Writes host config files,
        reconnects MQTT/notify, re-sends the board config (paced), and adds any
        saved programs not already present."""
        if not isinstance(data, dict) or data.get("schema") != 1:
            return {"ok": False, "error": "Not a valid HeaterMeter backup."}
        applied = []
        host = data.get("host") or {}

        # MQTT / ntfy: reconfigure (persists + reconnects). Merge over the
        # current effective config so an omitted secret keeps the existing one.
        if "mqtt" in host:
            try:
                self.reconfigure_mqtt({**self.mqtt_effective_config(), **host["mqtt"]})
                applied.append("mqtt")
            except Exception:
                pass
        if "notify" in host:
            try:
                self.reconfigure_notify({**self.notify_effective_config(), **host["notify"]})
                applied.append("notify")
            except Exception:
                pass
        # Plain JSON config files.
        for key, path in (("display", self.display_config_path),
                          ("cookdone", self.cookdone_config_path),
                          ("probe_presets", self.probe_presets_path)):
            if key in host and path:
                try:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "w") as f:
                        json.dump(host[key], f)
                    applied.append(key)
                except Exception:
                    pass
        if "cookdone" in host:
            try:
                self._cookdone.set_config(cookdone.sanitize(host["cookdone"]))
            except Exception:
                pass

        # Board config: send paced to avoid command merges; do not idle.
        board = data.get("board")
        if board:
            cmds = self._build_restore_commands(board, idle=False)
            cmds.append(protocol.request_config())
            if self.loop is not None:
                self._fw_send_sequence(cmds)
            else:
                for c in cmds:
                    self._fw_safe_send(c)
            applied.append("board")

        # Saved cook programs (skip names that already exist).
        progs = data.get("programs") or []
        if progs:
            try:
                existing = {p.get("name") for p in self.store.list_programs()}
                added = 0
                for p in progs:
                    if p.get("name") and p["name"] not in existing and p.get("stages"):
                        self.store.save_program(p["name"], p["stages"], self.time_fn())
                        added += 1
                if added:
                    applied.append(f"programs({added})")
            except Exception:
                pass
        return {"ok": True, "applied": applied}

    def _mqtt_targets(self) -> dict:
        """Current food targets (high alarms) for MQTT publishing."""
        al = self.state.alarms or []

        def t(idx):
            try:
                v = float(str(al[idx]).rstrip("LH"))
                return v if v >= 0 else None
            except (IndexError, ValueError, TypeError):
                return None

        return {"food1": t(3), "food2": t(5), "ambient": t(7)}

    async def stop(self) -> None:
        close = getattr(self.link, "close", None)
        if close:
            close()
        if self.mqtt is not None:
            try:
                self.mqtt.close()
            except Exception:
                pass
        # Wake every live WebSocket reader so it unblocks from q.get(), closes
        # its socket, and lets uvicorn shut down promptly (see WS_SHUTDOWN).
        for q in list(self.subscribers):
            try:
                q.put_nowait(WS_SHUTDOWN)
            except asyncio.QueueFull:
                # Make room and retry — the sentinel must get through.
                try:
                    q.get_nowait()
                    q.put_nowait(WS_SHUTDOWN)
                except Exception:
                    pass

    # -- session lifecycle -------------------------------------------------

    def _ensure_session(self, ts: float) -> int:
        """Start a new session if none is open or the idle gap has elapsed."""
        if (self.session_id is not None and self._last_sample_ts is not None
                and (ts - self._last_sample_ts) > self.idle_gap):
            # Long silence: close the stale session and start fresh.
            self.store.close_session(self.session_id, self._last_sample_ts)
            self.session_id = None
        if self.session_id is None:
            self.session_id = self.store.start_session(ts)
            self._cookdone.reset()   # fresh cook-completion tracking per session
            self._probewatch.reset()  # fresh probe-health tracking per cook
            self.probe_health = {}
            self._emit({"type": "session_started", "session_id": self.session_id,
                        "ts": ts})
        return self.session_id

    # -- incoming line (runs on the loop thread) ---------------------------

    def _on_line(self, line: str) -> None:
        ts = self.time_fn()
        sentence = protocol.parse(line)
        if sentence is None:
            return  # not a $-framed sentence (command echo, log noise, etc.)

        # Reject corrupted lines. The firmware always appends an XOR checksum, so
        # a missing or mismatched checksum means the line was garbled in transit
        # (dropped bytes, two sentences merged by a lost newline, serial noise).
        # Ingesting those silently corrupts state. Checksums catch this; honour them.
        if not sentence.checksum_ok:
            self.bad_checksums += 1
            return

        # Host-interactive request: the board is asking us to render an LCD
        # screen (e.g. Net Info) and will show "Offline" if we don't reply within
        # ~800ms. Answer immediately, right here on the read path.
        if sentence.type == "HMHI":
            self._handle_host_interactive(sentence)
            return

        self.state.ingest(sentence, ts=ts)
        if sentence.type == "UCID":
            # hm4+ firmware validates a *XX checksum on inbound commands and
            # drops garbled lines. Enable it as soon as the board identifies.
            try:
                self.link.cmd_checksum = protocol.supports_cmd_checksum(
                    self.state.version)
            except Exception:
                pass
        if sentence.type == "HMSU":
            sid = self._ensure_session(ts)
            try:
                self.store.insert(self.state.status, ts, session_id=sid)
            except Exception:
                pass  # never let a storage hiccup kill the read path
            self._last_sample_ts = ts
            if self._device_dark:
                self._device_dark = False
                self._emit({"type": "device_back", "ts": ts})
                self._push("HeaterMeter back online",
                           "The controller is reporting again.",
                           tags="white_check_mark")
            self._check_alarms(ts)
            self._check_eta_push(ts)
            self._check_cook_done(ts, sid)
            self._check_probe_health(ts)
            self._check_timeline_edges(ts)
            self._drive_guided(ts)
            self._check_fuel(ts)
            # Feed the auto-tuner if one is running (it drives the fan relay).
            if self.tuner is not None:
                try:
                    self.tuner.on_sample(ts, self.state.status.pit)
                except Exception:
                    pass
            # Tick a running cook program (drives setpoint through stages).
            if self.program is not None and not self.program.state.done:
                try:
                    st = self.state.status
                    self.program.on_sample(ts, {
                        "pit": st.pit, "food1": st.food1,
                        "food2": st.food2, "ambient": st.ambient})
                except Exception:
                    pass
            state_d = self.state.to_dict()
            # Service-level live fields the UI renders alongside board state.
            state_d["probe_health"] = self.probe_health
            state_d["fuel"] = self._fuel.status()
            state_d["guided"] = self.guided_status()
            self._broadcast({"ts": ts, "session_id": sid, "state": state_d})
            if self.mqtt is not None:
                try:
                    self.mqtt.publish_state(
                        self.state.status.to_dict(), self.state.version,
                        targets=self._mqtt_targets(),
                        unit=(self.state.pid.get("units") or None),
                        extras=self._mqtt_extras(ts))
                except Exception:
                    pass

    # -- alarms / notifications -------------------------------------------

    def _check_alarms(self, ts: float) -> None:
        """Detect probe alarm edges from the firmware's $HMAL state and emit
        notification events on the rising edge (alarm starts ringing)."""
        # state.alarms is the raw flat list [low0, high0, low1, high1, ...].
        # The firmware suffixes a ringing value with 'L' or 'H'. We surface those.
        labels = ["Pit", "Food 1", "Food 2", "Ambient"]
        names = self.state.probe_names or labels
        al = self.state.alarms or []
        for probe in range(4):
            for half, idx in (("low", probe * 2), ("high", probe * 2 + 1)):
                if idx >= len(al):
                    continue
                raw = str(al[idx])
                ringing = raw.endswith("L") or raw.endswith("H")
                key = f"{probe}:{half}"
                was = self._alarm_state.get(key, False)
                if ringing and not was:
                    self._emit({
                        "type": "alarm",
                        "probe": probe,
                        "probe_name": names[probe] if probe < len(names) else labels[probe],
                        "edge": half,
                        "ts": ts,
                    })
                    nm = names[probe] if probe < len(names) else labels[probe]
                    temp = self._probe_temp(probe)
                    ch = ("pit", "food1", "food2", "ambient")[probe]
                    if half == "high":
                        self._record_event(ts, "target", channel=ch,
                                           label=f"{nm} reached target", value=temp)
                    else:
                        self._record_event(ts, "alarm_low", channel=ch,
                                           label=f"{nm} below low alarm", value=temp)
                self._alarm_state[key] = ringing
                # Away-from-home push with debounce + repeat (separate channel
                # from the WS edge event above, which drives the in-page alert).
                self._maybe_notify_alarm(key, ringing, ts, probe, half, names, labels)

    def _probe_temp(self, probe: int):
        st = self.state.status
        return [st.pit, st.food1, st.food2, st.ambient][probe] if 0 <= probe < 4 else None

    def _maybe_notify_alarm(self, key, ringing, ts, probe, half, names, labels) -> None:
        cfg = self.notify_effective_config()
        if not (cfg.get("enabled") and cfg.get("topic")) or not ringing:
            self._alarm_notify.pop(key, None)
            return
        debounce = int(cfg.get("debounce_sec", 0) or 0)
        repeat = int(cfg.get("repeat_min", 0) or 0) * 60
        rec = self._alarm_notify.get(key)
        if rec is None:
            # Start the debounce timer on the rising edge.
            self._alarm_notify[key] = {"since": ts, "last": None}
            return
        if rec["last"] is None:
            due = (ts - rec["since"]) >= debounce
        else:
            due = repeat > 0 and (ts - rec["last"]) >= repeat
        if not due:
            return
        name = names[probe] if probe < len(names) else labels[probe]
        temp = self._probe_temp(probe)
        tstr = f" ({temp:.0f}°)" if isinstance(temp, (int, float)) else ""
        if half == "high":
            title, msg = (f"{name} reached target{tstr}",
                          f"{name} is at or above its high alarm{tstr}.")
        else:
            title, msg = (f"{name} low alarm{tstr}",
                          f"{name} dropped below its low alarm{tstr}.")
        self._push(title, msg, priority="high", tags="fire")
        rec["last"] = ts

    # Push once when a food probe is predicted to be within this many seconds of
    # its target (the "almost done" heads-up, distinct from the at-target alarm).
    ETA_NOTIFY_SEC = 15 * 60

    def _check_eta_push(self, ts: float) -> None:
        """Predict each targeted food probe's time-to-target and push a one-time
        'almost done' heads-up when it drops within ETA_NOTIFY_SEC. Throttled."""
        cfg = self.notify_effective_config()
        if not (cfg.get("enabled") and cfg.get("topic")):
            return
        if (ts - self._last_eta_check) < 30:   # predict at most every 30s
            return
        self._last_eta_check = ts
        from . import predict
        al = self.state.alarms or []
        names = self.state.probe_names or ["Pit", "Food 1", "Food 2", "Ambient"]
        st = self.state.status
        env = st.pit if isinstance(st.pit, (int, float)) else st.set_point
        window = 900.0
        for probe, channel in ((1, "food1"), (2, "food2"), (3, "ambient")):
            idx = probe * 2 + 1
            raw = al[idx] if idx < len(al) else None
            try:
                target = float(str(raw).rstrip("LH"))
            except (TypeError, ValueError):
                target = None
            if target is None or target < 0:
                self._eta_notified.discard(channel)
                continue
            cur = self._probe_temp(probe)
            if isinstance(cur, (int, float)) and cur >= target:
                self._eta_notified.discard(channel)   # already there; alarm covers it
                continue
            try:
                tss, vals = self.store.recent_series(channel, max(window * 2, 3600), ts)
            except Exception:
                continue
            p = predict.predict(tss, vals, target, env_temp=env, window_seconds=window)
            eta = p.eta_seconds
            # Cache for the MQTT "Predicted Done" sensor (and anything else that
            # wants the latest prediction without recomputing).
            self.last_predictions[channel] = {
                "ts": ts, "eta": eta, "confidence": p.confidence,
                "done_at": (ts + eta) if eta is not None else None,
            }
            if eta is not None and 0 < eta <= self.ETA_NOTIFY_SEC \
                    and p.confidence in ("low", "medium", "high"):
                if channel not in self._eta_notified:
                    self._eta_notified.add(channel)
                    name = names[probe] if probe < len(names) else channel
                    mins = max(1, round(eta / 60))
                    self._push(f"{name} almost done",
                               f"{name} is about {mins} min from {target:.0f}°.",
                               priority="default", tags="hourglass_flowing_sand")
            elif eta is None or eta > self.ETA_NOTIFY_SEC * 1.5:
                # Drifted back from the target - allow a fresh heads-up later.
                self._eta_notified.discard(channel)

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
        # Also push notable events to live clients as a side channel.
        self._broadcast({"event": event})

    # -- broadcast / subscriptions ----------------------------------------

    def _broadcast(self, message: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: drop its oldest message to make room.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except Exception:
                    pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    # -- auto-tune ---------------------------------------------------------

    def start_autotune(self, setpoint: float, rule: str = "tyreus_luyben",
                       relay_high: float = 100.0, relay_low: float = 0.0,
                       hysteresis: float = 1.0, max_cycles: int = 5,
                       max_seconds: float = 3600.0,
                       pit_ceiling: float = 450.0) -> bool:
        """Begin a relay auto-tune around *setpoint*. Returns False if one is
        already running. The session drives the fan via the link and, on
        success, writes the new PID constants."""
        from .autotune import AutoTuneSession
        if self.tuner is not None and not self.tuner.done:
            return False
        self.tuner = AutoTuneSession(
            service=self, setpoint=setpoint, rule=rule,
            relay_high=relay_high, relay_low=relay_low, hysteresis=hysteresis,
            max_cycles=max_cycles, max_seconds=max_seconds,
            pit_ceiling=pit_ceiling)
        self.tuner.start()
        return True

    def cancel_autotune(self) -> None:
        if self.tuner is not None:
            self.tuner.abort("cancelled")

    def autotune_status(self) -> Optional[dict]:
        return self.tuner.status() if self.tuner is not None else None

    # -- cook program ------------------------------------------------------

    def start_program(self, stages: list, name: str = "") -> None:
        """Start a (validated) multi-stage cook program. Replaces any running
        one. Applies stage 0's setpoint immediately."""
        from .cookprogram import CookProgramRunner
        self.program = CookProgramRunner(self, stages, name=name)
        ts = self.time_fn()
        self.program.start(ts)

    def stop_program(self) -> None:
        if self.program is not None:
            self.program.state.done = True
            self._emit({"type": "program", "event": "stopped",
                        "program": self.program.status()})
            self.program = None

    def advance_program(self) -> bool:
        if self.program is None or self.program.state.done:
            return False
        self.program.advance_now(self.time_fn())
        return True

    def program_status(self) -> Optional[dict]:
        if self.program is None:
            return None
        return self.program.status()

    def send_command_threadsafe(self, line: str) -> None:
        """Send a command from a non-loop context (e.g. the tuner's timer)."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(
                lambda: self.link.send(
                    protocol.command(line) if not line.endswith("\n") else line))

    # -- outgoing ----------------------------------------------------------

    async def send_command(self, line: str) -> None:
        self.link.send(protocol.command(line) if not line.endswith("\n") else line)
