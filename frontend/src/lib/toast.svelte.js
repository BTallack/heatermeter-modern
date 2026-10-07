// Brief, app-wide messages: confirmation that a command reached the
// HeaterMeter, or a plain account of why it didn't. Rendered by Toasts.svelte.

let seq = 0;
export const toasts = $state([]);

/**
 * @param {string} text
 * @param {{tone?: 'info'|'error', ms?: number, actionLabel?: string, action?: () => void}} [opts]
 */
export function toast(text, opts = {}) {
  const t = {
    id: ++seq,
    text,
    tone: opts.tone || 'info',
    actionLabel: opts.actionLabel || null,
    action: opts.action || null,
  };
  toasts.push(t);
  setTimeout(() => dismiss(t.id), opts.ms ?? (t.tone === 'error' ? 8000 : t.action ? 7000 : 4000));
  return t.id;
}

export function dismiss(id) {
  const i = toasts.findIndex((x) => x.id === id);
  if (i >= 0) toasts.splice(i, 1);
}

// The one failure message for a command that never reached the board.
export const unreachable = (what) =>
  `Couldn't reach the HeaterMeter, so ${what}. Check the connection and try again.`;
