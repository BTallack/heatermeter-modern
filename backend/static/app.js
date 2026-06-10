"use strict";

// Series we plot, in order. Indexes line up with the uPlot data array.
const SERIES = [
  { key: "set_point", label: "Set", stroke: "#b3bac1", dash: [6, 4], width: 1 },
  { key: "pit", label: "Pit", stroke: "#ff5630", width: 2 },
  { key: "food1", label: "Food 1", stroke: "#36b37e", width: 2 },
  { key: "food2", label: "Food 2", stroke: "#00b8d9", width: 2 },
  { key: "compare", label: "vs (pit)", stroke: "#a78bfa", width: 1, dash: [2, 3] },
  // Output % series on a separate right-hand 0-100 scale. Click the legend to
  // hide them. Order here must match the `data` array indices below.
  { key: "fan_pct", label: "Fan %", stroke: "#ffd166", width: 1, scale: "%" },
  { key: "servo_pct", label: "Servo %", stroke: "#9aa7b3", width: 1, dash: [4, 3], scale: "%" },
];

const MAX_POINTS = 20000;

// Channels that can carry a "done" target. Each maps to a probe index, the
// HIGH-alarm slot that stores the target (probe idx 1->3, 2->5, 3->7), the
// status field, and the DOM ids for its value/eta nodes. The 4th probe is
// "ambient" by convention and is target-capable only when the user toggles it
// to a food probe (see ambientIsFood); its channel name ("ambient") differs
// from its DOM id stem ("amb"), which is why the ids are explicit here.
const FOOD_META = {
  food1:   { idx: 1, hi: 3, valId: "food1", etaId: "food1eta" },
  food2:   { idx: 2, hi: 5, valId: "food2", etaId: "food2eta" },
  ambient: { idx: 3, hi: 7, valId: "amb",   etaId: "ambeta"   },
};
const AMBIENT_AS_FOOD_KEY = "hm.ambientAsFood";
function ambientIsFood() { return localStorage.getItem(AMBIENT_AS_FOOD_KEY) === "1"; }
// Channels with a live target field right now (ambient only when toggled on).
function foodChannels() {
  return ambientIsFood() ? ["food1", "food2", "ambient"] : ["food1", "food2"];
}

// Probe 3 (ambient) target row stays in place but is greyed/disabled until it
// is toggled to a food probe, so enabling it never shifts the layout.
function setAmbientRowEnabled(on) {
  const row = document.getElementById("tgt3Row");
  if (row) row.classList.toggle("set-row--disabled", !on);
  for (const id of ["tgt3", "preset3"]) {
    const e = document.getElementById(id);
    if (e) e.disabled = !on;
  }
}

// t, set, pit, food1, food2, compare, fan%, servo%
let data = [[], [], [], [], [], [], [], []];
let chart = null;
let unit = "F";
let lastState = null;
let notes = [];
let foodTargets = { food1: null, food2: null, ambient: null };
let viewingSession = null;
let compareCurve = null;   // {start, ts:[], pit:[]} of a past session, or null

const $ = (id) => document.getElementById(id);

// DOM helpers (no innerHTML, so untrusted text can never inject markup).
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  if (children) for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function option(value, label) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  return o;
}

function fmt(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? "--" : Math.round(v);
}
function fmtDuration(seconds) {
  if (seconds == null) return "";
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function axisTheme() {
  return { stroke: "#8a97a3", grid: { stroke: "#2c343c", width: 1 },
           ticks: { stroke: "#2c343c", width: 1 } };
}

// uPlot plugin: vertical dashed markers + labels at note timestamps.
function notesPlugin() {
  return { hooks: { draw: (u) => {
    if (!notes.length) return;
    const ctx = u.ctx;
    const { left, top, width, height } = u.bbox;
    ctx.save();
    ctx.strokeStyle = "#ffab00"; ctx.fillStyle = "#ffab00";
    ctx.lineWidth = 1; ctx.font = "11px sans-serif";
    for (const n of notes) {
      const xpos = u.valToPos(n.ts, "x", true);
      if (xpos < left || xpos > left + width) continue;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(xpos, top); ctx.lineTo(xpos, top + height); ctx.stroke();
      ctx.setLineDash([]);
      ctx.save(); ctx.translate(xpos + 3, top + 4);
      ctx.fillText(String(n.text).slice(0, 24), 0, 8); ctx.restore();
    }
    ctx.restore();
  } } };
}

function makeChart() {
  const elc = $("chart");
  chart = new uPlot({
    width: elc.clientWidth || 800, height: 340,
    // Temps on the default left "y" scale; output % on a fixed 0-100 "%" scale.
    scales: { x: { time: true }, "%": { range: [0, 100] } },
    legend: { live: true },
    cursor: { drag: { x: true, y: false } }, plugins: [notesPlugin()],
    series: [{}, ...SERIES.map((s) => ({
      label: s.label, stroke: s.stroke, width: s.width, dash: s.dash,
      scale: s.scale,            // undefined => default temp scale; "%" => output
      spanGaps: false,
      value: (u, v) => (v == null ? "--" : v.toFixed(1) + (s.scale === "%" ? "%" : "°")),
      points: { show: false },
    }))],
    axes: [
      Object.assign({}, axisTheme()),                 // x (time)
      Object.assign({ size: 50 }, axisTheme()),       // left: temperature
      Object.assign({ scale: "%", side: 1, size: 44,  // right: output %
                      values: (u, vals) => vals.map((v) => v + "%") }, axisTheme()),
    ],
  }, data, elc);
  window.addEventListener("resize", () =>
    chart.setSize({ width: elc.clientWidth || 800, height: 340 }));
}

// Resample the compare curve onto the live time axis: for each live timestamp,
// find the past cook's pit temp at the same ELAPSED time from its start.
function compareAt(t) {
  if (!compareCurve || !data[0].length) return null;
  const liveStart = data[0][0];
  const elapsed = t - liveStart;                 // seconds into the live cook
  const target = compareCurve.start + elapsed;   // same elapsed in the past cook
  const ts = compareCurve.ts;
  if (target < ts[0] || target > ts[ts.length - 1]) return null;
  // Binary search for the bracketing sample.
  let lo = 0, hi = ts.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] <= target) lo = mid; else hi = mid; }
  const v0 = compareCurve.pit[lo], v1 = compareCurve.pit[hi];
  if (v0 == null || v1 == null) return v0 ?? v1 ?? null;
  const frac = (target - ts[lo]) / (ts[hi] - ts[lo] || 1);
  return v0 + (v1 - v0) * frac;
}

function rebuildCompareColumn() {
  data[5] = data[0].map((t) => compareAt(t));
}

function setData(cols) {
  data = [cols.t || [], cols.set_point || [], cols.pit || [], cols.food1 || [],
          cols.food2 || [], [], cols.fan_pct || [], cols.servo_pct || []];
  rebuildCompareColumn();   // fills data[5]
  if (chart) chart.setData(data);
}
function pushPoint(ts, st) {
  data[0].push(ts); data[1].push(st.set_point); data[2].push(st.pit);
  data[3].push(st.food1); data[4].push(st.food2); data[5].push(compareAt(ts));
  data[6].push(st.fan_pct); data[7].push(st.servo_pct);
  if (data[0].length > MAX_POINTS) for (const arr of data) arr.shift();
  if (chart) chart.setData(data);
}

// -- session compare --------------------------------------------------------

async function populateCompare() {
  try {
    const list = await getJSON("api/sessions");
    const sel = $("compareSel");
    const cur = sel.value;
    clear(sel);
    sel.appendChild(option("", "none"));
    for (const s of list) {
      if (s.ended_ts == null) continue;   // only finished cooks
      sel.appendChild(option(String(s.id), s.name || ("Cook #" + s.id)));
    }
    sel.value = cur;
  } catch (e) {}
}

async function setCompare(sessionId) {
  if (!sessionId) { compareCurve = null; rebuildCompareColumn(); if (chart) chart.setData(data); return; }
  try {
    const cols = await getJSON(`api/history?session_id=${sessionId}&limit=10000`);
    if (!cols.t || !cols.t.length) { compareCurve = null; }
    else { compareCurve = { start: cols.t[0], ts: cols.t, pit: cols.pit }; }
    rebuildCompareColumn();
    if (chart) chart.setData(data);
    toast(sessionId ? "Comparing to past cook." : "");
  } catch (e) { toast("Compare failed", true); }
}

function wireCompare() {
  $("compareSel").addEventListener("change", (e) => setCompare(e.target.value));
}

function resolveFoodTargets(state) {
  const al = state.alarms || [];
  const parse = (raw) => {
    if (raw == null) return null;
    const n = parseFloat(String(raw).replace(/[LH]$/, ""));
    return Number.isNaN(n) || n < 0 ? null : n;
  };
  foodTargets.food1 = parse(al[3]);
  foodTargets.food2 = parse(al[5]);
  foodTargets.ambient = ambientIsFood() ? parse(al[7]) : null;
}

function applyState(state) {
  lastState = state;
  const st = state.status || {};
  const names = state.probe_names || ["Pit", "Food1", "Food2", "Ambient"];
  if (state.pid && state.pid.units) unit = state.pid.units;
  resolveFoodTargets(state);

  $("device").textContent = state.version ? `v${state.version}` : "";
  $("pitLabel").textContent = names[0] || "Pit";
  $("food1Label").textContent = names[1] || "Food 1";
  $("food2Label").textContent = names[2] || "Food 2";
  $("ambLabel").textContent = names[3] || "Ambient";
  $("pitUnit").textContent = "°" + unit;

  $("pit").textContent = fmt(st.pit);
  $("set").textContent = fmt(st.set_point);
  $("food1").textContent = fmt(st.food1);
  $("food2").textContent = fmt(st.food2);
  $("amb").textContent = fmt(st.ambient);
  $("fan").textContent = fmt(st.fan_pct);

  const fan = Math.max(0, Math.min(100, st.fan_pct || 0));
  $("fanBar").style.width = fan + "%";
  $("servoSub").textContent = (st.servo_pct != null && st.servo_pct > 0)
    ? `servo ${Math.round(st.servo_pct)}%` : "";

  const lid = st.lid_countdown || 0;
  // Pit sub-line: lid countdown takes priority; otherwise show the PID mode
  // label (only present on fork firmware 20260601-hm1+, blank on stock).
  $("lid").textContent = lid > 0 ? `LID OPEN ${lid}s` : (st.pid_mode_label || "");

  if (document.activeElement !== $("spInput") && st.set_point != null) {
    $("spInput").value = Math.round(st.set_point);
  }

  // Ambient probe: show or hide its food-probe affordances (target button +
  // eta line) based on the toggle. When off, it is a plain reading.
  const ambFood = ambientIsFood();
  const ambBtn = $("ambTargetBtn");
  if (ambBtn) ambBtn.hidden = !ambFood;
  const ambEta = $("ambeta");
  if (ambEta && !ambFood) ambEta.textContent = "";

  for (const ch of foodChannels()) {
    const tgt = foodTargets[ch];
    const sub = $(FOOD_META[ch].etaId);
    if (!sub) continue;
    if (tgt == null) sub.textContent = "";
    else if (sub.dataset.eta) sub.textContent = `→ ${tgt}° · ${sub.dataset.eta}`;
    else sub.textContent = `→ ${tgt}°`;
  }

  renderPidInternals(state.pid_internals);
}

// PID component breakdown card. The board only sends $HMPS when tp=1 is enabled;
// when present, b+p+i+d = the output %. We show P/I/D as signed bars.
function renderPidInternals(pi) {
  const card = $("pidCard");
  if (!pi || pi.p == null) { card.hidden = true; return; }
  card.hidden = false;
  const p = parseFloat(pi.p), i = parseFloat(pi.i), d = parseFloat(pi.d);
  const b = pi.b != null ? parseFloat(pi.b) : 0;
  // Scale bars relative to 100% output; clamp width 0-100, sign shown by colour.
  const setBar = (id, valId, v) => {
    if (Number.isNaN(v)) { $(valId).textContent = "--"; $(id).style.width = "0%"; return; }
    $(valId).textContent = (v >= 0 ? "+" : "") + v.toFixed(1);
    $(id).style.width = Math.min(100, Math.abs(v)) + "%";
    $(id).classList.toggle("neg", v < 0);
  };
  setBar("pidBarP", "pidValP", p);
  setBar("pidBarI", "pidValI", i);
  setBar("pidBarD", "pidValD", d);
  const sum = (Number.isNaN(p) ? 0 : p) + (Number.isNaN(i) ? 0 : i) +
              (Number.isNaN(d) ? 0 : d) + b;
  $("pidSum").textContent = `output ${sum.toFixed(0)}%`;
}

function setConn(on) {
  $("dot").className = "dot " + (on ? "dot--on" : "dot--off");
  $("connText").textContent = viewingSession ? "viewing past cook"
    : (on ? "live" : "reconnecting…");
}

// -- networking -------------------------------------------------------------

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function postJSON(path, body, method = "POST") {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).error || ""; } catch (e) {}
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

async function loadHistory(minutes) {
  if (viewingSession) return;
  const q = minutes && minutes > 0 ? `?minutes=${minutes}` : "";
  try { setData(await getJSON(`api/history${q}`)); } catch (e) {}
  try { notes = await getJSON("api/notes"); renderNotesStrip(); if (chart) chart.redraw(); } catch (e) {}
}

// Render the timeline-notes strip below the chart (time + text + photo thumb).
function renderNotesStrip() {
  const strip = $("notesStrip");
  if (!strip) return;
  clear(strip);
  if (!notes || !notes.length) { strip.hidden = true; return; }
  strip.hidden = false;
  for (const n of notes) {
    const t = new Date(n.ts * 1000).toLocaleTimeString([],
      { hour: "2-digit", minute: "2-digit" });
    const chip = el("div", { class: "note-chip" });
    if (n.photo) {
      const a = el("a", { href: "api/photo/" + n.photo, target: "_blank", rel: "noopener" });
      a.appendChild(el("img", { class: "note-thumb", src: "api/photo/" + n.photo, alt: "" }));
      chip.appendChild(a);
    }
    chip.appendChild(el("div", { class: "note-chip__txt" }, [
      el("span", { class: "note-chip__time", text: t }),
      el("span", { text: n.text || "" }),
    ]));
    strip.appendChild(chip);
  }
}

// Downscale a chosen photo client-side (phone shots are huge) to a JPEG data URL.
function fileToDataURL(file, maxDim = 1280, quality = 0.82) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      let w = img.width, h = img.height;
      const scale = Math.min(1, maxDim / Math.max(w, h));
      w = Math.round(w * scale); h = Math.round(h * scale);
      const cv = document.createElement("canvas");
      cv.width = w; cv.height = h;
      cv.getContext("2d").drawImage(img, 0, 0, w, h);
      try { resolve(cv.toDataURL("image/jpeg", quality)); }
      catch (e) { reject(e); }
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("bad image")); };
    img.src = url;
  });
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connectWs, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event) { handleEvent(msg.event); return; }
    if (!msg.state) return;
    applyState(msg.state);
    if (!viewingSession && msg.ts && msg.state.status) pushPoint(msg.ts, msg.state.status);
  };
}

function notify(title, body, tag) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  // Prefer the service worker (shows even when the tab is backgrounded); fall
  // back to a page Notification when no SW is controlling the page yet.
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: "notify", title, body, tag });
  } else {
    try { new Notification(title, { body, icon: "/icon.svg", tag }); } catch (e) {}
  }
}

function handleEvent(ev) {
  if (ev.type === "alarm") {
    const where = ev.probe_name || `Probe ${ev.probe}`;
    toast(`Alarm: ${where} ${ev.edge === "high" ? "high" : "low"}`, true);
    notify("HeaterMeter alarm", `${where} ${ev.edge}`, "hm-alarm");
  } else if (ev.type === "session_started") {
    if (!viewingSession) loadHistory(parseInt($("rangeSel").value, 10));
  } else if (ev.type === "autotune") {
    renderAutotune(ev.phase === "done" ? null : ev, ev);
    if (ev.phase === "done") {
      if (ev.success) toast("Auto-tune complete - new PID written.");
      else toast("Auto-tune stopped: " + (ev.error || "unknown"), true);
      pollAutotune();
    }
  } else if (ev.type === "program") {
    if (ev.program) renderProgramLive(ev.program);
    if (ev.event === "advanced") toast(`Program: now "${ev.program.stage_name}"`);
    else if (ev.event === "completed") toast("Cook program complete.");
    else if (ev.event === "stopped") { $("programBanner").hidden = true; }
  }
}

async function sendSetpoint(value) {
  try { await postJSON("api/setpoint", { value, unit }); } catch (e) {}
}

// -- toast ------------------------------------------------------------------

let toastTimer = null;
function toast(text, isErr) {
  let t = $("toast");
  if (!t) {
    t = el("div", { id: "toast" });
    t.style.cssText = "position:fixed;top:14px;left:50%;transform:translateX(-50%);" +
      "z-index:30;padding:10px 16px;border-radius:10px;font-size:14px;font-weight:600;" +
      "box-shadow:0 6px 20px rgba(0,0,0,.4);";
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.style.background = isErr ? "#ff5630" : "#36b37e";
  t.style.color = "#fff"; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

// -- predictions ------------------------------------------------------------

async function pollPredictions() {
  if (viewingSession) return;
  for (const ch of foodChannels()) {
    const tgt = foodTargets[ch];
    const sub = $(FOOD_META[ch].etaId);
    if (!sub) continue;
    const live = lastState && lastState.status ? lastState.status[ch] : null;
    if (tgt == null || live == null) { delete sub.dataset.eta; continue; }
    try {
      const p = await getJSON(`api/predict?channel=${ch}&target=${tgt}`);
      if (p.eta_seconds == null) delete sub.dataset.eta;
      else if (p.eta_seconds === 0) sub.dataset.eta = "done";
      else sub.dataset.eta = `${fmtDuration(p.eta_seconds)} (${p.confidence})`;
    } catch (e) { delete sub.dataset.eta; }
  }
  if (lastState) applyState(lastState);
}

// -- cook programs ----------------------------------------------------------

let stageRows = [];   // [{name, setpoint, advType, channel, temp, minutes}]

function newStage() {
  return { name: "", setpoint: 225, advType: "probe", channel: "food1", temp: 165, minutes: 60 };
}

function renderStages() {
  const wrap = $("stageList");
  clear(wrap);
  stageRows.forEach((s, i) => {
    const box = el("div", { class: "stage" });
    const head = el("div", { class: "stage__head" }, [
      el("span", { class: "stage__num", text: `Stage ${i + 1}` }),
      (() => { const b = el("button", { class: "linkbtn linkbtn--danger", text: "remove" });
               b.addEventListener("click", () => { stageRows.splice(i, 1); renderStages(); }); return b; })(),
    ]);
    // Name
    const nameIn = el("input", { type: "text", placeholder: "Stage name", value: s.name });
    nameIn.addEventListener("input", () => { s.name = nameIn.value; });
    // Setpoint (or OFF)
    const spIn = el("input", { type: "number", value: s.setpoint == null ? "" : s.setpoint, placeholder: "°F or off" });
    spIn.addEventListener("input", () => { s.setpoint = spIn.value.trim() === "" ? null : spIn.value; });
    const offBtn = el("button", { class: "linkbtn", text: "shutdown" });
    offBtn.addEventListener("click", () => { s.setpoint = "off"; spIn.value = ""; spIn.placeholder = "OFF (shutdown)"; });
    // Advance condition
    const advSel = el("select", { class: "ptsel" });
    [["probe", "until probe hits"], ["time", "for a duration"], ["manual", "until I tap Next"]].forEach(([v, l]) => {
      const o = option(v, l); if (v === s.advType) o.selected = true; advSel.appendChild(o);
    });
    const probeBox = el("span", {}, []);
    const chSel = el("select", { class: "ptsel", style: "max-width:90px" });
    [["food1", "Food 1"], ["food2", "Food 2"], ["pit", "Pit"], ["ambient", "Ambient"]].forEach(([v, l]) => {
      const o = option(v, l); if (v === s.channel) o.selected = true; chSel.appendChild(o);
    });
    chSel.addEventListener("change", () => { s.channel = chSel.value; });
    const tempIn = el("input", { type: "number", value: s.temp, style: "max-width:64px" });
    tempIn.addEventListener("input", () => { s.temp = tempIn.value; });
    const minIn = el("input", { type: "number", value: s.minutes, style: "max-width:64px" });
    minIn.addEventListener("input", () => { s.minutes = minIn.value; });

    function syncAdv() {
      clear(probeBox);
      if (s.advType === "probe") { probeBox.append(chSel, document.createTextNode(" ≥ "), tempIn, document.createTextNode("°")); }
      else if (s.advType === "time") { probeBox.append(minIn, document.createTextNode(" min")); }
    }
    advSel.addEventListener("change", () => { s.advType = advSel.value; syncAdv(); });
    syncAdv();

    const spRow = el("div", { class: "stage__row" }, [el("label", { text: "Hold at" }), spIn, offBtn]);
    const advRow = el("div", { class: "stage__row" }, [el("label", { text: "Advance" }), advSel]);
    const advRow2 = el("div", { class: "stage__row stage__row--cond" }, [el("label", {}), probeBox]);
    box.append(head, el("div", { class: "stage__row" }, [el("label", { text: "Name" }), nameIn]), spRow, advRow, advRow2);
    wrap.appendChild(box);
  });
}

function stagesToProgram() {
  return stageRows.map((s) => {
    const stage = { name: s.name || undefined, setpoint: s.setpoint };
    if (s.advType === "probe") stage.advance = { type: "probe", channel: s.channel, temp: parseFloat(s.temp) };
    else if (s.advType === "time") stage.advance = { type: "time", seconds: parseFloat(s.minutes) * 60 };
    else stage.advance = { type: "manual" };
    return stage;
  });
}

function renderProgramLive(st) {
  const running = st && !st.done && st.stage_count;
  // Banner on the dashboard.
  const banner = $("programBanner");
  if (running) {
    banner.hidden = false;
    $("programBannerText").textContent =
      `${st.name || "Program"} · stage ${st.stage_index + 1}/${st.stage_count}: ${st.stage_name || ""}`;
  } else { banner.hidden = true; }
  // Drawer detail.
  const box = $("programLiveBox");
  if (running) {
    box.hidden = false;
    const wrap = $("programLive");
    clear(wrap);
    (st.stages || []).forEach((stg, i) => {
      const cur = i === st.stage_index;
      const done = i < st.stage_index;
      const adv = stg.advance || {};
      let cond = "manual";
      if (adv.type === "probe") cond = `→ ${adv.channel} ≥ ${adv.temp}°`;
      else if (adv.type === "time") cond = `→ ${Math.round(adv.seconds / 60)} min`;
      const sp = stg.shutdown ? "OFF" : `${stg.setpoint}°`;
      const row = el("div", { class: "prog-stage" + (cur ? " current" : "") + (done ? " done" : "") },
        [el("span", { text: `${i + 1}. ${stg.name} — ${sp} ${cond}` })]);
      wrap.appendChild(row);
    });
  } else { box.hidden = true; }
}

function renderSavedPrograms(list) {
  const wrap = $("savedPrograms");
  clear(wrap);
  if (!list.length) { wrap.appendChild(el("p", { class: "hint", text: "No saved programs yet." })); return; }
  for (const p of list) {
    const loadBtn = el("button", { class: "linkbtn", text: "Load" });
    loadBtn.addEventListener("click", () => loadProgram(p));
    const startBtn = el("button", { class: "linkbtn", text: "Start" });
    startBtn.addEventListener("click", () => startProgram(p.stages, p.name));
    const delBtn = el("button", { class: "linkbtn linkbtn--danger", text: "Delete" });
    delBtn.addEventListener("click", async () => {
      try { await postJSON(`api/programs/${p.id}`, undefined, "DELETE"); loadSavedPrograms(); } catch (e) {}
    });
    wrap.appendChild(el("div", { class: "session" }, [
      el("div", { class: "session__name", text: `${p.name} (${p.stages.length} stages)` }),
      el("div", { class: "session__actions" }, [loadBtn, startBtn, delBtn]),
    ]));
  }
}

function loadProgram(p) {
  $("progName").value = p.name || "";
  stageRows = (p.stages || []).map((stg) => {
    const adv = stg.advance || { type: "manual" };
    return {
      name: stg.name || "",
      setpoint: stg.shutdown ? "off" : stg.setpoint,
      advType: adv.type,
      channel: adv.channel || "food1",
      temp: adv.temp != null ? adv.temp : 165,
      minutes: adv.seconds != null ? Math.round(adv.seconds / 60) : 60,
    };
  });
  renderStages();
  setMsg("Loaded " + (p.name || "program"), false, "programMsg");
}

async function startProgram(stages, name) {
  try {
    await postJSON("api/program/start", { stages, name });
    toast("Program started.");
    pollProgram();
  } catch (e) { setMsg("Start failed: " + e.message, true, "programMsg"); }
}

async function loadSavedPrograms() {
  try { renderSavedPrograms(await getJSON("api/programs")); } catch (e) {}
}

async function pollProgram() {
  try {
    const st = await getJSON("api/program");
    renderProgramLive(st);
    if (st && !st.done && st.stage_count) setTimeout(pollProgram, 5000);
  } catch (e) {}
}

function openProgram() {
  if (!stageRows.length) { stageRows = [newStage()]; renderStages(); }
  loadSavedPrograms();
  pollProgram();
  $("programOverlay").hidden = false; $("programPanel").hidden = false;
}
function closeProgram() { $("programOverlay").hidden = true; $("programPanel").hidden = true; }

async function advanceProgram() {
  try { await postJSON("api/program/advance", undefined, "POST"); pollProgram(); }
  catch (e) { toast("Advance failed", true); }
}
async function stopProgram() {
  if (!confirm("Stop the running program?")) return;
  try { await postJSON("api/program/stop", undefined, "POST"); toast("Program stopped."); pollProgram(); }
  catch (e) {}
}

function wireProgram() {
  $("programBtn").addEventListener("click", openProgram);
  $("programClose").addEventListener("click", closeProgram);
  $("programOverlay").addEventListener("click", closeProgram);
  $("addStageBtn").addEventListener("click", () => { stageRows.push(newStage()); renderStages(); });
  $("programStart").addEventListener("click", async () => {
    const stages = stagesToProgram();
    if (!stages.length) { setMsg("Add at least one stage.", true, "programMsg"); return; }
    await startProgram(stages, $("progName").value.trim());
  });
  $("programSave").addEventListener("click", async () => {
    const name = $("progName").value.trim();
    if (!name) { setMsg("Name the program first.", true, "programMsg"); return; }
    try { await postJSON("api/programs", { name, stages: stagesToProgram() }); setMsg("Saved.", false, "programMsg"); loadSavedPrograms(); }
    catch (e) { setMsg("Save failed: " + e.message, true, "programMsg"); }
  });
  $("programAdvance").addEventListener("click", advanceProgram);
  $("programStop").addEventListener("click", stopProgram);
  $("programAdvance2").addEventListener("click", advanceProgram);
  $("programStop2").addEventListener("click", stopProgram);
}

// -- settings ---------------------------------------------------------------

let probeOptionsReady = false;
let presetsCache = null;
let probeTypeLabels = {};   // numeric type -> label, from /api/probe-presets

function setMsg(text, isErr, which = "settingsMsg") {
  // Feedback shows as a top-of-page toast "pill" (same as the MQTT test result),
  // not an inline line inside the panel. Signature kept so every caller (the
  // settings savers and the cook-program drawer) works unchanged. Any legacy
  // inline element is cleared if it still exists in the DOM.
  if (text) toast(text, isErr);
  const m = document.getElementById(which);
  if (m) m.textContent = "";
}
function numVal(id) { const v = $(id).value.trim(); return v === "" ? null : parseFloat(v); }

function populateSettings() {
  const s = lastState;
  if (!s) return;
  const labels = ["Pit", "Food 1", "Food 2", "Ambient"];
  const names = s.probe_names || [];
  for (let i = 0; i < 4; i++) {
    const nm = names[i] || labels[i];
    $("pn" + i).value = names[i] || "";
    $("off" + i + "l").textContent = nm;
    $("al" + i + "l").textContent = nm;
    $("pt" + i + "l").textContent = nm;
  }
  const offs = s.probe_offsets || [];
  for (let i = 0; i < 4; i++) $("off" + i).value = (offs[i] ?? "");

  // Reflect the board's CURRENT probe type next to each select (from $HMPC).
  const coeffs = s.probe_coeffs || {};
  for (let i = 0; i < 4; i++) {
    const c = coeffs[i];
    const lbl = $("pt" + i + "l");
    let suffix = "";
    if (c && c.type != null) {
      const tl = probeTypeLabels[String(c.type)];
      if (tl) suffix = ` — ${tl}`;
    }
    lbl.textContent = (names[i] || labels[i]) + suffix;
  }

  const pid = s.pid || {};
  $("pidp").value = pid.p ?? "";
  $("pidi").value = pid.i ?? "";
  $("pidd").value = pid.d ?? "";

  const al = s.alarms || [];
  for (let i = 0; i < 4; i++) {
    $("alLo" + i).value = (al[i * 2] ?? "");
    $("alHi" + i).value = (al[i * 2 + 1] ?? "");
  }

  $("tgt1").value = foodTargets.food1 ?? "";
  $("tgt2").value = foodTargets.food2 ?? "";
  $("tgt1l").textContent = names[1] || "Food 1";
  $("tgt2l").textContent = names[2] || "Food 2";
  // Ambient-as-food-probe toggle + its target row. The Probe 3 row is always
  // shown (right under Probe 2) but greyed/disabled until the toggle is on, so
  // the layout never shifts.
  const ambFood = ambientIsFood();
  if ($("ambAsFood")) $("ambAsFood").checked = ambFood;
  if ($("tgt3")) $("tgt3").value = foodTargets.ambient ?? "";
  if ($("tgt3l")) $("tgt3l").textContent = names[3] || "Ambient";
  setAmbientRowEnabled(ambFood);

  const fan = s.fan || {};
  $("fnLow").value = fan.low ?? "";
  $("fnHigh").value = fan.high ?? "";
  $("fnStartup").value = fan.max_startup ?? "";
  $("fnFloor").value = fan.fan_active_floor ?? "";
  $("fnServoMin").value = fan.servo_min ?? "";
  $("fnServoMax").value = fan.servo_max ?? "";
  $("fnServoCeil").value = fan.servo_active_ceil ?? "";
  const flags = parseInt(fan.flags || 0, 10) || 0;
  $("fnInvFan").checked = !!(flags & 1);
  $("fnInvServo").checked = !!(flags & 2);

  const lid = s.lid_detect || {};
  $("lidOffset").value = lid.offset_percent ?? "";
  $("lidDur").value = lid.duration ?? "";

  // LCD / LEDs from decoded $HMLB.
  const disp = s.display || {};
  const invBit = window._ledInvertBit || 0x80;
  if (disp.backlight != null && document.activeElement !== $("lcdBacklight"))
    $("lcdBacklight").value = disp.backlight;
  if (disp.home_mode != null && $("lcdHome").options.length)
    $("lcdHome").value = String(disp.home_mode);
  const leds = disp.leds || [];
  for (let i = 0; i < 4; i++) {
    const raw = leds[i] != null ? parseInt(leds[i], 10) : null;
    if (raw == null || Number.isNaN(raw)) continue;
    const stim = raw & ~invBit;
    if ($("lcdLed" + i).options.length) $("lcdLed" + i).value = String(stim);
    $("lcdLed" + i + "inv").checked = !!(raw & invBit);
  }
}

async function ensureOptions() {
  if (!presetsCache) {
    try { presetsCache = await getJSON("api/presets"); } catch (e) { presetsCache = {}; }
  }
  if (probeOptionsReady) return;

  let probePresets = {};
  try {
    const resp = await getJSON("api/probe-presets");
    probePresets = resp.presets || {};
    probeTypeLabels = resp.types || {};
  } catch (e) {}
  for (let i = 0; i < 4; i++) {
    const sel = $("pt" + i);
    clear(sel);
    sel.appendChild(option("", "Leave unchanged"));
    for (const [key, v] of Object.entries(probePresets)) sel.appendChild(option(key, v.label));
    sel.appendChild(option("__disabled", "Disabled"));
    sel.addEventListener("change", () => { sel.dataset.touched = "1"; });
  }

  // Meat preset dropdowns (Probe 3 included; it's disabled until toggled on).
  const meat = (presetsCache.meat) || [];
  for (const id of ["preset1", "preset2", "preset3"]) {
    const sel = $(id);
    if (!sel) continue;
    clear(sel);
    sel.appendChild(option("", "Preset…"));
    for (const p of meat) sel.appendChild(option(String(p.temp_f), `${p.label} (${p.temp_f}°)`));
  }

  // PID presets.
  const pidPresets = (presetsCache.pid) || [];
  const pp = $("pidPreset");
  clear(pp);
  pp.appendChild(option("", "Custom"));
  for (const p of pidPresets) pp.appendChild(option(p.key, p.label));
  // onchange (not addEventListener) so this isn't stacked each time
  // populateSettings re-runs while the drawer is open. Filling the fields here
  // uses .value = (programmatic), which does NOT fire the input listeners wired
  // in wireSettings(), so picking a preset keeps the preset name showing; only
  // the user typing flips the selector to "Custom".
  pp.onchange = () => {
    const p = pidPresets.find((x) => x.key === pp.value);
    if (!p) { $("pidPresetNote").textContent = ""; return; }
    $("pidb").value = p.b; $("pidp").value = p.p; $("pidi").value = p.i; $("pidd").value = p.d;
    $("pidPresetNote").textContent = p.note || "";
  };

  // Blower presets.
  const blowerPresets = (presetsCache.blower) || [];
  const bp = $("blowerPreset");
  clear(bp);
  bp.appendChild(option("", "Custom / pick a preset…"));
  for (const p of blowerPresets) bp.appendChild(option(p.key, p.label));
  bp.addEventListener("change", () => {
    const p = blowerPresets.find((x) => x.key === bp.value);
    if (!p) { $("blowerPresetNote").textContent = ""; return; }
    $("fnLow").value = p.fan_low; $("fnHigh").value = p.fan_high;
    $("fnStartup").value = p.max_startup; $("fnFloor").value = p.fan_active_floor;
    $("blowerPresetNote").textContent = p.note || "";
  });

  // LCD / LED config dropdowns from /api/lcd-options.
  try {
    const lcd = await getJSON("api/lcd-options");
    const home = $("lcdHome");
    clear(home);
    home.appendChild(option("", "Leave unchanged"));
    for (const [v, label] of Object.entries(lcd.home_modes || {})) home.appendChild(option(v, label));
    for (let i = 0; i < 4; i++) {
      const sel = $("lcdLed" + i);
      clear(sel);
      sel.appendChild(option("", "Leave unchanged"));
      for (const [v, label] of Object.entries(lcd.led_stimuli || {})) sel.appendChild(option(v, label));
    }
    window._ledInvertBit = lcd.led_invert_bit || 0x80;
  } catch (e) {}

  probeOptionsReady = true;
}

const SAVERS = {
  async names() {
    for (let i = 0; i < 4; i++) {
      const name = $("pn" + i).value.trim();
      if (name) await postJSON("api/probe-name", { index: i, name });
    }
    setMsg("Probe names saved.");
  },
  async probetypes() {
    for (let i = 0; i < 4; i++) {
      const val = $("pt" + i).value;
      if (val === "") continue;
      if (val === "__disabled") await postJSON("api/probe-type", { index: i, disabled: true });
      else await postJSON("api/probe-type", { index: i, preset: val });
      $("pt" + i).dataset.touched = "";
    }
    setMsg("Probe types applied.");
  },
  async offsets() {
    await postJSON("api/offsets", { offsets: [0, 1, 2, 3].map((i) => numVal("off" + i)) });
    setMsg("Offsets saved.");
  },
  async targets() {
    const t1 = numVal("tgt1"), t2 = numVal("tgt2");
    // Ambient gets a target only when it is toggled to a food probe; otherwise
    // leave its HIGH-alarm slot untouched (null = no change).
    const t3 = ambientIsFood() ? numVal("tgt3") : null;
    await postJSON("api/alarms", { thresholds: [null, null, null, t1, null, t2, null, t3] });
    setMsg("Targets saved.");
  },
  async pid() {
    await postJSON("api/pid", { b: numVal("pidb"), p: numVal("pidp"), i: numVal("pidi"), d: numVal("pidd") });
    setMsg("PID saved.");
  },
  async fan() {
    await postJSON("api/fan", {
      fan_low: numVal("fnLow"), fan_high: numVal("fnHigh"),
      max_startup: numVal("fnStartup"), fan_active_floor: numVal("fnFloor"),
      servo_min: numVal("fnServoMin"), servo_max: numVal("fnServoMax"),
      servo_active_ceil: numVal("fnServoCeil"),
      invert_fan: $("fnInvFan").checked, invert_servo: $("fnInvServo").checked,
    });
    setMsg("Fan settings saved.");
  },
  async lid() {
    await postJSON("api/lid", { offset_percent: numVal("lidOffset"), duration_seconds: numVal("lidDur") });
    setMsg("Lid detection saved.");
  },
  async lcd() {
    const invBit = window._ledInvertBit || 0x80;
    // Build the 4 LED bytes only if all four selects have a value; otherwise
    // omit leds entirely (leave unchanged). A blank select means "unchanged",
    // but the firmware lb command is positional, so we send leds only when the
    // user has set all four (the populate step pre-fills them from the board).
    let leds = null;
    const vals = [0, 1, 2, 3].map((i) => $("lcdLed" + i).value);
    if (vals.every((v) => v !== "")) {
      leds = vals.map((v, i) => parseInt(v, 10) | ($("lcdLed" + i + "inv").checked ? invBit : 0));
    }
    const home = $("lcdHome").value;
    await postJSON("api/lcd", {
      backlight: numVal("lcdBacklight"),
      home_mode: home === "" ? null : parseInt(home, 10),
      leds,
    });
    setMsg("LCD & LEDs saved.");
  },
  async alarms() {
    const thresholds = [];
    for (let i = 0; i < 4; i++) { thresholds.push(numVal("alLo" + i)); thresholds.push(numVal("alHi" + i)); }
    await postJSON("api/alarms", { thresholds });
    setMsg("Alarms saved.");
  },
  async manual() {
    const percent = numVal("manPct") || 0;
    await postJSON("api/manual", { percent });
    setMsg(`Manual output set to ${percent}%.`);
  },
  async mqtt() {
    const m = await postJSON("api/mqtt", mqttBodyFromForm());
    $("mqttPass").value = "";
    $("mqttPass").placeholder = m.has_password ? "(unchanged)" : "(none set)";
    renderMqttStatus(m);
    // The connection completes asynchronously; refresh the status shortly.
    setTimeout(loadMqttConfig, 1500);
    setMsg("Home Assistant settings saved.");
  },
  async notify() {
    const n = await postJSON("api/notify", notifyBodyFromForm());
    $("ntToken").value = "";
    $("ntToken").placeholder = n.has_token ? "(unchanged)" : "(none)";
    renderNotifyStatus(n);
    setMsg("Notification settings saved.");
  },
};

async function refreshSettings() {
  try {
    lastState = await getJSON("api/status");
    if (!$("settingsPanel").hidden && !$("settingsPanel").contains(document.activeElement)) {
      populateSettings();
    }
  } catch (e) {}
}

async function openSettings() {
  await ensureOptions();
  populateSettings();
  pollAutotune();
  showSettingsTab(localStorage.getItem("settingsTab") || "probes");
  loadMqttConfig();
  loadNotifyConfig();
  $("settingsOverlay").hidden = false;
  $("settingsPanel").hidden = false;
}
function closeSettings() { $("settingsOverlay").hidden = true; $("settingsPanel").hidden = true; }

// Show one settings tab: highlight its button and reveal only the set-groups
// tagged for it (DOM order within a tab is the display order). Resets that
// tab's scroll to the top and remembers the choice.
function showSettingsTab(tab) {
  const tabs = document.querySelectorAll("#settingsTabs button");
  let valid = false;
  tabs.forEach((b) => { if (b.dataset.tab === tab) valid = true; });
  if (!valid) tab = "probes";
  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("#settingsBody .set-group").forEach((s) => {
    s.hidden = (s.dataset.tab !== tab);
  });
  const body = $("settingsBody");
  if (body) body.scrollTop = 0;
  localStorage.setItem("settingsTab", tab);
}

// -- MQTT / Home Assistant config -------------------------------------------

function renderMqttStatus(m) {
  const el = $("mqttStatusHint");
  if (!el) return;
  if (!m || !m.enabled) { el.textContent = "disabled"; el.style.color = "var(--muted)"; return; }
  if (m.connected) { el.textContent = "connected"; el.style.color = "var(--food1)"; }
  else if (m.last_error) { el.textContent = m.last_error; el.style.color = "var(--pit)"; }
  else { el.textContent = "connecting…"; el.style.color = "var(--muted)"; }
}

async function loadMqttConfig() {
  try {
    const m = await getJSON("api/mqtt");
    $("mqttEnabled").checked = !!m.enabled;
    $("mqttHost").value = m.host || "";
    $("mqttPort").value = m.port || 1883;
    $("mqttUser").value = m.username || "";
    $("mqttNode").value = m.node_id || "hm";
    $("mqttPass").value = "";
    $("mqttPass").placeholder = m.has_password ? "(unchanged)" : "(none set)";
    renderMqttStatus(m);
  } catch (e) { /* MQTT panel just stays blank if the endpoint is unavailable */ }
}

function mqttBodyFromForm() {
  return {
    enabled: $("mqttEnabled").checked,
    host: $("mqttHost").value.trim(),
    port: parseInt($("mqttPort").value, 10) || 1883,
    username: $("mqttUser").value.trim(),
    password: $("mqttPass").value,   // blank => server keeps the stored one
    node_id: $("mqttNode").value.trim() || "hm",
  };
}

// -- Notifications (ntfy) ---------------------------------------------------

function renderNotifyStatus(n) {
  const el = $("notifyStatusHint");
  if (!el) return;
  if (!n || !n.enabled) { el.textContent = "disabled"; el.style.color = "var(--muted)"; }
  else if (!n.topic) { el.textContent = "no topic set"; el.style.color = "var(--pit)"; }
  else { el.textContent = "on → " + n.topic; el.style.color = "var(--food1)"; }
}

async function loadNotifyConfig() {
  try {
    const n = await getJSON("api/notify");
    $("ntEnabled").checked = !!n.enabled;
    $("ntServer").value = n.server || "https://ntfy.sh";
    $("ntTopic").value = n.topic || "";
    $("ntToken").value = "";
    $("ntToken").placeholder = n.has_token ? "(unchanged)" : "(none)";
    $("ntDebounce").value = n.debounce_sec ?? 30;
    $("ntRepeat").value = n.repeat_min ?? 0;
    $("ntDark").value = n.dark_timeout_sec ?? 90;
    renderNotifyStatus(n);
  } catch (e) { /* leave blank if endpoint unavailable */ }
}

function notifyBodyFromForm() {
  return {
    enabled: $("ntEnabled").checked,
    server: $("ntServer").value.trim() || "https://ntfy.sh",
    topic: $("ntTopic").value.trim(),
    token: $("ntToken").value,   // blank => server keeps the stored one
    debounce_sec: parseInt($("ntDebounce").value, 10) || 0,
    repeat_min: parseInt($("ntRepeat").value, 10) || 0,
    dark_timeout_sec: parseInt($("ntDark").value, 10) || 0,
  };
}

function wireSettings() {
  $("settingsBtn").addEventListener("click", openSettings);
  $("settingsClose").addEventListener("click", closeSettings);
  $("settingsOverlay").addEventListener("click", closeSettings);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAll(); });

  // Settings tabs: switch which group of cards is shown.
  document.querySelectorAll("#settingsTabs button").forEach((b) => {
    b.addEventListener("click", () => showSettingsTab(b.dataset.tab));
  });

  // MQTT: test the entered credentials without saving them.
  $("mqttTest").addEventListener("click", async () => {
    const btn = $("mqttTest"), hint = $("mqttStatusHint");
    btn.disabled = true;
    if (hint) { hint.textContent = "testing…"; hint.style.color = "var(--muted)"; }
    try {
      const r = await postJSON("api/mqtt/test", mqttBodyFromForm());
      if (r.ok) {
        if (hint) { hint.textContent = "test OK"; hint.style.color = "var(--food1)"; }
        toast("MQTT connection OK.");
      } else {
        if (hint) { hint.textContent = "test failed"; hint.style.color = "var(--pit)"; }
        toast("MQTT test failed: " + (r.error || "unknown"), true);
      }
    } catch (e) { toast("Test error: " + e.message, true); }
    finally { btn.disabled = false; }
  });

  // Notifications: send a test push without saving.
  $("ntTest").addEventListener("click", async () => {
    const btn = $("ntTest");
    btn.disabled = true;
    try {
      const r = await postJSON("api/notify/test", notifyBodyFromForm());
      if (r.ok) toast("Test notification sent.");
      else toast("Notify test failed: " + (r.error || "unknown"), true);
    } catch (e) { toast("Test error: " + e.message, true); }
    finally { btn.disabled = false; }
  });

  $("preset1").addEventListener("change", (e) => { if (e.target.value) $("tgt1").value = e.target.value; });
  $("preset2").addEventListener("change", (e) => { if (e.target.value) $("tgt2").value = e.target.value; });
  $("preset3").addEventListener("change", (e) => { if (e.target.value) $("tgt3").value = e.target.value; });

  // Toggle: treat the 4th probe (Ambient) as a food probe with a target.
  // Persisted in localStorage (display preference, no board state). Re-render so
  // the dashboard tile and settings row reflect the change immediately.
  if ($("ambAsFood")) {
    $("ambAsFood").addEventListener("change", (e) => {
      localStorage.setItem(AMBIENT_AS_FOOD_KEY, e.target.checked ? "1" : "0");
      if (lastState) { resolveFoodTargets(lastState); applyState(lastState); }
      populateSettings();
    });
  }

  // Editing any PID number by hand means the tuning no longer matches the
  // selected preset, so flip the selector to "Custom" (value ""). Preset
  // selection fills these fields programmatically via .value =, which does not
  // fire "input", so this only reacts to the user typing.
  for (const id of ["pidb", "pidp", "pidi", "pidd"]) {
    $(id).addEventListener("input", () => {
      const pp = $("pidPreset");
      if (pp.value !== "") { pp.value = ""; $("pidPresetNote").textContent = ""; }
    });
  }

  document.querySelectorAll(".savebtn[data-save]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.save;
      const saver = SAVERS[key];
      if (typeof saver !== "function") {
        // This only happens if the served app.js is out of sync with the HTML
        // (e.g. a stale cached copy). Tell the user how to recover.
        setMsg("This page is out of date - please hard-refresh (Cmd/Ctrl+Shift+R).", true);
        return;
      }
      btn.disabled = true;
      try { await saver(); setTimeout(refreshSettings, 1200); }
      catch (e) { setMsg("Save failed: " + e.message, true); }
      finally { btn.disabled = false; }
    });
  });

  $("atStart").addEventListener("click", startAutotune);
  $("atCancel").addEventListener("click", cancelAutotune);

  // PID internals toggle: persists in localStorage and re-asserts on connect.
  const pidTog = $("pidInternalsToggle");
  pidTog.checked = localStorage.getItem("pidInternals") === "1";
  pidTog.addEventListener("change", async () => {
    localStorage.setItem("pidInternals", pidTog.checked ? "1" : "0");
    try { await postJSON("api/pid-internals", { enabled: pidTog.checked }); }
    catch (e) { setMsg("Failed: " + e.message, true); }
    if (!pidTog.checked) $("pidCard").hidden = true;
  });
}

async function applyPidInternalsPref() {
  // On load/reconnect, tell the board whether to stream $HMPS to match the pref.
  const on = localStorage.getItem("pidInternals") === "1";
  if (on) { try { await postJSON("api/pid-internals", { enabled: true }); } catch (e) {} }
}

// -- auto-tune --------------------------------------------------------------

async function startAutotune() {
  const setpoint = numVal("atSetpoint");
  if (setpoint == null) { setMsg("Enter a target temp.", true); return; }
  try {
    await postJSON("api/autotune", {
      setpoint, rule: $("atRule").value,
      max_cycles: parseInt($("atCycles").value, 10) || 4,
    });
    toast("Auto-tune started. Supervise the cooker.");
    pollAutotune();
  } catch (e) { setMsg("Auto-tune failed to start: " + e.message, true); }
}
async function cancelAutotune() {
  try { await postJSON("api/autotune", undefined, "DELETE"); } catch (e) {}
  pollAutotune();
}
function renderAutotune(running, st) {
  const active = running && !st.done && st.phase === "running";
  $("atStart").style.display = active ? "none" : "";
  $("atCancel").style.display = active ? "" : "none";
  const s = $("atStatus");
  if (!st || st.phase === "idle") { s.textContent = ""; return; }
  if (st.phase === "running") {
    s.textContent = `Tuning… cycle ${st.cycles || 0}/${st.max_cycles || "?"}` +
      (st.amplitude ? `, swing ${st.amplitude.toFixed(1)}°` : "");
  } else if (st.phase === "done") {
    if (st.result) {
      const r = st.result;
      s.textContent = `Done: p=${r.kp.toFixed(2)} i=${r.ki.toFixed(4)} d=${r.kd.toFixed(2)} (${r.cycles} cycles)`;
    } else { s.textContent = "Stopped: " + (st.error || "no result"); }
  }
}
async function pollAutotune() {
  try {
    const st = await getJSON("api/autotune");
    renderAutotune(st.phase === "running", st);
    if (st.phase === "running") setTimeout(pollAutotune, 4000);
  } catch (e) {}
}

// -- probe rename (click a probe name) --------------------------------------

let renameIdx = null;
function openRename(idx) {
  renameIdx = idx;
  const names = (lastState && lastState.probe_names) || [];
  const labels = ["Pit", "Food 1", "Food 2", "Ambient"];
  $("renameTitle").textContent = "Rename " + (names[idx] || labels[idx]);
  $("renameText").value = names[idx] || "";
  $("renameOverlay").hidden = false; $("renameDialog").hidden = false;
  $("renameText").focus(); $("renameText").select();
}
function closeRename() { $("renameOverlay").hidden = true; $("renameDialog").hidden = true; }
async function saveRename() {
  const name = $("renameText").value.trim();
  if (name && renameIdx != null) {
    try { await postJSON("api/probe-name", { index: renameIdx, name }); toast("Renamed."); }
    catch (e) { toast("Rename failed: " + e.message, true); }
  }
  closeRename();
}
function wireRename() {
  document.querySelectorAll(".probe-name").forEach((elx) => {
    elx.style.cursor = "pointer";
    elx.addEventListener("click", () => openRename(parseInt(elx.dataset.probe, 10)));
  });
  $("renameCancel").addEventListener("click", closeRename);
  $("renameOverlay").addEventListener("click", closeRename);
  $("renameSave").addEventListener("click", saveRename);
  $("renameText").addEventListener("keydown", (e) => { if (e.key === "Enter") saveRename(); });
}

// -- food target (quick-set with presets) -----------------------------------

let targetChannel = null;   // "food1" | "food2" | "ambient"
function openTarget(channel) {
  targetChannel = channel;
  const idx = (FOOD_META[channel] || {}).idx ?? 1;
  const names = (lastState && lastState.probe_names) || [];
  $("targetTitle").textContent = "Target: " + (names[idx] || channel);
  $("targetUnit").textContent = "°" + unit;
  $("targetInput").value = foodTargets[channel] ?? "";
  // Render preset quick-picks.
  const wrap = $("targetPicks");
  clear(wrap);
  const meat = (presetsCache && presetsCache.meat) || [];
  for (const p of meat) {
    const b = el("button", { text: `${p.label} ${p.temp_f}°`, title: p.note || "" });
    b.addEventListener("click", () => { $("targetInput").value = p.temp_f; });
    wrap.appendChild(b);
  }
  $("targetOverlay").hidden = false; $("targetDialog").hidden = false;
  $("targetInput").focus();
}
function closeTarget() { $("targetOverlay").hidden = true; $("targetDialog").hidden = true; }
async function saveTarget(clearIt) {
  if (!targetChannel) { closeTarget(); return; }
  // Target is the probe's HIGH alarm slot (food1->3, food2->5, ambient->7).
  const hiIndex = (FOOD_META[targetChannel] || {}).hi ?? 3;
  const val = clearIt ? -1 : numVal("targetInput");
  if (!clearIt && val == null) { closeTarget(); return; }
  const thresholds = [null, null, null, null, null, null, null, null];
  thresholds[hiIndex] = val;
  try {
    await postJSON("api/alarms", { thresholds });
    toast(clearIt ? "Target cleared." : `Target set to ${val}°.`);
  } catch (e) { toast("Target failed: " + e.message, true); }
  closeTarget();
}
async function ensurePresetsCache() {
  if (!presetsCache) {
    try { presetsCache = await getJSON("api/presets"); } catch (e) { presetsCache = {}; }
  }
}
function wireTargets() {
  document.querySelectorAll(".targetbtn[data-target]").forEach((btn) => {
    btn.addEventListener("click", async () => { await ensurePresetsCache(); openTarget(btn.dataset.target); });
  });
  $("targetCancel").addEventListener("click", closeTarget);
  $("targetOverlay").addEventListener("click", closeTarget);
  $("targetClear").addEventListener("click", () => saveTarget(true));
  $("targetSave").addEventListener("click", () => saveTarget(false));
  $("targetInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveTarget(false); });
}

// -- sessions ---------------------------------------------------------------

async function openSessions() {
  $("sessionsOverlay").hidden = false; $("sessionsPanel").hidden = false;
  await loadSessionList();
}
function closeSessions() { $("sessionsOverlay").hidden = true; $("sessionsPanel").hidden = true; }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }

function sessionRow(s) {
  const live = s.ended_ts == null;
  const dur = s.ended_ts ? fmtDuration(s.ended_ts - s.started_ts) : "in progress";
  const name = el("span", { class: "session__name", contenteditable: "true",
    "data-id": s.id, text: s.name || ("Cook #" + s.id) });
  name.addEventListener("blur", async () => {
    try { await postJSON(`api/sessions/${s.id}`, { name: name.textContent.trim() }, "PATCH");
      setMsg("Renamed.", false, "sessionsMsg"); }
    catch (e) { setMsg("Rename failed", true, "sessionsMsg"); }
  });
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); name.blur(); } });

  const top = el("div", { class: "session__top" },
    [name, live ? el("span", { class: "hint", text: "LIVE" }) : null]);
  if (live) top.lastChild.style.color = "var(--food1)";
  const meta = el("div", { class: "session__meta",
    text: `${fmtTime(s.started_ts)} · ${dur} · ${s.sample_count} pts` });
  const viewBtn = el("button", { class: "linkbtn", text: "View" });
  viewBtn.addEventListener("click", () => viewSession(s.id));
  const exportLink = el("a", { class: "linkbtn", href: `api/export.csv?session_id=${s.id}`, text: "Export CSV" });
  const shareBtn = el("button", { class: "linkbtn", text: s.share_token ? "Sharing ✓" : "Share" });
  shareBtn.addEventListener("click", () => toggleShare(s, shareBtn));
  const delBtn = el("button", { class: "linkbtn linkbtn--danger", text: "Delete" });
  delBtn.addEventListener("click", () => deleteSession(s.id));
  const actions = el("div", { class: "session__actions" }, [viewBtn, exportLink, shareBtn, delBtn]);
  const row = el("div", { class: "session" + (live ? " active" : "") }, [top, meta, actions]);
  if (s.share_token) row.appendChild(shareLinkEl(s.share_token));
  return row;
}

function shareLinkEl(token) {
  const url = location.origin + "/share/" + token;
  const a = el("a", { class: "share-link", href: url, target: "_blank", text: url });
  return el("div", { class: "session__share" }, [el("span", { class: "hint", text: "Public link: " }), a]);
}

async function toggleShare(s, btn) {
  const enabling = !s.share_token;
  try {
    const r = await postJSON(`api/sessions/${s.id}/share`, { enabled: enabling });
    s.share_token = r.token || null;
    btn.textContent = s.share_token ? "Sharing ✓" : "Share";
    if (s.share_token) {
      const url = location.origin + "/share/" + s.share_token;
      try { await navigator.clipboard.writeText(url); toast("Share link copied to clipboard."); }
      catch (e) { toast("Sharing enabled."); }
      loadSessionList();
    } else { toast("Sharing disabled."); loadSessionList(); }
  } catch (e) { toast("Share failed: " + e.message, true); }
}

async function loadSessionList() {
  const search = $("sessionSearch").value.trim();
  let list;
  try { list = await getJSON("api/sessions" + (search ? `?search=${encodeURIComponent(search)}` : "")); }
  catch (e) { setMsg("Failed to load cooks", true, "sessionsMsg"); return; }
  const wrap = $("sessionList");
  clear(wrap);
  if (!list.length) { wrap.appendChild(el("p", { class: "hint", text: "No cooks yet." })); return; }
  for (const s of list) wrap.appendChild(sessionRow(s));
}

async function viewSession(id) {
  try {
    setData(await getJSON(`api/history?session_id=${id}&limit=10000`));
    try { notes = (await getJSON(`api/sessions/${id}`)).notes || []; } catch (e) { notes = []; }
    if (chart) chart.redraw();
    viewingSession = id; setConn(true); showLiveButton(); closeSessions();
    toast("Viewing past cook. Click 'Return to live' to resume.");
  } catch (e) { setMsg("Failed to load cook", true, "sessionsMsg"); }
}
function showLiveButton() {
  if ($("liveBtn")) return;
  const b = el("button", { id: "liveBtn", class: "spbtn spbtn--go", text: "Return to live" });
  b.style.cssText = "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:15;";
  b.addEventListener("click", () => {
    viewingSession = null; b.remove(); setConn(true);
    loadHistory(parseInt($("rangeSel").value, 10));
  });
  document.body.appendChild(b);
}
async function deleteSession(id) {
  if (!confirm("Delete this cook and all its data?")) return;
  try {
    await postJSON(`api/sessions/${id}`, undefined, "DELETE");
    if (viewingSession === id) {
      viewingSession = null;
      const lb = $("liveBtn"); if (lb) lb.remove();
      loadHistory(parseInt($("rangeSel").value, 10));
    }
    loadSessionList();
  } catch (e) { setMsg("Delete failed", true, "sessionsMsg"); }
}
function wireSessions() {
  $("sessionsBtn").addEventListener("click", openSessions);
  $("sessionsClose").addEventListener("click", closeSessions);
  $("sessionsOverlay").addEventListener("click", closeSessions);
  let searchTimer = null;
  $("sessionSearch").addEventListener("input", () => {
    clearTimeout(searchTimer); searchTimer = setTimeout(loadSessionList, 250);
  });
}

// -- note dialog ------------------------------------------------------------

function openNote() {
  $("noteOverlay").hidden = false; $("noteDialog").hidden = false;
  $("noteText").value = ""; if ($("notePhoto")) $("notePhoto").value = "";
  $("noteText").focus();
}
function closeNote() { $("noteOverlay").hidden = true; $("noteDialog").hidden = true; }
async function saveNote() {
  const text = $("noteText").value.trim();
  const file = $("notePhoto") && $("notePhoto").files[0];
  if (!text && !file) { closeNote(); return; }   // need text or a photo
  try {
    let photo_b64 = null;
    if (file) {
      try { photo_b64 = await fileToDataURL(file); }
      catch (e) { toast("Couldn't read that photo.", true); }
    }
    await postJSON("api/notes", { text: text || "(photo)", photo_b64 });
    notes = await getJSON("api/notes");
    renderNotesStrip();
    if (chart) chart.redraw();
    toast("Note added.");
  } catch (e) { toast("Note failed: " + e.message, true); }
  closeNote();
}
function wireNotes() {
  $("addNoteBtn").addEventListener("click", openNote);
  $("noteCancel").addEventListener("click", closeNote);
  $("noteOverlay").addEventListener("click", closeNote);
  $("noteSave").addEventListener("click", saveNote);
  $("noteText").addEventListener("keydown", (e) => { if (e.key === "Enter") saveNote(); });
}

function closeAll() { closeSettings(); closeSessions(); closeNote(); closeRename(); closeTarget(); closeProgram(); }

// -- main controls ----------------------------------------------------------

function renderPitPicks(pit) {
  const wrap = $("pitPicks");
  clear(wrap);
  for (const p of pit) {
    const b = el("button", { text: `${p.label} ${p.temp_f}°` });
    b.addEventListener("click", () => { $("spInput").value = p.temp_f; sendSetpoint(p.temp_f); });
    wrap.appendChild(b);
  }
}
function wireControls() {
  $("spSet").addEventListener("click", () => {
    const v = parseFloat($("spInput").value);
    if (!Number.isNaN(v)) sendSetpoint(v);
  });
  $("spUp").addEventListener("click", () => {
    const v = (parseFloat($("spInput").value) || 0) + 5; $("spInput").value = v; sendSetpoint(v);
  });
  $("spDown").addEventListener("click", () => {
    const v = (parseFloat($("spInput").value) || 0) - 5; $("spInput").value = v; sendSetpoint(v);
  });
  $("spInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("spSet").click(); });
  $("rangeSel").addEventListener("change", (e) => loadHistory(parseInt(e.target.value, 10)));
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  // Only register on a secure context (https or localhost); browsers block SWs
  // on plain http for non-localhost. On the Pi's http LAN address the SW won't
  // register, but everything else (live data, notifications via the page) still
  // works - the SW is a progressive enhancement.
  if (!window.isSecureContext && location.hostname !== "localhost") return;
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

async function init() {
  makeChart();
  registerServiceWorker();
  wireControls(); wireSettings(); wireSessions(); wireNotes();
  wireRename(); wireTargets(); wireProgram(); wireCompare();
  if ("Notification" in window && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch (e) {}
  }
  try { applyState(await getJSON("api/status")); } catch (e) {}
  try { renderPitPicks((await getJSON("api/presets")).pit); } catch (e) {}
  await loadHistory(parseInt($("rangeSel").value, 10));
  connectWs();
  pollPredictions();
  setInterval(pollPredictions, 20000);
  pollProgram();          // restore banner if a program is already running
  populateCompare();      // fill the compare dropdown
  applyPidInternalsPref(); // re-enable $HMPS streaming if the user had it on
}

init();
