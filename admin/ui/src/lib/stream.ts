import type { QueueSnapshot } from '@/contract'

// How the queue reaches the tab: 'live' while the server-sent stream delivers
// it, 'reconnecting' from the moment a live stream drops until its reopen has
// been tried, 'polling' when the stream is not available (never opened, refused,
// or a reopen failed). Polling `/api/egress/queue` runs in the last two.
export type LinkState = 'live' | 'reconnecting' | 'polling'

export const STREAM_URL = '/api/egress/stream'
// A dropped live stream is reopened once quickly (a daemon restart or a blip);
// after a failed reopen, or a refusal (all eight slots taken, broker down), the
// tab keeps polling and looks again less often.
export const REOPEN_AFTER_DROP_MS = 3000
export const REOPEN_AFTER_FAILURE_MS = 10000

// The slice of EventSource the link uses, so a test can stand in for it.
export type StreamSource = {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export type LinkDeps = {
  // null: this browser has no EventSource, so the tab only polls.
  open: () => StreamSource | null
  onSnapshot: (snapshot: QueueSnapshot) => void
  onState: (state: LinkState) => void
  startPolling: () => void
  stopPolling: () => void
}

function parseSnapshot(raw: unknown): QueueSnapshot | null {
  if (typeof raw !== 'string') return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed === 'object' && parsed !== null && Array.isArray((parsed as QueueSnapshot).open)) {
      return parsed as QueueSnapshot
    }
  } catch {
    // a frame that is not JSON is dropped; the next one replaces it
  }
  return null
}

// The stream/poll state machine. The stream is preferred: while a frame has
// arrived and the stream stays up there is no polling. Any error closes the
// source (the browser's own retry is not used, so the timing is ours), starts
// polling at once, and schedules a reopen.
export function createLiveLink(deps: LinkDeps) {
  let source: StreamSource | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let state: LinkState = 'reconnecting' // connecting for the first time reads as reconnecting
  let running = false

  function set(next: LinkState): void {
    if (next === state) return
    state = next
    deps.onState(next)
  }

  function drop(dead: StreamSource): void {
    if (dead !== source) return
    dead.close()
    source = null
    const wasLive = state === 'live'
    set(wasLive ? 'reconnecting' : 'polling')
    deps.startPolling()
    schedule(wasLive ? REOPEN_AFTER_DROP_MS : REOPEN_AFTER_FAILURE_MS)
  }

  function schedule(ms: number): void {
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      connect()
    }, ms)
  }

  function connect(): void {
    if (!running) return
    const next = deps.open()
    if (next === null) {
      set('polling')
      deps.startPolling()
      return
    }
    source = next
    next.addEventListener('queue', (event) => {
      if (next !== source) return
      const snapshot = parseSnapshot(event.data)
      if (snapshot === null) return
      if (state !== 'live') {
        set('live')
        deps.stopPolling()
      }
      deps.onSnapshot(snapshot)
    })
    next.addEventListener('error', () => drop(next))
  }

  return {
    state: () => state,
    start(): void {
      running = true
      connect()
    },
    stop(): void {
      running = false
      if (timer !== null) clearTimeout(timer)
      timer = null
      if (source !== null) source.close()
      source = null
      state = 'reconnecting'
    },
  }
}
