// Light / dark / auto theme. Konsta and our Tailwind `dark:` utilities both key
// off the `.dark` class on <html>, so we just toggle that (plus the theme-color
// meta for the mobile status bar). 'auto' follows the OS preference.
const KEY = 'hm.theme'; // 'auto' | 'light' | 'dark'

export function getTheme() {
  const t = localStorage.getItem(KEY);
  return t === 'light' || t === 'dark' ? t : 'auto';
}

export function effectiveDark() {
  const t = getTheme();
  if (t === 'dark') return true;
  if (t === 'light') return false;
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

export function applyTheme() {
  const dark = effectiveDark();
  document.documentElement.classList.toggle('dark', dark);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? '#0b0e11' : '#f2f3f5');
  return dark;
}

export function setTheme(t) {
  localStorage.setItem(KEY, t);
  return applyTheme();
}

export function onSystemChange(cb) {
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', cb);
  }
}
