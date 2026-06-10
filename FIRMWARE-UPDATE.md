# Firmware update assessment (Phase A)

Read-only analysis of updating the HeaterMeter board's AVR firmware. **No board
was touched.** Flashing (Phase B) is a separate, supervised step to be done with
the user present, with a backup and rollback ready.

## TL;DR

**The functional firmware delta between your board and the latest source is a
single 4-line LCD-menu navigation fix.** Everything else our host software needs
is already present on your board. An update is low-risk and low-reward: nice to
be current, but nothing is blocked. Do it only as a deliberate, supervised bench
exercise.

## Versions

| | Version | Date | Source commit |
|---|---|---|---|
| **Your board** (from `$UCID`) | `20201120` (rev `B`) | 2020-11-20 | `10914a9` |
| **Latest source** (`HM_VERSION`) | `20210202` | 2021-02-02 | `ddf98db` (repo HEAD for the firmware) |

The `B` in "20201120B" is `HM_BOARD_REV` - a compile-time hardware-revision tag
appended at runtime (`hmcore.cpp:509-510`), NOT a firmware variant. So your board
is exactly the `10914a9` source state.

The firmware source has not changed since Feb 2021; later repo commits (through
Aug 2025) touch only 3D-print files and the separate ESP32 "MeterMonitor"
project, not `arduino/heatermeter/`.

## The complete delta: 2 commits

```
7525d29  [hm] Arduino 1.8.12              <- VS project file ONLY, zero firmware change
ddf98db  [hm] Fix missing Reset Config LCD menu item ...  <- the only functional change
```

`7525d29` edits `heatermeter.vcxproj` (a Visual Studio project file) - it does not
change a single line of compiled code.

`ddf98db` is the only functional change. It is a 4-line fix to `hmmenus.cpp`
(plus the `HM_VERSION` bump):

```diff
   { ST_LIDOPEN_DUR, BUTTON_LEFT | BUTTON_TIMEOUT, ST_HOME_FOOD1 },
-  { ST_LIDOPEN_DUR, BUTTON_RIGHT, ST_NETINFO },
+  { ST_LIDOPEN_DUR, BUTTON_RIGHT, ST_RESETCONFIG },
 ...
 static state_t menuNetInfo(button_t button)
 {
   if (Menus.getHostState() == HmMenuSystemHostState::OFFLINE)
-    return ST_RESETCONFIG;
+    return ST_LCDBACKLIGHT;
```

### What that fix actually does

It's purely LCD-menu navigation:

1. **Restores the "Reset Config" menu item.** In your firmware, pressing Right at
   the "Lid Open Duration" menu jumps straight to "Net Info", skipping the
   "Reset Config" item. The fix makes Right go to "Reset Config" (then "Reset
   Config" -> Right -> "Set Point", and "Net Info" is reached the normal way).
   Net effect on your board: the **"Reset Config" item is currently hard to reach
   from the front panel** (you can still reset config via the host/web UI, so this
   is cosmetic for our setup).

2. **Skips the Net Info screen gracefully when the host is offline.** When the
   host hasn't answered (`OFFLINE`), the menu now advances to "LCD Backlight"
   instead of looping back to "Reset Config". This avoids the front-panel menu
   getting stuck/looping when no Pi is talking to it.

Both are front-panel quality-of-life. Neither changes the serial protocol, the
PID control, probe handling, or anything our host software depends on. Our host
already handles the Net Info handshake, so even the offline-skip is moot for us.

## Why an update is low priority

- **Nothing is blocked.** Every feature we've built works against `20201120B`.
  The board already emits `$HMPC` (with units), `ServoPct` in `$HMSU`,
  `$HMND`/`$HMAR` noise, supports the `$HMHI` Net Info handshake, etc. - all the
  newer-firmware features are already on your board; they predate the version bump.
- **The only change is a front-panel menu tweak**, and we drive almost everything
  from the web UI anyway.
- **Flashing is the one operation that can brick the hardware**, so the
  risk/reward is poor for a cosmetic menu fix.

## Build config (for Phase B)

The board's firmware is compiled with these feature flags (`hmcore.h:7-11`):

```c
#define HEATERMETER_SERIAL 38400       // serial @ 38400
#define HEATERMETER_RFM12  RF12_915MHZ // RFM12B receive, 915 MHz
#define PIEZO_HZ 4000                  // piezo buzzer
#define SHIFTREGLCD_NATIVE             // native shift-reg LCD (HM PCB < v3.2)
```

- MCU: ATmega328P @ 16 MHz (`Makefile`)
- Libraries (all present in `arduino/libraries/`): ShiftRegLCD, rf12_itplus,
  digitalWriteFast, jeelib
- A prebuilt `hm.hex` exists at
  `openwrt/package/linkmeter/targets/bcm2708/lib/firmware/hm.hex` (71359 bytes)
  but it predates a from-source rebuild; Phase A rebuilds from source to verify.

**Important build note:** `SHIFTREGLCD_NATIVE` is set for HeaterMeter PCB
revisions BELOW 3.2. We must confirm the board's PCB revision before flashing -
a wrong LCD mode would garble the display. The `B` board-rev tag and the fact the
current firmware uses NATIVE suggests this is correct, but verify against the
physical board at the bench.

## Flash mechanism (Phase B, supervised)

The Pi flashes the AVR over SPI (`/dev/spidev0.0`) using `hmdude` (a stripped
avrdude by the HeaterMeter author), driven by `avrupdate`
(`openwrt/package/linkmeter/root/usr/bin/avrupdate`). Key points from that script:

- Uses `/dev/spidev0.0` (the Pi's hardware SPI, wired to the AVR's ISP header on
  the HeaterMeter PCB - no external programmer needed).
- **Preserves EEPROM** (your probe calibration, names, PID, etc.): for a board
  with "Arduino fuses" it rewrites fuses to `lfuse 0xff / hfuse 0xd7 / efuse 0x05`
  (hfuse 0xd7 keeps EEPROM through a chip erase) before flashing.
- We are NOT on OpenWrt, so `avrupdate` won't run as-is; for Phase B we replicate
  its logic with a host tool (avrdude over `linuxspi`, or build hmdude) on the
  current Raspberry Pi OS. The Pi 3's SPI must be enabled (`dtparam=spi=on`).

## Phase B plan (TOMORROW, with the user present)

1. **Back up first** (read-only): dump the current flash AND EEPROM off the board
   via SPI before writing anything. This is the rollback.
2. Confirm the PCB revision -> confirm `SHIFTREGLCD_NATIVE` is correct.
3. Enable SPI on the Pi (`dtparam=spi=on`), install a flasher (avrdude w/ linuxspi
   or build hmdude), wire-check `/dev/spidev0.0`.
4. Flash the verified `20210202` hex with EEPROM-preserving fuses.
5. Verify: read back, confirm `$UCID` reports `20210202`, run a smoke cook, check
   the LCD + all probes + the web UI.
6. Rollback path: re-flash the backed-up `20201120B` image if anything is off.

## Phase A status - COMPLETE

- [x] Identified exact version delta (board `20201120` -> source `20210202`).
- [x] Confirmed the delta is 2 commits, only 1 functional (a 4-line LCD menu fix).
- [x] Documented the build config + flash mechanism + EEPROM preservation.
- [x] **Reproducible from-source build succeeds.** Toolchain: arduino-cli 1.5.0 +
      arduino:avr@1.8.8 (avr-gcc 7.3.0). Built with `arduino-cli compile --fqbn
      arduino:avr:uno --libraries <repo libs>`. The HeaterMeter feature flags
      (serial, RFM12 915MHz, piezo, SHIFTREGLCD_NATIVE) come from `hmcore.h`, so
      no extra build flags are needed.
- [x] **Verified fit:** 25336 bytes / 78% of 32256 flash; 1347 bytes / 65% RAM.
      Comfortable margin.
- [x] **Artifacts saved** to `heatermeter-modern/firmware/`:
      - `heatermeter-20210202.hex` (71290 bytes)
      - `heatermeter-20210202.elf`
      - `heatermeter-20210202.hex.sha256` =
        `7b34efba9bdd6c166cf209d057c8fdff68bab9c59fcfe807301a6225a281ebea`

### To rebuild identically

```
arduino-cli core install arduino:avr           # avr-gcc 7.3.0 toolchain
# stage sketch + repo libraries into a build dir, then:
arduino-cli compile --fqbn arduino:avr:uno \
  --libraries <repo>/arduino/libraries \
  --build-path <out> <sketch-dir>
# hex appears at <out>/heatermeter.ino.hex
```

(Note: `arduino:avr:uno` matches the HeaterMeter target - ATmega328P @ 16MHz,
standard variant - identical to the repo Makefile's `MCU=atmega328p VARIANT=
standard F_CPU=16000000`.)

Recommendation: **proceed with the build verification, but treat the actual flash
as optional.** The functional payoff is one front-panel menu fix. If the user
wants to be fully current and de-risk any future "host expects newer firmware"
edge case, it's a clean, tiny update - but there is no urgency.

---

# In-software firmware updater

The manual recipe above is now driven from the web app: Settings -> Firmware
lists vetted images and flashes the selected one with a backup taken first, live
progress, and one-click rollback. The actual flash remains a **supervised**
one-click action: keep the controller powered and stay nearby until it reports
success.

## Architecture

Three privilege domains, talking through a group-writable spool under the data
dir (not `/tmp`, because the daemon unit sets `PrivateTmp=true`):

```
heatermeterd (user brennan, NoNewPrivileges) -- cannot escalate
  | writes data/firmware/spool/request.json, tails <job>.progress.jsonl
  v
systemd hm-flash.path watches request.json -> starts hm-flash.service (root)
  v
/usr/local/sbin/hm-flash (root): dtoverlay + avrdude + gpioset
  reads only /usr/local/share/heatermeter/firmware/{manifest.json,*.hex}
```

- The daemon never flashes and never gains privilege. It guards (refuses while
  cooking), snapshots config, idles the cooker, pauses serial, and writes the
  request. It then tails the helper's progress and rebroadcasts it over the
  WebSocket.
- The helper is the only privileged actor. It re-verifies the image's sha256
  against its own trusted, root-owned manifest before writing anything, and a
  `trap cleanup EXIT` GUARANTEES SPI is turned back off and the LCD is re-init
  pulsed on every exit path.
- Only the `dtoverlay` overlay toggle needs root. `avrdude` and `gpioset` reach
  `/dev/spidev0.0` and `/dev/gpiochip0` via the `spi`/`gpio` group membership the
  daemon user already has, so the privileged surface is tiny.

## Runtime SPI toggle (no reboot)

The helper enables SPI at runtime with `dtoverlay spi0-2cs` (the runtime
equivalent of `dtparam=spi=on`) and removes it afterward by the overlay index it
captured. This avoids the two Pi reboots the old manual recipe needed, so the
web server stays up for the whole flash. The UART (`/dev/serial0`) does not
conflict with SPI, so the daemon keeps running; it just pauses ingestion so it
does not read the reset garbage.

### Step 0 validation gate (SUPERVISED, do before trusting the no-reboot path)

The no-reboot design rests on the runtime overlay actually producing
`/dev/spidev0.0`. Confirm once, by hand, with the user present:

```
ls /dev/spidev*                         # absent (SPI off)
sudo dtoverlay spi0-2cs
ls -l /dev/spidev0.0                     # present, group spi, 0660
avrdude -c linuxspi -p m328p -P /dev/spidev0.0:/dev/gpiochip0:25 -B 400kHz
                                         # must print 0x1e950f (run as brennan, no sudo)
dtoverlay -l                             # note the spi0-2cs index
sudo dtoverlay -r <index>
ls /dev/spidev*                          # gone
gpioset -c gpiochip0 --toggle 60ms,0 25=0  # LCD re-inits; web UI stayed up
```

If all pass, the helper's default `spi0-2cs` overlay is correct. If
`/dev/spidev0.0` does not appear without a reboot, set `HM_FLASH_OVERLAY` to the
working overlay or fall back to a reboot path with a loud UI warning that the LCD
stays frozen until reboot.

**Step 0 result: PASS (validated on hardware 2026-06-02).** `dtoverlay spi0-2cs`
created `/dev/spidev0.0` (group `spi`, 0660); the sig-gate read `0x1e950f` running
as the unprivileged `brennan` user (no sudo), confirming group access; removal by
index plus the GPIO25 pulse restored the LCD with the web server up throughout.
The full updater was then validated end to end: dry-run backup, a real hm3
re-flash (verify + idempotent restore), a real EEPROM-reset flash to hm1 (the
auto-restore recovered the pit's type-3 calibration to 73.6 F instead of the ~437 F
a defaulted thermistor would read), and a rollback hm1 -> hm3 that recovered config
again. The board ended on hm3B with names, pit type 3, and alarms intact.

### Bugs found and fixed during the first supervised run

The unit tests cannot exercise `apt`/`avrdude`/`systemd`, so the first live run
surfaced three issues, all fixed in `deploy/hm-flash` / `deploy/install.sh`:

1. `install.sh` now runs `apt-get update` before installing `jq` (the bare
   install failed on a stale index, and the helper hard-depends on `jq`). The
   helper also now writes an error result if `jq` is missing instead of hanging.
2. The sig-gate grepped avrdude's stdout, but avrdude logs to stderr; it now
   folds stderr into the pipe (`2>&1 | tee -a "$LOG" | grep`).
3. A `backup` (dry run) still resets the board via the programmer, so the daemon
   now idles the cooker after a backup too (not only after a flash/rollback).

## EEPROM and config restore

Bumping `EEPROM_MAGIC` (firmware/src/heatermeter/hmcore.cpp) resets the board's
EEPROM to defaults. Because whether the magic actually changes depends on the
version transition (a downgrade/rollback always resets it), the daemon snapshots
the live config before every flash and re-sends it afterward. Re-sending is
idempotent when EEPROM was not reset. The pit (probe 0) is an AD8495 thermocouple
(type 3); the restore replays `/set?pc0=A,B,C,R,3` first so the pit reads
correctly, and idles the cooker (`/set?sp=O`) last because a board reset resumes
the stored setpoint.

## Bundled images + manifest

`firmware/manifest.json` lists each vetted image (version, file, sha256,
changelog, eeprom_reset, board_rev, min_compat). Run `bash firmware/gen-manifest.sh`
after building or replacing a `.hex` to refresh the sha256 fields so the
committed manifest always matches the committed hexes. `deploy/install.sh` copies
the images + manifest to the trusted root-owned dir and installs the helper and
the two systemd units.

## Files

- `backend/heatermeterd/firmware.py` - manifest/sha/guard/IPC pure logic
- `backend/heatermeterd/service.py` - `start_firmware_flash` + poll + restore
- `backend/heatermeterd/api.py` - `/api/firmware*` endpoints
- `deploy/hm-flash` - the root flash helper (the privileged actor)
- `deploy/hm-flash.path` / `deploy/hm-flash.service` - reference units (install.sh
  generates the installed copies with this host's paths)
- `firmware/manifest.json` + `firmware/gen-manifest.sh`
- `frontend/src/lib/Settings.svelte` - the Firmware card
