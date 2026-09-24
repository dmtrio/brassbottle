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
// The banner clears only on a good poll that STARTED after the failure that
// raised it: a poll already in flight when a decide fails predates the failure
// and must not wipe the banner before anyone has seen it.
let staleAfter = 0

async function refresh(): Promise<void> {
  const mine = ++seq
  const result = await fetchQueue()
  if (mine !== seq) return // a newer poll is in flight or landed; drop this one
  if (result.ok) {
    state.snapshot = result.data
    if (mine > staleAfter) state.stale = null
  } else {
    state.stale = result.error
    state.staleSource = 'poll'
    staleAfter = mine
  }
}

// A failed decide raises the banner too, and it clears on the first good poll
// that starts after this call. When a failed poll already raised it, that one
// stays: it says more (the list is out of date).
function setStale(message: string): void {
  staleAfter = seq
  if (state.stale && state.staleSource === 'poll') return
  state.stale = message
  state.staleSource = 'decide'
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
