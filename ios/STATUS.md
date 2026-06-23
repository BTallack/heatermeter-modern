# HeaterMeter iOS app — status & handoff

_Paused 2026-06-22 to make other improvements first. This is where to pick up._

## TL;DR

A full native SwiftUI **control app** (all four screens) is written and committed
to `main`. It's a second client of the existing REST + WebSocket API — no backend
changes. The XcodeGen project **generates cleanly** (`xcodegen generate` succeeds,
which validates `project.yml`). It has **not been compiled yet**: Xcode.app is
installed but the Xcode license hasn't been accepted on the dev machine, so
`xcodebuild` refuses. That's the only thing between here and a first build.

## To resume (exact next steps)

```sh
# 1. One-time: accept the Xcode license (must be done by a human; it's a legal click)
sudo xcodebuild -license accept

# 2. Generate + open
cd ~/Developer/heatermeter-modern/ios
xcodegen generate
open HeaterMeter.xcodeproj      # set Signing Team on the target, then Run

# (Optional) headless compile-check for the simulator, no signing needed:
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project HeaterMeter.xcodeproj -target HeaterMeter \
  -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

First launch asks for the HeaterMeter address (e.g. `192.168.3.164:8080`) and
validates it before saving.

## What's built (committed: `main`, commit 6802596; a11y/predicted-done fixes in 0ec0c9f / earlier)

Files under `ios/`:

| File | Role |
|------|------|
| `project.yml` | XcodeGen spec. iOS 17 target, bundle `dev.tallack.heatermeter`, `NSAllowsLocalNetworking` (plain-HTTP LAN), Bonjour keys. `.xcodeproj` + generated `Info.plist` are gitignored. |
| `HeaterMeter/HeaterMeterApp.swift` | `@main`; connection-gated 4-tab `TabView`. |
| `HeaterMeter/Connection.swift` | `ConnectionStore` (host+token in UserDefaults) + onboarding screen that validates via `fetchStatus` before saving. `normalize()` accepts `ip:port` / `host` / full URL, defaults http+:8080. |
| `HeaterMeter/HeaterMeterClient.swift` | `@MainActor @Observable`. Full REST control/data surface + reconnecting `URLSessionWebSocketTask` live feed. |
| `HeaterMeter/Models.swift` | Codable mirrors of the API. `DeviceState` has a manual `init(from:)` (alarms decode whether the board sends strings or numbers; missing keys don't throw). `@LenientDouble` for string-typed numerics. `JSONValue` carries opaque program-stage blobs back to `/api/program/start`. |
| `HeaterMeter/DashboardView.swift` | Pit hero + setpoint + Off, probe tiles w/ targets + predict ETAs, manual-fan sheet, lid open/cancel, probe target/rename editor w/ meat presets. |
| `HeaterMeter/GraphView.swift` | Swift Charts (pit/food/setpoint), range picker incl. "This cook" (via `session_id`), dashed target `RuleMark`s. |
| `HeaterMeter/CookView.swift` | Sessions list + per-cook stats (computed from `/api/history`), finish cook, rename/delete, start a program preset + active-program banner/stop. |
| `HeaterMeter/SettingsView.swift` | Probe names+offsets, PID preset + b/p/i/d + save, auto-tune (start/cancel, live poll, load-result), smart lid recovery, °F/°C, integration status (read-only), disconnect. |
| `README.md` | Open/run instructions, connectivity notes. |

## Verified vs NOT

- ✅ `Models.swift` type-checks against the macOS SDK.
- ✅ All Swift files `swiftc -parse` clean (Command Line Tools).
- ✅ `xcodegen generate` succeeds → `project.yml` is valid and the project builds its structure.
- ❌ **The SwiftUI views are NOT type-checked / compiled.** Their `@Observable`/`@State`
  macros need full Xcode (the CLT lacks the macro plugins), and the license gate
  blocked the Xcode build. **Expect to fix a small compile error or two on first build.**

## Things to check on first compile (authored without a compiler)

- SwiftUI ViewBuilder type inference across the four views (the most likely source of fixes).
- `LidRecoverySection` uses `if var c = cfg { … Binding(get/set mutating c) }` — confirm it compiles/behaves.
- `ProbeEditor` Picker with nested `Section`s for grouped meat presets.
- `GraphView` `chartForegroundStyleScale([... KeyValuePairs ...])` and the `if/else` inside the `Chart` builder.
- `sheet(item:)` Identifiable conformances (`ProbeReading`, `CookSession`).

## Deferred / not built (intentional, per chosen scope)

- **Push notifications + Live Activity / Dynamic Island.** Daemon side is ready
  (`backend/heatermeterd/apns.py`, `/api/push/*`). App TODO: register device token
  via `client.registerPush(deviceToken:)` (needs an APNs entitlement + remote-notif
  registration), define an `ActivityAttributes.ContentState` matching
  `apns.liveactivity_content_state(...)`, start/update the activity during a cook,
  and on the Pi `pip install "heatermeter[ios]"` + configure APNs creds (`POST /api/push`).
- **Probe-type picker** (uses `/api/probe-presets`) and **MQTT/notify editing** — app
  shows integration status read-only; configured from the web UI for now.
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

## Ideas backlog (when we return)

- Push + Live Activity (the headline native feature).
- Home Screen / Lock Screen widgets; Apple Watch complication.
- Session compare overlay on the graph; timeline notes + photos.
- Cooker-profile switching; guided cooks flow.
- Bonjour auto-discovery + a saved "away" (Tailscale) URL.
