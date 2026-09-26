import { computed, onMounted, onUnmounted, reactive } from 'vue'
import type { QueueSnapshot } from '@/contract'
import { fetchQueue } from '@/api/egress'
import { POLL_MS, STREAM_URL, createLiveLink, type LinkState } from '@/lib/stream'

// What raised the banner: a failed poll means the list may be out of date, a
// failed decide means only that the decision was not sent.
export type StaleSource = 'poll' | 'decide'

type QueueState = {
  snapshot: QueueSnapshot | null
  stale: string | null
  staleSource: StaleSource
  link: LinkState
}

// One queue for the whole app: the sidebar badge, the tab title and the
// queue panel read the same snapshot and share one stream (or, when the stream
// is down, one poller).
const state = reactive<QueueState>({
  snapshot: null,
  stale: null,
  staleSource: 'poll',
  link: 'connecting',
})

let intervalId: ReturnType<typeof setInterval> | null = null
let intervalMs = 0
let consumers = 0
let seq = 0
// Each banner has its own clearing marker. A poll banner clears on the first
// good poll that started after the failed poll that raised it. A decide banner
// clears on the first good poll that started after that decide failed: a poll
// already in flight when a decide fails predates the failure and must not wipe
// the banner before anyone has seen it.
let pollFailedAt = 0
let decideFailedAt = 0
// The message of a decide that failed while a poll banner was up. If the poll
// that recovers the list predates that failure, it becomes the banner.
let pendingDecide: string | null = null
let decideClearId: ReturnType<typeof setTimeout> | null = null

async function refresh(): Promise<void> {
  const mine = ++seq
  const result = await fetchQueue()
  if (mine !== seq) return // a newer poll is in flight or landed; drop this one
  if (result.ok) {
    accept(result.data, mine)
    // A read that failed while the stream was up started a poll to recover; it has done that.
    if (state.link === 'live') stopPolling()
  } else {
    state.stale = result.error
    state.staleSource = 'poll'
    pollFailedAt = mine
    pendingDecide = null
    // With the stream up nothing polls, and it sends a frame only when the queue changes: a
    // failed read (after a decide, or the read that clears a decide banner) would leave the
    // banner over a list nothing refreshes. Poll until one read succeeds.
    if (state.link === 'live') startPolling(POLL_MS, false)
  }
}

// A good snapshot, from a poll or a stream frame (`mine` is when it was
// asked for or arrived, in the same numbering): the list is current, and any
// banner the snapshot postdates clears.
function accept(data: QueueSnapshot, mine: number): void {
  state.snapshot = data
  if (state.stale === null) return
  if (state.staleSource === 'poll') {
    if (mine <= pollFailedAt) return
    if (pendingDecide !== null && mine <= decideFailedAt) {
      state.stale = pendingDecide
      state.staleSource = 'decide'
      pendingDecide = null
      return
    }
  } else if (mine <= decideFailedAt) {
    return
  }
  state.stale = null
  pendingDecide = null
}

// The stream needs no polling while it is up. Polling is the fallback: it
// starts (with a read at once, unless `readNow` is off) when the stream is not
// delivering and stops with the first frame of a stream that is. Asked again at the
// interval it already runs at it does nothing; at another, it re-arms at that one.
function startPolling(ms: number, readNow = true): void {
  if (intervalId !== null && intervalMs === ms) return
  if (intervalId !== null) clearInterval(intervalId)
  intervalMs = ms
  intervalId = setInterval(() => void refresh(), ms)
  if (readNow) void refresh()
}

function stopPolling(): void {
  if (intervalId === null) return
  clearInterval(intervalId)
  intervalId = null
}

const link = createLiveLink({
  open: () => (typeof EventSource === 'undefined' ? null : new EventSource(STREAM_URL)),
  hidden: () => document.visibilityState === 'hidden',
  onSnapshot: (data) => accept(data, ++seq),
  onState: (next) => {
    state.link = next
  },
  startPolling: (ms) => startPolling(ms),
  stopPolling,
})

function onVisibilityChange(): void {
  link.visibilityChanged()
}

// A failed decide raises the banner too. When a failed poll already raised it,
// that one stays (it says more: the list is out of date) and the decide's
// message waits behind it.
function setStale(message: string): void {
  decideFailedAt = seq
  // With the stream up nothing polls, and a stream frame comes only when the
  // queue changes: read once after a poll interval, as the next poll would have.
  if (state.link === 'live') {
    if (decideClearId !== null) clearTimeout(decideClearId)
    decideClearId = setTimeout(() => {
      decideClearId = null
      void refresh()
    }, POLL_MS)
  }
  if (state.stale && state.staleSource === 'poll') {
    pendingDecide = message
    return
  }
  state.stale = message
  state.staleSource = 'decide'
  pendingDecide = null
}

export function useQueue() {
  onMounted(() => {
    consumers++
    if (consumers === 1) {
      document.addEventListener('visibilitychange', onVisibilityChange)
      link.start()
    }
  })

  onUnmounted(() => {
    consumers--
    if (consumers === 0) {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      link.stop()
      stopPolling()
      if (decideClearId !== null) clearTimeout(decideClearId)
      decideClearId = null
    }
  })

  return {
    snapshot: computed(() => state.snapshot),
    stale: computed(() => state.stale),
    staleSource: computed(() => state.staleSource),
    link: computed(() => state.link),
    openCount: computed(() => state.snapshot?.count ?? 0),
    refresh,
    setStale,
  }
}
