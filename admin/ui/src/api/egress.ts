import type { AdminDecideResponse, ErrorResponse, QueueSnapshot } from '@/contract'

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string }

export type DecideAction =
  | 'allow_live'
  | 'allow_manifest'
  | 'deny'
  | 'deny_bottle'
  | 'deny_global'

export type DecidePayload = {
  action: DecideAction
  host: string
  container?: string
  reason?: string
}

// status 0 means the request never got an answer (network failure).
const UNREACHABLE = 'egress daemon unreachable'

function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'error' in value &&
    typeof (value as ErrorResponse).error === 'string'
  )
}

async function call<T>(url: string, init: RequestInit): Promise<ApiResult<T>> {
  let response: Response
  try {
    response = await fetch(url, { ...init, credentials: 'same-origin' })
  } catch {
    return { ok: false, status: 0, error: UNREACHABLE }
  }
  let parsed: unknown = null
  try {
    parsed = await response.json()
  } catch {
    parsed = null
  }
  if (response.ok && typeof parsed === 'object' && parsed !== null) {
    return { ok: true, data: parsed as T }
  }
  const error = isErrorResponse(parsed) ? parsed.error : response.statusText || 'request failed'
  return { ok: false, status: response.status, error }
}

export function fetchQueue(): Promise<ApiResult<QueueSnapshot>> {
  return call<QueueSnapshot>('/api/egress/queue', { method: 'GET' })
}

export function decide(payload: DecidePayload): Promise<ApiResult<AdminDecideResponse>> {
  return call<AdminDecideResponse>('/api/egress/decide', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-UI': '1',
    },
    body: JSON.stringify(payload),
  })
}
