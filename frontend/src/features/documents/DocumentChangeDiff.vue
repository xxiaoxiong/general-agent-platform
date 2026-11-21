<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentReviewChange } from '@/api/documents'
import DocumentReviewImage from './DocumentReviewImage.vue'

const props = defineProps<{ change: DocumentReviewChange }>()

const panes = computed(() => {
  const before = Array.from(props.change.before ?? '')
  const after = Array.from(props.change.after ?? '')
  const shortest = Math.min(before.length, after.length)
  let start = 0
  while (start < shortest && before[start] === after[start]) start += 1
  let end = 0
  while (end < shortest - start && before[before.length - 1 - end] === after[after.length - 1 - end]) end += 1

  return [
    { key: 'before', label: '修改前', characters: before, image: props.change.before_image_url },
    { key: 'after', label: '修改后', characters: after, image: props.change.after_image_url },
  ].map(({ characters, ...pane }) => {
    const endIndex = characters.length - end
    return { ...pane, head: characters.slice(0, start).join(''), change: characters.slice(start, endIndex).join(''),
      tail: characters.slice(endIndex).join('') }
  })
})
</script>

<template>
  <div class="comparison">
    <div v-for="pane in panes" :key="pane.key" :data-side="pane.key" :aria-label="pane.label" role="group" class="pane">
      <header>{{ pane.label }}</header>
      <DocumentReviewImage v-if="pane.image" :path="pane.image" :alt="`${pane.label}${change.title || '的图片'}`" />
      <p v-if="pane.head || pane.change || pane.tail">{{ pane.head }}<mark v-if="pane.change">{{ pane.change }}</mark>{{ pane.tail }}</p>
      <p v-else-if="!pane.image" class="empty">无内容</p>
    </div>
  </div>
</template>

<style scoped>
.comparison {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.pane {
  border: 1px solid var(--ui-border);
  background: var(--ui-surface);
  border-radius: var(--ui-radius-sm);
  padding: 8px 10px;
  min-width: 0;
}
.pane header {
  color: var(--ui-secondary);
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 5px;
}
.pane p {
  max-height: 18rem;
  overflow: auto;
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.6;
}
.pane img + p { margin-top: 5px; }
.pane .empty { color: var(--ui-muted); }
mark {
  background: var(--ui-danger-soft);
  color: var(--ui-text);
  border-radius: 2px;
}
[data-side="after"] mark { background: var(--ui-success-soft); }
@media (max-width: 600px) { .comparison { grid-template-columns: minmax(0, 1fr); } }
</style>
