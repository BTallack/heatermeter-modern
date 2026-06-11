<script>
  import { onMount, onDestroy } from 'svelte';
  import { getJSON, postJSON, delJSON, connectWs, authHeaders, setAuthToken } from './api.js';
  import { getTheme, setTheme } from './theme.js';

  // App passes onTheme so the sidebar/navbar icon updates when theme changes
  // here. showTitle is false when shown inside the desktop modal (whose header
  // already carries the title).
  let { onTheme, showTitle = true } = $props();

  // ---- toast pill (top of page) ---------------------------------------
  let toast = $state(null); // { msg, ok }
  let toastTimer = null;
  function flash(msg, ok = true) {
    toast = { msg, ok };
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = null), 2800);
  }

  // ---- loaded reference data ------------------------------------------
  let status = $state(null);        // /api/status
  let probePresets = $state({});    // key -> {label}
  let probeTypes = $state({});      // type number -> label (Disabled/Thermistor/...)
  let units = $state('F');          // board temperature unit (F/C)
  let ledStimuli = $state({});      // value -> label
  let homeModes = $state({});       // value -> label
  let ledInvertBit = $state(0x80);
  let pidPresets = $state([]);
  let blowerPresets = $state([]);

  // ---- editable form models -------------------------------------------
  let names = $state(['', '', '', '']);
  let offsets = $state(['', '', '', '']);
  let probeType = $state(['', '', '', '']); // per-probe pending dropdown

  let alarms = $state(['', '', '', '', '', '', '', '']); // low0,high0,...

  let pid = $state({ b: '', p: '', i: '', d: '' });
  let pidPresetSel = $state('');

  let tune = $state({ setpoint: 225, rule: 'tyreus_luyben' });
  let tuneStatus = $state(null);
  let tuneTimer = null;

  let fan = $state({
    fan_low: '', fan_high: '', max_startup: '', fan_active_floor: '',
    servo_min: '', servo_max: '', servo_active_ceil: '',
    invert_fan: false, invert_servo: false,
  });
  let blowerSel = $state('');

  let lid = $state({ offset_percent: '', duration_seconds: '', active: true });

  let lcd = $state({ backlight: 255, home_mode: 255, leds: [0, 0, 0, 0], inv: [false, false, false, false] });
  let homeRotate = $state(5);   // LCD probe rotation interval (seconds)

  let mqtt = $state({ enabled: false, host: '', port: 1883, username: '', password: '', node_id: 'hm', has_password: false, connected: false, last_error: null });
  let notify = $state({ enabled: false, server: 'https://ntfy.sh', topic: '', token: '', has_token: false, debounce_sec: 30, repeat_min: 0, dark_timeout_sec: 90 });
  let mqttBusy = $state(false);
  let notifyBusy = $state(false);

  let theme = $state(getTheme());

  // ---- helpers --------------------------------------------------------
  const numOrNull = (v) => (v === '' || v == null ? null : Number(v));
  const numOrUndef = (v) => (v === '' || v == null ? undefined : Number(v));

  function applyStatus(d) {
    status = d;
    names = (d.probe_names || ['', '', '', '']).slice(0, 4);
    while (names.length < 4) names.push('');
    offsets = (d.probe_offsets || ['', '', '', '']).map((x) => (x == null ? '' : String(x))).slice(0, 4);
    while (offsets.length < 4) offsets.push('');

    const al = d.alarms || [];
    alarms = Array.from({ length: 8 }, (_, i) => {
      const v = al[i];
      return v == null || v === '' || Number(v) < 0 ? '' : String(v);
    });

    const p = d.pid || {};
    pid = { b: p.b ?? '', p: p.p ?? '', i: p.i ?? '', d: p.d ?? '' };
    if (p.units) units = p.units;
    matchPidPreset();

    const f = d.fan || {};
    const flags = Number(f.flags || 0);
    fan = {
      fan_low: f.low ?? '', fan_high: f.high ?? '',
      max_startup: f.max_startup ?? '', fan_active_floor: f.fan_active_floor ?? '',
      servo_min: f.servo_min ?? '', servo_max: f.servo_max ?? '',
      servo_active_ceil: f.servo_active_ceil ?? '',
      invert_fan: !!(flags & 1), invert_servo: !!(flags & 2),
    };

    const ld = d.lid_detect || {};
    lid = {
      offset_percent: ld.offset_percent ?? '',
      duration_seconds: ld.duration ?? '',
      active: true,
    };

    const disp = d.display || {};
    const leds = (disp.leds || [0, 0, 0, 0]).slice(0, 4);
    while (leds.length < 4) leds.push(0);
    lcd = {
      backlight: disp.backlight ?? 255,
      home_mode: Number(disp.home_mode ?? 255),
      leds: leds.map((b) => Number(b) & 0x7f),
      inv: leds.map((b) => !!(Number(b) & ledInvertBit)),
    };
    syncProbeTypeSel();
  }

  // Pre-select each probe's dropdown to the preset actually configured on it
  // (persisted server-side, since the board only reports type+coeffs). If the
  // saved preset's type no longer matches the board (e.g. after an EEPROM
  // reset), fall back to the generic "current type" placeholder.
  function syncProbeTypeSel() {
    const sel = status?.probe_preset_sel || {};
    for (let i = 0; i < 4; i++) {
      const key = sel[i] ?? sel[String(i)] ?? '';
      const bt = status?.probe_coeffs?.[i]?.type;
      const boardType = (bt === undefined || bt === null || bt === '') ? null : Number(bt);
      if (!key) { probeType[i] = ''; continue; }
      if (key === '__off') { probeType[i] = (boardType === null || boardType === 0) ? '__off' : ''; continue; }
      const p = probePresets[key];
      probeType[i] = (p && boardType !== null && Number(p.type) !== boardType) ? '' : key;
    }
    probeType = [...probeType];
  }

  function matchPidPreset() {
    const cur = pidPresets.find(
      (pp) => Number(pp.p) === Number(pid.p) && Number(pp.i) === Number(pid.i) && Number(pp.d) === Number(pid.d)
    );
    pidPresetSel = cur ? cur.key : '';
  }
  function applyPidPreset() {
    const pp = pidPresets.find((x) => x.key === pidPresetSel);
    if (!pp) return;
    pid = { b: pp.b ?? '', p: pp.p, i: pp.i, d: pp.d };
  }
  function applyBlowerPreset() {
    const bp = blowerPresets.find((x) => x.key === blowerSel);
    if (!bp) return;
    fan = { ...fan, fan_low: bp.fan_low, fan_high: bp.fan_high, max_startup: bp.max_startup, fan_active_floor: bp.fan_active_floor };
  }

  // ---- loaders --------------------------------------------------------
  async function loadAll() {
    try {
      const [pp, lo] = await Promise.all([
        getJSON('probe-presets').catch(() => ({})),
        getJSON('lcd-options').catch(() => ({})),
      ]);
      probePresets = pp.presets || {};
      probeTypes = pp.types || {};
      ledStimuli = lo.led_stimuli || {};
      homeModes = lo.home_modes || {};
      ledInvertBit = lo.led_invert_bit || 0x80;
    } catch (_) {}
    try {
      const pr = await getJSON('presets');
      pidPresets = pr.pid || [];
      blowerPresets = pr.blower || [];
    } catch (_) {}
    try { applyStatus(await getJSON('status')); } catch (_) {}
    try { homeRotate = (await getJSON('home-rotate')).rotate_secs ?? 5; } catch (_) {}
    try { Object.assign(mqtt, await getJSON('mqtt')); mqtt.password = ''; } catch (_) {}
    try { Object.assign(notify, await getJSON('notify')); notify.token = ''; } catch (_) {}
    try { tuneStatus = await getJSON('autotune'); pollTuneIfRunning(); } catch (_) {}
  }

  async function refreshStatus() {
    try { applyStatus(await getJSON('status')); } catch (_) {}
  }

  // ---- savers ---------------------------------------------------------
  async function saveProbes() {
    try {
      // Pace these out: each name write triggers an EEPROM write on the board,
      // during which incoming serial can be dropped. Spacing the commands keeps
      // the next one from merging into the previous (the unchecksummed command
      // channel has no other guard). ~150ms covers a name's EEPROM write.
      const gap = () => new Promise((r) => setTimeout(r, 150));
      for (let i = 0; i < 4; i++) { await postJSON('probe-name', { index: i, name: names[i] || '' }); await gap(); }
      await postJSON('offsets', { offsets: offsets.map((x) => numOrNull(x)) });
      flash('Probes saved');
      refreshStatus();
    } catch (e) { flash('Probe save failed', false); }
  }
  async function setUnits(u) {
    if (u === units) return;
    if (!confirm(`Switch temperature units to ${u === 'C' ? 'Celsius' : 'Fahrenheit'}? Your setpoint, alarm targets, and probe offsets will be converted.`)) return;
    try {
      const r = await postJSON('units', { unit: u });
      if (r.ok === false) { flash(r.error || 'Unit change failed', false); return; }
      units = u;
      flash(`Units set to °${u}`);
      setTimeout(refreshStatus, 900);
    } catch (e) { flash('Unit change failed', false); }
  }
  function currentTypeLabel(i) {
    const t = status?.probe_coeffs?.[i]?.type;
    if (t === undefined || t === null || t === '') return 'unknown';
    return probeTypes[t] ?? ('type ' + t);
  }
  async function changeProbeType(i) {
    const sel = probeType[i];
    if (!sel) return;
    try {
      if (sel === '__off') await postJSON('probe-type', { index: i, disabled: true });
      else await postJSON('probe-type', { index: i, preset: sel });
      flash(`Probe ${i + 1} type updated`);
      // Keep the chosen preset shown in the dropdown; refreshStatus re-syncs it
      // from the persisted selection once the board echoes the new config.
      setTimeout(refreshStatus, 600);
    } catch (e) { flash('Type change failed', false); }
  }

  async function saveAlarms() {
    try {
      await postJSON('alarms', { thresholds: alarms.map((x) => numOrNull(x)) });
      flash('Alarms saved');
      refreshStatus();
    } catch (e) { flash('Alarm save failed', false); }
  }

  async function savePid() {
    try {
      await postJSON('pid', { b: numOrUndef(pid.b), p: numOrUndef(pid.p), i: numOrUndef(pid.i), d: numOrUndef(pid.d) });
      flash('PID saved');
      refreshStatus();
    } catch (e) { flash('PID save failed', false); }
  }

  async function startTune() {
    try {
      await postJSON('autotune', { setpoint: Number(tune.setpoint), rule: tune.rule });
      flash('Auto-tune started');
      tuneStatus = await getJSON('autotune');
      pollTuneIfRunning();
    } catch (e) { flash('Could not start auto-tune', false); }
  }
  async function cancelTune() {
    try { await delJSON('autotune'); flash('Auto-tune cancelled'); tuneStatus = await getJSON('autotune'); }
    catch (e) { flash('Cancel failed', false); }
  }
  function pollTuneIfRunning() {
    clearInterval(tuneTimer);
    if (tuneStatus && tuneStatus.phase === 'running') {
      tuneTimer = setInterval(async () => {
        try {
          tuneStatus = await getJSON('autotune');
          if (tuneStatus.phase !== 'running') { clearInterval(tuneTimer); refreshStatus(); }
        } catch (_) {}
      }, 4000);
    }
  }
  function applyTuneResult() {
    const r = tuneStatus && tuneStatus.result;
    if (!r) return;
    pid = { b: pid.b, p: r.p ?? r.kp ?? pid.p, i: r.i ?? r.ki ?? pid.i, d: r.d ?? r.kd ?? pid.d };
    matchPidPreset();
    flash('Loaded tuned values into PID fields - press Save PID to apply');
  }

  async function saveFan() {
    try {
      await postJSON('fan', {
        fan_low: numOrUndef(fan.fan_low), fan_high: numOrUndef(fan.fan_high),
        max_startup: numOrUndef(fan.max_startup), fan_active_floor: numOrUndef(fan.fan_active_floor),
        servo_min: numOrUndef(fan.servo_min), servo_max: numOrUndef(fan.servo_max),
        servo_active_ceil: numOrUndef(fan.servo_active_ceil),
        invert_fan: fan.invert_fan, invert_servo: fan.invert_servo,
      });
      flash('Blower & servo saved');
      refreshStatus();
    } catch (e) { flash('Blower save failed', false); }
  }

  async function saveLid() {
    try {
      await postJSON('lid', {
        offset_percent: numOrUndef(lid.offset_percent),
        duration_seconds: numOrUndef(lid.duration_seconds),
        active: lid.active ? 1 : 0,
      });
      flash('Lid detection saved');
      refreshStatus();
    } catch (e) { flash('Lid save failed', false); }
  }

  async function saveLcd() {
    try {
      const leds = lcd.leds.map((s, i) => (Number(s) & 0x7f) | (lcd.inv[i] ? ledInvertBit : 0));
      await postJSON('lcd', { backlight: Number(lcd.backlight), home_mode: Number(lcd.home_mode), leds });
      await postJSON('home-rotate', { seconds: Number(homeRotate) || 5 });
      flash('Display saved');
      refreshStatus();
    } catch (e) { flash('Display save failed', false); }
  }

  async function testMqtt() {
    mqttBusy = true;
    try {
      const r = await postJSON('mqtt/test', mqttBody());
      flash(r.ok ? 'MQTT connected OK' : ('MQTT: ' + (r.error || 'failed')), !!r.ok);
    } catch (e) { flash('MQTT test failed', false); }
    mqttBusy = false;
  }
  function mqttBody() {
    return {
      enabled: mqtt.enabled, host: mqtt.host.trim(), port: Number(mqtt.port) || 1883,
      username: mqtt.username, password: mqtt.password || null, node_id: mqtt.node_id || 'hm',
    };
  }
  async function saveMqtt() {
    mqttBusy = true;
    try {
      const r = await postJSON('mqtt', mqttBody());
      Object.assign(mqtt, r); mqtt.password = '';
      flash('Home Assistant settings saved');
    } catch (e) { flash('MQTT save failed', false); }
    mqttBusy = false;
  }

  function notifyBody() {
    return {
      enabled: notify.enabled, server: notify.server.trim() || 'https://ntfy.sh',
      topic: notify.topic.trim(), token: notify.token || null,
      debounce_sec: Number(notify.debounce_sec) || 0, repeat_min: Number(notify.repeat_min) || 0,
      dark_timeout_sec: Number(notify.dark_timeout_sec) || 0,
    };
  }
  async function testNotify() {
    notifyBusy = true;
    try {
      const r = await postJSON('notify/test', notifyBody());
      flash(r.ok ? 'Test notification sent' : ('Notify: ' + (r.error || 'failed')), !!r.ok);
    } catch (e) { flash('Notify test failed', false); }
    notifyBusy = false;
  }
  async function saveNotify() {
    notifyBusy = true;
    try {
      const r = await postJSON('notify', notifyBody());
      Object.assign(notify, r); notify.token = '';
      flash('Notifications saved');
    } catch (e) { flash('Notify save failed', false); }
    notifyBusy = false;
  }

  function chooseTheme(t) {
    theme = t;
    const dark = setTheme(t);
    onTheme && onTheme(t, dark);
  }

  // Desktop panel style: popup vs left/right sidebar (read by App.svelte via
  // the hm-prefs event, same pattern as the other appearance preferences).
  const _storedPanel = localStorage.getItem('hm.panelStyle');
  let panelStyle = $state(['modal', 'left', 'right'].includes(_storedPanel) ? _storedPanel : 'modal');
  function choosePanelStyle(v) {
    panelStyle = v;
    try { localStorage.setItem('hm.panelStyle', v); } catch (_) {}
    window.dispatchEvent(new Event('hm-prefs'));
  }

  // Temperature-reactive panel colors (read by Dashboard via the hm-prefs event).
  let heatColors = $state(localStorage.getItem('hm.heatColors') !== '0');
  function toggleHeatColors() {
    heatColors = !heatColors;
    try { localStorage.setItem('hm.heatColors', heatColors ? '1' : '0'); } catch (_) {}
    window.dispatchEvent(new Event('hm-prefs'));
  }

  // -- live state for PID internals + diagnostics ----------------------------
  let live = $state({});   // latest full state dict (status, rf_sources, adc_noise, log, pid_internals)
  let pidInternalsOn = $state(false);
  let wsStop;
  async function togglePidInternals() {
    pidInternalsOn = !pidInternalsOn;
    try { await postJSON('pid-internals', { enabled: pidInternalsOn }); flash(pidInternalsOn ? 'PID internals streaming' : 'PID internals off'); }
    catch (e) { flash('Could not toggle PID internals', false); }
  }
  // Sum of the PID component magnitudes, for drawing relative contribution bars.
  function pidPart(k) { const v = Number(live.pid_internals?.[k]); return Number.isFinite(v) ? v : 0; }
  const pidTotal = $derived(['b', 'p', 'i', 'd'].reduce((s, k) => s + Math.abs(pidPart(k)), 0) || 1);

  // -- firmware updater ------------------------------------------------------
  let fw = $state({ configured: false, current: null, current_clean: null, images: [], status: { state: 'idle' } });
  let fwSel = $state('');
  let fwModal = $state(false);
  let fwConfirm = $state(false);
  let fwAction = $state('flash');
  let fwShowLog = $state(false);
  const selectedImage = $derived((fw.images || []).find((i) => i.version === fwSel) || null);
  const fwBusy = $derived(fw.status?.state === 'flashing');
  function fwClean(v) { return (v || '').replace(/([0-9a-z])([A-Z])$/, '$1'); }
  function fwStepLabel(step) {
    return ({ start: 'Starting', verified: 'Image verified', spi_on: 'Enabling programmer',
      siggate: 'Controller detected', backup: 'Backing up current firmware',
      flashed: 'Writing firmware', spi_off: 'Finishing up' })[step] || step;
  }
  async function loadFirmware() {
    try { const d = await getJSON('firmware'); fw = d; if (!fwSel && d.images?.length) fwSel = d.images[0].version; }
    catch (_) {}
  }
  function applyFirmwareEvent(ev) {
    const prev = fw.status?.state;
    fw = { ...fw, status: ev };
    if (ev.state === 'success' && prev !== 'success') { flash('Firmware updated'); loadFirmware(); }
    else if (ev.state === 'error' && prev !== 'error') flash(ev.message || 'Firmware update failed', false);
  }
  function openFlash(action) { fwAction = action; fwConfirm = false; fwModal = true; }
  async function doFlash(action) {
    fwModal = false;
    const path = action === 'rollback' ? 'firmware/rollback' : 'firmware/flash';
    const body = action === 'rollback' ? { confirm: true } : { version: fwSel, confirm: true };
    try {
      const r = await fetch('/api/' + path, { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { flash(j.error || ('Update refused (HTTP ' + r.status + ')'), false); return; }
      fw = { ...fw, status: { state: 'flashing', action, steps: [],
        version: action === 'rollback' ? (fw.current_clean || 'previous') : fwSel } };
      flash('Flashing firmware. Keep the controller powered.');
    } catch (e) { flash('Update request failed', false); }
  }

  // -- host software update --------------------------------------------------
  let hu = $state({ configured: false, current: null,
    config: { manifest_url: '', auto_check: false }, available: null,
    status: { state: 'idle' } });
  let huUrl = $state('');
  let huChecking = $state(false);
  const huBusy = $derived(['downloading', 'applying'].includes(hu.status?.state));
  function huStepLabel(step) {
    return ({ start: 'Starting', verified: 'Download verified', extracted: 'Unpacked',
      backup: 'Backed up current version', swap: 'Installed new version',
      restart: 'Restarting', rollback: 'Rolling back' })[step] || step;
  }
  async function loadHostUpdate() {
    try { const d = await getJSON('host-update'); hu = d; huUrl = d.config?.manifest_url || ''; }
    catch (_) {}
  }
  async function saveHuConfig() {
    try {
      const r = await postJSON('host-update/config',
        { manifest_url: huUrl.trim(), auto_check: hu.config?.auto_check || false });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      hu = { ...hu, config: { manifest_url: r.manifest_url, auto_check: r.auto_check },
             configured: !!r.manifest_url };
      flash('Update channel saved');
    } catch (e) { flash('Save failed', false); }
  }
  async function checkHostUpdate() {
    huChecking = true;
    try {
      const r = await postJSON('host-update/check', {});
      if (r.ok === false) { flash(r.error || 'Check failed', false); }
      else {
        hu = { ...hu, available: { version: r.version, changelog: r.changelog,
                                   update_available: r.update_available } };
        flash(r.update_available ? ('Update available: ' + r.version)
                                 : 'You are on the latest version');
      }
    } catch (e) { flash('Check failed', false); }
    finally { huChecking = false; }
  }
  function applyHostUpdateEvent(ev) {
    const prev = hu.status?.state;
    hu = { ...hu, status: ev };
    if (ev.state === 'success' && prev !== 'success') {
      flash('Software updated' + (ev.version ? ' to ' + ev.version : '')); loadHostUpdate();
    } else if (ev.state === 'error' && prev !== 'error') {
      flash(ev.message || 'Update failed', false);
    }
  }
  async function dismissHostUpdate() {
    try { await postJSON('host-update/ack', {}); } catch (_) {}
    hu = { ...hu, status: { state: 'idle' } };
  }
  async function applyHostUpdate(action) {
    const v = hu.available?.version || 'the latest version';
    const msg = action === 'rollback'
      ? 'Roll back to the previously installed version? The app will restart and reconnect.'
      : `Update HeaterMeter to ${v}? It downloads the new build, restarts, and reconnects automatically. The board keeps cooking; monitoring pauses for a few seconds.`;
    if (!confirm(msg)) return;
    try {
      const r = await fetch('/api/host-update/apply', { method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ confirm: true, action }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { flash(j.error || ('Update refused (HTTP ' + r.status + ')'), false); return; }
      hu = { ...hu, status: { state: 'applying', action, steps: [],
                              version: action === 'rollback' ? 'previous' : hu.available?.version } };
      flash('Updating. The app will restart and reconnect shortly.');
    } catch (e) { flash('Update request failed', false); }
  }

  // -- cook completion (Meater-style) ---------------------------------------
  let cd = $state({ enabled: true, grace_secs: 180, on_complete: 'notify', keep_warm_temp: 150 });
  let cdMins = $state(3);
  async function loadCookdone() {
    try { cd = await getJSON('cook-done'); cdMins = Math.max(1, Math.round((cd.grace_secs || 180) / 60)); }
    catch (_) {}
  }
  async function saveCookdone() {
    try {
      const r = await postJSON('cook-done', {
        enabled: cd.enabled,
        grace_secs: Math.round((Number(cdMins) || 3) * 60),
        on_complete: cd.on_complete,
        keep_warm_temp: Number(cd.keep_warm_temp) || 150,
      });
      cd = r; cdMins = Math.max(1, Math.round((r.grace_secs || 180) / 60));
      flash('Cook completion saved');
    } catch (e) { flash('Save failed', false); }
  }

  // -- cooker profiles ---------------------------------------------------------
  let profiles = $state({ profiles: [], active: null });
  let profileName = $state('');
  async function loadProfiles() { try { profiles = await getJSON('profiles'); } catch (_) {} }
  async function saveProfileNow() {
    try {
      const r = await postJSON('profiles', { name: profileName.trim() });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      profiles = r; profileName = '';
      flash('Profile saved from current tuning');
    } catch (e) { flash('Save failed', false); }
  }
  async function applyProfile(name) {
    try {
      const r = await postJSON('profiles/apply', { name });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      profiles = r;
      flash(`Profile "${name}" sent to the cooker`);
      setTimeout(loadAll, 1500);
    } catch (e) { flash('Apply failed', false); }
  }
  async function deleteProfile(name) {
    if (!confirm(`Delete profile "${name}"?`)) return;
    try {
      const r = await delJSON('profiles/' + encodeURIComponent(name));
      if (r && r.ok !== false) profiles = r;
      flash('Profile deleted');
    } catch (e) { flash('Delete failed', false); }
  }

  // -- LCD toast message ------------------------------------------------------
  let lcdMsg1 = $state('');
  let lcdMsg2 = $state('');
  async function sendLcdMessage() {
    try {
      const r = await postJSON('lcd/message', { line1: lcdMsg1, line2: lcdMsg2 });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      flash('Message sent to the display');
      lcdMsg1 = ''; lcdMsg2 = '';
    } catch (e) { flash('Failed to send', false); }
  }

  // -- sensor alerts (probe health + stall) ---------------------------------
  let pw = $state({ enabled: true, dropout_secs: 20, stall_enabled: true });
  async function loadProbeWatch() {
    try { pw = { ...pw, ...(await getJSON('probe-watch')) }; } catch (_) {}
  }
  async function saveProbeWatch() {
    try {
      const r = await postJSON('probe-watch', {
        enabled: pw.enabled,
        dropout_secs: Math.max(2, Number(pw.dropout_secs) || 20),
        stall_enabled: pw.stall_enabled,
      });
      pw = { ...pw, ...r };
      flash('Sensor alerts saved');
    } catch (e) { flash('Save failed', false); }
  }

  // -- system power ---------------------------------------------------------
  let shuttingDown = $state(false);
  async function shutdownSystem() {
    if (!confirm('Shut down HeaterMeter? This idles the cooker and powers off the Pi. You will need to physically power it back on.')) return;
    try {
      const r = await fetch('/api/system/shutdown', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ confirm: true }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { flash(j.error || 'Shutdown refused', false); return; }
      shuttingDown = true;
      flash('Shutting down. The Pi will power off shortly.');
    } catch (e) { flash('Shutdown request failed', false); }
  }

  // -- backup & restore -----------------------------------------------------
  let backupSecrets = $state(false);
  async function downloadBackup() {
    try {
      const r = await fetch('/api/backup' + (backupSecrets ? '?include_secrets=1' : ''), { headers: authHeaders() });
      const j = await r.json();
      const blob = new Blob([JSON.stringify(j, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `heatermeter-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      flash('Backup downloaded');
    } catch (e) { flash('Backup failed', false); }
  }
  async function restoreBackup(ev) {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    if (!confirm('Restore this backup? It overwrites probe calibration, names, PID, alarms, fan, display, and MQTT/notify settings, and adds any saved cook programs.')) {
      ev.target.value = ''; return;
    }
    try {
      const data = JSON.parse(await file.text());
      const r = await postJSON('restore', { data });
      if (r.ok === false) flash(r.error || 'Restore failed', false);
      else { flash('Restored: ' + (r.applied || []).join(', ')); setTimeout(() => { loadAll(); loadCookdone(); }, 1300); }
    } catch (e) { flash('Restore failed (invalid file?)', false); }
    ev.target.value = '';
  }

  // -- storage / retention --------------------------------------------------
  let db = $state({ samples: 0, sessions: 0, size_bytes: 0, oldest_ts: null, retention_days: 0, downsample_days: 0 });
  async function loadDb() { try { db = await getJSON('db'); } catch (_) {} }
  function fmtMB(b) { return b ? (b / 1048576).toFixed(1) + ' MB' : '—'; }
  async function cleanupDb() {
    const msg = (Number(db.retention_days) ? `delete samples older than ${db.retention_days} days` : 'keep all samples')
      + (Number(db.downsample_days) ? ` and thin samples older than ${db.downsample_days} days to ~1/min` : '');
    if (!confirm(`Apply storage settings now? This will ${msg}.`)) return;
    try {
      const r = await postJSON('db/cleanup', { retention_days: Number(db.retention_days) || 0, downsample_days: Number(db.downsample_days) || 0 });
      db = r;
      flash(`Cleanup done: removed ${(r.pruned || 0) + (r.downsampled || 0)} samples`);
    } catch (e) { flash('Cleanup failed', false); }
  }

  // -- security (optional auth) ---------------------------------------------
  let authCfg = $state({ enabled: false });
  let authPw = $state('');
  let authNew = $state('');
  async function loadAuth() { try { authCfg = await getJSON('auth'); } catch (_) {} }
  async function saveAuthPassword() {
    if (!authNew || authNew.length < 4) { flash('Password must be at least 4 characters', false); return; }
    try {
      const r = await postJSON('auth/password', { password: authNew, current: authCfg.enabled ? authPw : undefined });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      if (r.token) setAuthToken(r.token);
      authPw = ''; authNew = ''; authCfg = { enabled: true };
      flash('Password set. Authentication is on.');
    } catch (e) { flash('Failed (wrong current password?)', false); }
  }
  async function disableAuth() {
    if (!confirm('Turn off the password? The dashboard and API will be open to anyone on your network.')) return;
    try {
      const r = await postJSON('auth/disable', { current: authPw });
      if (r.ok === false) { flash(r.error || 'Failed', false); return; }
      setAuthToken(''); authPw = ''; authCfg = { enabled: false };
      flash('Authentication disabled.');
    } catch (e) { flash('Failed (wrong password?)', false); }
  }


  // -- settings tabs ----------------------------------------------------------
  const SETTINGS_TABS = [
    { id: 'probes', label: 'Probes' },
    { id: 'cooker', label: 'Cooker' },
    { id: 'device', label: 'Device' },
    { id: 'connect', label: 'Connect' },
    { id: 'app', label: 'App' },
  ];
  const _storedTab = localStorage.getItem('hm.settingsTab');
  let sTab = $state(SETTINGS_TABS.some((t) => t.id === _storedTab) ? _storedTab : 'probes');
  let rootEl;   // container div; used to find the scroll parent on tab switch
  function setSettingsTab(id) {
    sTab = id;
    try { localStorage.setItem('hm.settingsTab', id); } catch (_) {}
    // Jump back to the top of the new tab. Desktop: the modal body is the
    // scroll container; mobile: the page itself scrolls.
    const sc = rootEl?.closest('.overflow-y-auto');
    if (sc) sc.scrollTo({ top: 0 });
    else window.scrollTo({ top: 0 });
  }

  onMount(() => {
    loadAll();
    loadAuth();
    loadCookdone();
    loadDb();
    loadFirmware();
    loadHostUpdate();
    loadProbeWatch();
    loadProfiles();
    getJSON('status').then((s) => (live = s)).catch(() => {});
    wsStop = connectWs((m) => {
      if (m.state) live = m.state;
      if (m.event?.type === 'firmware') applyFirmwareEvent(m.event);
      if (m.event?.type === 'hostupdate') applyHostUpdateEvent(m.event);
      if (m.event?.type === 'probe_event')
        flash(m.event.message, !['warning', 'critical'].includes(m.event.severity));
    });
  });
  onDestroy(() => { clearTimeout(toastTimer); clearInterval(tuneTimer); wsStop && wsStop(); });
</script>

<div class="px-4 pt-4 pb-28 lg:pb-10 max-w-xl lg:max-w-3xl mx-auto space-y-4" bind:this={rootEl}>
  {#if showTitle}<h1 class="text-2xl font-black tracking-tight px-1">Settings</h1>{/if}

  <!-- Settings tab bar -->
  <div class="sticky top-0 z-10 -mx-1 px-1 py-1.5 backdrop-blur bg-white/85 dark:bg-neutral-950/85 rounded-xl">
    <div class="flex gap-1.5 overflow-x-auto no-scrollbar">
      {#each SETTINGS_TABS as t}
        <button class={'px-3.5 py-1.5 rounded-full text-sm font-semibold whitespace-nowrap transition-colors ' +
                       (sTab === t.id ? 'bg-orange-600 text-white' : 'bg-black/5 dark:bg-white/10 opacity-80 hover:opacity-100')}
                onclick={() => setSettingsTab(t.id)}>{t.label}</button>
      {/each}
    </div>
  </div>

  {#if sTab === 'probes'}
  <!-- Probes -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Probes</summary>
    <div class="px-4 pb-4 space-y-4">
      <div class="space-y-3">
        {#each [0, 1, 2, 3] as i}
          <div class="grid grid-cols-[1fr_auto] gap-2 items-end">
            <div>
              <label class="block text-xs font-semibold opacity-60 mb-1">Probe {i + 1} name</label>
              <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" maxlength="22" bind:value={names[i]} placeholder={i === 0 ? 'Pit' : 'Food ' + i} />
            </div>
            <div>
              <label class="block text-xs font-semibold opacity-60 mb-1">Offset °</label>
              <input class="w-20 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" step="0.1" bind:value={offsets[i]} />
            </div>
          </div>
          <div>
            <label class="block text-xs font-semibold opacity-60 mb-1">Change probe {i + 1} type</label>
            <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={probeType[i]} onchange={() => changeProbeType(i)}>
              <option value="">Current: {currentTypeLabel(i)} (keep)</option>
              {#each Object.entries(probePresets) as [key, p]}
                <option value={key}>{p.label}</option>
              {/each}
              <option value="__off">Disable this probe</option>
            </select>
          </div>
        {/each}
      </div>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveProbes}>Save Names & Calibration</button>
    </div>
  </details>

  <!-- Alarms -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Temperature Alarms</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Leave a field blank to disable that alarm.</p>
      {#each [0, 1, 2, 3] as i}
        <div class="grid grid-cols-[1fr_1fr_1fr] gap-2 items-end">
          <div class="text-sm font-semibold self-center">{names[i] || (i === 0 ? 'Pit' : 'Probe ' + (i + 1))}</div>
          <div>
            <label class="block text-xs opacity-60 mb-1">Low °</label>
            <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={alarms[i * 2]} />
          </div>
          <div>
            <label class="block text-xs opacity-60 mb-1">High °</label>
            <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={alarms[i * 2 + 1]} />
          </div>
        </div>
      {/each}
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveAlarms}>Save Alarms</button>
    </div>
  </details>

  <!-- Sensor Alerts -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Sensor Alerts</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Warn when a probe that was reading suddenly disconnects or reads something impossible, so a yanked or failed probe never silently ends a cook. A pit-probe dropout mid-cook is sent as a high-priority alert.</p>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={pw.enabled} /> Alert on probe disconnect / sensor fault</label>
      <div>
        <label class="block text-xs opacity-60 mb-1">Disconnect confirmation (seconds)</label>
        <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" min="2" max="120" bind:value={pw.dropout_secs} />
        <p class="text-xs opacity-50 mt-1">How long a probe must read nothing before it counts as disconnected (filters brief glitches).</p>
      </div>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={pw.stall_enabled} /> Detect the stall (heads-up when a food probe plateaus, and when it breaks out)</label>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveProbeWatch}>Save Sensor Alerts</button>
    </div>
  </details>

  <!-- Temperature Units -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Temperature Units</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Switch the controller between Fahrenheit and Celsius. Your setpoint, alarm targets, and probe offsets are converted automatically, and Home Assistant follows the change.</p>
      <div class="flex gap-2">
        <button class={'flex-1 px-4 py-2 rounded-lg font-semibold ' + (units === 'F' ? 'bg-orange-600 text-white' : 'bg-neutral-200 dark:bg-neutral-800')} onclick={() => setUnits('F')}>Fahrenheit (°F)</button>
        <button class={'flex-1 px-4 py-2 rounded-lg font-semibold ' + (units === 'C' ? 'bg-orange-600 text-white' : 'bg-neutral-200 dark:bg-neutral-800')} onclick={() => setUnits('C')}>Celsius (°C)</button>
      </div>
    </div>
  </details>

  {/if}

  {#if sTab === 'cooker'}
  <!-- Cooker Profiles -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Cooker Profiles</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Save the current control tuning (PID, fan and servo, lid detection) under a name per grill, and switch with one tap when you move the controller.</p>
      {#if profiles.profiles?.length}
        <div class="space-y-2">
          {#each profiles.profiles as p}
            <div class="flex items-center gap-2">
              <span class="flex-1 text-sm font-semibold truncate">{p.name}
                {#if profiles.active === p.name}<span class="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-green-600/15 text-green-700 dark:text-green-400 ml-1">active</span>{/if}
              </span>
              <button class="text-xs px-2 py-1 rounded bg-orange-600 text-white" onclick={() => applyProfile(p.name)}>Apply</button>
              <button class="text-xs px-2 py-1 rounded text-red-500" onclick={() => deleteProfile(p.name)}>✕</button>
            </div>
          {/each}
        </div>
      {:else}
        <p class="text-xs opacity-50">No profiles yet. Tune the cooker, then save it here.</p>
      {/if}
      <div class="flex gap-2">
        <input class="flex-1 bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" maxlength="40" placeholder="Profile name (e.g. Kamado)" bind:value={profileName} />
        <button class="px-3 py-2 rounded-lg bg-neutral-700 text-white text-sm disabled:opacity-40" disabled={!profileName.trim()} onclick={saveProfileNow}>Save current</button>
      </div>
    </div>
  </details>

  <!-- PID -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">PID Tuning</summary>
    <div class="px-4 pb-4 space-y-3">
      <div>
        <label class="block text-xs font-semibold opacity-60 mb-1">Preset</label>
        <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={pidPresetSel} onchange={applyPidPreset}>
          <option value="">Custom</option>
          {#each pidPresets as pp}<option value={pp.key}>{pp.label}</option>{/each}
        </select>
        {#if pidPresetSel}
          <p class="text-xs opacity-60 mt-1">{pidPresets.find((x) => x.key === pidPresetSel)?.note}</p>
        {/if}
      </div>
      <div class="grid grid-cols-4 gap-2">
        {#each ['b', 'p', 'i', 'd'] as k}
          <div>
            <label class="block text-xs font-semibold opacity-60 mb-1 uppercase">{k}</label>
            <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" step="0.001" bind:value={pid[k]} oninput={() => (pidPresetSel = '')} />
          </div>
        {/each}
      </div>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={savePid}>Save PID</button>

      <div class="border-t border-black/10 dark:border-white/10 pt-3 space-y-2">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold">Live PID internals</h3>
          <label class="flex items-center gap-2 text-xs"><input type="checkbox" checked={pidInternalsOn} onchange={togglePidInternals} /> stream</label>
        </div>
        {#if pidInternalsOn}
          <p class="text-xs opacity-60">Relative contribution of each term to the current fan output.</p>
          {#each [['b', 'Bias', 'bg-neutral-400'], ['p', 'P', 'bg-orange-500'], ['i', 'I', 'bg-sky-500'], ['d', 'D', 'bg-green-500']] as [k, lbl, col]}
            <div class="flex items-center gap-2 text-xs">
              <span class="w-10 opacity-60">{lbl}</span>
              <div class="flex-1 h-3 rounded bg-black/5 dark:bg-white/10 overflow-hidden">
                <div class={'h-full ' + col} style={'width:' + Math.min(100, Math.abs(pidPart(k)) / pidTotal * 100) + '%'}></div>
              </div>
              <span class="w-14 text-right tabular-nums">{Number(live.pid_internals?.[k] ?? 0).toFixed(2)}</span>
            </div>
          {/each}
        {:else}
          <p class="text-xs opacity-60">Stream the live P/I/D term breakdown from the controller (diagnostic).</p>
        {/if}
      </div>

      <div class="border-t border-black/10 dark:border-white/10 pt-3 space-y-2">
        <h3 class="text-sm font-bold">Auto-tune</h3>
        <p class="text-xs opacity-60">Runs a relay-feedback test on the pit to derive P/I/D automatically. The cooker must be lit and stable.</p>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="block text-xs opacity-60 mb-1">Setpoint °</label>
            <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={tune.setpoint} />
          </div>
          <div>
            <label class="block text-xs opacity-60 mb-1">Rule</label>
            <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={tune.rule}>
              <option value="tyreus_luyben">Tyreus-Luyben (gentle)</option>
              <option value="ziegler_nichols">Ziegler-Nichols (aggressive)</option>
              <option value="no_overshoot">No overshoot (conservative)</option>
            </select>
          </div>
        </div>
        {#if tuneStatus && tuneStatus.phase && tuneStatus.phase !== 'idle'}
          <div class="text-sm bg-black/5 dark:bg-white/5 rounded-lg px-3 py-2">
            <div>Phase: <strong>{tuneStatus.phase}</strong>{#if tuneStatus.cycles != null} · cycle {tuneStatus.cycles}/{tuneStatus.max_cycles}{/if}</div>
            {#if tuneStatus.error}<div class="text-red-500">{tuneStatus.error}</div>{/if}
            {#if tuneStatus.result}
              <div class="mt-1">Result: P {tuneStatus.result.p ?? tuneStatus.result.kp} · I {tuneStatus.result.i ?? tuneStatus.result.ki} · D {tuneStatus.result.d ?? tuneStatus.result.kd}
                <button class="ml-2 underline text-orange-600" onclick={applyTuneResult}>Load into fields</button>
              </div>
            {/if}
          </div>
        {/if}
        <div class="flex gap-2">
          {#if tuneStatus && tuneStatus.phase === 'running'}
            <button class="flex-1 px-4 py-2 rounded-lg bg-red-600 text-white font-semibold" onclick={cancelTune}>Cancel auto-tune</button>
          {:else}
            <button class="flex-1 px-4 py-2 rounded-lg bg-neutral-700 text-white font-semibold" onclick={startTune}>Start auto-tune</button>
          {/if}
        </div>
      </div>
    </div>
  </details>

  <!-- Blower & servo -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Blower &amp; Servo</summary>
    <div class="px-4 pb-4 space-y-3">
      <div>
        <label class="block text-xs font-semibold opacity-60 mb-1">Blower preset</label>
        <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={blowerSel} onchange={applyBlowerPreset}>
          <option value="">Custom</option>
          {#each blowerPresets as bp}<option value={bp.key}>{bp.label}</option>{/each}
        </select>
        {#if blowerSel}<p class="text-xs opacity-60 mt-1">{blowerPresets.find((x) => x.key === blowerSel)?.note}</p>{/if}
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div><label class="block text-xs opacity-60 mb-1">Fan low %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.fan_low} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Fan high %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.fan_high} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Max startup %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.max_startup} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Fan active floor %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.fan_active_floor} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Servo min (10µs)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.servo_min} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Servo max (10µs)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.servo_max} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Servo active ceil %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={fan.servo_active_ceil} /></div>
      </div>
      <div class="flex gap-4 text-sm">
        <label class="flex items-center gap-2"><input type="checkbox" bind:checked={fan.invert_fan} /> Invert fan</label>
        <label class="flex items-center gap-2"><input type="checkbox" bind:checked={fan.invert_servo} /> Invert servo</label>
      </div>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveFan}>Save Blower &amp; Servo</button>
    </div>
  </details>

  <!-- Lid detection -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Lid Detection</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Pauses the fan when a sudden temp drop suggests the lid was opened.</p>
      <div class="grid grid-cols-2 gap-2">
        <div><label class="block text-xs opacity-60 mb-1">Trigger drop %</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={lid.offset_percent} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Max duration (s)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={lid.duration_seconds} /></div>
      </div>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={lid.active} /> Lid detection enabled</label>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveLid}>Save Lid Detection</button>
    </div>
  </details>

  <!-- Cook Completion -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Cook Completion</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Automatically mark a cook complete once a food probe reaches its target and is then removed for a few minutes. Moving the probe to another spot to check doneness will not end the cook. Needs a target set on the probe.</p>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={cd.enabled} /> Auto-complete cooks when the probe is removed</label>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="block text-xs opacity-60 mb-1">Confirmation delay (min)</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" min="1" max="30" bind:value={cdMins} />
        </div>
        <div>
          <label class="block text-xs opacity-60 mb-1">When complete</label>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={cd.on_complete}>
            <option value="notify">Notify only</option>
            <option value="shutdown">Shut cooker down</option>
            <option value="keep_warm">Drop to keep-warm</option>
          </select>
        </div>
      </div>
      {#if cd.on_complete === 'keep_warm'}
        <div>
          <label class="block text-xs opacity-60 mb-1">Keep-warm temp</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" min="100" max="300" bind:value={cd.keep_warm_temp} />
        </div>
      {/if}
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveCookdone}>Save Cook Completion</button>
    </div>
  </details>

  {/if}

  {#if sTab === 'device'}
  <!-- LCD & LEDs -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Display &amp; LEDs</summary>
    <div class="px-4 pb-4 space-y-3">
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="block text-xs opacity-60 mb-1">Backlight (0-100)</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" min="0" max="100" bind:value={lcd.backlight} />
        </div>
        <div>
          <label class="block text-xs opacity-60 mb-1">Home screen</label>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={lcd.home_mode}>
            {#each Object.entries(homeModes) as [v, label]}<option value={Number(v)}>{label}</option>{/each}
          </select>
        </div>
        <div>
          <label class="block text-xs opacity-60 mb-1">Probe rotation (s)</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" min="1" max="60" bind:value={homeRotate} />
        </div>
      </div>
      <div class="space-y-2">
        <div class="text-xs font-semibold opacity-60">LED triggers</div>
        {#each [0, 1, 2, 3] as i}
          <div class="grid grid-cols-[auto_1fr_auto] gap-2 items-center">
            <span class="text-sm w-12">LED {i + 1}</span>
            <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={lcd.leds[i]}>
              {#each Object.entries(ledStimuli) as [v, label]}<option value={Number(v)}>{label}</option>{/each}
            </select>
            <label class="flex items-center gap-1 text-xs"><input type="checkbox" bind:checked={lcd.inv[i]} /> invert</label>
          </div>
        {/each}
      </div>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={saveLcd}>Save Display</button>

      <div class="pt-2 border-t border-black/10 dark:border-white/10">
        <label class="block text-xs opacity-60 mb-1">Send a message to the display</label>
        <div class="grid grid-cols-2 gap-2">
          <input class="bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" maxlength="16" placeholder="Line 1" bind:value={lcdMsg1} />
          <input class="bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" maxlength="16" placeholder="Line 2 (optional)" bind:value={lcdMsg2} />
        </div>
        <button class="mt-2 px-4 py-2 rounded-lg bg-neutral-700 text-white text-sm w-full disabled:opacity-40" disabled={!lcdMsg1.trim()} onclick={sendLcdMessage}>Show on display</button>
        <p class="text-xs opacity-50 mt-1">Flashes briefly on the controller's LCD (16 characters per line).</p>
      </div>
    </div>
  </details>

  <!-- Firmware -->
  {#if fw.configured}
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Firmware</summary>
    <div class="px-4 pb-4 space-y-3">
      <div class="text-sm">Controller firmware:
        <span class="font-display">{fw.current_clean || fw.current || 'unknown'}</span>
      </div>
      {#if fw.error}
        <p class="text-xs text-red-500">Firmware list unavailable: {fw.error}</p>
      {/if}

      {#if fwBusy}
        <div class="bg-black/5 dark:bg-white/5 rounded-lg px-3 py-2 space-y-2">
          <div class="flex items-center gap-2 text-sm font-semibold">
            <span class="inline-block w-3 h-3 rounded-full bg-orange-500 animate-pulse"></span>
            Updating firmware{#if fw.status.version}{' to ' + fwClean(fw.status.version)}{/if}…
          </div>
          <p class="text-xs opacity-70">The display goes blank for up to a minute. Do not power off the controller or the Pi.</p>
          {#if (fw.status.steps || []).length}
            <ol class="text-xs space-y-0.5">
              {#each fw.status.steps as s}<li class="opacity-80">• {fwStepLabel(s.step)}</li>{/each}
            </ol>
          {/if}
        </div>
      {:else}
        <div>
          <label class="block text-xs opacity-60 mb-1">Available firmware</label>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={fwSel}>
            {#each fw.images as img}
              <option value={img.version}>{img.version}{img.installed ? ' (installed)' : ''}</option>
            {/each}
          </select>
        </div>
        {#if selectedImage?.changelog}<p class="text-xs opacity-70">{selectedImage.changelog}</p>{/if}
        {#if selectedImage?.eeprom_reset}
          <p class="text-xs text-amber-600 dark:text-amber-400">This version resets the controller to defaults. Your calibration, names, PID, and alarms are saved now and restored automatically.</p>
        {/if}
        <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full disabled:opacity-40"
                disabled={!fwSel || selectedImage?.installed} onclick={() => openFlash('flash')}>
          {selectedImage?.installed ? 'Already installed' : 'Update firmware'}
        </button>

        {#if fw.status?.state === 'success'}
          <div class="text-xs bg-green-600/10 text-green-700 dark:text-green-400 rounded-lg px-3 py-2">
            Updated{#if fw.status.read_version}{' to ' + fwClean(fw.status.read_version)}{/if}.{#if fw.status.verified === false}{' Verifying…'}{/if}
          </div>
          <button class="px-4 py-2 rounded-lg bg-neutral-700 text-white text-sm w-full" onclick={() => openFlash('rollback')}>Roll back to previous</button>
        {:else if fw.status?.state === 'error'}
          <div class="text-xs bg-red-600/10 text-red-600 dark:text-red-400 rounded-lg px-3 py-2">{fw.status.message || 'The last update failed.'}</div>
          <div class="flex gap-2">
            <button class="flex-1 px-3 py-2 rounded-lg bg-neutral-700 text-white text-xs" onclick={() => (fwShowLog = !fwShowLog)}>{fwShowLog ? 'Hide' : 'View'} steps</button>
            <button class="flex-1 px-3 py-2 rounded-lg bg-neutral-700 text-white text-xs" onclick={() => openFlash('rollback')}>Roll back</button>
          </div>
          {#if fwShowLog}
            <pre class="text-[10px] bg-black/40 text-neutral-200 rounded p-2 overflow-x-auto whitespace-pre-wrap">{(fw.status.steps || []).map((s) => fwStepLabel(s.step) + (s.msg && s.msg !== s.step ? ': ' + s.msg : '')).join('\n')}</pre>
          {/if}
        {/if}
      {/if}
    </div>
  </details>
  {/if}

  <!-- Software Update (host app) -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Software Update</summary>
    <div class="px-4 pb-4 space-y-3">
      <div class="text-sm">App version:
        <span class="font-display">{hu.current || 'unknown'}</span>
      </div>

      {#if huBusy}
        <div class="bg-black/5 dark:bg-white/5 rounded-lg px-3 py-2 space-y-2">
          <div class="flex items-center gap-2 text-sm font-semibold">
            <span class="inline-block w-3 h-3 rounded-full bg-orange-500 animate-pulse"></span>
            {hu.status.state === 'downloading' ? 'Downloading update' : 'Installing update'}{#if hu.status.version && hu.status.version !== 'previous'}{' ' + hu.status.version}{/if}…
          </div>
          <p class="text-xs opacity-70">The app will restart and reconnect on its own. The controller keeps running.</p>
          {#if (hu.status.steps || []).length}
            <ol class="text-xs space-y-0.5">
              {#each hu.status.steps as s}<li class="opacity-80">• {huStepLabel(s.step)}</li>{/each}
            </ol>
          {/if}
        </div>
      {:else}
        {#if hu.status?.state === 'success'}
          <div class="flex items-center justify-between gap-2 text-xs bg-green-600/10 text-green-700 dark:text-green-400 rounded-lg px-3 py-2">
            <span>Updated{#if hu.status.version}{' to ' + hu.status.version}{/if}.</span>
            <button class="underline shrink-0" onclick={dismissHostUpdate}>Dismiss</button>
          </div>
        {:else if hu.status?.state === 'error'}
          <div class="flex items-center justify-between gap-2 text-xs bg-red-600/10 text-red-600 dark:text-red-400 rounded-lg px-3 py-2">
            <span>{hu.status.message || 'The last update failed.'}</span>
            <button class="underline shrink-0" onclick={dismissHostUpdate}>Dismiss</button>
          </div>
        {/if}

        <div>
          <label class="block text-xs opacity-60 mb-1">Update channel (manifest URL)</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 text-sm" placeholder="https://…/update.json" bind:value={huUrl} />
          <p class="text-xs opacity-50 mt-1">Point this at your release manifest. Leave blank to disable updates. Builds are verified by SHA-256 before they are installed, and a bad build is rolled back automatically.</p>
        </div>
        <div class="flex gap-2">
          <button class="flex-1 px-3 py-2 rounded-lg bg-neutral-700 text-white text-sm" onclick={saveHuConfig}>Save channel</button>
          <button class="flex-1 px-3 py-2 rounded-lg bg-neutral-700 text-white text-sm disabled:opacity-40" disabled={!hu.configured || huChecking} onclick={checkHostUpdate}>{huChecking ? 'Checking…' : 'Check now'}</button>
        </div>

        {#if hu.available}
          {#if hu.available.update_available}
            <div class="bg-amber-500/10 rounded-lg px-3 py-2 space-y-1">
              <div class="text-sm font-semibold">Update available: {hu.available.version}</div>
              {#if hu.available.changelog}<p class="text-xs opacity-70 whitespace-pre-wrap">{hu.available.changelog}</p>{/if}
            </div>
            <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={() => applyHostUpdate('update')}>Update to {hu.available.version}</button>
          {:else}
            <p class="text-xs opacity-60">You are on the latest version ({hu.available.version}).</p>
          {/if}
        {/if}

        <button class="px-3 py-2 rounded-lg bg-neutral-700/70 text-white text-xs w-full" onclick={() => applyHostUpdate('rollback')}>Roll back to previous version</button>
      {/if}
    </div>
  </details>

  <!-- Storage -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Storage</summary>
    <div class="px-4 pb-4 space-y-3">
      <div class="grid grid-cols-3 gap-2 text-sm tabular-nums">
        <div><span class="opacity-50 block text-xs">Samples</span><b>{(db.samples || 0).toLocaleString()}</b></div>
        <div><span class="opacity-50 block text-xs">Cooks</span><b>{db.sessions || 0}</b></div>
        <div><span class="opacity-50 block text-xs">DB size</span><b>{fmtMB(db.size_bytes)}</b></div>
      </div>
      <p class="text-xs opacity-60">The controller logs a reading every second, so the database grows over time. Set a retention policy to keep it bounded.</p>
      <div class="grid grid-cols-2 gap-2">
        <div>
          <label class="block text-xs opacity-60 mb-1">Delete samples older than</label>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={db.retention_days}>
            <option value={0}>Keep all</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
            <option value={180}>180 days</option>
            <option value={365}>1 year</option>
          </select>
        </div>
        <div>
          <label class="block text-xs opacity-60 mb-1">Thin to ~1/min after</label>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={db.downsample_days}>
            <option value={0}>Off</option>
            <option value={7}>7 days</option>
            <option value={14}>14 days</option>
            <option value={30}>30 days</option>
          </select>
        </div>
      </div>
      <button class="px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold w-full" onclick={cleanupDb}>Save &amp; clean up now</button>
    </div>
  </details>

  <!-- System -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">System</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Gracefully idle the cooker and power off the Raspberry Pi. Use this before unplugging so the system shuts down cleanly. You will need to physically power it back on.</p>
      {#if shuttingDown}
        <div class="text-sm bg-black/5 dark:bg-white/5 rounded-lg px-3 py-2">Shutting down. The Pi will power off in a few seconds.</div>
      {:else}
        <button class="px-4 py-2 rounded-lg bg-red-600 text-white font-semibold w-full" onclick={shutdownSystem}>Shut down HeaterMeter</button>
      {/if}
    </div>
  </details>

  <!-- Diagnostics -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Diagnostics</summary>
    <div class="px-4 pb-4 space-y-4 text-sm">
      <div>
        <div class="text-xs font-semibold opacity-60 mb-1">RF wireless probes</div>
        {#if live.rf_sources && live.rf_sources.length}
          {#each live.rf_sources as r}
            <div class="flex items-center gap-3 py-1 tabular-nums">
              <span class="font-semibold">Node {r.node}</span>
              <span class="opacity-60">RSSI {r.rssi}</span>
              {#if r.low_battery}<span class="text-red-500">low battery</span>{/if}
              {#if r.recent_reset}<span class="text-yellow-500">reset</span>{/if}
              {#if r.native}<span class="opacity-50">native</span>{/if}
            </div>
          {/each}
        {:else}
          <div class="opacity-50 text-xs">No RF transmitters detected.</div>
        {/if}
      </div>

      <div>
        <div class="text-xs font-semibold opacity-60 mb-1">ADC noise (per probe)</div>
        {#if live.adc_noise && live.adc_noise.length}
          <div class="tabular-nums text-xs opacity-80">{live.adc_noise.join(' · ')}</div>
        {:else}
          <div class="opacity-50 text-xs">No ADC data.</div>
        {/if}
      </div>

      <div>
        <div class="text-xs font-semibold opacity-60 mb-1">Recent device log</div>
        {#if live.log && live.log.length}
          <div class="rounded-lg bg-black/5 dark:bg-white/5 p-2 max-h-40 overflow-y-auto font-mono text-[11px] leading-relaxed">
            {#each live.log.slice(-30) as line}<div class="truncate">{line}</div>{/each}
          </div>
        {:else}
          <div class="opacity-50 text-xs">No recent log messages.</div>
        {/if}
      </div>
    </div>
  </details>

  {/if}

  {#if sTab === 'connect'}
  <!-- Home Assistant (MQTT) -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Home Assistant (MQTT)</summary>
    <div class="px-4 pb-4 space-y-3">
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={mqtt.enabled} /> Publish to MQTT (auto-discovery)</label>
      {#if mqtt.connected}<div class="text-xs text-green-600 font-semibold">Connected</div>{:else if mqtt.last_error}<div class="text-xs text-red-500">{mqtt.last_error}</div>{/if}
      <div class="grid grid-cols-2 gap-2">
        <div class="col-span-2"><label class="block text-xs opacity-60 mb-1">Broker host</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={mqtt.host} placeholder="192.168.1.10" /></div>
        <div><label class="block text-xs opacity-60 mb-1">Port</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2 nums" type="number" bind:value={mqtt.port} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Node ID</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={mqtt.node_id} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Username</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={mqtt.username} autocomplete="off" /></div>
        <div><label class="block text-xs opacity-60 mb-1">Password</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" type="password" bind:value={mqtt.password} placeholder={mqtt.has_password ? '•••••• (unchanged)' : ''} autocomplete="new-password" /></div>
      </div>
      <div class="flex gap-2">
        <button class="flex-1 px-4 py-2 rounded-lg bg-neutral-700 text-white font-semibold disabled:opacity-50" disabled={mqttBusy} onclick={testMqtt}>Test Connection</button>
        <button class="flex-1 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold disabled:opacity-50" disabled={mqttBusy} onclick={saveMqtt}>Save</button>
      </div>
    </div>
  </details>

  <!-- Notifications -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Notifications (ntfy)</summary>
    <div class="px-4 pb-4 space-y-3">
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={notify.enabled} /> Send push alerts via ntfy</label>
      <div class="grid grid-cols-2 gap-2">
        <div class="col-span-2"><label class="block text-xs opacity-60 mb-1">Server</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={notify.server} placeholder="https://ntfy.sh" /></div>
        <div class="col-span-2"><label class="block text-xs opacity-60 mb-1">Topic</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" bind:value={notify.topic} placeholder="my-heatermeter" /></div>
        <div class="col-span-2"><label class="block text-xs opacity-60 mb-1">Access token (optional)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-3 py-2" type="password" bind:value={notify.token} placeholder={notify.has_token ? '•••••• (unchanged)' : ''} autocomplete="new-password" /></div>
        <div><label class="block text-xs opacity-60 mb-1">Debounce (s)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={notify.debounce_sec} /></div>
        <div><label class="block text-xs opacity-60 mb-1">Repeat every (min)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={notify.repeat_min} /></div>
        <div class="col-span-2"><label class="block text-xs opacity-60 mb-1">Device-dark failsafe (s)</label><input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2 nums" type="number" bind:value={notify.dark_timeout_sec} /><p class="text-xs opacity-50 mt-1">Alert if the board stops reporting for this long. 0 = off.</p></div>
      </div>
      <div class="flex gap-2">
        <button class="flex-1 px-4 py-2 rounded-lg bg-neutral-700 text-white font-semibold disabled:opacity-50" disabled={notifyBusy} onclick={testNotify}>Send Test</button>
        <button class="flex-1 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold disabled:opacity-50" disabled={notifyBusy} onclick={saveNotify}>Save</button>
      </div>
    </div>
  </details>

  {/if}

  {#if sTab === 'app'}
  <!-- Appearance -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Appearance</summary>
    <div class="px-4 pb-4 space-y-2">
      <div class="text-xs font-semibold opacity-60">Theme</div>
      <div class="flex gap-2">
        {#each [['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']] as [val, label]}
          <button
            class={'flex-1 px-4 py-2 rounded-lg font-semibold ' + (theme === val ? 'bg-orange-600 text-white' : 'bg-neutral-200 dark:bg-neutral-800')}
            onclick={() => chooseTheme(val)}>{label}</button>
        {/each}
      </div>
      <div class="text-xs font-semibold opacity-60 mt-4">Cook and Settings panels (larger screens)</div>
      <div class="flex gap-2">
        {#each [['modal', 'Popup'], ['left', 'Left sidebar'], ['right', 'Right sidebar']] as [val, label]}
          <button
            class={'flex-1 px-4 py-2 rounded-lg font-semibold ' + (panelStyle === val ? 'bg-orange-600 text-white' : 'bg-neutral-200 dark:bg-neutral-800')}
            onclick={() => choosePanelStyle(val)}>{label}</button>
        {/each}
      </div>
      <p class="text-xs opacity-50">A centered popup, or a panel that slides out from the side. Phones always use full-screen tabs.</p>
      <label class="flex items-center gap-2 text-sm mt-4">
        <input type="checkbox" checked={heatColors} onchange={toggleHeatColors} />
        Color temperature panels by progress to target
      </label>
      <p class="text-xs opacity-50">Warms each panel as it nears its target (the pit nears its setpoint), turning green once reached.</p>
    </div>
  </details>

  <!-- Security -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold flex items-center gap-2">
      <span>Security</span>
      <span class={'text-[10px] font-semibold px-1.5 py-0.5 rounded-full ' + (authCfg.enabled ? 'bg-green-600/15 text-green-700 dark:text-green-400' : 'bg-neutral-500/15 opacity-70')}>{authCfg.enabled ? 'Password on' : 'Open'}</span>
    </summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Protect the dashboard and API with a password. Off by default, so anyone on your network can control the cooker until you set one. (Home Assistant control via MQTT is separate and unaffected.)</p>
      {#if authCfg.enabled}
        <div>
          <label class="block text-xs opacity-60 mb-1">Current password</label>
          <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" type="password" autocomplete="current-password" bind:value={authPw} />
        </div>
      {/if}
      <div>
        <label class="block text-xs opacity-60 mb-1">{authCfg.enabled ? 'New password' : 'Set a password'}</label>
        <input class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" type="password" autocomplete="new-password" bind:value={authNew} />
      </div>
      <div class="flex gap-2">
        <button class="flex-1 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={saveAuthPassword}>{authCfg.enabled ? 'Change password' : 'Enable password'}</button>
        {#if authCfg.enabled}
          <button class="px-4 py-2 rounded-lg bg-neutral-200 dark:bg-neutral-800 font-semibold" onclick={disableAuth}>Turn off</button>
        {/if}
      </div>
    </div>
  </details>

  <!-- Backup & Restore -->
  <details class="hm-card rounded-2xl overflow-hidden">
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">Backup &amp; Restore</summary>
    <div class="px-4 pb-4 space-y-3">
      <p class="text-xs opacity-60">Download a full backup of probe calibration, names, PID, alarms, fan, display, MQTT/notify settings, and saved cook programs. Restore it here or on another HeaterMeter.</p>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={backupSecrets} /> Include passwords &amp; tokens</label>
      <div class="flex gap-2">
        <button class="flex-1 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={downloadBackup}>Download backup</button>
        <label class="flex-1 px-4 py-2 rounded-lg bg-neutral-200 dark:bg-neutral-800 font-semibold text-center cursor-pointer">
          Restore from file
          <input type="file" accept="application/json,.json" class="hidden" onchange={restoreBackup} />
        </label>
      </div>
    </div>
  </details>

  <!-- About -->
  <details class="hm-card rounded-2xl overflow-hidden" open>
    <summary class="cursor-pointer select-none px-4 py-3 font-bold">About</summary>
    <div class="px-4 pb-4 space-y-2 text-sm">
      <div class="flex justify-between"><span class="opacity-60">App version</span><span class="nums font-semibold">{status?.app_version ?? '—'}</span></div>
      <div class="flex justify-between"><span class="opacity-60">Board firmware</span><span class="nums font-semibold">{status?.version ?? '—'}</span></div>
      <div class="flex justify-between"><span class="opacity-60">Device</span><span class="font-semibold">{status?.device_name ?? 'HeaterMeter'}</span></div>
      <div class="pt-2 border-t border-black/10 dark:border-white/10 space-y-2">
        <button class="px-3 py-2 rounded-lg bg-neutral-700 text-white text-sm w-full"
                onclick={() => window.dispatchEvent(new Event('hm-run-wizard'))}>Run first-time setup again</button>
        <a class="text-orange-600 underline text-sm" href="https://github.com/BTallack/heatermeter-modern" target="_blank" rel="noopener">Project on GitHub</a>
      </div>
    </div>
  </details>

  {/if}
</div>

{#if toast}
  <div class={'fixed top-3 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-full text-sm font-semibold shadow-lg ' + (toast.ok ? 'bg-green-600 text-white' : 'bg-red-600 text-white')}>
    {toast.msg}
  </div>
{/if}

{#if fwModal}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onclick={() => (fwModal = false)}>
    <div class="hm-card rounded-2xl max-w-sm w-full p-4 space-y-3" onclick={(e) => e.stopPropagation()}>
      <h2 class="font-display text-lg">{fwAction === 'rollback' ? 'Roll back firmware' : 'Update firmware'}</h2>
      {#if fwAction === 'rollback'}
        <p class="text-sm">Reverts the controller to the firmware that was on it before the last update.</p>
      {:else}
        <p class="text-sm">Reflash the controller to <strong class="font-display">{fwSel}</strong>?</p>
      {/if}
      <ul class="text-xs opacity-80 space-y-1 list-disc pl-4">
        <li>The display goes blank for about 30 to 60 seconds.</li>
        <li>Do not power off the HeaterMeter or the Pi during the update.</li>
        <li>The whole update takes about a minute.</li>
        {#if fwAction !== 'rollback' && selectedImage?.eeprom_reset}
          <li>Your calibration, names, PID, and alarms are saved now and restored automatically.</li>
        {/if}
        <li>This is a supervised update. Stay nearby until it reports success.</li>
      </ul>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={fwConfirm} /> I understand, proceed</label>
      <div class="flex gap-2">
        <button class="flex-1 px-4 py-2 rounded-lg bg-neutral-300 dark:bg-neutral-700 font-semibold" onclick={() => (fwModal = false)}>Cancel</button>
        <button class="flex-1 px-4 py-2 rounded-lg bg-orange-600 text-white font-semibold disabled:opacity-40"
                disabled={!fwConfirm} onclick={() => doFlash(fwAction)}>
          {fwAction === 'rollback' ? 'Roll back' : 'Flash now'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Native <select> renders ~4px shorter than text inputs at the same padding;
     pin them to the input height (40px) so every control lines up. */
  select { height: 2.5rem; }

  /* Replace the native disclosure triangle with a subtle right-side chevron
     that rotates when the card opens. */
  details > summary { list-style: none; position: relative; padding-right: 2.5rem; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::after {
    content: '';
    position: absolute;
    right: 1.1rem;
    top: 50%;
    width: 0.5rem;
    height: 0.5rem;
    border-right: 2px solid currentColor;
    border-bottom: 2px solid currentColor;
    opacity: 0.35;
    transform: translateY(-70%) rotate(45deg);
    transition: transform 0.15s ease;
  }
  details[open] > summary::after { transform: translateY(-30%) rotate(225deg); }

  /* The tab chip row scrolls horizontally on narrow screens; the scrollbar
     itself is just noise. */
  .no-scrollbar { scrollbar-width: none; }
  .no-scrollbar::-webkit-scrollbar { display: none; }
</style>
