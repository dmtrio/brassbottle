// Fake data shaped like EgressBroker.queue_snapshot() (open rows) and the
// planned GET /recent page (decided rows). Mockup only: no network.
import { reactive, ref } from 'vue'

export interface OpenRow {
  request_id: string
  container: string
  host: string
  port: number
  host_is_ip: boolean
  opened_at: string
  age_seconds: number
  hit_count: number
  uid: number | null
  comm: string | null
  reason: string | null
  attempt: number
  last_error: string | null
}

export type Status = 'allowed' | 'denied'
export interface DecidedRow {
  request_id: string
  container: string
  host: string
  port: number
  status: Status
  scope: 'live' | 'manifest' | 'once' | 'bottle' | 'global' | null
  decided_at: string
  decided_by: string
  apply_status: 'applied' | 'failed' | null
  deny_reason: string | null
  hit_count: number
}

export const BOTTLES = ['coding-brassbottle', 'trip-research', 'rhino', 'coding-tanks']

const now = Date.now()
const iso = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString()
let seq = 100
const rid = () => (seq++).toString(16).padStart(8, '0')

function open(container: string, host: string, age: number, extra: Partial<OpenRow> = {}): OpenRow {
  return {
    request_id: rid(), container, host, port: 443, host_is_ip: /^\d/.test(host),
    opened_at: iso(age), age_seconds: age, hit_count: 1, uid: 1000, comm: 'node',
    reason: null, attempt: 0, last_error: null, ...extra,
  }
}

export const state = reactive({
  open: [
    open('coding-brassbottle', 'registry.yarnpkg.com', 42, { comm: 'yarn', reason: 'yarn install for admin/ui', hit_count: 6 }),
    open('coding-brassbottle', 'objects.githubusercontent.com', 95, { comm: 'curl', reason: 'download yq release asset' }),
    open('coding-brassbottle', 'fonts.gstatic.com', 610, { comm: 'chromium', hit_count: 14 }),
    open('trip-research', 'www.lonelyplanet.com', 180, { comm: 'python3', reason: 'fetch itinerary pages' }),
    open('trip-research', 'api.open-meteo.com', 1250, { comm: 'python3', reason: 'weather lookups', attempt: 3,
      last_error: "allow-egress.sh exit 1: ipset add allowed-domains 104.21.3.9: Kernel error received: set is full" }),
    open('trip-research', '151.101.1.140', 3700, { comm: 'wget', hit_count: 2 }),
    open('rhino', 'huggingface.co', 20, { comm: 'python3', reason: 'pull tokenizer for eval run' }),
    open('rhino', 'cdn-lfs.hf.co', 18, { comm: 'python3', reason: 'pull tokenizer for eval run' }),
    open('rhino', 'sentry.io', 7300, { comm: 'node', attempt: 1,
      last_error: 'docker exec djinn-rhino: container is restarting, wait until the container is running' }),
  ] as OpenRow[],
  recent: [] as DecidedRow[],
})

const DECIDED_HOSTS = ['pypi.org', 'files.pythonhosted.org', 'api.github.com', 'registry.npmjs.org', 'crates.io',
  'static.crates.io', 'proxy.golang.org', 'deb.debian.org', 'api.anthropic.com', 'ghcr.io', 'docs.python.org',
  'example.org', 'duckduckgo.com', 'raw.githubusercontent.com']

for (let i = 0; i < 180; i++) {
  const denied = i % 5 === 0
  const container = BOTTLES[i % BOTTLES.length]
  state.recent.push({
    request_id: rid(), container, host: DECIDED_HOSTS[(i * 7) % DECIDED_HOSTS.length], port: 443,
    status: denied ? 'denied' : 'allowed',
    scope: denied ? (i % 3 === 0 ? 'bottle' : 'once') : (i % 4 === 0 ? 'manifest' : 'live'),
    decided_at: iso(600 + i * 5400), decided_by: 'admin', apply_status: denied ? null : (i === 7 ? 'failed' : 'applied'),
    deny_reason: denied ? 'not needed for this task' : null, hit_count: 1,
  })
}
// Denylist hits: denied rows decided by the denylist, repeats counted in hit_count.
for (const [container, host, hits, age] of [
  ['coding-brassbottle', 'telemetry.example-analytics.io', 212, 300],
  ['coding-brassbottle', 'o1234.ingest.sentry.io', 37, 900],
  ['trip-research', 'ads.doubleclick.net', 58, 1500],
  ['rhino', 'telemetry.example-analytics.io', 4, 4000],
] as const) {
  state.recent.unshift({ request_id: rid(), container, host, port: 443, status: 'denied', scope: 'global',
    decided_at: iso(age), decided_by: 'denylist', apply_status: null, deny_reason: 'denylist: telemetry', hit_count: hits })
}

export type DecideAction = 'allow_live' | 'allow_manifest' | 'deny' | 'deny_bottle' | 'deny_global'
export const failNext = ref(false)

// Simulates POST /api/egress/decide: moves the row to recent after a short delay.
export function decide(row: OpenRow, action: DecideAction, reason?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    setTimeout(() => {
      if (failNext.value) { failNext.value = false; reject(new Error('broker unreachable (simulated)')); return }
      const i = state.open.findIndex((r) => r.request_id === row.request_id)
      if (i >= 0) state.open.splice(i, 1)
      const denied = action.startsWith('deny')
      state.recent.unshift({
        request_id: row.request_id, container: row.container, host: row.host, port: row.port,
        status: denied ? 'denied' : 'allowed',
        scope: ({ allow_live: 'live', allow_manifest: 'manifest', deny: 'once', deny_bottle: 'bottle', deny_global: 'global' } as const)[action],
        decided_at: new Date().toISOString(), decided_by: 'admin', apply_status: denied ? null : 'applied',
        deny_reason: denied ? reason || null : null, hit_count: row.hit_count,
      })
      resolve()
    }, 350)
  })
}

const NEW_HOSTS = [['coding-tanks', 'api.stripe.com', 'stripe fixtures'], ['rhino', 'wandb.ai', 'log eval metrics'],
  ['coding-brassbottle', 'playwright.azureedge.net', 'playwright install chromium'], ['trip-research', 'maps.googleapis.com', null]] as const
let newIdx = 0
// Simulates an SSE `queue` event carrying a new filing.
export function simulateFiling(): OpenRow {
  const [c, h, r] = NEW_HOSTS[newIdx++ % NEW_HOSTS.length]
  const row = open(c, h, 0, { reason: r, comm: 'node' })
  state.open.unshift(row)
  return row
}

export const ACTION_LABEL: Record<DecideAction, string> = {
  allow_live: 'Allow',
  allow_manifest: 'Allow permanently · bottle',
  deny: 'Deny',
  deny_bottle: 'Deny permanently · bottle',
  deny_global: 'Deny permanently · global',
}

// Newest first. Grouped view: bottles alphabetical, rows newest first within.
export const byNewest = (a: OpenRow, b: OpenRow) => Date.parse(b.opened_at) - Date.parse(a.opened_at)

export function relTime(isoTs: string): string {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(isoTs)) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
