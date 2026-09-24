import { getLocalTimeZone, type DateValue } from '@internationalized/date'

export type DayRange = { start?: DateValue; end?: DateValue }

// The broker's timestamps: whole seconds, UTC, trailing Z.
export function isoSeconds(when: Date): string {
  return when.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

// A picked day range as the `since` / `until` query values: local midnight at
// the start of the first day to the last second of the last day. A range with
// only a start is that one day. The end is the next local midnight less one
// second, found by calendar arithmetic: a 23 or 25 hour DST day still ends at
// its own last second.
export function rangeToQuery(
  range: DayRange,
  zone: string = getLocalTimeZone(),
): { since: string | null; until: string | null } {
  if (!range.start) return { since: null, until: null }
  const last = range.end ?? range.start
  const since = range.start.toDate(zone)
  const until = new Date(last.add({ days: 1 }).toDate(zone).getTime() - 1000)
  return { since: isoSeconds(since), until: isoSeconds(until) }
}
