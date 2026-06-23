"""Daemon entry point.

Run against the real board on the Pi:
    python -m heatermeterd.main --serial /dev/serial0

Or with no hardware at all (in-process simulator, in-memory history):
    python -m heatermeterd.main --sim

Add Home Assistant via MQTT (optional):
    python -m heatermeterd.main --serial /dev/serial0 --mqtt-host 192.168.3.x

Then open http://<host>:8080/ in a browser.
"""

from __future__ import annotations

import argparse
import os
import sys

from .links import SerialLink, SimLink
from .service import HeaterMeterService
from .store import Store


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="HeaterMeter host daemon")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="DEVICE",
                     help="serial device, e.g. /dev/serial0 or a simulator PTY")
    src.add_argument("--sim", action="store_true",
                     help="run an in-process simulated board (no hardware)")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=None,
                    help="SQLite history file (default: hm.sqlite, "
                         "or in-memory for --sim)")
    ap.add_argument("--setpoint", type=float, default=225.0,
                    help="initial setpoint for --sim")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="update interval for --sim")
    # Optional MQTT / Home Assistant. If no host (flag or HM_MQTT_HOST) is
    # given, MQTT is off. Credentials may also come from the environment
    # (HM_MQTT_USER / HM_MQTT_PASS) so the password can live in a chmod-600
    # systemd EnvironmentFile instead of the unit file / process list.
    ap.add_argument("--mqtt-host", default=None,
                    help="MQTT broker host (enables Home Assistant discovery); "
                         "falls back to $HM_MQTT_HOST")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default=None,
                    help="MQTT username; falls back to $HM_MQTT_USER")
    ap.add_argument("--mqtt-pass", default=None,
                    help="MQTT password; falls back to $HM_MQTT_PASS")
    ap.add_argument("--ssl-cert", default=None,
                    help="TLS certificate file; with --ssl-key, serves HTTPS "
                         "(also via $HM_SSL_CERT). Enables PWA install and "
                         "web push, which need a secure context.")
    ap.add_argument("--ssl-key", default=None,
                    help="TLS private key file (also via $HM_SSL_KEY)")
    ap.add_argument("--mqtt-node", default="hm",
                    help="MQTT node id / unique-id prefix")
    return ap


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)

    if args.sim:
        link = SimLink(setpoint=args.setpoint, interval=args.interval)
        db = args.db or ":memory:"
    else:
        link = SerialLink(args.serial, args.baud)
        db = args.db or "hm.sqlite"

    store = Store(db)
    service = HeaterMeterService(link, store)

    # MQTT / Home Assistant. The persisted UI config (mqtt.json, next to the
    # history DB) wins; the flags / $HM_MQTT_* env vars below only seed the
    # defaults the first time, before anything is saved from the dashboard.
    if db and db != ":memory:":
        cfg_dir = os.path.dirname(os.path.abspath(db)) or "."
        service.mqtt_config_path = os.path.join(cfg_dir, "mqtt.json")
        service.notify_config_path = os.path.join(cfg_dir, "notify.json")
        service.push_config_path = os.path.join(cfg_dir, "push.json")
        service.display_config_path = os.path.join(cfg_dir, "display.json")
        service.cookdone_config_path = os.path.join(cfg_dir, "cookdone.json")
        service.probewatch_config_path = os.path.join(cfg_dir, "probewatch.json")
        service.lidrecovery_config_path = os.path.join(cfg_dir, "lidrecovery.json")
        service.uiprefs_config_path = os.path.join(cfg_dir, "uiprefs.json")
        service.profiles_path = os.path.join(cfg_dir, "profiles.json")
        service.probe_presets_path = os.path.join(cfg_dir, "probe_presets.json")
        service.storage_config_path = os.path.join(cfg_dir, "storage.json")
        service.auth_config_path = os.path.join(cfg_dir, "auth.json")
        # In-software firmware updater. The spool is the group-writable IPC
        # handoff with the root flash helper; the manifest is the trusted,
        # root-owned list of vetted images the helper will accept (overridable
        # via $HM_FIRMWARE_MANIFEST for dev). See deploy/hm-flash + install.sh.
        service.firmware_dir = os.path.join(cfg_dir, "firmware")
        service.firmware_spool = os.path.join(service.firmware_dir, "spool")
        service.firmware_manifest_path = os.environ.get(
            "HM_FIRMWARE_MANIFEST",
            "/usr/local/share/heatermeter/firmware/manifest.json")
        try:
            os.makedirs(service.firmware_spool, exist_ok=True)
        except OSError:
            pass
        # In-software host-app updater. The daemon downloads + verifies a release
        # into staging, then the root helper deploy/hm-update swaps it into the
        # install root and restarts the service. The spool is the IPC handoff.
        # install_root is the repo dir to update (override via $HM_INSTALL_ROOT).
        service.hostupdate_dir = os.path.join(cfg_dir, "hostupdate")
        service.hostupdate_spool = os.path.join(service.hostupdate_dir, "spool")
        service.hostupdate_staging = os.path.join(service.hostupdate_dir, "staging")
        service.hostupdate_config_path = os.path.join(cfg_dir, "hostupdate.json")
        service.install_root = os.environ.get(
            "HM_INSTALL_ROOT",
            os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))
        try:
            os.makedirs(service.hostupdate_staging, exist_ok=True)
            os.makedirs(service.hostupdate_spool, exist_ok=True)
        except OSError:
            pass
    mqtt_host = args.mqtt_host or os.environ.get("HM_MQTT_HOST")
    if mqtt_host:
        service._mqtt_env_default = {
            "enabled": True,
            "host": mqtt_host,
            "port": args.mqtt_port,
            "username": args.mqtt_user or os.environ.get("HM_MQTT_USER") or "",
            "password": args.mqtt_pass or os.environ.get("HM_MQTT_PASS") or "",
            "node_id": args.mqtt_node or "hm",
        }
    # The bridge itself is built in service.start() (_start_mqtt) from the
    # effective config, so it can be reconfigured live via the API.

    # Imported here so the web deps are only needed when actually serving.
    import uvicorn
    from .api import create_app

    app = create_app(service)
    # Optional HTTPS: both a cert and key (flags or env) switch uvicorn to TLS.
    # A secure context is what unlocks PWA installation and real web push.
    ssl_cert = args.ssl_cert or os.environ.get("HM_SSL_CERT")
    ssl_key = args.ssl_key or os.environ.get("HM_SSL_KEY")
    ssl_kwargs = {}
    if ssl_cert and ssl_key:
        if not (os.path.exists(ssl_cert) and os.path.exists(ssl_key)):
            print(f"HTTPS requested but cert/key not found: {ssl_cert}, {ssl_key}",
                  file=sys.stderr)
            return 1
        ssl_kwargs = {"ssl_certfile": ssl_cert, "ssl_keyfile": ssl_key}
    uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                **ssl_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
