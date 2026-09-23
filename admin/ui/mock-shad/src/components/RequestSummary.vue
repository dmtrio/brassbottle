<script setup lang="ts">
import { AlertTriangle, Box } from 'lucide-vue-next'
import type { OpenRow } from '@/mock/data'

// Two-line hierarchy: line 1 is WHAT (destination), line 2 is WHO and WHY.
defineProps<{ row: OpenRow; showBottle: boolean; decideFailed: boolean; when?: string }>()
</script>

<template>
  <div class="stack-line min-w-0">
    <div class="inline-row">
      <span class="row-title">{{ row.host }}<span class="font-normal text-muted-foreground">:{{ row.port }}</span></span>
      <span v-if="row.host_is_ip" class="pill pill-warn">IP address</span>
      <span v-if="row.hit_count > 1" class="pill pill-neutral">{{ row.hit_count }} hits</span>
      <span v-if="decideFailed" class="pill pill-deny">decide failed</span>
    </div>
    <div class="inline-row row-meta">
      <span v-if="showBottle" class="inline-flex items-center gap-tight font-medium text-foreground"><Box class="size-icon-sm" />{{ row.container }}</span>
      <span v-if="showBottle" aria-hidden="true">·</span>
      <span v-if="row.reason" class="text-foreground">{{ row.reason }}</span>
      <span v-else class="italic">No reason given</span>
      <span aria-hidden="true">·</span>
      <span class="row-caption font-mono">{{ row.comm }} (uid {{ row.uid }})</span>
      <template v-if="when"><span aria-hidden="true">·</span><span class="row-caption">{{ when }}</span></template>
    </div>
    <p v-if="row.last_error" class="row-error">
      <AlertTriangle class="size-icon flex-none" />
      <span>Apply failed after {{ row.attempt }} attempt{{ row.attempt === 1 ? '' : 's' }}:
        <span class="row-caption font-mono text-destructive">{{ row.last_error }}</span></span>
    </p>
  </div>
</template>
