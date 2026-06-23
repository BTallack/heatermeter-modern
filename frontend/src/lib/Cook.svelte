<script>
  import { onMount, onDestroy } from 'svelte';
  import { getJSON, postJSON, patchJSON, delJSON, connectWs, authToken } from './api.js';
  import { fmt, fmtClock, fmtDuration } from './util.js';

  // The report opens in a new tab, so the bearer header is not sent; carry the
  // token in the query string when auth is enabled (the API accepts ?token=).
  const reportHref = (id) =>
    '/api/report/' + id + (authToken() ? '?token=' + encodeURIComponent(authToken()) : '');

  const AMB_KEY = 'hm.ambientAsFood';
  let ambFood = $state(localStorage.getItem(AMB_KEY) === '1');

  let status = $state({});
  let names = $state(['Pit', 'Food 1', 'Food 2', 'Ambient']);
  let alarms = $state([]);
  let meat = $state([]);
  let unit = $state('F');

  // targets keyed by channel
  let t1 = $state(''), t2 = $state(''), t3 = $state('');

  // program
  let prog = $state({ running: false });
  let stages = $state([]);
  let progName = $state('');
  let saved = $state([]);
  let progTimer;

  // program presets
  let programPresets = $state([]);
  let presetSel = $state('');
  let presetNote = $state('');
  const presetCats = $derived([...new Set(programPresets.map((p) => p.category))]);

  // sessions
  let sessions = $state([]);

  let stop;

  function parseTarget(v) {
    if (v == null) return '';
    const n = parseFloat(String(v).replace(/[LH]$/, ''));
    return (Number.isNaN(n) || n < 0) ? '' : String(n);
  }

  function apply(d) {
    if (!d || !d.state) return;
    status = d.state.status || {};
    names = d.state.probe_names || names;
    alarms = d.state.alarms || alarms;
    if (d.state.pid && d.state.pid.units) unit = d.state.pid.units;
    t1 = parseTarget(alarms[3]); t2 = parseTarget(alarms[5]);
    t3 = ambFood ? parseTarget(alarms[7]) : '';
  }

  const probeRows = $derived([
    { key: 't1', label: names[1] || 'Food 1', temp: status.food1, get: () => t1, set: (v) => (t1 = v) },
    { key: 't2', label: names[2] || 'Food 2', temp: status.food2, get: () => t2, set: (v) => (t2 = v) },
    ...(ambFood ? [{ key: 't3', label: names[3] || 'Ambient', temp: status.ambient, get: () => t3, set: (v) => (t3 = v) }] : []),
  ]);

  async function saveTargets() {
    const num = (s) => (s === '' || s == null ? null : parseFloat(s));
    const th = [null, null, null, num(t1), null, num(t2), null, ambFood ? num(t3) : null];
    try { await postJSON('alarms', { thresholds: th }); } catch (_) {}
  }
  function toggleAmb() {
    ambFood = !ambFood;
    localStorage.setItem(AMB_KEY, ambFood ? '1' : '0');
    if (ambFood) t3 = parseTarget(alarms[7]);
  }

  // -- cook control (start / stop) -------------------------------------------
  let startTemp = $state(225);
  const quickTemps = [225, 250, 275, 325];
  const pitOn = $derived(Number(status.set_point) > 0);
  const liveSession = $derived(sessions.find((s) => !s.ended_ts && !s.completed_ts) || null);

  async function startCook() {
    const v = Number(startTemp);
    if (!(v > 0)) return;
    try { await postJSON('setpoint', { value: v, unit }); } catch (_) {}
  }
  async function turnOffPit() {
    if (!confirm('Turn the pit off? This stops heating.')) return;
    try { await postJSON('command', { path: '/set?sp=O' }); } catch (_) {}
  }
  function fmtElapsed(startTs) {
    if (!startTs) return '';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - startTs));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  // -- cook program ----------------------------------------------------------
  const running = $derived(!!(prog && prog.stage_count != null && !prog.done));

  function newStage() { return { name: '', setpoint: 225, advanceType: 'probe', channel: 'food1', temp: 203, minutes: 30 }; }
  function addStage() { stages = [...stages, newStage()]; }
  function removeStage(i) { stages = stages.filter((_, j) => j !== i); }

  function toApiStages(list) {
    return list.map((s) => {
      const advance = { type: s.advanceType };
      if (s.advanceType === 'time') advance.seconds = (Number(s.minutes) || 0) * 60;
      else if (s.advanceType === 'probe') { advance.channel = s.channel; advance.temp = Number(s.temp) || 0; }
      const sp = (s.setpoint === '' || s.setpoint == null) ? 'off' : Number(s.setpoint);
      return { name: s.name || '', setpoint: sp, advance };
    });
  }

  async function loadProgram() {
    try { prog = await getJSON('program'); } catch (_) {}
  }
  async function loadSaved() { try { saved = await getJSON('programs'); } catch (_) {} }

  async function startProgram(list, name) {
    if (!list.length) return;
    try { await postJSON('program/start', { stages: toApiStages(list), name: name || '' }); await loadProgram(); }
    catch (_) {}
  }
  async function advance() { try { await postJSON('program/advance'); await loadProgram(); } catch (_) {} }
  async function stopProgram() { try { await postJSON('program/stop'); await loadProgram(); } catch (_) {} }
  async function saveProgram() {
    if (!progName.trim() || !stages.length) return;
    try { await postJSON('programs', { name: progName.trim(), stages: toApiStages(stages) }); await loadSaved(); }
    catch (_) {}
  }
  async function deleteProgram(id) { try { await delJSON('programs/' + id); await loadSaved(); } catch (_) {} }
  function loadStages(p) { stages = (p.stages || []).map((s) => ({
    name: s.name || '', setpoint: s.setpoint === 'off' || s.setpoint == null ? '' : s.setpoint,
    advanceType: s.advance?.type || 'manual', channel: s.advance?.channel || 'food1',
    temp: s.advance?.temp || 203, minutes: s.advance?.seconds ? Math.round(s.advance.seconds / 60) : 30,
  })); progName = p.name || ''; }

  function loadPreset() {
    const p = programPresets.find((x) => x.key === presetSel);
    if (!p) { presetNote = ''; return; }
    loadStages({ name: p.label, stages: p.stages });
    presetNote = p.note || '';
  }

  // -- sessions --------------------------------------------------------------
  async function loadSessions() { try { sessions = await getJSON('sessions'); } catch (_) {} }
  async function finishCook() {
    if (!confirm('Finish the current cook now? This marks it complete and applies your cook-completion action.')) return;
    try { await postJSON('cook/finish', {}); } catch (_) {}
    await loadSessions();
  }
  async function renameSession(s) {
    const name = prompt('Rename cook', s.name || '');
    if (name == null) return;
    try { await patchJSON('sessions/' + s.id, { name }); await loadSessions(); } catch (_) {}
  }
  async function deleteSession(s) {
    if (!confirm('Delete this cook and its data?')) return;
    try { await delJSON('sessions/' + s.id); await loadSessions(); } catch (_) {}
  }
  async function shareSession(s) {
    try {
      const r = await postJSON('sessions/' + s.id + '/share', { enabled: true });
      const url = location.origin + r.url;
      try { await navigator.clipboard.writeText(url); alert('Public link copied:\n' + url); }
      catch (_) { prompt('Public link:', url); }
    } catch (_) {}
  }

  // -- per-cook insights (computed from the session's history) ----------------
  let insightsFor = $state(null);
  let insights = $state(null);
  const _nums = (a) => (a || []).filter((v) => typeof v === 'number' && !Number.isNaN(v));
  const _avg = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  async function toggleInsights(s) {
    if (insightsFor === s.id) { insightsFor = null; insights = null; return; }
    insightsFor = s.id; insights = null;
    try {
      const h = await getJSON(`history?session_id=${s.id}&limit=20000`);
      const t = h.t || [], pit = _nums(h.pit), f1 = _nums(h.food1), f2 = _nums(h.food2), fan = _nums(h.fan_pct);
      const dur = ((s.ended_ts || t[t.length - 1]) || 0) - ((s.started_ts || t[0]) || 0);
      insights = {
        duration: dur > 0 ? dur : null, points: t.length,
        pitAvg: _avg(pit), pitMin: pit.length ? Math.min(...pit) : null, pitMax: pit.length ? Math.max(...pit) : null,
        food1Max: f1.length ? Math.max(...f1) : null, food2Max: f2.length ? Math.max(...f2) : null,
        fanAvg: _avg(fan),
      };
    } catch (_) { insights = { error: true }; }
  }

  // -- cook insights + repeat ---------------------------------------------------
  let cookStats = $state(null);
  async function loadInsights() { try { cookStats = await getJSON('insights'); } catch (_) {} }
  async function repeatCook(s) {
    if (!confirm(`Repeat "${s.name || 'Cook #' + s.id}"? This applies that cook's setpoint and food targets to the board now.`)) return;
    try {
      const r = await postJSON('sessions/' + s.id + '/repeat', {});
      if (r.ok === false) { alert(r.error || 'Could not repeat'); return; }
      const tg = Object.entries(r.targets || {}).map(([k, v]) => `${k} ${v}°`).join(', ');
      alert(`Applied: pit ${r.setpoint}°${tg ? ' · targets ' + tg : ''}`);
    } catch (e) { alert('Could not repeat the cook'); }
  }
  function fmtHrs(s) { return s ? (s / 3600).toFixed(1) + 'h' : '--'; }

  // -- guided cooks -----------------------------------------------------------
  let guidedCat = $state([]);
  let guidedActive = $state(null);
  let guidedSel = $state('');
  let guidedCh = $state('food1');
  let guidedKeepWarm = $state(false);
  let guidedPrompt = $state('');
  const guidedSelCook = $derived(guidedCat.find((c) => c.key === guidedSel) || null);
  async function loadGuided() {
    try {
      const g = await getJSON('guided');
      guidedCat = g.catalog || [];
      guidedActive = g.active;
      if (!guidedSel && guidedCat.length) guidedSel = guidedCat[0].key;
    } catch (_) {}
  }
  async function startGuided() {
    try {
      const r = await postJSON('guided/start',
        { key: guidedSel, channel: guidedCh, auto_keep_warm: guidedKeepWarm });
      if (r.ok === false) { alert(r.error || 'Could not start'); return; }
      guidedActive = r.guided; guidedPrompt = '';
    } catch (e) { alert('Could not start the guided cook'); }
  }
  async function stopGuided() {
    if (!confirm('Stop the guided cook? The pit keeps its current setpoint.')) return;
    try { await postJSON('guided/stop', {}); guidedActive = null; guidedPrompt = ''; } catch (_) {}
  }
  async function confirmWrap() {
    try {
      const r = await postJSON('guided/wrapped', {});
      if (r.ok !== false) guidedActive = r.guided;
    } catch (_) {}
  }
  function onGuidedEvent(ev) {
    if (ev.event === 'prompt') { guidedPrompt = ev.prompt; guidedActive = ev.guided; }
    else if (ev.event === 'started' || ev.event === 'wrapped') guidedActive = ev.guided;
    else if (ev.event === 'stopped') { guidedActive = null; guidedPrompt = ''; }
  }

  onMount(async () => {
    try { apply({ state: await getJSON('status') }); } catch (_) {}
    try { const pr = await getJSON('presets'); meat = pr.meat || []; programPresets = pr.program || []; } catch (_) {}
    loadProgram(); loadSaved(); loadSessions(); loadTimers(); loadGuided(); loadInsights();
    stages = [newStage()];
    stop = connectWs((m) => {
      if (m.state) apply(m);
      if (m.event?.type === 'guided') onGuidedEvent(m.event);
    });
    progTimer = setInterval(loadProgram, 5000);
    timerTick = setInterval(tickTimers, 1000);
  });
  onDestroy(() => { stop && stop(); clearInterval(progTimer); clearInterval(timerTick); });

  // -- kitchen timers (reminders) --------------------------------------------
  const TIMER_KEY = 'hm.timers';
  let timers = $state([]);
  let now = $state(Date.now());
  let timerName = $state('');
  let timerMin = $state(15);
  let timerTick;
  let timerSeq = 0;

  function loadTimers() {
    try { timers = JSON.parse(localStorage.getItem(TIMER_KEY) || '[]'); } catch (_) { timers = []; }
    for (const t of timers) if (now >= t.endTs) t.done = true;
    timerSeq = timers.reduce((m, t) => Math.max(m, t.id || 0), 0) + 1;
  }
  function saveTimers() { try { localStorage.setItem(TIMER_KEY, JSON.stringify(timers)); } catch (_) {} }
  function addTimer() {
    const mins = Number(timerMin) || 0;
    if (mins <= 0) return;
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      try { Notification.requestPermission(); } catch (_) {}
    }
    timers = [...timers, { id: timerSeq++, name: timerName.trim() || 'Timer', endTs: Date.now() + mins * 60000, done: false }];
    timerName = ''; saveTimers();
  }
  function removeTimer(id) { timers = timers.filter((t) => t.id !== id); saveTimers(); }
  function ringTimer(t) {
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const o = ac.createOscillator(), g = ac.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ac.destination);
      g.gain.setValueAtTime(0.15, ac.currentTime);
      o.start(); o.stop(ac.currentTime + 0.4);
    } catch (_) {}
    try {
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted')
        new Notification('HeaterMeter timer', { body: (t.name || 'Timer') + ' is up' });
    } catch (_) {}
  }
  function tickTimers() {
    now = Date.now();
    let changed = false;
    for (const t of timers) {
      if (!t.done && now >= t.endTs) { t.done = true; changed = true; ringTimer(t); }
    }
    if (changed) { timers = [...timers]; saveTimers(); }
  }
  function fmtRemain(endTs) {
    let s = Math.max(0, Math.round((endTs - now) / 1000));
    const m = Math.floor(s / 60); s = s % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
</script>

<div class="px-4 pt-4 pb-28 lg:pb-10 max-w-xl lg:max-w-3xl mx-auto space-y-5">

  <!-- Cook control: the one place to start or stop a cook -->
  <div class="hm-card rounded-2xl p-4">
    {#if pitOn}
      <div class="text-xs uppercase tracking-wider opacity-50">Cooking</div>
      <div class="font-display text-2xl font-bold leading-tight">
        {fmt(status.pit)}°<span class="text-base font-normal opacity-50"> / {fmt(status.set_point)}°{unit}</span>
      </div>
      <div class="text-xs opacity-50">{status.pid_mode_label || ''}{liveSession ? ' · ' + fmtElapsed(liveSession.started_ts) : ''}</div>
      <div class="flex gap-2 mt-3">
        <button class="flex-1 py-2.5 rounded-xl bg-green-600 text-white font-semibold" onclick={finishCook}>Finish cook</button>
        <button class="px-4 py-2.5 rounded-xl bg-neutral-200 dark:bg-neutral-800 font-semibold" onclick={turnOffPit}>Turn off pit</button>
      </div>
    {:else}
      <h3 class="font-bold mb-1">Start a cook</h3>
      <p class="text-xs opacity-60 mb-3">Set your pit temperature and the cook starts logging automatically. Or pick a guided cook or program below.</p>
      <div class="flex items-center gap-2">
        <div class="flex items-center gap-1 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 shrink-0">
          <input class="w-14 text-center bg-transparent py-2 tabular-nums" type="number" bind:value={startTemp} aria-label="Pit temperature" />
          <span class="opacity-50 text-sm">°{unit}</span>
        </div>
        <button class="flex-1 py-2.5 rounded-xl bg-orange-600 text-white font-semibold" onclick={startCook}>Start cook</button>
      </div>
      <div class="grid grid-cols-4 gap-2 mt-2">
        {#each quickTemps as q}
          <button class="py-1.5 rounded-lg bg-black/5 dark:bg-white/10 text-sm tabular-nums" onclick={() => (startTemp = q)}>{q}°</button>
        {/each}
      </div>
      {#if liveSession}
        <div class="flex items-center justify-between mt-3 text-sm">
          <span class="opacity-60">A cook is still open.</span>
          <button class="px-3 py-1.5 rounded-lg bg-neutral-200 dark:bg-neutral-800 font-medium" onclick={finishCook}>Finish it</button>
        </div>
      {/if}
    {/if}
  </div>

  <!-- Guided Cook -->
  <div class="hm-card rounded-2xl p-4">
    <h3 class="font-bold mb-1">Guided Cook</h3>
    {#if guidedActive}
      <div class="text-sm font-semibold">{guidedActive.label}
        <span class="opacity-50 font-normal">· pit {guidedActive.pit_setpoint}° · pull at {guidedActive.food_target}°</span>
      </div>
      {#if guidedPrompt}
        <div class="mt-2 text-sm bg-orange-500/10 border border-orange-500/30 rounded-lg px-3 py-2">{guidedPrompt}</div>
      {/if}
      <ol class="mt-3 space-y-1">
        {#each guidedActive.milestones as m}
          <li class="text-sm flex items-start gap-2 {m.fired ? 'opacity-50' : ''}">
            <span class="mt-0.5 inline-block w-4 shrink-0 text-center">{m.fired ? '✓' : '·'}</span>
            <span>{m.prompt}</span>
          </li>
        {/each}
      </ol>
      <div class="mt-3 flex gap-2">
        {#if guidedActive.wrap_pending}
          <button class="flex-1 px-3 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={confirmWrap}>I wrapped it</button>
        {/if}
        {#if guidedActive.done}
          <span class="flex-1 text-sm text-green-600 dark:text-green-400 font-semibold self-center">Target reached. Rest and enjoy.</span>
        {/if}
        <button class="px-3 py-2 rounded-lg bg-neutral-700 text-white text-sm" onclick={stopGuided}>{guidedActive.done ? 'Clear' : 'Stop'}</button>
      </div>
    {:else}
      <p class="text-xs opacity-60 mb-2">Pick what you're cooking. The pit is set, the probe is named and targeted, and you get a heads-up at every milestone: the stall, the wrap, the pull, the rest.</p>
      <div class="grid grid-cols-[2fr_1fr] gap-2">
        <select class="w-full min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 h-10" bind:value={guidedSel} aria-label="What you're cooking">
          {#each [...new Set(guidedCat.map((c) => c.category))] as cat}
            <optgroup label={cat}>
              {#each guidedCat.filter((c) => c.category === cat) as c}
                <option value={c.key}>{c.label}</option>
              {/each}
            </optgroup>
          {/each}
        </select>
        <select class="w-full min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 h-10" bind:value={guidedCh} aria-label="On which probe">
          <option value="food1">{names[1] || 'Food 1'}</option>
          <option value="food2">{names[2] || 'Food 2'}</option>
          {#if ambFood}<option value="ambient">{names[3] || 'Ambient'}</option>{/if}
        </select>
      </div>
      {#if guidedSelCook}
        <p class="text-xs opacity-60 mt-2">{guidedSelCook.description} Pit {guidedSelCook.pit_setpoint}°, pull at {guidedSelCook.food_target}°, rest {Math.round((guidedSelCook.rest_secs || 0) / 60)} min.</p>
      {/if}
      <label class="flex items-center gap-2 text-sm mt-2"><input type="checkbox" bind:checked={guidedKeepWarm} /> Drop pit to keep-warm when the food hits its target</label>
      <button class="mt-2 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full disabled:opacity-40" disabled={!guidedSel} onclick={startGuided}>Start Guided Cook</button>
    {/if}
  </div>

  <!-- Targets -->
  <div class="hm-card rounded-2xl p-4">
    <h3 class="font-bold mb-3">Food targets</h3>
    {#each probeRows as r}
      <div class="flex items-center gap-2 mb-2">
        <span class="w-24 text-sm opacity-70 truncate">{r.label}</span>
        <span class="w-12 text-sm tabular-nums opacity-50">{fmt(r.temp)}°</span>
        <select class="hm-card rounded-lg px-2 py-1.5 text-sm flex-1 min-w-0"
                onchange={(e) => { if (e.target.value) r.set(e.target.value); }}>
          <option value="">Preset…</option>
          {#each meat as m}<option value={m.temp_f}>{m.label} ({m.temp_f}°)</option>{/each}
        </select>
        <input class="w-16 text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg py-1.5 tabular-nums"
               type="number" placeholder="°" value={r.get()} oninput={(e) => r.set(e.target.value)} />
      </div>
    {/each}
    <label class="flex items-center gap-2 text-sm mt-3">
      <input type="checkbox" checked={ambFood} onchange={toggleAmb} />
      Use Ambient probe as a food probe
    </label>
    <button class="mt-3 w-full py-2.5 rounded-xl bg-orange-600 text-white font-semibold" onclick={saveTargets}>Save targets</button>
  </div>

  <!-- Cook program -->
  <div class="hm-card rounded-2xl p-4">
    <h3 class="font-bold mb-3">Cook program</h3>

    <!-- Preset picker -->
    {#if programPresets.length}
      <div class="rounded-xl bg-black/5 dark:bg-white/5 p-2.5 mb-3">
        <div class="flex items-center gap-2">
          <span class="text-sm opacity-70 shrink-0">Preset</span>
          <select class="flex-1 min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-1.5 text-sm" bind:value={presetSel} onchange={loadPreset}>
            <option value="">Start from a preset…</option>
            {#each presetCats as cat}
              <optgroup label={cat}>
                {#each programPresets.filter((p) => p.category === cat) as p}
                  <option value={p.key}>{p.label}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
        </div>
        {#if presetNote}<p class="text-xs opacity-60 mt-2">{presetNote}</p>{/if}
      </div>
    {/if}

    {#if running}
      <div class="rounded-xl bg-orange-600/15 p-3 mb-3">
        <div class="font-semibold">{prog.name || 'Running'}</div>
        <div class="text-sm opacity-70">Stage {(prog.stage_index ?? 0) + 1} of {prog.stage_count}: {prog.stage_name || '—'}</div>
        <div class="flex gap-2 mt-2">
          <button class="px-3 py-1.5 rounded-lg bg-neutral-200 dark:bg-neutral-800 text-sm" onclick={advance}>Next stage</button>
          <button class="px-3 py-1.5 rounded-lg bg-red-600 text-white text-sm" onclick={stopProgram}>Stop</button>
        </div>
      </div>
    {/if}

    <div class="space-y-2">
      {#each stages as s, i}
        <div class="rounded-xl bg-black/5 dark:bg-white/5 p-2.5 space-y-2">
          <div class="flex items-center gap-2">
            <input class="flex-1 min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-1.5 text-sm" placeholder={'Stage ' + (i + 1)} bind:value={s.name} />
            <button class="text-red-500 text-sm px-1" onclick={() => removeStage(i)} aria-label="Remove">✕</button>
          </div>
          <div class="flex items-center gap-2 text-sm">
            <span class="opacity-60">Pit</span>
            <input class="w-16 text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg py-1.5 tabular-nums" type="number" placeholder="off" bind:value={s.setpoint} />
            <span class="opacity-60">until</span>
            <select class="bg-neutral-200 dark:bg-neutral-800 rounded-lg px-1.5 py-1.5 flex-1 min-w-0" bind:value={s.advanceType}>
              <option value="probe">probe reaches</option>
              <option value="time">time elapsed</option>
              <option value="manual">I advance</option>
            </select>
          </div>
          {#if s.advanceType === 'probe'}
            <div class="flex items-center gap-2 text-sm">
              <select class="bg-neutral-200 dark:bg-neutral-800 rounded-lg px-1.5 py-1.5 flex-1 min-w-0" bind:value={s.channel}>
                <option value="pit">Pit</option><option value="food1">Food 1</option>
                <option value="food2">Food 2</option><option value="ambient">Ambient</option>
              </select>
              <span class="opacity-60">hits</span>
              <input class="w-16 text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg py-1.5 tabular-nums" type="number" bind:value={s.temp} /><span>°</span>
            </div>
          {:else if s.advanceType === 'time'}
            <div class="flex items-center gap-2 text-sm">
              <input class="w-20 text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg py-1.5 tabular-nums" type="number" bind:value={s.minutes} /><span class="opacity-60">minutes</span>
            </div>
          {/if}
        </div>
      {/each}
    </div>
    <button class="mt-2 w-full py-2 rounded-xl bg-black/5 dark:bg-white/5 text-sm" onclick={addStage}>+ Add stage</button>

    <div class="flex gap-2 mt-3">
      <input class="flex-1 min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" placeholder="Program name (to save)" bind:value={progName} />
      <button class="px-4 rounded-lg bg-neutral-200 dark:bg-neutral-800 text-sm" onclick={saveProgram}>Save</button>
      <button class="px-4 rounded-lg bg-orange-600 text-white font-semibold text-sm" onclick={() => startProgram(stages, progName)}>Start</button>
    </div>

    {#if saved.length}
      <div class="mt-4">
        <div class="text-xs uppercase tracking-wider opacity-50 mb-1">Saved</div>
        {#each saved as p}
          <div class="flex items-center gap-2 py-1.5 text-sm">
            <span class="flex-1 truncate">{p.name}</span>
            <button class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" onclick={() => loadStages(p)}>Edit</button>
            <button class="text-xs px-2 py-1 rounded bg-orange-600 text-white" onclick={() => startProgram(p.stages, p.name)}>Run</button>
            <button class="text-xs px-2 py-1 rounded text-red-500" onclick={() => deleteProgram(p.id)}>✕</button>
          </div>
        {/each}
      </div>
    {/if}
  </div>

  <!-- Sessions -->
  <div class="hm-card rounded-2xl p-4">
    <div class="flex items-center justify-between mb-3">
      <h3 class="font-bold">Past cooks</h3>
      {#if sessions.length}
        <a class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" href="/api/export.csv" download>Export all</a>
      {/if}
    </div>
    {#if cookStats && cookStats.cooks > 1}
      <p class="text-xs opacity-50 mb-2">{cookStats.cooks} cooks logged · average {fmtHrs(cookStats.avg_duration_secs)}{cookStats.stalls_seen ? ` · your stalls average ${fmtHrs(cookStats.avg_stall_secs)}` : ''}{cookStats.longest_secs ? ` · longest ${fmtHrs(cookStats.longest_secs)}` : ''}</p>
    {/if}
    {#if !sessions.length}
      <div class="text-sm opacity-50">No saved cooks yet. Your first cook starts logging automatically when the pit comes up to temp.</div>
    {/if}
    {#each sessions as s}
      <div class="py-2 border-b border-black/5 dark:border-white/5 last:border-0">
        <div class="flex items-center gap-2">
          <button class="flex-1 min-w-0 text-left" onclick={() => toggleInsights(s)}>
            <div class="font-medium truncate flex items-center gap-2">
              <span class="truncate">{s.name || ('Cook #' + s.id)}</span>
              {#if s.completed_ts}<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-green-600/15 text-green-700 dark:text-green-400">Done</span>{/if}
            </div>
            <div class="text-xs opacity-50">{fmtClock(s.started_ts)}{s.ended_ts ? ' – ' + fmtClock(s.ended_ts) : (s.completed_ts ? ' · done ' + fmtClock(s.completed_ts) : ' · live')} · tap for stats</div>
          </button>
          <button class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" onclick={() => repeatCook(s)}>Repeat</button>
          <a class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" href={reportHref(s.id)} target="_blank" rel="noopener">Report</a>
          <a class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" href={'/api/export.csv?session_id=' + s.id} download>CSV</a>
          <button class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" onclick={() => renameSession(s)}>Rename</button>
          <button class="text-xs px-2 py-1 rounded bg-black/5 dark:bg-white/10" onclick={() => shareSession(s)}>Share</button>
          <button class="text-xs px-2 py-1 rounded text-red-500" onclick={() => deleteSession(s)}>✕</button>
        </div>
        {#if insightsFor === s.id}
          <div class="mt-2 rounded-lg bg-black/5 dark:bg-white/5 p-3 text-xs">
            {#if !insights}
              <span class="opacity-50">Loading…</span>
            {:else if insights.error}
              <span class="text-red-500">Couldn't load stats.</span>
            {:else}
              <div class="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 tabular-nums">
                <div><span class="opacity-50">Duration</span> <b>{insights.duration ? fmtDuration(insights.duration) : '—'}</b></div>
                <div><span class="opacity-50">Pit avg</span> <b>{fmt(insights.pitAvg)}°</b></div>
                <div><span class="opacity-50">Pit range</span> <b>{fmt(insights.pitMin)}–{fmt(insights.pitMax)}°</b></div>
                <div><span class="opacity-50">Food 1 max</span> <b>{insights.food1Max == null ? '—' : fmt(insights.food1Max) + '°'}</b></div>
                <div><span class="opacity-50">Food 2 max</span> <b>{insights.food2Max == null ? '—' : fmt(insights.food2Max) + '°'}</b></div>
                <div><span class="opacity-50">Avg fan</span> <b>{insights.fanAvg == null ? '—' : fmt(insights.fanAvg) + '%'}</b></div>
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
  <!-- Timers -->
  <div class="hm-card rounded-2xl p-4">
    <h3 class="font-bold mb-3">Timers</h3>
    <div class="flex gap-2">
      <input class="flex-1 min-w-0 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" placeholder="Label (e.g. spritz)" bind:value={timerName} />
      <input class="w-20 text-center bg-neutral-200 dark:bg-neutral-800 rounded-lg py-2 tabular-nums" type="number" min="1" bind:value={timerMin} />
      <span class="self-center text-sm opacity-50">min</span>
      <button class="px-4 rounded-lg bg-orange-600 text-white font-semibold text-sm" onclick={addTimer}>Start</button>
    </div>
    {#if timers.length}
      <div class="mt-3 space-y-1">
        {#each timers as t (t.id)}
          <div class="flex items-center gap-2 py-1.5 text-sm {t.done ? 'text-red-500 font-semibold' : ''}">
            <span class="flex-1 truncate">{t.name}</span>
            <span class="tabular-nums">{t.done ? 'DONE' : fmtRemain(t.endTs)}</span>
            <button class="text-xs px-2 py-1 rounded {t.done ? 'bg-red-500/15' : 'text-red-500'}" onclick={() => removeTimer(t.id)}>{t.done ? 'Dismiss' : '✕'}</button>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  /* Keep selects and the (compact) text/number inputs the same height (40px)
     so every form control on the Cook screen lines up. The small ✕ / Run /
     Edit list buttons are unaffected. */
  select,
  input[type="number"] { height: 2.5rem; }
  /* Let selects shrink + truncate inside flex/grid rows instead of overflowing
     the card when an option (e.g. a long probe name) is wide. */
  select { min-width: 0; }
</style>
