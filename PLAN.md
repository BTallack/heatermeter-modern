# HeaterMeter Pi modernization - plan

## Goal

Replace the dated Raspberry Pi software stack (a custom OpenWrt image running a
Lua/LuCI daemon, RRDtool, jQuery/Flot, and a 2012-era WiFi driver) with a modern
Linux application that runs on standard Raspberry Pi OS. **The board and its
ATmega328 firmware are not touched** - the new software talks to it over the
existing serial protocol (see [PROTOCOL.md](PROTOCOL.md)), so all existing
hardware keeps working unchanged.

## Why this is safe

The firmware is a self-contained serial peripheral with a small, documented
protocol. As long as the host speaks that protocol, the entire Pi side can be
rewritten without risk to the hardware. Development happens on a *second* SD
card / Pi; the working unit stays on its current image as a reference and
instant rollback. We can develop the whole stack with no hardware at all using
the bundled board simulator (`--sim`).

## Stack (decided)

| Layer | Choice |
|---|---|
| OS | Raspberry Pi OS Lite 64-bit (Bookworm), headless |
| Target hardware | Raspberry Pi 3 (onboard WiFi, so the old Realtek driver problem is gone) |
| Backend | Python 3 + asyncio, FastAPI (REST + WebSocket) |
| Serial | pyserial on the Pi; raw PTY/file for hardware-free dev |
| Storage | SQLite (columnar history reads for the chart) |
| Frontend | uPlot dashboard, vendored (offline-capable). Svelte migration is a later polish step. |
| Integrations | MQTT + Home Assistant discovery; ntfy/Telegram notifications (Phase 3) |
| Packaging | systemd service |

## Roadmap

Each phase is independently useful and never risks the working unit.

- **Phase 0 - Bench capture** (needs hardware): flash Pi OS Lite, free the UART
  (see [deploy/uart-setup.md](deploy/uart-setup.md)), capture real `$HM*`
  traffic + a `/config` dump with `tools/hmmonitor.py --capture`. Confirm
  PROTOCOL.md field-for-field via `tools/replay.py`.
- **Phase 1 - Read-only + live dashboard (DONE, live on hardware 2026-05-31):**
  protocol parser, state model, serial transport, SQLite history, the async
  service, the FastAPI API (status / history / WebSocket / setpoint control),
  and a live uPlot dashboard. Runs against real serial or the `--sim` board.
- **Phase 2 - Control + config (DONE, verified on hardware 2026-05-31):**
  Settings drawer in the dashboard with probe naming, calibration offsets, PID
  tuning, per-probe alarm thresholds, and manual fan mode. New API endpoints:
  `/api/manual`, `/api/probe-name`, `/api/offsets`, `/api/pid`, `/api/alarms`,
  `/api/config`. Auto-sends `/config` on link-up so identity/config populate
  without a manual poke. Hardened the read path to reject bad-checksum lines
  (a corrupted `$HMPN` had shifted probe-name fields; checksums now enforced).
  All controls verified round-tripping into the real board.
- **Phase 2.5 - Polish + research features (DONE, verified on hardware 2026-05-31):**
  - Setpoint button overflow CSS fixed (control row wraps within the card).
  - Fan & Servo settings card: min/max fan, max startup, fan floor, servo
    min/max/ceil, invert-fan/servo flags (`/api/fan`, packs flags bitfield).
  - Probe TYPE selection with the firmware's Steinhart-Hart presets
    (ThermoWorks Pro, Maverick ET-72/732, Radio Shack, Vishay, EPCOS, Semitec)
    + disable (`/api/probe-type`, `/api/probe-presets`).
  - Lid-detection settings card (`/api/lid`).
  - Cook **Sessions**: auto-start on data, auto-close after 30 min idle, named,
    searchable, renamable, deletable history (`/api/sessions*`). Samples tagged
    by session; history filterable by session. SQLite migration self-heals an
    older `samples` table (adds `session_id`).
  - **Time-to-done prediction** (`/api/predict`): least-squares rate-of-rise
    with confidence grading; food cards show ETA to target.
  - **Meat/doneness presets** (~14 USDA targets) + pit quick-picks
    (`/api/presets`); food-target picker writes the high alarm.
  - **Timeline notes** rendered as chart markers (`/api/notes`).
  - **CSV export**, full resolution, per-session or all (`/api/export.csv`).
  - **Alarm notifications**: service detects $HMAL ringing edges and pushes
    events over the WebSocket; dashboard shows a toast + browser Notification.
  - Frontend rewritten XSS-safe (no innerHTML; DOM built via helpers).
  44 backend tests pass.
- **Phase 2.6 - Bugfixes + feature-completeness + auto-tune (DONE, verified on
  hardware 2026-05-31):**
  - **FIXED: stale frontend.** The deployed `app.js` had silently reverted to the
    9.6KB Phase-2 version (lost probe-types/sessions/predictions), which is why
    "probe presets don't show" and the fan card appeared to do nothing. Rewrote
    the full 26.6KB `app.js`; verified the SERVED file contains every feature.
  - **FIXED: fan stuck at 40%.** The board's EEPROM had `fanMaxSpeed=40`, which
    the firmware uses as a hard scaling cap on BOTH auto and manual output
    (confirmed by direct serial test: max=40 -> 40% ceiling; max=100 -> 100%).
    The Fan card now actually writes it; set to 100 on the live unit.
  - **Config now parsed completely**: added `$HMPC` parsing (per-probe type +
    Steinhart-Hart coeffs) to `state.py` - the board reports 4 probe types
    (0 Disabled, 1 Thermistor, 2 RF, 3 Thermocouple). The pit on this unit is a
    **thermocouple (type 3, AD8495)** - presets must not overwrite it with a
    thermistor curve. Probe-type labels now shown in the UI.
  - **Robust config retrieval**: `service` re-requests `/config` every 4s until
    an `$HMFN` arrives (mirrors the original linkmeterd retry), so config
    reliably populates on the noisy link instead of sometimes coming up empty.
  - **PID presets** (HeaterMeter default, Kamado, Kettle, Offset) and **blower
    presets** (Standard, Quiet, Gentle Startup, High Output) via `/api/presets`.
  - **PID AUTO-TUNE** (relay / Astrom-Hagglund method): `autotune.py` +
    `/api/autotune` (GET status / POST start / DELETE cancel). Drives the blower
    as an on/off relay around the setpoint, measures the pit oscillation
    (amplitude + period via a peak detector), computes Ku/Tu -> Kp/Ki/Kd with a
    selectable rule (Tyreus-Luyben default, Ziegler-Nichols, No-Overshoot), and
    writes the result. Safety: pit-ceiling abort, wall-clock timeout, returns to
    auto on finish. UI section with live progress in the settings drawer.
  - 59 backend tests pass. Probe-type + fan + autotune round-trips verified live.
- **Phase 3 - Integrations (DONE earlier this session)**: MQTT + Home Assistant
  discovery; deployed dormant (enable with `--mqtt-host`). See above.
- **Phase 2.7 - Net Info + dashboard probe UX (DONE, verified in Chrome on
  hardware 2026-05-31):**
  - **FIXED the LCD "Offline" net-info gap.** The board's Net Info screen is
    host-driven: it emits `$HMHI,<opaque>,<topic>,<button>` and shows "Offline"
    if the host doesn't reply with `/set?hi=<opaque>,<l1>,<l2>` within 800ms.
    The daemon now parses `$HMHI` and replies immediately on the read path
    (`hostinteractive.py` + `service._handle_host_interactive`). Screens: title,
    IP address, hostname; UP/DOWN scroll. Detects the Pi IP (UDP-connect trick)
    + hostname; toasts the IP on change; light `/ucid` keepalive every 60s keeps
    the board "online". Mirrors the original ipwatch.lua.
  - **Inline probe rename on the dashboard** - click any probe name (pit/food/
    ambient) to rename it (matches the original's jeditable behaviour). Round-
    trips to the board, verified live (renamed Probe 1 -> Brisket).
  - **Quick food-target dialog with meat-preset chips** - "Set target" button on
    each food card opens a dialog of preset quick-picks (Brisket 203, etc.) +
    a manual field; writes the probe's high alarm and drives time-to-done.
    Verified live (Brisket 203 -> board -> card shows ->203deg).
  - **Predictor sanity-gated**: suppresses ETAs when the probe rises < 2 deg/hr
    or projects > 48h (was showing "161511h" on a flat probe).
  - **Browser cache fix**: app HTML/JS/CSS served `no-cache` (vendor immutable);
    Save handler guards a missing SAVERS fn; error messages scroll into view +
    toast so they're never hidden above the fold.
  - 65 backend tests pass.
- **Phase 2.8 - "Worth doing" research-feature batch (DONE, verified in Chrome
  on hardware 2026-05-31):**
  - **Multi-stage cook programs** (`cookprogram.py`): ordered stages, each a pit
    setpoint + advance condition (probe-hits-temp / time-elapsed / manual).
    Subsumes keep-warm (a low-setpoint final stage) and auto-shutdown (an "off"
    stage -> manual 0%). Runner ticks on each HMSU; events broadcast over WS.
    Saved templates persist (`programs` table). API: `/api/program/{start,
    advance,stop}`, `/api/program` status, `/api/programs` CRUD. Full stage-
    builder UI in a new drawer + a running banner. Verified live (built + saved
    "Test Brisket", 2 stages).
  - **Public shareable cook links** (FireBoard-style): `/api/sessions/{id}/share`
    mints a token; `/share/{token}` serves a read-only public page (own minimal
    `share.html`) backed by `/api/share/{token}` (safe fields only). Revocable.
    Share button on each session row copies the link. Verified live (public page
    rendered the chart with no auth; disabling -> API 404).
  - **Session compare overlay**: a "Compare" dropdown overlays a past cook's pit
    curve on the live chart, resampled by ELAPSED time to align starts (6th uPlot
    series). Only finished sessions are offered.
  - **Stall-aware S-curve predictor** (`predict_scurve` + `predict` dispatcher):
    Newton's-law-of-cooling fit (meat approaches cooker temp exponentially) that
    captures the stall; food probes use it with the pit temp as the environment,
    falling back to linear. Carries model + eta_low/eta_high band. Verified the
    fit recovers true time-to-target within 10%.
  - **PWA**: manifest + icon + service worker (`sw.js`, network-first shell,
    relays alarm notifications even when backgrounded). Dashboard is installable.
    CAVEAT: the Pi serves plain HTTP on the LAN, so browsers DISABLE the service
    worker (non-secure origin) - `registerServiceWorker()` guards on
    `isSecureContext`. In-page notifications still work; true background push
    needs HTTPS + VAPID (future). Manifest/install confirmed in Chrome.
  - **Firmware feature audit follow-ups** (full audit by background agent;
    probe-name->LCD passthrough confirmed FIRMWARE-AUTOMATIC, no host work):
    now parse the previously-ignored sentences - `$HMRF` (decoded RF
    transmitters: node/low-battery/reset/native/rssi), `$HMPS` (PID internals
    breakdown), `$HMAR` (per-probe ADC noise), and decoded `$HMLB`
    (backlight/home-mode/LEDs). New commands: `/api/lcd` (`lb` - backlight, home
    display mode, 4 LED stimuli), `/api/pid-internals` (`tp` toggle), `/api/lid/
    {open,cancel}` (manual lid trigger). `/api/lcd-options` exposes the 17 LED
    stimuli + 6 home modes. Verified live: board's $HMLB/$HMAR parsed correctly
    (LEDs FanMax/LidOpen/FanOn/Off, quiet ADC). No RF probes so rf_sources empty.
  - 85 backend tests pass. Full audit doc: see the firmware-audit agent output.
- **Firmware Phase A (DONE 2026-05-31, no board touched):** Assessed updating
  the board's AVR firmware (`20201120B` -> source `20210202`). Delta is just 2
  commits, only 1 functional: a 4-line LCD-menu-nav fix. Built the `20210202`
  firmware reproducibly from source (arduino-cli + avr-gcc 7.3.0; fits 78% flash);
  saved verified hex+sha to `heatermeter-modern/firmware/`. Full writeup:
  `FIRMWARE-UPDATE.md`. **Phase B (actual flash) is SUPERVISED - tomorrow with
  the user** (backup EEPROM/flash first; flash over Pi SPI; rollback ready).
- **Phase 2.9 - Diagnostic UIs (DONE, verified in Chrome on hardware 2026-05-31):**
  - **PID internals live card** - a "Show PID internals" toggle in settings
    enables `tp=1`; the board streams `$HMPS` and a dashboard card shows live
    P/I/D contribution bars (sum = output %). Pref persists in localStorage and
    re-asserts on connect. Verified live (board streamed P=1126/I=9/D=-0).
  - **LCD & LEDs settings card** - backlight, home display mode (2-line/4-line/
    BigNum), and the 4 LED stimulus mappings + invert, populated from decoded
    `$HMLB` and `/api/lcd-options`. Verified live (showed the board's real config:
    backlight 40, 2-line home, LEDs FanMax/LidOpen/FanOn/Off).
- **Phase 4 - Cutover / polish (NEXT)**: install script, MQTT-aware systemd
  unit, optional Grafana. Interface revamp (user wants clean/usable on any
  device) - the REST+WS API is framework-agnostic so a Svelte rebuild is a
  drop-in. Optional RF transmitter panel (when user has RF probes). HTTPS +
  VAPID for true away-from-home push.
- **Phase 4 - Cutover**: systemd packaging, install script, docs; switch the
  real cook over; keep the old SD as rollback.
- **Phase 5 (optional)**: OTA firmware flashing from the Pi via `avrdude`.

## Status

| Component | File | State |
|---|---|---|
| Protocol parse/encode | `backend/heatermeterd/protocol.py` | Done + tested |
| State model | `backend/heatermeterd/state.py` | Done + tested |
| Serial transport | `backend/heatermeterd/serial_io.py` | Done (PTY + pyserial) |
| Transport links | `backend/heatermeterd/links.py` | Done (SerialLink + SimLink) |
| SQLite history | `backend/heatermeterd/store.py` | Done + tested |
| Service (glue/broadcast) | `backend/heatermeterd/service.py` | Done + tested |
| FastAPI app | `backend/heatermeterd/api.py` | Done (smoke-tested) |
| Daemon entry point | `backend/heatermeterd/main.py` | Done |
| Dashboard (uPlot) | `backend/static/` | Done (vendored uPlot) |
| Board simulator | `backend/tools/hmsim.py` + `sim.py` | Done |
| Monitor / capture | `backend/tools/hmmonitor.py` | Done |
| Command sender | `backend/tools/hmsend.py` | Done |
| Log replay | `backend/tools/replay.py` | Done |
| Test suite | `backend/tests/`, `backend/run_tests.py` | 26 tests, green |
| systemd unit | `deploy/heatermeterd.service` | Drafted |
| Svelte dashboard | `frontend/` | Deferred polish |

## What we deliberately drop / do not touch

- **Drop:** the OpenWrt image, Lua/LuCI, RRDtool, jQuery/Flot, the 8192cu WiFi
  driver, the Pebble app, the C# Windows tools.
- **Do not touch:** the AVR firmware + board. Keep it on upstream so firmware
  updates still flow normally.

## Phase 3 - Home Assistant via MQTT (DONE, deployed 2026-05-31)

`heatermeterd/mqtt.py` + `tests/test_mqtt.py` + `tests/test_mqtt_service.py`:
MQTT bridge with Home Assistant auto-discovery. Publishes 4 temp sensors +
fan% sensor + lid binary_sensor + a writable setpoint `number` entity, all under
one HA device, with availability/LWT online/offline. Subscribes to the setpoint
command topic so HA can change the pit target; the command hops onto the event
loop (`service.mqtt_set_setpoint` -> `loop.call_soon_threadsafe`) before touching
the link. Discovery + state-flatten are pure and unit-tested with a fake client;
an integration test drives service+bridge+SimLink end to end. Wired into
`service.start/stop/_on_line`; enabled via `main.py --mqtt-host` (also
`--mqtt-port/-user/-pass/-node`). **MQTT is OFF unless `--mqtt-host` is passed**,
so the deployed daemon behaves identically until configured. `paho-mqtt` is
installed in the Pi venv. 52 backend tests green.

**To turn MQTT on** (when the user has a broker / Home Assistant): edit the
systemd unit's ExecStart to add `--mqtt-host <broker-ip>` (+ creds if any),
`sudo systemctl daemon-reload && sudo systemctl restart heatermeterd`. HeaterMeter
then appears in HA automatically as a device with live temps, fan%, lid state,
and a controllable setpoint. No HA YAML needed (discovery handles it).

## Handoff note (2026-05-31, end of session)

**Deployed + verified live on the Pi** (http://192.168.3.164:8080/, service
active + enabled, 4 probes reading): Phases 1, 2, 2.5, and 3 code. MQTT is built,
tested, deployed, and dormant (no `--mqtt-host` yet). 52 backend tests green.

**Next session candidates:** turn on MQTT against the user's Home Assistant;
Phase 4 cutover polish (install script, make systemd unit MQTT-aware); research
Tier-3 items (public shareable cook links, session compare overlay, PWA + web
push for phone alerts away from the dashboard). Lower priority: stall-aware
S-curve upgrade to the predictor (currently linear rate-of-rise).

**Deploy reminders:** ALWAYS migrate the sqlite schema (PRAGMA table_info +
ALTER TABLE) — a missing column crash-looped the daemon this session. The
PreToolUse security hook blocks writing JS with innerHTML; build DOM via helpers.
Deploy = rsync (exclude .venv data *.sqlite *.log __pycache__) + `sudo systemctl
restart heatermeterd`; verify over HTTP (curl) since SSH to the Pi is flaky.

**Deploy reminder:** ALWAYS migrate the sqlite schema (PRAGMA table_info +
ALTER TABLE), never assume a fresh DB — a missing column crash-looped the daemon
this session. And the PreToolUse security hook blocks writing JS with innerHTML;
build DOM via helper functions instead.
