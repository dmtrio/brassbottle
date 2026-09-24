import type { DecidedRow } from '@/types'

const OUTCOME_LABEL: Record<string, string> = {
  live: 'Allowed',
  manifest: 'Allowed permanently · bottle',
  once: 'Denied',
  bottle: 'Denied permanently · bottle',
  global: 'Denied permanently · global',
}

// How a decided row reads to the operator; shared by the queue's 24 h list and
// the History tab, so the same decision never has two names.
export function outcomeLabel(row: DecidedRow): string {
  if (row.decided_by === 'denylist') return 'Denylist'
  if (row.status === 'stale') return 'Expired'
  return (row.scope && OUTCOME_LABEL[row.scope]) || (row.status === 'allowed' ? 'Allowed' : 'Denied')
}

export function outcomeTone(row: DecidedRow): string {
  if (row.status === 'allowed') return 'pill-allow'
  if (row.status === 'stale') return 'pill-neutral'
  return 'pill-deny'
}

// "Sep 23, 02:55 PM"; the year appears only for another year, so a row from
// last winter is not mistaken for this one.
export function decidedAt(isoTs: string): string {
  const when = new Date(isoTs)
  const sameYear = when.getFullYear() === new Date().getFullYear()
  return when.toLocaleString(undefined, {
    year: sameYear ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// "Sep 23": the day alone, for a phone row that has no room for the clock.
export function decidedDay(isoTs: string): string {
  const when = new Date(isoTs)
  const sameYear = when.getFullYear() === new Date().getFullYear()
  return when.toLocaleDateString(undefined, {
    year: sameYear ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

// "5m ago": how long ago a timestamp was, to the largest whole unit. `nowMs`
// lets a caller hand in a reactive clock so the text can refresh.
export function relTime(isoTs: string, nowMs: number = Date.now()): string {
  const s = Math.max(0, Math.round((nowMs - Date.parse(isoTs)) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
