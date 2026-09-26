<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { Ban, Box, Check, ChevronDown, FilterX, Group, Rows3, X } from '@lucide/vue'
import { useMediaQuery } from '@vueuse/core'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { decide as apiDecide, type DecideAction, type DecidePayload } from '@/api/egress'
import { focusRequestId, focusRequestRow, hasRequestRow } from '@/composables/useNotifications'
import { useQueue } from '@/composables/useQueue'
import { outcomeLabel, outcomeTone, relTime } from '@/lib/decision'
import {
  AGE_OPTIONS,
  denylistHitTotal,
  filterOpen,
  filtersActive,
  isDenylistHit,
  NO_FILTERS,
  type AgeFilter,
  type QueueFilters,
  type StateFilter,
} from '@/lib/queue'
import type { OpenRow } from '@/types'
import BottleMultiFilter from './BottleMultiFilter.vue'
import DecideButtons from './DecideButtons.vue'
import EmptyState from './EmptyState.vue'
import RequestSummary, { type RowNote } from './RequestSummary.vue'
import SearchField from './SearchField.vue'

const REASON_MAX = 200

// Below 1024 px the sidebar leaves the table too little width (the Decision
// column clips and hosts break mid-word), so requests become cards.
const compact = useMediaQuery('(max-width: 1023px)')
const { snapshot, stale, staleSource, refresh, setStale } = useQueue()

type View = 'grouped' | 'flat'
const view = ref<View>('grouped')

// A toggle group deselects on a second click; a view must always be chosen.
function chooseView(next: unknown): void {
  if (next === 'grouped' || next === 'flat') view.value = next
}

// Request id -> how many in-flight decides cover it. A count, not a flag: with
// two overlapping decides the first to finish must not unlock rows the second
// still holds.
const busy = reactive(new Map<string, number>())
const isBusy = (id: string): boolean => (busy.get(id) ?? 0) > 0
// Request id -> the action of the latest decide holding it, so the spinner sits
// on the button that was pressed (an Allow run must not spin the Deny button).
const busyAction = reactive(new Map<string, DecideAction>())
const notes = reactive<Record<string, RowNote | undefined>>({})

function byNewest(a: OpenRow, b: OpenRow): number {
  return Date.parse(b.opened_at) - Date.parse(a.opened_at)
}

// Filters narrow the list in the browser: the queue is the whole snapshot, a
// few dozen rows at most, so nothing here asks the broker.
const filters = reactive<QueueFilters>({ ...NO_FILTERS, bottles: [] })
const filtered = computed(() => filtersActive(filters))

// A toggle group deselects on a second click; a state must always be chosen.
function chooseState(next: unknown): void {
  if (next === 'all' || next === 'failed') filters.state = next as StateFilter
}

function chooseAge(next: unknown): void {
  if (AGE_OPTIONS.some((option) => option.value === next)) filters.age = next as AgeFilter
}

function clearFilters(): void {
  Object.assign(filters, NO_FILTERS, { bottles: [] })
}

const openRows = computed(() => snapshot.value?.open ?? [])

// Said in the queue's polite live region when a notification click had to
// reset the filters, so the reset is not silent. It is about one request and
// one filter state: it goes when a filter changes, when that request leaves the
// queue (decided), or on the next click.
const filtersClearedNote = ref('')
const filtersClearedFor = ref<string | null>(null)

function dropClearedNote(): void {
  filtersClearedNote.value = ''
  filtersClearedFor.value = null
}

// Synchronous, so the reset that raises the note (which changes the filters
// too) has finished before the note is set and cannot clear it.
watch(filters, dropClearedNote, { deep: true, flush: 'sync' })
watch(
  () => (filtersClearedFor.value ? openRows.value.some((row) => row.request_id === filtersClearedFor.value) : true),
  (stillOpen) => {
    if (!stillOpen) dropClearedNote()
  },
)

// A notification click lands here: bring the request's row into view and focus
// it. A filter may be hiding it, and the notification is about that request,
// so the filters give way, and only then. A request decided since has no row;
// the click still lands on the queue.
watch(
  focusRequestId,
  async (id) => {
    if (!id) return
    dropClearedNote()
    const open = openRows.value.find((row) => row.request_id === id)
    if (open && !hasRequestRow(id)) {
      clearFilters()
      filtersClearedNote.value = `Filters cleared to show ${open.host}`
      filtersClearedFor.value = id
      await nextTick()
    }
    focusRequestRow(id)
    focusRequestId.value = null
  },
  { immediate: true, flush: 'post' },
)

// Every bottle with an open request, plus any still selected after its last
// request was decided, so a selection can always be undone.
const bottleOptions = computed(() =>
  [...new Set([...openRows.value.map((row) => row.container), ...filters.bottles])].sort(),
)

// `Date.now()` is read when the snapshot or a filter changes: a request that
// crosses an age edge between polls is re-bucketed by the next one.
const visibleRows = computed(() => filterOpen(openRows.value, filters, Date.now()))

// Grouped by hand rather than with TanStack's getGroupedRowModel: "bottles
// alphabetical, rows newest first within" is a plain sort and the grouped row
// model would need overriding to express it. TanStack Table was not added for
// the filters either: they are the plain predicates in lib/queue.ts.
const groups = computed(() => {
  const byBottle = new Map<string, OpenRow[]>()
  for (const row of visibleRows.value) {
    const list = byBottle.get(row.container) ?? []
    list.push(row)
    byBottle.set(row.container, list)
  }
  return [...byBottle.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([container, rows]) => ({ container, rows: [...rows].sort(byNewest) }))
})

const sections = computed(() =>
  view.value === 'grouped'
    ? groups.value
    : [{ container: null as string | null, rows: [...visibleRows.value].sort(byNewest) }],
)

// Denylist hits are not decisions anyone made: one collapsed group, its hits
// summed, instead of a row each in the list of what was decided.
const decidedByTime = computed(() =>
  [...(snapshot.value?.recent ?? [])].sort((a, b) => Date.parse(b.decided_at) - Date.parse(a.decided_at)),
)
const denylistRows = computed(() => decidedByTime.value.filter(isDenylistHit))
const denylistHits = computed(() => denylistHitTotal(denylistRows.value))
const recentRows = computed(() => decidedByTime.value.filter((row) => !isDenylistHit(row)))
const denylistOpen = ref(false)

function clock(isoTs: string): string {
  return new Date(isoTs).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

// A failed poll leaves the last good snapshot on screen, so the banner says
// when it is from. A failed decide leaves the list current: it only says the
// decision was not sent.
const staleText = computed(() => {
  if (!stale.value) return null
  if (staleSource.value === 'decide') return `Decision not sent: ${stale.value}`
  const at = snapshot.value?.generated_at
  if (!at) return stale.value
  const time = new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return `Showing data from ${time}: ${stale.value}`
})

// Permanent deny dialog: optional reason; global also needs the exact host.
const dlg = reactive({
  open: false,
  row: null as OpenRow | null,
  action: 'deny_bottle' as 'deny_bottle' | 'deny_global',
  reason: '',
  confirm: '',
})

const hostMismatch = computed(() => dlg.action === 'deny_global' && dlg.confirm !== dlg.row?.host)

function openPermanentDeny(row: OpenRow, action: 'deny_bottle' | 'deny_global'): void {
  dlg.row = row
  dlg.action = action
  dlg.reason = ''
  dlg.confirm = ''
  dlg.open = true
}

async function submitPermanentDeny(): Promise<void> {
  if (!dlg.row || hostMismatch.value) return
  const { row, action, reason } = dlg
  dlg.open = false
  await runDecision(row, action, reason)
}

// Every open request this decide acts on: the broker decides all rows for the
// zone in the clicked row's bottle (the host on any port, and every subzone of
// it), or in every bottle on a global deny. They all lock while it is in
// flight, or a second click on a sibling would double-decide.
function affectedIds(row: OpenRow, action: DecideAction): string[] {
  const ids = (snapshot.value?.open ?? [])
    .filter(
      (r) =>
        (r.host === row.host || r.host.endsWith(`.${row.host}`)) &&
        (action === 'deny_global' || r.container === row.container),
    )
    .map((r) => r.request_id)
  return ids.includes(row.request_id) ? ids : [...ids, row.request_id]
}

function lockRows(ids: string[], action: DecideAction): void {
  for (const id of ids) {
    busy.set(id, (busy.get(id) ?? 0) + 1)
    busyAction.set(id, action)
  }
}

function unlockRows(ids: string[]): void {
  for (const id of ids) {
    const left = (busy.get(id) ?? 1) - 1
    if (left > 0) busy.set(id, left)
    else {
      busy.delete(id)
      busyAction.delete(id)
    }
  }
}

function payloadFor(row: OpenRow, action: DecideAction, reason = ''): DecidePayload {
  const payload: DecidePayload = { action, host: row.host }
  if (action !== 'deny_global') payload.container = row.container
  if (reason) payload.reason = reason
  return payload
}

// What a decide's answer means for its row: the outcome note, and the queue
// re-read that drops the rows the broker decided. Returns how the request
// ended: 'decided' (it leaves the queue), 'needs-cidr' (an IP-literal allow:
// recorded, but the row stays open until the CIDR is added by hand) or
// 'failed' (it stays queued with a failure marked on it). Legacy semantics for
// each outcome; see PLN "Decide outcomes". `bulk` keeps a failed decide off
// the banner: a bulk run reports its failures once, in its own result line.
type Settled = 'decided' | 'needs-cidr' | 'failed'
async function settle(
  row: OpenRow,
  result: Awaited<ReturnType<typeof apiDecide>>,
  bulk: boolean,
): Promise<Settled> {
  const key = row.request_id
  if (!result.ok) {
    if (result.status === 400) {
      notes[key] = { tone: 'deny', text: result.error }
      await refresh()
    } else {
      // 502/503/network: the banner stays until a poll that starts after this
      // failure succeeds, and the row says the decision was not sent. No
      // immediate refresh here: a good one would clear the banner unseen.
      notes[key] = { tone: 'deny', text: `Not sent: ${result.error}` }
      if (!bulk) setStale(result.error)
    }
    return 'failed'
  }

  const failure = (result.data.apply_failures ?? []).find((f) => f.request_id === key)
  let settled: Settled = 'decided'
  if (!failure) {
    notes[key] = { tone: 'neutral', text: 'Decision recorded' }
  } else if (failure.reason === 'ip_requires_cidr') {
    notes[key] = { tone: 'neutral', text: 'Recorded — add the CIDR to the manifest by hand' }
    settled = 'needs-cidr'
  } else {
    notes[key] = { tone: 'deny', text: 'Decision recorded but the rule install failed — the request stays queued' }
    settled = 'failed'
  }
  await refresh()
  return settled
}

async function runDecision(row: OpenRow, action: DecideAction, reason = ''): Promise<void> {
  const locked = affectedIds(row, action)
  lockRows(locked, action)
  notes[row.request_id] = undefined
  const result = await apiDecide(payloadFor(row, action, reason))
  unlockRows(locked)
  await settle(row, result, false)
}

// Bulk decide for one bottle. The broker has no batch endpoint, so this is the
// single-row decide, once per open request, one after another. `rows` is every
// open request of the bottle when the dialog opened, not only the ones the
// filters show: the dialog says so and lists them all.
type BulkAction = 'allow_live' | 'deny'
type BulkRun = { container: string; action: BulkAction; total: number; done: number; failed: number; needsCidr: number; running: boolean }

const bulkAsk = reactive({
  open: false,
  container: '',
  action: 'allow_live' as BulkAction,
  rows: [] as OpenRow[],
  hidden: 0,
})
const bulk = ref<BulkRun | null>(null)
const bulkRunning = computed(() => bulk.value?.running === true)
const bulkPercent = computed(() => (bulk.value ? (100 * bulk.value.done) / bulk.value.total : 0))

function bottleRows(container: string): OpenRow[] {
  return openRows.value.filter((row) => row.container === container).sort(byNewest)
}

// A bottle's Allow all / Deny all are off while a bulk run is going (one at a
// time, so the decides stay sequential) and while any of its rows is mid-decide.
function bulkDisabled(container: string): boolean {
  return bulkRunning.value || bottleRows(container).some((row) => isBusy(row.request_id))
}

function askBulk(container: string, action: BulkAction): void {
  const rows = bottleRows(container)
  bulkAsk.container = container
  bulkAsk.action = action
  bulkAsk.rows = rows
  bulkAsk.hidden = rows.filter((row) => !visibleRows.value.includes(row)).length
  bulkAsk.open = true
}

async function runBulk(): Promise<void> {
  bulkAsk.open = false
  if (bulkRunning.value) return
  const { container, action } = bulkAsk
  // Rows decided or locked since the dialog opened are left out: never a
  // second decide on a request another is already deciding.
  const stillOpen = new Set(openRows.value.map((row) => row.request_id))
  const rows = bulkAsk.rows.filter((row) => stillOpen.has(row.request_id) && !isBusy(row.request_id))
  if (rows.length === 0) return

  const run: BulkRun = reactive({ container, action, total: rows.length, done: 0, failed: 0, needsCidr: 0, running: true })
  bulk.value = run
  const pending = rows.map((row) => row.request_id)
  lockRows(pending, action)
  try {
    for (const row of rows) {
      const id = row.request_id
      // Swept by an earlier decide of this run (a zone decide takes its
      // subdomains with it): nothing left to decide, so nothing is sent.
      if (!openRows.value.some((open) => open.request_id === id)) {
        unlockRows([id])
        pending.splice(pending.indexOf(id), 1)
        run.done += 1
        continue
      }
      notes[id] = undefined
      const result = await apiDecide(payloadFor(row, action))
      unlockRows([id])
      pending.splice(pending.indexOf(id), 1)
      const settled = await settle(row, result, true)
      if (settled === 'failed') run.failed += 1
      else if (settled === 'needs-cidr') run.needsCidr += 1
      run.done += 1
    }
  } finally {
    unlockRows(pending)
    run.running = false
  }
}

const bulkVerb = (action: BulkAction): string => (action === 'allow_live' ? 'Allow' : 'Deny')

const bulkResult = computed(() => {
  const run = bulk.value
  if (!run || run.running) return null
  const done = run.total - run.failed - run.needsCidr
  const verb = run.action === 'allow_live' ? 'Allowed' : 'Denied'
  // An IP-literal allow leaves its row open with a note: counting it as done
  // would say "Allowed 5 of 5" while the request still sits in the queue.
  const cidr = run.needsCidr ? ` · ${run.needsCidr} needs a CIDR in the manifest` : ''
  const failed = run.failed ? ` · ${run.failed} failed and stay${run.failed === 1 ? 's' : ''} open, marked in the list` : ''
  return `${verb} ${done} of ${run.total} in ${run.container}${cidr}${failed}`
})

function onDecide(row: OpenRow, action: DecideAction): void {
  if (action === 'deny_bottle' || action === 'deny_global') {
    openPermanentDeny(row, action)
    return
  }
  void runDecision(row, action)
}
</script>

<template>
  <div class="stack-section">
    <div
      class="toolbar"
      data-testid="queue-filters"
    >
      <SearchField
        v-model="filters.search"
        placeholder="Search destination"
      />
      <BottleMultiFilter
        v-model="filters.bottles"
        :options="bottleOptions"
      />
      <ToggleGroup
        :model-value="filters.state"
        type="single"
        variant="outline"
        class="bg-background"
        data-testid="queue-state"
        aria-label="Request state"
        @update:model-value="chooseState"
      >
        <ToggleGroupItem
          value="all"
          class="h-control"
        >
          All
        </ToggleGroupItem>
        <ToggleGroupItem
          value="failed"
          class="h-control"
        >
          Failed apply
        </ToggleGroupItem>
      </ToggleGroup>
      <Select
        :model-value="filters.age"
        @update:model-value="chooseAge"
      >
        <SelectTrigger
          class="w-select bg-background"
          aria-label="Age"
          data-testid="queue-age"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem
            v-for="option in AGE_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </SelectItem>
        </SelectContent>
      </Select>
      <Button
        v-if="filtered"
        variant="ghost"
        class="h-control"
        data-testid="queue-filter-clear"
        @click="clearFilters"
      >
        <FilterX class="size-icon" /> Clear
      </Button>
    </div>

    <div class="toolbar justify-between">
      <p
        class="row-meta"
        data-testid="summary"
      >
        {{ visibleRows.length }} of {{ snapshot?.count ?? 0 }} open · {{ groups.length }} bottle{{ groups.length === 1 ? '' : 's' }} · newest first
      </p>
      <ToggleGroup
        :model-value="view"
        type="single"
        variant="outline"
        size="sm"
        class="bg-background"
        data-testid="queue-view"
        @update:model-value="chooseView"
      >
        <ToggleGroupItem value="grouped">
          <Group class="size-icon" /> By bottle
        </ToggleGroupItem>
        <ToggleGroupItem value="flat">
          <Rows3 class="size-icon" /> All
        </ToggleGroupItem>
      </ToggleGroup>
    </div>

    <p
      v-if="stale"
      class="inline-row"
      role="alert"
      data-testid="stale-banner"
    >
      <span class="pill pill-warn">{{ staleText }}</span>
    </p>

    <!-- Always rendered, so a screen reader is already watching it when the text
         arrives; the pill shows the same words to everyone else. -->
    <p
      class="sr-only"
      role="status"
      data-testid="filters-cleared-status"
    >
      {{ filtersClearedNote }}
    </p>
    <p
      v-if="filtersClearedNote"
      class="inline-row"
      aria-hidden="true"
    >
      <span class="pill pill-neutral">{{ filtersClearedNote }}</span>
    </p>

    <p
      v-if="bulk && (bulk.running || bulkResult)"
      class="inline-row"
      role="status"
      data-testid="bulk-status"
    >
      <template v-if="bulk.running">
        <span class="pill pill-neutral">{{ bulkVerb(bulk.action) }} all in {{ bulk.container }}: {{ bulk.done }}/{{ bulk.total }}</span>
      </template>
      <template v-else>
        <span
          class="pill"
          :class="bulk.failed ? 'pill-deny' : 'pill-neutral'"
        >{{ bulkResult }}</span>
        <Button
          variant="ghost"
          size="sm"
          @click="bulk = null"
        >
          Dismiss
        </Button>
      </template>
    </p>

    <EmptyState
      v-if="!snapshot?.open.length"
      :message="snapshot ? 'No open requests.' : 'Loading…'"
    />
    <EmptyState
      v-else-if="!visibleRows.length"
      message="Nothing matches these filters."
    />

    <!-- Desktop / tablet -->
    <div
      v-else-if="!compact"
      class="panel"
    >
      <Table>
        <TableHeader>
          <TableRow class="hover:bg-transparent">
            <TableHead>Request</TableHead>
            <TableHead class="w-col-time">
              Filed
            </TableHead>
            <TableHead class="w-col-actions text-right">
              Decision
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <template
            v-for="s in sections"
            :key="s.container ?? 'all'"
          >
            <TableRow
              v-if="s.container"
              class="bg-muted/50 hover:bg-muted/50"
              data-testid="group"
            >
              <TableCell
                colspan="3"
                class="py-group-y"
              >
                <div class="toolbar">
                  <Box class="size-icon" />
                  <span class="font-semibold">{{ s.container }}</span>
                  <span class="pill pill-neutral rounded-full">{{ s.rows.length }} open</span>
                  <div class="flex-1" />
                  <template v-if="bulk?.running && bulk.container === s.container">
                    <div class="w-col-time">
                      <Progress
                        :model-value="bulkPercent"
                        :aria-label="`${bulkVerb(bulk.action)} all in ${s.container}`"
                        data-testid="bulk-progress"
                      />
                    </div>
                    <span
                      class="row-meta"
                      data-testid="bulk-count"
                    >{{ bulk.done }}/{{ bulk.total }}</span>
                  </template>
                  <template v-else>
                    <Button
                      variant="ghost"
                      size="sm"
                      class="text-allow-text hover:text-allow-hover"
                      :disabled="bulkDisabled(s.container)"
                      :aria-label="`Allow all in ${s.container}`"
                      @click="askBulk(s.container, 'allow_live')"
                    >
                      <Check class="size-icon" /> Allow all
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      class="text-destructive hover:text-destructive"
                      :disabled="bulkDisabled(s.container)"
                      :aria-label="`Deny all in ${s.container}`"
                      @click="askBulk(s.container, 'deny')"
                    >
                      <X class="size-icon" /> Deny all
                    </Button>
                  </template>
                </div>
              </TableCell>
            </TableRow>
            <TableRow
              v-for="r in s.rows"
              :key="r.request_id"
              class="request-target"
              tabindex="-1"
              :data-request-id="r.request_id"
              data-testid="request"
            >
              <TableCell :class="s.container && 'pl-indent'">
                <RequestSummary
                  :row="r"
                  :show-bottle="!s.container"
                  :note="notes[r.request_id]"
                />
              </TableCell>
              <TableCell>
                <div class="stack-line">
                  <p class="text-body font-medium">
                    {{ relTime(r.opened_at) }}
                  </p>
                  <p class="row-caption">
                    {{ clock(r.opened_at) }}
                  </p>
                </div>
              </TableCell>
              <TableCell>
                <DecideButtons
                  :row="r"
                  :busy="isBusy(r.request_id)"
                  :pending="busyAction.get(r.request_id)"
                  @decide="(a) => onDecide(r, a)"
                  @permanent-deny="(a) => openPermanentDeny(r, a)"
                />
              </TableCell>
            </TableRow>
          </template>
        </TableBody>
      </Table>
    </div>

    <!-- Compact: tablet and phone -->
    <div
      v-else
      class="stack-section"
    >
      <section
        v-for="s in sections"
        :key="s.container ?? 'all'"
        class="stack-line"
      >
        <div
          v-if="s.container"
          class="inline-row"
          data-testid="group"
        >
          <Box class="size-icon" /><span class="font-semibold">{{ s.container }}</span>
          <span class="pill pill-neutral rounded-full">{{ s.rows.length }} open</span>
          <div class="flex-1" />
          <template v-if="bulk?.running && bulk.container === s.container">
            <div class="w-col-time">
              <Progress
                :model-value="bulkPercent"
                :aria-label="`${bulkVerb(bulk.action)} all in ${s.container}`"
                data-testid="bulk-progress"
              />
            </div>
            <span
              class="row-meta"
              data-testid="bulk-count"
            >{{ bulk.done }}/{{ bulk.total }}</span>
          </template>
          <template v-else>
            <Button
              variant="ghost"
              size="sm"
              class="text-allow-text hover:text-allow-hover"
              :disabled="bulkDisabled(s.container)"
              :aria-label="`Allow all in ${s.container}`"
              @click="askBulk(s.container, 'allow_live')"
            >
              <Check class="size-icon" /> Allow all
            </Button>
            <Button
              variant="ghost"
              size="sm"
              class="text-destructive hover:text-destructive"
              :disabled="bulkDisabled(s.container)"
              :aria-label="`Deny all in ${s.container}`"
              @click="askBulk(s.container, 'deny')"
            >
              <X class="size-icon" /> Deny all
            </Button>
          </template>
        </div>
        <article
          v-for="r in s.rows"
          :key="r.request_id"
          class="item-card request-target"
          tabindex="-1"
          :data-request-id="r.request_id"
          data-testid="request"
        >
          <RequestSummary
            :row="r"
            :show-bottle="!s.container"
            :note="notes[r.request_id]"
            :when="relTime(r.opened_at)"
          />
          <DecideButtons
            :row="r"
            :busy="isBusy(r.request_id)"
            :pending="busyAction.get(r.request_id)"
            stretch
            @decide="(a) => onDecide(r, a)"
            @permanent-deny="(a) => openPermanentDeny(r, a)"
          />
        </article>
      </section>
    </div>

    <!-- Recent (24 h) -->
    <section
      v-if="recentRows.length || denylistRows.length"
      class="stack-section"
      data-testid="recent"
    >
      <h2 class="text-lead font-semibold">
        Recent decisions (24 h)
      </h2>
      <Collapsible
        v-if="denylistRows.length"
        v-model:open="denylistOpen"
        class="panel"
        data-testid="denylist-group"
      >
        <CollapsibleTrigger
          class="cell-group toolbar w-full text-left"
          data-testid="denylist-toggle"
        >
          <Ban class="size-icon text-muted-foreground" />
          <span class="font-medium">Denylist</span>
          <span
            class="pill pill-neutral rounded-full"
            data-testid="denylist-hits"
          >{{ denylistHits }} hit{{ denylistHits === 1 ? '' : 's' }}</span>
          <span class="row-meta max-sm:hidden">Blocked automatically, no decision needed</span>
          <ChevronDown
            class="size-icon ml-auto transition-transform"
            :class="denylistOpen && 'rotate-180'"
          />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div class="divide-y border-t">
            <div
              v-for="r in denylistRows"
              :key="r.request_id"
              class="cell-group stack-line"
              data-testid="recent-row"
            >
              <div class="inline-row">
                <span class="row-title">{{ r.host }}<span class="font-normal text-muted-foreground">:{{ r.port }}</span></span>
                <span
                  class="pill pill-neutral"
                  data-testid="denylist-row-hits"
                >{{ r.hit_count }}×</span>
              </div>
              <div class="overflow-hidden">
                <div class="meta-flow row-meta">
                  <span class="meta-item inline-flex items-center gap-tight font-medium text-foreground"><Box class="size-icon-sm" />{{ r.container }}</span>
                  <span class="meta-item row-caption">{{ relTime(r.decided_at) }}</span>
                </div>
              </div>
              <p
                v-if="r.deny_reason"
                class="row-caption"
              >
                {{ r.deny_reason }}
              </p>
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
      <div
        v-if="recentRows.length"
        class="panel divide-y"
        data-testid="recent-list"
      >
        <div
          v-for="r in recentRows"
          :key="r.request_id"
          class="cell-group stack-line"
          data-testid="recent-row"
        >
          <div class="row-title">
            {{ r.host }}<span class="font-normal text-muted-foreground">:{{ r.port }}</span>
          </div>
          <div class="overflow-hidden">
            <div class="meta-flow row-meta">
              <span class="meta-item inline-flex items-center gap-tight font-medium text-foreground"><Box class="size-icon-sm" />{{ r.container }}</span>
              <span class="meta-item"><span
                class="pill"
                :class="outcomeTone(r)"
              >{{ outcomeLabel(r) }}</span></span>
              <span class="meta-item row-caption">{{ relTime(r.decided_at) }}</span>
            </div>
          </div>
          <p
            v-if="r.deny_reason"
            class="row-caption"
          >
            {{ r.deny_reason }}
          </p>
        </div>
      </div>
    </section>
  </div>

  <!-- Bulk confirm -->
  <AlertDialog v-model:open="bulkAsk.open">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>
          {{ bulkVerb(bulkAsk.action) }} all {{ bulkAsk.rows.length }} open request{{ bulkAsk.rows.length === 1 ? '' : 's' }} for {{ bulkAsk.container }}?
        </AlertDialogTitle>
        <AlertDialogDescription
          as="div"
          class="stack-section"
        >
          <p v-if="bulkAsk.hidden">
            This is every open request in the bottle, including {{ bulkAsk.hidden }} the current filters hide.
          </p>
          <ul
            class="stack-line font-mono text-body text-foreground"
            data-testid="bulk-hosts"
          >
            <li
              v-for="r in bulkAsk.rows"
              :key="r.request_id"
            >
              {{ r.host }}:{{ r.port }}
            </li>
          </ul>
          <p>
            {{ bulkAsk.action === 'allow_live'
              ? 'Each is allowed until the bottle restarts, as the Allow button on its row does.'
              : 'Each is denied once. Nothing is added to a denylist.' }}
            They are decided one after another; one that fails stays in the list, marked.
          </p>
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Cancel</AlertDialogCancel>
        <AlertDialogAction
          :variant="bulkAsk.action === 'allow_live' ? 'allow' : 'destructive'"
          @click="runBulk"
        >
          {{ bulkVerb(bulkAsk.action) }} {{ bulkAsk.rows.length }}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>

  <!-- Permanent deny dialog -->
  <Dialog v-model:open="dlg.open">
    <DialogContent class="max-w-dialog">
      <DialogHeader>
        <DialogTitle>{{ dlg.action === 'deny_bottle' ? 'Deny permanently · bottle' : 'Deny permanently · global' }}</DialogTitle>
        <DialogDescription>
          <span class="font-mono text-foreground">{{ dlg.row?.host }}:{{ dlg.row?.port }}</span>
          <template v-if="dlg.action === 'deny_bottle'">
            is denied for <strong class="text-foreground">{{ dlg.row?.container }}</strong> from now on. It will not ask again.
          </template>
          <template v-else>
            goes on the global denylist. <strong class="text-foreground">No bottle</strong> can reach it, and none will ask.
          </template>
        </DialogDescription>
      </DialogHeader>
      <div class="stack-section">
        <div
          v-if="dlg.action === 'deny_global'"
          class="stack-line"
        >
          <Label for="confirm-host">Type <span class="font-mono">{{ dlg.row?.host }}</span> to confirm</Label>
          <Input
            id="confirm-host"
            v-model="dlg.confirm"
            autocomplete="off"
            :aria-invalid="!!dlg.confirm && hostMismatch"
          />
          <p
            v-if="dlg.confirm && hostMismatch"
            class="row-error"
            role="alert"
          >
            The host does not match. Type it exactly to enable the button.
          </p>
        </div>
        <div class="stack-line">
          <Label for="deny-reason">Reason <span class="font-normal text-muted-foreground">(optional, shown to the agent)</span></Label>
          <Textarea
            id="deny-reason"
            v-model="dlg.reason"
            :maxlength="REASON_MAX"
            rows="2"
            class="field-sizing-fixed"
          />
          <p class="row-caption text-right">
            {{ dlg.reason.length }}/{{ REASON_MAX }}
          </p>
        </div>
      </div>
      <DialogFooter>
        <Button
          variant="ghost"
          @click="dlg.open = false"
        >
          Cancel
        </Button>
        <Button
          variant="destructive"
          :disabled="hostMismatch"
          @click="submitPermanentDeny"
        >
          <X class="size-icon" /> Deny permanently
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
