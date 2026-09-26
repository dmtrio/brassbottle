import type { DecidedRow, OpenRow } from '@/types'

export type StateFilter = 'all' | 'failed'
// Cumulative, so each reads as its label: under 5 minutes, under an hour, older
// than an hour. A 3-minute-old request is in both of the first two.
export type AgeFilter = 'any' | '5m' | '1h' | 'old'

export type QueueFilters = {
  bottles: string[]
  state: StateFilter
  age: AgeFilter
  search: string
}

export const AGE_OPTIONS: { value: AgeFilter; label: string }[] = [
  { value: 'any', label: 'Any age' },
  { value: '5m', label: 'Under 5 minutes' },
  { value: '1h', label: 'Under 1 hour' },
  { value: 'old', label: 'Older than 1 hour' },
]

const FIVE_MINUTES = 5 * 60
const ONE_HOUR = 60 * 60

export const NO_FILTERS: QueueFilters = { bottles: [], state: 'all', age: 'any', search: '' }

export function filtersActive(filters: QueueFilters): boolean {
  return (
    filters.search.trim() !== '' ||
    filters.bottles.length > 0 ||
    filters.state !== 'all' ||
    filters.age !== 'any'
  )
}

// How long a request has been open, from when it opened rather than from the
// snapshot's `age_seconds`, so a snapshot a few polls old still buckets right.
function ageSeconds(row: OpenRow, nowMs: number): number {
  return Math.max(0, (nowMs - Date.parse(row.opened_at)) / 1000)
}

export function matchesFilters(row: OpenRow, filters: QueueFilters, nowMs: number): boolean {
  const needle = filters.search.trim().toLowerCase()
  if (needle && !`${row.host}:${row.port}`.toLowerCase().includes(needle)) return false
  if (filters.bottles.length > 0 && !filters.bottles.includes(row.container)) return false
  if (filters.state === 'failed' && !row.last_error) return false
  const age = ageSeconds(row, nowMs)
  if (filters.age === '5m' && age >= FIVE_MINUTES) return false
  if (filters.age === '1h' && age >= ONE_HOUR) return false
  if (filters.age === 'old' && age < ONE_HOUR) return false
  return true
}

export function filterOpen(rows: OpenRow[], filters: QueueFilters, nowMs: number): OpenRow[] {
  return rows.filter((row) => matchesFilters(row, filters, nowMs))
}

// A request the denylist denied on arrival: nobody decided it, so it is
// reported once as a group instead of as a row to act on.
export function isDenylistHit(row: DecidedRow): boolean {
  return row.status === 'denied' && row.decided_by === 'denylist'
}

export function denylistHitTotal(rows: DecidedRow[]): number {
  return rows.reduce((total, row) => total + row.hit_count, 0)
}
