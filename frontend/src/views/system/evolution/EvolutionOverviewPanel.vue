<template>
  <!-- ── 运行评估（概览 + 评估记录） ── -->
  <el-tab-pane label="运行评估" name="overview">
    <div class="toolbar">
      <el-input-number v-model="agentId" :min="1" aria-label="Agent ID" placeholder="Agent ID" style="width:140px;" />
      <label for="overview-window" class="hint">时间窗口（小时）</label>
      <el-input-number id="overview-window" v-model="windowHours" :min="1" :max="2160" style="width:130px;" />
      <el-button type="primary" :loading="sectionLoading" @click="filterOverview">查询</el-button>
      <el-button :loading="loading" @click="triggerEval">触发评估</el-button>
    </div>
    <div class="metric-cards" v-if="overview">
      <el-card shadow="never" v-for="card in metricCards" :key="card.label">
        <div class="metric-label">{{ card.label }}</div>
        <div class="metric-value">{{ card.value }}</div>
      </el-card>
    </div>
    <el-card shadow="never" v-if="overview?.by_version?.length">
      <h4>分版本对比（回归检测）</h4>
      <el-table :data="overview.by_version" size="small" stripe>
        <el-table-column prop="version_no" label="版本" width="70" />
        <el-table-column prop="sample_count" label="样本" width="70" />
        <el-table-column prop="success_rate" label="成功率" width="90" />
        <el-table-column prop="avg_overall" label="综合分" width="90" />
        <el-table-column prop="avg_steps" label="平均步数" width="90" />
        <el-table-column prop="avg_tokens" label="平均 Tokens" width="110" />
        <el-table-column prop="tool_error_rate" label="工具错误率" width="100" />
        <el-table-column prop="limit_exceeded_rate" label="超限率" width="90" />
      </el-table>
    </el-card>
    <el-card shadow="never" style="margin-top:12px;">
      <h4>评估记录（LLM 裁判）</h4>
      <el-table :data="evaluations" v-loading="sectionLoading || loading" empty-text="暂无运行评估记录" size="small" stripe>
        <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
        <el-table-column prop="agent_version_id" label="版本ID" width="80" />
        <el-table-column label="成功" width="70">
          <template #default="{ row }">
            <el-tag :type="row.success ? 'success' : 'danger'" size="small">
              {{ row.success ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="overall" label="综合分" width="80" />
        <el-table-column label="维度分（完成/质量/工具/效率/事实/格式）" min-width="220">
          <template #default="{ row }">{{ dimText(row.judge_scores) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatDate(row.evaluated_at) }}</template>
        </el-table-column>
        <el-table-column label="Trace" width="90">
          <template #default="{ row }">
            <el-button v-if="row.langfuse_trace_url" size="small" link type="primary"
              @click="openUrl(row.langfuse_trace_url)">Langfuse</el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top:12px;text-align:right;" v-if="evalTotal > evalPageSize">
        <el-pagination v-model:current-page="evalPage" :page-size="evalPageSize" :total="evalTotal"
          layout="total, prev, pager, next" @current-change="reloadSection" />
      </div>
    </el-card>
  </el-tab-pane>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import {
  evolutionApi,
  type EvaluationItem,
} from '@/api/evolution'
import { formatDate, pct } from '../evaluation/shared'

const props = defineProps<{
  agentId: number | undefined
  windowHours: number
  loading: boolean
  sectionLoading: boolean
}>()
const emit = defineEmits<{
  'update:agentId': [value: number | undefined]
  'update:windowHours': [value: number]
  'update:loading': [value: boolean]
  reload: []
}>()

// agentId / windowHours / loading 为外壳公共状态（v-model），数据状态全部自管
const agentId = computed({ get: () => props.agentId, set: v => emit('update:agentId', v) })
const windowHours = computed({ get: () => props.windowHours, set: v => emit('update:windowHours', v) })
const loading = computed({ get: () => props.loading, set: v => emit('update:loading', v) })

// ── 运行评估 ──
const overview = ref<any>(null)
const evaluations = ref<EvaluationItem[]>([])
const evalTotal = ref(0)
const evalPage = ref(1)
const evalPageSize = 20

const metricCards = computed(() => {
  const o = overview.value?.overall || {}
  return [
    { label: '样本数', value: o.sample_count ?? 0 },
    { label: '成功率', value: pct(o.success_rate) },
    { label: '综合分', value: o.avg_overall ?? '-' },
    { label: '平均步数', value: o.avg_steps ?? '-' },
    { label: '工具错误率', value: pct(o.tool_error_rate) },
    { label: '超限率', value: pct(o.limit_exceeded_rate) },
    { label: '平均耗时(s)', value: o.avg_duration_s ?? '-' },
    { label: 'HITL驳回', value: `${o.hitl_rejected ?? 0}/${o.hitl_total ?? 0}` },
  ]
})
function dimText(judge: any) {
  const d = judge?.dimensions
  if (!d) return '-'
  return [d.task_completion, d.answer_quality, d.tool_selection,
    d.efficiency, d.groundedness, d.format_compliance].map(v => v ?? '-').join(' / ')
}
function openUrl(url: string) { window.open(url, '_blank') }

// ── 加载 ──
async function loadOverview() {
  overview.value = await evolutionApi.overview({
    agent_id: agentId.value, hours: windowHours.value,
  })
}
async function loadEvaluations() {
  const res = await evolutionApi.listEvaluations({
    agent_id: agentId.value, page: evalPage.value, page_size: evalPageSize,
  })
  evaluations.value = res.items
  evalTotal.value = res.total
}
// 分区数据加载（运行评估分区 = 概览 + 评估记录），任一失败向上抛出
async function loadAll() {
  const results = await Promise.allSettled([loadOverview(), loadEvaluations()])
  if (results.some(result => result.status === 'rejected')) throw new Error('运行评估分区加载失败')
}
// 触发外壳的分区重载（保持原 loadSection 语义：错误提示 + 分页/查询共用）
function reloadSection() {
  emit('reload')
}
function filterOverview() {
  evalPage.value = 1
  reloadSection()
}
async function triggerEval() {
  if (!agentId.value) {
    ElMessage.warning('请先填写 Agent ID')
    return
  }
  loading.value = true
  try {
    const res = await evolutionApi.triggerEvaluations({ agent_id: agentId.value })
    ElMessage.success(`已评估 ${res.queued} 个 Run`)
    await loadEvaluations()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}

defineExpose({ loadAll })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.hint { color: var(--ui-muted); font-size: var(--ui-size-sm); }
.metric-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--ui-space-3); margin-bottom: var(--ui-space-4); }
.metric-label { color: var(--ui-muted); font-size: var(--ui-size-xs); }
.metric-value { font-size: var(--ui-size-xl); font-weight: var(--ui-weight-semibold); margin-top: 5px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
@media (max-width: 680px) {
  .metric-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
