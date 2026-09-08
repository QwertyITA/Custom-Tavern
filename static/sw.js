// Service worker: makes the app installable and instant to open (§2).
//
// Only the shell is cached. API traffic is never cached — a chat is live state
// and a stale reply is worse than no reply. SSE endpoints are passed straight
// through, since caching a stream would break it outright.

const CACHE = "tavern-shell-v2";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/markup.js",
  "/static/vendor/alpine.min.js",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// A reply notification (§ notifyReply, app.js), tapped. There is no
// navigation to do — chatId is already whatever it was when the message was
// sent, and the page reads it straight off localStorage on its own — so this
// only has to find an existing tab and put it in front, or open one if the
// app was closed rather than merely backgrounded.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow ? self.clients.openWindow("/") : null;
    })
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/")) return; // always live

  // Network first. Cache-first served the stored copy and only refreshed it in
  // the background, so every update landed a reload late: pull a fix, restart,
  // see the old app, and the change appears not to have worked. The server is
  // on the same device as the browser, so asking it first costs nothing — the
  // cache is here to keep the app openable when it is *not* running, which is
  // the one case cache-first was actually buying.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok && url.origin === self.location.origin) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() =>
        caches.match(event.request).then((hit) => hit || Response.error())
      )
  );
});
