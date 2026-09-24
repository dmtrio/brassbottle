<script setup lang="ts">
import { computed, onMounted, ref, type Ref } from 'vue'
import { AlertTriangle, Box, CalendarDays, ChevronLeft, ChevronRight } from '@lucide/vue'
import { useMediaQuery } from '@vueuse/core'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RangeCalendar } from '@/components/ui/range-calendar'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useHistory, type HistoryFilters } from '@/composables/useHistory'
import { useQueue } from '@/composables/useQueue'
import { decidedAt, outcomeLabel, outcomeTone } from '@/lib/decision'
import { rangeToQuery, type DayRange } from '@/lib/history'
import type { DateRange } from 'reka-ui'
import { getLocalTimeZone } from '@internationalized/date'
import BottleFilter from './BottleFilter.vue'
import EmptyState from './EmptyState.vue'
import SearchField from './SearchField.vue'

type StatusFilter = 'all' | 'allowed' | 'denied'

const phone = useMediaQuery('(max-width: 639px)')

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
        <div class="row-title">
          {{ r.host }}<span class="font-normal text-muted-foreground">:{{ r.port }}</span>
        </div>
        <div class="overflow-hidden">
          <div class="meta-flow row-meta">
            <span class="meta-item inline-flex items-center gap-tight font-medium text-foreground"><Box class="size-icon-sm" />{{ r.container }}</span>
            <span class="meta-item inline-flex items-center gap-inline">
              <span
                class="pill"
                :class="outcomeTone(r)"
              >{{ outcomeLabel(r) }}</span>
              <span
                v-if="r.apply_status === 'apply_failed'"
                class="pill pill-deny"
              ><AlertTriangle class="size-icon-sm" />apply failed</span>
            </span>
            <span class="meta-item">by {{ r.decided_by }}</span>
            <span class="meta-item row-caption">{{ decidedAt(r.decided_at) }}</span>
            <span
              v-if="r.deny_reason"
              class="meta-item"
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
