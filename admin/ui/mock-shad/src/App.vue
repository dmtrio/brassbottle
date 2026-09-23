<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useColorMode } from '@vueuse/core'
import { toast } from 'vue-sonner'
import { Globe, Ban, Box, Database, Bell, BellOff, Sun, Moon, SlidersHorizontal, ShieldCheck, Plus } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Toaster } from '@/components/ui/sonner'
import { state, simulateFiling, failNext } from './mock/data'
import { notifyEnabled, live, usePhone } from './mock/ui'

const route = useRoute()
const phone = usePhone()
const mode = useColorMode()
const nav = [
  { to: '/egress', label: 'Egress', icon: Globe },
  { to: '/denylist', label: 'Denylist', icon: Ban },
  { to: '/bottles', label: 'Bottles', icon: Box },
  { to: '/backup', label: 'Backup', icon: Database },
]
const openCount = computed(() => state.open.length)
const liveMeta = computed(() => ({
  live: { label: 'Live', pill: 'pill-allow' },
  reconnecting: { label: 'Reconnecting', pill: 'pill-warn' },
  polling: { label: 'Polling', pill: 'pill-neutral' },
}[live.value]))

function toggleNotify() {
  notifyEnabled.value = !notifyEnabled.value
  toast(notifyEnabled.value ? 'Notifications on' : 'Notifications off', {
    description: notifyEnabled.value ? 'You get an OS notification for each new request while this app is open.' : undefined,
  })
}
function fileOne() {
  const row = simulateFiling()
  if (notifyEnabled.value) toast(`🔔 ${row.container}`, { description: `wants ${row.host}:${row.port} (stand-in for the OS notification)` })
}
</script>

<template>
  <div class="flex min-h-screen text-foreground">
    <aside v-if="!phone" class="sidebar">
      <div class="brand"><ShieldCheck class="size-icon-lg" /> Djinn admin</div>
      <nav class="flex flex-col gap-tight">
        <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="nav-item" active-class="nav-item-active">
          <component :is="n.icon" class="size-icon" /> {{ n.label }}
          <span v-if="n.to === '/egress' && openCount" class="count-badge ml-auto">{{ openCount }}</span>
          <span v-else-if="n.to !== '/egress'" class="nav-caption">later</span>
        </RouterLink>
      </nav>
      <p class="frame-foot">127.0.0.1:8817 · session ok</p>
    </aside>

    <div class="flex min-w-0 flex-1 flex-col">
      <header class="topbar page-bar">
        <h1 class="text-title font-semibold">{{ route.meta.title }}</h1>
        <span class="pill rounded-full" :class="liveMeta.pill"><span class="live-dot" />{{ liveMeta.label }}</span>
        <div class="flex-1" />
        <Button variant="ghost" size="icon" :title="notifyEnabled ? 'Notifications on' : 'Enable notifications'" @click="toggleNotify">
          <Bell v-if="notifyEnabled" class="size-icon" /><BellOff v-else class="size-icon" />
        </Button>
        <Button variant="ghost" size="icon" title="Theme" @click="mode = mode === 'dark' ? 'light' : 'dark'">
          <Sun v-if="mode === 'dark'" class="size-icon" /><Moon v-else class="size-icon" />
        </Button>
        <Popover>
          <PopoverTrigger as-child>
            <Button variant="outline" size="sm"><SlidersHorizontal class="size-icon" /> Mock</Button>
          </PopoverTrigger>
          <PopoverContent align="end" class="w-popover stack-section">
            <div>
              <p class="text-body font-medium">Mockup controls</p>
              <p class="row-caption">Not part of the design. They drive the fake data.</p>
            </div>
            <Button size="sm" class="w-full" @click="fileOne"><Plus class="size-icon" /> Simulate new filing</Button>
            <div class="inline-row"><Switch id="fail" v-model="failNext" /><Label for="fail">Fail the next decide</Label></div>
            <div class="stack-line">
              <Label>Connection</Label>
              <ToggleGroup v-model="live" type="single" variant="outline" size="sm">
                <ToggleGroupItem value="live">Live</ToggleGroupItem>
                <ToggleGroupItem value="reconnecting">Reconnecting</ToggleGroupItem>
                <ToggleGroupItem value="polling">Polling</ToggleGroupItem>
              </ToggleGroup>
            </div>
          </PopoverContent>
        </Popover>
      </header>
      <main class="page flex-1"><RouterView /></main>
    </div>

    <nav v-if="phone" class="tabbar">
      <RouterLink v-for="n in nav" :key="n.to" :to="n.to" class="tabbar-item" active-class="tabbar-item-active">
        <component :is="n.icon" class="size-icon-lg" />{{ n.label }}
        <span v-if="n.to === '/egress' && openCount" class="count-badge tabbar-badge">{{ openCount }}</span>
      </RouterLink>
    </nav>
    <Toaster rich-colors :position="phone ? 'top-center' : 'bottom-right'" />
  </div>
</template>
