<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import Badge from 'primevue/badge'
import Tag from 'primevue/tag'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'
import Popover from 'primevue/popover'
import ToggleSwitch from 'primevue/toggleswitch'
import SelectButton from 'primevue/selectbutton'
import { useToast } from 'primevue/usetoast'
import { state, simulateFiling, failNext } from './mock/data'
import { useMedia, notifyEnabled, live } from './mock/ui'

const route = useRoute()
const phone = useMedia('(max-width: 640px)')
const toast = useToast()

const nav = [
  { to: '/egress', label: 'Egress', icon: 'pi pi-globe' },
  { to: '/denylist', label: 'Denylist', icon: 'pi pi-ban' },
  { to: '/bottles', label: 'Bottles', icon: 'pi pi-box' },
  { to: '/backup', label: 'Backup', icon: 'pi pi-database' },
]
const openCount = computed(() => state.open.length)

const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
const dark = ref(prefersDark)
const applyTheme = () => document.documentElement.classList.toggle('app-dark', dark.value)
applyTheme()
function toggleTheme() { dark.value = !dark.value; applyTheme() }

function toggleNotify() {
  notifyEnabled.value = !notifyEnabled.value
  toast.add({ severity: 'info', summary: notifyEnabled.value ? 'Notifications on' : 'Notifications off',
    detail: notifyEnabled.value ? 'You will get an OS notification for each new request while this app is open.' : undefined, life: 3000 })
}

const liveMeta = computed(() => ({
  live: { label: 'Live', severity: 'success' },
  reconnecting: { label: 'Reconnecting', severity: 'warn' },
  polling: { label: 'Polling', severity: 'secondary' },
}[live.value] as { label: string; severity: string }))

const mockPanel = ref()
function fileOne() {
  const row = simulateFiling()
  if (notifyEnabled.value) {
    toast.add({ severity: 'contrast', summary: `🔔 ${row.container}`, detail: `wants ${row.host}:${row.port}  (stand-in for the OS notification)`, life: 5000 })
  }
}
</script>

<template>
  <div class="shell" :class="{ phone }">
    <aside v-if="!phone" class="sidebar">
      <div class="brand"><i class="pi pi-shield" /> <span>Djinn admin</span></div>
      <nav>
        <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="nav-item" active-class="active">
          <i :class="n.icon" /> <span>{{ n.label }}</span>
          <Badge v-if="n.to === '/egress' && openCount" :value="openCount" severity="danger" class="ml-auto" />
          <Tag v-else-if="n.to !== '/egress'" value="later" severity="secondary" class="ml-auto soon" />
        </RouterLink>
      </nav>
      <div class="sidebar-foot muted">127.0.0.1:8817 · session ok</div>
    </aside>

    <div class="main">
      <header class="topbar">
        <h1>{{ route.meta.title }}</h1>
        <Tag :severity="liveMeta.severity" rounded class="live"><span class="dot" />{{ liveMeta.label }}</Tag>
        <div class="grow" />
        <Button :icon="notifyEnabled ? 'pi pi-bell' : 'pi pi-bell-slash'" text rounded
          v-tooltip.bottom="notifyEnabled ? 'Notifications on' : 'Enable notifications'" @click="toggleNotify" />
        <Button :icon="dark ? 'pi pi-sun' : 'pi pi-moon'" text rounded v-tooltip.bottom="'Theme'" @click="toggleTheme" />
        <Button icon="pi pi-sliders-h" label="Mock" size="small" severity="secondary" outlined @click="mockPanel.toggle($event)" />
      </header>
      <main class="content"><RouterView /></main>
    </div>

    <nav v-if="phone" class="bottomnav">
      <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="bn-item" active-class="active">
        <i :class="n.icon" /><span>{{ n.label }}</span>
        <Badge v-if="n.to === '/egress' && openCount" :value="openCount" severity="danger" class="bn-badge" />
      </RouterLink>
    </nav>

    <Popover ref="mockPanel">
      <div class="mockpanel">
        <strong>Mockup controls</strong>
        <p class="muted">Not part of the design. They drive the fake data.</p>
        <Button label="Simulate new filing" icon="pi pi-plus" size="small" @click="fileOne" />
        <label class="row"><ToggleSwitch v-model="failNext" /> Fail the next decide</label>
        <label class="col">Connection
          <SelectButton v-model="live" :options="['live', 'reconnecting', 'polling']" size="small" :allow-empty="false" />
        </label>
      </div>
    </Popover>
    <Toast position="bottom-right" />
    <ConfirmDialog />
  </div>
</template>

<style scoped>
.shell { display: flex; min-height: 100vh; }
.sidebar { width: 232px; flex: none; background: var(--app-surface); border-right: 1px solid var(--app-border);
  display: flex; flex-direction: column; padding: 16px 12px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; gap: 10px; align-items: center; font-weight: 700; font-size: 16px; padding: 4px 10px 18px; }
.brand .pi { color: var(--p-primary-color); font-size: 18px; }
.sidebar nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; color: inherit; text-decoration: none; }
.nav-item:hover { background: var(--p-content-hover-background); }
.nav-item.active { background: var(--p-highlight-background); color: var(--p-highlight-color); font-weight: 600; }
.ml-auto { margin-left: auto; }
.soon { font-size: 10px; padding: 1px 6px; }
.sidebar-foot { margin-top: auto; font-size: 12px; padding: 0 10px; }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar { display: flex; align-items: center; gap: 8px; padding: 12px 24px; background: var(--app-surface);
  border-bottom: 1px solid var(--app-border); position: sticky; top: 0; z-index: 5; }
.topbar h1 { font-size: 18px; margin: 0 8px 0 0; }
.live .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; margin-right: 6px; }
.grow { flex: 1; }
.content { padding: 20px 24px 40px; }
.phone .topbar { padding: 10px 12px; }
.phone .content { padding: 12px 12px 88px; }
.bottomnav { position: fixed; bottom: 0; left: 0; right: 0; display: flex; background: var(--app-surface);
  border-top: 1px solid var(--app-border); z-index: 6; padding-bottom: env(safe-area-inset-bottom); }
.bn-item { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 0 10px;
  color: var(--app-muted); text-decoration: none; font-size: 11px; position: relative; }
.bn-item .pi { font-size: 18px; }
.bn-item.active { color: var(--p-primary-color); font-weight: 600; }
.bn-badge { position: absolute; top: 3px; left: 55%; }
.mockpanel { display: flex; flex-direction: column; gap: 10px; width: 250px; }
.mockpanel p { margin: 0; font-size: 12px; }
.row { display: flex; gap: 8px; align-items: center; }
.col { display: flex; flex-direction: column; gap: 6px; }
</style>
