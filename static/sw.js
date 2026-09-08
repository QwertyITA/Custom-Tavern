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

// A real reply, pushed from app/push_notify.py (§ toggleReplyNotifications,
// app.js, for how this got subscribed). This is the whole reason push
// exists here rather than the page raising a Notification itself: this
// handler runs even after Android has frozen or fully discarded the tab —
// the push service waking the service worker is the one path the OS does
// not throttle the same way, because it is also how the OS delivers every
// other app's notifications.
//
// Silent when a focused window already has this on screen — the same
// reasoning WhatsApp's own desktop-plus-phone sync uses: a message you are
// already looking at does not also need to buzz.
self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) { /* not JSON */ }
  const title = payload.title || "Custom Tavern";
  const body = payload.body || "";
  if (!body) return;

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      if (list.some((client) => client.focused)) return null;
      return self.registration.showNotification(title, {
        body,
        icon: "/static/icons/icon-192.png",
        badge: "/static/icons/icon-192.png",
        // One live notification at a time rather than a stack — a second
        // reply while the first is still showing replaces it, the same
        // collapsing WhatsApp itself does per conversation.
        tag: "tavern-reply",
      });
    })
  );
});

// The notification above, tapped. There is no navigation to do — the page
// reads its last chat off localStorage on its own — so this only has to
// find an existing tab and put it in front, or open one if the app was
// closed rather than merely backgrounded.
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
