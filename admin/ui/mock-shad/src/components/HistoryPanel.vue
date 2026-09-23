<script setup lang="ts">
import { computed, ref, watch, type Ref } from 'vue'
import type { DateRange } from 'reka-ui'
import { getLocalTimeZone } from '@internationalized/date'
import { Box, CalendarDays, ChevronLeft, ChevronRight, AlertTriangle } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import SearchField from './SearchField.vue'
import BottleFilter from './BottleFilter.vue'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RangeCalendar } from '@/components/ui/range-calendar'
import { state, BOTTLES, relTime, type DecidedRow } from '@/mock/data'
import { usePhone } from '@/mock/ui'

const phone = usePhone()
const search = ref('')
const bottles = ref<string[]>([])
const status = ref<'all' | 'allowed' | 'denied'>('all')
const range = ref({ start: undefined, end: undefined }) as Ref<DateRange>
const pageIdx = ref(0)
const PER = 25

const filtered = computed(() => state.recent.filter((r) => {
  if (bottles.value.length && !bottles.value.includes(r.container)) return false
  if (status.value !== 'all' && r.status !== status.value) return false
  if (search.value && !r.host.includes(search.value.trim().toLowerCase())) return false
  if (range.value.start) {
    const from = range.value.start.toDate(getLocalTimeZone()).getTime()
    const to = (range.value.end ?? range.value.start).toDate(getLocalTimeZone()).getTime() + 86400000
    const t = Date.parse(r.decided_at)
    if (t < from || t >= to) return false
  }
  return true
}))
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PER)))
const page = computed(() => filtered.value.slice(pageIdx.value * PER, pageIdx.value * PER + PER))
const reset = () => { pageIdx.value = 0 }
const rangeLabel = computed(() => {
  const { start, end } = range.value
  if (!start) return 'Any date'
  const f = (d: typeof start) => d!.toDate(getLocalTimeZone()).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return end ? `${f(start)} – ${f(end)}` : f(start)
})
watch([search, bottles, status, range], reset)

function decision(r: DecidedRow): { label: string; pill: string } {
  if (r.status === 'allowed') return { label: r.scope === 'manifest' ? 'Allowed permanently · bottle' : 'Allowed', pill: 'pill-allow' }
  if (r.decided_by === 'denylist') return { label: 'Denylist', pill: 'pill-neutral' }
  if (r.scope === 'bottle') return { label: 'Denied permanently · bottle', pill: 'pill-deny' }
  if (r.scope === 'global') return { label: 'Denied permanently · global', pill: 'pill-deny' }
  return { label: 'Denied', pill: 'pill-deny' }
}
const day = (s: string) => new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
const clock = (s: string) => new Date(s).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
</script>

<template>
  <div class="stack-section">
    <div class="toolbar">
      <SearchField v-model="search" placeholder="Search destination" />
      <Popover>
        <PopoverTrigger as-child>
          <Button variant="outline" class="h-control bg-background"><CalendarDays class="size-icon" />{{ rangeLabel }}</Button>
        </PopoverTrigger>
        <PopoverContent class="w-auto p-0" align="start">
          <RangeCalendar v-model="range" :number-of-months="phone ? 1 : 2" />
          <div class="cell-group flex justify-end border-t"><Button variant="ghost" size="sm" @click="range = { start: undefined, end: undefined }">Clear</Button></div>
        </PopoverContent>
      </Popover>
      <BottleFilter v-model="bottles" :options="BOTTLES" />
      <ToggleGroup v-model="status" type="single" variant="outline" class="bg-background">
        <ToggleGroupItem value="all" class="h-control">All</ToggleGroupItem>
        <ToggleGroupItem value="allowed" class="h-control">Allowed</ToggleGroupItem>
        <ToggleGroupItem value="denied" class="h-control">Denied</ToggleGroupItem>
      </ToggleGroup>
    </div>
    <p class="row-meta">{{ filtered.length }} decisions · newest first · whole store</p>

    <div v-if="!phone" class="panel">
      <Table>
        <TableHeader>
          <TableRow class="hover:bg-transparent">
            <TableHead >Request</TableHead>
            <TableHead >Decision</TableHead>
            <TableHead class="w-col-date">Decided</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow v-for="r in page" :key="r.request_id">
            <TableCell class="py-cell-y-compact">
              <div class="stack-line">
                <p class="row-title">{{ r.host }}<span class="font-normal text-muted-foreground">:{{ r.port }}</span></p>
                <p class="inline-row row-meta"><span class="inline-flex items-center gap-tight"><Box class="size-icon-sm" />{{ r.container }}</span>
                  <template v-if="r.deny_reason"><span aria-hidden="true">·</span>{{ r.deny_reason }}</template></p>
              </div>
            </TableCell>
            <TableCell class="py-cell-y-compact">
              <div class="stack-line">
                <div class="inline-row">
                  <span class="pill" :class="decision(r).pill">{{ decision(r).label }}</span>
                  <span v-if="r.hit_count > 1" class="pill pill-neutral">{{ r.hit_count }}×</span>
                  <span v-if="r.apply_status === 'failed'" class="pill pill-deny"><AlertTriangle class="size-icon-sm" /> apply failed</span>
                </div>
                <p class="row-caption">by {{ r.decided_by }}</p>
              </div>
            </TableCell>
            <TableCell class="py-cell-y-compact">
              <div class="stack-line">
                <p class="text-body font-medium">{{ day(r.decided_at) }}</p>
                <p class="row-caption">{{ clock(r.decided_at) }} · {{ relTime(r.decided_at) }}</p>
              </div>
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
    <div v-else class="stack-line">
      <article v-for="r in page" :key="r.request_id" class="item-card">
        <div class="stack-line">
          <div class="flex items-start justify-between gap-inline">
            <p class="row-title">{{ r.host }}</p>
            <span class="row-caption flex-none">{{ day(r.decided_at) }}</span>
          </div>
          <div class="inline-row row-meta">
            <span class="pill" :class="decision(r).pill">{{ decision(r).label }}</span>
            <span>{{ r.container }}</span>
          </div>
        </div>
      </article>
    </div>

    <div class="toolbar justify-between">
      <p class="row-meta">Page {{ pageIdx + 1 }} of {{ pages }}</p>
      <div class="inline-row">
        <Button variant="outline" size="sm" :disabled="pageIdx === 0" @click="pageIdx--"><ChevronLeft class="size-icon" /> Newer</Button>
        <Button variant="outline" size="sm" :disabled="pageIdx >= pages - 1" @click="pageIdx++">Older <ChevronRight class="size-icon" /></Button>
      </div>
    </div>
  </div>
</template>
