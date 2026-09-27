import { describe, expect, it } from 'vitest'
import { syncThemeColor } from './theme-color'

function meta(scheme: string, content: string) {
  const attrs: Record<string, string> = { content }
  return {
    dataset: { scheme },
    get content() { return attrs.content },
    setAttribute(name: string, value: string) { attrs[name] = value },
  }
}
const page = () => {
  const metas = [meta('light', '#fafafb'), meta('dark', '#0f0f12')]
  return { metas, querySelectorAll: () => metas }
}

describe('syncThemeColor', () => {
  it('points both metas at the dark colour, then back at the light one', () => {
    const doc = page()
    syncThemeColor(doc, 'dark')
    expect(doc.metas.map((m) => m.content)).toEqual(['#0f0f12', '#0f0f12'])
    syncThemeColor(doc, 'light')
    expect(doc.metas.map((m) => m.content)).toEqual(['#fafafb', '#fafafb'])
  })
  it('leaves a document without the build-made metas alone', () => {
    expect(() => syncThemeColor({ querySelectorAll: () => [] }, 'dark')).not.toThrow()
  })
})
