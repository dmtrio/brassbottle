<script setup lang="ts">
import { computed } from 'vue'
import { Box, ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

// Any number of bottles, none meaning all. The queue filters in the browser, so
// unlike BottleFilter (History's, one bottle because the broker's `container`
// parameter takes one) it can offer several.
const model = defineModel<string[]>({ required: true })
defineProps<{ options: string[] }>()

const label = computed(() => {
  if (model.value.length === 0) return 'All bottles'
  return model.value.length === 1 ? model.value[0] : `${model.value.length} bottles`
})

function toggle(bottle: string, on: boolean): void {
  model.value = on ? [...model.value, bottle] : model.value.filter((name) => name !== bottle)
}
</script>

<template>
  <DropdownMenu>
    <DropdownMenuTrigger as-child>
      <Button
        variant="outline"
        class="h-control w-select justify-between bg-background"
        :aria-label="`Bottles: ${label}`"
        data-testid="queue-bottle-filter"
      >
        <Box class="size-icon flex-none" />
        <span class="flex-1 truncate text-left">{{ label }}</span>
        <ChevronDown class="size-icon flex-none opacity-60" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent
      align="start"
      class="w-menu"
    >
      <DropdownMenuCheckboxItem
        v-for="bottle in options"
        :key="bottle"
        :model-value="model.includes(bottle)"
        @update:model-value="(on: boolean | 'indeterminate') => toggle(bottle, on === true)"
        @select.prevent
      >
        {{ bottle }}
      </DropdownMenuCheckboxItem>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
