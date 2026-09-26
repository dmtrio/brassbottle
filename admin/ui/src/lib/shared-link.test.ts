import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { QueueSnapshot } from '@/contract'
import {
  FOLLOWER_SILENCE_MS,
  HELLO_WAIT_MS,
  createSharedLink,
  parseMessage,
  type Bus,
  type Election,
  type Message,
} from './shared-link'
import { HIDDEN_POLL_MS, POLL_MS, SILENCE_LIMIT_MS, type LinkState, type StreamSource } from './stream'

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
const frame = (count: number) => JSON.stringify(snapshot(count))

// The browser: one lock every tab queues for, and one channel every tab hears (except the sender).
class Browser {
  sources: FakeSource[] = []
  private buses: { owner: number; listener: (raw: unknown) => void }[] = []
  private waiting: { tab: number; onLead: () => void }[] = []
  holder: number | null = null
  // tabs whose posts nobody hears (a leader frozen or cut off from the channel)
  muted = new Set<number>()
  posted: Message[] = []
  nextTab = 0

  release(tab: number): void {
    if (this.holder === tab) this.holder = null
    this.waiting = this.waiting.filter((w) => w.tab !== tab)
    const next = this.waiting.shift()
    if (next && this.holder === null) {
      this.holder = next.tab
      next.onLead()
    }
  }

  election(tab: number): Election {
    return {
      acquire: (onLead) => {
        this.waiting.push({ tab, onLead })
        if (this.holder === null) this.release(-1)
        return () => this.release(tab)
      },
    }
  }

  bus(tab: number): Bus {
    const entry = { owner: tab, listener: (() => {}) as (raw: unknown) => void }
    this.buses.push(entry)
    return {
      post: (message) => {
        if (this.muted.has(tab)) return
        this.posted.push(message)
        for (const other of this.buses) if (other.owner !== tab) other.listener(structuredClone(message))
      },
      listen: (listener) => {
        entry.listener = listener
      },
      close: () => {
        this.buses = this.buses.filter((b) => b !== entry)
      },
    }
  }
}

// Run the clock `ms` forward while the tab's stream keeps sending its heartbeat every 10 s (as a live one does).
function keepAlive(tab: ReturnType<typeof openTab>, ms: number): void {
  let left = ms
  while (left > 0) {
    const step = Math.min(left, 10_000)
    vi.advanceTimersByTime(step)
    left -= step
    if (step === 10_000) tab.sources.at(-1)?.emit('hb')
  }
}

type TabOptions = { locks?: boolean; hidden?: boolean }

function openTab(browser: Browser, options: TabOptions = {}) {
  const tab = browser.nextTab++
  const log: string[] = []
  const snapshots: number[] = []
  const states: LinkState[] = []
  const sources: FakeSource[] = []
  const env = { hidden: options.hidden ?? false }
  const link = createSharedLink({
    id: `tab${tab}`,
    election: options.locks === false ? null : browser.election(tab),
    openBus: () => (options.locks === false ? null : browser.bus(tab)),
    open: () => {
      const source = new FakeSource()
      sources.push(source)
      browser.sources.push(source)
      return source
    },
    hidden: () => env.hidden,
    onSnapshot: (s) => snapshots.push(s.count),
    onState: (s) => states.push(s),
    startPolling: (ms) => log.push(`poll-start:${ms}`),
    stopPolling: () => log.push('poll-stop'),
    log: () => {},
  })
  return { link, log, snapshots, states, sources, env }
}

describe('createSharedLink: election', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('the first tab leads: it opens the stream and reads the queue at once', () => {
    const browser = new Browser()
    const a = openTab(browser)
    a.link.start()
    expect(a.link.role()).toBe('leader')
    expect(a.sources).toHaveLength(1)
    expect(a.log).toEqual([`poll-start:${POLL_MS}`])
    expect(a.link.state()).toBe('connecting')
  })

  it('later tabs follow: no stream, no read, and they take the leader state and snapshots as they come', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    a.sources[0].emit('queue', frame(3))
    b.link.start()
    expect(b.link.role()).toBe('follower')
    expect(b.sources).toHaveLength(0)
    // b's hello was answered with the leader's state and its last snapshot
    expect(b.link.state()).toBe('live')
    expect(b.snapshots).toEqual([3])
    a.sources[0].emit('queue', frame(4))
    expect(b.snapshots).toEqual([3, 4])
    expect(b.log).toEqual([])
    vi.advanceTimersByTime(FOLLOWER_SILENCE_MS - 1) // a follower's watch is on the clock, not a poll
    expect(b.log).toEqual([])
  })

  it('a leader that has no snapshot yet answers a hello with its state only', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    expect(b.link.state()).toBe('connecting')
    expect(b.snapshots).toEqual([])
    vi.advanceTimersByTime(HELLO_WAIT_MS + 1) // it was heard, so no early poll
    expect(b.log).toEqual([])
  })

  it('a snapshot the leader read by polling reaches the followers through publish', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    a.link.publish(snapshot(7))
    expect(b.snapshots).toEqual([7])
    b.link.publish(snapshot(8)) // a follower's own read is its own
    expect(a.snapshots).toEqual([])
  })

  it('the leader keeps its stream whatever its visibility, and a follower hidden or shown does nothing', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    a.sources[0].emit('queue', frame(1))
    a.env.hidden = true
    b.env.hidden = true
    a.link.visibilityChanged()
    b.link.visibilityChanged()
    expect(a.sources[0].closed).toBe(false)
    expect(a.link.state()).toBe('live')
    expect(b.link.state()).toBe('live')
    expect(b.log).toEqual([])
    expect(a.log).not.toContain(`poll-start:${HIDDEN_POLL_MS}`)
  })

  it('the leader\'s link changes reach the followers, and a follower shows them', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    a.sources[0].emit('queue', frame(1))
    a.sources[0].emit('error')
    expect(a.link.state()).toBe('reconnecting')
    expect(b.link.state()).toBe('reconnecting')
    expect(b.log).toEqual([]) // the leader polls; the follower does not
    vi.advanceTimersByTime(3000)
    a.sources[1].emit('queue', frame(2))
    expect(b.states).toEqual(['live', 'reconnecting', 'live'])
    expect(b.snapshots).toEqual([1, 2])
  })

  it('when the leader closes the lock passes to the next tab, which starts over as Connecting and the other follows it', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    const c = openTab(browser)
    a.link.start()
    b.link.start()
    c.link.start()
    a.sources[0].emit('queue', frame(1))
    expect(c.link.state()).toBe('live')
    a.link.stop()
    expect(a.sources[0].closed).toBe(true)
    expect(b.link.role()).toBe('leader')
    expect(c.link.role()).toBe('follower')
    expect(b.sources).toHaveLength(1)
    expect(b.link.state()).toBe('connecting')
    expect(c.link.state()).toBe('connecting')
    expect(b.log).toEqual([`poll-start:${POLL_MS}`])
    b.sources[0].emit('queue', frame(5))
    expect(c.link.state()).toBe('live')
    expect(c.snapshots).toEqual([1, 5])
    expect(c.sources).toHaveLength(0)
  })

  it('a tab that follows and then leads stops any polling of its own', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    vi.advanceTimersByTime(HELLO_WAIT_MS + 1)
    a.link.stop()
    expect(b.link.role()).toBe('leader')
    expect(b.sources).toHaveLength(1)
  })

  it('stop releases the lock and cancels the watch, so a tab that was still waiting never leads', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    b.link.start()
    b.link.stop()
    expect(vi.getTimerCount()).toBeLessThanOrEqual(1) // only a's own watchdog
    a.link.stop()
    expect(vi.getTimerCount()).toBe(0)
    expect(b.sources).toHaveLength(0)
    expect(b.link.role()).toBe('follower')
  })
})

describe('createSharedLink: a leader that goes quiet', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a follower polls and says Polling after the silence limit and a margin; any message from the leader ends that', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    a.sources[0].emit('queue', frame(1))
    b.link.start()
    expect(FOLLOWER_SILENCE_MS).toBeGreaterThanOrEqual(SILENCE_LIMIT_MS)
    browser.muted.add(0) // a leader whose relays nobody hears
    keepAlive(a, FOLLOWER_SILENCE_MS - 1)
    expect(b.log).toEqual([])
    keepAlive(a, 1)
    expect(b.link.state()).toBe('polling')
    expect(b.log).toEqual([`poll-start:${POLL_MS}`])
    browser.muted.clear()
    a.sources[0].emit('hb')
    expect(b.link.state()).toBe('live')
    expect(b.log).toEqual([`poll-start:${POLL_MS}`, 'poll-stop'])
    // and the watch is armed again
    browser.muted.add(0)
    keepAlive(a, FOLLOWER_SILENCE_MS)
    expect(b.link.state()).toBe('polling')
  })

  it('a leader\'s heartbeats, relayed every 15 s, keep a follower quiet however long the queue is unchanged', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    a.sources[0].emit('queue', frame(1))
    b.link.start()
    for (let i = 0; i < 10; i++) {
      vi.advanceTimersByTime(15_000)
      a.sources[0].emit('hb')
    }
    expect(b.link.state()).toBe('live')
    expect(b.log).toEqual([])
  })

  it('a timer that fires late in a background tab does not call a recently heard leader silent', () => {
    const browser = new Browser()
    const a = openTab(browser)
    const b = openTab(browser)
    a.link.start()
    a.sources[0].emit('queue', frame(1))
    b.link.start()
    browser.muted.add(0)
    keepAlive(a, FOLLOWER_SILENCE_MS - 1000)
    browser.muted.clear()
    a.sources.at(-1)?.emit('hb') // heard 1 s before the (already armed) watch fires
    vi.advanceTimersByTime(1000)
    expect(b.link.state()).toBe('live')
    expect(b.log).toEqual([])
    browser.muted.add(0)
    keepAlive(a, FOLLOWER_SILENCE_MS - 1000)
    expect(b.log).toEqual([`poll-start:${POLL_MS}`])
  })

  it('a follower that hears no answer to its hello within a few seconds reads the queue itself', () => {
    const browser = new Browser()
    browser.holder = 99 // some tab holds the lock and never answers
    const c = openTab(browser)
    c.link.start()
    expect(c.link.role()).toBe('follower')
    vi.advanceTimersByTime(HELLO_WAIT_MS - 1)
    expect(c.log).toEqual([])
    vi.advanceTimersByTime(1)
    expect(c.link.state()).toBe('polling')
    expect(c.log).toEqual([`poll-start:${POLL_MS}`])
  })
})

describe('createSharedLink: solo (no Web Locks or BroadcastChannel)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('behaves as the per-tab link: its own stream, closed while hidden, reopened when shown', () => {
    const browser = new Browser()
    const a = openTab(browser, { locks: false })
    const b = openTab(browser, { locks: false })
    a.link.start()
    b.link.start()
    expect(a.link.role()).toBe('solo')
    expect(a.sources).toHaveLength(1)
    expect(b.sources).toHaveLength(1)
    a.sources[0].emit('queue', frame(1))
    expect(a.link.state()).toBe('live')
    a.env.hidden = true
    a.link.visibilityChanged()
    expect(a.sources[0].closed).toBe(true)
    expect(a.link.state()).toBe('paused')
    expect(a.log.at(-1)).toBe(`poll-start:${HIDDEN_POLL_MS}`)
    a.env.hidden = false
    a.link.visibilityChanged()
    expect(a.sources).toHaveLength(2)
    expect(a.link.state()).toBe('connecting')
    expect(b.snapshots).toEqual([])
    expect(browser.posted).toEqual([])
  })

  it('a tab that starts hidden is solo and Paused', () => {
    const browser = new Browser()
    const a = openTab(browser, { locks: false, hidden: true })
    a.link.start()
    expect(a.link.state()).toBe('paused')
    expect(a.sources).toHaveLength(0)
  })
})

describe('parseMessage', () => {
  it('accepts the four kinds and nothing else', () => {
    expect(parseMessage({ kind: 'hello', from: 'x' })).toEqual({ kind: 'hello', from: 'x' })
    expect(parseMessage({ kind: 'link', from: 'x', state: 'live' })).toEqual({ kind: 'link', from: 'x', state: 'live' })
    expect(parseMessage({ kind: 'hb', from: 'x', state: 'polling' })).toEqual({ kind: 'hb', from: 'x', state: 'polling' })
    expect(parseMessage({ kind: 'snapshot', from: 'x', state: 'live', snapshot: snapshot(1) })?.kind).toBe('snapshot')
    for (const bad of [
      null,
      'live',
      {},
      { kind: 'hello' },
      { kind: 'link', from: 'x' },
      { kind: 'link', from: 'x', state: 'nope' },
      { kind: 'snapshot', from: 'x', state: 'live', snapshot: { nope: true } },
      { kind: 'snapshot', from: 'x', state: 'live' },
      { kind: 'other', from: 'x', state: 'live' },
    ]) {
      expect(parseMessage(bad)).toBeNull()
    }
  })

  it('a tab ignores a message that is not ours, and its own', () => {
    vi.useFakeTimers()
    try {
      const browser = new Browser()
      const a = openTab(browser)
      const b = openTab(browser)
      a.link.start()
      b.link.start()
      const bus = browser.bus(99)
      bus.post({ kind: 'snapshot', from: 'x', state: 'live', snapshot: { open: 'no' } } as unknown as Message)
      expect(b.snapshots).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })
})
