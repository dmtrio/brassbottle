<script setup lang="ts">
import { CheckCircle2 } from 'lucide-vue-next'
defineProps<{ message: string }>()
</script>

<template>
  <div class="panel empty-state">
    <CheckCircle2 class="size-icon-xl text-allow-text" />
    <p>{{ message }}</p>
  </div>
</template>
