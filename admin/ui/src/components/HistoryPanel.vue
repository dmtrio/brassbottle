<script setup lang="ts">
import { computed, onMounted, ref, type Ref } from 'vue'
import { AlertTriangle, Box, CalendarDays, ChevronLeft, ChevronRight } from '@lucide/vue'
import { useIntervalFn, useMediaQuery } from '@vueuse/core'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RangeCalendar } from '@/components/ui/range-calendar'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useHistory, type HistoryFilters } from '@/composables/useHistory'
import { useQueue } from '@/composables/useQueue'
import { decidedAt, decidedDay, outcomeLabel, outcomeTone, relTime } from '@/lib/decision'
import { rangeToQuery, type DayRange } from '@/lib/history'
import type { DateRange } from 'reka-ui'
import { getLocalTimeZone } from '@internationalized/date'
import BottleFilter from './BottleFilter.vue'
import EmptyState from './EmptyState.vue'
import SearchField from './SearchField.vue'

type StatusFilter = 'all' | 'allowed' | 'denied'

const phone = useMediaQuery('(max-width: 639px)')

// History does not poll, so the relative times ("10m ago") read this clock and
// refresh once a minute on an open page.
const now = ref(Date.now())
useIntervalFn(() => { now.value = Date.now() }, 60_000)

// Search and the status toggle filter the loaded page only. The bottle and the
// date range are broker filters: changing one starts the walk again from the
// newest page.
const search = ref('')
const status = ref<StatusFilter>('all')
const bottle = ref<string | null>(null)
const range = ref({ start: undefined, end: undefined }) as Ref<DateRange>

const filters = computed<HistoryFilters>(() => ({
  container: bottle.value,
  ...rangeToQuery(range.value as DayRange),
}))

const history = useHistory(filters)
const { snapshot } = useQueue()
onMounted(() => void history.load())

// A toggle group deselects on a second click; a status must always be chosen.
function chooseStatus(next: unknown): void {
  if (next === 'all' || next === 'allowed' || next === 'denied') status.value = next
}

const bottleOptions = computed(() => {
  const names = new Set(history.bottles.value)
  for (const row of snapshot.value?.open ?? []) names.add(row.container)
  for (const row of snapshot.value?.recent ?? []) names.add(row.container)
  if (bottle.value) names.add(bottle.value)
  return [...names].sort()
})

const visible = computed(() => {
  const needle = search.value.trim().toLowerCase()
  return history.rows.value.filter((row) => {
    if (status.value !== 'all' && row.status !== status.value) return false
    return !needle || row.host.toLowerCase().includes(needle)
  })
})

const rangeLabel = computed(() => {
  const { start, end } = range.value
  if (!start) return 'Any date'
  const day = (d: typeof start) =>
    d.toDate(getLocalTimeZone()).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return end ? `${day(start)} – ${day(end)}` : day(start)
})

// The calendar reports an empty range on Escape, which also closes the
// popover; that must not drop the filter. Only the Clear button empties it.
function pickRange(next: DateRange): void {
  if (next.start) range.value = next
}

function clearRange(): void {
  range.value = { start: undefined, end: undefined }
}
</script>

<template>
  <div class="stack-section">
    <div class="toolbar">
      <SearchField
        v-model="search"
        placeholder="Search destination"
      />
      <Popover>
        <PopoverTrigger as-child>
          <Button
            variant="outline"
            class="h-control bg-background"
            :aria-label="`Date range: ${rangeLabel}`"
            data-testid="history-range"
          >
            <CalendarDays class="size-icon" />{{ rangeLabel }}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          class="w-auto p-0"
          align="start"
        >
          <RangeCalendar
            :model-value="range"
            :number-of-months="phone ? 1 : 2"
            @update:model-value="pickRange"
          />
          <div class="cell-group flex justify-end border-t">
            <Button
              variant="ghost"
              size="sm"
              data-testid="history-range-clear"
              @click="clearRange"
            >
              Clear
            </Button>
          </div>
        </PopoverContent>
      </Popover>
      <BottleFilter
        v-model="bottle"
        :options="bottleOptions"
      />
      <ToggleGroup
        :model-value="status"
        type="single"
        variant="outline"
        class="bg-background"
        data-testid="history-status"
        @update:model-value="chooseStatus"
      >
        <ToggleGroupItem
          value="all"
          class="h-control"
        >
          All
        </ToggleGroupItem>
        <ToggleGroupItem
          value="allowed"
          class="h-control"
        >
          Allowed
        </ToggleGroupItem>
        <ToggleGroupItem
          value="denied"
          class="h-control"
        >
          Denied
        </ToggleGroupItem>
      </ToggleGroup>
    </div>

    <p
      class="row-meta"
      data-testid="history-summary"
    >
      {{ visible.length }} of {{ history.rows.value.length }} on this page · newest first · whole store
    </p>

    <p
      v-if="history.error.value"
      class="inline-row"
      role="alert"
      data-testid="history-error"
    >
      <span class="pill pill-warn">Could not load history: {{ history.error.value }}</span>
      <Button
        variant="outline"
        size="sm"
        @click="history.retry()"
      >
        Retry
      </Button>
    </p>

    <EmptyState
      v-if="!visible.length"
      :message="history.loaded.value ? 'No decisions match.' : history.error.value ? 'Not loaded.' : 'Loading…'"
    />

    <div
      v-else
      class="panel divide-y"
    >
      <div
        v-for="r in visible"
        :key="r.request_id"
        class="cell-group stack-line"
        data-testid="history-row"
      >
        <div class="flex items-start justify-between gap-inline">
          <div
            class="row-title min-w-0 truncate"
            data-testid="history-row-host"
            :title="`${r.host}:${r.port}`"
          >
            {{ r.host }}<span class="font-normal text-muted-foreground">:{{ r.port }}</span>
          </div>
          <span
            class="row-caption flex-none"
            data-testid="history-row-when"
          ><span class="sm:hidden">{{ decidedDay(r.decided_at) }}</span><span class="hidden sm:inline">{{ decidedAt(r.decided_at) }} · {{ relTime(r.decided_at, now) }}</span></span>
        </div>
        <div class="overflow-hidden">
          <div class="meta-line row-meta">
            <span class="meta-item inline-flex flex-none items-center gap-inline">
              <span
                class="pill"
                :class="outcomeTone(r)"
              >{{ outcomeLabel(r) }}</span>
              <span
                v-if="r.apply_status === 'apply_failed'"
                class="pill pill-deny"
              ><AlertTriangle class="size-icon-sm" />apply failed</span>
            </span>
            <span
              class="meta-item flex min-w-0 items-center gap-tight font-medium text-foreground"
              data-testid="history-row-bottle"
            ><Box class="size-icon-sm flex-none" /><span class="truncate">{{ r.container }}</span></span>
            <span class="meta-item hidden flex-none sm:block">by {{ r.decided_by }}</span>
            <span
              v-if="r.deny_reason"
              class="meta-item hidden min-w-reason-min flex-1 basis-0 truncate sm:block"
              data-testid="history-row-reason"
              :title="r.deny_reason"
            >{{ r.deny_reason }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="toolbar justify-between">
      <p
        class="row-meta"
        data-testid="history-page"
      >
        Page {{ history.pageNumber.value }}
      </p>
      <div class="inline-row">
        <Button
          variant="outline"
          size="sm"
          :disabled="history.pageNumber.value === 1 || history.loading.value"
          data-testid="history-newer"
          @click="history.newer()"
        >
          <ChevronLeft class="size-icon" /> Newer
        </Button>
        <Button
          variant="outline"
          size="sm"
          :disabled="!history.next.value || history.loading.value"
          data-testid="history-older"
          @click="history.older()"
        >
          Older <ChevronRight class="size-icon" />
        </Button>
      </div>
    </div>
  </div>
</template>
