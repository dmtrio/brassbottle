<script setup lang="ts">
import { computed, ref } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import DatePicker from 'primevue/datepicker'
import MultiSelect from 'primevue/multiselect'
import SelectButton from 'primevue/selectbutton'
import InputText from 'primevue/inputtext'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import Tag from 'primevue/tag'
import Paginator from 'primevue/paginator'
import { state, BOTTLES, relTime, type DecidedRow } from '../mock/data'
import { useMedia } from '../mock/ui'

const phone = useMedia('(max-width: 640px)')
const range = ref<Date[] | null>(null)
const bottles = ref<string[]>([])
const status = ref('All')
const search = ref('')
const first = ref(0)
const rows = 25

const filtered = computed(() => state.recent.filter((r) => {
  if (bottles.value.length && !bottles.value.includes(r.container)) return false
  if (status.value === 'Allowed' && r.status !== 'allowed') return false
  if (status.value === 'Denied' && r.status !== 'denied') return false
  if (search.value && !r.host.includes(search.value.trim().toLowerCase())) return false
  if (range.value?.[0]) {
    const t = Date.parse(r.decided_at)
    const from = range.value[0].getTime()
    const to = (range.value[1] ?? range.value[0]).getTime() + 86400000
    if (t < from || t >= to) return false
  }
  return true
}))
const page = computed(() => filtered.value.slice(first.value, first.value + rows))

function decision(r: DecidedRow): { label: string; severity: string } {
  if (r.status === 'allowed') return { label: r.scope === 'manifest' ? 'Allowed · manifest' : 'Allowed · live', severity: 'success' }
  if (r.decided_by === 'denylist') return { label: 'Denylist', severity: 'secondary' }
  return { label: { once: 'Denied', bottle: 'Denied · bottle', global: 'Denied · global' }[r.scope as 'once'] ?? 'Denied', severity: 'danger' }
}
const fmt = (s: string) => new Date(s).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
</script>

<template>
  <div class="toolbar">
    <IconField class="search">
      <InputIcon class="pi pi-search" />
      <InputText v-model="search" placeholder="Search destination" fluid @input="first = 0" />
    </IconField>
    <DatePicker v-model="range" selection-mode="range" :manual-input="false" show-icon icon-display="input"
      placeholder="Any date" show-button-bar class="w-date" @update:model-value="first = 0" />
    <MultiSelect v-model="bottles" :options="BOTTLES" placeholder="All bottles" :max-selected-labels="1"
      selected-items-label="{0} bottles" class="w-bottles" show-clear @change="first = 0" />
    <SelectButton v-model="status" :options="['All', 'Allowed', 'Denied']" :allow-empty="false" @change="first = 0" />
  </div>
  <p class="summary muted">{{ filtered.length }} decisions · newest first · whole store</p>

  <DataTable v-if="!phone" :value="page" size="small" data-key="request_id" class="hist">
    <Column header="Decided" class="c-when">
      <template #body="{ data }"><span v-tooltip.top="relTime(data.decided_at)">{{ fmt(data.decided_at) }}</span></template>
    </Column>
    <Column field="container" header="Bottle" />
    <Column header="Destination">
      <template #body="{ data }"><span class="mono">{{ data.host }}<span class="muted">:{{ data.port }}</span></span></template>
    </Column>
    <Column header="Decision">
      <template #body="{ data }">
        <Tag :value="decision(data).label" :severity="decision(data).severity" />
        <Tag v-if="data.hit_count > 1" :value="`${data.hit_count}×`" severity="secondary" class="ml" />
      </template>
    </Column>
    <Column header="Apply">
      <template #body="{ data }">
        <Tag v-if="data.apply_status === 'failed'" value="failed" severity="danger" icon="pi pi-exclamation-triangle" />
        <span v-else-if="data.apply_status" class="muted">applied</span>
        <span v-else class="muted">·</span>
      </template>
    </Column>
    <Column header="Reason">
      <template #body="{ data }"><span class="muted">{{ data.deny_reason ?? '' }}</span></template>
    </Column>
  </DataTable>

  <div v-else class="plist">
    <article v-for="r in page" :key="r.request_id" class="pitem">
      <div class="row1"><span class="mono dest">{{ r.host }}</span><Tag :value="decision(r).label" :severity="decision(r).severity" /></div>
      <div class="muted small">{{ r.container }} · {{ fmt(r.decided_at) }}<template v-if="r.hit_count > 1"> · {{ r.hit_count }}×</template></div>
    </article>
  </div>

  <Paginator v-model:first="first" :rows="rows" :total-records="filtered.length"
    :template="phone ? 'PrevPageLink CurrentPageReport NextPageLink' : 'FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink CurrentPageReport'"
    current-page-report-template="{first}–{last} of {totalRecords}" />
</template>

<style scoped>
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.search { flex: 1 1 220px; }
.w-bottles { width: 190px; }
.w-date { width: 240px; }
.summary { margin: 12px 2px; font-size: 13px; }
.hist { border: 1px solid var(--app-border); border-radius: 10px; overflow: hidden; }
:deep(.c-when) { width: 150px; white-space: nowrap; }
.ml { margin-left: 6px; }
.plist { display: flex; flex-direction: column; gap: 6px; }
.pitem { background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 10px; padding: 10px 12px; }
.row1 { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.dest { font-weight: 600; word-break: break-all; }
.small { font-size: 12px; margin-top: 2px; }
</style>
