const CACHE_VERSION = "djinn-admin-shell-v3";
const SHELL_PATHS = [
  "/app.js",
  "/vendor/htm-preact-standalone.module.js",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL_PATHS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return;
  const isShellPath = SHELL_PATHS.includes(url.pathname);

  // "/" is NEVER cached: without a session cookie it is the pointer page, and
  // a cached pointer page would masquerade as the app shell after a session
  // expires. Only the asset routes below are cached.
  if (url.pathname === "/") {
    event.respondWith(fetch(req));
    return;
  }

  if (!isShellPath) return;

  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy)).catch(() => {});
      return resp;
    }))
  );
});
