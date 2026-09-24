<script setup lang="ts">
import { Search } from '@lucide/vue'
import { Input } from '@/components/ui/input'

const model = defineModel<string>({ required: true })
defineProps<{ placeholder?: string }>()
</script>

<template>
  <div class="search-field">
    <Search class="search-icon" />
    <Input
      v-model="model"
      :placeholder="placeholder ?? 'Search'"
      :aria-label="placeholder ?? 'Search'"
      class="bg-background pl-search-pad"
    />
  </div>
</template>
