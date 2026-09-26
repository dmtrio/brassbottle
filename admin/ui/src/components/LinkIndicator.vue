<script setup lang="ts">
import { computed } from 'vue'
import { Clock, LoaderCircle, Pause, Radio, RefreshCw } from '@lucide/vue'
import type { LinkState } from '@/lib/stream'

const props = defineProps<{ state: LinkState }>()

const copy = {
  connecting: {
    label: 'Connecting',
    tone: 'pill-neutral',
    hint: 'Opening the live connection. Reading the queue meanwhile.',
  },
  live: {
    label: 'Live',
    tone: 'pill-allow',
    hint: 'New requests appear as soon as they are filed.',
  },
  reconnecting: {
    label: 'Reconnecting',
    tone: 'pill-warn',
    hint: 'The live connection dropped. Checking the queue every 5 seconds while it reopens.',
  },
  polling: {
    label: 'Polling',
    tone: 'pill-neutral',
    hint: 'Live updates are unavailable. Checking the queue every 5 seconds.',
  },
  paused: {
    label: 'Paused',
    tone: 'pill-neutral',
    hint: 'This tab is in the background, so its live connection is closed. Checking the queue every 30 seconds; live updates resume when you return.',
  },
} as const

const current = computed(() => copy[props.state])
</script>

<template>
  <span
    role="status"
    data-testid="link-state"
    :data-state="state"
    :aria-label="`Queue updates: ${current.label}`"
    :title="current.hint"
    class="pill"
    :class="current.tone"
  >
    <Radio
      v-if="state === 'live'"
      class="size-icon"
      aria-hidden="true"
    />
    <LoaderCircle
      v-else-if="state === 'connecting'"
      class="size-icon"
      aria-hidden="true"
    />
    <RefreshCw
      v-else-if="state === 'reconnecting'"
      class="size-icon"
      aria-hidden="true"
    />
    <Pause
      v-else-if="state === 'paused'"
      class="size-icon"
      aria-hidden="true"
    />
    <Clock
      v-else
      class="size-icon"
      aria-hidden="true"
    />
    {{ current.label }}
  </span>
</template>
