<script setup lang="ts">
import { computed } from 'vue'
import { Box, ChevronDown } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

const model = defineModel<string[]>({ required: true })
defineProps<{ options: string[] }>()
const label = computed(() => model.value.length ? `${model.value.length} bottle${model.value.length > 1 ? 's' : ''}` : 'All bottles')
function toggle(b: string, on: boolean) { model.value = on ? [...model.value, b] : model.value.filter((x) => x !== b) }
</script>

<template>
  <DropdownMenu>
    <DropdownMenuTrigger as-child>
      <Button variant="outline" class="h-control bg-background"><Box class="size-icon" />{{ label }}<ChevronDown class="size-icon opacity-60" /></Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent class="w-menu">
      <DropdownMenuCheckboxItem v-for="b in options" :key="b" :model-value="model.includes(b)"
        @update:model-value="(v: boolean) => toggle(b, v)" @select.prevent>{{ b }}</DropdownMenuCheckboxItem>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
