<script setup lang="ts">
import { computed, ref } from 'vue'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import QueuePanel from '@/components/QueuePanel.vue'
import HistoryPanel from '@/components/HistoryPanel.vue'
import { state } from '@/mock/data'

const tab = ref('queue')
const openCount = computed(() => state.open.length)
</script>

<template>
  <Tabs v-model="tab" class="gap-section">
    <TabsList>
      <TabsTrigger value="queue" class="gap-inline">Queue <span v-if="openCount" class="count-badge">{{ openCount }}</span></TabsTrigger>
      <TabsTrigger value="history">History</TabsTrigger>
    </TabsList>
    <TabsContent value="queue"><QueuePanel /></TabsContent>
    <TabsContent value="history"><HistoryPanel /></TabsContent>
  </Tabs>
</template>
