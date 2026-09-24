import type { QueueSnapshot } from '@/contract'

export type OpenRow = QueueSnapshot['open'][number]
export type DecidedRow = QueueSnapshot['recent'][number]
