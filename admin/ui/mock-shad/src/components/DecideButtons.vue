<script setup lang="ts">
import { Check, X, ChevronDown, RotateCw, Loader2 } from 'lucide-vue-next'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ACTION_LABEL, type DecideAction, type OpenRow } from '@/mock/data'

const props = defineProps<{ row: OpenRow; busy: boolean; stretch?: boolean }>()
const emit = defineEmits<{ decide: [action: DecideAction]; permanentDeny: [action: 'deny_bottle' | 'deny_global'] }>()
</script>

<template>
  <div class="flex gap-inline" :class="stretch ? 'w-full' : 'justify-end'">
    <div class="flex" :class="stretch && 'flex-1'">
      <Button size="sm" variant="allow" class="rounded-r-none" :class="stretch && 'flex-1'" :disabled="busy" @click="emit('decide', 'allow_live')">
        <Loader2 v-if="busy" class="size-icon animate-spin" />
        <RotateCw v-else-if="props.row.last_error" class="size-icon" /><Check v-else class="size-icon" />
        {{ props.row.last_error ? 'Retry allow' : 'Allow' }}
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button size="sm" variant="allow" class="rounded-l-none border-l border-background/30 px-inline" :disabled="busy" aria-label="More allow options">
            <ChevronDown class="size-icon" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-menu">
          <DropdownMenuItem @select="emit('decide', 'allow_live')">
            <div><p>{{ ACTION_LABEL.allow_live }}</p><p class="row-caption">Until the bottle restarts</p></div>
          </DropdownMenuItem>
          <DropdownMenuItem @select="emit('decide', 'allow_manifest')">
            <div><p>{{ ACTION_LABEL.allow_manifest }}</p><p class="row-caption">Saved to this bottle's manifest</p></div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    <div class="flex" :class="stretch && 'flex-1'">
      <Button size="sm" variant="outline" class="rounded-r-none text-destructive hover:text-destructive" :class="stretch && 'flex-1'" :disabled="busy" @click="emit('decide', 'deny')">
        <X class="size-icon" /> Deny
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button size="sm" variant="outline" class="rounded-l-none border-l-0 px-inline text-destructive hover:text-destructive" :disabled="busy" aria-label="More deny options">
            <ChevronDown class="size-icon" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-menu">
          <DropdownMenuItem @select="emit('decide', 'deny')">
            <div><p>{{ ACTION_LABEL.deny }}</p><p class="row-caption">This request only</p></div>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel class="row-caption font-normal">Permanently</DropdownMenuLabel>
          <DropdownMenuItem @select="emit('permanentDeny', 'deny_bottle')">
            <div><p>{{ ACTION_LABEL.deny_bottle }}…</p><p class="row-caption">This bottle never asks again</p></div>
          </DropdownMenuItem>
          <DropdownMenuItem variant="destructive" @select="emit('permanentDeny', 'deny_global')">
            <div><p>{{ ACTION_LABEL.deny_global }}…</p><p class="row-caption">No bottle can reach it</p></div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </div>
</template>
