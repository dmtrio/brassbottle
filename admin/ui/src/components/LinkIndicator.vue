<script setup lang="ts">
import { computed } from 'vue'
import { Clock, Radio, RefreshCw } from '@lucide/vue'
import type { LinkState } from '@/lib/stream'

const props = defineProps<{ state: LinkState }>()

const copy = {
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
    <RefreshCw
      v-else-if="state === 'reconnecting'"
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
