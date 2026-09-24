<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const route = useRoute()
const title = computed(() => route.meta.title as string)
const blurbs: Record<string, string[]> = {
  Denylist: ['Global and per-bottle deny rules, with the hit count for each.', 'Add, edit and remove entries.'],
  Bottles: ['One card per bottle: running state, manifest egress zones, live allows.', 'Drill into a bottle to see its request history.'],
  Backup: ['Export and restore the egress store and admin state.', 'Last backup time and size.'],
}
const items = computed(() => blurbs[title.value] ?? [])
const isEgress = computed(() => title.value === 'Egress')
</script>

<template>
  <Card class="max-w-xl">
    <CardHeader>
      <CardTitle>{{ isEgress ? 'Egress queue' : title }}</CardTitle>
      <CardDescription>
        {{ isEgress ? 'The request queue is being rebuilt here. Until then, use the current admin page.' : 'Not built yet. Planned:' }}
      </CardDescription>
    </CardHeader>
    <CardContent v-if="!isEgress">
      <ul class="stack-line list-disc list-inside text-body">
        <li
          v-for="t in items"
          :key="t"
        >
          {{ t }}
        </li>
      </ul>
    </CardContent>
  </Card>
</template>
