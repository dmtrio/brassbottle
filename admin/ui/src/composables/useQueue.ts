import { computed, onMounted, onUnmounted, reactive } from 'vue'
import type { QueueSnapshot } from '@/contract'
import { fetchQueue } from '@/api/egress'

const POLL_INTERVAL_MS = 5000

// What raised the banner: a failed poll means the list may be out of date, a
// failed decide means only that the decision was not sent.
export type StaleSource = 'poll' | 'decide'

type QueueState = {
  snapshot: QueueSnapshot | null
  stale: string | null
  staleSource: StaleSource
}

// One queue for the whole app: the sidebar badge, the tab title and the
// queue panel read the same snapshot and share one poller.
const state = reactive<QueueState>({ snapshot: null, stale: null, staleSource: 'poll' })

let intervalId: ReturnType<typeof setInterval> | null = null
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

async function refresh(): Promise<void> {
  const mine = ++seq
  const result = await fetchQueue()
  if (mine !== seq) return // a newer poll is in flight or landed; drop this one
  if (result.ok) {
    state.snapshot = result.data
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
  } else {
    state.stale = result.error
    state.staleSource = 'poll'
    pollFailedAt = mine
    pendingDecide = null
  }
}

// A failed decide raises the banner too. When a failed poll already raised it,
// that one stays (it says more: the list is out of date) and the decide's
// message waits behind it.
function setStale(message: string): void {
  decideFailedAt = seq
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
      void refresh()
      intervalId = setInterval(() => void refresh(), POLL_INTERVAL_MS)
    }
  })

  onUnmounted(() => {
    consumers--
    if (consumers === 0 && intervalId !== null) {
      clearInterval(intervalId)
      intervalId = null
    }
  })

  return {
    snapshot: computed(() => state.snapshot),
    stale: computed(() => state.stale),
    staleSource: computed(() => state.staleSource),
    openCount: computed(() => state.snapshot?.count ?? 0),
    refresh,
    setStale,
  }
}
