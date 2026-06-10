# HeaterMeter modern — capability roadmap

Living strategy doc. Goal: make this open, local-first HeaterMeter smarter and more
capable than off-the-shelf BBQ controllers, within the hardware ceiling (ATmega328
board: 1 pit + 3 food probes, single zone, wired; all "smart" work lives on the Pi).

## Where the market is (2026)

The frontier moved from **data + alerts** to **prediction + guidance**:
- **Combustion Inc** ships a physics-based prediction engine (simulates the food:
  heat dynamics, moisture migration, evaporative cooling) + a pasteurization tracker.
- **MEATER** leads on Guided Cook (protein → cut → doneness, with carryover + rest)
  and a "previous cooks" library.
- **FireBoard's** own 2025–2026 roadmap is *hardware only* — the software leader has
  stalled. That is the opening.

We already match/beat the old bar: sessions, predictions (basic), meat presets,
notes+photos, MQTT/HA, multi-stage programs, keep-warm/auto-shutdown, share links,
session compare, manual fan, CSV, diagnostics, timers, cook-done detection, firmware
updater, host self-update, optional auth, backup/restore, retention.

## The thesis: we control the cook — they only watch it

Every competitor is a *passive thermometer*. HeaterMeter is the only one here that holds
a closed loop on a real PID, so it can **act** (change setpoint, keep-warm, freeze the
integrator on lid-open, refuse to let a cook die). Local-first + open + HA-native on top
of that = **the only controller that predicts a cook AND steers it, no cloud, full data
ownership.** Our prediction can beat MEATER's for a controlled cook because we know and
control the ambient (the pit) and keep continuous high-res history.

Out of scope (hardware-bound): >4 probes, wireless probes, an 8-sensor food array, a
native smartwatch app. We win on software + the control loop.

## Initiatives (priority order)

### A. Predictive "Cook Coach" + Guided Cooks  — headline differentiator
- A1. Stall-aware **thermal-model predictor** using the pit as a known environment
  (explicit stall entry/exit, online fit, tightening confidence band, "done by ~4:15 PM
  ±25 min" clock).
- A2. **Carryover + rest estimate** → predict done-*and-rested* time; tie to keep-warm.
- A3. **Guided Cooks**: protein → cut → doneness/style auto-configures food target+alarm,
  pit setpoint, a multi-stage program, and step prompts (wrap at the stall, spritz, rest).
  Offline recipe library wrapping existing presets + programs.
- A4. **Closed-loop actions (unique):** guided cook can *act* with confirmation —
  auto-bump setpoint after a wrap, auto keep-warm on target, auto-start the rest timer.

### B. Smarter control — lean into the real-PID moat
- B5. **Cooker profiles**: save PID + fan tuning per grill, quick-switch, auto-restore.
- B6. **Fuel/charcoal-remaining estimate** + "add fuel soon" alert from fan-duty + trend.
- B7. **Control hardening**: verify lid-open integrator freeze, cold-start overshoot guard,
  surface as selling points (overshoot is competitors' #1 complaint).

### C. Glanceability & presence — our way, not a watch
- C8. **HA automation cookbook + entities**: predicted-done-time, stall-detected, fuel-low
  sensors + blueprints ("announce on Sonos when food hits target"). Beats a watch app:
  infinitely extensible + local.
- C9. **Richer push events** (ntfy): stall started/ended, wrap window, predicted-done,
  fuel-low. Optional HTTPS for true web-push.

### D. Data intelligence & coaching — turn history into a coach
- D10. **Cook reports**: printable/shareable per-cook summary (graph, stages, notes, stats,
  prediction-vs-actual).
- D11. **Learn from your cooks**: after a few cooks, suggest tighter PID, typical stall
  duration for a cut, your time-per-pound. (FireBoard "compare to past self," automatic + local.)
- D12. **"Repeat this cook"** templates from history.

### E. Reliability & trust — table stakes  ← CURRENT FOCUS
- E13. **Probe-dropout / sensor-fault alerts** (#67): a yanked/failed probe must never
  silently end a cook; pit dropout during a cook is critical.
- E14. **Auto timeline events** (#68): lid, stall, stage change, target reached, wrap markers.
- E15. **Cook reports** (#70) (also D10).
- E16. **Firmware command checksums** (#72, SUPERVISED): the host→board `/set?` channel is
  unchecksummed (caused the `SET?PO=0.0` glitch). Bricking-capable reflash → do with user present.

### F. Foundation
- F17. **First-run onboarding wizard**: cooker type, probe types, units, optional HA.
- F18. **Publish a release channel** (self-update exists now) so others can run it.

## Status (2026-06-10): ALL LANES BUILT

Every initiative above is implemented, tested (244 backend tests), and deployed:
- E: probe-dropout/fault/stall alerts, auto timeline events, cook reports, LCD
  toasts, HTTPS option, firmware command checksums (hm4 built + offered in the
  Firmware card - **the flash itself is supervised and still pending**).
- A: stall-aware predictor v2 with done-by/ready-at clocks, Guided Cooks
  (8-cook catalog, milestone prompts, wrap confirm, auto keep-warm).
- B: cooker profiles, fuel/charcoal monitor with add-fuel alert.
- C: HA entities (cook_stalled, fuel_low, predicted_done) + docs/HOME-ASSISTANT.md.
- D: cook insights + repeat-cook.
- F: first-run setup wizard. (F18 publish-a-release-channel is ready when wanted:
  the self-updater is host-agnostic; build an artifact + manifest and set the URL.)

Remaining: flash firmware 20260610-hm4 via Settings -> Firmware (user present).
