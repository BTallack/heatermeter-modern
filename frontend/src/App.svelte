<script>
  import { App, Page, Navbar } from 'konsta/svelte';
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import Dashboard from './lib/Dashboard.svelte';
  import Graph from './lib/Graph.svelte';
  import Cook from './lib/Cook.svelte';
  import Settings from './lib/Settings.svelte';
  import { getTheme, applyTheme, onSystemChange } from './lib/theme.js';
  import { getJSON, postJSON, setAuthToken, authToken } from './lib/api.js';

  // Mobile/tablet: four full-screen tabs.
  const NAV = [
    { id: 'dash', label: 'Dashboard' },
    { id: 'graph', label: 'Graph' },
    { id: 'cook', label: 'Cook' },
    { id: 'settings', label: 'Settings' },
  ];
  const MODAL_TITLE = { cook: 'Cook', settings: 'Settings' };

  let tab = $state('dash');     // mobile: active full-screen tab
  let modal = $state(null);     // desktop: 'cook' | 'settings' | null
  let theme = $state(getTheme());
  let dark = $state(true);
  let isDesktop = $state(false); // >=1024px -> full-width dashboard + FABs

  // -- optional auth gate ---------------------------------------------------
  let needsAuth = $state(false);
  let loginPw = $state('');
  let loginBusy = $state(false);
  let loginErr = $state('');
  async function checkAuth() {
    try {
      const a = await getJSON('auth');
      if (a.enabled && !authToken()) needsAuth = true;
    } catch (_) {}
  }
  async function doLogin() {
    loginBusy = true; loginErr = '';
    try {
      const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: loginPw }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false || !j.token) { loginErr = j.error || 'Incorrect password.'; loginBusy = false; return; }
      setAuthToken(j.token);
      location.reload();
    } catch (e) { loginErr = 'Login failed.'; loginBusy = false; }
  }

  onMount(() => {
    dark = applyTheme();
    checkAuth();
    window.addEventListener('hm-auth-required', () => { needsAuth = true; });
    onSystemChange(() => { if (theme === 'auto') dark = applyTheme(); });
    // Welcome banner is remembered on the HeaterMeter, not the browser: a local
    // dismissal is the instant fast-path; otherwise ask the server so a new
    // browser/device respects a dismissal made anywhere. Stays hidden until we
    // know, so it never flashes.
    if (localStorage.getItem('hm.welcomed') === '1') welcomed = true;
    else getJSON('ui-prefs').then((p) => { welcomed = !!(p && p.welcomed); })
                            .catch(() => { welcomed = false; });
    readPanelStyle();
    window.addEventListener('hm-prefs', readPanelStyle);
    window.addEventListener('hm-run-wizard', openWizard);
    const mq = window.matchMedia('(min-width: 1024px)');
    const upd = () => {
      isDesktop = mq.matches;
      if (isDesktop) {
        if (tab === 'cook' || tab === 'settings') modal = tab;
        if (tab === 'graph' || tab === 'cook' || tab === 'settings') tab = 'dash';
      } else if (modal) {
        tab = modal;
        modal = null;
      }
    };
    upd();
    mq.addEventListener('change', upd);
  });

  // The theme is chosen in Settings -> Appearance; it calls this back so the
  // Konsta <App> dark flag and the auto-watcher stay in sync.
  function syncTheme(t, d) { theme = t; dark = d; }
  // Desktop floating buttons toggle the Cook / Settings popups.
  function toggleModal(id) { modal = (modal === id) ? null : id; }

  // Desktop panel style (Settings -> Appearance): centered popup (default) or
  // a slide-out sidebar snapped left/right. Persisted in localStorage and
  // synced via the hm-prefs event, like the other appearance preferences.
  let panelStyle = $state('modal');
  function readPanelStyle() {
    const v = localStorage.getItem('hm.panelStyle');
    panelStyle = ['modal', 'left', 'right'].includes(v) ? v : 'modal';
  }

  // First-run welcome banner (shown once until dismissed).
  let welcomed = $state(true);
  function dismissWelcome() {
    welcomed = true;
    try { localStorage.setItem('hm.welcomed', '1'); } catch (_) {}
    postJSON('ui-prefs', { welcomed: true }).catch(() => {});   // remember server-side
  }
  function openSettings() { if (isDesktop) modal = 'settings'; else tab = 'settings'; dismissWelcome(); }

  // First-run setup wizard: cooker PID preset, pit probe type, units. Three
  // small steps; everything is skippable and re-doable later in Settings.
  let wizardOpen = $state(false);
  let wizardStep = $state(0);
  let wizPidPresets = $state([]);
  let wizProbePresets = $state({});
  let wizPid = $state('');
  let wizProbe = $state('');
  let wizUnit = $state('F');
  async function openWizard() {
    wizardOpen = true; wizardStep = 0;
    try { wizPidPresets = (await getJSON('presets')).pid || []; } catch (_) {}
    try { wizProbePresets = (await getJSON('probe-presets')).presets || {}; } catch (_) {}
    try { const s = await getJSON('status'); wizUnit = s.pid?.units || 'F'; } catch (_) {}
  }
  async function wizardNext() {
    try {
      if (wizardStep === 0 && wizPid) {
        const p = wizPidPresets.find((x) => x.key === wizPid || x.label === wizPid);
        if (p) await postJSON('pid', { b: p.b, p: p.p, i: p.i, d: p.d });
      } else if (wizardStep === 1) {
        if (wizProbe) await postJSON('probe-type', { index: 0, preset: wizProbe });
        await postJSON('units', { unit: wizUnit });
      }
    } catch (_) {}
    if (wizardStep >= 2) { wizardOpen = false; dismissWelcome(); }
    else wizardStep += 1;
  }
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') modal = null; }} />

{#if needsAuth}
  <div class="fixed inset-0 z-[100] bg-neutral-950 text-white flex items-center justify-center p-6">
    <form class="w-full max-w-xs space-y-4" onsubmit={(e) => { e.preventDefault(); doLogin(); }}>
      <div class="text-center">
        <div class="font-display font-extrabold text-2xl">HeaterMeter</div>
        <p class="text-sm opacity-60 mt-1">Enter your password to continue.</p>
      </div>
      <input class="w-full bg-neutral-800 rounded-lg px-3 py-2.5" type="password" placeholder="Password" autocomplete="current-password" bind:value={loginPw} />
      {#if loginErr}<p class="text-sm text-red-400">{loginErr}</p>{/if}
      <button class="w-full px-4 py-2.5 rounded-lg bg-orange-600 font-semibold disabled:opacity-50" disabled={loginBusy} type="submit">{loginBusy ? 'Signing in…' : 'Sign in'}</button>
    </form>
  </div>
{/if}

{#snippet welcome()}
  <div class="hm-card rounded-2xl p-4 flex items-start gap-3">
    <svg viewBox="0 0 24 24" fill="currentColor" class="w-6 h-6 text-orange-500 shrink-0" aria-hidden="true">
      <path d="M12 23a7 7 0 0 0 7-7c0-2.1-1-4-2.6-5.5-.2 1.2-1.1 2.1-2.3 2.1.9-2.5.2-5.2-2.3-7.9-.5 2.7-2 3.8-3.6 5.1C6.7 11.4 5 13.5 5 16a7 7 0 0 0 7 7z" />
    </svg>
    <div class="flex-1 min-w-0">
      <div class="font-display font-bold">Welcome to HeaterMeter</div>
      <p class="text-sm opacity-70 mt-0.5">Tap a probe to set a target temperature. Open Settings to name your probes and pick a PID preset for your cooker.</p>
      <div class="flex gap-2 mt-3">
        <button class="px-3 py-1.5 rounded-lg bg-orange-600 text-white text-sm font-semibold" onclick={openWizard}>Set up my cooker</button>
        <button class="px-3 py-1.5 rounded-lg bg-black/5 dark:bg-white/10 text-sm" onclick={openSettings}>Open Settings</button>
        <button class="px-3 py-1.5 rounded-lg bg-black/5 dark:bg-white/10 text-sm" onclick={dismissWelcome}>Got it</button>
      </div>
    </div>
    <button class="text-xl leading-none opacity-50 hover:opacity-100" onclick={dismissWelcome} aria-label="Dismiss">✕</button>
  </div>
{/snippet}

{#if wizardOpen}
  <div class="fixed inset-0 z-[90] bg-black/60 flex items-center justify-center p-4" role="dialog" aria-label="Setup wizard">
    <div class="hm-card rounded-2xl p-5 w-full max-w-md space-y-4">
      <div class="flex items-center justify-between">
        <h3 class="font-display font-bold text-lg">
          {wizardStep === 0 ? 'Your cooker' : wizardStep === 1 ? 'Your probes' : 'All set'}
        </h3>
        <span class="text-xs opacity-50">Step {wizardStep + 1} of 3</span>
      </div>
      {#if wizardStep === 0}
        <p class="text-sm opacity-70">Pick the control preset closest to your grill. You can fine-tune or auto-tune later in Settings.</p>
        <div class="space-y-2">
          {#each wizPidPresets as p}
            <label class="flex items-start gap-2 text-sm cursor-pointer">
              <input type="radio" name="wizpid" value={p.key} bind:group={wizPid} class="mt-1" />
              <span><b>{p.label}</b>{#if p.note}<span class="opacity-60"> - {p.note}</span>{/if}</span>
            </label>
          {/each}
          <label class="flex items-start gap-2 text-sm cursor-pointer">
            <input type="radio" name="wizpid" value="" bind:group={wizPid} class="mt-1" />
            <span><b>Keep current tuning</b></span>
          </label>
        </div>
      {:else if wizardStep === 1}
        <p class="text-sm opacity-70">What kind of pit probe do you have, and which unit do you cook in?</p>
        <div>
          <label class="block"><span class="block text-xs opacity-60 mb-1">Pit probe type</span>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={wizProbe}>
            <option value="">Keep current</option>
            {#each Object.entries(wizProbePresets) as [key, p]}
              <option value={key}>{p.label}</option>
            {/each}
          </select></label>
        </div>
        <div>
          <label class="block"><span class="block text-xs opacity-60 mb-1">Temperature unit</span>
          <select class="w-full bg-neutral-200 dark:bg-neutral-800 rounded-lg px-2 py-2" bind:value={wizUnit}>
            <option value="F">Fahrenheit</option>
            <option value="C">Celsius</option>
          </select></label>
        </div>
      {:else}
        <p class="text-sm opacity-70">You're ready to cook. Two optional extras live in Settings:</p>
        <ul class="text-sm space-y-1 opacity-80">
          <li>· <b>Home Assistant (MQTT)</b> - the cooker appears in HA automatically.</li>
          <li>· <b>Notifications (ntfy)</b> - phone alerts when you're away from the pit.</li>
        </ul>
        <p class="text-sm opacity-70">Then try a <b>Guided Cook</b> on the Cook screen - pick what you're making and the rest is configured for you.</p>
      {/if}
      <div class="flex gap-2 pt-1">
        <button class="px-3 py-2 rounded-lg bg-black/5 dark:bg-white/10 text-sm" onclick={() => { wizardOpen = false; dismissWelcome(); }}>Skip</button>
        <button class="flex-1 px-3 py-2 rounded-lg bg-orange-600 text-white font-semibold" onclick={wizardNext}>{wizardStep >= 2 ? 'Start cooking' : 'Next'}</button>
      </div>
    </div>
  </div>
{/if}

{#snippet navIcon(id)}
  {#if id === 'dash'}
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" class="w-6 h-6" aria-hidden="true">
      <rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" />
    </svg>
  {:else if id === 'graph'}
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-6 h-6" aria-hidden="true">
      <path d="M3 3v18h18" /><polyline points="6 14 10 9 14 12 20 5" />
    </svg>
  {:else if id === 'cook'}
    <svg viewBox="0 0 24 24" fill="currentColor" class="w-6 h-6" aria-hidden="true">
      <path d="M12 23a7 7 0 0 0 7-7c0-2.1-1-4-2.6-5.5-.2 1.2-1.1 2.1-2.3 2.1.9-2.5.2-5.2-2.3-7.9-.5 2.7-2 3.8-3.6 5.1C6.7 11.4 5 13.5 5 16a7 7 0 0 0 7 7z" />
    </svg>
  {:else}
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" class="w-6 h-6" aria-hidden="true">
      <line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="20" y2="17" />
      <circle cx="9" cy="7" r="2" fill="currentColor" stroke="none" />
      <circle cx="15" cy="12" r="2" fill="currentColor" stroke="none" />
      <circle cx="8" cy="17" r="2" fill="currentColor" stroke="none" />
    </svg>
  {/if}
{/snippet}

<App theme="ios" {dark}>
  <Page>
    <!-- Mobile/tablet: simple top title bar (theme toggle now lives in Settings). -->
    {#if !isDesktop}
      <Navbar title="HeaterMeter" titleClass="font-display" />
    {/if}

    {#if isDesktop}
      <!-- Desktop: no sidebar. Temps band above the graph, full width. -->
      <div class="mx-auto max-w-[1100px] px-6 pt-5 pb-24 space-y-5">
        <div class="flex items-center">
          <span class="font-display font-extrabold text-lg tracking-tight">HeaterMeter</span>
        </div>
        {#if !welcomed}{@render welcome()}{/if}
        <Dashboard embedded />
        <Graph embedded />
      </div>
    {:else}
      <div>
        {#if tab === 'dash'}
          {#if !welcomed}<div class="px-4 pt-4">{@render welcome()}</div>{/if}
          <Dashboard />
        {:else if tab === 'graph'}
          <Graph />
        {:else if tab === 'cook'}
          <Cook />
        {:else if tab === 'settings'}
          <Settings onTheme={syncTheme} />
        {/if}
      </div>
    {/if}

  </Page>

  <!-- Mobile/tablet: bottom icon nav with a visible top separation. -->
  {#if !isDesktop}
    <nav class="fixed bottom-0 left-0 right-0 z-30 flex
                border-t border-black/10 dark:border-white/10
                bg-white/95 dark:bg-neutral-900/95 backdrop-blur"
         style="padding-bottom: env(safe-area-inset-bottom)">
      {#each NAV as n}
        <button
          class={'flex-1 flex items-center justify-center py-3 transition-colors ' +
            (tab === n.id ? 'text-orange-600' : 'text-neutral-500 dark:text-neutral-400')}
          onclick={() => (tab = n.id)}
          aria-label={n.label}
          aria-current={tab === n.id ? 'page' : undefined}>
          {@render navIcon(n.id)}
        </button>
      {/each}
    </nav>
  {/if}

  <!-- Desktop: floating Cook / Settings buttons (replace the sidebar nav). -->
  {#if isDesktop}
    <div class="fixed bottom-6 right-6 z-50 flex flex-col gap-3">
      <button
        class={'w-14 h-14 rounded-full shadow-lg flex items-center justify-center transition-colors ' +
          (modal === 'cook' ? 'bg-orange-700 text-white ring-2 ring-orange-300' : 'bg-orange-600 text-white hover:bg-orange-700')}
        onclick={() => toggleModal('cook')} aria-label="Cook" title="Cook">
        <svg viewBox="0 0 24 24" fill="currentColor" class="w-6 h-6" aria-hidden="true">
          <path d="M12 23a7 7 0 0 0 7-7c0-2.1-1-4-2.6-5.5-.2 1.2-1.1 2.1-2.3 2.1.9-2.5.2-5.2-2.3-7.9-.5 2.7-2 3.8-3.6 5.1C6.7 11.4 5 13.5 5 16a7 7 0 0 0 7 7z"/>
        </svg>
      </button>
      <button
        class={'w-14 h-14 rounded-full shadow-lg flex items-center justify-center transition-colors ' +
          (modal === 'settings' ? 'bg-neutral-900 text-white ring-2 ring-white/40 dark:bg-neutral-700' : 'bg-neutral-800 text-white hover:bg-neutral-900')}
        onclick={() => toggleModal('settings')} aria-label="Settings" title="Settings">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" class="w-6 h-6" aria-hidden="true">
          <line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="12" x2="20" y2="12" /><line x1="4" y1="17" x2="20" y2="17" />
          <circle cx="9" cy="7" r="2" fill="currentColor" stroke="none" />
          <circle cx="15" cy="12" r="2" fill="currentColor" stroke="none" />
          <circle cx="8" cy="17" r="2" fill="currentColor" stroke="none" />
        </svg>
      </button>
    </div>

    <!-- Desktop: Cook / Settings as a centered popup or a slide-out sidebar,
         per the Appearance preference. Both shells share the same header +
         scroll body via the snippet below. -->
    {#snippet panelInner()}
      <div class="shrink-0 flex items-center justify-between px-4 py-3
                  border-b border-black/10 dark:border-white/10">
        <span class="font-display font-bold text-lg">{MODAL_TITLE[modal]}</span>
        <button class="text-2xl leading-none px-2 opacity-60 hover:opacity-100" onclick={() => (modal = null)} aria-label="Close">✕</button>
      </div>
      <div class="grow overflow-y-auto overscroll-contain">
        {#if modal === 'cook'}
          <Cook />
        {:else if modal === 'settings'}
          <Settings showTitle={false} onTheme={syncTheme} />
        {/if}
      </div>
    {/snippet}

    {#if modal}
      <div class={'fixed inset-0 z-40 bg-black/50 ' + (panelStyle === 'modal' ? 'flex items-center justify-center p-4 lg:p-6' : '')}
           role="presentation" transition:fade={{ duration: 150 }} onclick={(e) => { if (e.target === e.currentTarget) modal = null; }}>
        {#if panelStyle === 'modal'}
          <!-- Centered popup: fixed-height flex column. The header stays put
               and the body is the single scroll container, so a mouse wheel
               scrolls reliably. The height is FIXED (h-, not max-h-) so
               switching Settings tabs with different content lengths never
               resizes or re-centers the panel. -->
          <div class="hm-card rounded-2xl w-full max-w-2xl h-[88vh] flex flex-col overflow-hidden"
               role="dialog" aria-modal="true" tabindex="-1">
            {@render panelInner()}
          </div>
        {:else}
          <!-- Slide-out sidebar snapped to the chosen edge: full height, same
               header + single scroll body as the popup. -->
          <div class={'hm-card absolute inset-y-0 w-full max-w-xl flex flex-col overflow-hidden shadow-2xl '
                      + (panelStyle === 'left' ? 'left-0 rounded-r-2xl rounded-l-none' : 'right-0 rounded-l-2xl rounded-r-none')}
               role="dialog" aria-modal="true" tabindex="-1"
               transition:fly={{ x: panelStyle === 'left' ? -560 : 560, duration: 220, opacity: 1 }}>
            {@render panelInner()}
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</App>
