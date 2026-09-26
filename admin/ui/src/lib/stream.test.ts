import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { QueueSnapshot } from '@/contract'
import {
  REOPEN_AFTER_DROP_MS,
  REOPEN_AFTER_FAILURE_MS,
  createLiveLink,
  type LinkState,
  type StreamSource,
} from './stream'

class FakeSource implements StreamSource {
  closed = false
  private listeners: Record<string, ((event: MessageEvent) => void)[]> = {}
  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    ;(this.listeners[type] ??= []).push(listener)
  }
  close(): void {
    this.closed = true
  }
  emit(type: string, data?: unknown): void {
    for (const listener of this.listeners[type] ?? []) listener({ data } as MessageEvent)
  }
}

const snapshot = (count: number): QueueSnapshot => ({ open: [], count, recent: [], generated_at: 'now' })

function harness(supported = true) {
  const sources: FakeSource[] = []
  const log: string[] = []
  const snapshots: number[] = []
  const states: LinkState[] = []
  const link = createLiveLink({
    open: () => {
      if (!supported) return null
      const source = new FakeSource()
      sources.push(source)
      return source
    },
    onSnapshot: (s) => snapshots.push(s.count),
    onState: (s) => states.push(s),
    startPolling: () => log.push('poll-start'),
    stopPolling: () => log.push('poll-stop'),
  })
  return { link, sources, log, snapshots, states }
}

describe('createLiveLink', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('is live from its first frame, stops polling, and hands every frame on', () => {
    const h = harness()
    h.link.start()
    expect(h.link.state()).toBe('reconnecting')
    h.sources[0].emit('queue', JSON.stringify(snapshot(3)))
    h.sources[0].emit('queue', JSON.stringify(snapshot(4)))
    expect(h.link.state()).toBe('live')
    expect(h.snapshots).toEqual([3, 4])
    expect(h.log).toEqual(['poll-stop']) // never polled: the stream was there first
  })

  it('a stream that drops while live is reconnecting, polls at once, and reopens after 3 s', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.sources[0].emit('error')
    expect(h.link.state()).toBe('reconnecting')
    expect(h.sources[0].closed).toBe(true)
    expect(h.log.at(-1)).toBe('poll-start')
    vi.advanceTimersByTime(REOPEN_AFTER_DROP_MS - 1)
    expect(h.sources).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(h.sources).toHaveLength(2)
    h.sources[1].emit('queue', JSON.stringify(snapshot(2)))
    expect(h.link.state()).toBe('live')
    expect(h.log.at(-1)).toBe('poll-stop')
  })

  it('a failed reopen is polling, and the next look is 10 s later', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.sources[0].emit('error')
    vi.advanceTimersByTime(REOPEN_AFTER_DROP_MS)
    h.sources[1].emit('error')
    expect(h.link.state()).toBe('polling')
    vi.advanceTimersByTime(REOPEN_AFTER_FAILURE_MS - 1)
    expect(h.sources).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(h.sources).toHaveLength(3)
  })

  it('a stream refused on the first try (the ninth, broker down) is polling', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('error')
    expect(h.link.state()).toBe('polling')
    expect(h.log).toEqual(['poll-start'])
  })

  it('no EventSource in the browser: polling, no timer, nothing to reopen', () => {
    const h = harness(false)
    h.link.start()
    expect(h.link.state()).toBe('polling')
    vi.advanceTimersByTime(60_000)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('ignores a frame that is not a queue snapshot, and events from a source it dropped', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', 'not json')
    h.sources[0].emit('queue', JSON.stringify({ nope: true }))
    expect(h.link.state()).toBe('reconnecting')
    expect(h.snapshots).toEqual([])
    h.sources[0].emit('error')
    h.sources[0].emit('queue', JSON.stringify(snapshot(9)))
    expect(h.snapshots).toEqual([])
  })

  it('stop closes the source and cancels the reopen', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.sources[0].emit('error')
    h.link.stop()
    vi.advanceTimersByTime(60_000)
    expect(h.sources).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })
})
