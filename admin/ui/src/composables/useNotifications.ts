import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter, type Router } from 'vue-router'
import { useQueue } from '@/composables/useQueue'
import {
  bellState,
  BELL_LABELS,
  notificationFor,
  readPermission,
  SeenRequests,
  type Permission,
} from '@/lib/notify'

const MUTE_KEY = 'djinn-admin-notifications-muted'

// The mute is per viewer, not per request or per session: it lives in this
// browser's localStorage and nowhere else. Storage can be blocked (a private
// window, cleared site data), so every touch is guarded and the bell still works.
function readMuted(): boolean {
  try {
    return window.localStorage.getItem(MUTE_KEY) === '1'
  } catch {
    return false
  }
}

function writeMuted(muted: boolean): void {
  try {
    if (muted) window.localStorage.setItem(MUTE_KEY, '1')
    else window.localStorage.removeItem(MUTE_KEY)
  } catch {
    // the mute holds for this page load only
  }
}

// One notifier for the whole app, like the queue it watches.
const permission = ref<Permission | null>(readPermission())
const muted = ref(readMuted())
const seen = new SeenRequests()
let router: Router | null = null

// The request a notification click asked to show. The queue panel takes it
// when it is on screen (after the route and tab are right) and clears it.
export const focusRequestId = ref<string | null>(null)

function requestRow(id: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-request-id="${CSS.escape(id)}"]`)
}

export function hasRequestRow(id: string): boolean {
  return requestRow(id) !== null
}

// Scroll a request's row into view and focus it; false when the row is not on the page.
export function focusRequestRow(id: string): boolean {
  const row = requestRow(id)
  log(`stage=focus request_id=${id} found=${row !== null}`)
  row?.scrollIntoView({ block: 'center' })
  row?.focus()
  return row !== null
}

function log(message: string): void {
  console.info(`[notify] ${message}`)
}

function syncPermission(): void {
  const now = readPermission()
  if (now !== permission.value) {
    log(`stage=permission-changed from=${permission.value} to=${now}`)
    permission.value = now
  }
}

async function askPermission(): Promise<void> {
  const started = performance.now()
  let answer: unknown = null
  try {
    answer = await Notification.requestPermission()
  } catch (error) {
    console.warn('[notify] stage=permission failed', error)
  }
  // The answer is the browser's word; the property is the fallback for a
  // browser whose promise resolves with nothing.
  permission.value = answer === 'granted' || answer === 'denied' || answer === 'default' ? answer : readPermission()
  if (permission.value === 'granted') {
    // Turning notifications on from the bell means on, even if a mute from an
    // earlier grant is still stored.
    muted.value = false
    writeMuted(false)
  }
  log(`stage=permission result=${permission.value} duration=${Math.round(performance.now() - started)}ms`)
}

function press(): void {
  const state = bellState(permission.value, muted.value)
  if (state === 'default') {
    void askPermission()
  } else if (state === 'on' || state === 'muted') {
    muted.value = state === 'on'
    writeMuted(muted.value)
    log(`stage=mute muted=${muted.value}`)
  }
  // denied: the browser will not ask again, so the bell only explains; unsupported: nothing to do
}

function openRequest(id: string): void {
  window.focus()
  focusRequestId.value = id
  void router?.push('/egress')
}

function fire(requestId: string, title: string, body: string): void {
  try {
    const shown = new Notification(title, { body, tag: requestId })
    shown.onclick = () => {
      shown.close()
      openRequest(requestId)
    }
  } catch (error) {
    // e.g. permission revoked between the check and the call
    console.warn(`[notify] stage=fire failed request_id=${requestId}`, error)
  }
}

export function useNotifications() {
  const { snapshot } = useQueue()
  router = useRouter()

  // Every snapshot is read, whether or not anything is shown: a request that
  // arrived while muted or before permission was granted is still "shown", or
  // turning notifications on would burst them all.
  watch(
    snapshot,
    (snap) => {
      if (!snap) return
      const first = !seen.seeded
      const fresh = seen.take(snap.open)
      const active = bellState(permission.value, muted.value) === 'on'
      if (first || fresh.length > 0) {
        log(`stage=snapshot rows=${snap.open.length} new=${fresh.length} first=${first} notify=${active}`)
      }
      if (!active) return
      for (const row of fresh) {
        const { title, body, tag } = notificationFor(row)
        fire(tag, title, body)
      }
    },
    { immediate: true },
  )

  // The permission can change under the page (browser settings); look again
  // when the tab comes back and when the browser says so.
  let permissionStatus: PermissionStatus | null = null
  onMounted(() => {
    window.addEventListener('focus', syncPermission)
    document.addEventListener('visibilitychange', syncPermission)
    navigator.permissions
      ?.query({ name: 'notifications' })
      .then((status) => {
        permissionStatus = status
        status.addEventListener('change', syncPermission)
      })
      .catch(() => {})
  })
  onUnmounted(() => {
    window.removeEventListener('focus', syncPermission)
    document.removeEventListener('visibilitychange', syncPermission)
    permissionStatus?.removeEventListener('change', syncPermission)
  })
}

// The bell reads and drives the same state from any component.
export function useBell() {
  const state = computed(() => bellState(permission.value, muted.value))
  return { state, label: computed(() => BELL_LABELS[state.value]), press }
}
