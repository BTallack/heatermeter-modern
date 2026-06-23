# HeaterMeter iOS app (starting point)

A native SwiftUI client for the HeaterMeter daemon. The daemon already exposes
everything over REST + WebSocket (`/api/*`, `/api/ws`), so this app is purely a
*client* — no backend rewrite. These files are a scaffold, not a full Xcode
project: drop them into a new iOS App target (iOS 17+) and build out from there.

## What's here

| File | Role |
|------|------|
| `HeaterMeter/Models.swift` | `Codable` models mirroring the daemon JSON (`/api/status`, the WS envelope, predictions, sessions, push status). Includes `LenientDouble` for the board's string-typed numeric config fields. |
| `HeaterMeter/HeaterMeterClient.swift` | `@Observable` client: REST control (setpoint, manual fan, lid, predict, sessions, push register) + a reconnecting WebSocket live feed that updates `state`. |
| `HeaterMeter/DashboardView.swift` | A compact dashboard (pit hero + setpoint stepper + probe tiles) driven by the client. Mirrors the web `Dashboard.svelte`. |

Graph (Swift Charts), Cook, and Settings screens follow the same pattern against
the same client.

## Wiring it up

```swift
@main
struct HeaterMeterApp: App {
    @State private var client = HeaterMeterClient(
        baseURL: URL(string: "http://192.168.3.164:8080")!)
    var body: some Scene {
        WindowGroup { NavigationStack { DashboardView(client: client) } }
    }
}
```

## Things to handle before it works on-device

- **App Transport Security.** The Pi serves plain HTTP on the LAN; `URLSession`
  blocks that by default. Either add an ATS exception for the host in
  `Info.plist`, or front the daemon with TLS (the backend has an HTTPS option)
  and/or reach it over a Tailscale hostname.
- **Finding the box.** Hard-code the URL to start; add Bonjour/`NWBrowser`
  discovery so the app finds the HeaterMeter on the LAN, plus a manual host field
  and a stored "away" URL (Tailscale / reverse proxy).
- **Auth.** If the daemon's optional password auth is on, log in via
  `POST /api/login`, stash the bearer token in `authToken` (Keychain), and the
  client sends it on REST and as the WS `?token=`.

## Push notifications (away-from-home alerts + Live Activity)

The daemon side already exists (`backend/heatermeterd/apns.py`,
`POST /api/push/*`). To light it up:

1. In the app, register for remote notifications; on the granted device token
   call `client.registerPush(deviceToken:)` → the daemon stores it.
2. On the Pi, install the sender deps (`pip install "heatermeterd[ios]"` —
   `cryptography` + `httpx[http2]`) and configure APNs credentials via
   `POST /api/push` (Team ID, Key ID, `.p8` path, bundle id, sandbox flag for
   debug builds).
3. Existing daemon alerts (`_push`) then fan out to every registered device via
   APNs alongside ntfy — stall, lid, fuel-low, "almost done", etc.
4. **Live Activity / Dynamic Island** is the premium step: define an
   `ActivityAttributes` whose `ContentState` matches
   `apns.liveactivity_content_state(...)` (pit/setpoint/foods/fan/ETA), start it
   when a cook begins, and have the daemon post `liveactivity_payload(...)`
   updates to the per-activity push token (the `apns-topic` gets the
   `.push-type.liveactivity` suffix, already handled by `apns.apns_topic`).

## Distribution

No App Store needed for personal use: a free Apple ID sideloads to your own
device (re-sign weekly), or a paid developer account ($99/yr) gets TestFlight —
handy if you want others running HeaterMeter to install it.
