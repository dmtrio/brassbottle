import { CHANNEL_NAME, LOCK_NAME, type Bus, type Election } from '@/lib/shared-link'

// The browser's side of shared-link.ts: the Web Locks API as the election and a BroadcastChannel as
// the bus. Each is null where the browser lacks it (`navigator.locks` exists only in a secure
// context: https, or localhost), and the tab then keeps a stream of its own.

type LockManagerLike = {
  request: (name: string, options: { signal: AbortSignal }, callback: () => Promise<void> | undefined) => Promise<unknown>
}

export function webLocksElection(locks: LockManagerLike | null | undefined = globalThis.navigator?.locks): Election | null {
  if (!locks || typeof locks.request !== 'function') return null
  return {
    acquire(onLead) {
      const pending = new AbortController()
      let cancelled = false
      let free: () => void = () => {}
      const held = new Promise<void>((resolve) => {
        free = resolve
      })
      // The lock is held while the callback's promise is unsettled: until `release`, or the page unloads.
      locks
        .request(LOCK_NAME, { signal: pending.signal }, () => {
          if (cancelled) return undefined
          onLead()
          return held
        })
        .catch(() => {
          // aborted while waiting: this tab stopped before it was ever the leader
        })
      return () => {
        cancelled = true
        pending.abort()
        free()
      }
    },
  }
}

export function broadcastBus(
  Channel: typeof BroadcastChannel | null | undefined = globalThis.BroadcastChannel,
): Bus | null {
  if (typeof Channel !== 'function') return null
  try {
    const channel = new Channel(CHANNEL_NAME)
    return {
      post: (message) => channel.postMessage(message),
      listen: (listener) => {
        channel.onmessage = (event: MessageEvent) => listener(event.data)
      },
      close: () => channel.close(),
    }
  } catch {
    return null
  }
}
