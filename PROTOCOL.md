# HeaterMeter serial protocol

This is the contract between the ATmega328 firmware (unchanged) and this host
software. It is transcribed from the upstream firmware source
(`arduino/heatermeter/README.txt` and `hmcore.cpp`) and is implemented and
tested in [`backend/heatermeterd/protocol.py`](backend/heatermeterd/protocol.py).

> Status: transcribed from source AND verified against live hardware on
> 2026-05-31 (Pi 3 -> ttyAMA0 @ 38400). A capture produced HMSU and HMAR
> sentences; all parsed with 0 bad checksums.
>
> **Live-hardware delta:** the firmware emits `U` (a single letter, for
> "unplugged") for an empty/disabled probe field, NOT a blank between commas as
> the upstream README example (`$HMXX,,,,,,*DB`) implied. Real line:
> `$HMSU,375,74.5,U,U,U,100,99,0,30,0*4D`. The parser decodes any non-numeric
> token (`U`, blank, etc.) to `None`, so it is robust regardless.

## Link layer

| Property | Value |
|---|---|
| Interface | UART (TTL serial) |
| Baud | 38400 |
| Framing | 8N1 |
| Device on Pi | `/dev/serial0` (preferred), historically `/dev/ttyAMA0` / `/dev/ttyS0` |
| Line ending | bare `\n` (LF) from the board; commands accept CR / LF / CRLF |

## Sentence framing (board -> host)

```
$<TYPE>,<field>,<field>,...*<CK>\n
```

- `<TYPE>` is 4 chars: a 2-char talker id + 2-char message id (e.g. `HMSU`, `UCID`).
- Fields are comma separated. A blank field means "not present / disabled".
- `<CK>` is the two-uppercase-hex-digit XOR of every character between `$` and `*`.
- Lines terminate with a single `\n`.

Example: `$HMSU,225,198.5,145.0,,72.0,35,33,0,35,50*7B`

## Sentences the board sends

| Type | Fields | Meaning |
|---|---|---|
| `$HMSU` | SetPoint, Pit, Food1, Food2, Ambient, OutputPct, OutputMovAvg, LidOpenCountdown, FanPct, ServoPct | **State update. The ~1 Hz heartbeat.** |
| `$UCID` | "HeaterMeter", Version(+BoardRev) | Identity. Version has the board revision char appended (e.g. `20210202B`). |
| `$HMPD` | 0, PidP, PidI, PidD, Units | PID coefficients + temperature units (F/C). |
| `$HMPN` | Probe0, Probe1, Probe2, Probe3 | Probe names (0=pit 1=food1 2=food2 3=ambient). |
| `$HMPO` | Probe0..3 | Probe calibration offsets. |
| `$HMPC` | ProbeIdx, A, B, C, R, Type | Probe Steinhart-Hart coefficients + type. |
| `$HMAL` | Low0, High0, Low1, High1, ... | Alarm thresholds. `L`/`H` suffix = ringing; negative = disabled. |
| `$HMFN` | Low, High, ServoMin, ServoMax, Flags, MaxStartup, FanActiveFloor, ServoActiveCeil | Fan/servo output params. |
| `$HMLB` | Backlight, HomeMode, LED0, LED1, LED2, LED3 | Display params. |
| `$HMLD` | OffsetPercent, Duration | Lid-detect params. |
| `$HMPS` | cPidB, cPidP, cPidI, cPidD, tempD | PID internals (sum the cPID* terms for the output). |
| `$HMRF` | 255, 0, CrcStatus[, NodeId, Flags, Rssi ...] | RF wireless probe status. |
| `$HMRM` | SourceId x4 | RF source mapping. |
| `$HMLG` | Message | Debug log line. |
| `$HMHI` | Topic, HostOpaque, Button | Interactive LCD menu event (host-driven menus). |
| `$HMAR` | range x4 | ADC noise/range report (diagnostic). |

## Commands the host sends

Plain URL-style lines starting with `/`, terminated by a newline. **Not
checksummed.** Max length 63 bytes.

| Command | Effect |
|---|---|
| `/set?sp=<n><U>` | Setpoint to `<n>` with unit U = F/C/R/A. Negative = manual output mode (`-0` = 0%). |
| `/set?pid<x>=<v>` | Tune PID constant x = b/p/i/d. |
| `/set?pn<i>=<name>` | Set probe i name. |
| `/set?po=<a>,<b>,<c>,<d>` | Probe offsets. Blank entries keep current value (e.g. `po=,,,-2`). |
| `/set?pc<i>=<A>,<B>,<C>,<R>,<TRM>` | Probe coefficients + type. Type: 0=off 1=internal 2=RFM12B; 128+ = RF node id+128; 255 = any. |
| `/set?al=<L>,<H>[,...]` | Alarm thresholds. Negative disables; 0 silences + disarms. |
| `/set?fn=<FL>,<FH>,<SL>,<SH>,<Flags>,<MSS>,<FAF>,<SAC>` | Fan/servo params. |
| `/set?lb=<A>,<B>,<C>[,...]` | Display: backlight, home mode, LED config bytes. |
| `/set?ld=<A>,<B>,<C>` | Lid detect: offset %, duration s, active flag. |
| `/set?tt=<L1>[,<L2>]` | Show a temporary toast message on the LCD. |
| `/set?tp=<A>` | Temp param (e.g. enable `$HMPS` PID-internals logging). |
| `/config` | Dump version, probe names, RF map (serial-only). |
| `/reboot` | Reboot the MCU (only if wired for it). |

## Firmware defaults (from `hmcore.cpp`)

For sanity-checking a fresh board:

- Setpoint: `225` F
- PID constants: Kb=`0`, Kp=`4.0`, Ki=`0.02`, Kd=`5.0`
- Units: `F`
- Lid open: offset `6%`, duration `240 s`
- Fan: min `0%`, max `100%`; servo range `1000-2000 µs`
- Default probe curve: ThermoWorks Pro-Series Steinhart-Hart coefficients,
  10k divider.
