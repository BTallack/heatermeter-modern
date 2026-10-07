<script>
  import { toasts, dismiss } from './toast.svelte.js';

  // Desktop puts them bottom-left (the floating buttons own bottom-right);
  // phones put them just above the tab bar.
  let { desktop = false } = $props();
</script>

<div
  class={'fixed z-[60] flex flex-col gap-2 pointer-events-none ' +
    (desktop ? 'left-6 bottom-6 w-[22rem]' : 'left-4 right-4 hm-toast-phone')}
  role="status"
  aria-live="polite"
>
  {#each toasts as t (t.id)}
    <div
      class="pointer-events-auto flex items-center gap-3 rounded-xl px-4 py-3 text-sm shadow-lg
             bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-950"
      role={t.tone === 'error' ? 'alert' : undefined}
    >
      {#if t.tone === 'error'}
        <svg viewBox="0 0 24 24" class="w-5 h-5 shrink-0 text-red-400 dark:text-red-600" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" /><line x1="12" y1="7.5" x2="12" y2="12.5" /><line x1="12" y1="16" x2="12" y2="16.5" />
        </svg>
      {/if}
      <span class="flex-1 min-w-0">{t.text}</span>
      {#if t.action}
        <button
          class="shrink-0 -my-1 px-2 h-9 rounded-lg font-semibold text-orange-300 dark:text-orange-700
                 hover:bg-white/10 dark:hover:bg-black/10"
          onclick={() => { t.action(); dismiss(t.id); }}>{t.actionLabel}</button>
      {/if}
      <button class="shrink-0 -my-1 -mr-2 w-9 h-9 rounded-lg grid place-items-center opacity-60 hover:opacity-100"
              onclick={() => dismiss(t.id)} aria-label="Dismiss message">
        <svg viewBox="0 0 24 24" class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.25"
             stroke-linecap="round" aria-hidden="true"><line x1="6" y1="6" x2="18" y2="18" /><line x1="18" y1="6" x2="6" y2="18" /></svg>
      </button>
    </div>
  {/each}
</div>

<style>
  .hm-toast-phone { bottom: calc(env(safe-area-inset-bottom) + 4.25rem); }
</style>
