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
    loadPitHistory();
    loadCookDone();
    clockTimer = setInterval(() => (nowMs = Date.now()), 1000);
    stop = connectWs((m) => {
      if (m.state) {
        apply(m);
        if (m.state.status) pushPit(m.ts ?? Date.now() / 1000, m.state.status.pit);
      }
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
    { label: probeLabel(names[1], 'Food 1'), val: status.food1, cls: 'text-green-500', bar: 'bg-green-500', pi: 1, ch: 'food1' },
    { label: probeLabel(names[2], 'Food 2'), val: status.food2, cls: 'text-sky-400', bar: 'bg-sky-400', pi: 2, ch: 'food2' },
    { label: probeLabel(names[3], 'Ambient'), val: status.ambient, cls: 'text-purple-400', bar: 'bg-purple-400', pi: 3, ch: 'ambient' },
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
  // A one-word state after a probe's target: " · done", " · stall", or "".
  function tileState(f) {
    const tg = targetFor(f.pi);
    if (tg == null) return '';
    if (doneAt(f.val, tg)) return ' · done';
    const e = etas[f.ch];
    if (e?.model === 'plateau') return ' · levelling off';
    if (e?.stalled) return ' · stall';
    return '';
  }

  // -- colour only where it carries information ------------------------------
  // The pit card takes colour only while Pit Guard is warning; food tiles show
  // a thin progress bar toward their target in the probe's own hue, green when
  // done. (The bars can be turned off in Settings -> Appearance.)
  let progressBars = $state(true);
  function readPrefs() { progressBars = localStorage.getItem('hm.heatColors') !== '0'; }
  // "Done" by the displayed (rounded) numbers, so a probe reads done exactly
  // when the shown temp meets the target (avoids 70.6→"71" looking not-done).
  const doneAt = (val, target) => val != null && target != null && Math.round(val) >= Math.round(target);
  function progressFor(f) {
    const tg = targetFor(f.pi);
    if (!progressBars || tg == null || tg <= 0 || f.val == null) return null;
    return { pct: Math.max(0, Math.min(1, f.val / tg)), done: doneAt(f.val, tg) };
  }
  const guardTone = $derived(pitGuard.fire_dying || pitGuard.overtemp ? 'bad' : pitGuard.low ? 'warn' : null);

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

  // -- pit trend: which way the pit is heading, from the last 10 minutes -------
  let pitHist = [];           // [{t, v}] in seconds, the last ~20 minutes
  let trend = $state(null);   // {per10, dir: 'up' | 'down' | 'flat'} or null
  function computeTrend() {
    const end = pitHist.length ? pitHist[pitHist.length - 1].t : null;
    const w = end == null ? [] : pitHist.filter((p) => p.t >= end - 600);
    if (w.length < 5 || end - w[0].t < 180) { trend = null; return; }
    const n = w.length;
    const mt = w.reduce((a, p) => a + p.t, 0) / n, mv = w.reduce((a, p) => a + p.v, 0) / n;
    let sxy = 0, sxx = 0;
    for (const p of w) { sxy += (p.t - mt) * (p.v - mv); sxx += (p.t - mt) ** 2; }
    const per10 = sxx ? (sxy / sxx) * 600 : 0;
    const flat = unit === 'C' ? 2 : 3;
    trend = { per10: Math.round(per10), dir: per10 > flat ? 'up' : per10 < -flat ? 'down' : 'flat' };
  }
  function pushPit(t, v, recompute = true) {
    if (typeof v !== 'number' || typeof t !== 'number') return;
    pitHist.push({ t, v });
    while (pitHist.length && pitHist[0].t < t - 1200) pitHist.shift();
    if (recompute) computeTrend();
  }
  async function loadPitHistory() {
    try {
      const h = await getJSON('history?minutes=20&limit=400');
      (h.t || []).forEach((t, i) => pushPit(t, h.pit?.[i], false));
      pitHist.sort((a, b) => a.t - b.t);
      computeTrend();
    } catch (_) {}
  }

  // -- the verdict: is everything fine, when will it be done, do I need to act --
  let cookDone = $state(null);  // completion settings (keep-warm on/off + temp)
  async function loadCookDone() { try { cookDone = await getJSON('cook-done'); } catch (_) {} }
  const keepWarmOn = $derived(cookDone?.on_complete === 'keep_warm');
  const keepWarmTemp = $derived(Math.round(cookDone?.keep_warm_temp ?? 150));
  const step = $derived(unit === 'C' ? { small: 8, big: 10 } : { small: 15, big: 20 });

  async function nudgePit(delta) {
    const was = spSent;
    if (!was) return;
    const v = was + delta;
    try {
      await sendSetpoint(v);
      toast(`Pit set to ${v}°${unit}.`, { actionLabel: 'Undo', action: () => restoreSetpoint(was) });
    } catch (_) { toast(unreachable(`the pit is still set to ${was}°`), { tone: 'error' }); }
  }
  async function keepWarm() {
    const prev = cookDone?.on_complete || 'notify';
    try {
      cookDone = await postJSON('cook-done', { on_complete: 'keep_warm' });
      toast(`When the food is done, the pit drops to ${keepWarmTemp}° to keep it warm.`, {
        actionLabel: 'Undo',
        action: async () => {
          try { cookDone = await postJSON('cook-done', { on_complete: prev }); }
          catch (_) { toast(unreachable('keep-warm is still on'), { tone: 'error' }); }
        },
      });
    } catch (_) { toast(unreachable("keep-warm wasn't turned on"), { tone: 'error' }); }
  }
  const openServePlan = () => window.dispatchEvent(new CustomEvent('hm-open', { detail: 'cook' }));

  // "About 3 h 50 min" / "about 40 min", rounded the way a cook would say it.
  function roughly(secs) {
    const s = Math.abs(secs);
    if (s >= 3600) {
      let h = Math.floor(s / 3600), m = Math.round((s % 3600) / 600) * 10;
      if (m === 60) { h += 1; m = 0; }
      return m ? `${h} h ${m} min` : `${h} h`;
    }
    return `${Math.max(5, Math.round(s / 300) * 5)} min`;
  }

  // Something that needs a person, worded as what is wrong and what to do.
  const alertLine = $derived.by(() => {
    if (stale) {
      return { tone: 'warn', title: 'Lost contact with the HeaterMeter.',
        text: wsOpen ? `No reading for ${ageText(ageSecs)}. The numbers above may be out of date.`
          : 'Reconnecting… The numbers above may be out of date.' };
    }
    const sp = status.set_point, pit = status.pit;
    if (pitGuard.fire_dying) {
      return { tone: 'bad', title: 'Fire dying.', text: 'The fan is maxed and the pit keeps falling. The fuel is likely spent: add charcoal now.' };
    }
    if (pitGuard.overtemp) {
      return { tone: 'bad', title: 'Pit running hot.',
        text: `${typeof pit === 'number' && sp > 0 ? Math.round(pit - sp) + '° over the setpoint. ' : ''}Check the damper and the lid.` };
    }
    if (pitGuard.low) {
      return { tone: 'warn', title: 'Pit running low.',
        text: `${typeof pit === 'number' && sp > 0 ? Math.round(sp - pit) + '° under the setpoint. ' : ''}Check the fire.` };
    }
    if (status.lid_countdown > 0) {
      return { tone: 'info', title: 'Lid open.', text: `The fan is paused. Control resumes in ${status.lid_countdown}s.` };
    }
    for (const f of foods) {
      if (health[f.ch] === 'disconnected') return { tone: 'warn', title: `${f.label} disconnected.`, text: 'Check the probe plug.' };
      if (health[f.ch] === 'fault') return { tone: 'warn', title: `${f.label} sensor fault.`, text: 'Check the probe and its cable.' };
    }
    if (fuelInfo.alerted) {
      return { tone: 'warn', title: 'Fuel running low.', text: 'The fan is working harder to hold temperature. Add charcoal soon.' };
    }
    return null;
  });

  // When each targeted food will be done, in plain words.
  const whenLines = $derived.by(() => {
    const out = [];
    for (const f of foods) {
      const tg = targetFor(f.pi);
      if (tg == null || f.val == null || health[f.ch] === 'disconnected' || health[f.ch] === 'fault') continue;
      const e = etas[f.ch];
      const at = e && e.eta_seconds > 0 ? '~' + fmtClock(Date.now() / 1000 + e.eta_seconds) : null;
      if (doneAt(f.val, tg)) out.push({ name: f.label, text: 'is done.', tone: 'good' });
      else if (e?.model === 'plateau') out.push({ name: f.label, text: e.plateau_temp != null ? `is levelling off near ${Math.round(e.plateau_temp)}°.` : 'is levelling off.', tone: 'warn' });
      else if (e?.stalled) out.push({ name: f.label, text: at ? 'is in the stall, done' : 'is in the stall.', time: at ? `${at} or later` : null });
      else if (at && e.confidence !== 'none') out.push({ name: f.label, text: 'done', time: at });
      else out.push({ name: f.label, text: 'has no done time yet.', tone: 'muted' });
    }
    const a = servePlan?.enabled ? servePlan.assessment : null;
    if (a?.ready_at) {
      const hi = a.ready_at_high && a.ready_at_high - a.ready_at > 600 ? '–' + fmtClock(a.ready_at_high) : '';
      const rest = servePlan.rest_secs ? ` after a ${roughly(servePlan.rest_secs)} rest` : '';
      out.push({ name: 'Ready to eat', text: '', time: `~${fmtClock(a.ready_at)}${hi}`, after: rest ? `${rest}.` : '.' });
    }
    if (!out.length && isCooking) out.push({ name: '', text: 'No food target yet. Tap a probe to set one.', tone: 'muted' });
    return out;
  });

  // What, if anything, to do about it: the serve plan's verdict with its levers.
  const act = $derived.by(() => {
    if (stale || alertLine) return null;
    const lever = {
      drop: (n) => ({ label: `Drop the pit ${n}°`, run: () => nudgePit(-n) }),
      raise: (n) => ({ label: `Raise the pit ${n}°`, run: () => nudgePit(n) }),
      warm: () => ({ label: 'Keep warm when done', run: keepWarm }),
      time: () => ({ label: 'Change the time', run: openServePlan }),
    };
    const canSteer = spSent != null;
    const a = servePlan?.enabled ? servePlan.assessment : null;
    if (a) {
      const when = `to eat at ${fmtClock(servePlan.serve_ts)}`;
      const stalled = whenLines.some((w) => w.text.startsWith('is in the stall'));
      switch (a.status) {
        case 'early': {
          const levers = [];
          if (!keepWarmOn) levers.push(lever.warm());
          if (canSteer) levers.push(lever.drop(step.small));
          return { tone: 'info', text: `About ${roughly(a.slack_high_secs ?? a.slack_secs)} early ${when}.` +
            (keepWarmOn ? ` Keep-warm will hold it at ${keepWarmTemp}°.` : ''), levers };
        }
        case 'at_risk':
          return { tone: 'warn', text: `Might run late ${when}.` + (stalled ? ' Wrapping now would help.' : ''),
            levers: canSteer ? [lever.raise(step.small)] : [] };
        case 'late':
          if (a.plateau_temp != null) {
            return { tone: 'bad', text: `Levelling off near ${Math.round(a.plateau_temp)}°, so it won't finish at this pit temperature.`,
              levers: [...(canSteer ? [lever.raise(step.big)] : []), lever.time()] };
          }
          return { tone: 'bad', text: `About ${roughly(a.slack_secs)} late ${when}.` + (stalled ? ' Wrap now to push through the stall.' : ''),
            levers: [...(canSteer ? [lever.raise(step.big)] : []), lever.time()] };
        case 'on_track':
          return { tone: 'good', text: `On track ${when}.`, levers: [] };
        case 'no_target':
          return { tone: 'muted', text: `Set a food target to plan for ${fmtClock(servePlan.serve_ts)}.`, levers: [] };
        case 'no_eta':
          return { tone: 'muted', text: `Working out whether it will be ready ${when}.`, levers: [] };
        case 'past':
          return { tone: 'muted', text: `It's past ${fmtClock(servePlan.serve_ts)}, the time you planned to eat.`, levers: [lever.time()] };
      }
    }
    const plateau = whenLines.find((w) => w.tone === 'warn');
    if (plateau && canSteer) {
      return { tone: 'warn', text: `Raise the pit ${step.small}–${step.big + 5}° to finish.`, levers: [lever.raise(step.big)] };
    }
    return isCooking ? { tone: 'muted', text: 'Nothing needs you right now.', levers: [] } : null;
  });

  const toneCls = (t) => ({
    bad: 'text-red-600 dark:text-red-400',
    warn: 'text-amber-700 dark:text-amber-300',
    good: 'text-green-700 dark:text-green-400',
    muted: 'opacity-60',
  }[t] || '');

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

{#snippet trendIcon(dir)}
  <svg viewBox="0 0 24 24" class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {#if dir === 'up'}<path d="M6 17 17 6" /><path d="M9 6h8v8" />
    {:else if dir === 'down'}<path d="M6 7l11 11" /><path d="M17 10v8H9" />
    {:else}<path d="M4 12h15" /><path d="M15 8l4 4-4 4" />{/if}
  </svg>
{/snippet}

{#snippet pit(extra = '')}
  <section class={'hm-card rounded-3xl p-5 ' + (guardTone ? `hm-alert-${guardTone} ` : '') + extra} aria-labelledby="pit-h">
    <div class="flex items-center justify-between gap-2">
      <h2 id="pit-h" class="font-sans text-xs uppercase tracking-widest opacity-50 font-semibold">{names[0] || 'Pit'}</h2>
      <span class={'text-xs px-2 py-0.5 rounded-full font-semibold tabular-nums ' + pillCls}>{freshness ?? ps.label}</span>
    </div>

    <div class="flex items-end justify-between gap-3 mt-2">
      <div class={'font-display text-7xl lg:text-8xl font-black text-orange-500 leading-none tracking-tight tabular-nums transition-opacity ' + (stale ? 'opacity-40' : '')}>
        {fmt(status.pit)}<span class="text-2xl opacity-50 align-top relative top-[0.12em]">°{unit}</span>
      </div>
      {#if trend && !stale}
        <div class="pb-1 text-right tabular-nums leading-tight shrink-0">
          <div class="flex items-center justify-end gap-1 text-sm font-semibold">
            {@render trendIcon(trend.dir)}
            <span>{trend.dir === 'flat' ? 'Steady' : (trend.per10 > 0 ? '+' : '−') + Math.abs(trend.per10) + '°'}</span>
          </div>
          <div class="text-xs opacity-60">{trend.dir === 'flat' ? 'last 10 min' : 'in 10 min'}</div>
        </div>
      {/if}
    </div>

    <div class="flex flex-wrap gap-x-5 gap-y-1 mt-3 text-sm opacity-70 tabular-nums">
      <span>Set <b class="opacity-100">{status.set_point == null || status.set_point <= 0 ? '—' : fmt(status.set_point) + '°'}</b></span>
      <span>Fan <b class="opacity-100">{fmt(status.fan_pct)}%</b></span>
      {#if isCooking && cookStartTs}
        <span>Cooking <b class="opacity-100">{cookElapsed()}</b></span>
      {/if}
      {#if status.servo_pct != null && status.servo_pct > 0}
        <span>Damper <b class="opacity-100">{fmt(status.servo_pct)}%</b></span>
      {/if}
      {#if !fuelInfo.alerted && fuelInfo.depleting && fuelInfo.est_secs_to_max}
        <span>Fuel <b class="opacity-100">{fuelHours(fuelInfo.est_secs_to_max)}</b></span>
      {/if}
    </div>

    {#if alertLine || whenLines.length || act}
      <div class="mt-4 pt-4 border-t border-black/10 dark:border-white/10 space-y-1.5 text-[15px] leading-snug">
        {#if alertLine}
          <p class={toneCls(alertLine.tone)}><b class="font-semibold">{alertLine.title}</b> {alertLine.text}</p>
        {/if}
        {#each whenLines as w}
          <p class={toneCls(w.tone)}>
            {#if w.name}<span class="font-semibold">{w.name}</span>{/if}
            {w.text}
            {#if w.time}<b class="font-semibold tabular-nums">{w.time}</b>{/if}{w.after ?? ''}
          </p>
        {/each}
        {#if act}
          <p class={toneCls(act.tone)}>{act.text}</p>
          {#if act.levers.length}
            <div class="flex flex-wrap gap-2 pt-1.5">
              {#each act.levers as l}
                <button class="h-11 px-4 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-sm font-semibold" onclick={l.run}>{l.label}</button>
              {/each}
            </div>
          {/if}
        {/if}
      </div>
    {/if}
  </section>
{/snippet}

{#snippet controls()}
  <section class="hm-card rounded-2xl p-4" aria-labelledby="ctl-h">
    <h2 id="ctl-h" class="sr-only">Pit controls</h2>
    <div class="lg:flex lg:items-start lg:gap-4">
      <div class="lg:flex-1 lg:max-w-md">
        <div class="flex items-center gap-2">
          <button class="w-11 h-11 shrink-0 rounded-xl grid place-items-center bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white active:opacity-70"
                  onclick={() => bump(-10)} aria-label="Lower the setpoint 10°">
            <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12" /></svg>
          </button>
          <input class="flex-1 min-w-0 text-center text-2xl font-semibold bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white rounded-xl h-11 tabular-nums"
                 type="number" inputmode="numeric" aria-label={`Pit setpoint, °${unit}`} aria-describedby="sp-note"
                 bind:value={spInput} oninput={() => { spDirty = true; spError = ''; }}
                 onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); setSp(); } }} />
          <button class="w-11 h-11 shrink-0 rounded-xl grid place-items-center bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white active:opacity-70"
                  onclick={() => bump(10)} aria-label="Raise the setpoint 10°">
            <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12" /><line x1="12" y1="5" x2="12" y2="19" /></svg>
          </button>
          <button class={'px-5 h-11 shrink-0 rounded-xl bg-orange-600 text-white font-semibold active:bg-orange-700 disabled:opacity-60 ' + (spUnsent ? 'ring-2 ring-offset-2 ring-orange-500 ring-offset-white dark:ring-offset-neutral-900' : '')}
                  onclick={setSp} disabled={spBusy}>{spBusy ? 'Setting…' : 'Set'}</button>
        </div>
        <p id="sp-note" class="text-xs mt-1 min-h-4 {spError ? 'text-red-600 dark:text-red-400' : 'text-amber-700 dark:text-amber-300'}">
          {#if spError}{spError}{:else if spUnsent}Not sent yet. Tap Set to change the pit to {spInput}°.{/if}
        </p>
      </div>
      <div class="mt-1 lg:mt-0 lg:ml-auto grid grid-cols-3 gap-2 lg:w-[22rem]">
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
  </section>
{/snippet}

{#snippet probeCard(f, i, extra)}
  <button class={'hm-card hm-tile rounded-2xl p-4 text-left w-full ' + extra} onclick={() => openProbe(i)}>
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
    <!-- Always reserve the target line so the card height is constant (no
         layout shift when a target appears or disappears). The done-by clock
         lives in the pit card's verdict. -->
    <div class="text-xs mt-1.5 tabular-nums min-h-4 {targetFor(f.pi) != null && doneAt(f.val, targetFor(f.pi)) ? 'text-green-600 dark:text-green-400 font-semibold' : 'opacity-60'}">
      {#if targetFor(f.pi) != null}→ {targetFor(f.pi)}°{tileState(f)}{/if}
    </div>
    {#if progressFor(f)}
      {@const p = progressFor(f)}
      <div class="mt-2 h-1 rounded-full bg-black/5 dark:bg-white/10 overflow-hidden" aria-hidden="true">
        <div class={'h-full rounded-full ' + (p.done ? 'bg-green-600 dark:bg-green-500' : f.bar)} style={`width: ${Math.round(p.pct * 100)}%`}></div>
      </div>
    {/if}
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
  <!-- Desktop: the pit card and its controls on the left, the food probes
       stacked beside them, stretching to the same height. -->
  <div class="space-y-4">
    {@render guidedStrip()}
    <div class="grid grid-cols-3 gap-4 items-stretch">
      <div class="col-span-2 flex flex-col gap-4">
        {@render pit('flex-1')}
        {@render controls()}
      </div>
      <div class="flex flex-col gap-3">
        <h2 class="sr-only">Food probes</h2>
        {#each foods as f, i}{@render probeCard(f, i, 'flex-1 flex flex-col justify-center')}{/each}
      </div>
    </div>
  </div>
{:else}
  <!-- Phone: read top to bottom (the pit and its verdict, then the food), with
       the controls at the bottom where the thumb is. -->
  <div class="px-4 pt-4 pb-28 space-y-4">
    {@render guidedStrip()}
    {@render pit()}
    <h2 class="sr-only">Food probes</h2>
    <div class="grid grid-cols-3 gap-3">
      {#each foods as f, i}{@render probeCard(f, i, '')}{/each}
    </div>
    {@render controls()}
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
