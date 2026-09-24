<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { Box, Group, Rows3, X } from '@lucide/vue'
import { useMediaQuery } from '@vueuse/core'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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
import { useQueue } from '@/composables/useQueue'
import { outcomeLabel, outcomeTone, relTime } from '@/lib/decision'
import type { OpenRow } from '@/types'
import DecideButtons from './DecideButtons.vue'
import EmptyState from './EmptyState.vue'
import RequestSummary, { type RowNote } from './RequestSummary.vue'

const REASON_MAX = 200

// Below 1024 px the sidebar leaves the table too little width (the Decision
// column clips and hosts break mid-word), so requests become cards.
const compact = useMediaQuery('(max-width: 1023px)')
const { snapshot, stale, refresh, setStale } = useQueue()

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
const notes = reactive<Record<string, RowNote | undefined>>({})

function byNewest(a: OpenRow, b: OpenRow): number {
  return Date.parse(b.opened_at) - Date.parse(a.opened_at)
}

// Grouped by hand rather than with TanStack's getGroupedRowModel: "bottles
// alphabetical, rows newest first within" is a plain sort and the grouped row
// model would need overriding to express it.
const groups = computed(() => {
  const byBottle = new Map<string, OpenRow[]>()
  for (const row of snapshot.value?.open ?? []) {
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
    : [{ container: null as string | null, rows: [...(snapshot.value?.open ?? [])].sort(byNewest) }],
)

const recentRows = computed(() =>
  [...(snapshot.value?.recent ?? [])].sort((a, b) => Date.parse(b.decided_at) - Date.parse(a.decided_at)),
)

function clock(isoTs: string): string {
  return new Date(isoTs).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

// The banner says which data the list still shows: the last good snapshot.
const staleText = computed(() => {
  if (!stale.value) return null
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

// Legacy semantics for each outcome; see PLN "Decide outcomes".
async function runDecision(row: OpenRow, action: DecideAction, reason = ''): Promise<void> {
  const key = row.request_id
  const payload: DecidePayload = { action, host: row.host }
  if (action !== 'deny_global') payload.container = row.container
  if (reason) payload.reason = reason

  const locked = affectedIds(row, action)
  for (const id of locked) busy.set(id, (busy.get(id) ?? 0) + 1)
  notes[key] = undefined
  const result = await apiDecide(payload)
  for (const id of locked) {
    const left = (busy.get(id) ?? 1) - 1
    if (left > 0) busy.set(id, left)
    else busy.delete(id)
  }

  if (!result.ok) {
    if (result.status === 400) {
      notes[key] = { tone: 'deny', text: result.error }
      await refresh()
    } else {
      // 502/503/network: the banner stays until a poll that starts after this
      // failure succeeds, and the row says the decision was not sent. No
      // immediate refresh here: a good one would clear the banner unseen.
      notes[key] = { tone: 'deny', text: `Not sent: ${result.error}` }
      setStale(result.error)
    }
    return
  }

  const failure = (result.data.apply_failures ?? []).find((f) => f.request_id === key)
  if (!failure) {
    notes[key] = { tone: 'neutral', text: 'Decision recorded' }
  } else if (failure.reason === 'ip_requires_cidr') {
    notes[key] = { tone: 'neutral', text: 'Recorded — add the CIDR to the manifest by hand' }
  } else {
    notes[key] = { tone: 'deny', text: 'Decision recorded but the rule install failed — the request stays queued' }
  }
  await refresh()
}

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
    <div class="toolbar justify-between">
      <p
        class="row-meta"
        data-testid="summary"
      >
        {{ snapshot?.count ?? 0 }} open · {{ groups.length }} bottle{{ groups.length === 1 ? '' : 's' }} · newest first
      </p>
      <ToggleGroup
        :model-value="view"
        type="single"
        variant="outline"
        size="sm"
        class="bg-background"
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

    <EmptyState
      v-if="!snapshot?.open.length"
      :message="snapshot ? 'No open requests.' : 'Loading…'"
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
                </div>
              </TableCell>
            </TableRow>
            <TableRow
              v-for="r in s.rows"
              :key="r.request_id"
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
        </div>
        <article
          v-for="r in s.rows"
          :key="r.request_id"
          class="item-card"
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
            stretch
            @decide="(a) => onDecide(r, a)"
            @permanent-deny="(a) => openPermanentDeny(r, a)"
          />
        </article>
      </section>
    </div>

    <!-- Recent (24 h) -->
    <section
      v-if="recentRows.length"
      class="stack-section"
      data-testid="recent"
    >
      <h2 class="text-lead font-semibold">
        Recent decisions (24 h)
      </h2>
      <div class="panel divide-y">
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
