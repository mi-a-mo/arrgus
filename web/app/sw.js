// Arrgus app service worker: keeps the app shell, theme and icons on the phone so the app opens instantly
// and can show its "can't reach Arrgus" screen (with the last known status) when the server is unreachable.
// Live data (/api/...) always comes from the network; the app keeps its own copy of the last status.
const CACHE = "arrgus-app-v2";
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

// ---- notifications ----------------------------------------------------------------------------------------
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = {body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "Arrgus", {
    body: d.body || "", tag: d.tag || undefined, icon: "/app/icon-192.png", badge: "/app/favicon-64.png",
    data: {url: d.url || "/app"},
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/app";
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then((list) => {
    for (const c of list) {
      if (new URL(c.url).pathname === "/app") { c.postMessage({open: url}); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});

// The phone's notification service replaced the subscription: subscribe again and tell Arrgus.
self.addEventListener("pushsubscriptionchange", (e) => {
  const post = (path, body) => fetch(path, {method: "POST", headers: {"X-Arrgus": "1", "Content-Type": "application/json"},
                                             body: JSON.stringify(body)});
  e.waitUntil(fetch("/api/push", {cache: "no-store"}).then((r) => r.json()).then((info) => {
    const raw = atob(info.public_key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - info.public_key.length % 4) % 4));
    const key = Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
    return self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
  }).then((sub) => post("/api/push/subscribe", {subscription: sub.toJSON(), name: "Phone (renewed)",
                                                 old: e.oldSubscription ? e.oldSubscription.endpoint : null})));
});
