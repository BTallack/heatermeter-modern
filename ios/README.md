# HeaterMeter iOS app

A native SwiftUI control app for the HeaterMeter daemon — a second client of the
same REST + WebSocket API the web UI uses, so there's no backend to change. It
covers the four screens: live **Dashboard** (pit hero, setpoint, probe targets
+ ETAs, manual fan, lid), **Graph** (Swift Charts), **Cook** (sessions, stats,
programs), and **Settings** (probes, PID + auto-tune, smart lid recovery, units,
integration status). Push / Live Activity is not wired yet (the daemon side
exists in `backend/heatermeterd/apns.py`).

## Open and run

The Xcode project is generated from `project.yml` (so it never drifts), not
committed.

```sh
brew install xcodegen        # one time
cd ios
xcodegen generate            # writes HeaterMeter.xcodeproj
open HeaterMeter.xcodeproj
```

In Xcode: select the **HeaterMeter** target → Signing & Capabilities → set your
Team (a free Apple ID works for running on your own device), then Run. iOS 17+.

On first launch the app asks for your HeaterMeter's address (e.g.
`192.168.3.164:8080`) and validates it before saving. Leave the password blank
unless you turned on the daemon's optional auth.

## Files

| File | Role |
|------|------|
| `project.yml` | XcodeGen spec: iOS 17 app target, bundle id, ATS local-networking exception, Bonjour keys. |
| `HeaterMeter/HeaterMeterApp.swift` | `@main`; gates on a configured connection, then the four-tab app. |
| `HeaterMeter/Connection.swift` | Persists host + token; onboarding screen. |
| `HeaterMeter/HeaterMeterClient.swift` | `@Observable` client: REST control + data, reconnecting WebSocket live feed. |
| `HeaterMeter/Models.swift` | Codable models mirroring the API (incl. lenient decoding for the board's string-typed numerics). |
| `HeaterMeter/DashboardView.swift` | Pit hero, setpoint, probe tiles + targets/ETAs, manual fan, lid, probe editor. |
| `HeaterMeter/GraphView.swift` | Swift Charts temperature graph with range picker + target lines. |
| `HeaterMeter/CookView.swift` | Sessions list + per-cook stats, finish cook, start a program. |
| `HeaterMeter/SettingsView.swift` | Probes, PID + auto-tune, smart lid recovery, unit, integrations, disconnect. |

## Connectivity notes

- **Plain HTTP on the LAN** is allowed via `NSAllowsLocalNetworking` (set in
  `project.yml`). On home Wi-Fi the app talks directly to the Pi.
- **Away from home**: point the host at a Tailscale address or a reverse proxy.
  If that host isn't a private-LAN address you may need a per-domain ATS
  exception or TLS (the daemon has an HTTPS option).
- **Discovery**: the project ships the Bonjour keys; auto-discovery (`NWBrowser`
  for `_http._tcp`) is a natural next addition — for now you enter the host once.

## Not built yet (daemon side is ready)

- **APNs push + Live Activity / Dynamic Island.** Register the device token via
  `client.registerPush(deviceToken:)`, install `heatermeterd[ios]` on the Pi,
  and configure APNs creds (`POST /api/push`). The Live Activity content-state
  matches `apns.liveactivity_content_state(...)`.
- **Probe-type picker, MQTT/notify editing** — currently configured from the web
  UI; the app shows integration status read-only.

## Verifying without full Xcode

`Models.swift` type-checks against the macOS SDK and every file parses cleanly
with the Command Line Tools. The SwiftUI views need full Xcode to type-check
(their `@Observable`/`@State` macros ship with Xcode, not the CLT).
