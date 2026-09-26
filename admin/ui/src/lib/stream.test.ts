import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { QueueSnapshot } from '@/contract'
import {
  HIDDEN_POLL_MS,
  POLL_MS,
  REOPEN_AFTER_DROP_MS,
  REOPEN_AFTER_FAILURE_MS,
  SILENCE_LIMIT_MS,
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
const POLL = `poll-start:${POLL_MS}`
const SLOW = `poll-start:${HIDDEN_POLL_MS}`

function harness(supported = true, hidden = false) {
  const sources: FakeSource[] = []
  const log: string[] = []
  const snapshots: number[] = []
  const states: LinkState[] = []
  const tab = { hidden }
  const link = createLiveLink({
    open: () => {
      if (!supported) return null
      const source = new FakeSource()
      sources.push(source)
      return source
    },
    hidden: () => tab.hidden,
    onSnapshot: (s) => snapshots.push(s.count),
    onState: (s) => states.push(s),
    startPolling: (ms) => log.push(`poll-start:${ms}`),
    stopPolling: () => log.push('poll-stop'),
  })
  return { link, sources, log, snapshots, states, tab }
}

describe('createLiveLink', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('the first open is connecting and reads the queue at once; the first frame is live and stops the read', () => {
    const h = harness()
    h.link.start()
    expect(h.link.state()).toBe('connecting')
    expect(h.log).toEqual([POLL])
    h.sources[0].emit('queue', JSON.stringify(snapshot(3)))
    h.sources[0].emit('queue', JSON.stringify(snapshot(4)))
    expect(h.link.state()).toBe('live')
    expect(h.snapshots).toEqual([3, 4])
    expect(h.log).toEqual([POLL, 'poll-stop'])
    expect(h.states).toEqual(['live'])
  })

  it('a stream that drops while live is reconnecting, polls at once, and reopens after 3 s', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.sources[0].emit('error')
    expect(h.link.state()).toBe('reconnecting')
    expect(h.sources[0].closed).toBe(true)
    expect(h.log.at(-1)).toBe(POLL)
    vi.advanceTimersByTime(REOPEN_AFTER_DROP_MS - 1)
    expect(h.sources).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(h.sources).toHaveLength(2)
    expect(h.link.state()).toBe('reconnecting')
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
    expect(h.log).toEqual([POLL, POLL]) // the poll of the first open carries on
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
    expect(h.link.state()).toBe('connecting')
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

describe('createLiveLink in a hidden tab', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a tab that starts hidden opens no stream and reads the queue slowly', () => {
    const h = harness(true, true)
    h.link.start()
    expect(h.link.state()).toBe('paused')
    expect(h.sources).toHaveLength(0)
    expect(h.log).toEqual([SLOW])
    vi.advanceTimersByTime(60_000)
    expect(h.sources).toHaveLength(0)
  })

  it('hiding a live tab closes its stream, cancels every timer and reads slowly; showing it opens a stream and reads at once', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.tab.hidden = true
    h.link.visibilityChanged()
    expect(h.sources[0].closed).toBe(true)
    expect(h.link.state()).toBe('paused')
    expect(h.log.at(-1)).toBe(SLOW)
    expect(vi.getTimerCount()).toBe(0)
    h.sources[0].emit('queue', JSON.stringify(snapshot(5))) // a late frame from the closed source
    expect(h.snapshots).toEqual([1])
    h.tab.hidden = false
    h.link.visibilityChanged()
    expect(h.link.state()).toBe('connecting')
    expect(h.log.at(-1)).toBe(POLL)
    expect(h.sources).toHaveLength(2)
    h.sources[1].emit('queue', JSON.stringify(snapshot(2)))
    expect(h.link.state()).toBe('live')
    expect(h.log.at(-1)).toBe('poll-stop')
  })

  it('hiding a tab that is waiting to reopen cancels the reopen', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    h.sources[0].emit('error')
    h.tab.hidden = true
    h.link.visibilityChanged()
    vi.advanceTimersByTime(60_000)
    expect(h.sources).toHaveLength(1)
    expect(h.link.state()).toBe('paused')
  })

  it('a visibility change that changes nothing does nothing', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    const before = [...h.log]
    h.link.visibilityChanged() // still visible
    h.tab.hidden = true
    h.link.visibilityChanged()
    h.link.visibilityChanged() // still hidden
    expect(h.log).toEqual([...before, SLOW])
    h.link.stop()
    h.tab.hidden = false
    h.link.visibilityChanged() // stopped: nothing reopens
    expect(h.sources).toHaveLength(1)
  })
})

describe('createLiveLink heartbeat watchdog', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a stream that goes silent past the limit is dropped: reconnecting, polling at once, reopened after 3 s', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    vi.advanceTimersByTime(SILENCE_LIMIT_MS - 1)
    expect(h.link.state()).toBe('live')
    vi.advanceTimersByTime(1)
    expect(h.link.state()).toBe('reconnecting')
    expect(h.sources[0].closed).toBe(true)
    expect(h.log.at(-1)).toBe(POLL)
    vi.advanceTimersByTime(REOPEN_AFTER_DROP_MS)
    expect(h.sources).toHaveLength(2)
  })

  it('a heartbeat or a frame keeps the stream: each restarts the silence', () => {
    const h = harness()
    h.link.start()
    h.sources[0].emit('queue', JSON.stringify(snapshot(1)))
    for (let beat = 0; beat < 4; beat++) {
      vi.advanceTimersByTime(SILENCE_LIMIT_MS - 1000)
      h.sources[0].emit(beat % 2 === 0 ? 'hb' : 'queue', beat % 2 === 0 ? '{}' : JSON.stringify(snapshot(beat)))
    }
    expect(h.link.state()).toBe('live')
    expect(h.sources).toHaveLength(1)
    vi.advanceTimersByTime(SILENCE_LIMIT_MS)
    expect(h.link.state()).toBe('reconnecting')
  })

  it('a stream that never delivers a frame is dropped to polling; stop and hiding cancel the watchdog', () => {
    const h = harness()
    h.link.start()
    vi.advanceTimersByTime(SILENCE_LIMIT_MS)
    expect(h.link.state()).toBe('polling')
    h.link.stop()
    expect(vi.getTimerCount()).toBe(0)
    const hidden = harness()
    hidden.link.start()
    hidden.tab.hidden = true
    hidden.link.visibilityChanged()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('the limit is injectable', () => {
    const sources: FakeSource[] = []
    const states: LinkState[] = []
    const link = createLiveLink(
      {
        open: () => sources[sources.push(new FakeSource()) - 1],
        hidden: () => false,
        onSnapshot: () => {},
        onState: (s) => states.push(s),
        startPolling: () => {},
        stopPolling: () => {},
      },
      { silenceLimitMs: 100 },
    )
    link.start()
    sources[0].emit('queue', JSON.stringify(snapshot(1)))
    vi.advanceTimersByTime(100)
    expect(states).toEqual(['live', 'reconnecting'])
  })
})
