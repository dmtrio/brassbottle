import { describe, expect, it } from 'vitest'
import { broadcastBus, watchFreeze, webLocksElection } from './browser-link'
import { CHANNEL_NAME, LOCK_NAME } from './shared-link'

// Just enough of navigator.locks: exclusive, queued in order, held while the callback's promise is unsettled.
class FakeLocks {
  names: string[] = []
  private held = false
  private queue: (() => void)[] = []
  request(name: string, options: { signal: AbortSignal }, callback: () => Promise<void> | undefined): Promise<unknown> {
    this.names.push(name)
    return new Promise((resolve, reject) => {
      const run = () => {
        if (options.signal.aborted) return this.next()
        this.held = true
        void Promise.resolve(callback()).then(() => {
          this.held = false
          resolve(undefined)
          this.next()
        })
      }
      options.signal.addEventListener('abort', () => {
        const at = this.queue.indexOf(run)
        if (at >= 0) {
          this.queue.splice(at, 1)
          reject(new DOMException('aborted', 'AbortError'))
        }
      })
      if (this.held) this.queue.push(run)
      else run()
    })
  }
  private next(): void {
    this.queue.shift()?.()
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('webLocksElection', () => {
  it('is null where the browser has no Web Locks', () => {
    expect(webLocksElection(null)).toBeNull()
  })

  it('the first tab leads at once and holds the lock until it releases; the next then leads', async () => {
    const locks = new FakeLocks()
    const election = webLocksElection(locks)!
    const led: string[] = []
    const releaseA = election.acquire(() => led.push('a'))
    election.acquire(() => led.push('b'))
    await settle()
    expect(led).toEqual(['a'])
    expect(locks.names).toEqual([LOCK_NAME, LOCK_NAME])
    releaseA()
    await settle()
    expect(led).toEqual(['a', 'b'])
  })

  it('a tab that stops while waiting never leads and does not block the one behind it', async () => {
    const locks = new FakeLocks()
    const election = webLocksElection(locks)!
    const led: string[] = []
    const releaseA = election.acquire(() => led.push('a'))
    const releaseB = election.acquire(() => led.push('b'))
    election.acquire(() => led.push('c'))
    await settle()
    releaseB()
    releaseA()
    await settle()
    expect(led).toEqual(['a', 'c'])
  })
})

describe('broadcastBus', () => {
  it('is null without BroadcastChannel, and when the constructor throws', () => {
    expect(broadcastBus(null)).toBeNull()
    const throwing = class {
      constructor() {
        throw new Error('nope')
      }
    } as unknown as typeof BroadcastChannel
    expect(broadcastBus(throwing)).toBeNull()
  })

  it('posts on the named channel, hands each incoming message to the listener and closes', () => {
    const made: FakeChannel[] = []
    class FakeChannel {
      name: string
      posted: unknown[] = []
      closed = false
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(name: string) {
        this.name = name
        made.push(this)
      }
      postMessage(message: unknown): void {
        this.posted.push(message)
      }
      close(): void {
        this.closed = true
      }
    }
    const bus = broadcastBus(FakeChannel as unknown as typeof BroadcastChannel)!
    const heard: unknown[] = []
    bus.listen((raw) => heard.push(raw))
    bus.post({ kind: 'hello', from: 'x' })
    made[0].onmessage?.({ data: { kind: 'hello', from: 'y' } } as MessageEvent)
    bus.close()
    expect(made[0].name).toBe(CHANNEL_NAME)
    expect(made[0].posted).toEqual([{ kind: 'hello', from: 'x' }])
    expect(heard).toEqual([{ kind: 'hello', from: 'y' }])
    expect(made[0].closed).toBe(true)
  })
})

describe('watchFreeze', () => {
  it('calls back on freeze and on resume, and stops when told', () => {
    const page = new EventTarget()
    const seen: string[] = []
    const stop = watchFreeze(page, () => seen.push('freeze'), () => seen.push('resume'))
    page.dispatchEvent(new Event('freeze'))
    page.dispatchEvent(new Event('resume'))
    page.dispatchEvent(new Event('visibilitychange'))
    stop()
    page.dispatchEvent(new Event('freeze'))
    expect(seen).toEqual(['freeze', 'resume'])
  })
})
