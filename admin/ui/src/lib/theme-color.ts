// Keeps the page's `theme-color` meta in step with the colour scheme in use.
//
// The build writes two metas into index.html, one per scheme, each valued from
// the canvas token (themeColorPlugin in pwa.ts) and tagged `data-scheme`. The
// browser picks by `prefers-color-scheme`, which is right until the in-app
// theme toggle disagrees with the OS. This sets both metas to the scheme the
// app is showing, so the title bar follows the toggle as well as the OS. The
// colours are read from the metas themselves, once, so none is written here.

export type Scheme = 'light' | 'dark'

interface MetaLike { dataset: { scheme?: string }; content: string; setAttribute(name: string, value: string): void }
interface DocumentLike { querySelectorAll(selector: string): ArrayLike<MetaLike> }

const SELECTOR = 'meta[name="theme-color"][data-scheme]'
const original = new WeakMap<object, Partial<Record<Scheme, string>>>()

/** Point every theme-color meta at `scheme`'s colour; a document with no build-made metas is left alone. */
export function syncThemeColor(doc: DocumentLike, scheme: Scheme): void {
  const metas = Array.from(doc.querySelectorAll(SELECTOR))
  if (metas.length === 0) return
  let colours = original.get(doc)
  if (!colours) {
    colours = {}
    for (const meta of metas) colours[meta.dataset.scheme as Scheme] = meta.content
    original.set(doc, colours)
  }
  const colour = colours[scheme]
  if (colour === undefined) return
  for (const meta of metas) meta.setAttribute('content', colour)
}
