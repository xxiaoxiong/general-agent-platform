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
          <el-form-item label="返回片段数">
            <el-input-number v-model="topK" :min="1" :max="20" />
          </el-form-item>
          <el-form-item label="最低相似度">
            <el-input-number v-model="threshold" :min="0" :max="1" :step="0.05" :precision="2" />
          </el-form-item>
        </div>
        <el-button type="primary" native-type="submit" :icon="Search" :loading="searching" :disabled="!canSearch">运行检索</el-button>
      </el-form>
    </section>

    <section class="data-surface retrieval-panel" aria-labelledby="results-heading">
      <div class="retrieval-title">
        <h2 id="results-heading">匹配片段</h2>
        <span v-if="completed && !searching" class="resource-meta">{{ results.length }} 条结果</span>
      </div>
      <div aria-live="polite">
        <el-skeleton v-if="searching" animated :rows="5" />
        <EmptyState v-else-if="failed" title="检索失败" description="请检查知识库状态或稍后重试。" :icon="CircleAlert">
          <el-button @click="search" :disabled="!canSearch">重试</el-button>
        </EmptyState>
        <EmptyState v-else-if="!results.length" :title="completed ? '未找到匹配片段' : '等待检索'"
          :description="completed ? '试试更换检索内容或降低最低相似度。' : '运行检索后可在此查看原文片段和相似度。'" :icon="Search" />
        <article v-for="(result, index) in results" v-show="!searching && !failed" :key="`${result.document_id}-${result.chunk_index}-${index}`" class="search-result">
          <header>
            <FileText :size="16" />
            <strong>{{ result.filename }}</strong>
            <el-tag type="success" size="small">{{ (result.score * 100).toFixed(1) }}%</el-tag>
          </header>
          <div class="resource-meta">片段 {{ result.chunk_index + 1 }}</div>
          <p>{{ result.content }}</p>
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
import type { KnowledgeBase } from '@/types'

interface Match {
  document_id: number
  chunk_index: number
  filename: string
  score: number
  content: string
}

const props = defineProps<{ baseId?: number }>()
const query = ref('')
const topK = ref(5)
const threshold = ref(0.3)
const bases = ref<KnowledgeBase[]>([])
const selectedIds = ref<number[]>([])
const results = ref<Match[]>([])
const loadingOptions = ref(false)
const searching = ref(false)
const completed = ref(false)
const failed = ref(false)

const baseIds = computed(() => props.baseId == null ? selectedIds.value : [props.baseId])
const canSearch = computed(() => !!query.value.trim() && !!baseIds.value.length)

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
  results.value = []
  try {
    const res = await knowledgeApi.search({
      query: query.value.trim(),
      knowledge_base_ids: baseIds.value,
      top_k: topK.value ?? 5,
      score_threshold: threshold.value ?? 0.3,
    })
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
  grid-template-columns: minmax(250px, 340px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.retrieval-panel { padding: 20px; min-width: 0; }
.retrieval-panel h2 { font-size: 15px; margin: 0 0 16px; }
.retrieval-title { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.retrieval-options { display: flex; flex-wrap: wrap; gap: 12px; }
.retrieval-options :deep(.el-input-number) { width: 116px; }
.search-result { padding: 16px 0; border-top: 1px solid var(--ui-border); }
.search-result header { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 4px; }
.search-result header svg { flex: none; color: var(--ui-muted); }
.search-result strong { overflow-wrap: anywhere; flex: 1; font-size: 14px; }
.search-result p { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ui-secondary); line-height: 1.7; margin-top: 8px; }
@media (max-width: 850px) { .retrieval-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
