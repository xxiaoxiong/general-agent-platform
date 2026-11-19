<script setup lang="ts">
import { Orbit } from '@lucide/vue'

defineProps<{ size?: number }>()
</script>

<template>
  <span class="brand-mark" :style="{ width: (size || 30) + 'px', height: (size || 30) + 'px' }" aria-hidden="true">
    <Orbit :size="(size || 30) * .63" :stroke-width="1.7" />
  </span>
</template>

<style scoped>
.brand-mark {
  flex: none; display: inline-grid; place-items: center;
  border-radius: var(--ui-radius-md);
  background: var(--ui-accent); color: var(--ui-on-accent); box-shadow: var(--ui-shadow-sm);
}
</style>
