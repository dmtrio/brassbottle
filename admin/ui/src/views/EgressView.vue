<script setup lang="ts">
import { ref } from 'vue'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import HistoryPanel from '@/components/HistoryPanel.vue'
import QueuePanel from '@/components/QueuePanel.vue'
import { useQueue } from '@/composables/useQueue'

const tab = ref('queue')
const { openCount } = useQueue()
</script>

<template>
  <Tabs
    v-model="tab"
    class="gap-section"
  >
    <TabsList>
      <TabsTrigger
        value="queue"
        class="gap-inline"
        data-testid="tab-queue"
      >
        Queue <span
          v-if="openCount"
          class="count-badge"
        >{{ openCount }}</span>
      </TabsTrigger>
      <TabsTrigger
        value="history"
        data-testid="tab-history"
      >
        History
      </TabsTrigger>
    </TabsList>
    <!-- The queue stays mounted while History is open, so a decision in flight
         keeps its row note and busy lock (force-mounted content is not hidden by
         Reka itself, so the class does it); History mounts fresh on each visit
         and reads the store when it opens. -->
    <TabsContent
      value="queue"
      force-mount
      :class="tab !== 'queue' && 'hidden'"
    >
      <QueuePanel />
    </TabsContent>
    <TabsContent value="history">
      <HistoryPanel />
    </TabsContent>
  </Tabs>
</template>
