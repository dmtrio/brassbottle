import type { OpenRow } from '@/types'

// The browser's own three answers, plus what it can be asked about.
export type Permission = 'default' | 'granted' | 'denied'
export type BellState = 'unsupported' | 'default' | 'on' | 'muted' | 'denied'

// Which request ids this tab has already shown. The first snapshot it loads
// only seeds the set: the requests waiting when the page opens are not news.
// Ids are never forgotten, so a request that leaves and comes back with the
// same id (a sweep, a re-file) does not notify twice either. The set grows by
// one short string per request the tab ever sees, a few per hour at the most.
export class SeenRequests {
  private seen: Set<string> | null = null

  get seeded(): boolean {
    return this.seen !== null
  }

  // The rows of `rows` this tab has not seen before, oldest snapshot order kept.
  take(rows: OpenRow[]): OpenRow[] {
    if (this.seen === null) {
      this.seen = new Set(rows.map((row) => row.request_id))
      return []
    }
    const fresh: OpenRow[] = []
    for (const row of rows) {
      if (this.seen.has(row.request_id)) continue
      this.seen.add(row.request_id)
      fresh.push(row)
    }
    return fresh
  }
}

// Why the bell is `unsupported`: the page is not a secure context (the
// Notification API does not exist there, even in a browser that has it), or the
// browser or device cannot show them (no API, or a constructor that throws, as on
// Chrome for Android).
export type UnsupportedReason = 'insecure' | 'browser'

// `unavailable`: the constructor threw this session, so the bell must not claim
// "on" again whatever the permission says.
export function bellState(permission: Permission | null, muted: boolean, unavailable = false): BellState {
  if (permission === null || unavailable) return 'unsupported'
  if (permission === 'denied') return 'denied'
  if (permission === 'default') return 'default'
  return muted ? 'muted' : 'on'
}

export function unsupportedReason(secure: boolean, permission: Permission | null): UnsupportedReason {
  return permission === null && !secure ? 'insecure' : 'browser'
}

export const BELL_LABELS: Record<BellState, string> = {
  unsupported: 'Desktop notifications are not available on this device or browser',
  default: 'Enable desktop notifications',
  on: 'Desktop notifications on. Click to mute',
  muted: 'Desktop notifications muted. Click to turn on',
  denied: 'Desktop notifications are blocked in browser settings',
}

export const INSECURE_LABEL = 'Desktop notifications need a secure connection (https or localhost)'

// The tooltip and the popover's heading: what the bell is doing right now.
export function bellLabel(state: BellState, reason: UnsupportedReason): string {
  return state === 'unsupported' && reason === 'insecure' ? INSECURE_LABEL : BELL_LABELS[state]
}

// The accessible name. A toggle keeps one name and `aria-pressed` carries the
// state; the wording that changes with it lives in the title.
export const TOGGLE_NAME = 'Desktop notifications'

export function bellName(state: BellState, reason: UnsupportedReason): string {
  return state === 'on' || state === 'muted' ? TOGGLE_NAME : bellLabel(state, reason)
}

// `tag` is the request id: a second notification with the same tag replaces the
// first instead of stacking.
export function notificationFor(row: OpenRow): { title: string; body: string; tag: string } {
  return {
    title: `New egress request from ${row.container}`,
    body: `${row.host}:${row.port}`,
    tag: row.request_id,
  }
}

export function readPermission(): Permission | null {
  if (typeof Notification === 'undefined') return null
  const value: string = Notification.permission
  return value === 'granted' || value === 'denied' ? value : 'default'
}
