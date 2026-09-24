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
