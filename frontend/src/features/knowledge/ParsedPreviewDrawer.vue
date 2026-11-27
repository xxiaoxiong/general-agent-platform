<template>
  <el-drawer
    :model-value="modelValue"
    title="解析预览"
    size="min(760px, 92vw)"
    @update:model-value="emit('update:modelValue', $event)"
    @open="load"
  >
    <div v-if="doc" class="preview-head">
      <div class="preview-title">{{ preview?.title || doc.filename }}</div>
      <div class="preview-sub">
        <el-tag size="small" type="info">{{ (preview?.source_type || doc.file_type).toUpperCase() }}</el-tag>
        <span class="resource-meta">{{ preview?.parser_name || '-' }} v{{ preview?.parser_version || '-' }}</span>
        <span v-if="doc.page_count" class="resource-meta">{{ doc.page_count }} 页</span>
        <span v-if="doc.sheet_count" class="resource-meta">{{ doc.sheet_count }} Sheet</span>
        <span v-if="doc.slide_count" class="resource-meta">{{ doc.slide_count }} Slide</span>
        <span class="resource-meta">{{ preview?.total_elements ?? doc.element_count }} 元素</span>
      </div>
    </div>

    <el-skeleton v-if="loading" animated :rows="8" />
    <EmptyState v-else-if="failed" title="解析结果加载失败" description="文档可能仍在构建或构建失败，请查看文档状态与错误信息。" :icon="CircleAlert" />
    <template v-else-if="preview">
      <el-alert
        v-for="warning in preview.warnings" :key="warning"
        :title="warning" type="warning" :closable="false" show-icon class="preview-warning"
      />
      <div v-if="stats.length" class="stat-row">
        <span v-for="item in stats" :key="item.label" class="stat-chip">
          <span class="stat-label">{{ item.label }}</span>{{ item.value }}
        </span>
      </div>

      <div class="element-list">
        <div v-for="element in preview.elements" :key="element.element_id" class="element" :class="`is-${element.type}`">
          <div class="element-head">
            <el-tag size="small" :type="elementTagType(element.type)">{{ elementLabel(element.type) }}</el-tag>
            <span v-for="(part, index) in locationParts(element)" :key="index" class="element-loc">{{ part }}</span>
            <span v-if="element.ocr_score != null" class="element-loc">OCR {{ (element.ocr_score * 100).toFixed(0) }}%</span>
          </div>
          <div v-if="element.heading_path?.length" class="element-path">{{ element.heading_path.join(' / ') }}</div>
          <div v-if="element.type === 'heading'" class="element-heading" :style="{ paddingInlineStart: `${Math.max(0, (element.level || 1) - 1) * 14}px` }">
            {{ element.text }}
          </div>
          <template v-else-if="element.type === 'table' && element.table">
            <el-table :data="tableRows(element)" size="small" border class="element-table">
              <el-table-column
                v-for="(header, index) in element.table.headers" :key="index"
                :label="header || `列 ${index + 1}`"
              >
                <template #default="{ row }">{{ row[index] }}</template>
              </el-table-column>
            </el-table>
            <div v-if="element.table.rows.length > 20" class="resource-meta table-more">
              仅展示前 20 行，共 {{ element.table.rows.length }} 行
            </div>
          </template>
          <pre v-else-if="element.type === 'code'" class="element-code">{{ element.text }}</pre>
          <p v-else class="element-text">{{ element.text }}</p>
        </div>
      </div>

      <div v-if="preview.total_elements > loadedCount" class="load-more">
        <el-button :loading="loadingMore" @click="loadMore">加载更多（已显示 {{ loadedCount }} / {{ preview.total_elements }}）</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CircleAlert } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { KnowledgeDocument, ParsedElement, ParsedPreview } from '@/types'

const props = defineProps<{ modelValue: boolean; doc: KnowledgeDocument | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const PAGE_SIZE = 100
const preview = ref<ParsedPreview | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const failed = ref(false)
const offset = ref(0)

const loadedCount = computed(() => offset.value + (preview.value?.elements.length || 0))
const stats = computed(() => {
  const value = preview.value?.parse_stats || {}
  const items = [
    { label: '页', value: value.page_count },
    { label: '元素', value: value.element_count },
    { label: '表格', value: value.table_count },
    { label: '图片', value: value.image_count },
    { label: 'OCR 页', value: Array.isArray(value.ocr_pages) ? value.ocr_pages.length : 0 },
    { label: '字符', value: value.char_count },
  ]
  return items.filter(item => item.value != null && Number(item.value) > 0)
})

const TYPE_LABELS: Record<string, string> = {
  heading: '标题', paragraph: '正文', list_item: '列表', table: '表格',
  code: '代码', formula: '公式', image: '图片', caption: '图注',
}

function elementLabel(type: string) {
  return TYPE_LABELS[type] || type
}

function elementTagType(type: string) {
  if (type === 'heading') return 'primary'
  if (type === 'table') return 'success'
  if (type === 'code') return 'info'
  if (type === 'image') return 'warning'
  return 'info'
}

function locationParts(element: ParsedElement) {
  const prov = element.provenance || {}
  const parts: string[] = []
  if (prov.page) {
    parts.push(prov.page_end && prov.page_end !== prov.page ? `第 ${prov.page}-${prov.page_end} 页` : `第 ${prov.page} 页`)
  }
  if (prov.sheet_name) {
    parts.push(`Sheet「${prov.sheet_name}」${prov.row_start ? ` 第 ${prov.row_start}-${prov.row_end ?? prov.row_start} 行` : ''}`)
  }
  if (prov.slide_number) parts.push(`Slide ${prov.slide_number}`)
  if (prov.table_index) parts.push(`表格 ${prov.table_index}`)
  return parts
}

function tableRows(element: ParsedElement) {
  return (element.table?.rows || []).slice(0, 20)
}

async function load() {
  if (!props.doc) return
  loading.value = true
  failed.value = false
  preview.value = null
  offset.value = 0
  try {
    preview.value = await knowledgeApi.getParsedPreview(props.doc.id, { offset: 0, limit: PAGE_SIZE })
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (!props.doc || !preview.value || loadingMore.value) return
  loadingMore.value = true
  try {
    const nextOffset = offset.value + preview.value.elements.length
    const next = await knowledgeApi.getParsedPreview(props.doc.id, { offset: nextOffset, limit: PAGE_SIZE })
    preview.value.elements.push(...next.elements)
    offset.value = nextOffset
  } finally {
    loadingMore.value = false
  }
}

watch(() => props.doc?.id, () => {
  if (props.modelValue) void load()
})
</script>

<style scoped>
.preview-head { margin-bottom: 12px; }
.preview-title { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.preview-sub { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 6px; }
.preview-warning { margin-bottom: 6px; }
.stat-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
.stat-chip {
  display: inline-flex; align-items: baseline; gap: 6px; padding: 4px 10px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  background: var(--ui-subtle); font-size: 12px; color: var(--ui-text);
}
.stat-label { color: var(--ui-muted); }
.element-list { display: grid; gap: 12px; }
.element {
  padding: 10px 12px; border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-sm); background: var(--ui-surface);
}
.element.is-heading { border-inline-start: 3px solid var(--ui-accent); }
.element-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 6px; }
.element-loc { color: var(--ui-muted); font-family: var(--ui-font-mono); font-size: 11px; }
.element-path { color: var(--ui-muted); font-size: 11px; margin-bottom: 4px; overflow-wrap: anywhere; }
.element-heading { font-weight: 600; font-size: 15px; overflow-wrap: anywhere; }
.element-text { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ui-secondary); line-height: 1.7; }
.element-code {
  margin: 0; padding: 10px; background: var(--ui-subtle); border-radius: var(--ui-radius-sm);
  font-family: var(--ui-font-mono); font-size: 12px; overflow-x: auto; white-space: pre-wrap;
}
.element-table { margin-top: 4px; }
.table-more { margin-top: 4px; }
.load-more { display: flex; justify-content: center; padding: 16px 0; }
</style>
