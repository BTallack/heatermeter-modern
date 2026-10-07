<script>
  // A native modal <dialog>: the browser traps focus, makes the page behind it
  // inert, and closes it on Escape. Focus goes to the first field (or the
  // element marked autofocus) and returns to whatever opened it.
  let { open = $bindable(false), title, width = 'sm', children } = $props();
  const id = $props.id();
  let el;
  let opener = null;

  $effect(() => {
    if (!el) return;
    if (open && !el.open) {
      opener = document.activeElement;
      el.showModal();
      const first = el.querySelector('[autofocus]') ||
        el.querySelector('input:not([type=hidden]), select, textarea, button:not([data-close])');
      first?.focus();
    } else if (!open && el.open) {
      el.close();
    }
  });

  function onclose() {
    open = false;
    if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
    opener = null;
  }
</script>

<dialog
  bind:this={el}
  class={'hm-dialog ' + (width === 'xs' ? 'hm-dialog-xs' : '')}
  aria-labelledby={id}
  {onclose}
  onclick={(e) => { if (e.target === el) open = false; }}
>
  <div class="hm-card rounded-2xl p-4 space-y-3">
    <div class="flex items-center justify-between gap-2">
      <h2 {id} class="font-display font-bold text-lg">{title}</h2>
      <button data-close class="-mr-2 w-11 h-11 rounded-xl grid place-items-center opacity-60 hover:opacity-100"
              onclick={() => (open = false)} aria-label="Close">
        <svg viewBox="0 0 24 24" class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" aria-hidden="true"><line x1="6" y1="6" x2="18" y2="18" /><line x1="18" y1="6" x2="6" y2="18" /></svg>
      </button>
    </div>
    {@render children()}
  </div>
</dialog>
