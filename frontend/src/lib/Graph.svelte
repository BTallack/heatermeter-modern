<script>
  import { onMount, onDestroy } from 'svelte';
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { getJSON, postJSON, connectWs } from './api.js';
  import { fmtClock, fileToDataURL } from './util.js';

  // Embedded = rendered inside the desktop dashboard grid; parent owns padding.
  let { embedded = false } = $props();

  const MAX_POINTS = 20000;
  // Series order lines up with the `data` columns below.
  const SERIES = [
    { key: 'set_point', label: 'Set', role: 'set', dash: [6, 4], width: 1 },
    { key: 'pit', label: 'Pit', role: 'ember', width: 2 },
    { key: 'food1', label: 'Food 1', role: 'food1', width: 2 },
    { key: 'food2', label: 'Food 2', role: 'food2', width: 2 },
    { key: 'compare', label: 'vs', role: 'food3', width: 1, dash: [2, 3] },
    { key: 'fan_pct', label: 'Fan %', role: 'fan', width: 1, scale: '%' },
    { key: 'servo_pct', label: 'Servo %', role: 'servo', width: 1, dash: [4, 3], scale: '%' },
  ];

  // Remember which series are toggled on/off across page loads + navigation.
  // Fan/Servo default OFF (they crowd the temperature detail); toggling any
  // series persists the choice.
  const VIS_KEY = 'hm.graphVis';
  function loadVis() { try { return JSON.parse(localStorage.getItem(VIS_KEY)) || {}; } catch (_) { return {}; } }
  function initialShow(s) {
    const v = loadVis();
    if (v[s.label] !== undefined) return v[s.label];
    return s.key !== 'fan_pct' && s.key !== 'servo_pct';
  }
  function saveVis(u) {
    const v = loadVis();
    u.series.forEach((s, i) => { if (i > 0 && s.label && s.label !== 'vs') v[s.label] = s.show !== false; });
    try { localStorage.setItem(VIS_KEY, JSON.stringify(v)); } catch (_) {}
  }

  // Colours come from the theme's colour roles (app.css), so the graph matches
  // the Dashboard and stays legible in daylight; read again on a theme change.
  const ROLES = ['set', 'ember', 'food1', 'food2', 'food3', 'fan', 'servo', 'axis', 'warn', 'bad', 'lid', 'resume'];
  let pal = {};
  function readPalette() {
    const cs = getComputedStyle(document.documentElement);
    pal = Object.fromEntries(ROLES.map((r) => [r, cs.getPropertyValue('--hm-' + r).trim() || '#9aa0a6']));
  }
  let themeObs = null;

  let chartEl;
  let chart = null;
  let data = [[], [], [], [], [], [], [], []]; // t,set,pit,food1,food2,compare,fan,servo
  let compareCurve = null; // {start, ts:[], pit:[]}
  let targets = { food1: null, food2: null }; // food target temps for graph lines
  let ro = null;
  let stop;
  // Liveness: the chart advances via the WebSocket; if that stalls (Wi-Fi drop,
  // tab throttling) we resync from the API so it never appears frozen.
  let lastLiveTs = 0;
  let staleTimer, onVis;

  const parseAlarm = (v) => {
    const n = parseFloat(String(v ?? '').replace(/[LH]$/, ''));
    return Number.isNaN(n) || n < 0 ? null : n;
  };
  // Food-probe targets live at alarms[3] (food1 high) and alarms[5] (food2 high).
  function updateTargets(alarms) {
    targets = { food1: parseAlarm((alarms || [])[3]), food2: parseAlarm((alarms || [])[5]) };
    if (chart) chart.redraw();
  }

  let notes = $state([]);
  let events = $state([]);   // auto timeline events (lid/stall/target/setpoint/...)
  let sessions = $state([]);
  // Range can be the current cook ('cook'), a trailing window in minutes, or
  // all history (0). Defaults to the live cook so readings from before the
  // unit was moved/restarted don't muddy what you're looking at right now.
  let rangeMin = $state('cook');
  let sessionId = $state(null);   // current session id (from status / WS)
  let compareSel = $state('');

  // note dialog
  let noteOpen = $state(false);
  let noteText = $state('');
  let noteFile = null;

  function axis(extra = {}) {
    return Object.assign({
      stroke: pal.axis,
      font: '12px "Work Sans Variable", system-ui, sans-serif',
      grid: { stroke: 'rgba(128,128,128,0.18)', width: 1 },
      ticks: { stroke: 'rgba(128,128,128,0.18)', width: 1 },
    }, extra);
  }

  // Dashed horizontal lines at the food-probe targets (on the temp 'y' scale).
  function targetLinesPlugin() {
    return { hooks: { draw: (u) => {
      const lines = [
        { v: targets.food1, color: pal.food1 },
        { v: targets.food2, color: pal.food2 },
      ];
      const ctx = u.ctx, { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.setLineDash([2, 4]); ctx.lineWidth = 1; ctx.globalAlpha = 0.55;
      for (const ln of lines) {
        if (ln.v == null) continue;
        const y = u.valToPos(ln.v, 'y', true);
        if (y < top || y > top + height) continue;
        ctx.strokeStyle = ln.color;
        ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke();
      }
      ctx.restore();
    } } };
  }

  // Auto timeline events as subtle markers along the bottom of the plot: a
  // faint vertical line + a colored dot, with a short caption for the major
  // kinds. (User notes draw their labels at the TOP - see notesPlugin.)
  // Event markers coloured by meaning (DESIGN.md's Colour Is Data rule).
  const EVENT_ROLES = {
    lid_open: 'lid', lid_closed: 'lid',
    stall_start: 'food3', stall_end: 'food3',
    target: 'food1', probe_done: 'food1', cook_complete: 'food1', food_target: 'food1',
    setpoint: 'set', stage: 'set', program_done: 'set', serve_status: 'set', powerup_idle: 'set',
    disconnect: 'bad', fault: 'bad', overtemp: 'bad', fire_dying: 'bad',
    alarm_low: 'warn', pit_low: 'warn',
    reconnect: 'resume', powerup_resume: 'resume',
  };
  const EVENT_CAPTIONS = {
    lid_open: 'Lid', stall_start: 'Stall', stall_end: 'Stall over',
    target: '✓', cook_complete: 'Done', disconnect: '✕', overtemp: 'Hot',
    pit_low: 'Low', fire_dying: 'Fire!', serve_status: 'Serve',
    powerup_idle: 'Power', powerup_resume: 'Resumed',
  };
  function eventsPlugin() {
    return { hooks: { draw: (u) => {
      if (!events.length) return;
      const ctx = u.ctx, { left, top, width, height } = u.bbox;
      const pr = uPlot.pxRatio || 1;   // the plot is drawn in device pixels
      ctx.save();
      ctx.font = `${Math.round(11 * pr)}px "Work Sans Variable", system-ui, sans-serif`;
      ctx.textAlign = 'center';
      // Captions never overlap or run off the plot: a caption that would land on
      // the previous one is skipped (its dot and line still show).
      let lastRight = -Infinity;
      for (const ev of [...events].sort((a, b) => a.ts - b.ts)) {
        if (ev.kind === 'prediction') continue;   // forecast data, not a marker
        const x = u.valToPos(ev.ts, 'x', true);
        if (x < left || x > left + width) continue;
        const color = pal[EVENT_ROLES[ev.kind]] || pal.set;
        ctx.strokeStyle = color; ctx.fillStyle = color;
        ctx.globalAlpha = 0.25; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 0.9;
        ctx.beginPath(); ctx.arc(x, top + height - 5 * pr, 3 * pr, 0, Math.PI * 2); ctx.fill();
        const cap = ev.kind === 'setpoint' ? (ev.label || '').replace('Set ', '')
                  : ev.kind === 'stage' ? (ev.label || '').replace('Stage: ', '')
                  : ev.kind === 'food_target' ? (ev.value != null ? Math.round(ev.value) + '°' : 'Target')
                  : EVENT_CAPTIONS[ev.kind];
        if (cap) {
          const text = String(cap).slice(0, 12);
          const w = ctx.measureText(text).width;
          const cx = Math.min(Math.max(x, left + w / 2 + 2 * pr), left + width - w / 2 - 2 * pr);
          if (cx - w / 2 > lastRight + 4 * pr) {
            ctx.fillText(text, cx, top + height - 12 * pr);
            lastRight = cx + w / 2;
          }
        }
      }
      ctx.restore();
    } } };
  }

  function notesPlugin() {
    return { hooks: { draw: (u) => {
      if (!notes.length) return;
      const ctx = u.ctx, { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.strokeStyle = pal.lid; ctx.fillStyle = pal.lid;
      ctx.lineWidth = 1; ctx.font = `${Math.round(11 * (uPlot.pxRatio || 1))}px "Work Sans Variable", system-ui, sans-serif`;
      for (const n of notes) {
        const x = u.valToPos(n.ts, 'x', true);
        if (x < left || x > left + width) continue;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        ctx.setLineDash([]);
        ctx.save(); ctx.translate(x + 3, top + 4);
        ctx.fillText(String(n.text || '').slice(0, 22), 0, 8); ctx.restore();
      }
      ctx.restore();
    } } };
  }

  // When the cursor isn't over the chart, pin the live legend to the most
  // recent sample so it shows the current time + temps instead of "--".
  function latestLegendPlugin() {
    const toLast = (u) => {
      const n = u.data[0] ? u.data[0].length : 0;
      if (n && u.cursor.idx == null) u.setLegend({ idx: n - 1 }, false);
    };
    return { hooks: {
      ready: [toLast],
      setData: [toLast],
      setCursor: [(u) => { if (u.cursor.idx == null) toLast(u); }],
    } };
  }

  // One time format on the x axis ("3:15 PM"), or dates for multi-day ranges.
  function timeTicks(u, splits) {
    const span = (u.scales.x.max ?? 0) - (u.scales.x.min ?? 0);
    if (span > 36 * 3600) return splits.map((ts) => new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }));
    return splits.map((ts) => fmtClock(ts));
  }
  // The left % axis only earns its space while Fan or Servo is shown.
  const pctShown = (u) => u.series.some((s) => s.scale === '%' && s.show);

  function makeChart() {
    readPalette();
    chart = new uPlot({
      width: chartEl.clientWidth || 600, height: 330,
      // Temperature scale 'y' is intentionally left unconfigured so uPlot
      // applies its native auto-ranging: it re-fits min/max to the visible
      // data (with default 10% padding) on every setData. Fan/Servo '%' stays
      // pinned to 0-100. A custom range function was tried and reverted: uPlot
      // ran it once at construction (empty data) and did not re-invoke it after
      // history loaded, so the axis stuck at the fallback range.
      scales: { x: { time: true }, '%': { range: [0, 100] } },
      legend: { live: true },
      cursor: { drag: { x: true, y: false } },
      plugins: [targetLinesPlugin(), eventsPlugin(), notesPlugin(), latestLegendPlugin(),
                // Re-measure the axes so the % axis appears/disappears with Fan/Servo.
                { hooks: { setSeries: [(u) => { saveVis(u); u.redraw(false, true); }] } }],
      // x series: show time-only in the legend (no date) so the legend row
      // doesn't wrap to a second line at normal width.
      series: [{ value: (u, ts) => (ts == null ? '--' : fmtClock(ts)) }, ...SERIES.map((s) => ({
        // Temperature series have no explicit scale key; bind them to 'y' so the
        // data lands on the same scale the temperature axis renders. Without the
        // `|| 'y'`, uPlot creates a stray scale keyed "undefined" for the lines
        // while the 'y' axis stays empty (axis shows 0-250, lines don't match).
        label: s.label, stroke: pal[s.role], width: s.width, dash: s.dash, scale: s.scale || 'y',
        show: initialShow(s),
        spanGaps: false, points: { show: false },
        value: (u, v) => (v == null ? '--' : v.toFixed(1) + (s.scale === '%' ? '%' : '°')),
      }))],
      axes: [
        axis({ values: timeTicks, space: 80 }),           // x (time, bottom)
        axis({ size: 48, side: 1 }),                     // temperature -> right
        // Percent -> left. Its grid would double up with the temp grid, so
        // drop it and let the temperature scale own the gridlines.
        axis({ scale: '%', side: 3, grid: { show: false }, ticks: { show: false },
               size: (u) => (pctShown(u) ? 42 : 0),
               values: (u, vs) => vs.map((v) => (pctShown(u) ? v + '%' : '')) }),
      ],
    }, data, chartEl);
    updateVsLegend();
  }

  function compareAt(t) {
    if (!compareCurve || !data[0].length) return null;
    const target = compareCurve.start + (t - data[0][0]);
    const ts = compareCurve.ts;
    if (target < ts[0] || target > ts[ts.length - 1]) return null;
    let lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ts[m] <= target) lo = m; else hi = m; }
    const a = compareCurve.pit[lo], b = compareCurve.pit[hi];
    if (a == null || b == null) return a ?? b ?? null;
    return a + (b - a) * ((target - ts[lo]) / (ts[hi] - ts[lo] || 1));
  }
  function rebuildCompare() { data[5] = data[0].map(compareAt); }

  // The "vs" (compare) series only makes sense when a comparison cook is
  // selected. When it isn't, the line is already absent (null data) - this also
  // hides its row in the legend so there's no dangling "vs: --". The legend is
  // a uPlot <table>; the rows are Time,Set,Pit,Food1,Food2,[vs],Fan%,Servo% so
  // the compare row is index 5.
  function updateVsLegend() {
    if (!chart || !chart.root) return;
    const rows = chart.root.querySelectorAll('.u-legend .u-series');
    if (rows[5]) rows[5].style.display = compareCurve ? '' : 'none';
  }

  function setData(cols) {
    data = [cols.t || [], cols.set_point || [], cols.pit || [], cols.food1 || [],
            cols.food2 || [], [], cols.fan_pct || [], cols.servo_pct || []];
    rebuildCompare();
    if (chart) chart.setData(data);
    updateVsLegend();
  }
  function pushPoint(ts, st) {
    data[0].push(ts); data[1].push(st.set_point); data[2].push(st.pit);
    data[3].push(st.food1); data[4].push(st.food2); data[5].push(compareAt(ts));
    data[6].push(st.fan_pct); data[7].push(st.servo_pct);
    if (data[0].length > MAX_POINTS) for (const a of data) a.shift();
    if (chart) chart.setData(data);
    lastLiveTs = Date.now() / 1000;
  }

  function rangeQuery() {
    // "This cook" scopes to the live session; with nothing cooking, fall back
    // to a recent window rather than dumping all of history.
    if (rangeMin === 'cook') return sessionId ? `?session_id=${sessionId}` : '?minutes=120';
    return Number(rangeMin) > 0 ? `?minutes=${rangeMin}` : '';
  }
  async function loadHistory() {
    const q = rangeQuery();
    try { setData(await getJSON(`history${q}`)); lastLiveTs = Date.now() / 1000; } catch (_) {}
    // Notes + events share the graph's scope so old cooks' markers don't bleed in.
    try { notes = await getJSON(`notes${q}`); if (chart) chart.redraw(); } catch (_) {}
    try { events = await getJSON(`events${q}`); if (chart) chart.redraw(); } catch (_) {}
  }
  async function loadSessions() {
    try { sessions = (await getJSON('sessions')).filter((s) => s.ended_ts); } catch (_) {}
  }
  async function setCompare() {
    if (!compareSel) { compareCurve = null; rebuildCompare(); if (chart) chart.setData(data); updateVsLegend(); return; }
    try {
      const c = await getJSON(`history?session_id=${compareSel}&limit=10000`);
      compareCurve = { start: (c.t || [])[0], ts: c.t || [], pit: c.pit || [] };
      rebuildCompare(); if (chart) chart.setData(data); updateVsLegend();
    } catch (_) {}
  }

  async function saveNote() {
    if (!noteText.trim() && !noteFile) { noteOpen = false; return; }
    let photo_b64 = null;
    if (noteFile) { try { photo_b64 = await fileToDataURL(noteFile); } catch (_) {} }
    try {
      await postJSON('notes', { text: noteText.trim() || '(photo)', photo_b64 });
      notes = await getJSON(`notes${rangeQuery()}`); if (chart) chart.redraw();
    } catch (_) {}
    noteOpen = false; noteText = ''; noteFile = null;
  }

  onMount(() => {
    makeChart();
    getJSON('status').then((s) => {
      sessionId = s.session_id ?? null;
      updateTargets(s.alarms || []);
      loadHistory();
    }).catch(() => loadHistory());
    loadSessions();
    ro = new ResizeObserver(() => chart && chart.setSize({ width: chartEl.clientWidth || 600, height: 330 }));
    ro.observe(chartEl);
    // Light/dark switch: rebuild with that theme's colours (uPlot bakes them in).
    let wasDark = document.documentElement.classList.contains('dark');
    themeObs = new MutationObserver(() => {
      const dark = document.documentElement.classList.contains('dark');
      if (dark === wasDark || !chart) return;
      wasDark = dark;
      chart.destroy();
      makeChart();
    });
    themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    stop = connectWs((m) => {
      if (m.event?.type === 'timeline') {
        events = [...events, m.event];
        if (chart) chart.redraw();
        return;
      }
      if (!m.state) return;
      // A new cook started (e.g. auto-new-session after a restart): when we're
      // scoped to the current cook, reload so stale data drops away.
      if (m.session_id != null && m.session_id !== sessionId) {
        sessionId = m.session_id;
        if (rangeMin === 'cook') { loadHistory(); return; }
      }
      if (m.state.status) pushPoint(m.ts || (Date.now() / 1000), m.state.status);
      if (m.state.alarms) updateTargets(m.state.alarms);
    });
    // Self-heal if the live feed stalls: resync from the API when the graph has
    // gone quiet for >15s while visible (and immediately on tab re-focus).
    onVis = () => { if (document.visibilityState === 'visible') loadHistory(); };
    document.addEventListener('visibilitychange', onVis);
    staleTimer = setInterval(() => {
      if (document.visibilityState === 'visible' && data[0].length
          && (Date.now() / 1000 - lastLiveTs) > 15) loadHistory();
    }, 10000);
  });
  onDestroy(() => {
    if (ro) ro.disconnect(); if (themeObs) themeObs.disconnect(); if (chart) chart.destroy(); stop && stop();
    clearInterval(staleTimer);
    if (onVis) document.removeEventListener('visibilitychange', onVis);
  });
</script>

<div class={embedded ? 'space-y-4' : 'px-4 pt-4 pb-28 lg:pb-10 max-w-xl lg:max-w-5xl mx-auto space-y-4'}>
  <div class="flex items-center gap-2 flex-wrap text-sm">
    <select class="hm-card rounded-lg px-2 h-10" bind:value={rangeMin} onchange={loadHistory}>
      <option value={'cook'}>This cook</option>
      <option value={30}>30 min</option>
      <option value={120}>2 hours</option>
      <option value={360}>6 hours</option>
      <option value={720}>12 hours</option>
      <option value={0}>All</option>
    </select>
    <select class="hm-card rounded-lg px-2 h-10" bind:value={compareSel} onchange={setCompare}>
      <option value="">Compare…</option>
      {#each sessions as s}<option value={s.id}>{s.name || ('Cook #' + s.id)}</option>{/each}
    </select>
    <button class="ml-auto px-4 h-10 inline-flex items-center rounded-lg bg-action text-white font-semibold" onclick={() => (noteOpen = true)}>+ Note</button>
  </div>

  <div class="hm-card rounded-2xl p-2"><div bind:this={chartEl}></div></div>

  {#if notes.length}
    <div class="flex gap-2 overflow-x-auto pb-1">
      {#each notes as n}
        <div class="hm-card rounded-xl p-2 flex items-center gap-2 shrink-0 max-w-[240px]">
          {#if n.photo}
            <a href={'/api/photo/' + n.photo} target="_blank" rel="noopener">
              <img class="w-10 h-10 object-cover rounded-md" src={'/api/photo/' + n.photo} alt="" />
            </a>
          {/if}
          <div class="min-w-0">
            <div class="text-[11px] opacity-50">{fmtClock(n.ts)}</div>
            <div class="text-sm truncate">{n.text}</div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') noteOpen = false; }} />

{#if noteOpen}
  <div class="fixed inset-0 z-40 bg-black/50 flex items-center justify-center p-4" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) noteOpen = false; }}>
    <div class="hm-card rounded-2xl p-4 w-full max-w-sm space-y-3" role="dialog" aria-modal="true" tabindex="-1">
      <h3 class="font-bold text-lg">Add note</h3>
      <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" placeholder="e.g. wrapped the brisket" maxlength="120" bind:value={noteText} />
      <input type="file" accept="image/*" class="w-full text-sm" onchange={(e) => (noteFile = e.target.files[0])} />
      <div class="flex gap-2 justify-end">
        <button class="px-4 py-2 rounded-lg bg-neutral-200 dark:bg-neutral-800" onclick={() => (noteOpen = false)}>Cancel</button>
        <button class="px-4 py-2 rounded-lg bg-action text-white font-semibold" onclick={saveNote}>Add</button>
      </div>
    </div>
  </div>
{/if}
