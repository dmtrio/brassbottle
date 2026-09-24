<script setup lang="ts">
import { Box } from '@lucide/vue'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

// One bottle or all of them: the broker's `container` filter takes a single name.
const ALL = '__all__'
const model = defineModel<string | null>({ required: true })
defineProps<{ options: string[] }>()

function choose(value: unknown): void {
  model.value = typeof value === 'string' && value !== ALL ? value : null
}
</script>

<template>
  <Select
    :model-value="model ?? ALL"
    @update:model-value="choose"
  >
    <SelectTrigger
      class="w-select bg-background"
      aria-label="Bottle"
      data-testid="history-bottle"
    >
      <Box class="size-icon" />
      <SelectValue
        class="flex-1 text-left"
        placeholder="All bottles"
      />
    </SelectTrigger>
    <SelectContent>
      <SelectItem :value="ALL">
        All bottles
      </SelectItem>
      <SelectItem
        v-for="bottle in options"
        :key="bottle"
        :value="bottle"
      >
        {{ bottle }}
      </SelectItem>
    </SelectContent>
  </Select>
</template>
