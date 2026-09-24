<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useColorMode, useMediaQuery } from '@vueuse/core'
import { Ban, Box, Database, Globe, Moon, ShieldCheck, Sun } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { useQueue } from '@/composables/useQueue'

const route = useRoute()
const phone = useMediaQuery('(max-width: 639px)')
const mode = useColorMode()
const { openCount } = useQueue()

const nav = [
  { to: '/egress', label: 'Egress', icon: Globe, showCaption: false },
  { to: '/denylist', label: 'Denylist', icon: Ban, showCaption: true },
  { to: '/bottles', label: 'Bottles', icon: Box, showCaption: true },
  { to: '/backup', label: 'Backup', icon: Database, showCaption: true },
]
const activeLabel = computed(() => {
  const match = nav.find((n) => n.to === route.path)
  return match?.label ?? 'Djinn admin'
})

// "(N) Egress · Djinn admin" on the egress route; the count rides on every
// route's title so a background tab still shows it.
watch(
  [openCount, activeLabel],
  ([count, label]) => {
    window.document.title = `${count > 0 ? `(${count}) ` : ''}${label} · Djinn admin`
  },
  { immediate: true },
)
</script>

<template>
  <div class="flex min-h-screen text-foreground">
    <aside
      v-if="!phone"
      class="sidebar"
    >
      <div class="brand">
        <ShieldCheck class="size-icon-lg" /> Djinn admin
      </div>
      <nav class="flex flex-col gap-tight">
        <RouterLink
          v-for="n in nav"
          :key="n.to"
          :to="n.to"
          class="nav-item"
          active-class="nav-item-active"
        >
          <component
            :is="n.icon"
            class="size-icon"
          /> {{ n.label }}
          <span
            v-if="n.to === '/egress' && openCount > 0"
            class="count-badge"
          >{{ openCount }}</span>
          <span
            v-if="n.showCaption"
            class="nav-caption"
          >later</span>
        </RouterLink>
      </nav>
    </aside>

    <div class="flex min-w-0 flex-1 flex-col">
      <header class="topbar page-bar">
        <h1 class="text-title font-semibold">
          {{ activeLabel }}
        </h1>
        <div class="flex-1" />
        <Button
          variant="ghost"
          size="icon"
          title="Theme"
          @click="mode = mode === 'dark' ? 'light' : 'dark'"
        >
          <Sun
            v-if="mode === 'dark'"
            class="size-icon"
          />
          <Moon
            v-else
            class="size-icon"
          />
        </Button>
      </header>
      <main class="page flex-1">
        <RouterView />
      </main>
    </div>

    <nav
      v-if="phone"
      class="tabbar"
    >
      <RouterLink
        v-for="n in nav"
        :key="n.to"
        :to="n.to"
        class="tabbar-item"
        active-class="tabbar-item-active"
      >
        <component
          :is="n.icon"
          class="size-icon-lg"
        />{{ n.label }}
        <span
          v-if="n.to === '/egress' && openCount > 0"
          class="tabbar-badge count-badge"
        >{{ openCount }}</span>
      </RouterLink>
    </nav>
  </div>
</template>
