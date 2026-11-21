// Arrgus app service worker: keeps the app shell, theme and icons on the phone so the app opens instantly
// and can show its "can't reach Arrgus" screen (with the last known status) when the server is unreachable.
// Live data (/api/...) always comes from the network; the app keeps its own copy of the last status.
const CACHE = "arrgus-app-v1";
const SHELL = ["/app", "/app/manifest.webmanifest", "/app/icon-192.png", "/app/apple-touch-icon.png", "/app/favicon-64.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const same = url.origin === self.location.origin;
  if (same && url.pathname.startsWith("/api/")) return;                 // live data: network only

  if (req.mode === "navigate" && same && url.pathname === "/app") {     // the app page: network first
    e.respondWith(fetch(req).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put("/app", copy)); }
      return r;
    }).catch(() => caches.match("/app")));
    return;
  }
  const isStatic = (same && (url.pathname === "/theme.css" || url.pathname.startsWith("/app/")))
    || url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
  if (!isStatic) return;
  e.respondWith(caches.open(CACHE).then(async (c) => {                  // static: cached copy now, refresh in background
    const hit = await c.match(req, {ignoreSearch: url.pathname === "/theme.css"});
    const net = fetch(req).then((r) => { if (r.ok || r.type === "opaque") c.put(req, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  }));
});
