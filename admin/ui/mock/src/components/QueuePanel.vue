<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import SplitButton from 'primevue/splitbutton'
import InputText from 'primevue/inputtext'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Message from 'primevue/message'
import Dialog from 'primevue/dialog'
import Textarea from 'primevue/textarea'
import Accordion from 'primevue/accordion'
import AccordionPanel from 'primevue/accordionpanel'
import AccordionHeader from 'primevue/accordionheader'
import AccordionContent from 'primevue/accordioncontent'
import ProgressBar from 'primevue/progressbar'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import { state, decide, relTime, type OpenRow, type DecideAction } from '../mock/data'
import { useMedia } from '../mock/ui'

const phone = useMedia('(max-width: 640px)')
const toast = useToast()
const confirm = useConfirm()

// Filters
const search = ref('')
const bottles = ref<string[]>([])
const stateFilter = ref('All')
const age = ref('any')
const ages = [
  { label: 'Any age', value: 'any' }, { label: 'Under 5 min', value: '5m' },
  { label: 'Under 1 hour', value: '1h' }, { label: 'Older than 1 hour', value: 'old' },
]
const bottleOptions = computed(() => [...new Set(state.open.map((r) => r.container))].sort())
const filtered = computed(() => state.open.filter((r) => {
  if (search.value && !`${r.host}:${r.port}`.includes(search.value.trim().toLowerCase())) return false
  if (bottles.value.length && !bottles.value.includes(r.container)) return false
  if (stateFilter.value === 'Failed apply' && !r.last_error) return false
  if (age.value === '5m' && r.age_seconds >= 300) return false
  if (age.value === '1h' && r.age_seconds >= 3600) return false
  if (age.value === 'old' && r.age_seconds < 3600) return false
  return true
}).sort((a, b) => a.container.localeCompare(b.container) || a.age_seconds - b.age_seconds))
const groups = computed(() => {
  const m = new Map<string, OpenRow[]>()
  for (const r of filtered.value) m.set(r.container, [...(m.get(r.container) ?? []), r])
  return [...m.entries()]
})
const failedCount = computed(() => state.open.filter((r) => r.last_error).length)
const filtersActive = computed(() => !!search.value || bottles.value.length > 0 || stateFilter.value !== 'All' || age.value !== 'any')
function clearFilters() { search.value = ''; bottles.value = []; stateFilter.value = 'All'; age.value = 'any' }

// Decisions
const busy = reactive(new Set<string>())
const failed = reactive(new Set<string>())
const LABEL: Record<DecideAction, string> = {
  allow_live: 'Allowed (live)', allow_manifest: 'Allowed and saved to manifest', deny: 'Denied once',
  deny_bottle: 'Denied for this bottle', deny_global: 'Denied everywhere',
}
async function run(row: OpenRow, action: DecideAction, reason?: string, quiet = false) {
  busy.add(row.request_id); failed.delete(row.request_id)
  try {
    await decide(row, action, reason)
    if (!quiet) toast.add({ severity: action.startsWith('allow') ? 'success' : 'warn', summary: LABEL[action], detail: `${row.container} → ${row.host}`, life: 2500 })
    return true
  } catch (e) {
    failed.add(row.request_id)
    toast.add({ severity: 'error', summary: 'Decision not applied', detail: `${row.host}: ${(e as Error).message}`, life: 5000 })
    return false
  } finally { busy.delete(row.request_id) }
}

const allowMenu = (row: OpenRow) => [
  { label: 'Allow and save to manifest', icon: 'pi pi-save', command: () => run(row, 'allow_manifest') },
]
const denyMenu = (row: OpenRow) => [
  { label: 'Deny with reason…', icon: 'pi pi-comment', command: () => openDeny(row, 'deny') },
  { label: 'Deny for this bottle…', icon: 'pi pi-box', command: () => openDeny(row, 'deny_bottle') },
  { label: 'Deny everywhere…', icon: 'pi pi-globe', command: () => openDeny(row, 'deny_global') },
]

const denyDlg = reactive({ open: false, row: null as OpenRow | null, action: 'deny' as DecideAction, reason: '', confirmHost: '' })
function openDeny(row: OpenRow, action: DecideAction) { Object.assign(denyDlg, { open: true, row, action, reason: '', confirmHost: '' }) }
const denyOk = computed(() => denyDlg.action !== 'deny_global' || denyDlg.confirmHost.trim() === denyDlg.row?.host)
async function submitDeny() {
  if (!denyDlg.row || !denyOk.value) return
  denyDlg.open = false
  await run(denyDlg.row, denyDlg.action, denyDlg.reason)
}

// Bulk per bottle: one decide per row, sequentially.
const bulk = reactive<Record<string, { done: number; total: number; failed: number }>>({})
function bulkDecide(container: string, rows: OpenRow[], action: 'allow_live' | 'deny') {
  const verb = action === 'allow_live' ? 'Allow' : 'Deny'
  confirm.require({
    header: `${verb} all ${rows.length} for ${container}?`,
    message: rows.map((r) => `${r.host}:${r.port}`).join('\n'),
    icon: action === 'allow_live' ? 'pi pi-check-circle' : 'pi pi-exclamation-triangle',
    acceptProps: { label: `${verb} ${rows.length}`, severity: action === 'allow_live' ? 'success' : 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      const snapshot = [...rows]
      bulk[container] = { done: 0, total: snapshot.length, failed: 0 }
      for (const r of snapshot) {
        const ok = await run(r, action, undefined, true)
        bulk[container].done++
        if (!ok) bulk[container].failed++
      }
      const b = bulk[container]
      toast.add({ severity: b.failed ? 'warn' : 'success', summary: `${verb}: ${b.total - b.failed} of ${b.total} done`,
        detail: b.failed ? `${b.failed} failed and stay open, marked in the list.` : container, life: 4000 })
      delete bulk[container]
    },
  })
}

// Denylist hits (already decided; shown collapsed)
const denyHits = computed(() => state.recent.filter((r) => r.status === 'denied' && r.decided_by === 'denylist'))
const denyHitTotal = computed(() => denyHits.value.reduce((n, r) => n + r.hit_count, 0))
</script>

<template>
  <div class="toolbar">
    <IconField class="search">
      <InputIcon class="pi pi-search" />
      <InputText v-model="search" placeholder="Search destination" fluid />
    </IconField>
    <MultiSelect v-model="bottles" :options="bottleOptions" placeholder="All bottles" :max-selected-labels="1"
      selected-items-label="{0} bottles" class="w-bottles" show-clear />
    <SelectButton v-model="stateFilter" :options="['All', 'Failed apply']" :allow-empty="false" />
    <Select v-model="age" :options="ages" option-label="label" option-value="value" class="w-age" />
    <Button v-if="filtersActive" label="Clear" icon="pi pi-filter-slash" text size="small" @click="clearFilters" />
  </div>

  <p class="summary muted">
    {{ filtered.length }} of {{ state.open.length }} open · {{ groups.length }} bottle{{ groups.length === 1 ? '' : 's' }}
    <template v-if="failedCount"> · <a class="link-warn" @click="stateFilter = 'Failed apply'">{{ failedCount }} failed apply</a></template>
  </p>

  <div v-if="!filtered.length" class="empty">
    <i class="pi pi-check-circle" />
    <p v-if="state.open.length">Nothing matches these filters.</p>
    <p v-else>No open requests. New ones appear here live.</p>
  </div>

  <!-- Desktop / tablet: one table, grouped by bottle -->
  <DataTable v-else-if="!phone" :value="filtered" row-group-mode="subheader" group-rows-by="container"
    data-key="request_id" size="small" class="queue" :row-class="(r: OpenRow) => ({ 'row-failed': failed.has(r.request_id) })">
    <template #groupheader="{ data }">
      <div class="ghead">
        <i class="pi pi-box" /><strong>{{ data.container }}</strong>
        <Tag :value="`${groups.find(g => g[0] === data.container)?.[1].length} open`" severity="secondary" rounded />
        <div class="grow" />
        <template v-if="bulk[data.container]">
          <ProgressBar :value="Math.round(100 * bulk[data.container].done / bulk[data.container].total)" class="bulkbar" />
          <span class="muted">{{ bulk[data.container].done }}/{{ bulk[data.container].total }}</span>
        </template>
        <template v-else>
          <Button label="Allow all" icon="pi pi-check" size="small" severity="success" text
            @click="bulkDecide(data.container, groups.find(g => g[0] === data.container)![1], 'allow_live')" />
          <Button label="Deny all" icon="pi pi-times" size="small" severity="danger" text
            @click="bulkDecide(data.container, groups.find(g => g[0] === data.container)![1], 'deny')" />
        </template>
      </div>
    </template>
    <Column header="Destination" class="c-dest">
      <template #body="{ data }">
        <div class="dest mono">{{ data.host }}<span class="muted">:{{ data.port }}</span></div>
        <div class="chips">
          <Tag v-if="data.host_is_ip" value="IP" severity="warn" />
          <Tag v-if="data.hit_count > 1" :value="`${data.hit_count} hits`" severity="secondary" />
          <Tag v-if="failed.has(data.request_id)" value="decide failed" severity="danger" icon="pi pi-exclamation-circle" />
        </div>
        <Message v-if="data.last_error" severity="error" size="small" variant="simple" class="err">
          <i class="pi pi-exclamation-triangle" /> Apply failed ({{ data.attempt }} attempt{{ data.attempt === 1 ? '' : 's' }}):
          <span class="mono">{{ data.last_error }}</span>
        </Message>
      </template>
    </Column>
    <Column header="Reason">
      <template #body="{ data }">
        <span v-if="data.reason">{{ data.reason }}</span><span v-else class="muted">none given</span>
        <div class="muted small mono">{{ data.comm }} · uid {{ data.uid }}</div>
      </template>
    </Column>
    <Column header="Filed" class="c-age">
      <template #body="{ data }"><span v-tooltip.top="new Date(data.opened_at).toLocaleString()">{{ relTime(data.opened_at) }}</span></template>
    </Column>
    <Column class="c-act">
      <template #body="{ data }">
        <div class="acts">
          <SplitButton :label="data.last_error ? 'Retry allow' : 'Allow'" icon="pi pi-check" size="small" severity="success"
            :model="allowMenu(data)" :loading="busy.has(data.request_id)" :disabled="busy.has(data.request_id)" @click="run(data, 'allow_live')" />
          <SplitButton label="Deny" icon="pi pi-times" size="small" severity="danger" outlined
            :model="denyMenu(data)" :disabled="busy.has(data.request_id)" @click="run(data, 'deny')" />
        </div>
      </template>
    </Column>
  </DataTable>

  <!-- Phone: cards per bottle -->
  <div v-else class="cards">
    <section v-for="[container, rows] in groups" :key="container" class="cgroup">
      <div class="ghead">
        <i class="pi pi-box" /><strong>{{ container }}</strong>
        <Tag :value="`${rows.length}`" severity="secondary" rounded />
        <div class="grow" />
        <span v-if="bulk[container]" class="muted">{{ bulk[container].done }}/{{ bulk[container].total }}</span>
        <template v-else>
          <Button icon="pi pi-check" size="small" severity="success" text aria-label="Allow all" @click="bulkDecide(container, rows, 'allow_live')" />
          <Button icon="pi pi-times" size="small" severity="danger" text aria-label="Deny all" @click="bulkDecide(container, rows, 'deny')" />
        </template>
      </div>
      <article v-for="r in rows" :key="r.request_id" class="card" :class="{ 'row-failed': failed.has(r.request_id) }">
        <div class="dest mono">{{ r.host }}<span class="muted">:{{ r.port }}</span></div>
        <div class="chips">
          <Tag v-if="r.host_is_ip" value="IP" severity="warn" />
          <Tag v-if="r.hit_count > 1" :value="`${r.hit_count} hits`" severity="secondary" />
          <span class="muted small">{{ relTime(r.opened_at) }} · {{ r.comm }}</span>
        </div>
        <div v-if="r.reason" class="small">{{ r.reason }}</div>
        <Message v-if="r.last_error" severity="error" size="small" variant="simple" class="err">
          Apply failed ×{{ r.attempt }}: <span class="mono">{{ r.last_error }}</span>
        </Message>
        <div class="acts">
          <SplitButton label="Allow" icon="pi pi-check" size="small" severity="success" :model="allowMenu(r)"
            :loading="busy.has(r.request_id)" @click="run(r, 'allow_live')" />
          <SplitButton label="Deny" icon="pi pi-times" size="small" severity="danger" outlined :model="denyMenu(r)" @click="run(r, 'deny')" />
        </div>
      </article>
    </section>
  </div>

  <Accordion v-if="denyHits.length" class="dhits">
    <AccordionPanel value="hits">
      <AccordionHeader>
        <span class="dh-head"><i class="pi pi-ban" /> Denylist hits
          <Tag :value="`${denyHits.length} hosts · ${denyHitTotal} hits`" severity="secondary" rounded /></span>
      </AccordionHeader>
      <AccordionContent>
        <p class="muted small">Blocked automatically by the denylist. They need no decision. Repeats are counted, not listed.</p>
        <div v-for="r in denyHits" :key="r.request_id" class="dhrow">
          <span class="mono">{{ r.host }}</span>
          <span class="muted">{{ r.container }}</span>
          <Tag :value="`${r.hit_count}×`" severity="secondary" />
          <span class="muted small">{{ relTime(r.decided_at) }}</span>
        </div>
      </AccordionContent>
    </AccordionPanel>
  </Accordion>

  <Dialog v-model:visible="denyDlg.open" modal :header="LABEL[denyDlg.action].replace('Denied', 'Deny')" :style="{ width: 'min(460px, 94vw)' }">
    <p class="mono">{{ denyDlg.row?.container }} → {{ denyDlg.row?.host }}:{{ denyDlg.row?.port }}</p>
    <p v-if="denyDlg.action === 'deny_bottle'" class="muted small">Future requests from this bottle to this host are denied without asking.</p>
    <template v-if="denyDlg.action === 'deny_global'">
      <Message severity="warn" size="small">Adds this host to the global denylist. Every bottle will be refused without asking.</Message>
      <label class="fld">Type <span class="mono">{{ denyDlg.row?.host }}</span> to confirm
        <InputText v-model="denyDlg.confirmHost" fluid :invalid="!!denyDlg.confirmHost && !denyOk" />
      </label>
    </template>
    <label class="fld">Reason <span class="muted">(optional, shown to the agent)</span>
      <Textarea v-model="denyDlg.reason" rows="2" maxlength="200" auto-resize fluid />
      <span class="muted small right">{{ denyDlg.reason.length }}/200</span>
    </label>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="denyDlg.open = false" />
      <Button label="Deny" icon="pi pi-times" severity="danger" :disabled="!denyOk" @click="submitDeny" />
    </template>
  </Dialog>
</template>

<style scoped>
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.search { flex: 1 1 220px; }
.w-bottles { width: 190px; }
.w-age { width: 170px; }
.summary { margin: 12px 2px; font-size: 13px; }
.link-warn { color: var(--p-orange-500); cursor: pointer; text-decoration: underline; }
.empty { text-align: center; padding: 48px 0; color: var(--app-muted); }
.empty .pi { font-size: 28px; color: var(--p-green-500); }
.queue { border: 1px solid var(--app-border); border-radius: 10px; overflow: hidden; }
.ghead { display: flex; align-items: center; gap: 8px; width: 100%; }
.ghead .pi-box { color: var(--p-primary-color); }
.grow { flex: 1; }
.bulkbar { width: 120px; height: 6px; }
.dest { font-weight: 600; word-break: break-all; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 4px; }
.chips:empty { display: none; }
.err { margin-top: 6px; }
.err .mono { font-size: 12px; word-break: break-word; }
.small { font-size: 12px; }
.acts { display: flex; gap: 6px; justify-content: flex-end; flex-wrap: nowrap; }
:deep(.c-age) { width: 90px; white-space: nowrap; }
:deep(.c-act) { width: 270px; }
:deep(.c-dest) { width: 40%; }
:deep(.row-failed) { background: color-mix(in srgb, var(--p-red-500) 8%, transparent) !important; }
.cards { display: flex; flex-direction: column; gap: 14px; }
.cgroup .ghead { padding: 4px 2px 6px; }
.card { background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 10px; padding: 12px; margin-bottom: 8px;
  display: flex; flex-direction: column; gap: 6px; }
.card .acts { justify-content: stretch; }
.card .acts > * { flex: 1; }
.dhits { margin-top: 20px; border: 1px solid var(--app-border); border-radius: 10px; overflow: hidden; }
.dh-head { display: flex; gap: 8px; align-items: center; }
.dhrow { display: grid; grid-template-columns: 1fr auto auto auto; gap: 12px; align-items: center; padding: 6px 0; border-top: 1px solid var(--app-border); }
.fld { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
.right { text-align: right; }
</style>
