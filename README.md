# heatermeter-modern

A modern Raspberry Pi host for the [HeaterMeter](https://github.com/CapnBry/HeaterMeter)
BBQ controller. It replaces the legacy OpenWrt / Lua / LuCI software stack with a
standard Raspberry Pi OS application (Python + FastAPI backend, live uPlot
dashboard) while talking to the **unmodified** ATmega328 board over its existing
serial protocol.

See [ROADMAP.md](ROADMAP.md) for where this is headed, [PROTOCOL.md](PROTOCOL.md)
for the board contract, and [docs/HOME-ASSISTANT.md](docs/HOME-ASSISTANT.md) for
the Home Assistant automation cookbook.

## Layout

```
backend/
  heatermeterd/      the package: protocol, serial_io, links, store, state,
                     service, api, main
  static/            the public /share cook page + its assets (uPlot vendored)
  tools/             dev tools: simulator, monitor/capture, sender, replay
  tests/             unit + integration tests
  run_tests.py       dependency-free test runner (pytest also works)
frontend/            the primary UI: a Svelte + Konsta build (served at /)
deploy/              systemd unit + Raspberry Pi setup notes
```

## Web UI

One self-contained UI (no CDN / internet dependency at runtime): a Svelte +
Konsta app served at `/` - mobile-first with a bottom tab bar on phones,
popup or slide-out panels on desktop, light/dark/auto theme. Public
`/share/<token>` cook pages are served read-only without authentication.

The app is a build-time artifact - `npm` runs only on a dev machine, and the
daemon just serves the compiled static files in `frontend/dist/`:

```bash
cd frontend
npm install      # one time, on a machine with internet
npm run build    # emits frontend/dist/ (deployed alongside the backend)
```

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

- **Guided Cooks:** pick what you're cooking (brisket, pork butt, ribs, ...) and
  the pit is set, the probe named and targeted, and you're coached through every
  milestone - the stall, the wrap (with an "I wrapped it" confirm), the pull,
  and the rest - in the app, by push, and on the LCD. Optional auto keep-warm
  the moment the food hits its target.
- **Cook intelligence:** stall-aware time-to-done predictions with a done-by
  clock, automatic stall detection, charcoal/fuel monitoring with an "add fuel"
  alert from blower effort, probe-dropout/fault alerts, and Meater-style
  automatic cook completion when the probe is pulled.
- **Sessions, timeline, and reports:** every cook is a named session with an
  auto-annotated timeline (lid events, setpoint changes, stall, wrap, target),
  notes with photos, a printable per-cook report with prediction accuracy,
  one-click "repeat this cook", and CSV export. Public shareable cook links.
- Live multi-probe uPlot dashboard with fan/servo overlay, setpoint control,
  manual fan override, meat/doneness presets, multi-stage cook programs
  (incl. keep-warm / auto-shutdown), session compare overlay, kitchen timers.
- A full tabbed Settings screen: probe types (incl. K-type thermocouple) and
  calibration, alarms, PID tuning + relay auto-tune, **cooker profiles**
  (save/switch tunings per grill), blower/servo, lid detection, LCD/LEDs +
  display messages, storage retention, backup/restore, optional password
  protection, and a first-run setup wizard.
- **Home Assistant** via MQTT auto-discovery - temperatures, setpoint and food
  targets (writable), lid, stall, fuel-low, and predicted-done entities; see the
  [automation cookbook](docs/HOME-ASSISTANT.md). **Away-from-home push** via
  ntfy with debounce, repeat, and a "device went dark" failsafe.
- **In-app updates, both layers:** flash the board firmware from Settings
  (SHA-256 verified, auto-backup, one-click rollback) and update the host app
  itself from a GitHub release channel (verified, health-checked,
  auto-rolled-back on failure). Nightly local backups via systemd timer.
- Optional HTTPS (enables PWA install + screen wake-lock during cooks).

## On the Raspberry Pi

1. Flash Raspberry Pi OS Lite 64-bit.
2. Free the serial UART - see [deploy/uart-setup.md](deploy/uart-setup.md).
3. **One-command install** (creates the venv, installs deps, enables the systemd
   service for the current user):
   ```bash
   bash deploy/install.sh
   # open http://<pi-address>:8080/
   ```
   The installer also sets up the in-app firmware updater, the host
   self-updater, system power helpers, and the nightly backup timer.
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
