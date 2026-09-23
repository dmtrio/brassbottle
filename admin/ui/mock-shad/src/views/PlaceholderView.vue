<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const route = useRoute()
const blurbs: Record<string, string[]> = {
  Denylist: ['Global and per-bottle deny rules, with the hit count for each.', 'Add, edit and remove entries.'],
  Bottles: ['One card per bottle: running state, manifest egress zones, live allows.', 'Drill into a bottle to see its request history.'],
  Backup: ['Export and restore the egress store and admin state.', 'Last backup time and size.'],
}
const items = computed(() => blurbs[route.meta.title as string] ?? [])
</script>

<template>
  <Card class="max-w-xl">
    <CardHeader>
      <CardTitle>{{ route.meta.title }}: not built yet</CardTitle>
      <CardDescription>This panel is a placeholder in this PLN. It is planned to hold:</CardDescription>
    </CardHeader>
    <CardContent>
      <ul class="stack-line list-disc list-inside text-body"><li v-for="t in items" :key="t">{{ t }}</li></ul>
    </CardContent>
  </Card>
</template>
