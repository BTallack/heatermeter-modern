# HeaterMeter iOS app — status & handoff

_Updated 2026-09-15. All four screens verified live against the Pi in the simulator._

## TL;DR

A full native SwiftUI **control app** (all four screens) is written and committed
to `main`. It's a second client of the existing REST + WebSocket API — no backend
changes. It **compiles clean** (Xcode 27 / iOS 27 SDK) and **every tab has been
verified rendering live data** from the Pi (2026-09-15): Dashboard, Graph, Cook,
Settings. Fixes made during the tap-through:

- Multi-variable `@State` declaration in SettingsView (compile error); `NSOrderedSet
  as? [String]` casts in Dashboard/Cook always yielded an empty list (blank preset
  pickers) — replaced with a Set-based dedupe.
- **Settings seeded its fields only on appear**, so opening it before the first status
  arrived left probe names / PID / units blank forever. Now seeds from the first status
  that carries values (`onChange(..., initial: true)`), never clobbering edits.
- Settings now shows the board's **probe offsets** (`probe_offsets` added to
  `DeviceState`). The PID **B** field stays blank because `/api/status` doesn't carry
  bias (same on the web UI).
- **Cook tab**: the live session now sits under "Current cook" with started time and
  a ticking elapsed counter; "Finish cook" only shows when a cook is active; the live
  cook no longer appears under "Past cooks".
- `-hm.initialTab dashboard|graph|cook|settings` launch argument opens a given tab
  (used for headless screenshots; also handy as an Xcode scheme argument).

## Build + run from the CLI (no signing needed for the simulator)

`xcode-select` now points at `/Applications/Xcode.app/Contents/Developer` and the
license is accepted, so `xcodebuild` works directly. The iOS 27.0 simulator runtime
is installed (`xcodebuild -downloadPlatform iOS`).

```sh
cd ~/Developer/heatermeter-modern/ios && xcodegen generate    # only if project.yml changed
xcodebuild -project HeaterMeter.xcodeproj -target HeaterMeter \
  -sdk iphonesimulator -configuration Debug -arch arm64 CODE_SIGNING_ALLOWED=NO build
# -> ios/build/Debug-iphonesimulator/HeaterMeter.app

U=3239EF15-E46B-4BCC-9D25-27242A25973A          # iPhone 18 Pro
xcrun simctl boot $U; xcrun simctl bootstatus $U -b
xcrun simctl install $U build/Debug-iphonesimulator/HeaterMeter.app
# seed the saved host so onboarding is skipped
xcrun simctl spawn $U defaults write dev.tallack.heatermeter hm.baseURL "http://192.168.3.164:8080"
for t in dashboard graph cook settings; do
  xcrun simctl launch $U dev.tallack.heatermeter -hm.initialTab $t; sleep 6
  xcrun simctl io $U screenshot ios-$t.png
  xcrun simctl terminate $U dev.tallack.heatermeter
done
```

### Why headless (2026-09-15)

- This Xcode 27 install has **no Simulator.app** (`Contents/Developer/Applications`
  doesn't exist), so there is no window to click in; `simctl` has no tap command.
- The Claude Code iOS Simulator panel (`claude-ios-sim` helper) **crashes on attach**
  in Metal (`-[_MTLDevice recordBinaryArchiveUsage:]` → nil in `NSArray` → abort),
  repeatably — a desktop-app bug on this macOS, not ours. If a later Claude build
  fixes it, `attach` + `tap`/`inspect` work against the same booted device. If the
  CoreSimulator service ever reports the runtime "malformed"/unavailable:
  `xcrun simctl shutdown all; launchctl remove com.apple.CoreSimulator.CoreSimulatorService`.
- Interactive flows that still need real taps to verify: the probe target/rename
  editor sheet, the manual-fan sheet, session detail (rename/delete), program start,
  PID preset picker, disconnect. All compile; none crashed on render.

## What's built (`ios/`)

| File | Role |
|------|------|
| `project.yml` | XcodeGen spec. iOS 17 target, bundle `dev.tallack.heatermeter`, `NSAllowsLocalNetworking` (plain-HTTP LAN), Bonjour keys. `.xcodeproj` + generated `Info.plist` are gitignored. |
| `HeaterMeter/HeaterMeterApp.swift` | `@main`; connection-gated 4-tab `TabView` (`AppTab`, `-hm.initialTab`). |
| `HeaterMeter/Connection.swift` | `ConnectionStore` (host+token in UserDefaults) + onboarding screen that validates via `fetchStatus` before saving. `normalize()` accepts `ip:port` / `host` / full URL, defaults http+:8080. |
| `HeaterMeter/HeaterMeterClient.swift` | `@MainActor @Observable`. Full REST control/data surface + reconnecting `URLSessionWebSocketTask` live feed. |
| `HeaterMeter/Models.swift` | Codable mirrors of the API. `DeviceState` has a manual `init(from:)` (alarms decode whether the board sends strings or numbers; missing keys don't throw; `probe_offsets` strings or numbers). `@LenientDouble` for string-typed numerics. `JSONValue` carries opaque program-stage blobs back to `/api/program/start`. |
| `HeaterMeter/DashboardView.swift` | Pit hero + setpoint + Off, probe tiles w/ targets + predict ETAs, manual-fan sheet, lid open/cancel, probe target/rename editor w/ meat presets. |
| `HeaterMeter/GraphView.swift` | Swift Charts (pit/food/setpoint), range picker incl. "This cook" (via `session_id`), dashed target `RuleMark`s. `/api/history` decimates server-side, so long cooks stay complete. |
| `HeaterMeter/CookView.swift` | Current cook (live session + elapsed, finish), start a program preset + active-program banner/stop, past cooks with per-cook stats (computed from `/api/history`), rename/delete. |
| `HeaterMeter/SettingsView.swift` | Probe names+offsets, PID preset + b/p/i/d + save, auto-tune (start/cancel, live poll, load-result), smart lid recovery, °F/°C, integration status (read-only), disconnect. |
| `README.md` | Open/run instructions, connectivity notes. |

First launch asks for the HeaterMeter address (e.g. `192.168.3.164:8080`) and
validates it before saving.

## Deferred / not built (intentional, per chosen scope)

- **Push notifications + Live Activity / Dynamic Island.** Daemon side is ready
  (`backend/heatermeterd/apns.py`, `/api/push/*`). App TODO: register device token
  via `client.registerPush(deviceToken:)` (needs an APNs entitlement + remote-notif
  registration), define an `ActivityAttributes.ContentState` matching
  `apns.liveactivity_content_state(...)`, start/update the activity during a cook,
  and on the Pi `pip install "heatermeter[ios]"` + configure APNs creds (`POST /api/push`).
- **Probe-type picker** (uses `/api/probe-presets`) and **MQTT/notify editing** — app
  shows integration status read-only; configured from the web UI for now.
- **Pit Guard / serve-time plan / power-up policy** cards (web-only so far; the
  Dashboard could show the serve-plan status line from `/api/status.serve_plan`).
- **Bonjour discovery** — keys are in `project.yml`; manual host entry for now.
  Next step: `NWBrowser` for `_http._tcp`.
- App icon, launch screen, iPad layout polish.

## Backend endpoints the app uses (reference)

`GET /api/status`, `GET /api/ws` (live), `POST /api/setpoint|manual|command`,
`POST /api/lid/open|lid/cancel`, `POST /api/probe-name|offsets|probe-type|alarms`,
`GET /api/predict`, `GET /api/history`, `GET/PATCH/DELETE /api/sessions[/id]`,
`POST /api/cook/finish`, `GET /api/presets`, `GET /api/program` + `POST /api/program/start|stop`,
`POST /api/pid`, `GET/POST/DELETE /api/autotune`, `GET/POST /api/lid-recovery`,
`POST /api/units`, `GET /api/push`.

## Ideas backlog

- Push + Live Activity (the headline native feature).
- Home Screen / Lock Screen widgets; Apple Watch complication.
- Session compare overlay on the graph; timeline notes + photos.
- Cooker-profile switching; guided cooks flow.
- Bonjour auto-discovery + a saved "away" (Tailscale) URL.
