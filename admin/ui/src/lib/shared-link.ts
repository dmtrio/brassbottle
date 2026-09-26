import type { QueueSnapshot } from '@/contract'
import {
  POLL_MS,
  SILENCE_LIMIT_MS,
  createLiveLink,
  type LinkState,
  type StreamSource,
} from '@/lib/stream'

// One live stream per browser, however many admin tabs are open.
//
// A browser allows about six HTTP/1.1 connections to one origin across all its tabs, and every
// stream takes one for as long as it is open, so tabs that each held a stream froze the admin at
// six. Instead the tabs elect a leader with a Web Lock: the leader opens the one stream (with all
// of stream.ts's states, watchdog and fallback polling, whether or not it is visible: a stream is
// not throttled in a background tab, a timer is) and relays every snapshot, heartbeat and change
// of link state over a BroadcastChannel. The other tabs, the followers, open no stream and read
// nothing while the leader is heard from. When the leader tab closes the browser passes the lock
// to another tab, which opens the stream.
//
// Where Web Locks or BroadcastChannel is missing (an insecure origin has no `navigator.locks`),
// a tab is `solo`: its own stream, closed while the tab is hidden, exactly as stream.ts has it.

export const LOCK_NAME = 'djinn-admin-stream'
export const CHANNEL_NAME = 'djinn-admin-queue'
// A follower that hears nothing from the leader (no snapshot, heartbeat or link change) for the
// stream's own silence limit and a margin (so a leader whose stream died has said so first) reads
// the queue itself and says Polling. Right after it opens, the leader gets only a short wait to
// answer: it answers a `hello` at once, so a longer silence means there is none to listen to.
export const FOLLOWER_SILENCE_MS = SILENCE_LIMIT_MS + 5000
export const HELLO_WAIT_MS = 3000

export type Role = 'solo' | 'follower' | 'leader'

// What travels on the channel. Everything but `hello` comes from the leader and carries its link
// state, so a follower that missed a change is corrected by the next message of any kind.
export type Message =
  | { kind: 'hello'; from: string }
  | { kind: 'link'; from: string; state: LinkState }
  | { kind: 'hb'; from: string; state: LinkState }
  | { kind: 'snapshot'; from: string; state: LinkState; snapshot: QueueSnapshot }

const STATES: readonly string[] = ['connecting', 'live', 'reconnecting', 'polling', 'paused']

// The channel is open to any script of the origin: a message is used only if it is one of ours.
export function parseMessage(raw: unknown): Message | null {
  if (typeof raw !== 'object' || raw === null) return null
  const m = raw as Record<string, unknown>
  if (typeof m.from !== 'string') return null
  if (m.kind === 'hello') return { kind: 'hello', from: m.from }
  if (typeof m.state !== 'string' || !STATES.includes(m.state)) return null
  const state = m.state as LinkState
  if (m.kind === 'link') return { kind: 'link', from: m.from, state }
  if (m.kind === 'hb') return { kind: 'hb', from: m.from, state }
  if (m.kind === 'snapshot') {
    const snapshot = m.snapshot as QueueSnapshot | null
    if (typeof snapshot === 'object' && snapshot !== null && Array.isArray(snapshot.open)) {
      return { kind: 'snapshot', from: m.from, state, snapshot }
    }
  }
  return null
}

// Leader election. `acquire` waits for the lock and calls `onLead` when this tab holds it; the
// tab holds it until the returned release is called (or the page goes away).
export type Election = {
  acquire: (onLead: () => void) => () => void
}

export type Bus = {
  post: (message: Message) => void
  listen: (listener: (raw: unknown) => void) => void
  close: () => void
}

export type SharedDeps = {
  id: string
  // null: this browser cannot elect a leader or reach the other tabs, so the tab is solo.
  election: Election | null
  openBus: () => Bus | null
  open: () => StreamSource | null
  // Only a solo tab looks at its visibility.
  hidden: () => boolean
  onSnapshot: (snapshot: QueueSnapshot) => void
  onState: (state: LinkState) => void
  startPolling: (intervalMs: number) => void
  stopPolling: () => void
  log: (line: string) => void
  now?: () => number
}

type LiveLink = ReturnType<typeof createLiveLink>

export function createSharedLink(deps: SharedDeps) {
  const now = deps.now ?? Date.now
  let role: Role = 'follower'
  let state: LinkState = 'connecting'
  let running = false
  let bus: Bus | null = null
  let release: (() => void) | null = null
  let live: LiveLink | null = null
  // A leader's last snapshot, for the tab that asks (`hello`); a follower's clock of the leader.
  let last: QueueSnapshot | null = null
  let lastHeard = 0
  let heard = false
  let watch: ReturnType<typeof setTimeout> | null = null
  let ownPolling = false
  const counts = { sent: 0, received: 0 }

  function setState(next: LinkState): void {
    if (next === state) return
    state = next
    deps.onState(next)
  }

  function send(message: Message): void {
    if (bus === null) return
    try {
      bus.post(message)
      counts.sent++
      const rows = message.kind === 'snapshot' ? ` rows=${message.snapshot.open.length}` : ''
      const carried = message.kind === 'hello' ? '' : ` state=${message.state}`
      deps.log(`stage=broadcast-out kind=${message.kind}${carried}${rows} sent=${counts.sent}`)
    } catch (error) {
      deps.log(`stage=broadcast-out failed kind=${message.kind} error=${String(error)}`)
    }
  }

  function clearWatch(): void {
    if (watch !== null) clearTimeout(watch)
    watch = null
  }

  function armWatch(ms: number): void {
    clearWatch()
    watch = setTimeout(checkSilence, ms)
  }

  // Timers in a background tab run late, so on firing the clock, not the timer, says how long it has been.
  function checkSilence(): void {
    watch = null
    if (!running || role !== 'follower') return
    const limit = heard ? FOLLOWER_SILENCE_MS : HELLO_WAIT_MS
    const quiet = now() - lastHeard
    if (quiet < limit) {
      armWatch(limit - quiet)
      return
    }
    deps.log(`stage=leader-silent quiet_ms=${quiet} action=poll`)
    ownPolling = true
    setState('polling')
    deps.startPolling(POLL_MS)
  }

  function receive(raw: unknown): void {
    const message = parseMessage(raw)
    if (message === null || message.from === deps.id) return
    counts.received++
    if (message.kind === 'hello') {
      if (role !== 'leader') return
      send({ kind: 'link', from: deps.id, state })
      if (last !== null) send({ kind: 'snapshot', from: deps.id, state, snapshot: last })
      return
    }
    if (role !== 'follower') return
    lastHeard = now()
    if (!heard || ownPolling) {
      heard = true
      if (ownPolling) {
        ownPolling = false
        deps.stopPolling()
        deps.log('stage=leader-heard action=stop-poll')
      }
      armWatch(FOLLOWER_SILENCE_MS)
    }
    setState(message.state)
    if (message.kind === 'snapshot') {
      deps.log(`stage=broadcast-in kind=snapshot rows=${message.snapshot.open.length} received=${counts.received}`)
      deps.onSnapshot(message.snapshot)
    }
  }

  function lead(): void {
    if (!running || role === 'leader') return
    role = 'leader'
    clearWatch()
    if (ownPolling) {
      ownPolling = false
      deps.stopPolling()
    }
    deps.log(`stage=leader-elected id=${deps.id}`)
    // A tab that followed shows the old leader's state until told otherwise; it starts over.
    setState('connecting')
    send({ kind: 'link', from: deps.id, state })
    live = createLiveLink({
      open: deps.open,
      hidden: () => false,
      onSnapshot: (snapshot) => {
        last = snapshot
        deps.onSnapshot(snapshot)
        send({ kind: 'snapshot', from: deps.id, state, snapshot })
      },
      onState: (next) => {
        setState(next)
        send({ kind: 'link', from: deps.id, state: next })
      },
      startPolling: deps.startPolling,
      stopPolling: deps.stopPolling,
      onHeartbeat: () => send({ kind: 'hb', from: deps.id, state }),
    })
    live.start()
  }

  return {
    role: () => role,
    state: () => state,
    start(): void {
      running = true
      state = 'connecting'
      const opened = deps.election === null ? null : deps.openBus()
      if (deps.election === null || opened === null) {
        opened?.close()
        role = 'solo'
        deps.log('stage=link-start role=solo reason=no-web-locks-or-broadcast-channel')
        live = createLiveLink({
          open: deps.open,
          hidden: deps.hidden,
          onSnapshot: deps.onSnapshot,
          onState: (next) => setState(next),
          startPolling: deps.startPolling,
          stopPolling: deps.stopPolling,
        })
        live.start()
        return
      }
      role = 'follower'
      bus = opened
      heard = false
      ownPolling = false
      last = null
      lastHeard = now()
      opened.listen(receive)
      deps.log(`stage=link-start role=follower id=${deps.id}`)
      send({ kind: 'hello', from: deps.id })
      armWatch(HELLO_WAIT_MS)
      release = deps.election.acquire(lead)
    },
    // A solo tab closes its stream when hidden and opens it again when shown; a leader or follower
    // has nothing to do (the leader's stream is not throttled, and a follower has none).
    visibilityChanged(): void {
      if (role === 'solo') live?.visibilityChanged()
    },
    // The leader's own reads of the queue (a poll, or the read after a decide) are as good as a
    // frame: hand them to the tabs following it.
    publish(snapshot: QueueSnapshot): void {
      if (role !== 'leader') return
      last = snapshot
      send({ kind: 'snapshot', from: deps.id, state, snapshot })
    },
    stop(): void {
      if (!running) return
      running = false
      clearWatch()
      live?.stop()
      live = null
      release?.()
      release = null
      if (bus !== null) {
        deps.log(`stage=link-stop role=${role} sent=${counts.sent} received=${counts.received}`)
        bus.close()
      }
      bus = null
      ownPolling = false
      role = 'follower'
      state = 'connecting'
    },
  }
}
