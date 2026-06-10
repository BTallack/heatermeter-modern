// Same-origin API helpers. Always absolute /api so they work no matter what
// path the SPA is mounted at (/app during build-out, / after cutover).
const BASE = '/api';

// Optional bearer token (only present when the user has enabled auth + logged
// in). Sent on every request and the WebSocket. A 401 broadcasts a window event
// so the app shell can show the login screen.
let _token = '';
try { _token = localStorage.getItem('hm.token') || ''; } catch (_) {}

export function authToken() { return _token; }
export function setAuthToken(t) {
  _token = t || '';
  try { t ? localStorage.setItem('hm.token', t) : localStorage.removeItem('hm.token'); } catch (_) {}
}
export function authHeaders(extra) {
  const h = { ...(extra || {}) };
  if (_token) h['Authorization'] = 'Bearer ' + _token;
  return h;
}
function _check(r) {
  if (r.status === 401) {
    try { window.dispatchEvent(new Event('hm-auth-required')); } catch (_) {}
  }
  return r;
}

export async function getJSON(path) {
  const r = _check(await fetch(`${BASE}/${path}`, { headers: authHeaders() }));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

export async function reqJSON(method, path, body) {
  const r = _check(await fetch(`${BASE}/${path}`, {
    method,
    headers: authHeaders(body === undefined ? undefined : { 'Content-Type': 'application/json' }),
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
export const postJSON = (p, b) => reqJSON('POST', p, b);
export const patchJSON = (p, b) => reqJSON('PATCH', p, b);
export const delJSON = (p) => reqJSON('DELETE', p);

// Live WebSocket with auto-reconnect. Returns a stop() function.
export function connectWs(onMsg) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws, timer, closed = false;
  const open = () => {
    const q = _token ? ('?token=' + encodeURIComponent(_token)) : '';
    ws = new WebSocket(`${proto}://${location.host}/api/ws${q}`);
    ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { if (!closed) timer = setTimeout(open, 2000); };
  };
  open();
  return () => { closed = true; clearTimeout(timer); if (ws) ws.close(); };
}
