<script setup lang="ts">
import { AlertTriangle, Box } from '@lucide/vue'
import type { OpenRow } from '@/types'

export type RowNote = { tone: 'neutral' | 'deny'; text: string }

defineProps<{
  row: OpenRow
  showBottle: boolean
  note?: RowNote | null
  when?: string
}>()

function lastErrorText(reason: string): string {
  if (reason === 'apply_failed') return 'rule install failed'
  if (reason === 'ip_requires_cidr') return 'an IP address needs a CIDR in the manifest'
  return reason
}
</script>

<template>
  <div class="min-w-0">
    <div class="stack-line">
      <div class="inline-row">
        <span
          class="row-title"
          data-testid="request-host"
        >{{ row.host }}<span class="font-normal text-muted-foreground">:{{ row.port }}</span></span>
        <span
          v-if="row.host_is_ip"
          class="pill pill-warn"
        >IP address</span>
        <span
          v-if="row.hit_count > 1"
          class="pill pill-neutral"
        >{{ row.hit_count }} hits</span>
      </div>
      <div class="overflow-hidden">
        <div class="meta-flow row-meta">
          <span
            v-if="showBottle"
            class="meta-item inline-flex items-center gap-tight font-medium text-foreground"
          ><Box class="size-icon-sm" />{{ row.container }}</span>
          <span
            v-if="row.reason"
            class="meta-item text-foreground"
          >{{ row.reason }}</span>
          <span
            v-else
            class="meta-item italic"
          >No reason given</span>
          <span
            v-if="row.comm"
            class="meta-item row-caption font-mono"
          >{{ row.comm }}<template v-if="row.uid !== null"> (uid {{ row.uid }})</template></span>
          <span
            v-if="when"
            class="meta-item row-caption"
          >{{ when }}</span>
        </div>
      </div>
      <p
        v-if="row.last_error"
        class="row-error"
      >
        <AlertTriangle class="size-icon flex-none" />
        <span>Apply failed after {{ row.last_error.attempt }} attempt{{ row.last_error.attempt === 1 ? '' : 's' }}:
          {{ lastErrorText(row.last_error.reason) }}</span>
      </p>
    </div>
    <!-- Always rendered, so a screen reader is already watching it when the
         outcome text arrives; only its content changes. -->
    <p
      class="inline-row"
      role="status"
    >
      <span
        v-if="note"
        class="pill mt-line"
        :class="note.tone === 'deny' ? 'pill-deny' : 'pill-neutral'"
      >{{ note.text }}</span>
    </p>
  </div>
</template>
