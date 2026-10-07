<script>
  import { onMount, onDestroy } from 'svelte';
  import { getJSON, postJSON, connectWs } from './api.js';
  import { fmtClock } from './util.js';
  import { pitState, cookActive } from './pitstate.js';
  import { toast, unreachable } from './toast.svelte.js';
  import Dialog from './Dialog.svelte';

  // When embedded (desktop), the parent owns padding/width and stacks the temps
  // above the graph; standalone (mobile) fills the screen height.
  let { embedded = false } = $props();

  let status = $state({});
  let names = $state(['Pit', 'Food 1', 'Food 2', 'Ambient']);
  // Optimistic rename guard: pi -> {name, until}. Holds a just-set probe name
  // until the board echoes it back (serial round-trip) so the UI updates
  // instantly instead of reverting to the old name for a few seconds.
  let pendingName = {};
  let alarms = $state([]);
  let meat = $state([]);
  let unit = $state('F');
  let spInput = $state(225);
  let spDirty = $state(false);
  let health = $state({});   // channel -> 'disconnected' | 'fault' | 'ok'
  let fuelInfo = $state({}); // blower-effort fuel assessment from the daemon
  let pitGuard = $state({}); // {low, fire_dying, overtemp, armed} from Pit Guard
  let guided = $state(null); // active guided cook (null when none)
  let servePlan = $state(null); // serve-time plan ("dinner at 6"), null when off

  // -- freshness: never show old numbers as if they were live ----------------
  // The board reports every second or two. Past STALE_SECS without a reading,
  // or with the socket down, the readings dim and the pill says so.
  const STALE_SECS = 12;
  let lastSeen = $state(0);   // ms timestamp of the last live reading
  let wsOpen = $state(true);
  let nowMs = $state(Date.now());
  let clockTimer;
  const ageSecs = $derived(lastSeen ? Math.max(0, Math.round((nowMs - lastSeen) / 1000)) : 0);
  const stale = $derived(lastSeen > 0 && (!wsOpen || ageSecs >= STALE_SECS));
  const ageText = (s) => (s < 90 ? `${s}s` : `${Math.round(s / 60)} min`);
  const freshness = $derived(
    !lastSeen ? null : !wsOpen ? 'Reconnecting…' : ageSecs >= STALE_SECS ? `No update for ${ageText(ageSecs)}` : null);

  // What the pit is doing, in words (derived on stock firmware).
  const ps = $derived(pitState(status, pitGuard, unit));

  // Keep the screen awake while the cooker is actively running (startup,
  // recovering, or at temp). The Wake Lock API only exists in a secure
  // context (HTTPS), so over plain HTTP this is a silent no-op.
  let wakeLock = null;
  async function syncWakeLock() {
    const cooking = cookActive(status);
    if (cooking && !wakeLock && navigator.wakeLock
        && document.visibilityState === 'visible') {
      try {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => (wakeLock = null));
      } catch (_) { wakeLock = null; }
    } else if (!cooking && wakeLock) {
      try { wakeLock.release(); } catch (_) {}
      wakeLock = null;
    }
  }
  function onVisibility() { syncWakeLock(); }
  let stop;

  // Elapsed cook time, from the active session's start (durable across reloads).
  const isCooking = $derived(cookActive(status));
  let cookStartTs = $state(null);
  let cookStartTimer;
  async function refreshCookStart() {
    try {
      const ss = await getJSON('sessions');
      const a = ss.find((s) => !s.ended_ts && !s.completed_ts);
      cookStartTs = a ? a.started_ts : null;
    } catch (_) {}
  }
  function cookElapsed() {
    if (!cookStartTs) return '';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - cookStartTs));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }
  // Next unfired milestone's prompt = "what to expect next" on the strip.
  const guidedNext = $derived(
    guided?.milestones?.find((m) => !m.fired)?.prompt || null);
  async function guidedWrapped() {
    try { await postJSON('guided/wrapped', {}); toast('Noted: wrapped.'); }
    catch (_) { toast(unreachable("the wrap wasn't recorded"), { tone: 'error' }); }
  }
  function fuelHours(s) { const h = s / 3600; return h >= 1 ? `~${h.toFixed(1)}h` : `~${Math.round(s / 60)}m`; }

  function apply(d) {
    if (!d || !d.state) return;
    status = d.state.status || {};
    if (d.state.status) lastSeen = Date.now();
    const incoming = (d.state.probe_names || names).slice();
    const t = Date.now();
    for (const k of Object.keys(pendingName)) {
      const p = pendingName[k];
      if (p.until < t || incoming[k] === p.name) delete pendingName[k]; // confirmed / expired
      else incoming[k] = p.name;                                        // hold optimistic name
    }
    names = incoming;
    alarms = d.state.alarms || alarms;
    if (d.state.pid && d.state.pid.units) unit = d.state.pid.units;
    if (d.state.probe_health) health = d.state.probe_health;
    if (d.state.fuel) fuelInfo = d.state.fuel;
    if (d.state.pit_guard) pitGuard = d.state.pit_guard;
    guided = d.state.guided ?? guided;
    if ('serve_plan' in d.state) servePlan = d.state.serve_plan;
    syncWakeLock();
    if (!spDirty && typeof status.set_point === 'number') {
      // Hold a just-sent value until the board echoes it, so the field doesn't
      // flick back to the old setpoint for a second.
      const sp = Math.round(status.set_point);
      if (spHold && Date.now() < spHold.until && sp !== spHold.v) return;
      spHold = null;
      spInput = sp;
    }
  }

  onMount(async () => {
    readPrefs();
    window.addEventListener('hm-prefs', readPrefs);
    document.addEventListener('visibilitychange', onVisibility);
    try { apply({ state: await getJSON('status') }); } catch (_) {}
    try { meat = (await getJSON('presets')).meat || []; } catch (_) {}
    refreshEtas();
    etaTimer = setInterval(refreshEtas, 25000);
    refreshCookStart();
    cookStartTimer = setInterval(refreshCookStart, 60000);
    clockTimer = setInterval(() => (nowMs = Date.now()), 1000);
    stop = connectWs((m) => {
      if (m.state) apply(m);
      if (m.event?.type === 'probe_event') {
        const ch = m.event.channel;
        if (m.event.kind === 'disconnect') health = { ...health, [ch]: 'disconnected' };
        else if (m.event.kind === 'reconnect') health = { ...health, [ch]: 'ok' };
        else if (m.event.kind === 'fault') health = { ...health, [ch]: 'fault' };
      }
    }, (open) => (wsOpen = open));
  });
  onDestroy(() => { stop && stop(); clearInterval(etaTimer); clearInterval(cookStartTimer); clearInterval(clockTimer); window.removeEventListener('hm-prefs', readPrefs); document.removeEventListener('visibilitychange', onVisibility); if (wakeLock) { try { wakeLock.release(); } catch (_) {} } });

  const fmt = (v) => (v == null || Number.isNaN(v)) ? '--' : Math.round(v);
  function bump(n) { spInput = Math.max(0, (parseInt(spInput, 10) || 0) + n); spDirty = true; spError = ''; }

  // The setpoint the board is actually running (null when off / manual).
  const spSent = $derived(typeof status.set_point === 'number' && status.set_point > 0 ? Math.round(status.set_point) : null);
  // A value typed or stepped but not sent: say so, or it reads as committed.
  const spUnsent = $derived(spDirty && Number(spInput) !== spSent);
  let spHold = null;            // {v, until}: a sent value awaiting the board's echo
  let spBusy = $state(false);
  let spError = $state('');
  async function sendSetpoint(v) {
    await postJSON('setpoint', { value: v, unit });
    spHold = { v, until: Date.now() + 6000 };
  }
  async function setSp() {
    const v = parseInt(spInput, 10);
    if (!Number.isFinite(v) || v <= 0) { spError = 'Enter a pit temperature. To stop the pit, use Turn off.'; return; }
    spError = ''; spBusy = true;
    try { await sendSetpoint(v); spDirty = false; toast(`Pit set to ${v}°${unit}.`); }
    catch (_) { toast(unreachable(spSent ? `the pit is still set to ${spSent}°` : 'nothing changed'), { tone: 'error' }); }
    spBusy = false;
  }

  // Turning the pit off is one of the few irreversible-feeling actions on this
  // screen: confirm it, then offer an undo.
  let offOpen = $state(false);
  let offBusy = $state(false);
  async function confirmOff() {
    const was = spSent;
    offBusy = true;
    try {
      await postJSON('command', { path: '/set?sp=O' });
      offOpen = false;
      toast('Pit turned off.', was ? { actionLabel: 'Undo', action: () => restoreSetpoint(was) } : {});
    } catch (_) { toast(unreachable('the pit is still running'), { tone: 'error' }); }
    offBusy = false;
  }
  async function restoreSetpoint(v) {
    try { await sendSetpoint(v); toast(`Pit set back to ${v}°${unit}.`); }
    catch (_) { toast(unreachable('the pit is still off'), { tone: 'error' }); }
  }

  // Food/ambient probes. pi = probe index on the board (1/2/3); the high-alarm
  // threshold for probe i lives at alarms[2*i + 1].
  // The board's own default names are "Food1"/"Food2"; show them as words.
  const probeLabel = (n, fallback) => (n || fallback).replace(/^Food\s*(\d)$/i, 'Food $1');
  const foods = $derived([
    { label: probeLabel(names[1], 'Food 1'), val: status.food1, cls: 'text-green-500', pi: 1, ch: 'food1' },
    { label: probeLabel(names[2], 'Food 2'), val: status.food2, cls: 'text-sky-400', pi: 2, ch: 'food2' },
    { label: probeLabel(names[3], 'Ambient'), val: status.ambient, cls: 'text-purple-400', pi: 3, ch: 'ambient' },
  ]);

  const parseAlarm = (v) => {
    const n = parseFloat(String(v ?? '').replace(/[LH]$/, ''));
    return Number.isNaN(n) || n < 0 ? null : n;
  };
  const targetFor = (pi) => parseAlarm(alarms[2 * pi + 1]);
  function buildThresholds(idx, val) {
    const a = [];
    for (let i = 0; i < 8; i++) a.push(i === idx ? val : parseAlarm(alarms[i]));
    return a;
  }

  // -- time-to-target ETAs (stall-aware predictor) ---------------------------
  let etas = $state({}); // channel -> prediction {eta_seconds, confidence, ...}
  let etaTimer;
  async function refreshEtas() {
    for (const f of foods) {
      const t = targetFor(f.pi);
      if (t == null) { etas[f.ch] = null; continue; }
      try { etas[f.ch] = await getJSON(`predict?channel=${f.ch}&target=${t}`); }
      catch (_) { etas[f.ch] = null; }
    }
    etas = { ...etas };
  }
  // Suffix for a probe's target line: " · done", " · ~6:45 PM", or "".
  function etaSuffix(f) {
    const tg = targetFor(f.pi);
    if (tg == null) return '';
    if (doneAt(f.val, tg)) return ' · done';
    const e = etas[f.ch];
    if (e && e.model === 'plateau')
      return e.plateau_temp != null ? ` · levelling off ~${Math.round(e.plateau_temp)}°` : ' · levelling off';
    if (e && e.stalled)
      return e.eta_seconds != null && e.eta_seconds > 0
        ? ' · stall · ~' + fmtClock(Date.now() / 1000 + e.eta_seconds) + '+'
        : ' · in the stall';
    if (e && e.eta_seconds != null && e.eta_seconds > 0 && e.confidence !== 'none')
      return ' · ~' + fmtClock(Date.now() / 1000 + e.eta_seconds);
    return '';
  }

  // -- temperature-reactive panel tint (opt-in, Settings -> Appearance) ------
  // Warms a panel's background as its temp nears the target (probes) or the
  // setpoint (pit), turning green once reached. Subtle inset overlay so the
  // card surface + text stay intact. Default on; only shows once cooking.
  let heatColors = $state(true);
  function readPrefs() { heatColors = localStorage.getItem('hm.heatColors') !== '0'; }
  // "Done" by the displayed (rounded) numbers, so a panel turns green exactly
  // when the shown temp meets the target (avoids 70.6→"71" looking not-done).
  const doneAt = (val, target) => val != null && target != null && Math.round(val) >= Math.round(target);
  function panelTint(p) {
    let c;
    if (p >= 0.95) c = 'rgba(255,86,48,0.22)';      // ember
    else if (p >= 0.85) c = 'rgba(255,138,0,0.16)'; // orange
    else if (p >= 0.6) c = 'rgba(255,193,7,0.10)';  // amber
    else return '';
    return 'box-shadow: inset 0 0 0 999px ' + c;
  }
  function tintStyle(val, target) {
    if (!heatColors || target == null || target <= 0 || val == null) return '';
    if (doneAt(val, target)) return 'box-shadow: inset 0 0 0 999px rgba(54,179,126,0.22)'; // green
    return panelTint(val / target);
  }

  // -- live controls: lid open + manual fan override -------------------------
  async function lidOpen() {
    try { await postJSON('lid/open'); }
    catch (_) { toast(unreachable("lid mode didn't start"), { tone: 'error' }); }
  }
  async function lidCancel() {
    try { await postJSON('lid/cancel'); }
    catch (_) { toast(unreachable('the lid timer is still running'), { tone: 'error' }); }
  }

  // Opens at the fan speed the controller is using now, so confirming without
  // touching the slider changes nothing rather than stopping the fan.
  let manualOpen = $state(false);
  let manualPct = $state(0);
  let manualBusy = $state(false);
  const currentFan = $derived(Math.round(status.output_pct ?? status.fan_pct ?? 0));
  function openManual() { manualPct = currentFan; manualOpen = true; }
  async function applyManual() {
    manualBusy = true;
    try {
      await postJSON('manual', { percent: Number(manualPct) || 0 });
      manualOpen = false;
      toast(`Fan held at ${manualPct}%. Set a pit temperature to go back to automatic.`);
    } catch (_) { toast(unreachable('the fan is unchanged'), { tone: 'error' }); }
    manualBusy = false;
  }

  // -- probe target popup ----------------------------------------------------
  let probeOpen = $state(false);
  let probeI = $state(0);     // index into foods
  let pName = $state('');
  let pTarget = $state('');
  let pPreset = $state('');
  let pError = $state('');
  let pBusy = $state(false);
  const meatCats = $derived([...new Set(meat.map((m) => m.category))]);

  function openProbe(i) {
    probeI = i;
    const pi = foods[i].pi;
    pName = names[pi] || foods[i].label;
    const t = targetFor(pi);
    pTarget = t == null ? '' : String(t);
    pPreset = '';
    pError = '';
    probeOpen = true;
  }
  function onPresetPick() {
    const m = meat.find((x) => x.key === pPreset);
    if (!m) return;
    pTarget = String(m.temp_f);                  // preset sets the target...
    pName = (m.label || '').slice(0, 22);        // ...and renames the probe (firmware now stores 22, scrolls on the LCD).
  }
  async function saveProbe() {
    const pi = foods[probeI].pi;
    // Protocol: a negative threshold DISABLES the alarm; null KEEPS the current
    // value. So an empty/cleared target must send -1, not null, to clear it.
    const cleared = (pTarget === '' || pTarget == null);
    const tgt = cleared ? -1 : Number(pTarget);
    if (!cleared && !(Number.isFinite(tgt) && tgt > 0 && tgt < 1000)) {
      pError = 'Enter a target temperature, or use Clear target.';
      return;
    }
    pError = ''; pBusy = true;
    // Clearing the target also resets the name to the slot default, so a probe
    // isn't left labelled for a cook it's no longer running (works whether the
    // target was cleared via the Clear button or by emptying the field). Use the
    // STATIC default, not foods[].label, which resolves to the current name.
    const defaultName = ['Pit', 'Food 1', 'Food 2', 'Ambient'][pi] || '';
    const newName = cleared ? defaultName : (pName || '').slice(0, 22);
    try {
      await postJSON('alarms', { thresholds: buildThresholds(2 * pi + 1, tgt) });
      if (newName !== (names[pi] || '')) {
        await postJSON('probe-name', { index: pi, name: newName });
        // Show the new name immediately; hold it until the board echoes it.
        pendingName[pi] = { name: newName, until: Date.now() + 8000 };
        names[pi] = newName; names = [...names];
      }
      apply({ state: await getJSON('status') });
      refreshEtas();
      probeOpen = false;
      toast(cleared
        ? (newName !== (names[pi] || '') ? `Target cleared. The probe is ${newName} again.` : 'Target cleared.')
        : `${newName || foods[probeI].label}: target ${tgt}°.`);
    } catch (_) {
      pError = "Couldn't reach the HeaterMeter, so nothing was saved. Check the connection and try again.";
    }
    pBusy = false;
  }
  function clearProbe() { pTarget = ''; saveProbe(); }

  // Screen readers hear the things that need a person, as they happen.
  const announce = $derived([
    stale ? 'Lost contact with the HeaterMeter.' : '',
    pitGuard.fire_dying ? 'Fire dying: the fan is maxed and the pit keeps falling.'
      : pitGuard.low ? 'Pit running low.' : '',
    pitGuard.overtemp ? 'Pit running hot.' : '',
    status.lid_countdown > 0 ? 'Lid open.' : '',
    fuelInfo.alerted ? 'Fuel running low.' : '',
    ...foods.map((f) => (health[f.ch] === 'disconnected' ? `${f.label} disconnected.`
      : health[f.ch] === 'fault' ? `${f.label} sensor fault.` : '')),
  ].filter(Boolean).join(' '));

  const pillCls = $derived(
    freshness ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
      : ps.active ? 'bg-orange-600/20 text-orange-700 dark:text-orange-300'
      : 'bg-neutral-500/15 text-neutral-600 dark:text-neutral-300');
</script>

{#snippet pit(extra = '')}
  <div class={'hm-card rounded-3xl p-5 flex flex-col justify-between ' + extra} style={tintStyle(status.pit, status.set_point)}>
    <div>
      <div class="flex items-center justify-between gap-2">
        <h2 class="font-sans text-xs uppercase tracking-widest opacity-50 font-semibold">{names[0] || 'Pit'}</h2>
        <span class={'text-xs px-2 py-0.5 rounded-full font-semibold tabular-nums ' + pillCls}>{freshness ?? ps.label}</span>
      </div>
      <div class={'font-display text-7xl lg:text-8xl font-black text-orange-500 leading-none tracking-tight tabular-nums mt-2 transition-opacity ' + (stale ? 'opacity-40' : '')}>
        {fmt(status.pit)}<span class="text-2xl opacity-50 align-top relative top-[0.12em]">°{unit}</span>
      </div>
      <div class="flex flex-wrap gap-x-5 gap-y-1 mt-4 text-sm opacity-70 tabular-nums">
        <span>Set <b class="opacity-100">{status.set_point == null ? '—' : fmt(status.set_point) + '°'}</b></span>
        <span>Fan <b class="opacity-100">{fmt(status.fan_pct)}%</b></span>
        {#if isCooking && cookStartTs}
          <span>Cooking <b class="opacity-100">{cookElapsed()}</b></span>
        {/if}
        {#if status.servo_pct != null && status.servo_pct > 0}
          <span>Servo <b class="opacity-100">{fmt(status.servo_pct)}%</b></span>
        {/if}
        {#if status.lid_countdown > 0}
          <span class="text-yellow-400">Lid open {status.lid_countdown}s</span>
        {/if}
        {#if fuelInfo.alerted}
          <span class="text-red-500 font-semibold">Add fuel</span>
        {:else if fuelInfo.depleting && fuelInfo.est_secs_to_max}
          <span class="text-amber-500">Fuel {fuelHours(fuelInfo.est_secs_to_max)}</span>
        {/if}
        {#if pitGuard.fire_dying}
          <span class="text-red-500 font-semibold">Fire dying</span>
        {:else if pitGuard.low}
          <span class="text-amber-500 font-semibold">Pit running low</span>
        {:else if pitGuard.overtemp}
          <span class="text-red-500 font-semibold">Running hot</span>
        {/if}
      </div>
      {#if servePlan?.enabled}
        <div class="mt-1.5 text-sm tabular-nums">
          Dinner <b>{fmtClock(servePlan.serve_ts)}</b>
          {#if servePlan.assessment?.status === 'late'}
            <span class="text-red-500 font-semibold">· {servePlan.assessment?.plateau_temp != null ? 'levelling off' : 'running late'}</span>
          {:else if servePlan.assessment?.status === 'at_risk'}
            <span class="text-amber-500 font-semibold">· may run late</span>
          {:else if servePlan.assessment?.status === 'on_track'}
            <span class="text-green-600 dark:text-green-400">· on track</span>
          {:else if servePlan.assessment?.status === 'early'}
            <span class="text-sky-500">· ahead</span>
          {/if}
          {#if servePlan.assessment?.ready_at}
            <span class="opacity-60">· ready ~{fmtClock(servePlan.assessment.ready_at)}{servePlan.assessment.ready_at_high - servePlan.assessment.ready_at > 600 ? '–' + fmtClock(servePlan.assessment.ready_at_high) : ''}</span>
          {/if}
        </div>
      {/if}
    </div>

    <div class="mt-5">
      <div class="flex items-center gap-2">
        <button class="w-11 h-11 shrink-0 rounded-xl grid place-items-center bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white active:opacity-70"
                onclick={() => bump(-10)} aria-label={`Lower the setpoint 10°`}>
          <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12" /></svg>
        </button>
        <input class="flex-1 min-w-0 text-center text-2xl font-semibold bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white rounded-xl h-11 tabular-nums"
               type="number" inputmode="numeric" aria-label={`Pit setpoint, °${unit}`} aria-describedby="sp-note"
               bind:value={spInput} oninput={() => { spDirty = true; spError = ''; }}
               onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); setSp(); } }} />
        <button class="w-11 h-11 shrink-0 rounded-xl grid place-items-center bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white active:opacity-70"
                onclick={() => bump(10)} aria-label={`Raise the setpoint 10°`}>
          <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12" /><line x1="12" y1="5" x2="12" y2="19" /></svg>
        </button>
        <button class={'px-5 h-11 shrink-0 rounded-xl bg-orange-600 text-white font-semibold active:bg-orange-700 disabled:opacity-60 ' + (spUnsent ? 'ring-2 ring-offset-2 ring-orange-500 ring-offset-white dark:ring-offset-neutral-900' : '')}
                onclick={setSp} disabled={spBusy}>{spBusy ? 'Setting…' : 'Set'}</button>
      </div>
      <p id="sp-note" class="text-xs mt-2 min-h-4 {spError ? 'text-red-600 dark:text-red-400' : 'text-amber-700 dark:text-amber-300'}">
        {#if spError}{spError}{:else if spUnsent}Not sent yet. Tap Set to change the pit to {spInput}°.{/if}
      </p>
      <div class="mt-2 grid grid-cols-3 gap-2">
        {#if status.lid_countdown > 0}
          <button class="h-11 rounded-xl bg-amber-500/15 text-amber-800 dark:text-amber-200 text-sm font-semibold leading-tight flex flex-col items-center justify-center"
                  onclick={lidCancel}>
            <span>Cancel lid</span><span class="text-xs font-normal tabular-nums opacity-80">{status.lid_countdown}s left</span>
          </button>
        {:else}
          <button class="h-11 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-sm font-semibold" onclick={lidOpen}>Lid open</button>
        {/if}
        <button class="h-11 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-sm font-semibold" onclick={openManual}>Fan override</button>
        <button class="h-11 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-sm font-semibold text-red-600 dark:text-red-400 disabled:opacity-40 disabled:text-current"
                onclick={() => (offOpen = true)} disabled={!isCooking}>Turn off</button>
      </div>
    </div>
  </div>
{/snippet}

{#snippet probeCard(f, i, extra)}
  <button class={'hm-card rounded-2xl p-4 text-left w-full transition-colors hover:border-orange-500/40 ' + extra} style={tintStyle(f.val, targetFor(f.pi))} onclick={() => openProbe(i)}>
    <div class="flex items-center justify-between gap-1">
      <span class="text-[11px] uppercase tracking-wider opacity-50 truncate font-semibold">{f.label}</span>
      {#if health[f.ch] === 'disconnected' || health[f.ch] === 'fault'}
        <span class="text-[10px] shrink-0 px-1.5 py-0.5 rounded bg-red-500/15 text-red-500 font-semibold">
          {health[f.ch] === 'fault' ? 'sensor fault' : 'disconnected'}
        </span>
      {:else}
        <!-- Hint only where there's room (desktop); on the narrow mobile cards it
             would crowd the probe name. -->
        <span class="text-[10px] opacity-40 shrink-0 hidden lg:inline">tap to set</span>
      {/if}
    </div>
    {#if f.val == null && !(health[f.ch] === 'disconnected' || health[f.ch] === 'fault')}
      <div class="text-sm opacity-50 mt-2 leading-tight">Not plugged in</div>
    {:else}
      <div class={'font-display text-3xl lg:text-4xl font-bold leading-none mt-1.5 tabular-nums transition-opacity ' + f.cls + (stale ? ' opacity-40' : '')}>
        {fmt(f.val)}<span class="text-sm opacity-50 align-top relative top-[0.5em]">°</span>
      </div>
    {/if}
    <span class="sr-only">. Set target.</span>
    <!-- Always reserve the target/ETA line so the card height is constant
         (no layout shift when a target or ETA appears/disappears). -->
    <div class="text-xs mt-1.5 tabular-nums min-h-[2.5rem] {targetFor(f.pi) != null && doneAt(f.val, targetFor(f.pi)) ? 'text-green-500 font-semibold' : 'opacity-60'}">
      {#if targetFor(f.pi) != null}→ {targetFor(f.pi)}°{etaSuffix(f)}{/if}
    </div>
  </button>
{/snippet}

{#snippet guidedStrip()}
  {#if guided}
    <div class={'hm-card rounded-2xl px-4 py-3 flex items-center gap-3 ' + (guided.done ? 'border border-green-600/40' : '')}>
      <svg viewBox="0 0 24 24" fill="currentColor" class="w-5 h-5 text-orange-500 shrink-0" aria-hidden="true">
        <path d="M12 23a7 7 0 0 0 7-7c0-2.1-1-4-2.6-5.5-.2 1.2-1.1 2.1-2.3 2.1.9-2.5.2-5.2-2.3-7.9-.5 2.7-2 3.8-3.6 5.1C6.7 11.4 5 13.5 5 16a7 7 0 0 0 7 7z" />
      </svg>
      <div class="flex-1 min-w-0">
        <div class="text-sm font-semibold truncate">{guided.label}
          {#if !guided.done && etas[guided.channel]?.eta_seconds > 0}
            <span class="opacity-50 font-normal tabular-nums"> · ~done {fmtClock(Date.now() / 1000 + etas[guided.channel].eta_seconds)}</span>
          {/if}
        </div>
        <div class="text-xs opacity-60 truncate">
          {#if guided.done}Target reached. Rest, then enjoy.{:else if guidedNext}Next: {guidedNext}{/if}
        </div>
      </div>
      {#if guided.wrap_pending}
        <button class="shrink-0 px-3 py-1.5 rounded-lg bg-orange-600 text-white text-sm font-semibold" onclick={guidedWrapped}>I wrapped it</button>
      {/if}
    </div>
  {/if}
{/snippet}

{#if embedded}
  <!-- Desktop: pit panel on the left, probes stacked vertically beside it. The
       pit card grows (flex-1) to match the probe column height (no gap). -->
  <div class="space-y-4">
    {@render guidedStrip()}
    <div class="grid grid-cols-3 gap-4 items-stretch">
      <div class="col-span-2 flex flex-col">{@render pit('flex-1')}</div>
      <div class="flex flex-col gap-3">
        <h2 class="sr-only">Food probes</h2>
        {#each foods as f, i}{@render probeCard(f, i, 'flex-1 flex flex-col justify-center')}{/each}
      </div>
    </div>
  </div>
{:else}
  <!-- Mobile/tablet: pit on top, probes side by side in a row. -->
  <div class="px-4 pt-4 pb-28 space-y-4">
    {@render guidedStrip()}
    {@render pit()}
    <h2 class="sr-only">Food probes</h2>
    <div class="grid grid-cols-3 gap-3">
      {#each foods as f, i}{@render probeCard(f, i, '')}{/each}
    </div>
  </div>
{/if}

<div class="sr-only" aria-live="assertive">{announce}</div>

<Dialog bind:open={probeOpen} title={`${foods[probeI]?.label} target`}>
  <div class="text-sm opacity-60 tabular-nums">Now {fmt(foods[probeI]?.val)}°</div>

  <div>
    <label for="probe-preset" class="block text-xs font-semibold opacity-60 mb-1">Preset</label>
    <select id="probe-preset" class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={pPreset} onchange={onPresetPick}>
      <option value="">Choose a preset…</option>
      {#each meatCats as c}
        <optgroup label={c}>
          {#each meat.filter((m) => m.category === c) as m}
            <option value={m.key}>{m.label} ({m.temp_f}°)</option>
          {/each}
        </optgroup>
      {/each}
    </select>
  </div>

  <div class="flex gap-2">
    <div class="flex-1 min-w-0">
      <label for="probe-name" class="block text-xs font-semibold opacity-60 mb-1">Probe name</label>
      <input id="probe-name" class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" maxlength="22" bind:value={pName} />
    </div>
    <div class="w-24">
      <label for="probe-target" class="block text-xs font-semibold opacity-60 mb-1">Target °</label>
      <input id="probe-target" class="w-full text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 tabular-nums"
             type="number" inputmode="numeric" min="1" max="999" bind:value={pTarget} aria-describedby="probe-error"
             onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); saveProbe(); } }} />
    </div>
  </div>
  <p id="probe-error" class="text-sm text-red-600 dark:text-red-400" role={pError ? 'alert' : undefined}>{pError}</p>

  <div class="flex items-center justify-between">
    <button class="-ml-2 px-2 h-11 rounded-lg text-red-600 dark:text-red-400 text-sm font-semibold disabled:opacity-50" onclick={clearProbe} disabled={pBusy}>Clear target</button>
    <div class="flex gap-2">
      <button class="px-4 h-11 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (probeOpen = false)}>Cancel</button>
      <button class="px-4 h-11 rounded-lg bg-orange-600 text-white font-semibold disabled:opacity-60" onclick={saveProbe} disabled={pBusy}>{pBusy ? 'Saving…' : 'Save'}</button>
    </div>
  </div>
</Dialog>

<Dialog bind:open={manualOpen} title="Manual fan override" width="xs">
  <p class="text-sm opacity-70">Holds the fan at a fixed speed instead of letting the controller adjust it. Set a pit temperature to go back to automatic.</p>
  <div class="flex items-center gap-3">
    <input class="flex-1 min-w-0" type="range" min="0" max="100" bind:value={manualPct} aria-label="Fan speed" aria-valuetext={`${manualPct}%`} />
    <span class="w-12 text-right tabular-nums font-semibold">{manualPct}%</span>
  </div>
  <p class="text-xs opacity-60 tabular-nums">The controller has it at {currentFan}% now.</p>
  <div class="flex gap-2 justify-end">
    <button class="px-4 h-11 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (manualOpen = false)}>Cancel</button>
    <button class="px-4 h-11 rounded-lg bg-orange-600 text-white font-semibold disabled:opacity-60" onclick={applyManual} disabled={manualBusy}>{manualBusy ? 'Applying…' : 'Hold the fan'}</button>
  </div>
</Dialog>

<Dialog bind:open={offOpen} title="Turn off the pit?" width="xs">
  <p class="text-sm opacity-70">The fan stops and the pit cools down. The cook keeps logging until you finish it.</p>
  <div class="flex gap-2 justify-end">
    <button class="px-4 h-11 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (offOpen = false)} autofocus>Keep cooking</button>
    <button class="px-4 h-11 rounded-lg bg-red-600 text-white font-semibold disabled:opacity-60" onclick={confirmOff} disabled={offBusy}>{offBusy ? 'Turning off…' : 'Turn off'}</button>
  </div>
</Dialog>

<style>
  /* Match the probe-popup <select> height to its text inputs (40px). */
  select { height: 2.5rem; }
</style>
