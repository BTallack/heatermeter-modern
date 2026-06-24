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
    { key: 'set_point', label: 'Set', stroke: '#9aa0a6', dash: [6, 4], width: 1 },
    { key: 'pit', label: 'Pit', stroke: '#ff5630', width: 2 },
    { key: 'food1', label: 'Food 1', stroke: '#36b37e', width: 2 },
    { key: 'food2', label: 'Food 2', stroke: '#00b8d9', width: 2 },
    { key: 'compare', label: 'vs', stroke: '#a78bfa', width: 1, dash: [2, 3] },
    { key: 'fan_pct', label: 'Fan %', stroke: '#ffd166', width: 1, scale: '%' },
    { key: 'servo_pct', label: 'Servo %', stroke: '#9aa7b3', width: 1, dash: [4, 3], scale: '%' },
  ];

  let chartEl;
  let chart = null;
  let data = [[], [], [], [], [], [], [], []]; // t,set,pit,food1,food2,compare,fan,servo
  let compareCurve = null; // {start, ts:[], pit:[]}
  let targets = { food1: null, food2: null }; // food target temps for graph lines
  let ro = null;
  let stop;

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
      stroke: '#8892a0',
      font: '12px "Work Sans Variable", system-ui, sans-serif',
      grid: { stroke: 'rgba(128,128,128,0.18)', width: 1 },
      ticks: { stroke: 'rgba(128,128,128,0.18)', width: 1 },
    }, extra);
  }

  // Dashed horizontal lines at the food-probe targets (on the temp 'y' scale).
  function targetLinesPlugin() {
    return { hooks: { draw: (u) => {
      const lines = [
        { v: targets.food1, color: '#36b37e' },
        { v: targets.food2, color: '#00b8d9' },
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
  const EVENT_COLORS = {
    lid_open: '#ffab00', lid_closed: '#ffab00',
    stall_start: '#a78bfa', stall_end: '#a78bfa',
    target: '#36b37e', probe_done: '#36b37e', cook_complete: '#36b37e',
    food_target: '#36b37e',
    setpoint: '#9aa0a6', stage: '#9aa0a6', program_done: '#9aa0a6',
    disconnect: '#ff5630', fault: '#ff5630', reconnect: '#6b9080',
    alarm_low: '#ff8b00',
  };
  const EVENT_CAPTIONS = {
    lid_open: 'Lid', stall_start: 'Stall', stall_end: 'Stall over',
    target: '✓', cook_complete: 'Done', disconnect: '✕',
  };
  function eventsPlugin() {
    return { hooks: { draw: (u) => {
      if (!events.length) return;
      const ctx = u.ctx, { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.font = '10px "Work Sans Variable", system-ui, sans-serif';
      ctx.textAlign = 'center';
      for (const ev of events) {
        if (ev.kind === 'prediction') continue;   // forecast data, not a marker
        const x = u.valToPos(ev.ts, 'x', true);
        if (x < left || x > left + width) continue;
        const color = EVENT_COLORS[ev.kind] || '#9aa0a6';
        ctx.strokeStyle = color; ctx.fillStyle = color;
        ctx.globalAlpha = 0.25; ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 0.9;
        ctx.beginPath(); ctx.arc(x, top + height - 5, 3, 0, Math.PI * 2); ctx.fill();
        const cap = ev.kind === 'setpoint' ? (ev.label || '').replace('Set ', '')
                  : ev.kind === 'stage' ? (ev.label || '').replace('Stage: ', '')
                  : ev.kind === 'food_target' ? (ev.value != null ? Math.round(ev.value) + '°' : '🎯')
                  : EVENT_CAPTIONS[ev.kind];
        if (cap) ctx.fillText(String(cap).slice(0, 12), x, top + height - 12);
      }
      ctx.restore();
    } } };
  }

  function notesPlugin() {
    return { hooks: { draw: (u) => {
      if (!notes.length) return;
      const ctx = u.ctx, { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.strokeStyle = '#ffab00'; ctx.fillStyle = '#ffab00';
      ctx.lineWidth = 1; ctx.font = '11px sans-serif';
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

  function makeChart() {
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
      plugins: [targetLinesPlugin(), eventsPlugin(), notesPlugin(), latestLegendPlugin()],
      // x series: show time-only in the legend (no date) so the legend row
      // doesn't wrap to a second line at normal width.
      series: [{ value: (u, ts) => (ts == null ? '--' : fmtClock(ts)) }, ...SERIES.map((s) => ({
        // Temperature series have no explicit scale key; bind them to 'y' so the
        // data lands on the same scale the temperature axis renders. Without the
        // `|| 'y'`, uPlot creates a stray scale keyed "undefined" for the lines
        // while the 'y' axis stays empty (axis shows 0-250, lines don't match).
        label: s.label, stroke: s.stroke, width: s.width, dash: s.dash, scale: s.scale || 'y',
        spanGaps: false, points: { show: false },
        value: (u, v) => (v == null ? '--' : v.toFixed(1) + (s.scale === '%' ? '%' : '°')),
      }))],
      axes: [
        axis(),                                          // x (time, bottom)
        axis({ size: 48, side: 1 }),                     // temperature -> right
        // Percent -> left. Its grid would double up with the temp grid, so
        // drop it and let the temperature scale own the gridlines.
        axis({ scale: '%', side: 3, size: 42, grid: { show: false },
               values: (u, vs) => vs.map((v) => v + '%') }),
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
  }

  function rangeQuery() {
    // "This cook" scopes to the live session; with nothing cooking, fall back
    // to a recent window rather than dumping all of history.
    if (rangeMin === 'cook') return sessionId ? `?session_id=${sessionId}` : '?minutes=120';
    return Number(rangeMin) > 0 ? `?minutes=${rangeMin}` : '';
  }
  async function loadHistory() {
    const q = rangeQuery();
    try { setData(await getJSON(`history${q}`)); } catch (_) {}
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
  });
  onDestroy(() => { if (ro) ro.disconnect(); if (chart) chart.destroy(); stop && stop(); });
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
    <button class="ml-auto px-4 h-10 inline-flex items-center rounded-lg bg-orange-600 text-white font-semibold" onclick={() => (noteOpen = true)}>+ Note</button>
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
        <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={saveNote}>Add</button>
      </div>
    </div>
  </div>
{/if}
