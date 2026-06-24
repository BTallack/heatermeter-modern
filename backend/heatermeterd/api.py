"""FastAPI application: REST endpoints, a WebSocket live stream, and static
serving of the dashboard.

Requires the ``web`` extras (fastapi + uvicorn). The pure modules
(protocol/state/store/service/links/predict/presets) do not import this, so the
test suite runs without these dependencies installed.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import protocol, presets, predict
from .service import WS_SHUTDOWN

# Host app version (shown in the dashboard's About screen, distinct from the
# board firmware version reported in $UCID).
APP_VERSION = "0.5.2"


# -- request bodies ---------------------------------------------------------

class SetpointBody(BaseModel):
    value: float
    unit: str = "F"


class ManualBody(BaseModel):
    percent: float


class CommandBody(BaseModel):
    path: str


class MqttConfigBody(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    # Omitted/blank password means "keep the stored one" (the GET never returns
    # it, so the UI can't echo it back).
    password: str | None = None
    node_id: str = "hm"


class NotifyConfigBody(BaseModel):
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str | None = None       # blank/None = keep the stored token
    debounce_sec: int = 30
    repeat_min: int = 0
    dark_timeout_sec: int = 90


class ProbeNameBody(BaseModel):
    index: int
    name: str


class OffsetsBody(BaseModel):
    offsets: list[float | None]   # 4 entries; null leaves that probe unchanged


class PidBody(BaseModel):
    b: float | None = None
    p: float | None = None
    i: float | None = None
    d: float | None = None


class AlarmsBody(BaseModel):
    thresholds: list[float | None]   # [low0, high0, low1, high1, ...]


class FanBody(BaseModel):
    fan_low: float | None = None
    fan_high: float | None = None
    servo_min: float | None = None
    servo_max: float | None = None
    max_startup: float | None = None
    fan_active_floor: float | None = None
    servo_active_ceil: float | None = None
    invert_fan: bool | None = None
    invert_servo: bool | None = None


class ProbeTypeBody(BaseModel):
    index: int
    # Either a named preset, or "disabled", or "internal" (generic), or rf node.
    preset: str | None = None
    disabled: bool = False


class LidBody(BaseModel):
    offset_percent: float | None = None
    duration_seconds: float | None = None
    active: int | None = None


class LcdBody(BaseModel):
    backlight: int | None = None
    home_mode: int | None = None
    leds: list[int] | None = None   # 4 LED stimulus bytes


class HomeRotateBody(BaseModel):
    seconds: int                    # LCD home-screen probe rotation interval (1-60)


class UnitsBody(BaseModel):
    unit: str                       # "F" or "C"


class CookDoneBody(BaseModel):
    enabled: bool | None = None
    grace_secs: int | None = None
    on_complete: str | None = None      # "notify" | "shutdown" | "keep_warm"
    keep_warm_temp: float | None = None
    drop_margin: float | None = None
    rise_delta: float | None = None


class LcdMessageBody(BaseModel):
    line1: str
    line2: str = ""


class GuidedStartBody(BaseModel):
    key: str                      # guided-cook catalog key
    channel: str = "food1"        # food probe carrying the meat
    auto_keep_warm: bool = False  # drop pit to keep-warm when target reached


class ProfileBody(BaseModel):
    name: str


class ProbeWatchBody(BaseModel):
    enabled: bool | None = None          # disconnect / fault watching
    dropout_secs: float | None = None    # sustained missing time before alerting
    stall_enabled: bool | None = None    # stall start/end detection
    stall_low: float | None = None
    stall_high: float | None = None


class UIPrefsBody(BaseModel):
    welcomed: bool | None = None        # welcome banner dismissed (per-device-agnostic)


class PushRegisterBody(BaseModel):
    token: str                          # APNs device token (hex)
    platform: str = "ios"


class PushConfigBody(BaseModel):
    enabled: bool | None = None
    sandbox: bool | None = None         # True for debug builds (sandbox APNs)
    team_id: str | None = None
    key_id: str | None = None
    key_path: str | None = None
    bundle_id: str | None = None


class LidRecoveryBody(BaseModel):
    enabled: bool | None = None          # smart lid-open recovery on/off
    recover_delta: float | None = None   # rise off the low that signals "closed"
    start_pct: int | None = None         # gentle initial fan % on resume
    ramp_secs: int | None = None         # ramp start_pct -> full over this long
    min_armed_secs: int | None = None    # dwell before recovery can fire
    step_pct: int | None = None          # ramp quantisation step


class PidInternalsBody(BaseModel):
    enabled: bool


class NoteBody(BaseModel):
    text: str
    channel: str | None = None
    # Optional attached photo as a data URL or bare base64 ("data:image/jpeg;base64,...").
    photo_b64: str | None = None


class AutoTuneBody(BaseModel):
    setpoint: float
    rule: str = "tyreus_luyben"
    relay_high: float = 100.0
    relay_low: float = 0.0
    hysteresis: float = 1.0
    max_cycles: int = 5
    max_seconds: float = 3600.0
    pit_ceiling: float = 450.0


class SessionPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class ProgramStartBody(BaseModel):
    stages: list
    name: str | None = None


class ProgramSaveBody(BaseModel):
    name: str
    stages: list


class ShareBody(BaseModel):
    enabled: bool


class FirmwareFlashBody(BaseModel):
    version: str
    confirm: bool = False           # the UI must explicitly confirm
    action: str = "flash"           # "flash" | "rollback"


class FirmwareRollbackBody(BaseModel):
    confirm: bool = False


class HostUpdateConfigBody(BaseModel):
    manifest_url: str = ""          # the release-channel manifest URL (http/https)
    auto_check: bool = False        # periodically poll + surface availability


class HostUpdateApplyBody(BaseModel):
    confirm: bool = False           # the UI must explicitly confirm
    version: str | None = None      # apply a specific offered version (optional)
    action: str = "update"          # "update" | "rollback"


class ShutdownBody(BaseModel):
    confirm: bool = False
    dryrun: bool = False    # validate the IPC chain without powering off


class RestoreBody(BaseModel):
    data: dict              # a backup object produced by GET /api/backup


class DbCleanupBody(BaseModel):
    retention_days: int = 0     # delete samples older than this (0 = keep all)
    downsample_days: int = 0    # thin samples older than this to ~1/min (0 = off)


class LoginBody(BaseModel):
    password: str


class AuthPasswordBody(BaseModel):
    password: str               # the new password (enables auth if off)
    current: str | None = None  # required to change an existing password


class AuthDisableBody(BaseModel):
    current: str | None = None  # required to turn auth off


def create_app(service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="HeaterMeter", version="0.2.0", lifespan=lifespan)

    # -- optional auth gate -----------------------------------------------
    # When auth is enabled, every /api/* call needs a valid bearer token EXCEPT
    # the login/status endpoints and public share links. Static files (the SPA)
    # are always served so it can present a login screen. Off by default => no-op.
    _AUTH_EXEMPT = ("/api/login", "/api/auth", "/api/share/")

    def _bearer(request: Request):
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:]
        return request.query_params.get("token")

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        path = request.url.path
        if (path.startswith("/api/")
                and not any(path.startswith(p) for p in _AUTH_EXEMPT)
                and service.auth_config()["enabled"]
                and not service.auth_valid(_bearer(request))):
            return JSONResponse({"error": "Authentication required."},
                                status_code=401)
        return await call_next(request)

    @app.get("/api/auth")
    async def auth_status():
        return service.auth_status()

    @app.post("/api/login")
    async def auth_login(body: LoginBody):
        result = service.auth_login(body.password)
        if not result.get("ok"):
            return JSONResponse(result, status_code=401)
        return result

    @app.post("/api/auth/password")
    async def auth_set_password(body: AuthPasswordBody):
        result = service.auth_set_password(body.password, body.current)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/auth/disable")
    async def auth_disable(body: AuthDisableBody):
        result = service.auth_disable(body.current)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    # -- live state & history ---------------------------------------------

    @app.get("/api/status")
    async def get_status():
        d = service.state.to_dict()
        d["session_id"] = service.session_id
        d["app_version"] = APP_VERSION
        d["firmware"] = service.firmware_status
        d["hostupdate"] = service.hostupdate_status
        d["probe_health"] = service.probe_health
        d["guided"] = service.guided_status()
        d["fuel"] = service.fuel_status()
        d["probe_preset_sel"] = service.get_probe_presets_sel()
        return d

    @app.get("/api/history")
    async def get_history(minutes: float | None = None, limit: int = 5000,
                          session_id: int | None = None):
        since = None
        if minutes:
            since = service.time_fn() - minutes * 60
        return await asyncio.to_thread(
            service.store.history_columns, since, limit, session_id)

    @app.get("/api/events")
    async def get_events(minutes: float | None = None,
                         session_id: int | None = None, limit: int = 1000):
        since = None
        if minutes:
            since = service.time_fn() - minutes * 60
        return await asyncio.to_thread(
            service.store.list_events, session_id, since, limit)

    @app.get("/api/report/{session_id}")
    async def get_report(session_id: int):
        """Printable per-cook report page (HTML, self-contained chart)."""
        from . import report
        s = await asyncio.to_thread(service.store.get_session, session_id)
        if not s:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        cols = await asyncio.to_thread(
            service.store.history_columns, None, 2000, session_id)
        evs = await asyncio.to_thread(
            service.store.list_events, session_id, None, 1000)
        nts = await asyncio.to_thread(service.store.list_notes, session_id)
        page = report.build_report_html(
            s, cols, evs, nts,
            probe_names=service.state.probe_names or None,
            unit=(service.state.pid.get("units") or "F"))
        return HTMLResponse(page)

    # -- control -----------------------------------------------------------

    @app.post("/api/setpoint")
    async def set_setpoint(body: SetpointBody):
        await service.send_command(protocol.set_setpoint(body.value, body.unit))
        return {"ok": True}

    @app.post("/api/manual")
    async def set_manual(body: ManualBody):
        await service.send_command(protocol.set_manual_output(body.percent))
        return {"ok": True}

    @app.post("/api/probe-name")
    async def set_probe_name(body: ProbeNameBody):
        if not 0 <= body.index <= 3:
            return JSONResponse({"error": "index must be 0-3"}, status_code=400)
        await service.send_command(protocol.set_probe_name(body.index, body.name))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/offsets")
    async def set_offsets(body: OffsetsBody):
        if len(body.offsets) != 4:
            return JSONResponse({"error": "offsets must have 4 entries"},
                                status_code=400)
        await service.send_command(protocol.set_probe_offsets(body.offsets))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/pid")
    async def set_pid(body: PidBody):
        sent = []
        for param in ("b", "p", "i", "d"):
            val = getattr(body, param)
            if val is not None:
                await service.send_command(protocol.set_pid(param, val))
                sent.append(param)
        await service.send_command(protocol.request_config())
        return {"ok": True, "sent": sent}

    @app.post("/api/alarms")
    async def set_alarms(body: AlarmsBody):
        await service.send_command(protocol.set_alarms(body.thresholds))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/fan")
    async def set_fan(body: FanBody):
        # Pack the two invert booleans into the firmware's flags bitfield
        # (bit0 = invert fan, bit1 = invert servo) only if either was provided.
        flags = None
        if body.invert_fan is not None or body.invert_servo is not None:
            cur = 0
            try:
                cur = int(service.state.fan.get("flags") or 0)
            except (ValueError, TypeError):
                cur = 0
            inv_fan = body.invert_fan if body.invert_fan is not None else bool(cur & 1)
            inv_servo = body.invert_servo if body.invert_servo is not None else bool(cur & 2)
            flags = (1 if inv_fan else 0) | (2 if inv_servo else 0)
        await service.send_command(protocol.set_fan(
            fan_low=body.fan_low, fan_high=body.fan_high,
            servo_min=body.servo_min, servo_max=body.servo_max,
            flags=flags, max_startup=body.max_startup,
            fan_active_floor=body.fan_active_floor,
            servo_active_ceil=body.servo_active_ceil))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/lid")
    async def set_lid(body: LidBody):
        await service.send_command(protocol.set_lid_detect(
            body.offset_percent, body.duration_seconds, body.active))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/lid/open")
    async def lid_open():
        await service.send_command(protocol.lid_open_now())
        return {"ok": True}

    @app.post("/api/lid/cancel")
    async def lid_cancel():
        await service.send_command(protocol.lid_open_cancel())
        return {"ok": True}

    # -- LCD / LED config --------------------------------------------------

    @app.get("/api/lcd-options")
    async def lcd_options():
        return {"led_stimuli": protocol.LED_STIMULI,
                "home_modes": protocol.HOME_MODES,
                "led_invert_bit": protocol.LED_INVERT}

    @app.post("/api/lcd/message")
    async def lcd_message(body: LcdMessageBody):
        result = service.send_lcd_message(body.line1, body.line2)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    # -- guided cooks --------------------------------------------------------

    @app.get("/api/guided")
    async def get_guided():
        from . import guided as guided_mod
        return {"catalog": guided_mod.catalog(),
                "active": service.guided_status()}

    @app.post("/api/guided/start")
    async def start_guided(body: GuidedStartBody):
        result = service.start_guided_cook(
            body.key, body.channel, auto_keep_warm=body.auto_keep_warm)
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/guided/wrapped")
    async def guided_wrapped():
        result = service.confirm_guided_wrap()
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/guided/stop")
    async def guided_stop():
        result = service.stop_guided_cook()
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    # -- cook insights + repeat ----------------------------------------------

    @app.get("/api/insights")
    async def get_insights():
        return await asyncio.to_thread(service.cook_insights)

    @app.post("/api/sessions/{session_id}/repeat")
    async def repeat_session(session_id: int):
        result = service.repeat_cook(session_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    # -- cooker profiles -----------------------------------------------------

    @app.get("/api/profiles")
    async def get_profiles():
        return service.get_profiles()

    @app.post("/api/profiles")
    async def save_profile(body: ProfileBody):
        result = service.save_profile(body.name)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/profiles/apply")
    async def apply_profile(body: ProfileBody):
        result = service.apply_profile(body.name)
        if not result.get("ok"):
            return JSONResponse(result, status_code=404)
        return result

    @app.delete("/api/profiles/{name}")
    async def delete_profile(name: str):
        result = service.delete_profile(name)
        if not result.get("ok"):
            return JSONResponse(result, status_code=404)
        return result

    @app.post("/api/lcd")
    async def set_lcd(body: LcdBody):
        if body.leds is not None and len(body.leds) != 4:
            return JSONResponse({"error": "leds must have 4 entries"},
                                status_code=400)
        await service.send_command(protocol.set_lcd(
            backlight=body.backlight, home_mode=body.home_mode, leds=body.leds))
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/pid-internals")
    async def set_pid_internals(body: PidInternalsBody):
        await service.send_command(protocol.set_pid_internals(body.enabled))
        return {"ok": True}

    @app.get("/api/home-rotate")
    async def get_home_rotate():
        return {"rotate_secs": service.get_home_rotate()}

    @app.post("/api/home-rotate")
    async def set_home_rotate(body: HomeRotateBody):
        return service.set_home_rotate(body.seconds)

    @app.post("/api/units")
    async def set_units(body: UnitsBody):
        result = service.set_units(body.unit)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.get("/api/cook-done")
    async def get_cook_done():
        return service.get_cookdone()

    @app.post("/api/cook-done")
    async def set_cook_done(body: CookDoneBody):
        cur = service.get_cookdone()
        merged = {**cur, **{k: v for k, v in body.model_dump().items()
                            if v is not None}}
        return service.save_cookdone(merged)

    @app.get("/api/probe-watch")
    async def get_probe_watch():
        return service.get_probewatch()

    @app.post("/api/probe-watch")
    async def set_probe_watch(body: ProbeWatchBody):
        cur = service.get_probewatch()
        merged = {**cur, **{k: v for k, v in body.model_dump().items()
                            if v is not None}}
        return service.save_probewatch(merged)

    @app.get("/api/lid-recovery")
    async def get_lid_recovery():
        return service.get_lidrecovery()

    @app.post("/api/lid-recovery")
    async def set_lid_recovery(body: LidRecoveryBody):
        cur = service.get_lidrecovery()
        merged = {**cur, **{k: v for k, v in body.model_dump().items()
                            if v is not None}}
        return service.save_lidrecovery(merged)

    @app.post("/api/cook/finish")
    async def finish_cook():
        result = service.finish_cook()
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    @app.get("/api/db")
    async def get_db():
        return service.db_stats()

    @app.post("/api/db/cleanup")
    async def db_cleanup(body: DbCleanupBody):
        service.save_storage_config(
            {"retention_days": body.retention_days,
             "downsample_days": body.downsample_days})
        # Prune + vacuum can take a moment; run off the event loop.
        return await asyncio.to_thread(service.cleanup_db)

    @app.get("/api/backup")
    async def get_backup(include_secrets: int = 0):
        d = service.config_backup(include_secrets=bool(include_secrets))
        d["app_version"] = APP_VERSION
        return d

    @app.post("/api/restore")
    async def post_restore(body: RestoreBody):
        result = service.config_restore(body.data)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/system/shutdown")
    async def system_shutdown(body: ShutdownBody):
        if not body.confirm:
            return JSONResponse(
                {"error": "Confirmation required to shut down."}, status_code=400)
        result = service.shutdown_system(dryrun=body.dryrun)
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    # -- in-software firmware updater -------------------------------------

    @app.get("/api/firmware")
    async def get_firmware():
        return service.firmware_listing()

    @app.get("/api/firmware/status")
    async def get_firmware_status():
        return service.firmware_status

    @app.post("/api/firmware/flash")
    async def flash_firmware(body: FirmwareFlashBody):
        if not body.confirm:
            return JSONResponse(
                {"error": "Confirmation required to flash firmware."},
                status_code=400)
        # "backup" is a dry run (sig-gate + backup, no write) used for the
        # supervised first-light self-test; the UI only ever sends flash.
        if body.action not in ("flash", "rollback", "backup"):
            return JSONResponse({"error": "invalid action"}, status_code=400)
        result = service.start_firmware_flash(body.version, action=body.action)
        if not result.get("ok"):
            # A guard refusal or busy state, not a server error.
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/firmware/rollback")
    async def rollback_firmware(body: FirmwareRollbackBody):
        if not body.confirm:
            return JSONResponse(
                {"error": "Confirmation required to roll back firmware."},
                status_code=400)
        result = service.start_firmware_flash("(previous)", action="rollback")
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return result

    # -- in-software host-app updater -------------------------------------

    @app.get("/api/host-update")
    async def get_host_update():
        return service.host_update_listing()

    @app.get("/api/host-update/status")
    async def get_host_update_status():
        return service.hostupdate_status

    @app.post("/api/host-update/config")
    async def set_host_update_config(body: HostUpdateConfigBody):
        result = service.save_host_update_config(
            {"manifest_url": body.manifest_url, "auto_check": body.auto_check})
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/host-update/check")
    async def check_host_update():
        result = await service.check_host_update()
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/host-update/apply")
    async def apply_host_update(body: HostUpdateApplyBody):
        if not body.confirm:
            return JSONResponse(
                {"error": "Confirmation required to update the software."},
                status_code=400)
        if body.action not in ("update", "rollback"):
            return JSONResponse({"error": "invalid action"}, status_code=400)
        result = await service.start_host_update(
            version=body.version, action=body.action)
        if not result.get("ok"):
            # A guard refusal, integrity failure, or busy state, not a 500.
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/host-update/ack")
    async def ack_host_update():
        return service.ack_host_update()

    # -- probe types / presets --------------------------------------------

    @app.get("/api/probe-presets")
    async def get_probe_presets():
        return {
            "presets": {k: {"label": v["label"],
                            "type": v.get("type", protocol.PROBETYPE_INTERNAL)}
                        for k, v in protocol.PROBE_PRESETS.items()},
            "types": protocol.PROBETYPE_LABELS,
        }

    @app.post("/api/probe-type")
    async def set_probe_type(body: ProbeTypeBody):
        if not 0 <= body.index <= 3:
            return JSONResponse({"error": "index must be 0-3"}, status_code=400)
        if body.disabled:
            await service.send_command(protocol.set_probe_disabled(body.index))
            service.set_probe_preset_sel(body.index, "__off")
        elif body.preset:
            if body.preset not in protocol.PROBE_PRESETS:
                return JSONResponse({"error": "unknown preset"}, status_code=400)
            await service.send_command(
                protocol.set_probe_preset(body.index, body.preset))
            service.set_probe_preset_sel(body.index, body.preset)
        else:
            return JSONResponse({"error": "provide preset or disabled"},
                                status_code=400)
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.get("/api/presets")
    async def get_presets():
        return presets.all_presets()

    # -- prediction --------------------------------------------------------

    @app.get("/api/predict")
    async def get_predict(channel: str, target: float, window: float = 900,
                          rest_secs: float = 0):
        """Estimate time-to-target for a probe channel
        (pit/food1/food2/ambient). Food probes use the stall-aware S-curve model
        with the current pit temperature as the cooker environment; the pit
        channel itself uses the linear model."""
        valid = {"pit", "food1", "food2", "ambient"}
        if channel not in valid:
            return JSONResponse({"error": f"channel must be one of {sorted(valid)}"},
                                status_code=400)
        now = service.time_fn()
        ts, vals = await asyncio.to_thread(
            service.store.recent_series, channel, max(window * 2, 3600), now)
        # For a food probe, the cooker environment is the pit temperature (use
        # the live setpoint if the pit reading is unavailable). This powers the
        # stall-aware model. The pit channel has no higher environment, so it
        # falls through to linear.
        env = None
        if channel in ("food1", "food2", "ambient"):
            # A food probe's environment is the cooker (pit) temperature. The
            # ambient channel uses this only when the user has toggled it to a
            # food probe; a true ambient reading never requests a prediction.
            st = service.state.status
            env = st.pit if isinstance(st.pit, (int, float)) else st.set_point
        # Live stall verdict from the probe watcher: during a detected stall the
        # estimate is flagged and its band widened instead of quoting a
        # confident clock.
        stalled = False
        try:
            chs = service._probewatch._ch.get(channel)
            stalled = bool(chs and chs.stalled)
        except Exception:
            pass
        p = predict.predict(ts, vals, target, env_temp=env,
                            window_seconds=window, stalled=stalled)
        d = p.to_dict()
        # Wall-clock framing: when the food will hit target, and when it is
        # ready to eat after an optional rest (carryover happens during rest).
        if p.eta_seconds is not None:
            d["done_at"] = now + p.eta_seconds
            if rest_secs and rest_secs > 0:
                d["ready_at"] = d["done_at"] + rest_secs
        d["rest_secs"] = rest_secs or 0
        return d

    # -- PID auto-tune -----------------------------------------------------

    @app.get("/api/autotune")
    async def autotune_status():
        st = service.autotune_status()
        return st if st is not None else {"phase": "idle"}

    @app.post("/api/autotune")
    async def autotune_start(body: AutoTuneBody):
        from .autotune import TUNING_RULES
        if body.rule not in TUNING_RULES:
            return JSONResponse(
                {"error": f"rule must be one of {sorted(TUNING_RULES)}"},
                status_code=400)
        started = service.start_autotune(
            setpoint=body.setpoint, rule=body.rule,
            relay_high=body.relay_high, relay_low=body.relay_low,
            hysteresis=body.hysteresis, max_cycles=body.max_cycles,
            max_seconds=body.max_seconds, pit_ceiling=body.pit_ceiling)
        if not started:
            return JSONResponse({"error": "auto-tune already running"},
                                status_code=409)
        return {"ok": True}

    @app.delete("/api/autotune")
    async def autotune_cancel():
        service.cancel_autotune()
        return {"ok": True}

    # -- cook programs -----------------------------------------------------

    @app.get("/api/program")
    async def program_status():
        st = service.program_status()
        return st if st is not None else {"running": False}

    @app.post("/api/program/start")
    async def program_start(body: ProgramStartBody):
        from .cookprogram import validate_program, ProgramError
        try:
            stages = validate_program(body.stages)
        except ProgramError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        service.start_program(stages, name=body.name or "")
        return {"ok": True}

    @app.post("/api/program/advance")
    async def program_advance():
        ok = service.advance_program()
        if not ok:
            return JSONResponse({"error": "no running program"}, status_code=409)
        return {"ok": True}

    @app.post("/api/program/stop")
    async def program_stop():
        service.stop_program()
        return {"ok": True}

    # Saved program templates.
    @app.get("/api/programs")
    async def list_programs():
        return await asyncio.to_thread(service.store.list_programs)

    @app.post("/api/programs")
    async def save_program(body: ProgramSaveBody):
        from .cookprogram import validate_program, ProgramError
        try:
            stages = validate_program(body.stages)
        except ProgramError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        pid = await asyncio.to_thread(
            service.store.save_program, body.name, stages, service.time_fn())
        return {"ok": True, "id": pid}

    @app.delete("/api/programs/{program_id}")
    async def delete_program(program_id: int):
        await asyncio.to_thread(service.store.delete_program, program_id)
        return {"ok": True}

    # -- sessions ----------------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions(search: str | None = None):
        return await asyncio.to_thread(service.store.list_sessions, search)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: int):
        s = await asyncio.to_thread(service.store.get_session, session_id)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        s["notes"] = await asyncio.to_thread(service.store.list_notes, session_id)
        return s

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: int, body: SessionPatch):
        await asyncio.to_thread(service.store.update_session, session_id,
                                body.name, body.description)
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: int):
        await asyncio.to_thread(service.store.delete_session, session_id)
        if service.session_id == session_id:
            service.session_id = None
        return {"ok": True}

    # -- session sharing (public read-only links) --------------------------

    @app.post("/api/sessions/{session_id}/share")
    async def set_share(session_id: int, body: ShareBody):
        s = await asyncio.to_thread(service.store.get_session, session_id)
        if not s:
            return JSONResponse({"error": "not found"}, status_code=404)
        if body.enabled:
            token = s.get("share_token")
            if not token:
                import secrets
                token = secrets.token_urlsafe(12)
                await asyncio.to_thread(service.store.set_session_share, session_id, token)
            return {"ok": True, "token": token, "url": f"/share/{token}"}
        await asyncio.to_thread(service.store.set_session_share, session_id, None)
        return {"ok": True, "token": None}

    @app.get("/api/share/{token}")
    async def get_shared(token: str):
        """Public read-only session data by share token. No auth."""
        s = await asyncio.to_thread(service.store.session_by_share_token, token)
        if not s:
            return JSONResponse({"error": "not found or sharing disabled"},
                                status_code=404)
        sid = s["id"]
        cols = await asyncio.to_thread(
            service.store.history_columns, None, 10000, sid)
        notes = await asyncio.to_thread(service.store.list_notes, sid)
        # Only expose safe, public fields.
        return {
            "name": s.get("name") or f"Cook #{sid}",
            "started_ts": s.get("started_ts"),
            "ended_ts": s.get("ended_ts"),
            "history": cols,
            "notes": [{"ts": n["ts"], "text": n["text"], "channel": n.get("channel")}
                      for n in notes],
        }

    # -- notes -------------------------------------------------------------

    @app.get("/api/notes")
    async def list_notes(session_id: int | None = None,
                         minutes: float | None = None):
        since = (service.time_fn() - minutes * 60) if minutes else None
        return await asyncio.to_thread(service.store.list_notes, session_id, since)

    @app.get("/api/ui-prefs")
    async def get_ui_prefs():
        return service.get_uiprefs()

    @app.post("/api/ui-prefs")
    async def set_ui_prefs(body: UIPrefsBody):
        return service.save_uiprefs({k: v for k, v in body.model_dump().items()
                                     if v is not None})

    def _decode_photo(b64: str | None):
        """Decode a data-URL/base64 photo. Returns (bytes, ext) or (None, None)
        on missing/invalid/oversize input (the note is still saved, sans photo)."""
        if not b64:
            return None, None
        import base64 as _b64
        import binascii
        ext = "jpg"
        if b64.startswith("data:"):
            header, _, b64 = b64.partition(",")
            sub = header[5:].split(";")[0].split("/")[-1].lower()
            ext = {"jpeg": "jpg"}.get(sub, sub) or "jpg"
        try:
            raw = _b64.b64decode(b64)
        except (binascii.Error, ValueError):
            return None, None
        if not raw or len(raw) > 10 * 1024 * 1024:   # 10 MB cap
            return None, None
        return raw, ext

    @app.post("/api/notes")
    async def add_note(body: NoteBody):
        photo_name = None
        raw, ext = _decode_photo(body.photo_b64)
        if raw is not None:
            photo_name = await asyncio.to_thread(service.store.save_photo, raw, ext)
        nid = await asyncio.to_thread(
            service.store.add_note, service.time_fn(), body.text,
            service.session_id, body.channel, photo_name)
        return {"ok": True, "id": nid, "photo": photo_name}

    @app.get("/api/photo/{name}")
    async def get_photo(name: str):
        path = await asyncio.to_thread(service.store.photo_fullpath, name)
        if not path:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @app.delete("/api/notes/{note_id}")
    async def delete_note(note_id: int):
        await asyncio.to_thread(service.store.delete_note, note_id)
        return {"ok": True}

    # -- export ------------------------------------------------------------

    @app.get("/api/export.csv")
    async def export_csv(session_id: int | None = None):
        csv_text = await asyncio.to_thread(service.store.export_csv, session_id)
        fname = f"heatermeter-session-{session_id}.csv" if session_id \
            else "heatermeter-export.csv"
        return PlainTextResponse(csv_text, headers={
            "Content-Disposition": f'attachment; filename="{fname}"'})

    # -- raw passthrough ---------------------------------------------------

    @app.post("/api/config")
    async def request_config():
        await service.send_command(protocol.request_config())
        return {"ok": True}

    @app.post("/api/command")
    async def post_command(body: CommandBody):
        path = body.path
        if not (path.startswith("/set") or path.startswith("/config")
                or path.startswith("/reboot")):
            return JSONResponse({"error": "unsupported command"}, status_code=400)
        await service.send_command(protocol.command(path))
        return {"ok": True}

    # -- MQTT / Home Assistant config -------------------------------------

    def _merge_mqtt_body(body: MqttConfigBody) -> dict:
        # Blank/None password = keep the existing stored one.
        cur = service.mqtt_effective_config()
        pw = cur.get("password", "") if not body.password else body.password
        return {
            "enabled": bool(body.enabled),
            "host": body.host.strip(),
            "port": int(body.port),
            "username": body.username.strip(),
            "password": pw,
            "node_id": (body.node_id or "hm").strip() or "hm",
        }

    @app.get("/api/mqtt")
    async def get_mqtt():
        return service.mqtt_status_public()

    @app.post("/api/mqtt")
    async def set_mqtt(body: MqttConfigBody):
        return service.reconfigure_mqtt(_merge_mqtt_body(body))

    @app.post("/api/mqtt/test")
    async def test_mqtt(body: MqttConfigBody):
        from . import mqtt as mqtt_mod
        cfg = _merge_mqtt_body(body)
        return await asyncio.to_thread(
            mqtt_mod.test_connection, cfg["host"], cfg["port"],
            cfg["username"] or None, cfg["password"] or None)

    # -- Notifications (ntfy) ---------------------------------------------

    def _merge_notify_body(body: NotifyConfigBody) -> dict:
        cur = service.notify_effective_config()
        tok = cur.get("token", "") if not body.token else body.token
        return {
            "enabled": bool(body.enabled),
            "server": (body.server or "https://ntfy.sh").strip(),
            "topic": body.topic.strip(),
            "token": tok,
            "debounce_sec": max(0, int(body.debounce_sec)),
            "repeat_min": max(0, int(body.repeat_min)),
            "dark_timeout_sec": max(0, int(body.dark_timeout_sec)),
        }

    @app.get("/api/notify")
    async def get_notify():
        return service.notify_status_public()

    @app.post("/api/notify")
    async def set_notify(body: NotifyConfigBody):
        return service.reconfigure_notify(_merge_notify_body(body))

    @app.post("/api/notify/test")
    async def test_notify(body: NotifyConfigBody):
        from . import notify as notify_mod
        cfg = _merge_notify_body(body)
        return await asyncio.to_thread(
            notify_mod.send, cfg, "HeaterMeter test",
            "Notifications are working.", "default", "bell")

    # -- native iOS push (APNs) -------------------------------------------

    @app.get("/api/push")
    async def get_push():
        return service.push_status()

    @app.post("/api/push")
    async def set_push(body: PushConfigBody):
        return service.save_push_config(
            {k: v for k, v in body.model_dump().items() if v is not None})

    @app.post("/api/push/register")
    async def register_push(body: PushRegisterBody):
        result = service.register_push_token(body.token, body.platform)
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/push/test")
    async def test_push():
        service._push("HeaterMeter test",
                      "Push notifications are working.", priority="default")
        return {"ok": True, **service.push_status()}

    @app.delete("/api/push/{token}")
    async def delete_push(token: str):
        return service.remove_push_token(token)

    # -- websocket ---------------------------------------------------------

    @app.websocket("/api/ws")
    async def ws(sock: WebSocket):
        # The browser can't set Authorization on a WebSocket, so the token comes
        # as a ?token= query param. Reject unauthenticated when auth is enabled.
        if service.auth_config()["enabled"] and not service.auth_valid(
                sock.query_params.get("token")):
            await sock.close(code=1008)   # policy violation
            return
        await sock.accept()
        q = service.subscribe()
        try:
            await sock.send_json({"ts": service.time_fn(),
                                  "session_id": service.session_id,
                                  "state": service.state.to_dict()})
            while True:
                message = await q.get()
                if message is WS_SHUTDOWN:
                    break  # daemon shutting down; close cleanly
                await sock.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            service.unsubscribe(q)

    # Serve the dashboard (no-build static files) at the root, if present.
    #
    # The app's own HTML/JS/CSS must NOT be cached by the browser: when we deploy
    # a new app.js but index.html stays the same name, an aggressively-cached
    # old app.js (calling functions the new HTML lacks, or vice-versa) breaks the
    # UI. So we send "no-cache" (revalidate every load) for our own files. The
    # vendored, versioned uPlot under /vendor/ is immutable, so let it cache hard.
    # The new Svelte/Konsta app (build-out), mounted at /app alongside the
    # classic dashboard at / so it can reach parity without disturbing the live
    # UI. Registered BEFORE the catch-all "/" routes below so /app wins. The
    # build is fully self-contained (no CDN) — see frontend/.
    svelte_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "frontend", "dist"))
    if os.path.isdir(svelte_dir):
        app.mount("/app", StaticFiles(directory=svelte_dir, html=True), name="svelte")

    # backend/static now holds only the public share page and its assets
    # (share.html, style.css, /vendor/uPlot); the classic dashboard was retired
    # once the Svelte app reached full parity.
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
    have_svelte = os.path.isdir(svelte_dir)
    have_static = os.path.isdir(static_dir)
    if have_svelte or have_static:
        NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
        IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

        def _safe_file(base: str, path: str):
            """Resolve *path* under *base*, blocking traversal. Returns the
            absolute path if it is a real file, else None."""
            target = os.path.abspath(os.path.join(base, path))
            if target != base and not target.startswith(base + os.sep):
                return None
            return target if os.path.isfile(target) else None

        if have_svelte:
            @app.get("/")
            async def index():
                return FileResponse(os.path.join(svelte_dir, "index.html"),
                                    headers=NO_CACHE)

        if have_static:
            @app.get("/share/{token}")
            async def share_page(token: str):
                # Public read-only cook page. The token is read client-side;
                # the page fetches /api/share/{token} for data.
                share_html = os.path.join(static_dir, "share.html")
                if os.path.isfile(share_html):
                    return FileResponse(share_html, headers=NO_CACHE)
                return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/{path:path}")
        async def static_file(path: str):
            # Serve assets from the Svelte build first (content-hashed under
            # /assets/), then fall back to the share-page assets (style.css,
            # /vendor/*). Both lookups are traversal-guarded.
            bases = []
            if have_svelte:
                bases.append(svelte_dir)
            if have_static:
                bases.append(static_dir)
            for base in bases:
                target = _safe_file(base, path)
                if target:
                    immutable = path.startswith("vendor/") or path.startswith("assets/")
                    return FileResponse(target,
                                        headers=IMMUTABLE if immutable else NO_CACHE)
            return JSONResponse({"error": "not found"}, status_code=404)

    return app
