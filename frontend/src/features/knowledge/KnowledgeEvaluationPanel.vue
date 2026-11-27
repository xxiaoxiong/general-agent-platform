<template>
  <div class="evaluation-grid">
    <section class="data-surface eval-panel">
      <h2>Retrieval Golden Dataset</h2>
      <p class="resource-meta eval-hint">
        每条用例包含问题与期望命中的文档（可选期望页码；无答案用例勾选「应拒答」）。
        指标为确定性计算：Recall@5/10、MRR@10、nDCG@10、Citation Precision、No-answer Accuracy 与延迟。
      </p>
      <div v-for="(item, index) in cases" :key="index" class="case-card">
        <div class="case-head">
          <span class="case-index">用例 {{ index + 1 }}</span>
          <el-checkbox v-model="item.no_answer">应拒答（无答案）</el-checkbox>
          <el-button text type="danger" size="small" :disabled="cases.length <= 1" @click="removeCase(index)">删除</el-button>
        </div>
        <el-input v-model="item.question" placeholder="问题（应与原文表达不同，覆盖同义词 / 编号 / 数值等）" />
        <el-select
          v-if="!item.no_answer" v-model="item.expected_document_ids" multiple filterable
          placeholder="期望命中的文档" class="w-full case-docs"
        >
          <el-option v-for="doc in docs" :key="doc.id" :label="doc.filename" :value="doc.id" />
        </el-select>
      </div>
      <div class="eval-actions">
        <el-button :icon="Plus" @click="addCase">添加用例</el-button>
        <el-select v-model="mode" class="eval-mode">
          <el-option label="混合检索" value="hybrid" />
          <el-option label="语义检索" value="dense" />
          <el-option label="关键词检索" value="bm25" />
        </el-select>
        <el-switch v-model="rerank" active-text="Reranker" />
        <el-button type="primary" :loading="running" :disabled="!canRun" @click="run">运行评测</el-button>
      </div>
    </section>

    <section class="data-surface eval-panel">
      <div class="eval-title">
        <h2>评测结果</h2>
        <el-button text :icon="RefreshCw" :loading="loading" @click="loadHistory">刷新</el-button>
      </div>
      <el-skeleton v-if="loading && !history.length" animated :rows="4" />
      <EmptyState v-else-if="!history.length" title="暂无评测" description="添加 Golden 用例并运行评测后，可对比不同检索模式 / Reranker 的质量。" :icon="Gauge" />
      <div v-for="evaluation in history" :key="evaluation.id" class="eval-result">
        <div class="eval-result-head">
          <strong>{{ evaluation.name || `评测 #${evaluation.id}` }}</strong>
          <el-tag size="small" :type="evaluation.status === 'completed' ? 'success' : 'danger'">{{ evaluation.status }}</el-tag>
          <span class="resource-meta">{{ modeLabel(evaluation.retrieval_mode) }}</span>
          <span class="resource-meta">{{ dayjs(evaluation.created_at).format('MM-DD HH:mm') }}</span>
        </div>
        <el-alert v-if="evaluation.error_msg" :title="evaluation.error_msg" type="error" :closable="false" show-icon />
        <div v-else-if="evaluation.metrics" class="metric-grid">
          <div v-for="metric in metricItems(evaluation)" :key="metric.key" class="metric-item">
            <div class="metric-label">{{ metric.label }}</div>
            <div class="metric-value">{{ metric.value }}</div>
          </div>
        </div>
        <el-collapse v-if="evaluation.details?.cases?.length" class="eval-details">
          <el-collapse-item title="逐条结果" :name="`detail-${evaluation.id}`">
            <div v-for="(entry, index) in evaluation.details?.cases || []" :key="index" class="eval-case">
              <div class="eval-case-question">{{ index + 1 }}. {{ entry.question }}</div>
              <div class="resource-meta">
                {{ entry.results?.length || 0 }} 条结果 · {{ entry.latency_ms }}ms
                <span v-if="entry.no_answer"> · 应拒答</span>
              </div>
              <ul class="eval-hit-list">
                <li v-for="hit in entry.results || []" :key="hit.chunk_id">
                  <el-tag size="small" :type="hit.relevant ? 'success' : 'info'">{{ hit.relevant ? '命中' : '未命中' }}</el-tag>
                  <span class="hit-name">{{ hit.filename }}</span>
                  <span v-if="hit.page_start" class="resource-meta">第 {{ hit.page_start }} 页</span>
                  <span class="resource-meta">score {{ hit.score }}</span>
                </li>
              </ul>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import { Gauge, Plus, RefreshCw } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { KnowledgeDocument, KnowledgeEvaluation } from '@/types'

interface CaseDraft {
  question: string
  expected_document_ids: number[]
  no_answer: boolean
}

const props = defineProps<{ kbId: number; docs: KnowledgeDocument[] }>()

const cases = ref<CaseDraft[]>([{ question: '', expected_document_ids: [], no_answer: false }])
const mode = ref<'hybrid' | 'dense' | 'bm25'>('hybrid')
const rerank = ref(false)
const running = ref(false)
const loading = ref(false)
const history = ref<KnowledgeEvaluation[]>([])

const canRun = computed(() => cases.value.some(item => item.question.trim()))

function modeLabel(value: string) {
  if (value === 'dense') return '语义检索'
  if (value === 'bm25') return '关键词检索'
  return '混合检索'
}

function addCase() {
  cases.value.push({ question: '', expected_document_ids: [], no_answer: false })
}

function removeCase(index: number) {
  cases.value.splice(index, 1)
}

function metricItems(evaluation: KnowledgeEvaluation) {
  const metrics = evaluation.metrics || {}
  const items = [
    { key: 'recall5', label: 'Recall@5', value: metrics.recall_at_5 },
    { key: 'recall10', label: 'Recall@10', value: metrics.recall_at_10 },
    { key: 'precision', label: 'Precision@K', value: metrics.precision_at_k },
    { key: 'mrr', label: 'MRR@10', value: metrics.mrr_at_10 },
    { key: 'ndcg', label: 'nDCG@10', value: metrics.ndcg_at_10 },
    { key: 'citation', label: 'Citation Precision', value: metrics.citation_precision },
    { key: 'noanswer', label: 'No-answer Accuracy', value: metrics.no_answer_accuracy },
    { key: 'p50', label: 'P50 (ms)', value: metrics.latency_p50_ms },
    { key: 'p95', label: 'P95 (ms)', value: metrics.latency_p95_ms },
  ]
  return items
    .filter(item => item.value != null)
    .map(item => ({ ...item, value: typeof item.value === 'number' ? Number(item.value.toFixed?.(4) ?? item.value) : item.value }))
}

async function loadHistory() {
  loading.value = true
  try {
    history.value = await knowledgeApi.listEvaluations(props.kbId, { limit: 20 })
  } catch {
    // 请求层已提示失败原因。
  } finally {
    loading.value = false
  }
}

async function run() {
  if (running.value || !canRun.value) return
  const payloadCases = cases.value
    .filter(item => item.question.trim())
    .map(item => ({
      question: item.question.trim(),
      expected_document_ids: item.no_answer ? [] : item.expected_document_ids,
      no_answer: item.no_answer,
    }))
  running.value = true
  try {
    const result = await knowledgeApi.createEvaluation(props.kbId, {
      name: `评测 ${dayjs().format('MM-DD HH:mm')}`,
      cases: payloadCases,
      mode: mode.value,
      rerank: rerank.value,
      top_k: 5,
    })
    if (result.status === 'completed') {
      ElMessage.success('评测完成')
    } else {
      ElMessage.error(result.error_msg || '评测失败')
    }
    await loadHistory()
  } catch {
    // 请求层已提示失败原因。
  } finally {
    running.value = false
  }
}

onMounted(loadHistory)
</script>

<style scoped>
.evaluation-grid { display: grid; grid-template-columns: minmax(320px, 460px) minmax(0, 1fr); gap: 16px; align-items: start; }
.eval-panel { padding: 20px; min-width: 0; }
.eval-panel h2 { font-size: 15px; margin: 0 0 8px; }
.eval-hint { margin-bottom: 12px; line-height: 1.6; }
.case-card { padding: 12px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); margin-bottom: 10px; }
.case-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.case-index { font-size: 12px; color: var(--ui-muted); }
.case-head :deep(.el-button) { margin-inline-start: auto; }
.case-docs { margin-top: 8px; }
.eval-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 8px; }
.eval-mode { width: 130px; }
.eval-title { display: flex; align-items: center; justify-content: space-between; }
.eval-result { padding: 14px 0; border-top: 1px solid var(--ui-border); }
.eval-result:first-of-type { border-top: 0; }
.eval-result-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.metric-item { padding: 8px 10px; background: var(--ui-subtle); border-radius: var(--ui-radius-sm); }
.metric-label { color: var(--ui-muted); font-size: 11px; margin-bottom: 4px; }
.metric-value { font-family: var(--ui-font-mono); font-size: 14px; font-weight: 600; }
.eval-details { margin-top: 10px; }
.eval-case { padding: 8px 0; border-top: 1px dashed var(--ui-border); }
.eval-case:first-child { border-top: 0; }
.eval-case-question { font-size: 13px; margin-bottom: 2px; overflow-wrap: anywhere; }
.eval-hit-list { list-style: none; padding: 0; margin: 6px 0 0; display: grid; gap: 4px; }
.eval-hit-list li { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.hit-name { font-size: 12px; overflow-wrap: anywhere; }
@media (max-width: 900px) { .evaluation-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
