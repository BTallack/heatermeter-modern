# heatermeter-modern

A modern Raspberry Pi host for the [HeaterMeter](https://github.com/CapnBry/HeaterMeter)
BBQ controller. It replaces the legacy OpenWrt / Lua / LuCI software stack with a
standard Raspberry Pi OS application (Python + FastAPI backend, live uPlot
dashboard) while talking to the **unmodified** ATmega328 board over its existing
serial protocol.

See [PLAN.md](PLAN.md) for the roadmap and [PROTOCOL.md](PROTOCOL.md) for the
board contract.

## Layout

```
backend/
  heatermeterd/      the package: protocol, serial_io, links, store, state,
                     service, api, main
  static/            the classic no-build dashboard (uPlot vendored locally),
                     now served at /classic as a fallback
  tools/             dev tools: simulator, monitor/capture, sender, replay
  tests/             unit + integration tests
  run_tests.py       dependency-free test runner (pytest also works)
frontend/            the primary UI: a Svelte + Konsta build (served at /)
deploy/              systemd unit + Raspberry Pi setup notes
```

## Web UI

Two UIs are served by the same daemon, fully self-contained (no CDN / internet
dependency at runtime):

| Path | UI |
|---|---|
| `/` | **Primary** - Svelte + Konsta app (mobile-first, bottom tab bar on phones, a sidebar on desktop, light/dark/auto theme). Built from `frontend/`. |
| `/classic` | Legacy no-build dashboard. Kept as a fallback; also backs the public `/share/<token>` cook pages. |

The Svelte app is a build-time artifact - `npm` runs only on a dev machine, and
the daemon just serves the compiled static files in `frontend/dist/`:

```bash
cd frontend
npm install      # one time, on a machine with internet
npm run build    # emits frontend/dist/ (committed/deployed alongside the backend)
```

If `frontend/dist/` is absent, the daemon falls back to serving the classic
dashboard at `/`.

## Run the dashboard with no hardware

The daemon has a built-in simulated board, so you can see the full UI before the
Pi is wired up.

```bash
cd backend
python3 -m venv ../.venv && ../.venv/bin/pip install fastapi "uvicorn[standard]"
../.venv/bin/python -m heatermeterd.main --sim
# open http://localhost:8080/
```

You get live readouts, a multi-probe uPlot graph, and a working setpoint control
(it drives the simulated board).

## Run the tests

```bash
cd backend
python3 run_tests.py        # pure suite, no dependencies required
# or, to include the FastAPI smoke test, use a venv that has fastapi+httpx:
../.venv/bin/python run_tests.py
```

## Features

- Live multi-probe uPlot dashboard with **fan % / servo %** overlaid on a second
  axis, setpoint control, and manual fan override.
- A full Settings screen: probe naming + Steinhart-Hart type presets, calibration
  offsets, temperature alarms, PID tuning + presets + **relay auto-tune**,
  blower/servo tuning, lid detection, LCD/LED config, plus appearance (theme) and
  an About panel (app + board firmware versions).
- **Cook intelligence:** named sessions (auto start/close), stall-aware
  time-to-done prediction, ~27 meat/doneness presets, multi-stage cook programs
  (incl. keep-warm / auto-shutdown), session compare overlay.
- **Timeline notes** with optional **photos** (downscaled client-side), shown
  below the chart and as chart markers.
- **Home Assistant** via MQTT auto-discovery and **away-from-home push** via ntfy
  - both configured in Settings, no files to edit. Alert tuning: debounce, repeat
  interval, and a "device went dark" failsafe if the board stops reporting.
- Public shareable cook links, CSV export, installable PWA.

## On the Raspberry Pi

1. Flash Raspberry Pi OS Lite 64-bit.
2. Free the serial UART - see [deploy/uart-setup.md](deploy/uart-setup.md).
3. **One-command install** (creates the venv, installs deps, enables the systemd
   service for the current user):
   ```bash
   bash deploy/install.sh
   # open http://<pi-address>:8080/
   ```
   Override defaults with env vars, e.g.
   `SERIAL=/dev/ttyAMA0 PORT=8080 bash deploy/install.sh`. Or install by hand:
   ```bash
   cd backend
   python3 -m venv ../.venv
   ../.venv/bin/pip install fastapi "uvicorn[standard]" pyserial paho-mqtt
   ../.venv/bin/python -m heatermeterd.main --serial /dev/serial0
   ```

## Integrations & alerts

Open **Settings**:

- **Home Assistant (MQTT):** enter your broker host + credentials and Save; the
  HeaterMeter device auto-appears in HA via MQTT discovery (no YAML).
- **Notifications (ntfy):** pick a hard-to-guess topic, subscribe to it in the
  free ntfy app, and Save for phone alerts away from home.

## HTTPS (optional)

The dashboard works fine over plain HTTP on your LAN. Serving it over HTTPS
gives the browser a *secure context*, which unlocks installing the dashboard as
a PWA (home-screen app) and real web push notifications.

```bash
bash deploy/gen-cert.sh          # writes data/certs/hm.crt + hm.key (self-signed)
sudo systemctl edit heatermeterd # add the two lines below, then restart
```

```ini
[Service]
Environment=HM_SSL_CERT=/home/<user>/heatermeter-modern/data/certs/hm.crt
Environment=HM_SSL_KEY=/home/<user>/heatermeter-modern/data/certs/hm.key
```

The same pair can be passed as `--ssl-cert` / `--ssl-key` flags. Your browser
will warn once about the self-signed certificate; accept it for the device (or
generate a locally-trusted cert with `mkcert` instead). The app then serves at
`https://<pi-address>:8080/`.

## Firmware flashing

The ATmega328 firmware is reflashed from the Pi over SPI. The easy path is the
in-app updater: **Settings -> Firmware** lists vetted images and flashes the
selected one with an automatic backup, live progress, and one-click rollback. It
toggles SPI at runtime (no reboot), re-inits the LCD afterward, and restores your
calibration/names/PID/alarms if the update resets the controller. The actual
flash is a supervised one-click action: keep the controller powered and stay
nearby until it reports success.

Under the hood the unprivileged daemon hands a request to a small root helper
(`/usr/local/sbin/hm-flash`, installed by `deploy/install.sh`) via a
systemd path unit; the helper sha256-verifies the image against a trusted
manifest before flashing. The manual recipe and full architecture are in
[FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md). **Note:** SPI must be off for normal
operation - the AVR drives the LCD over the same SPI pins, so the helper always
turns SPI back off when it finishes.

### Phase 0 capture (recommended first)

Before going live, record a real session so we can verify the protocol and
develop against real data:

```bash
../.venv/bin/python tools/hmmonitor.py /dev/serial0 --capture cook.log
../.venv/bin/python tools/replay.py cook.log --state
```

## Lower-level tools

```bash
# Fake board on a pseudo-terminal:
python3 tools/hmsim.py --pty            # prints a device path
python3 tools/hmmonitor.py <that-path>  # watch it
python3 tools/hmsend.py <that-path> --setpoint 250
```
