<script setup lang="ts">
import { Inbox } from '@lucide/vue'
import type { Component } from 'vue'

defineProps<{ title: string; description?: string; icon?: Component }>()
</script>

<template>
  <div class="flex flex-col items-center gap-2 px-5 py-10 text-center leading-normal" role="status">
    <div class="mb-1 grid size-10 place-items-center rounded-lg border border-line bg-subtle text-muted" aria-hidden="true">
      <component :is="icon || Inbox" :size="18" />
    </div>
    <strong class="font-semibold text-foreground">{{ title }}</strong>
    <p v-if="description" class="max-w-md text-sm text-muted">{{ description }}</p>
    <div v-if="$slots.default" class="mt-2"><slot /></div>
  </div>
</template>
