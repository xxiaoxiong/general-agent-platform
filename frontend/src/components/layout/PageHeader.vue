<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <header class="page-heading">
    <div class="page-heading-copy">
      <div v-if="$slots.eyebrow" class="page-heading-context"><slot name="eyebrow" /></div>
      <h1>{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="page-heading-actions"><slot name="actions" /></div>
  </header>
</template>

<style scoped>
.page-heading { display: flex; justify-content: space-between; align-items: end; flex-wrap: wrap; gap: var(--ui-space-4); margin-bottom: var(--ui-space-8); }
.page-heading-copy { min-width: 0; }
.page-heading h1 { font-size: var(--ui-size-title); line-height: var(--ui-leading-tight); letter-spacing: var(--ui-tracking-tight); font-weight: var(--ui-weight-semibold); overflow-wrap: anywhere; }
.page-heading p { margin-top: var(--ui-space-2); color: var(--ui-muted); }
.page-heading-context { color: var(--ui-muted); font-size: var(--ui-size-sm); margin-bottom: var(--ui-space-3); }
.page-heading-context :deep(a) { color: inherit; text-decoration: none; }
.page-heading-context :deep(a:hover) { color: var(--ui-accent); text-decoration: underline; }
.page-heading-actions { display: flex; align-items: center; gap: var(--ui-space-2); max-width: 100%; flex-wrap: wrap; }
.page-heading-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
