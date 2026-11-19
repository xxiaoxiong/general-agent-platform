<script setup lang="ts">
import { computed, ref, watch, nextTick } from 'vue'
import { CornerDownLeft, Search } from '@lucide/vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import UiDialog from '@/components/ui/UiDialog.vue'
import type { NavigationEntry, NavigationGroup } from './navigation'

const props = defineProps<{ groups: NavigationGroup[] }>()
const open = defineModel<boolean>('open', { required: true })
const emit = defineEmits<{ navigate: [path: string] }>()
const query = ref('')
const selected = ref(0)
const input = ref<HTMLInputElement>()
const filtered = computed(() => {
  const terms = query.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean)
  return props.groups.map(group => ({
    ...group,
    entries: group.entries.filter(entry => {
      const text = `${entry.title} ${entry.path} ${entry.keywords} ${group.title}`.toLocaleLowerCase()
      return terms.every(term => text.includes(term))
    }),
  })).filter(group => group.entries.length)
})
const results = computed(() => filtered.value.flatMap(group => group.entries))
const active = computed(() => results.value[selected.value])
const optionId = (entry: NavigationEntry) => `command-option-${entry.name}`

watch(query, () => { selected.value = 0 })
watch(open, async (value) => {
  if (!value) return
  query.value = ''
  selected.value = 0
  await nextTick()
  input.value?.focus()
})

function move(delta: number) {
  const count = results.value.length
  if (!count) return
  selected.value = (selected.value + delta + count) % count
  if (active.value) document.getElementById(optionId(active.value))?.scrollIntoView({ block: 'nearest' })
}

function navigate(entry?: NavigationEntry) {
  if (!entry) return
  open.value = false
  emit('navigate', entry.path)
}
</script>

<template>
  <UiDialog v-model:open="open" title="全局命令" description="搜索并打开平台页面" variant="command">
    <div class="command-input">
      <Search :size="18" aria-hidden="true" />
      <input
        ref="input" v-model="query" placeholder="搜索页面或模块…" aria-label="搜索页面"
        role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="command-results"
        :aria-activedescendant="active && optionId(active)" autocomplete="off"
        @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)"
        @keydown.enter="!$event.isComposing && navigate(active)"
      >
      <button type="button" class="command-escape" aria-label="关闭搜索" @click="open = false">Esc</button>
    </div>
    <div id="command-results" class="command-results" role="listbox" aria-label="搜索结果">
      <div v-for="group in filtered" :key="group.title" role="group" :aria-label="group.title">
        <p aria-hidden="true" class="command-group">{{ group.title }}</p>
        <div
          v-for="entry in group.entries" :id="optionId(entry)" :key="entry.name"
          role="option" :aria-selected="entry === active" class="command-option"
          @mousedown.prevent @click="navigate(entry)"
        >
          <component :is="entry.icon" :size="16" aria-hidden="true" />
          <span>{{ entry.title }}</span>
          <CornerDownLeft v-if="entry === active" :size="14" aria-hidden="true" />
        </div>
      </div>
      <EmptyState v-if="!results.length" :icon="Search" title="未找到相关页面" description="试试其他关键词。" />
    </div>
    <div class="command-hints" aria-hidden="true"><span><kbd>↑</kbd> <kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span></div>
  </UiDialog>
</template>

<style scoped>
.command-input { display: flex; align-items: center; gap: 12px; padding: 13px 16px; border-bottom: var(--ui-line); color: var(--ui-muted); }
.command-input:focus-within { box-shadow: inset 0 -1px var(--ui-accent); }
.command-input input { min-width: 0; flex: 1; padding: 7px 0; border: 0; outline: none; font-size: var(--ui-size-body); color: var(--ui-text); background: transparent; }
.command-escape, .command-hints kbd {
  padding: 2px 5px; border: var(--ui-line); border-radius: var(--ui-radius-sm);
  background: var(--ui-subtle); font: var(--ui-size-xs)/1.2 var(--ui-font-mono); color: var(--ui-secondary);
}
.command-results { max-height: min(46dvh, 420px); overflow-y: auto; padding: 6px; }
.command-group { padding: 8px 9px 4px; color: var(--ui-muted); font-size: var(--ui-size-xs); }
.command-option { display: flex; gap: 10px; align-items: center; cursor: pointer; padding: 8px 9px; border-radius: var(--ui-radius-md); }
.command-option span { flex: 1; }
.command-option svg { color: var(--ui-muted); }
.command-option[aria-selected="true"], .command-option:hover { background: var(--ui-accent-soft); color: var(--ui-accent); }
.command-option[aria-selected="true"] svg { color: inherit; }
.command-hints { display: flex; gap: 14px; padding: 10px 14px; color: var(--ui-muted); border-top: var(--ui-line); font-size: var(--ui-size-xs); }
</style>
