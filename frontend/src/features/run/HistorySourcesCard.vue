<template>
  <section v-if="sources.length" class="history-sources-card" aria-label="历史上下文来源">
    <header class="history-head">
      <span class="history-symbol"><MessagesSquare :size="17" aria-hidden="true" /></span>
      <div class="history-title">
        <h3>历史上下文来源</h3>
        <p>以下内容来自你过去的会话，仅作为背景资料，可点击打开原会话核实</p>
      </div>
      <span class="history-count">{{ grouped.length }} 个会话</span>
    </header>
    <ul class="history-list">
      <li v-for="group in grouped" :key="group.conversationId">
        <button
          type="button"
          class="history-item"
          :title="`打开《${group.title}》并定位消息`"
          @click="open(group)"
        >
          <span class="history-item-head">
            <span class="history-conversation">{{ group.title }}</span>
            <span v-if="group.scope === 'project'" class="history-scope">项目共享</span>
            <span v-else class="history-scope history-scope-self">本人历史</span>
            <span v-if="group.createdAt" class="history-time">
              <Clock :size="12" aria-hidden="true" />{{ formatTime(group.createdAt) }}
            </span>
            <span v-if="group.sources.length > 1" class="history-count-pill">{{ group.sources.length }} 段</span>
          </span>
          <span class="history-snippet">{{ group.snippet }}</span>
        </button>
      </li>
    </ul>
    <p v-if="queries?.length" class="history-queries">
      检索意图：{{ queries.join(' · ') }}
    </p>
  </section>
</template>

<script setup lang="ts">
/** 历史会话来源卡片：展示真实来源（标题/时间/片段），点击打开原会话并定位消息。 */
import { computed } from 'vue'
import { Clock, MessagesSquare } from '@lucide/vue'
import type { HistorySourceItem } from './blocks'

const props = defineProps<{ sources: HistorySourceItem[]; queries?: string[] }>()
const emit = defineEmits<{ open: [conversationId: number, messageId?: string] }>()

/** 同一会话多个片段合并为一张来源卡片（保留片段数），跳转取最相关片段的消息。 */
const grouped = computed(() => {
  const groups = new Map<number, {
    conversationId: number
    title: string
    scope?: string
    createdAt?: string | null
    snippet: string
    sources: HistorySourceItem[]
  }>()
  for (const source of props.sources) {
    const existing = groups.get(source.conversation_id)
    if (existing) {
      existing.sources.push(source)
      continue
    }
    groups.set(source.conversation_id, {
      conversationId: source.conversation_id,
      title: source.conversation_title,
      scope: source.scope,
      createdAt: source.created_at,
      snippet: source.snippet,
      sources: [source],
    })
  }
  return [...groups.values()]
})

function open(group: { sources: HistorySourceItem[] }) {
  const source = group.sources[0]
  emit('open', source.conversation_id, source.message_ids[0])
}

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}
</script>

<style scoped>
.history-sources-card {
  margin: 10px 0;
  padding: 10px 12px;
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-md);
  background: var(--ui-surface);
}
.history-head { display: flex; align-items: center; gap: 9px; }
.history-symbol {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: var(--ui-radius-sm);
  background: var(--ui-info-soft); color: var(--ui-info); flex: none;
}
.history-title { min-width: 0; flex: 1; }
.history-title h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ui-primary); }
.history-title p { margin: 2px 0 0; font-size: 11px; color: var(--ui-muted); }
.history-count { color: var(--ui-muted); font-size: 11px; flex: none; }
.history-list { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
.history-item {
  width: 100%; display: grid; gap: 4px; padding: 8px 9px; text-align: start;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  background: var(--ui-bg); color: inherit; cursor: pointer;
}
.history-item:hover { border-color: var(--ui-border-strong); background: var(--ui-subtle); }
.history-item-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.history-conversation { font-size: 12px; font-weight: 600; color: var(--ui-primary); }
.history-scope {
  padding: 1px 5px; border-radius: 4px; font-size: 10px;
  background: var(--ui-info-soft); color: var(--ui-info);
}
.history-scope-self { background: var(--ui-subtle); color: var(--ui-secondary); }
.history-count-pill {
  padding: 1px 5px; border-radius: 4px; font-size: 10px;
  background: var(--ui-subtle); color: var(--ui-muted);
}
.history-time { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; color: var(--ui-muted); }
.history-snippet {
  font-size: 12px; line-height: 1.55; color: var(--ui-secondary);
  overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
}
.history-queries { margin: 7px 0 0; font-size: 10px; color: var(--ui-muted); }
</style>
