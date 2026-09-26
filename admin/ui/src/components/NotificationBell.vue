<script setup lang="ts">
import { Bell, BellOff, BellRing } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useBell } from '@/composables/useNotifications'

const { state, label, press } = useBell()
</script>

<template>
  <!-- Blocked: the browser will not ask again, so a click explains instead of prompting. -->
  <Popover v-if="state === 'denied'">
    <PopoverTrigger as-child>
      <Button
        variant="ghost"
        size="icon"
        :title="label"
        :aria-label="label"
        data-testid="notify-bell"
        data-state="denied"
      >
        <BellOff class="size-icon text-warn-text" />
      </Button>
    </PopoverTrigger>
    <PopoverContent
      align="end"
      data-testid="notify-blocked"
    >
      <p class="text-body font-medium">
        Notifications are blocked
      </p>
      <p class="row-meta">
        Your browser is blocking notifications for this site. Allow them in the browser's
        site settings, then reload this page.
      </p>
    </PopoverContent>
  </Popover>
  <Button
    v-else
    variant="ghost"
    size="icon"
    :disabled="state === 'unsupported'"
    :title="label"
    :aria-label="label"
    :aria-pressed="state === 'on' ? 'true' : state === 'muted' ? 'false' : undefined"
    data-testid="notify-bell"
    :data-state="state"
    @click="press"
  >
    <BellRing
      v-if="state === 'on'"
      class="size-icon text-allow-text"
    />
    <Bell
      v-else-if="state === 'default'"
      class="size-icon"
    />
    <BellOff
      v-else
      class="size-icon text-muted-foreground"
    />
  </Button>
</template>
