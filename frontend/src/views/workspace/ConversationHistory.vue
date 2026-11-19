<template>
  <aside id="conversation-history" class="history" aria-label="会话历史">
    <div class="history-head">
      <div class="history-heading">
        <h2>会话历史</h2>
        <button type="button" class="square-button" aria-label="收起会话历史" title="收起会话历史" @click="$emit('close')">
          <PanelLeftClose :size="16" />
        </button>
      </div>
      <button type="button" class="new-chat" :disabled="creating" @click="$emit('create')">
        <LoaderCircle v-if="creating" class="spin" :size="16" />
        <SquarePen v-else :size="16" />
        新建对话
      </button>
    </div>
    <div v-if="items.length" class="history-search">
      <Search :size="14" />
      <input v-model="query" type="search" aria-label="搜索最近对话" placeholder="搜索最近对话" />
    </div>
    <div class="history-list" :aria-busy="loading">
      <div v-if="loading" role="status" class="history-state">
        <LoaderCircle class="spin" :size="16" /> 正在加载会话…
      </div>
      <div v-else-if="failed" class="history-state">
        无法加载会话
        <button type="button" class="retry" @click="$emit('retry')">重试</button>
      </div>
      <p v-else-if="!filtered.length" class="history-state">
        {{ query.trim() ? '没有匹配的对话' : '新建对话后，可在这里继续。' }}
      </p>
      <ul v-else>
        <li v-for="c in filtered" :key="c.id" class="conv-item" :class="{ active: activeId === c.id }">
          <button
            type="button"
            class="conv-target"
            :aria-current="activeId === c.id ? 'true' : undefined"
            :title="c.title || '新对话'"
            @click="$emit('select', c)"
          >
            <div class="conv-copy">
              <span class="conv-title">{{ c.title || '新对话' }}</span>
              <span v-if="status(c)" class="conv-status" :class="{ waiting: status(c) === '等待审批' }">
                {{ status(c) }}
              </span>
              <time v-else-if="c.updated_at || c.created_at" class="conv-date" :datetime="c.updated_at || c.created_at || ''">
                {{ formatDate(c.updated_at || c.created_at) }}
              </time>
            </div>
          </button>
          <button
            type="button"
            class="square-button delete"
            :aria-label="'删除对话 ' + (c.title || '新对话')"
            title="删除对话"
            :disabled="deletingId === c.id"
            @click="$emit('remove', c)"
          >
            <LoaderCircle v-if="deletingId === c.id" class="spin" :size="14" />
            <Trash2 v-else :size="14" />
          </button>
        </li>
      </ul>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { LoaderCircle, PanelLeftClose, Search, SquarePen, Trash2 } from '@lucide/vue'
import type { Conversation } from '@/api/v2'

const props = defineProps<{
  items: Conversation[]
  activeId?: number
  deletingId: number | null
  loading: boolean
  failed: boolean
  creating: boolean
  status: (item: Conversation) => string
}>()

defineEmits<{
  close: []
  retry: []
  create: []
  select: [item: Conversation]
  remove: [item: Conversation]
}>()

const query = ref('')
const filtered = computed(() => {
  const term = query.value.trim().toLocaleLowerCase()
  return props.items.filter((item) => (item.title || '新对话').toLocaleLowerCase().includes(term))
})

const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' })

function formatDate(value: string | null) {
  if (!value) return ''
  // 会话接口将数据库 UTC 时间序列化为 ISO 字符串，部分数据没有时区后缀。
  const timestamp = value.includes('T') && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value + 'Z' : value
  const date = new Date(timestamp)
  return Number.isNaN(date.valueOf()) ? '' : dateFormat.format(date)
}
</script>

<style scoped>
.history { display: flex; flex-direction: column; gap: 12px; background: var(--ui-bg); min-height: 0; }
.history-head { padding: 14px 12px 0; }
.history-heading { display: flex; align-items: center; justify-content: space-between; margin: 0 0 8px 4px; }
.history-heading h2 { font-size: 13px; font-weight: 650; color: var(--ui-text); }
.square-button {
  display: inline-flex; justify-content: center; align-items: center; width: 30px; height: 30px;
  border: 0; background: transparent; color: var(--ui-muted); border-radius: var(--ui-radius-sm); cursor: pointer;
}
.square-button:hover { background: var(--ui-hover); color: var(--ui-text); }
.new-chat {
  width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 8px 10px;
  color: var(--ui-text); background: var(--ui-surface); border: 1px solid var(--ui-border-strong);
  border-radius: var(--ui-radius-md); cursor: pointer; font-weight: 600; box-shadow: var(--ui-shadow-sm);
}
.new-chat:hover { border-color: var(--ui-accent); color: var(--ui-accent); }
.new-chat:disabled { cursor: wait; opacity: .65; }
.history-search {
  margin: 0 12px; padding: 0 9px; min-height: 32px; display: flex; align-items: center; gap: 8px;
  color: var(--ui-muted); border-bottom: 1px solid var(--ui-border);
}
.history-search svg { flex: none; }
.history-search input { width: 100%; min-width: 0; padding: 6px 0; border: 0; background: transparent; color: var(--ui-text); font-size: 13px; }
.history-search input:focus { outline: none; }
.history-search:focus-within { border-color: var(--ui-accent); }
.history-list { flex: 1; overflow: auto; padding: 0 8px 16px; scrollbar-gutter: stable; }
.history-list ul { list-style: none; margin: 0; padding: 0; }
.history-state { display: flex; align-items: center; gap: 6px; padding: 8px; color: var(--ui-muted); font-size: 12px; }
.retry { color: var(--ui-accent); border: none; background: transparent; cursor: pointer; }
.conv-item { display: flex; align-items: center; gap: 2px; margin: 2px 0; border-radius: var(--ui-radius-md); }
.conv-item:hover { background: var(--ui-hover); }
.conv-item.active { background: var(--ui-accent-soft); }
.conv-target { flex: 1; min-width: 0; text-align: left; border: 0; background: transparent; padding: 6px 8px; color: var(--ui-text); cursor: pointer; }
.conv-copy { display: grid; gap: 2px; flex: 1; min-width: 0; }
.conv-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.conv-item.active .conv-title { color: var(--ui-accent); font-weight: 650; }
.conv-date, .conv-status { font-size: 12px; color: var(--ui-muted); }
.conv-status { color: var(--ui-info); }
.conv-status.waiting { color: var(--ui-warning); }
.delete { flex: none; margin-right: 3px; opacity: 0; }
.conv-item:hover .delete, .conv-item:focus-within .delete { opacity: 1; }
.delete:hover { background: var(--ui-danger-soft); color: var(--ui-danger); }
@media (hover: none) { .delete { opacity: 1; } }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
