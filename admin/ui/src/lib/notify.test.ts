import { describe, expect, it } from 'vitest'
import type { OpenRow } from '@/types'
import { bellState, notificationFor, SeenRequests } from './notify'

function row(id: string, container = 'alpha', host = `${id}.example.com`, port = 443): OpenRow {
  return {
    request_id: id, container, host, port, host_is_ip: false, opened_at: '2026-09-26T10:00:00Z',
    age_seconds: 0, hit_count: 1, uid: 1000, comm: null, reason: null, attempt: 0, last_error: null,
  }
}
const ids = (rows: OpenRow[]) => rows.map((r) => r.request_id)

describe('SeenRequests', () => {
  it('treats the first snapshot as already shown', () => {
    const seen = new SeenRequests()
    expect(seen.take([row('a'), row('b')])).toEqual([])
  })

  it('an empty first snapshot still seeds, so the first request after it is new', () => {
    const seen = new SeenRequests()
    expect(seen.take([])).toEqual([])
    expect(ids(seen.take([row('a')]))).toEqual(['a'])
  })

  it('returns exactly the ids new to the tab, once each, in snapshot order', () => {
    const seen = new SeenRequests()
    seen.take([row('a')])
    expect(ids(seen.take([row('a'), row('b'), row('c')]))).toEqual(['b', 'c'])
    expect(seen.take([row('a'), row('b'), row('c')])).toEqual([])
    expect(ids(seen.take([row('d'), row('a')]))).toEqual(['d'])
  })

  it('does not report a request that left and came back with the same id', () => {
    const seen = new SeenRequests()
    seen.take([row('a')])
    seen.take([row('a'), row('b')])
    seen.take([row('a')])
    expect(seen.take([row('a'), row('b')])).toEqual([])
  })

  it('does not report a first-snapshot request that left and came back', () => {
    const seen = new SeenRequests()
    seen.take([row('a')])
    seen.take([])
    expect(seen.take([row('a')])).toEqual([])
  })

  it('keeps separate tabs (instances) independent', () => {
    const one = new SeenRequests()
    const two = new SeenRequests()
    one.take([row('a')])
    two.take([])
    expect(ids(two.take([row('a')]))).toEqual(['a'])
  })
})

describe('bellState', () => {
  it('reads permission first, then the mute', () => {
    expect(bellState(null, false)).toBe('unsupported')
    expect(bellState(null, true)).toBe('unsupported')
    expect(bellState('default', true)).toBe('default')
    expect(bellState('denied', false)).toBe('denied')
    expect(bellState('denied', true)).toBe('denied')
    expect(bellState('granted', false)).toBe('on')
    expect(bellState('granted', true)).toBe('muted')
  })
})

describe('notificationFor', () => {
  it('names the bottle and the destination and tags with the request id', () => {
    expect(notificationFor(row('r7', 'zeta', 'registry.npmjs.org', 8443))).toEqual({
      title: 'New egress request from zeta',
      body: 'registry.npmjs.org:8443',
      tag: 'r7',
    })
  })
})
