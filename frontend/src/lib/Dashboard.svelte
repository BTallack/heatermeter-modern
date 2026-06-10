<script>
  import { onMount, onDestroy } from 'svelte';
  import { getJSON, postJSON, connectWs } from './api.js';
  import { fmtClock } from './util.js';

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
  let modeLabel = $state('');
  let unit = $state('F');
  let spInput = $state(225);
  let spDirty = $state(false);
  let health = $state({});   // channel -> 'disconnected' | 'fault' | 'ok'
  let fuelInfo = $state({}); // blower-effort fuel assessment from the daemon
  let guided = $state(null); // active guided cook (null when none)
  let stop;
  // Next unfired milestone's prompt = "what to expect next" on the strip.
  const guidedNext = $derived(
    guided?.milestones?.find((m) => !m.fired)?.prompt || null);
  async function guidedWrapped() {
    try { await postJSON('guided/wrapped', {}); } catch (_) {}
  }
  function fuelHours(s) { const h = s / 3600; return h >= 1 ? `~${h.toFixed(1)}h` : `~${Math.round(s / 60)}m`; }

  function apply(d) {
    if (!d || !d.state) return;
    status = d.state.status || {};
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
    guided = d.state.guided ?? guided;
    modeLabel = status.pid_mode_label || '';
    if (!spDirty && typeof status.set_point === 'number') spInput = Math.round(status.set_point);
  }

  onMount(async () => {
    readPrefs();
    window.addEventListener('hm-prefs', readPrefs);
    try { apply({ state: await getJSON('status') }); } catch (_) {}
    try { meat = (await getJSON('presets')).meat || []; } catch (_) {}
    refreshEtas();
    etaTimer = setInterval(refreshEtas, 25000);
    stop = connectWs((m) => {
      if (m.state) apply(m);
      if (m.event?.type === 'probe_event') {
        const ch = m.event.channel;
        if (m.event.kind === 'disconnect') health = { ...health, [ch]: 'disconnected' };
        else if (m.event.kind === 'reconnect') health = { ...health, [ch]: 'ok' };
        else if (m.event.kind === 'fault') health = { ...health, [ch]: 'fault' };
      }
    });
  });
  onDestroy(() => { stop && stop(); clearInterval(etaTimer); window.removeEventListener('hm-prefs', readPrefs); });

  const fmt = (v) => (v == null || Number.isNaN(v)) ? '--' : Math.round(v);
  function bump(n) { spInput = Math.max(0, (parseInt(spInput, 10) || 0) + n); spDirty = true; }
  async function setSp() {
    try { await postJSON('setpoint', { value: parseInt(spInput, 10) || 0, unit }); spDirty = false; }
    catch (_) {}
  }
  async function setOff() { try { await postJSON('command', { path: '/set?sp=O' }); } catch (_) {} }

  // Food/ambient probes. pi = probe index on the board (1/2/3); the high-alarm
  // threshold for probe i lives at alarms[2*i + 1].
  const foods = $derived([
    { label: names[1] || 'Food 1', val: status.food1, cls: 'text-green-500', pi: 1, ch: 'food1' },
    { label: names[2] || 'Food 2', val: status.food2, cls: 'text-sky-400', pi: 2, ch: 'food2' },
    { label: names[3] || 'Ambient', val: status.ambient, cls: 'text-purple-400', pi: 3, ch: 'ambient' },
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
  async function lidOpen() { try { await postJSON('lid/open'); } catch (_) {} }
  async function lidCancel() { try { await postJSON('lid/cancel'); } catch (_) {} }

  let manualOpen = $state(false);
  let manualPct = $state(0);
  async function applyManual() {
    try { await postJSON('manual', { percent: Number(manualPct) || 0 }); } catch (_) {}
    manualOpen = false;
  }

  // -- probe target popup ----------------------------------------------------
  let probeOpen = $state(false);
  let probeI = $state(0);     // index into foods
  let pName = $state('');
  let pTarget = $state('');
  let pPreset = $state('');
  const meatCats = $derived([...new Set(meat.map((m) => m.category))]);

  function openProbe(i) {
    probeI = i;
    const pi = foods[i].pi;
    pName = names[pi] || foods[i].label;
    const t = targetFor(pi);
    pTarget = t == null ? '' : String(t);
    pPreset = '';
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
    } catch (_) {}
    probeOpen = false;
  }
  function clearProbe() { pTarget = ''; saveProbe(); }
</script>

{#snippet pit(extra = '')}
  <div class={'hm-card rounded-3xl p-5 flex flex-col justify-between ' + extra} style={tintStyle(status.pit, status.set_point)}>
    <div>
      <div class="flex items-center justify-between">
        <span class="text-xs uppercase tracking-widest opacity-50 font-semibold">{names[0] || 'Pit'}</span>
        {#if modeLabel}
          <span class="text-xs px-2 py-0.5 rounded-full bg-orange-600/20 text-orange-400 font-semibold">{modeLabel}</span>
        {:else}
          <span class="text-xs px-2 py-0.5 rounded-full bg-neutral-500/15 opacity-60 font-semibold">Off</span>
        {/if}
      </div>
      <div class="font-display text-7xl lg:text-8xl font-black text-orange-500 leading-none tracking-tight tabular-nums mt-2">
        {fmt(status.pit)}<span class="text-2xl opacity-50 align-top relative top-[0.12em]">°{unit}</span>
      </div>
      <div class="flex flex-wrap gap-x-5 gap-y-1 mt-4 text-sm opacity-70 tabular-nums">
        <span>Set <b class="opacity-100">{status.set_point == null ? '—' : fmt(status.set_point) + '°'}</b></span>
        <span>Fan <b class="opacity-100">{fmt(status.fan_pct)}%</b></span>
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
      </div>
    </div>

    <div class="mt-5">
      <div class="flex items-center gap-2">
        <button class="w-11 h-11 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white text-xl active:opacity-70" onclick={() => bump(-10)}>−</button>
        <input class="flex-1 min-w-0 text-center text-2xl font-semibold bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white rounded-xl h-11 tabular-nums" type="number" bind:value={spInput} oninput={() => (spDirty = true)} />
        <button class="w-11 h-11 rounded-xl bg-neutral-200 dark:bg-neutral-800 text-black dark:text-white text-xl active:opacity-70" onclick={() => bump(10)}>+</button>
        <button class="px-5 h-11 rounded-xl bg-orange-600 text-white font-semibold active:bg-orange-700" onclick={setSp}>Set</button>
      </div>
      <div class="mt-3 flex items-center gap-4 text-xs">
        <button class="opacity-50 underline hover:opacity-80" onclick={setOff}>Turn off</button>
        {#if status.lid_countdown > 0}
          <button class="text-yellow-500 underline" onclick={lidCancel}>Cancel lid ({status.lid_countdown}s)</button>
        {:else}
          <button class="opacity-50 underline hover:opacity-80" onclick={lidOpen}>Lid open</button>
        {/if}
        <button class="opacity-50 underline hover:opacity-80" onclick={() => (manualOpen = true)}>Fan override</button>
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
    <div class="font-display text-3xl lg:text-4xl font-bold {f.cls} leading-none mt-1.5 tabular-nums">
      {fmt(f.val)}<span class="text-sm opacity-50 align-top relative top-[0.5em]">°</span>
    </div>
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
        {#each foods as f, i}{@render probeCard(f, i, 'flex-1 flex flex-col justify-center')}{/each}
      </div>
    </div>
  </div>
{:else}
  <!-- Mobile/tablet: pit on top, probes side by side in a row. -->
  <div class="px-4 pt-4 pb-28 space-y-4">
    {@render guidedStrip()}
    {@render pit()}
    <div class="grid grid-cols-3 gap-3">
      {#each foods as f, i}{@render probeCard(f, i, '')}{/each}
    </div>
  </div>
{/if}

{#if probeOpen}
  <div class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" role="presentation" onclick={() => (probeOpen = false)}>
    <div class="hm-card rounded-2xl w-full max-w-sm p-4 space-y-3" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-center justify-between">
        <h3 class="font-bold text-lg">{foods[probeI]?.label} target</h3>
        <button class="text-2xl leading-none px-2 opacity-60 hover:opacity-100" onclick={() => (probeOpen = false)} aria-label="Close">✕</button>
      </div>
      <div class="text-sm opacity-60 tabular-nums">Current: {fmt(foods[probeI]?.val)}°</div>

      <div>
        <label class="block text-xs font-semibold opacity-60 mb-1">Preset</label>
        <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={pPreset} onchange={onPresetPick}>
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
          <label class="block text-xs font-semibold opacity-60 mb-1">Probe name</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" maxlength="22" bind:value={pName} />
        </div>
        <div class="w-24">
          <label class="block text-xs font-semibold opacity-60 mb-1">Target °</label>
          <input class="w-full text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 tabular-nums" type="number" bind:value={pTarget} />
        </div>
      </div>

      <div class="flex items-center justify-between pt-1">
        <button class="px-3 py-2 rounded-lg text-red-500 text-sm" onclick={clearProbe}>Clear target</button>
        <div class="flex gap-2">
          <button class="px-4 py-2 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (probeOpen = false)}>Cancel</button>
          <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={saveProbe}>Save</button>
        </div>
      </div>
    </div>
  </div>
{/if}

{#if manualOpen}
  <div class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" role="presentation" onclick={() => (manualOpen = false)}>
    <div class="hm-card rounded-2xl w-full max-w-xs p-4 space-y-3" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-center justify-between">
        <h3 class="font-bold text-lg">Manual fan override</h3>
        <button class="text-2xl leading-none px-2 opacity-60 hover:opacity-100" onclick={() => (manualOpen = false)} aria-label="Close">✕</button>
      </div>
      <p class="text-xs opacity-60">Drives the fan directly, bypassing PID control. Set a pit temperature to return to automatic control.</p>
      <div class="flex items-center gap-3">
        <input class="flex-1 min-w-0" type="range" min="0" max="100" bind:value={manualPct} />
        <span class="w-12 text-right tabular-nums font-semibold">{manualPct}%</span>
      </div>
      <div class="flex gap-2 justify-end pt-1">
        <button class="px-4 py-2 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (manualOpen = false)}>Cancel</button>
        <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={applyManual}>Apply</button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Match the probe-popup <select> height to its text inputs (40px). */
  select { height: 2.5rem; }
</style>
