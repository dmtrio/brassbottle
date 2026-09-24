import type { QueueSnapshot, RecentPage } from '@/contract'

export type OpenRow = QueueSnapshot['open'][number]
export type DecidedRow = QueueSnapshot['recent'][number]
export type HistoryRow = RecentPage['rows'][number]
