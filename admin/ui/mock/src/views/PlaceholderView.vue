<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Card from 'primevue/card'

const route = useRoute()
const blurbs: Record<string, { icon: string; text: string[] }> = {
  Denylist: { icon: 'pi pi-ban', text: ['Global and per-bottle deny patterns, with the hit count for each.', 'Add, edit and remove entries. Move a denied request here in one click.'] },
  Bottles: { icon: 'pi pi-box', text: ['One card per bottle: running state, manifest egress zones, live allows.', 'Drill into a bottle to see its request history.'] },
  Backup: { icon: 'pi pi-database', text: ['Export and restore the egress store and admin state.', 'Last backup time and size.'] },
}
const b = computed(() => blurbs[route.meta.title as string])
</script>

<template>
  <Card class="ph">
    <template #title><i :class="b.icon" /> {{ route.meta.title }}: not built yet</template>
    <template #content>
      <p class="muted">This panel is a placeholder in this PLN. It is planned to hold:</p>
      <ul><li v-for="t in b.text" :key="t">{{ t }}</li></ul>
    </template>
  </Card>
</template>

<style scoped>
.ph { max-width: 640px; }
.ph .pi { margin-right: 6px; color: var(--p-primary-color); }
</style>
