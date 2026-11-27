<template>
  <el-drawer
    :model-value="modelValue"
    title="Chunk Inspector"
    size="min(720px, 92vw)"
    @update:model-value="emit('update:modelValue', $event)"
    @open="load(1)"
  >
    <div v-if="doc" class="chunk-head">
      <div class="chunk-title">{{ doc.filename }}</div>
      <div class="resource-meta">共 {{ total }} 个 Child Chunk（命中后可按 Token 预算扩展 Parent / 相邻片段）</div>
    </div>
    <el-skeleton v-if="loading" animated :rows="8" />
    <EmptyState v-else-if="failed" title="Chunk 加载失败" description="文档可能尚未完成索引。" :icon="CircleAlert" />
    <EmptyState v-else-if="!items.length" title="暂无 Chunk" description="文档仍在构建或构建失败。" :icon="Layers" />
    <template v-else>
      <div class="chunk-list">
        <article v-for="chunk in items" :key="chunk.chunk_id" class="chunk-card">
          <div class="chunk-meta">
            <span class="chunk-index">#{{ chunk.chunk_index + 1 }}</span>
            <el-tag size="small" type="info">{{ chunk.element_type || 'text' }}</el-tag>
            <span class="chunk-loc">{{ locationText(chunk) }}</span>
            <span class="chunk-token">{{ chunk.token_count }} tokens</span>
          </div>
          <div v-if="chunk.heading_path?.length" class="chunk-path">{{ chunk.heading_path.join(' / ') }}</div>
          <pre class="chunk-content">{{ chunk.content }}</pre>
          <div class="chunk-foot">
            <span class="resource-meta">{{ chunk.chunk_id.slice(0, 12) }}…</span>
          </div>
        </article>
      </div>
      <div class="chunk-pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          @current-change="load"
        />
      </div>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { CircleAlert, Layers } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { KnowledgeChunk, KnowledgeDocument } from '@/types'

const props = defineProps<{ modelValue: boolean; doc: KnowledgeDocument | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const pageSize = 20
const page = ref(1)
const total = ref(0)
const items = ref<KnowledgeChunk[]>([])
const loading = ref(false)
const failed = ref(false)

function locationText(chunk: KnowledgeChunk) {
  const parts: string[] = []
  if (chunk.page_start) {
    parts.push(chunk.page_end && chunk.page_end !== chunk.page_start
      ? `第 ${chunk.page_start}-${chunk.page_end} 页` : `第 ${chunk.page_start} 页`)
  }
  if (chunk.sheet_name) {
    parts.push(`Sheet「${chunk.sheet_name}」${chunk.row_start ? ` 第 ${chunk.row_start}-${chunk.row_end ?? chunk.row_start} 行` : ''}`)
  }
  if (chunk.slide_number) parts.push(`Slide ${chunk.slide_number}`)
  if (!parts.length) parts.push('无位置信息')
  return parts.join(' · ')
}

async function load(nextPage = page.value) {
  if (!props.doc) return
  page.value = nextPage
  loading.value = true
  failed.value = false
  try {
    const result = await knowledgeApi.listChunks(props.doc.id, {
      page: nextPage, page_size: pageSize,
    })
    items.value = result.items
    total.value = result.total
  } catch {
    failed.value = true
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

watch(() => props.doc?.id, () => {
  if (props.modelValue) void load(1)
})
</script>

<style scoped>
.chunk-head { margin-bottom: 12px; }
.chunk-title { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.chunk-list { display: grid; gap: 12px; }
.chunk-card { padding: 12px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); background: var(--ui-surface); }
.chunk-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.chunk-index { font-family: var(--ui-font-mono); font-size: 12px; color: var(--ui-accent); }
.chunk-loc, .chunk-token { color: var(--ui-muted); font-family: var(--ui-font-mono); font-size: 11px; }
.chunk-token { margin-inline-start: auto; }
.chunk-path { color: var(--ui-muted); font-size: 11px; margin: 6px 0 2px; overflow-wrap: anywhere; }
.chunk-content {
  margin: 6px 0 0; padding: 10px; background: var(--ui-subtle);
  border-radius: var(--ui-radius-sm); font-family: var(--ui-font-mono); font-size: 12px;
  line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow-y: auto;
}
.chunk-foot { margin-top: 6px; display: flex; justify-content: flex-end; }
.chunk-pager { display: flex; justify-content: flex-end; padding-top: 14px; }
</style>
