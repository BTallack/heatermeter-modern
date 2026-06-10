/* HeaterMeter service worker.
 *
 * Two jobs:
 *  1. PWA install + offline shell: cache the app shell so the dashboard opens
 *     instantly and survives brief network blips at the grill.
 *  2. Notifications: relay alarm notifications from the page (postMessage) so
 *     they appear via the SW registration even when the tab is backgrounded,
 *     and handle Web Push events if/when VAPID push is configured server-side.
 *
 * The app's own HTML/JS/CSS are served no-cache by the server (so deploys are
 * picked up), so the SW uses a NETWORK-FIRST strategy for them and only falls
 * back to cache when offline. This keeps the stale-asset class of bug away.
 */

const CACHE = "hm-shell-v1";
const SHELL = ["/", "/app.js", "/style.css", "/manifest.webmanifest",
               "/icon.svg", "/vendor/uPlot.iife.min.js", "/vendor/uPlot.min.css"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Never cache the API or websocket; let them hit the network directly.
  if (url.pathname.startsWith("/api/")) return;
  // Network-first for the shell, fall back to cache when offline.
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        // Refresh the cached copy in the background.
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});

// The page relays alarm notifications here so they show even when backgrounded.
self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "notify") {
    self.registration.showNotification(d.title || "HeaterMeter", {
      body: d.body || "",
      icon: "/icon.svg",
      badge: "/icon.svg",
      tag: d.tag || "hm-alarm",
      renotify: true,
    });
  }
});

// Web Push (only fires if a push subscription + VAPID is configured server-side).
self.addEventListener("push", (e) => {
  let data = { title: "HeaterMeter", body: "Alarm" };
  try { if (e.data) data = e.data.json(); } catch (err) {}
  e.waitUntil(self.registration.showNotification(data.title || "HeaterMeter", {
    body: data.body || "", icon: "/icon.svg", badge: "/icon.svg",
    tag: data.tag || "hm-alarm", renotify: true,
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((cl) => {
    for (const c of cl) { if ("focus" in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow("/");
  }));
});
