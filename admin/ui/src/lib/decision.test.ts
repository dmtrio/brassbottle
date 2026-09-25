import { describe, expect, it } from 'vitest'
import { relTime } from './decision'

const T0 = '2026-01-01T00:00:00Z'
const at = (seconds: number) => Date.parse(T0) + seconds * 1000

describe('relTime', () => {
  it('reads the given clock, not the wall clock', () => {
    expect(relTime(T0, at(12 * 60))).toBe('12m ago')
  })

  it('uses seconds under a minute, minutes under an hour, hours under a day, then days', () => {
    expect(relTime(T0, at(59))).toBe('59s ago')
    expect(relTime(T0, at(60))).toBe('1m ago')
    expect(relTime(T0, at(3599))).toBe('59m ago')
    expect(relTime(T0, at(3600))).toBe('1h ago')
    expect(relTime(T0, at(86399))).toBe('23h ago')
    expect(relTime(T0, at(86400))).toBe('1d ago')
  })

  it('never reads a negative age for a timestamp ahead of the clock', () => {
    expect(relTime(T0, at(-30))).toBe('0s ago')
  })
})
