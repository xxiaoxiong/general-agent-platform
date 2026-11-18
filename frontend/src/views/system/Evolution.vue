<template>
  <div class="evolution-page">
    <div class="page-header">
      <h2>Agent 自进化</h2>
      <div class="header-actions">
        <el-input-number v-model="agentId" :min="1" placeholder="Agent ID" style="width:140px;" />
        <el-button type="primary" :loading="loading" @click="refreshAll">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="evolution-tabs">
      <!-- ── 概览 ── -->
      <el-tab-pane label="指标概览" name="overview">
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
        <el-empty v-else description="暂无评估数据（先跑几个 Agent Run）" />
      </el-tab-pane>

      <!-- ── 评估记录 ── -->
      <el-tab-pane label="评估记录" name="evaluations">
        <div class="toolbar">
          <el-button type="primary" :loading="loading" @click="triggerEval">触发评估</el-button>
        </div>
        <el-table :data="evaluations" v-loading="loading" size="small" stripe>
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
          <el-table-column prop="status" label="状态" width="90" />
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
            layout="total, prev, pager, next" @current-change="loadEvaluations" />
        </div>
      </el-tab-pane>

      <!-- ── 进化报告 ── -->
      <el-tab-pane label="进化报告" name="reports">
        <div class="toolbar">
          <el-input-number v-model="windowHours" :min="1" :max="2160" style="width:140px;" />
          <span class="hint">小时窗口</span>
          <el-button type="primary" :loading="loading" @click="generateReport">生成优化报告</el-button>
        </div>
        <el-table :data="reports" v-loading="loading" size="small" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="agent_id" label="Agent" width="70" />
          <el-table-column label="窗口" min-width="200">
            <template #default="{ row }">
              {{ formatDate(row.window_start) }} ~ {{ formatDate(row.window_end) }}
            </template>
          </el-table-column>
          <el-table-column label="建议数" width="80">
            <template #default="{ row }">{{ row.suggestions?.length || 0 }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="showReport(row as EvolutionReport)">查看</el-button>
              <el-button size="small" link @click="copyMarkdown(row as EvolutionReport)">导出MD</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ── 变更审计 ── -->
      <el-tab-pane label="变更审计" name="changes">
        <el-table :data="changes" v-loading="loading" size="small" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="change_type" label="类型" width="90" />
          <el-table-column prop="target" label="目标" width="130" show-overflow-tooltip />
          <el-table-column label="变更（before → after）" min-width="220">
            <template #default="{ row }">{{ changeText(row as EvolutionChange) }}</template>
          </el-table-column>
          <el-table-column prop="applied_version_no" label="发布版本" width="90" />
          <el-table-column label="验证" width="130">
            <template #default="{ row }">
              <el-tag v-if="row.validation?.verdict" size="small"
                :type="row.validation.verdict === 'improved' ? 'success' : row.validation.verdict === 'regressed' ? 'danger' : 'info'">
                {{ verdictText(row.validation.verdict) }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="90" />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button v-if="row.status === 'applied' || row.status === 'validated'" size="small"
                link type="danger" @click="rollback(row as EvolutionChange)">回滚</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ── 开发简报（客观数据） ── -->
      <el-tab-pane label="开发简报" name="briefing">
        <div class="toolbar">
          <el-input-number v-model="windowHours" :min="1" :max="2160" style="width:140px;" />
          <span class="hint">小时窗口</span>
          <el-button type="primary" :loading="loading" @click="loadBriefing">生成简报</el-button>
          <el-button :disabled="!briefing" @click="copyBriefingMd">复制 Markdown</el-button>
          <el-button :disabled="!briefing" @click="downloadBriefingMd">下载 .md</el-button>
          <span class="hint">仅客观数据，不含 AI 主观建议，可直接喂给开发 AI</span>
        </div>
        <template v-if="briefing">
          <div class="metric-cards">
            <el-card shadow="never">
              <div class="metric-label">Run 总数</div>
              <div class="metric-value">{{ briefing.runs?.total ?? 0 }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">成功率</div>
              <div class="metric-value">{{ pct(briefing.runs?.success_rate) }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">工具错误率</div>
              <div class="metric-value">{{ pct(briefing.tools?.error_rate) }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">HITL 驳回</div>
              <div class="metric-value">{{ briefing.hitl?.by_decision?.reject ?? 0 }}/{{ briefing.hitl?.total ?? 0 }}</div>
            </el-card>
          </div>

          <el-card shadow="never" v-if="briefing.tools?.per_tool?.length">
            <h4>工具调用统计</h4>
            <el-table :data="briefing.tools.per_tool" size="small" stripe>
              <el-table-column prop="tool_name" label="工具" width="140" />
              <el-table-column prop="calls" label="调用" width="80" />
              <el-table-column prop="failed" label="失败" width="80" />
              <el-table-column prop="retried" label="重试" width="80" />
              <el-table-column prop="repeat_calls" label="重复调用" width="90" />
              <el-table-column prop="error_rate" label="错误率" width="90" />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.failed_runs?.length">
            <h4>失败 Run 记录</h4>
            <el-table :data="briefing.failed_runs" size="small" stripe>
              <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
              <el-table-column prop="agent_id" label="Agent" width="70" />
              <el-table-column prop="status" label="状态" width="110" />
              <el-table-column prop="error" label="错误" min-width="220" show-overflow-tooltip />
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.by_version?.length">
            <h4>分版本趋势</h4>
            <el-table :data="briefing.by_version" size="small" stripe>
              <el-table-column label="版本" width="90">
                <template #default="{ row }">{{ row.version_no ?? '(无版本)' }}</template>
              </el-table-column>
              <el-table-column prop="runs" label="Run 数" width="80" />
              <el-table-column prop="completed" label="完成" width="70" />
              <el-table-column prop="failed" label="失败" width="70" />
              <el-table-column prop="tokens_avg" label="平均 Tokens" width="110" />
              <el-table-column prop="duration_avg_s" label="平均耗时(s)" width="110" />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.hitl?.rejected_records?.length">
            <h4>HITL 驳回记录</h4>
            <el-table :data="briefing.hitl.rejected_records" size="small" stripe>
              <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
              <el-table-column prop="tool_name" label="工具" width="110" />
              <el-table-column prop="decision" label="决策" width="80" />
              <el-table-column prop="action_summary" label="动作摘要" min-width="180" show-overflow-tooltip />
              <el-table-column prop="message" label="备注" min-width="150" show-overflow-tooltip />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.evaluations?.count">
            <h4>评估数据（LLM 裁判记录）</h4>
            <div class="hint" style="margin-bottom:8px;">
              样本 {{ briefing.evaluations.count }} | 平均综合分 {{ briefing.evaluations.avg_overall ?? '-' }}
              | 裁决：{{ verdictDist }}
            </div>
            <el-table :data="dimRows" size="small" stripe>
              <el-table-column prop="dim" label="维度" width="200" />
              <el-table-column prop="avg" label="平均分" width="100" />
            </el-table>
          </el-card>
        </template>
        <el-empty v-else description="点击「生成简报」汇总窗口内的客观执行数据" />
      </el-tab-pane>
    </el-tabs>

    <!-- 报告详情 -->
    <el-dialog v-model="reportVisible" title="优化报告" width="760px" top="6vh">
      <div v-if="currentReport">
        <pre class="report-summary">{{ currentReport.summary_text }}</pre>
        <h4>建议（可一键应用）</h4>
        <el-table :data="currentReport.suggestions" size="small" stripe>
          <el-table-column prop="type" label="类型" width="80" />
          <el-table-column prop="target" label="目标" width="120" show-overflow-tooltip />
          <el-table-column prop="rationale" label="依据" min-width="180" show-overflow-tooltip />
          <el-table-column label="预期" width="110">
            <template #default="{ row }">{{ JSON.stringify(row.expected_impact || {}) }}</template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="70" />
          <el-table-column label="操作" width="90">
            <template #default="{ row, $index }">
              <el-button v-if="row.auto_applicable" size="small" type="primary"
                :loading="loading" @click="applySuggestion($index)">应用</el-button>
              <el-tag v-else size="small" type="info">人工实施</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import dayjs from 'dayjs'
import {
  evolutionApi,
  type EvolutionReport, type EvolutionChange,
  type EvaluationItem, type DevBriefing,
} from '@/api/evolution'

const tab = ref('overview')
const loading = ref(false)
const agentId = ref<number | undefined>(undefined)
const windowHours = ref(168)

const overview = ref<any>(null)
const evaluations = ref<EvaluationItem[]>([])
const evalTotal = ref(0)
const evalPage = ref(1)
const evalPageSize = 20
const reports = ref<EvolutionReport[]>([])
const changes = ref<EvolutionChange[]>([])
const briefing = ref<DevBriefing | null>(null)

const reportVisible = ref(false)
const currentReport = ref<EvolutionReport | null>(null)

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

function pct(v: any) { return v == null ? '-' : `${(v * 100).toFixed(1)}%` }

const dimRows = computed(() => {
  const dims = briefing.value?.evaluations?.avg_dimensions || {}
  return Object.entries(dims).map(([dim, avg]) => ({ dim, avg: avg ?? '-' }))
})
const verdictDist = computed(() => {
  const v = briefing.value?.evaluations?.verdicts || {}
  const text = Object.entries(v).map(([k, n]) => `${k}=${n}`).join(', ')
  return text || '-'
})
function formatDate(d: string | null) { return d ? dayjs(d).format('YYYY-MM-DD HH:mm') : '-' }
function openUrl(url: string) { window.open(url, '_blank') }
function verdictText(v: string) {
  return v === 'improved' ? '已验证·改善' : v === 'regressed' ? '已验证·回退(建议回滚)' : '已验证·持平'
}
function dimText(judge: any) {
  const d = judge?.dimensions
  if (!d) return '-'
  return [d.task_completion, d.answer_quality, d.tool_selection,
    d.efficiency, d.groundedness, d.format_compliance].map(v => v ?? '-').join(' / ')
}
function changeText(row: EvolutionChange) {
  const b = JSON.stringify(row.before?.value ?? row.before)
  const a = JSON.stringify(row.after?.value ?? row.after)
  return `${b.length > 40 ? b.slice(0, 40) + '…' : b} → ${a.length > 40 ? a.slice(0, 40) + '…' : a}`
}

async function refreshAll() {
  loading.value = true
  try {
    await Promise.all([loadOverview(), loadEvaluations(), loadReports(), loadChanges()])
  } finally {
    loading.value = false
  }
}
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
async function loadReports() {
  const res = await evolutionApi.listReports({ agent_id: agentId.value })
  reports.value = res.items
}
async function loadChanges() {
  const res = await evolutionApi.listChanges({ agent_id: agentId.value })
  changes.value = res.items
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
  } finally {
    loading.value = false
  }
}

async function generateReport() {
  if (!agentId.value) {
    ElMessage.warning('请先填写 Agent ID')
    return
  }
  loading.value = true
  try {
    const report = await evolutionApi.generateReport({
      agent_id: agentId.value, window_hours: windowHours.value,
    })
    ElMessage.success('报告已生成')
    await loadReports()
    showReport(report)
  } finally {
    loading.value = false
  }
}

function showReport(report: EvolutionReport) {
  currentReport.value = report
  reportVisible.value = true
}

async function applySuggestion(index: number) {
  if (!currentReport.value) return
  const s = currentReport.value.suggestions[index]
  await ElMessageBox.confirm(
    `确认应用建议 [${s.type}/${s.target}]？将修改 Agent 配置并发布新版本。`,
    '一键应用', { type: 'warning' })
  loading.value = true
  try {
    const res = await evolutionApi.applySuggestion(currentReport.value.id, index)
    ElMessage.success(`已应用并发布 v${res.new_version_no}（新 Run 自动生效）`)
    await loadChanges()
  } finally {
    loading.value = false
  }
}

async function rollback(row: EvolutionChange) {
  await ElMessageBox.confirm(
    `确认回滚变更 #${row.id}（${row.target}）？将还原旧值并发布新版本。`,
    '回滚', { type: 'warning' })
  loading.value = true
  try {
    const res = await evolutionApi.rollbackChange(row.id)
    ElMessage.success(`已回滚并发布 v${res.restored_version_no}`)
    await loadChanges()
  } finally {
    loading.value = false
  }
}

async function copyMarkdown(row: EvolutionReport) {
  const res = await evolutionApi.exportMarkdown(row.id)
  await navigator.clipboard.writeText(res.markdown)
  ElMessage.success('报告 Markdown 已复制到剪贴板')
}

async function loadBriefing() {
  loading.value = true
  try {
    briefing.value = await evolutionApi.getBriefing({
      agent_id: agentId.value, hours: windowHours.value,
    })
    ElMessage.success('简报已生成')
  } finally {
    loading.value = false
  }
}

async function copyBriefingMd() {
  const res = await evolutionApi.getBriefingMarkdown({
    agent_id: agentId.value, hours: windowHours.value,
  })
  await navigator.clipboard.writeText(res.markdown)
  ElMessage.success('简报 Markdown 已复制到剪贴板')
}

async function downloadBriefingMd() {
  const res = await evolutionApi.getBriefingMarkdown({
    agent_id: agentId.value, hours: windowHours.value,
  })
  const blob = new Blob([res.markdown], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `dev-briefing-${dayjs().format('YYYYMMDD-HHmm')}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

onMounted(refreshAll)
</script>

<style scoped>
.evolution-page {
  padding: 24px;
  background: #fff;
  height: calc(100vh - 56px);
  box-sizing: border-box;
  overflow-y: auto;
}
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.header-actions { display: flex; gap: 10px; align-items: center; }
.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
.hint { color: #909399; font-size: 13px; }
.metric-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.metric-label { color: #909399; font-size: 12px; }
.metric-value { font-size: 22px; font-weight: 700; color: #0d0d0d; margin-top: 4px; }
.report-summary {
  background: #f7f8fa;
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: inherit;
}
</style>
