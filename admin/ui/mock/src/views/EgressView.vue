<script setup lang="ts">
import { computed, ref } from 'vue'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import Badge from 'primevue/badge'
import QueuePanel from '../components/QueuePanel.vue'
import HistoryPanel from '../components/HistoryPanel.vue'
import { state } from '../mock/data'

const tab = ref('queue')
const openCount = computed(() => state.open.length)
</script>

<template>
  <Tabs v-model:value="tab">
    <TabList>
      <Tab value="queue">Queue <Badge v-if="openCount" :value="openCount" severity="danger" size="small" /></Tab>
      <Tab value="history">History</Tab>
    </TabList>
    <TabPanels class="panels">
      <TabPanel value="queue"><QueuePanel /></TabPanel>
      <TabPanel value="history"><HistoryPanel /></TabPanel>
    </TabPanels>
  </Tabs>
</template>

<style scoped>
.panels { padding: 16px 0 0; background: transparent; }
</style>
