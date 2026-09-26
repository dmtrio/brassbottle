<script setup lang="ts">
import { computed } from 'vue'
import { Bell, BellMinus, BellOff, BellRing, ShieldBan } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useBell } from '@/composables/useNotifications'
import { BROWSER_NEXT_STEP, INSECURE_NEXT_STEP } from '@/lib/notify'

const { state, label, name, reason, press } = useBell()

// A bell that cannot be pressed still answers a click: it says why, which a
// disabled button cannot do on touch or in a browser without tooltips.
const explains = computed(() => state.value === 'denied' || state.value === 'unsupported')
</script>

<template>
  <Popover v-if="explains">
    <PopoverTrigger as-child>
      <Button
        variant="ghost"
        size="icon"
        :title="label"
        :aria-label="name"
        data-testid="notify-bell"
        :data-state="state"
      >
        <ShieldBan
          v-if="state === 'denied'"
          class="size-icon text-warn-text"
          data-icon="shield-ban"
        />
        <BellMinus
          v-else
          class="size-icon text-warn-text"
          data-icon="bell-minus"
        />
      </Button>
    </PopoverTrigger>
    <PopoverContent
      align="end"
      :data-testid="state === 'denied' ? 'notify-blocked' : 'notify-unsupported'"
    >
      <template v-if="state === 'denied'">
        <p class="text-body font-medium">
          Notifications are blocked
        </p>
        <p class="row-meta">
          Your browser is blocking notifications for this site. Allow them in the browser's
          site settings, then come back to this page.
        </p>
      </template>
      <template v-else>
        <p class="text-body font-medium">
          Notifications are not available
        </p>
        <p class="row-meta">
          {{ reason === 'insecure' ? INSECURE_NEXT_STEP : BROWSER_NEXT_STEP }}
        </p>
      </template>
    </PopoverContent>
  </Popover>
  <Button
    v-else
    variant="ghost"
    size="icon"
    :title="label"
    :aria-label="name"
    :aria-pressed="state === 'on' ? 'true' : state === 'muted' ? 'false' : undefined"
    data-testid="notify-bell"
    :data-state="state"
    @click="press"
  >
    <BellRing
      v-if="state === 'on'"
      class="size-icon text-allow-text"
      data-icon="bell-ring"
    />
    <Bell
      v-else-if="state === 'default'"
      class="size-icon"
      data-icon="bell"
    />
    <BellOff
      v-else
      class="size-icon text-muted-foreground"
      data-icon="bell-off"
    />
  </Button>
</template>
