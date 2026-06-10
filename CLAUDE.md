# heatermeter-modern - Claude Code project brief

Persistent context for sessions on this project. Read fully at the start of
every session.

## What this is

A from-scratch rewrite of the Raspberry Pi software for the **HeaterMeter** BBQ
temperature controller. The upstream project (cloned at
`/Users/btallack/Documents/GitHub/HeaterMeter`) is current but its Pi-side stack
is dated: a custom OpenWrt image running a Lua/LuCI daemon, RRDtool, jQuery/Flot,
and a 2012 Realtek WiFi driver. This project replaces all of that with a modern
Linux application on standard Raspberry Pi OS.

**Hard constraint: the board and its ATmega328 firmware are not modified.** The
user has working hardware. The new software is a drop-in replacement for the Pi
side only, talking to the board over its existing serial protocol.

## The contract

Everything hinges on the serial protocol in [PROTOCOL.md](PROTOCOL.md),
implemented in `backend/heatermeterd/protocol.py`. The board emits checksummed
`$HM*` NMEA-style sentences at ~1 Hz over UART (38400 8N1); the host sends
URL-style `/set?...` command lines back. The board doesn't care what software is
on the Pi, which is why this rewrite is safe.

## Stack (decided with the user)

- OS: Raspberry Pi OS Lite 64-bit on a **Raspberry Pi 3** (onboard WiFi).
- Backend: Python 3 + asyncio + FastAPI (REST + WebSocket).
- Serial: pyserial on the Pi; raw PTY/file for hardware-free dev.
- Storage: SQLite first; InfluxDB/Grafana optional later.
- Frontend: Svelte + uPlot.
- Integrations: MQTT + Home Assistant discovery; ntfy/Telegram notifications.
- Packaging: systemd service.

## Development without hardware

`backend/tools/hmsim.py` is a board simulator. `--pty` mode exposes a real
pseudo-terminal that behaves like the board (emits status, accepts commands), so
the full stack can be built and tested on a laptop. The integration test
(`tests/test_integration_pty.py`) exercises the real transport over a PTY.

## Conventions

- `protocol.py` and `state.py` are **pure** (stdlib only, no I/O) so they stay
  trivially testable. Keep them that way; put I/O in `serial_io.py` / the API.
- Tests must pass via `python3 backend/run_tests.py` (no third-party deps) and
  under pytest.
- Copy style: no em dashes, no emojis (matches the user's house style).

## Roadmap + status

See [PLAN.md](PLAN.md). Phase 1 (read-only daemon) is in progress: protocol,
state, serial transport, simulator, and tooling are done and tested. Next up is
the FastAPI service and the Svelte dashboard, then Phase 0 bench capture once the
user's SD card is flashed.

## Deployment safety

The user's working HeaterMeter stays on its current SD card untouched. All
development targets a separate SD card / Pi. Nothing here auto-updates the
working unit. Cutover (Phase 4) is a deliberate, manual SD swap with the old
card kept for instant rollback.
