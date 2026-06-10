# Competitive research: modern BBQ controllers (2026-05-31)

Research into four commercial controllers to inform the HeaterMeter rewrite roadmap.
Goal: identify features worth implementing, and lanes where an open, local-first
controller can decisively win. Full agent reports archived in the session transcript.

## The products at a glance

| Product | What it is | Probes | Control | Standout | Biggest weakness |
|---|---|---|---|---|---|
| **EGG Genius** (rebadged Flame Boss 400/500) | Cloud WiFi controller | 4 (1 pit + 3 food via Y-cable) | Adaptive bang-bang, variable fan | 3-tier connectivity (cloud/LAN/device-AP), share-a-cook | Cloud outages kill cooks; app only polls on foreground; overshoot |
| **FireBoard 2 Drive** | Gold-standard data/app | 6 (any food/pit) | Selectable PID modes, Drive Programs | "Sessions" model, **FireBoard Analyze** predictions, documented REST API | 2.4GHz only; poll-only rate-limited API; cable spaghetti |
| **Inkbird ISC-028BW** | Budget mainstream | 5 (4 food + 1 pit) | PID (degrades on long cooks) | USB-C power, 3-day offline buffer, meat presets, dual-band WiFi | Overshoot, PID goes bang-bang after hours, no official HA support |
| **ChefsTemp Breezo + ProTemp S1** | Wireless modular stack (~$245) | 4 wireless | "PID"/airflow pulsing (unverified) | Wireless probes, Apple Watch + Live Activities, doneness predictor, OTA | No API/HA, mandatory cloud, manual lid pause, flaky 2.4GHz, no CSV export |

## The single biggest insight

**Every one of these is cloud-dependent and closed.** Their shared, repeated failure
modes are: cloud outages (EGG Genius goes down every Thanksgiving), flaky 2.4GHz WiFi,
no/poor Home Assistant support, no local API, and crude control loops that overshoot.

HeaterMeter's rewrite is **local-first, open, and already runs a real PID on dedicated
hardware**. That is not a disadvantage to overcome - it is the competitive moat. The
roadmap should lean into: works with no internet, documented local API + MQTT, first-
class Home Assistant, transparent/tunable control, and full data ownership.

## Features worth implementing, grouped & prioritized

### Tier 1 - high value, software-only, plays to our strengths
1. **"Sessions" / cook history model** (FireBoard). Auto-start a session when data flows,
   auto-close after idle, every cook becomes a named, searchable, editable record
   (name/start/end/description). This is the core data primitive we lack. Our SQLite
   store already logs samples - this is mostly a `sessions` table + UI.
2. **Time-to-done prediction** (FireBoard Analyze is best-in-class; EGG Genius/Inkbird
   have NONE). Stall-aware S-curve model (rise→stall→finish) with a confidence band that
   narrows as data accumulates, plus a simple linear fallback. Pure math on data we log.
   This is the headline "smart" feature and a clear gap in the cheaper competitors.
3. **Meat/doneness presets** (Inkbird, ChefsTemp). ~26-29 USDA targets with doneness
   levels that auto-set the food-probe target + alarm. Just a lookup table; big beginner UX win.
4. **Timeline notes (+ photos) as chart markers** (FireBoard). Timestamped, channel-tagged
   annotations rendered on the graph ("wrapped the brisket"). Cheap, high payoff.
5. **First-class Home Assistant integration via local MQTT discovery** (all four are weak
   here; this is Phase 3 already planned). Expose pit/food/setpoint/fan%/lid as read+write
   entities. The HA community reverse-engineers these devices precisely because none ship
   it - we can just provide it.
6. **Rich alert tuning** (FireBoard): minutes-buffer debounce, repeat interval, time
   windows, multi-recipient, and a **"device went dark" failsafe** alert. The debounce and
   the lost-data failsafe especially cut false alarms and catch dead cooks.
7. **CSV/JSON export, multi-resolution, per-session or full archive** (FireBoard; a gap in
   ChefsTemp/Inkbird). Our audience loves data. Easy on local SQLite.
8. **Fan-output (%) overlaid on the temp graph** (Inkbird, FireBoard). We already compute
   it; plotting blower duty alongside temps lets pitmasters see the PID working.

### Tier 2 - control/firmware-side, real cooking value
9. **Multi-stage cook programs with food-probe-driven transitions** (FireBoard Drive
   Programs). "Hold 250°F for 2h, then hold until meat hits 200°F, then drop to 180°F
   keep-warm." A staged-setpoint state machine on top of our existing control loop.
10. **Keep-Warm auto setpoint drop + optional auto-shutdown** when a food probe hits target
    (EGG Genius). Per-probe mode of Off / On / On+Keep-Warm is a clean model.
11. **Open-lid auto-detection with configurable max duration** - we ALREADY have this in
    firmware; ChefsTemp still makes users press a button, EGG Genius spams messages. Make
    it visible/tunable in the UI as a selling point.
12. **Fan tuning exposed in UI**: min/max fan, max startup speed, fan floor, servo
    min/max/ceil, invert flags. (Already in `$HMFN`; user explicitly wants these. This is
    part of Phase 2 polish.)

### Tier 3 - "modern" polish
13. **Public shareable session links** (EGG Genius, FireBoard) with a revocable private
    toggle. Read-only live/historical cook page. Fits the open-source ethos.
14. **Session compare / overlay a past cook on the live one** (FireBoard) with time offset.
    Turns history into a coaching tool. Chart-layer plumbing only.
15. **Glanceable status outside the app**: Apple Watch / Lock-Screen Live Activities
    (ChefsTemp), or for us most cheaply a PWA + web push / ntfy. Phase 3 notifications.
16. **OTA firmware/software updates as a first-class feature** (ChefsTemp). We have updater
    muscle from the GS plugin work; same philosophy for the Pi app.

### Hardware/power notes (not urgent, but worth design awareness)
- **USB-C / power-bank operation** (Inkbird, ChefsTemp) as a supported "all-day off a
  battery bank" mode. The Pi already runs on USB.
- **Offline buffering with auto-sync** (Inkbird 3-day) - if a viewer/HA bridge drops, keep
  logging and backfill. We log locally regardless, so we're inherently ahead here.

## Anti-patterns to deliberately avoid (their real complaints)
- Don't make control depend on cloud (EGG Genius #1 failure).
- Don't let the app only poll on foreground (EGG Genius's worst bug).
- Always allow manual fan override (EGG Genius lacks it).
- Don't let the PID degrade to bang-bang on long cooks / overshoot on cold start
  (Inkbird & EGG Genius both criticized; our real PID is already ahead - keep it tuned,
  freeze the integrator on lid-open).
- Don't rate-limit / poll-only the API (FireBoard's weak spot) - offer MQTT/websockets/webhooks.
- Don't downsample high-res data after 24h (FireBoard) - keep full resolution locally.

## Sources
EGG Genius/Flame Boss, FireBoard (fireboard.com + docs.fireboard.io + public REST API),
Inkbird (inkbird.com + HA BLE reverse-engineering threads), ChefsTemp (chefstemp.com +
AmazingRibs review). Full URLs in the session transcript's research agent outputs.
