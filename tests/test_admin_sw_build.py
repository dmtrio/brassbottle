"""Build-output test: the service worker `npm run build` generates (admin/ui/dist/sw.js).

The claim it guards: the installed worker never serves `/` or `/api/*` from
cache. So the generated worker precaches hashed assets under /assets/ only, has
no navigation fallback and no runtime route, and prunes every cache it does not
own. Skips explicitly (a skip is not a pass) when admin/ui/dist is not built.
"""
import json
import re
import unittest
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "admin" / "ui" / "dist"
OWN_CACHE_PREFIX = "djinn-admin-precache-"


def precache_urls(sw_js: str) -> list[str]:
    """The url of every entry of the worker's precacheAndRoute([...]) list."""
    match = re.search(r"precacheAndRoute\(\[(.*?)\]\s*,", sw_js, re.S)
    if match is None:
        raise AssertionError("sw.js has no precacheAndRoute([...]) call")
    return re.findall(r"url:\s*\"([^\"]*)\"", match.group(1))


def imported_scripts(sw_js: str) -> list[str]:
    """The name of every script the worker pulls in with importScripts(...)."""
    names: list[str] = []
    for args in re.findall(r"importScripts\(([^)]*)\)", sw_js):
        names += re.findall(r"[\"']([^\"']+)[\"']", args)
    return names


def fetch_handling(script: str) -> list[str]:
    """The ways a script can answer a request: a fetch listener, a respondWith, a workbox route."""
    patterns = (r"addEventListener\(\s*[\"']fetch[\"']", r"\bonfetch\b", r"respondWith", r"registerRoute",
                r"setDefaultHandler", r"setCatchHandler", r"NavigationRoute")
    return [pattern for pattern in patterns if re.search(pattern, script)]


def cleanup_keeps(cleanup_js: str) -> list[str]:
    """Every string literal in the cleanup script that a cache name is kept by (startsWith / includes / ===)."""
    return re.findall(r"[\"'](djinn[^\"']*)[\"']", cleanup_js)


@unittest.skipUnless((DIST / "sw.js").is_file(), "SKIP: admin/ui/dist/sw.js is not built (cd admin/ui && npm run build)")
class GeneratedServiceWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sw = (DIST / "sw.js").read_text(encoding="utf-8")
        cls.urls = precache_urls(cls.sw)
        cls.imported = imported_scripts(cls.sw)

    def test_precache_is_hashed_assets_only(self):
        self.assertGreaterEqual(len(self.urls), 3)   # the bundle's js and css, and the fonts
        for url in self.urls:
            with self.subTest(url=url):
                self.assertRegex(url, r"^assets/[^/]+-[A-Za-z0-9_-]{8}\.[a-z0-9]+$")
                self.assertTrue((DIST / url).is_file(), f"{url} is precached but not in dist")

    def test_precache_holds_no_app_shell_entry(self):
        for url in self.urls:
            resolved = "/" + url.lstrip("/")
            with self.subTest(url=url):
                self.assertNotIn(resolved, ("/", "/index.html", "/egress", "/denylist", "/bottles", "/backup"))
                self.assertFalse(resolved.startswith("/api/"))
                self.assertNotIn("index.html", url)
        self.assertFalse(any(url in ("", "/", ".", "./") for url in self.urls))

    def test_no_navigation_fallback_and_no_runtime_route(self):
        # workbox's own chunk defines the classes; the worker file must not use them.
        for forbidden in ("NavigationRoute", "createHandlerBoundToURL", "registerRoute", "setDefaultHandler",
                          "setCatchHandler", "index.html", "/api/", "api/egress", "NetworkFirst", "StaleWhileRevalidate",
                          "CacheFirst"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sw)

    def test_the_only_fetch_listener_is_workboxs_precache_route(self):
        # precacheAndRoute answers a request only for a precached url; a fetch handler
        # of our own would be the way `/` or `/api/*` got cached.
        self.assertNotIn('addEventListener("fetch"', self.sw)
        self.assertNotIn("addEventListener('fetch'", self.sw)
        self.assertEqual(fetch_handling(self.sw), [])

    def test_no_imported_script_answers_a_request(self):
        # importScripts runs the script as part of the worker: a fetch listener there (network-first
        # for /api/*, written into the worker's own cache) is as good as one in sw.js.
        self.assertIn("sw-cleanup.js", self.imported)
        for name in self.imported:
            with self.subTest(script=name):
                script = (DIST / name).read_text(encoding="utf-8")
                self.assertEqual(fetch_handling(script), [], f"{name} handles requests")

    def test_worker_takes_over_at_once(self):
        self.assertIn("skipWaiting()", self.sw)
        self.assertIn("clientsClaim()", self.sw)

    def test_cleanup_keeps_only_the_workers_own_cache(self):
        self.assertIn('importScripts("sw-cleanup.js")', self.sw)
        self.assertIn(f'prefix:"{OWN_CACHE_PREFIX[:-len("-precache-")]}"', self.sw.replace(" ", ""))
        cleanup = (DIST / "sw-cleanup.js").read_text(encoding="utf-8")
        self.assertIn(f"!key.startsWith('{OWN_CACHE_PREFIX}')", cleanup)
        self.assertIn("caches.delete(key)", cleanup)
        # the own prefix is the only cache name the script mentions: no exemption for the legacy
        # `djinn-admin-shell-*` cache or any other name, which would survive the cleanup
        self.assertEqual(cleanup_keeps(cleanup), [OWN_CACHE_PREFIX])

    def test_every_script_the_worker_loads_is_in_dist(self):
        for name in re.findall(r"importScripts\(\"([^\"]+)\"\)", self.sw) + re.findall(r"define\(\[\"\./([^\"]+)\"\]", self.sw):
            with self.subTest(script=name):
                self.assertTrue((DIST / (name if name.endswith(".js") else name + ".js")).is_file())

    def test_manifest_makes_the_app_installable(self):
        manifest = json.loads((DIST / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["name"], "Djinn admin")
        self.assertEqual(manifest["theme_color"], "#fafafb")        # tokens.css --app-canvas, light
        self.assertEqual(manifest["background_color"], "#fafafb")
        sizes = {icon["sizes"]: icon["src"] for icon in manifest["icons"]}
        self.assertEqual(sorted(sizes), ["192x192", "512x512"])
        for src in sizes.values():
            self.assertTrue((DIST / src.lstrip("/")).is_file(), f"{src} is in the manifest but not in dist")
        html = (DIST / "index.html").read_text(encoding="utf-8")
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)


class ScriptReadingTests(unittest.TestCase):
    def test_imported_scripts_reads_every_name(self):
        self.assertEqual(imported_scripts('importScripts("sw-cleanup.js");x();importScripts("a.js", \'b.js\')'),
                         ["sw-cleanup.js", "a.js", "b.js"])

    def test_fetch_handling_sees_a_listener_and_a_respond_with(self):
        self.assertEqual(fetch_handling("self.addEventListener('activate', () => {})"), [])
        self.assertTrue(fetch_handling("self.addEventListener( 'fetch', (e) => e.respondWith(fetch(e.request)))"))
        self.assertTrue(fetch_handling('self.onfetch = null'))

    def test_cleanup_keeps_reads_the_names(self):
        self.assertEqual(cleanup_keeps("keys.filter((k) => !k.startsWith('djinn-admin-precache-'))"), ["djinn-admin-precache-"])
        self.assertEqual(cleanup_keeps("k !== 'djinn-admin-shell-v3' && !k.startsWith('djinn-admin-precache-')"),
                         ["djinn-admin-shell-v3", "djinn-admin-precache-"])


class PrecacheUrlsTests(unittest.TestCase):
    def test_reads_every_entry(self):
        sw = 'x.precacheAndRoute([{url:"assets/a-12345678.js",revision:null},{url:"assets/b-12345678.css",revision:null}],{})'
        self.assertEqual(precache_urls(sw), ["assets/a-12345678.js", "assets/b-12345678.css"])

    def test_a_worker_without_the_call_is_an_error(self):
        with self.assertRaises(AssertionError):
            precache_urls("self.addEventListener('fetch', () => {})")


if __name__ == "__main__":
    unittest.main()
