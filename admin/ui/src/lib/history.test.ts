import { CalendarDate } from '@internationalized/date'
import { describe, expect, it } from 'vitest'
import { isoSeconds, rangeToQuery } from './history'

const NEW_YORK = 'America/New_York'
const BERLIN = 'Europe/Berlin'

describe('isoSeconds', () => {
  it('drops the milliseconds and keeps the trailing Z', () => {
    expect(isoSeconds(new Date('2026-09-23T12:00:00.987Z'))).toBe('2026-09-23T12:00:00Z')
  })
})

describe('rangeToQuery', () => {
  it('has neither bound for an empty range', () => {
    expect(rangeToQuery({}, NEW_YORK)).toEqual({ since: null, until: null })
  })

  it('makes a lone start the whole of that local day', () => {
    // EDT is UTC-4 all day on 2026-09-23.
    expect(rangeToQuery({ start: new CalendarDate(2026, 9, 23) }, NEW_YORK)).toEqual({
      since: '2026-09-23T04:00:00Z',
      until: '2026-09-24T03:59:59Z',
    })
  })

  it('ends a 25-hour day (DST ends) at its own last second', () => {
    // 2026-11-01: EDT until 02:00, then EST. Midnight to midnight is 25 hours.
    expect(rangeToQuery({ start: new CalendarDate(2026, 11, 1) }, NEW_YORK)).toEqual({
      since: '2026-11-01T04:00:00Z',
      until: '2026-11-02T04:59:59Z',
    })
    // 2026-10-25 in Berlin: CEST until 03:00, then CET.
    expect(rangeToQuery({ start: new CalendarDate(2026, 10, 25) }, BERLIN)).toEqual({
      since: '2026-10-24T22:00:00Z',
      until: '2026-10-25T22:59:59Z',
    })
  })

  it('ends a 23-hour day (DST starts) before the next day begins', () => {
    // 2026-03-08: EST until 02:00, then EDT. Midnight to midnight is 23 hours.
    expect(rangeToQuery({ start: new CalendarDate(2026, 3, 8) }, NEW_YORK)).toEqual({
      since: '2026-03-08T05:00:00Z',
      until: '2026-03-09T03:59:59Z',
    })
    // 2026-03-29 in Berlin: CET until 02:00, then CEST.
    expect(rangeToQuery({ start: new CalendarDate(2026, 3, 29) }, BERLIN)).toEqual({
      since: '2026-03-28T23:00:00Z',
      until: '2026-03-29T21:59:59Z',
    })
  })

  it('runs a multi-day range across a DST change from first midnight to last second', () => {
    expect(
      rangeToQuery(
        { start: new CalendarDate(2026, 3, 7), end: new CalendarDate(2026, 3, 8) },
        NEW_YORK,
      ),
    ).toEqual({ since: '2026-03-07T05:00:00Z', until: '2026-03-09T03:59:59Z' })
  })
})
