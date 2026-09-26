import { describe, expect, it } from 'vitest'
import type { DecidedRow, OpenRow } from '@/types'
import { denylistHitTotal, filterOpen, filtersActive, isDenylistHit, NO_FILTERS, type QueueFilters } from './queue'

const NOW = Date.parse('2026-01-01T12:00:00Z')

function open(id: string, container: string, host: string, ageSeconds: number, failed = false): OpenRow {
  return {
    request_id: id,
    container,
    host,
    port: 443,
    host_is_ip: false,
    opened_at: new Date(NOW - ageSeconds * 1000).toISOString(),
    age_seconds: ageSeconds,
    hit_count: 1,
    uid: null,
    comm: null,
    reason: null,
    attempt: failed ? 1 : 0,
    last_error: failed ? { reason: 'apply_failed', attempt: 1, at: '2026-01-01T11:59:00Z' } : null,
  }
}

const ROWS = [
  open('a', 'alpha', 'one.example.com', 60),
  open('b', 'alpha', 'two.example.com', 299, true),
  open('c', 'mid', 'three.example.com', 300),
  open('d', 'mid', 'four.example.com', 3599, true),
  open('e', 'zeta', 'five.example.org', 3600),
]
const ids = (filters: Partial<QueueFilters>) => filterOpen(ROWS, { ...NO_FILTERS, ...filters }, NOW).map((r) => r.request_id)

describe('filterOpen', () => {
  it('keeps every row with no filter', () => {
    expect(ids({})).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('narrows to the chosen bottles', () => {
    expect(ids({ bottles: ['mid'] })).toEqual(['c', 'd'])
    expect(ids({ bottles: ['alpha', 'zeta'] })).toEqual(['a', 'b', 'e'])
  })

  it('narrows to failed applies by last_error, whatever its reason', () => {
    expect(ids({ state: 'failed' })).toEqual(['b', 'd'])
  })

  it('puts the age edges 5 minutes and 1 hour on the older side of each bucket', () => {
    expect(ids({ age: '5m' })).toEqual(['a', 'b'])
    expect(ids({ age: '1h' })).toEqual(['a', 'b', 'c', 'd'])
    expect(ids({ age: 'old' })).toEqual(['e'])
  })

  it('searches host:port case-insensitively, ignoring surrounding space', () => {
    expect(ids({ search: '  EXAMPLE.ORG ' })).toEqual(['e'])
    expect(ids({ search: 'three.example.com:443' })).toEqual(['c'])
    expect(ids({ search: ':8443' })).toEqual([])
  })

  it('applies every filter together', () => {
    expect(ids({ bottles: ['mid'], state: 'failed', age: '1h', search: 'four' })).toEqual(['d'])
    expect(ids({ bottles: ['alpha'], state: 'failed', age: 'old' })).toEqual([])
  })

  it('reads a row opened in the future as age 0', () => {
    const future = open('f', 'alpha', 'future.example.com', -30)
    expect(filterOpen([future], { ...NO_FILTERS, age: '5m' }, NOW)).toHaveLength(1)
  })
})

describe('filtersActive', () => {
  it('is false for the defaults and for whitespace-only search', () => {
    expect(filtersActive(NO_FILTERS)).toBe(false)
    expect(filtersActive({ ...NO_FILTERS, search: '   ' })).toBe(false)
  })

  it('is true when any one filter is set', () => {
    expect(filtersActive({ ...NO_FILTERS, bottles: ['a'] })).toBe(true)
    expect(filtersActive({ ...NO_FILTERS, state: 'failed' })).toBe(true)
    expect(filtersActive({ ...NO_FILTERS, age: 'old' })).toBe(true)
    expect(filtersActive({ ...NO_FILTERS, search: 'x' })).toBe(true)
  })
})

function decided(status: DecidedRow['status'], by: DecidedRow['decided_by'], hits: number): DecidedRow {
  return {
    request_id: `${status}-${by}-${hits}`,
    container: 'alpha',
    host: 'h.example.com',
    port: 443,
    status,
    scope: null,
    decided_at: '2026-01-01T11:00:00Z',
    decided_by: by,
    apply_status: null,
    deny_reason: null,
    hit_count: hits,
  }
}

describe('denylist hits', () => {
  it('are the denied rows the denylist decided, nothing else', () => {
    expect(isDenylistHit(decided('denied', 'denylist', 1))).toBe(true)
    expect(isDenylistHit(decided('denied', 'operator', 1))).toBe(false)
    expect(isDenylistHit(decided('allowed', 'operator', 1))).toBe(false)
    expect(isDenylistHit(decided('stale', 'sweep', 1))).toBe(false)
  })

  it('sum their hit counts', () => {
    expect(denylistHitTotal([decided('denied', 'denylist', 4), decided('denied', 'denylist', 7)])).toBe(11)
    expect(denylistHitTotal([])).toBe(0)
  })
})
