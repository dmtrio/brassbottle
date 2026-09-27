import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { canvasColours, oklchToHex, pwaOptions, themeColorTags } from './pwa.ts'

describe('oklchToHex', () => {
  it('maps the ends of the lightness scale', () => {
    expect(oklchToHex('oklch(1 0 0)')).toBe('#ffffff')
    expect(oklchToHex('oklch(0 0 0)')).toBe('#000000')
  })
  it('reads a token value with an alpha part', () => {
    expect(oklchToHex('oklch(0.985 0.001 286 / 1)')).toBe('#fafafb')
  })
  it('refuses a colour it cannot read', () => {
    expect(() => oklchToHex('#fff')).toThrow(/not an oklch colour/)
  })
})

describe('the manifest colours', () => {
  const root = import.meta.dirname
  it('come from --app-canvas in tokens.css, light and dark', () => {
    expect(canvasColours(root)).toEqual({ light: '#fafafb', dark: '#0f0f12' })
  })
  it('theme the installed app with the light canvas', () => {
    const manifest = pwaOptions(root).manifest as { theme_color: string; background_color: string; display: string }
    expect(manifest.theme_color).toBe('#fafafb')
    expect(manifest.background_color).toBe('#fafafb')
    expect(manifest.display).toBe('standalone')
  })
})

describe('the manifest icons', () => {
  const manifest = pwaOptions(import.meta.dirname).manifest as { icons: unknown[] }
  it('are the two rounded tiles, "any", and the full-bleed maskable one', () => {
    expect(manifest.icons).toEqual([
      { src: '/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ])
  })
})

describe('the theme-color metas', () => {
  it('are one per scheme, valued from the canvas tokens, in the head', () => {
    expect(themeColorTags(import.meta.dirname)).toEqual([
      { tag: 'meta', injectTo: 'head',
        attrs: { name: 'theme-color', media: '(prefers-color-scheme: light)', content: '#fafafb', 'data-scheme': 'light' } },
      { tag: 'meta', injectTo: 'head',
        attrs: { name: 'theme-color', media: '(prefers-color-scheme: dark)', content: '#0f0f12', 'data-scheme': 'dark' } },
    ])
  })
})

describe('the worker options', () => {
  const workbox = pwaOptions(path.resolve(import.meta.dirname)).workbox!
  it('precache /assets/ only', () => {
    expect(workbox.globPatterns).toEqual(['assets/**/*'])
  })
  it('have no navigation fallback and no runtime caching', () => {
    expect(workbox.navigateFallback).toBeNull()
    expect(workbox.runtimeCaching).toEqual([])
  })
})
