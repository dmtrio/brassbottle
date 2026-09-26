import type { QueueSnapshot } from '@/contract'

// How the queue reaches the tab:
//  - 'connecting': the first open (or the return from hidden) has not delivered a frame yet;
//  - 'live': the server-sent stream delivers it and nothing polls;
//  - 'reconnecting': a live stream dropped and its reopen has not been tried yet;
//  - 'polling': the stream is not available (refused, or a reopen failed);
//  - 'paused': the tab is hidden, the stream is closed and the queue is read slowly.
// `/api/egress/queue` is polled in every state but 'live'.
export type LinkState = 'connecting' | 'live' | 'reconnecting' | 'polling' | 'paused'

export const STREAM_URL = '/api/egress/stream'
// A dropped live stream is reopened once quickly (a daemon restart or a blip);
// after a failed reopen, or a refusal (all eight slots taken, broker down), the
// tab keeps polling and looks again less often.
export const REOPEN_AFTER_DROP_MS = 3000
export const REOPEN_AFTER_FAILURE_MS = 10000
// How often the queue is read while the stream is not delivering, and while the tab is hidden
// (no stream then, but the notification bell still wants a snapshot now and then).
export const POLL_MS = 5000
export const HIDDEN_POLL_MS = 30000
// The daemon sends a heartbeat every 15 s; a stream that has said nothing (frame or heartbeat)
// for two of them and a margin is half-open and is dropped.
export const SILENCE_LIMIT_MS = 35000

// The slice of EventSource the link uses, so a test can stand in for it.
export type StreamSource = {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

export type LinkDeps = {
  // null: this browser has no EventSource, so the tab only polls.
  open: () => StreamSource | null
  // The tab is in the background: it must not hold a stream (a browser allows about six
  // connections to one origin, and every open tab's stream takes one).
  hidden: () => boolean
  onSnapshot: (snapshot: QueueSnapshot) => void
  onState: (state: LinkState) => void
  // Read the queue every `intervalMs`, once at once unless already polling at that interval.
  startPolling: (intervalMs: number) => void
  stopPolling: () => void
  // Every `hb` heartbeat of the live stream, so a leader tab can tell the tabs following it the stream is alive.
  onHeartbeat?: () => void
}

export type LinkOptions = {
  silenceLimitMs?: number
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

// The stream/poll state machine. The stream is preferred: once a frame has
// arrived and the stream stays up there is no polling. While it opens (the first
// time, or after the tab was hidden) the queue is read once so the list is never
// empty. Any error, or silence past the limit, closes the source (the browser's own
// retry is not used, so the timing is ours), polls at once, and schedules a reopen.
// A hidden tab holds no stream at all (unless the caller says it is never hidden: the leader tab of
// shared-link.ts holds the browser's one stream whatever its visibility).
export function createLiveLink(deps: LinkDeps, options: LinkOptions = {}) {
  const silenceLimitMs = options.silenceLimitMs ?? SILENCE_LIMIT_MS
  let source: StreamSource | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let watchdog: ReturnType<typeof setTimeout> | null = null
  let state: LinkState = 'connecting'
  let running = false

  function set(next: LinkState): void {
    if (next === state) return
    state = next
    deps.onState(next)
  }

  function clearTimers(): void {
    if (timer !== null) clearTimeout(timer)
    if (watchdog !== null) clearTimeout(watchdog)
    timer = watchdog = null
  }

  function closeSource(): void {
    clearTimers()
    if (source !== null) source.close()
    source = null
  }

  function drop(dead: StreamSource): void {
    if (dead !== source) return
    const wasLive = state === 'live'
    closeSource()
    set(wasLive ? 'reconnecting' : 'polling')
    deps.startPolling(POLL_MS)
    schedule(wasLive ? REOPEN_AFTER_DROP_MS : REOPEN_AFTER_FAILURE_MS)
  }

  function schedule(ms: number): void {
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      connect()
    }, ms)
  }

  // Every frame and heartbeat shows the stream is alive; silence for too long means it is not.
  function armWatchdog(alive: StreamSource): void {
    if (watchdog !== null) clearTimeout(watchdog)
    watchdog = setTimeout(() => {
      watchdog = null
      drop(alive)
    }, silenceLimitMs)
  }

  function connect(): void {
    if (!running || state === 'paused') return
    const next = deps.open()
    if (next === null) {
      set('polling')
      deps.startPolling(POLL_MS)
      return
    }
    source = next
    armWatchdog(next)
    next.addEventListener('queue', (event) => {
      if (next !== source) return
      armWatchdog(next)
      const snapshot = parseSnapshot(event.data)
      if (snapshot === null) return
      if (state !== 'live') {
        set('live')
        deps.stopPolling()
      }
      deps.onSnapshot(snapshot)
    })
    next.addEventListener('hb', () => {
      if (next !== source) return
      armWatchdog(next)
      deps.onHeartbeat?.()
    })
    next.addEventListener('error', () => drop(next))
  }

  // The first open, and the return from hidden: read the queue now and keep reading until a
  // frame arrives, so the list shows (and, if the stream never comes, stays fresh) meanwhile.
  function begin(): void {
    set('connecting')
    deps.startPolling(POLL_MS)
    connect()
  }

  function pause(): void {
    closeSource()
    set('paused')
    deps.startPolling(HIDDEN_POLL_MS)
  }

  return {
    state: () => state,
    start(): void {
      running = true
      if (deps.hidden()) pause()
      else begin()
    },
    // The tab's visibility changed: close the stream when hidden, open it again when visible.
    visibilityChanged(): void {
      if (!running) return
      if (deps.hidden()) {
        if (state !== 'paused') pause()
      } else if (state === 'paused') {
        begin()
      }
    },
    stop(): void {
      running = false
      closeSource()
      set('connecting')
    },
  }
}
