<script setup lang="ts">
import { computed } from 'vue'
import { Check, ChevronDown, Loader2, RotateCw, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { DecideAction } from '@/api/egress'
import type { OpenRow } from '@/types'

const ACTION_LABEL: Record<DecideAction, string> = {
  allow_live: 'Allow',
  allow_manifest: 'Allow permanently · bottle',
  deny: 'Deny',
  deny_bottle: 'Deny permanently · bottle',
  deny_global: 'Deny permanently · global',
}

const props = defineProps<{
  row: OpenRow
  busy: boolean
  // The action of the decide holding the row: the spinner goes on that button.
  pending?: DecideAction
  stretch?: boolean
}>()

const emit = defineEmits<{
  decide: [action: DecideAction]
  permanentDeny: [action: 'deny_bottle' | 'deny_global']
}>()

const denying = computed(() => props.busy && props.pending?.startsWith('deny') === true)

const allowLabel = computed(() => (props.row.last_error ? 'Retry allow' : ACTION_LABEL.allow_live))

// Every row shows the same visible labels, so each button's accessible name
// says which request it acts on (the bottle too: the All view repeats hosts).
const target = computed(() => `${props.row.host}:${props.row.port} in ${props.row.container}`)
</script>

<template>
  <div
    class="flex gap-inline"
    :class="stretch ? 'w-full' : 'justify-end'"
    data-testid="decide-actions"
  >
    <div
      class="flex"
      :class="stretch && 'flex-1'"
    >
      <Button
        size="sm"
        variant="allow"
        class="rounded-r-none"
        :class="stretch && 'flex-1'"
        :disabled="busy"
        :aria-label="`${allowLabel} ${target}`"
        @click="emit('decide', 'allow_live')"
      >
        <Loader2
          v-if="busy && !denying"
          class="size-icon animate-spin"
        />
        <RotateCw
          v-else-if="row.last_error"
          class="size-icon"
        />
        <Check
          v-else
          class="size-icon"
        />
        {{ allowLabel }}
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            size="sm"
            variant="allow"
            class="rounded-l-none border-l border-l-background/30 px-inline"
            :disabled="busy"
            :aria-label="`More allow options for ${target}`"
          >
            <ChevronDown class="size-icon" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          class="w-menu"
        >
          <DropdownMenuItem @select="emit('decide', 'allow_live')">
            <div>
              <p>{{ ACTION_LABEL.allow_live }}</p><p class="row-caption">
                Until the bottle restarts
              </p>
            </div>
          </DropdownMenuItem>
          <DropdownMenuItem @select="emit('decide', 'allow_manifest')">
            <div>
              <p>{{ ACTION_LABEL.allow_manifest }}</p><p class="row-caption">
                Saved to this bottle's manifest
              </p>
            </div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    <div
      class="flex"
      :class="stretch && 'flex-1'"
    >
      <Button
        size="sm"
        variant="outline"
        class="rounded-r-none text-destructive hover:text-destructive"
        :class="stretch && 'flex-1'"
        :disabled="busy"
        :aria-label="`Deny ${target}`"
        @click="emit('decide', 'deny')"
      >
        <Loader2
          v-if="denying"
          class="size-icon animate-spin"
        />
        <X
          v-else
          class="size-icon"
        /> Deny
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            size="sm"
            variant="outline"
            class="rounded-l-none border-l-0 px-inline text-destructive hover:text-destructive"
            :disabled="busy"
            :aria-label="`More deny options for ${target}`"
          >
            <ChevronDown class="size-icon" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          class="w-menu"
        >
          <DropdownMenuItem @select="emit('decide', 'deny')">
            <div>
              <p>{{ ACTION_LABEL.deny }}</p><p class="row-caption">
                This request only
              </p>
            </div>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel class="row-caption font-normal">
            Permanently
          </DropdownMenuLabel>
          <DropdownMenuItem @select="emit('permanentDeny', 'deny_bottle')">
            <div>
              <p>{{ ACTION_LABEL.deny_bottle }}…</p><p class="row-caption">
                This bottle never asks again
              </p>
            </div>
          </DropdownMenuItem>
          <DropdownMenuItem
            class="text-destructive focus:text-destructive"
            @select="emit('permanentDeny', 'deny_global')"
          >
            <div>
              <p>{{ ACTION_LABEL.deny_global }}…</p><p class="row-caption">
                No bottle can reach it
              </p>
            </div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </div>
</template>
