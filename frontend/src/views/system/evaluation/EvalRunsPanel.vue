<template>
  <!-- ── 测评任务 ── -->
  <el-tab-pane label="测评任务" name="runs">
    <div v-if="sectionLoading || loading || runs.length" class="data-surface">
    <el-table :data="runs" v-loading="sectionLoading || loading" size="small" empty-text="暂无测评任务">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
      <el-table-column label="数据集" width="170" show-overflow-tooltip>
        <template #default="{ row }">{{ datasetName(row.dataset_id) }}</template>
      </el-table-column>
      <el-table-column label="Agent" width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ agentName(row.agent_id) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="runStatusType(row.status)" size="small">{{ runStatusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" width="150">
        <template #default="{ row }">
          <el-progress :percentage="runProgress(row as EvalRun)" :stroke-width="10"
            :status="row.status === 'failed' ? 'exception' : undefined" />
          <span class="progress-text">{{ row.aggregate?.completed ?? 0 }}/{{ row.aggregate?.total ?? 0 }}</span>
        </template>
      </el-table-column>
      <el-table-column label="通过率" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.aggregate?.pass_rate != null" size="small"
            :type="row.aggregate.pass_rate >= 0.8 ? 'success' : row.aggregate.pass_rate >= 0.5 ? 'warning' : 'danger'">
            {{ pct(row.aggregate.pass_rate) }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="平均分" width="80">
        <template #default="{ row }">{{ row.aggregate?.avg_score ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="155">
        <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="230">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openResult(row as EvalRun)">结果</el-button>
          <el-button size="small" link @click="exportMd(row as EvalRun)">导出MD</el-button>
          <el-button v-if="['pending', 'running', 'cancel_requested'].includes(row.status)"
            size="small" link type="danger" @click="cancelRun(row as EvalRun)">取消</el-button>
          <el-button v-if="['cancelled', 'failed', 'completed'].includes(row.status)"
            size="small" link type="warning" @click="resumeRun(row as EvalRun)">重跑失败项</el-button>
        </template>
      </el-table-column>
    </el-table>
    </div>
    <div v-else-if="!sectionError" class="data-surface">
      <EmptyState :icon="ClipboardList" title="还没有测评任务" description="选择一个数据集和 Agent，创建任务以评估结果。">
        <UiButton @click="tab = 'datasets'">查看数据集</UiButton>
      </EmptyState>
    </div>
  </el-tab-pane>

  <!-- ── 测评结果 ── -->
  <el-tab-pane label="测评结果" name="results">
    <template v-if="resultRun">
      <div class="toolbar">
        <span class="hint">任务：{{ resultRun.name }}（{{ datasetName(resultRun.dataset_id) }} × {{ agentName(resultRun.agent_id) }}）</span>
        <el-button size="small" :loading="loading" @click="loadResultItems">刷新</el-button>
        <el-button size="small" @click="exportMd(resultRun)">导出 Markdown</el-button>
      </div>
      <div class="metric-cards">
        <el-card shadow="never">
          <div class="metric-label">通过率</div>
          <div class="metric-value">{{ pct(resultRun.aggregate?.pass_rate) }}</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">通过 / 完成</div>
          <div class="metric-value">{{ resultRun.aggregate?.passed ?? 0 }} / {{ resultRun.aggregate?.completed ?? 0 }}</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">平均分</div>
          <div class="metric-value">{{ resultRun.aggregate?.avg_score ?? '-' }}</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">总 Tokens</div>
          <div class="metric-value">{{ resultRun.aggregate?.total_tokens ?? 0 }}</div>
        </el-card>
      </div>
      <!-- 能力画像（纯客观数据：分层表现 + 工具使用 + 问题用例） -->
      <div class="cap-grid">
        <el-card shadow="never">
          <h4>能力画像 · 按复杂度</h4>
          <div v-for="r in capRows" :key="r.lvl" class="cap-row">
            <span class="cap-name">{{ r.text }}</span>
            <el-progress :percentage="pctNum(r.pass_rate)" :stroke-width="10" class="cap-bar" />
            <span class="cap-detail">{{ r.passed }}/{{ r.total }} · 均分 {{ r.avg_score ?? '—' }} · 均 {{ r.avg_steps ?? '—' }} 步</span>
          </div>
          <div v-if="!capRows.length" class="hint">暂无数据</div>
          <h4 style="margin-top:12px;">按任务类型</h4>
          <div v-for="r in taskTypeRows" :key="r.tt" class="cap-row">
            <span class="cap-name">{{ r.text }}</span>
            <el-progress :percentage="pctNum(r.pass_rate)" :stroke-width="10" class="cap-bar" />
            <span class="cap-detail">{{ r.passed }}/{{ r.total }} · 均分 {{ r.avg_score ?? '—' }} · 均 {{ r.avg_steps ?? '—' }} 步</span>
          </div>
          <div v-if="!taskTypeRows.length" class="hint">暂无数据</div>
        </el-card>
        <el-card shadow="never">
          <h4>能力事实（客观）</h4>
          <ul class="cap-facts">
            <li>平均步数：{{ capFacts.avg_steps ?? '—' }}（最大 {{ capFacts.max_steps ?? 0 }}）</li>
            <li>多步执行用例（步数≥2）：{{ capFacts.multi_step_count ?? 0 }}</li>
            <li>全程未用工具用例：{{ capFacts.zero_tool_count ?? 0 }}</li>
            <li>工具错误率：{{ pct(capFacts.tool_error_rate) }}</li>
            <li>未调用必需工具用例：{{ capFacts.missing_required_tools_count ?? 0 }}</li>
            <li>超出执行上限用例：{{ capFacts.limit_exceeded_count ?? 0 }}</li>
            <li>平均输出字数：{{ capFacts.avg_output_chars ?? '—' }}（最大 {{ capFacts.max_output_chars ?? 0 }}）</li>
            <li>客观约束达成率：{{ pct(capFacts.constraint_ratio) }}
              （{{ capFacts.constraint_passed ?? 0 }}/{{ capFacts.constraint_total ?? 0 }}，
              覆盖 {{ capFacts.constraint_items ?? 0 }} 条用例）</li>
          </ul>
          <h4 style="margin-top:12px;">工具使用统计</h4>
          <el-table v-if="toolUsageRows.length" :data="toolUsageRows" size="small" stripe max-height="200">
            <el-table-column prop="name" label="工具" min-width="120" />
            <el-table-column prop="calls" label="调用" width="70" />
            <el-table-column prop="failed" label="失败" width="70" />
          </el-table>
          <div v-else class="hint">暂无工具调用</div>
        </el-card>
      </div>
      <el-card v-if="problemRows.length" shadow="never" class="problem-card">
        <h4>问题用例清单（客观事实，供开发分析）</h4>
        <el-table :data="problemRows" size="small" stripe max-height="260">
          <el-table-column prop="position" label="#" width="50" />
          <el-table-column label="复杂度" width="70">
            <template #default="{ row }">{{ row.complexity }}</template>
          </el-table-column>
          <el-table-column label="任务类型" width="92">
            <template #default="{ row }">{{ taskTypeText[row.task_type] || row.task_type }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">{{ itemStatusText(row.status) }}</template>
          </el-table-column>
          <el-table-column label="问题" min-width="180">
            <template #default="{ row }">
              <el-tag v-for="p in row.problems" :key="p" size="small" type="danger"
                style="margin-right:4px;">{{ problemText[p] || p }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="步数/工具" width="100">
            <template #default="{ row }">{{ row.steps ?? '-' }} / {{ row.tool_calls ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="未用工具" min-width="120">
            <template #default="{ row }">{{ (row.missing_tools || []).join('、') || '-' }}</template>
          </el-table-column>
        </el-table>
      </el-card>
      <el-table :data="resultItems" v-loading="loading" size="small" stripe
        @row-click="(row: EvalRunItemBrief) => openItem(row)">
        <el-table-column prop="position" label="#" width="50">
          <template #default="{ row }">{{ row.position + 1 }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="itemStatusType(row.status)" size="small">{{ itemStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="80">
          <template #default="{ row }">{{ row.score ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="复杂度" width="80">
          <template #default="{ row }">
            <el-tag :type="complexityTagType(row.complexity)" size="small">{{ row.complexity }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="任务类型" width="92">
          <template #default="{ row }">{{ taskTypeText[row.task_type] || row.task_type }}</template>
        </el-table-column>
        <el-table-column label="步数" width="64">
          <template #default="{ row }">{{ row.steps ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="工具" width="110">
          <template #default="{ row }">
            <el-tooltip v-if="row.used_tools?.length" :content="row.used_tools.join('、')" placement="top">
              <span>{{ row.tool_calls ?? 0 }} 次</span>
            </el-tooltip>
            <span v-else>-</span>
            <el-tag v-if="row.missing_tools?.length" type="danger" size="small" style="margin-left:4px;">缺工具</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="输出字数" width="86">
          <template #default="{ row }">{{ row.output_chars ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="input_preview" label="输入" min-width="200" show-overflow-tooltip />
        <el-table-column prop="output_preview" label="实际输出" min-width="200" show-overflow-tooltip />
        <el-table-column prop="run_id" label="Run" width="140" show-overflow-tooltip />
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openItem(row as EvalRunItemBrief)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>
    <el-empty v-else description="先在「测评任务」中点击「结果」查看某个测评的逐用例明细" />
  </el-tab-pane>

  <!-- 新建测评 -->
  <el-dialog v-model="createRunVisible" title="新建测评" width="560px">
    <el-form :model="runForm" label-width="110px">
      <el-form-item label="任务名称">
        <el-input v-model="runForm.name" placeholder="留空自动生成" />
      </el-form-item>
      <el-form-item label="数据集" required>
        <el-select v-model="runForm.dataset_id" placeholder="选择测评数据集" style="width:100%;">
          <el-option v-for="d in datasets" :key="d.id"
            :label="`${d.name}（${d.item_count ?? 0} 条）`" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="Agent" required>
        <el-select v-model="runForm.agent_id" placeholder="选择被测 Agent" style="width:100%;">
          <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="审批模式">
        <el-radio-group v-model="runForm.approval_mode">
          <el-radio value="auto">自动（推荐，测评无需人工审批）</el-radio>
          <el-radio value="manual">手动</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="启用记忆">
        <el-switch v-model="runForm.use_memory" />
        <span class="hint" style="margin-left:8px;">默认关闭，保证测评可复现</span>
      </el-form-item>
      <el-form-item label="单用例超时">
        <el-input-number v-model="runForm.timeout_s" :min="10" :max="3600" />
        <span class="hint" style="margin-left:8px;">秒</span>
      </el-form-item>
      <el-form-item label="用例上限">
        <el-input-number v-model="runForm.limit" :min="0" :max="500" />
        <span class="hint" style="margin-left:8px;">0 = 全部用例</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createRunVisible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="submitRun">创建并开始</el-button>
    </template>
  </el-dialog>

  <!-- 用例结果详情 -->
  <EvalItemDialog ref="itemDialog" />
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ClipboardList } from '@lucide/vue'
import UiButton from '@/components/ui/UiButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import dayjs from 'dayjs'
import {
  evaluationApi,
  type EvalDataset, type EvalRun, type EvalRunItemBrief,
} from '@/api/evaluation'
import { agentOptionApi } from '@/api/assistants'
import {
  formatDate, pct, pctNum, complexityText, complexityTagType, taskTypeText,
  runStatusType, runStatusText, itemStatusType, itemStatusText, confirmAction,
} from './shared'
import EvalItemDialog from './EvalItemDialog.vue'

const props = defineProps<{
  tab: string
  loading: boolean
  sectionLoading: boolean
  sectionError: boolean
}>()
const emit = defineEmits<{
  'update:tab': [value: string]
  'update:loading': [value: boolean]
}>()

// tab / loading 为外壳公共状态（v-model），数据状态全部自管
const tab = computed({ get: () => props.tab, set: v => emit('update:tab', v) })
const loading = computed({ get: () => props.loading, set: v => emit('update:loading', v) })

// ── 测评任务 / 结果 ──
const runs = ref<EvalRun[]>([])
const datasets = ref<EvalDataset[]>([])
const agents = ref<Array<{ id: number; name: string }>>([])
const createRunVisible = ref(false)
const runForm = ref({
  name: '', dataset_id: undefined as number | undefined,
  agent_id: undefined as number | undefined,
  approval_mode: 'auto', use_memory: false, timeout_s: 180, limit: 0,
})

const resultRun = ref<EvalRun | null>(null)
const resultItems = ref<EvalRunItemBrief[]>([])
const itemDialog = ref<InstanceType<typeof EvalItemDialog> | null>(null)

// ── 工具函数 ──
const problemText: Record<string, string> = {
  error: '执行异常', failed: '未通过', missing_required_tools: '未用必需工具',
  below_expected_steps: '未达期望步数',
}
// 能力画像（纯客观，直接消费 aggregate）
const capRows = computed(() => Object.entries(resultRun.value?.aggregate?.by_complexity || {})
  .map(([lvl, s]) => ({ lvl, text: complexityText[lvl] || lvl, ...s })))
const taskTypeRows = computed(() => Object.entries(resultRun.value?.aggregate?.by_task_type || {})
  .map(([tt, s]) => ({ tt, text: taskTypeText[tt] || tt, ...s })))
const toolUsageRows = computed(() => Object.entries(resultRun.value?.aggregate?.tool_usage || {})
  .map(([name, u]) => ({ name, ...u })))
const capFacts = computed(() => resultRun.value?.aggregate?.capability || {})
const problemRows = computed(() => resultRun.value?.aggregate?.problem_items || [])
function datasetName(id: number) { return datasets.value.find(d => d.id === id)?.name || `#${id}` }
function agentName(id: number) { return agents.value.find(a => a.id === id)?.name || `#${id}` }
function runProgress(row: EvalRun) {
  const total = row.aggregate?.total || 0
  const done = row.aggregate?.completed ?? 0
  return total ? Math.round((done / total) * 100) : 0
}

// ── 加载 ──
async function loadRuns() {
  runs.value = (await evaluationApi.listRuns()).items
  schedulePolling()
}
async function loadDatasets() {
  datasets.value = (await evaluationApi.listDatasets()).items
}
async function loadAgents() {
  try { agents.value = await agentOptionApi.list() } catch { agents.value = [] }
}
// 分区数据加载（测评任务分区 = 任务列表 + 数据集 + Agent 列表），任一失败向上抛出
async function loadAll() {
  const results = await Promise.allSettled([loadRuns(), loadDatasets(), loadAgents()])
  if (results.some(result => result.status === 'rejected')) throw new Error('测评任务分区加载失败')
}

// 运行中任务自动轮询
let pollTimer: number | undefined
function schedulePolling() {
  if (pollTimer) window.clearTimeout(pollTimer)
  const active = runs.value.some(r => ['pending', 'running', 'cancel_requested'].includes(r.status))
  if (active) {
    pollTimer = window.setTimeout(async () => {
      try {
        await loadRuns()
        if (resultRun.value && tab.value === 'results') await loadResultItems()
      } catch {
        schedulePolling()
      }
    }, 5000)
  }
}
onUnmounted(() => { if (pollTimer) window.clearTimeout(pollTimer) })

// ── 测评任务操作 ──
async function openCreateRun() {
  // 面板自管数据集/Agent 列表，打开前刷新一次，保证空校验与下拉选项为最新
  await Promise.allSettled([loadDatasets(), loadAgents()])
  if (!datasets.value.length) {
    ElMessage.warning('请先在「数据集」中导入用例或社区数据集')
    tab.value = 'datasets'
    return
  }
  createRunVisible.value = true
}
async function submitRun() {
  if (!runForm.value.dataset_id || !runForm.value.agent_id) {
    ElMessage.warning('请选择数据集与 Agent')
    return
  }
  loading.value = true
  try {
    const run = await evaluationApi.createRun({
      name: runForm.value.name || undefined,
      dataset_id: runForm.value.dataset_id,
      agent_id: runForm.value.agent_id,
      approval_mode: runForm.value.approval_mode,
      use_memory: runForm.value.use_memory,
      timeout_s: runForm.value.timeout_s,
      limit: runForm.value.limit,
    })
    createRunVisible.value = false
    ElMessage.success('测评任务已创建并开始执行')
    tab.value = 'runs'
    await loadRuns()
    await openResult(run)
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function cancelRun(row: EvalRun) {
  if (!await confirmAction(`确认取消测评「${row.name}」？未开始的用例将标记为跳过。`, '取消测评')) return
  loading.value = true
  try {
    await evaluationApi.cancelRun(row.id)
    ElMessage.success('已请求取消')
    await loadRuns()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function resumeRun(row: EvalRun) {
  loading.value = true
  try {
    await evaluationApi.resumeRun(row.id)
    ElMessage.success('已重新执行未完成用例')
    await loadRuns()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function exportMd(row: EvalRun) {
  const md = await evaluationApi.exportRunMarkdown(row.id)
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `eval-report-${row.id}-${dayjs().format('YYYYMMDD-HHmm')}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

// ── 测评结果 ──
async function openResult(row: EvalRun) {
  resultRun.value = row
  tab.value = 'results'
  await loadResultItems()
}
async function loadResultItems() {
  if (!resultRun.value) return
  loading.value = true
  try {
    resultRun.value = await evaluationApi.getRun(resultRun.value.id)
    resultItems.value = (await evaluationApi.listRunItems(resultRun.value.id)).items
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function openItem(row: EvalRunItemBrief) {
  await itemDialog.value?.open(row)
}

defineExpose({ loadAll, openCreateRun })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.hint, .progress-text { color: var(--ui-muted); font-size: var(--ui-size-sm); }
.metric-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--ui-space-3); margin-bottom: var(--ui-space-4); }
.metric-label { color: var(--ui-muted); font-size: var(--ui-size-xs); }
.metric-value { font-size: var(--ui-size-xl); font-weight: var(--ui-weight-semibold); margin-top: 5px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.cap-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--ui-space-3); margin-bottom: var(--ui-space-3); }
.cap-row { display: grid; grid-template-columns: minmax(5rem, auto) 1fr; gap: 4px 8px; margin-bottom: 8px; align-items: center; }
.cap-name { color: var(--ui-secondary); font-size: var(--ui-size-xs); }
.cap-detail { grid-column: 2; font-size: var(--ui-size-xs); color: var(--ui-muted); }
.cap-facts { padding-inline-start: 18px; color: var(--ui-secondary); }
.cap-facts li { margin-bottom: 4px; }
.problem-card { margin-bottom: var(--ui-space-3); }
@media (max-width: 1100px) {
  .cap-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 680px) {
  .metric-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
