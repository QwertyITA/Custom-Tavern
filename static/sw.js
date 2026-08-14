// Service worker: makes the app installable and instant to open (§2).
//
// Only the shell is cached. API traffic is never cached — a chat is live state
// and a stale reply is worse than no reply. SSE endpoints are passed straight
// through, since caching a stream would break it outright.

const CACHE = "tavern-shell-v1";
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

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/")) return; // always live

  event.respondWith(
    caches.match(event.request).then((hit) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response.ok && url.origin === self.location.origin) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => hit);
      // Cache-first for a fast cold start, with a background refresh.
      return hit || network;
    })
  );
});
