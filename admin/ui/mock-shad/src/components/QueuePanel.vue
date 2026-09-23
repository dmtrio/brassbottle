<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { toast } from 'vue-sonner'
import { Box, ChevronDown, Check, X, FilterX, Ban, Rows3, Group } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import SearchField from './SearchField.vue'
import BottleFilter from './BottleFilter.vue'
import EmptyState from './EmptyState.vue'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import RequestSummary from './RequestSummary.vue'
import DecideButtons from './DecideButtons.vue'
import { state, decide, relTime, byNewest, ACTION_LABEL, type OpenRow, type DecideAction } from '@/mock/data'
import { usePhone } from '@/mock/ui'

const phone = usePhone()

// View + filters
const view = ref<'grouped' | 'flat'>('grouped')
const search = ref('')
const bottles = ref<string[]>([])
const stateFilter = ref<'all' | 'failed'>('all')
const age = ref('any')
const bottleOptions = computed(() => [...new Set(state.open.map((r) => r.container))].sort())
const filtered = computed(() => state.open.filter((r) => {
  if (search.value && !`${r.host}:${r.port}`.includes(search.value.trim().toLowerCase())) return false
  if (bottles.value.length && !bottles.value.includes(r.container)) return false
  if (stateFilter.value === 'failed' && !r.last_error) return false
  if (age.value === '5m' && r.age_seconds >= 300) return false
  if (age.value === '1h' && r.age_seconds >= 3600) return false
  if (age.value === 'old' && r.age_seconds < 3600) return false
  return true
}).sort(byNewest))
const groups = computed(() => {
  const m = new Map<string, OpenRow[]>()
  for (const r of filtered.value) m.set(r.container, [...(m.get(r.container) ?? []), r])
  return [...m.entries()].sort(([a], [b]) => a.localeCompare(b))
})
// One shape for both views: a list of sections; the flat view is one untitled section.
const sections = computed(() => view.value === 'grouped'
  ? groups.value.map(([container, rows]) => ({ container, rows }))
  : [{ container: null as string | null, rows: filtered.value }])
const failedCount = computed(() => state.open.filter((r) => r.last_error).length)
const filtersActive = computed(() => !!search.value || bottles.value.length > 0 || stateFilter.value !== 'all' || age.value !== 'any')
function clearFilters() { search.value = ''; bottles.value = []; stateFilter.value = 'all'; age.value = 'any' }

// Decisions
const busy = reactive(new Set<string>())
const failed = reactive(new Set<string>())
async function run(row: OpenRow, action: DecideAction, reason?: string, quiet = false) {
  busy.add(row.request_id); failed.delete(row.request_id)
  try {
    await decide(row, action, reason)
    if (!quiet) (action.startsWith('allow') ? toast.success : toast.warning)(ACTION_LABEL[action], { description: `${row.container} → ${row.host}` })
    return true
  } catch (e) {
    failed.add(row.request_id)
    toast.error('Decision not applied', { description: `${row.host}: ${(e as Error).message}` })
    return false
  } finally { busy.delete(row.request_id) }
}

// Permanent deny dialog
const dlg = reactive({ open: false, row: null as OpenRow | null, action: 'deny_bottle' as 'deny_bottle' | 'deny_global', reason: '', confirm: '' })
function openDeny(row: OpenRow, action: 'deny_bottle' | 'deny_global') { Object.assign(dlg, { open: true, row, action, reason: '', confirm: '' }) }
const dlgOk = computed(() => dlg.action !== 'deny_global' || dlg.confirm.trim() === dlg.row?.host)
async function submitDeny() { if (!dlg.row || !dlgOk.value) return; dlg.open = false; await run(dlg.row, dlg.action, dlg.reason) }

// Bulk per bottle
const bulkAsk = reactive({ open: false, container: '', rows: [] as OpenRow[], action: 'allow_live' as 'allow_live' | 'deny' })
const bulk = reactive<Record<string, { done: number; total: number; failed: number }>>({})
function askBulk(container: string, rows: OpenRow[], action: 'allow_live' | 'deny') { Object.assign(bulkAsk, { open: true, container, rows: [...rows], action }) }
async function runBulk() {
  const { container, rows, action } = bulkAsk
  bulkAsk.open = false
  bulk[container] = { done: 0, total: rows.length, failed: 0 }
  for (const r of rows) { const ok = await run(r, action, undefined, true); bulk[container].done++; if (!ok) bulk[container].failed++ }
  const b = bulk[container]
  ;(b.failed ? toast.warning : toast.success)(`${ACTION_LABEL[action]}: ${b.total - b.failed} of ${b.total} done`, {
    description: b.failed ? `${b.failed} failed and stay open, marked in the list.` : container })
  delete bulk[container]
}

// Denylist hits
const denyHits = computed(() => state.recent.filter((r) => r.status === 'denied' && r.decided_by === 'denylist'))
const denyHitTotal = computed(() => denyHits.value.reduce((n, r) => n + r.hit_count, 0))
const hitsOpen = ref(false)
const clock = (s: string) => new Date(s).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
</script>

<template>
  <div class="stack-section">
    <div class="toolbar">
      <SearchField v-model="search" placeholder="Search destination" />
      <BottleFilter v-model="bottles" :options="bottleOptions" />
      <ToggleGroup v-model="stateFilter" type="single" variant="outline" class="bg-background">
        <ToggleGroupItem value="all" class="h-control">All</ToggleGroupItem>
        <ToggleGroupItem value="failed" class="h-control">Failed apply</ToggleGroupItem>
      </ToggleGroup>
      <Select v-model="age">
        <SelectTrigger class="w-select bg-background"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="any">Any age</SelectItem>
          <SelectItem value="5m">Under 5 minutes</SelectItem>
          <SelectItem value="1h">Under 1 hour</SelectItem>
          <SelectItem value="old">Older than 1 hour</SelectItem>
        </SelectContent>
      </Select>
      <Button v-if="filtersActive" variant="ghost" class="h-control" @click="clearFilters"><FilterX class="size-icon" /> Clear</Button>
    </div>

    <div class="toolbar justify-between">
      <p class="row-meta">
        {{ filtered.length }} of {{ state.open.length }} open · {{ groups.length }} bottle{{ groups.length === 1 ? '' : 's' }} · newest first
        <template v-if="failedCount"> · <button class="font-medium underline underline-offset-4 text-warn-text" @click="stateFilter = 'failed'">{{ failedCount }} failed apply</button></template>
      </p>
      <ToggleGroup v-model="view" type="single" variant="outline" size="sm" class="bg-background">
        <ToggleGroupItem value="grouped"><Group class="size-icon" /> By bottle</ToggleGroupItem>
        <ToggleGroupItem value="flat"><Rows3 class="size-icon" /> All</ToggleGroupItem>
      </ToggleGroup>
    </div>

    <EmptyState v-if="!filtered.length" :message="state.open.length ? 'Nothing matches these filters.' : 'No open requests. New ones appear here live.'" />

    <!-- Desktop / tablet -->
    <div v-else-if="!phone" class="panel">
      <Table>
        <TableHeader>
          <TableRow class="hover:bg-transparent">
            <TableHead >Request</TableHead>
            <TableHead class="w-col-time">Filed</TableHead>
            <TableHead class="w-col-actions text-right">Decision</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <template v-for="s in sections" :key="s.container ?? 'all'">
            <TableRow v-if="s.container" class="bg-muted/50 hover:bg-muted/50">
              <TableCell colspan="3" class="py-group-y">
                <div class="toolbar">
                  <Box class="size-icon" />
                  <span class="font-semibold">{{ s.container }}</span>
                  <span class="pill pill-neutral rounded-full">{{ s.rows.length }} open</span>
                  <div class="flex-1" />
                  <template v-if="bulk[s.container]">
                    <Progress :model-value="100 * bulk[s.container].done / bulk[s.container].total" class="w-col-time" />
                    <span class="row-meta">{{ bulk[s.container].done }}/{{ bulk[s.container].total }}</span>
                  </template>
                  <template v-else>
                    <Button variant="ghost" size="sm" class="text-allow-text hover:text-allow-hover" @click="askBulk(s.container, s.rows, 'allow_live')"><Check class="size-icon" /> Allow all</Button>
                    <Button variant="ghost" size="sm" class="text-destructive hover:text-destructive" @click="askBulk(s.container, s.rows, 'deny')"><X class="size-icon" /> Deny all</Button>
                  </template>
                </div>
              </TableCell>
            </TableRow>
            <TableRow v-for="r in s.rows" :key="r.request_id" :class="failed.has(r.request_id) && 'bg-deny-subtle'">
              <TableCell :class="s.container && 'pl-indent'">
                <RequestSummary :row="r" :show-bottle="!s.container" :decide-failed="failed.has(r.request_id)" />
              </TableCell>
              <TableCell>
                <div class="stack-line">
                  <p class="text-body font-medium">{{ relTime(r.opened_at) }}</p>
                  <p class="row-caption">{{ clock(r.opened_at) }}</p>
                </div>
              </TableCell>
              <TableCell>
                <DecideButtons :row="r" :busy="busy.has(r.request_id)" @decide="(a) => run(r, a)" @permanent-deny="(a) => openDeny(r, a)" />
              </TableCell>
            </TableRow>
          </template>
        </TableBody>
      </Table>
    </div>

    <!-- Phone -->
    <div v-else class="stack-section">
      <section v-for="s in sections" :key="s.container ?? 'all'" class="stack-line">
        <div v-if="s.container" class="inline-row">
          <Box class="size-icon" /><span class="font-semibold">{{ s.container }}</span>
          <span class="pill pill-neutral rounded-full">{{ s.rows.length }}</span>
          <div class="flex-1" />
          <span v-if="bulk[s.container]" class="row-meta">{{ bulk[s.container].done }}/{{ bulk[s.container].total }}</span>
          <template v-else>
            <Button variant="ghost" size="icon" class="text-allow-text hover:text-allow-hover" aria-label="Allow all" @click="askBulk(s.container, s.rows, 'allow_live')"><Check class="size-icon" /></Button>
            <Button variant="ghost" size="icon" class="text-destructive hover:text-destructive" aria-label="Deny all" @click="askBulk(s.container, s.rows, 'deny')"><X class="size-icon" /></Button>
          </template>
        </div>
        <article v-for="r in s.rows" :key="r.request_id" class="item-card" :class="failed.has(r.request_id) && 'border-deny-border bg-deny-subtle'">
          <RequestSummary :row="r" :show-bottle="!s.container" :decide-failed="failed.has(r.request_id)" :when="relTime(r.opened_at)" />
          <DecideButtons :row="r" :busy="busy.has(r.request_id)" stretch @decide="(a) => run(r, a)" @permanent-deny="(a) => openDeny(r, a)" />
        </article>
      </section>
    </div>

    <!-- Denylist hits -->
    <Collapsible v-if="denyHits.length" v-model:open="hitsOpen" class="panel">
      <CollapsibleTrigger class="cell-group toolbar w-full text-left">
        <Ban class="size-icon text-muted-foreground" />
        <span class="font-medium">Denylist hits</span>
        <span class="pill pill-neutral rounded-full">{{ denyHits.length }} hosts · {{ denyHitTotal }} hits</span>
        <span class="row-meta max-sm:hidden">Blocked automatically, no decision needed</span>
        <ChevronDown class="size-icon ml-auto transition-transform" :class="hitsOpen && 'rotate-180'" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div class="divide-y border-t">
          <div v-for="r in denyHits" :key="r.request_id" class="cell-group toolbar">
            <span class="row-title">{{ r.host }}</span>
            <span class="row-meta">{{ r.container }}</span>
            <div class="flex-1" />
            <span class="pill pill-neutral">{{ r.hit_count }}×</span>
            <span class="row-caption w-col-time text-right">{{ relTime(r.decided_at) }}</span>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  </div>

  <!-- Permanent deny -->
  <Dialog v-model:open="dlg.open">
    <DialogContent class="max-w-dialog">
      <DialogHeader>
        <DialogTitle>{{ ACTION_LABEL[dlg.action] }}</DialogTitle>
        <DialogDescription>
          <span class="font-mono text-foreground">{{ dlg.row?.host }}:{{ dlg.row?.port }}</span>
          <template v-if="dlg.action === 'deny_bottle'"> is denied for <strong class="text-foreground">{{ dlg.row?.container }}</strong> from now on. It will not ask again.</template>
          <template v-else> goes on the global denylist. <strong class="text-foreground">No bottle</strong> can reach it, and none will ask.</template>
        </DialogDescription>
      </DialogHeader>
      <div class="stack-section">
        <div v-if="dlg.action === 'deny_global'" class="stack-line">
          <Label for="confirm-host">Type <span class="font-mono">{{ dlg.row?.host }}</span> to confirm</Label>
          <Input id="confirm-host" v-model="dlg.confirm" autocomplete="off" :aria-invalid="!!dlg.confirm && !dlgOk" />
        </div>
        <div class="stack-line">
          <Label for="deny-reason">Reason <span class="font-normal text-muted-foreground">(optional, shown to the agent)</span></Label>
          <Textarea id="deny-reason" v-model="dlg.reason" maxlength="200" rows="2" />
          <p class="row-caption text-right">{{ dlg.reason.length }}/200</p>
        </div>
      </div>
      <DialogFooter>
        <Button variant="ghost" @click="dlg.open = false">Cancel</Button>
        <Button variant="destructive" :disabled="!dlgOk" @click="submitDeny"><X class="size-icon" /> Deny permanently</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- Bulk confirm -->
  <AlertDialog v-model:open="bulkAsk.open">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>{{ bulkAsk.action === 'allow_live' ? 'Allow' : 'Deny' }} all {{ bulkAsk.rows.length }} for {{ bulkAsk.container }}?</AlertDialogTitle>
        <AlertDialogDescription as="div" class="stack-section">
          <ul class="stack-line font-mono text-body text-foreground">
            <li v-for="r in bulkAsk.rows" :key="r.request_id">{{ r.host }}:{{ r.port }}</li>
          </ul>
          <p>{{ bulkAsk.action === 'allow_live' ? 'Allowed until the bottle restarts.' : 'Denied once. Nothing is added to a denylist.' }}</p>
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Cancel</AlertDialogCancel>
        <AlertDialogAction :class="bulkAsk.action === 'allow_live' ? 'bg-allow text-allow-foreground hover:bg-allow-hover' : 'bg-destructive text-on-status hover:bg-destructive/90'" @click="runBulk">
          {{ bulkAsk.action === 'allow_live' ? 'Allow' : 'Deny' }} {{ bulkAsk.rows.length }}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
</template>
