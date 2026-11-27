<template>
  <div class="retrieval-grid">
    <section class="data-surface retrieval-panel" aria-labelledby="query-heading">
      <h2 id="query-heading">检索设置</h2>
      <el-form label-position="top" @submit.prevent="search">
        <el-form-item v-if="baseId == null" label="知识库" required>
          <el-select v-model="selectedIds" multiple filterable placeholder="选择知识库" class="w-full" :loading="loadingOptions">
            <el-option v-for="kb in bases" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="检索内容" required>
          <el-input v-model="query" type="textarea" :rows="4" maxlength="4000" placeholder="输入问题或关键词" />
        </el-form-item>
        <div class="retrieval-options">
          <el-form-item label="检索模式">
            <el-select v-model="mode" class="mode-select">
              <el-option label="混合（Dense + BM25）" value="hybrid" />
              <el-option label="语义（Dense）" value="dense" />
              <el-option label="关键词（BM25）" value="bm25" />
            </el-select>
          </el-form-item>
          <el-form-item label="返回片段数">
            <el-input-number v-model="topK" :min="1" :max="20" />
          </el-form-item>
          <el-form-item label="Dense 绝对阈值">
            <el-input-number v-model="threshold" :min="0" :max="1" :step="0.05" :precision="2" />
          </el-form-item>
        </div>
        <el-form-item>
          <el-switch v-model="rerank" active-text="启用 Reranker" />
          <span class="resource-meta rerank-hint">开启后使用 CrossEncoder 对候选二次排序（首次加载较慢）</span>
        </el-form-item>
        <el-button type="primary" native-type="submit" :icon="Search" :loading="searching" :disabled="!canSearch">运行检索</el-button>
      </el-form>
    </section>

    <section class="data-surface retrieval-panel" aria-labelledby="results-heading">
      <div class="retrieval-title">
        <h2 id="results-heading">匹配片段</h2>
        <span v-if="completed && !searching" class="resource-meta">{{ results.length }} 条结果</span>
      </div>
      <div v-if="response" class="retrieval-diagnostics">
        <el-tag size="small" type="info">{{ modeLabel(response.mode) }}</el-tag>
        <span v-for="(value, key) in timings" :key="key" class="diagnostic-item">{{ key }} {{ value }}ms</span>
        <span class="diagnostic-item">候选 {{ response.candidates.unique ?? 0 }} / 返回 {{ response.total }}</span>
      </div>
      <el-alert v-for="note in response?.notes || []" :key="note" :title="note" type="warning" :closable="false" show-icon class="retrieval-note" />
      <div aria-live="polite">
        <el-skeleton v-if="searching" animated :rows="5" />
        <EmptyState v-else-if="failed" title="检索失败" description="请检查知识库状态或稍后重试。" :icon="CircleAlert">
          <el-button @click="search" :disabled="!canSearch">重试</el-button>
        </EmptyState>
        <EmptyState v-else-if="!results.length" :title="completed ? '没有可靠证据' : '等待检索'"
          :description="completed ? '知识库未找到足够相关的证据（已按阈值拒答），可调整问法或降低阈值后再试。' : '运行检索后可在此查看原文片段、来源位置与排序信号。'" :icon="Search" />
        <article v-for="(result, index) in results" v-show="!searching && !failed" :key="result.chunk_id || `${result.document_id}-${result.chunk_index}-${index}`" class="search-result">
          <header>
            <FileText :size="16" />
            <strong>{{ citationTitle(result) }}</strong>
            <el-tag size="small" type="info">#{{ index + 1 }}</el-tag>
          </header>
          <div class="result-meta">
            <span class="resource-meta">{{ locationText(result) }}</span>
            <span v-if="result.citation?.heading_path?.length" class="resource-meta">· {{ result.citation.heading_path.join(' / ') }}</span>
          </div>
          <div class="signal-row">
            <span class="signal">融合 {{ formatScore(result.signals?.rrf) }}</span>
            <span v-if="result.signals?.dense != null" class="signal">Dense {{ formatScore(result.signals.dense) }}</span>
            <span v-if="result.signals?.bm25 != null" class="signal">BM25 {{ formatScore(result.signals.bm25) }}</span>
            <span v-if="result.signals?.rerank != null" class="signal">Rerank {{ formatScore(result.signals.rerank) }}</span>
          </div>
          <p>{{ result.context || result.content }}</p>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CircleAlert, FileText, Search } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { KnowledgeBase, KnowledgeSearchResponse, KnowledgeSearchResult } from '@/types'

const props = defineProps<{ baseId?: number }>()
const query = ref('')
const mode = ref<'hybrid' | 'dense' | 'bm25'>('hybrid')
const topK = ref(5)
const threshold = ref(0.35)
const rerank = ref(false)
const bases = ref<KnowledgeBase[]>([])
const selectedIds = ref<number[]>([])
const results = ref<KnowledgeSearchResult[]>([])
const response = ref<KnowledgeSearchResponse | null>(null)
const loadingOptions = ref(false)
const searching = ref(false)
const completed = ref(false)
const failed = ref(false)

const baseIds = computed(() => props.baseId == null ? selectedIds.value : [props.baseId])
const canSearch = computed(() => !!query.value.trim() && !!baseIds.value.length)
const timings = computed(() => {
  const entries = Object.entries(response.value?.timings || {})
  return Object.fromEntries(entries.map(([key, value]) => [key.replace(/_ms$/, ''), Number(value).toFixed(0)]))
})

function modeLabel(value: string) {
  if (value === 'dense') return '语义检索'
  if (value === 'bm25') return '关键词检索'
  if (value === 'legacy') return '旧索引回退'
  return '混合检索'
}

function formatScore(value?: number | null) {
  return value == null ? '-' : Number(value).toFixed(4)
}

function citationTitle(result: KnowledgeSearchResult) {
  return result.citation?.filename || result.filename || '未知来源'
}

function locationText(result: KnowledgeSearchResult) {
  const c = result.citation
  if (!c) return `片段 ${result.chunk_index + 1}`
  const parts: string[] = []
  if (c.page_start) {
    parts.push(c.page_end && c.page_end !== c.page_start
      ? `第 ${c.page_start}-${c.page_end} 页` : `第 ${c.page_start} 页`)
  }
  if (c.sheet_name) {
    parts.push(`Sheet「${c.sheet_name}」${c.row_start ? ` 第 ${c.row_start}-${c.row_end ?? c.row_start} 行` : ''}`)
  }
  if (c.slide_number) parts.push(`Slide ${c.slide_number}`)
  if (!parts.length) parts.push(`片段 ${result.chunk_index + 1}`)
  return parts.join(' · ')
}

async function loadOptions() {
  if (props.baseId != null) return
  loadingOptions.value = true
  try {
    const first = await knowledgeApi.listBases({ page_size: 100 })
    bases.value = first.items
    for (let page = 2; page <= first.total_pages; page++) {
      const next = await knowledgeApi.listBases({ page, page_size: 100 })
      bases.value.push(...next.items)
    }
  } catch {
    // 请求层已提示失败原因。
  } finally {
    loadingOptions.value = false
  }
}

async function search() {
  if (!canSearch.value || searching.value) return
  searching.value = true
  failed.value = false
  completed.value = false
  results.value = []
  response.value = null
  try {
    const res = await knowledgeApi.search({
      query: query.value.trim(),
      knowledge_base_ids: baseIds.value,
      top_k: topK.value ?? 5,
      mode: mode.value,
      rerank: rerank.value,
      score_threshold: threshold.value ?? undefined,
    })
    response.value = res
    results.value = res.results
    completed.value = true
  } catch {
    failed.value = true
  } finally {
    searching.value = false
  }
}

onMounted(loadOptions)
</script>

<style scoped>
.retrieval-grid {
  display: grid;
  grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.retrieval-panel { padding: 20px; min-width: 0; }
.retrieval-panel h2 { font-size: 15px; margin: 0 0 16px; }
.retrieval-title { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.retrieval-options { display: flex; flex-wrap: wrap; gap: 12px; }
.retrieval-options :deep(.el-input-number) { width: 116px; }
.mode-select { width: 215px; }
.rerank-hint { margin-inline-start: 10px; }
.retrieval-diagnostics { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 8px 0 4px; }
.diagnostic-item { color: var(--ui-muted); font-family: var(--ui-font-mono); font-size: 11px; }
.retrieval-note { margin-bottom: 8px; }
.search-result { padding: 16px 0; border-top: 1px solid var(--ui-border); }
.search-result header { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 4px; }
.search-result header svg { flex: none; color: var(--ui-muted); }
.search-result strong { overflow-wrap: anywhere; flex: 1; font-size: 14px; }
.result-meta { display: flex; flex-wrap: wrap; gap: 4px; align-items: baseline; }
.signal-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0; }
.signal { color: var(--ui-muted); font-family: var(--ui-font-mono); font-size: 11px; }
.search-result p { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ui-secondary); line-height: 1.7; margin-top: 4px; }
@media (max-width: 850px) { .retrieval-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
