// The service worker and web app manifest the build emits (vite-plugin-pwa).
//
// The worker precaches the hashed build output under /assets/ and nothing
// else: never `/` or an app route (without a session cookie `/` is the pointer
// page, and a cached copy would stand in for it), never `/api/*`. It has no
// navigation fallback and no runtime caching. public/sw-cleanup.js, pulled in
// with importScripts, deletes every cache the worker does not own, the legacy
// page's `djinn-admin-shell-v3` included.
import { readFileSync } from 'node:fs'
import path from 'node:path'
import type { VitePWAOptions } from 'vite-plugin-pwa'

export const CACHE_ID = 'djinn-admin'   // the precache is `djinn-admin-precache-v2-<scope>`; sw-cleanup.js keeps that prefix

/** `oklch(L C H [/ A])` to `#rrggbb` (alpha dropped), the form every manifest reader accepts. */
export function oklchToHex(css: string): string {
  const m = /oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(css)
  if (!m) throw new Error(`not an oklch colour: ${css}`)
  const [l, c, h] = [Number(m[1]), Number(m[2]), (Number(m[3]) * Math.PI) / 180]
  const a = c * Math.cos(h)
  const b = c * Math.sin(h)
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3
  const linear = [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
  ]
  const channel = (v: number) => {
    const clamped = Math.min(1, Math.max(0, v))
    const gamma = clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * clamped ** (1 / 2.4) - 0.055
    return Math.round(gamma * 255).toString(16).padStart(2, '0')
  }
  return `#${linear.map(channel).join('')}`
}

/** The light and dark `--app-canvas` values of src/styles/tokens.css. */
export function canvasColours(root: string): { light: string; dark: string } {
  const css = readFileSync(path.join(root, 'src/styles/tokens.css'), 'utf8')
  const pick = (block: string) => {
    const found = new RegExp(`(?:^|\\n)${block}\\s*\\{[^}]*?--app-canvas:\\s*([^;]+);`).exec(css)
    if (!found) throw new Error(`tokens.css has no --app-canvas in ${block}`)
    return oklchToHex(found[1])
  }
  return { light: pick(':root'), dark: pick('\\.dark') }
}

export function pwaOptions(root: string): Partial<VitePWAOptions> {
  const { light } = canvasColours(root)
  return {
    strategies: 'generateSW',
    registerType: 'autoUpdate',
    injectRegister: false,        // src/main.ts registers /sw.js itself, in the production build only
    includeManifestIcons: false,  // the icons are not needed offline; only /assets/ is precached
    manifest: {
      name: 'Djinn admin',
      short_name: 'Djinn',
      display: 'standalone',
      start_url: '/',
      scope: '/',
      theme_color: light,
      background_color: light,
      icons: [
        { src: '/icon-192.png', sizes: '192x192', type: 'image/png' },
        { src: '/icon-512.png', sizes: '512x512', type: 'image/png' },
      ],
    },
    integration: {
      // The plugin appends the web manifest to the precache list; nothing outside /assets/ belongs there.
      beforeBuildServiceWorker(options) {
        options.workbox.additionalManifestEntries = []
      },
    },
    workbox: {
      cacheId: CACHE_ID,
      globPatterns: ['assets/**/*'],
      navigateFallback: null,     // no app-shell fallback: `/` and every app route go to the network
      runtimeCaching: [],         // and no runtime route, so `/api/*` is never cached
      cleanupOutdatedCaches: true,
      skipWaiting: true,          // a new worker takes over at once, and claims the open page on first install
      clientsClaim: true,
      importScripts: ['sw-cleanup.js'],
    },
  }
}
