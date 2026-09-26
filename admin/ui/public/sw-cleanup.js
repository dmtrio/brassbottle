/* global self, caches */
// Pulled into the generated service worker (pwa.ts, importScripts). On
// activation it deletes every cache the worker does not own: the legacy page's
// `djinn-admin-shell-v3` and anything else an earlier worker on this origin left.
// The worker's own precache is named `djinn-admin-precache-v2-<scope>`.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => !key.startsWith('djinn-admin-precache-')).map((key) => caches.delete(key)))
    )
  )
})
