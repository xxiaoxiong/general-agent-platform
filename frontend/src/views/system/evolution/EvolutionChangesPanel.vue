<template>
  <!-- ── 优化建议（报告 + 变更审计） ── -->
  <el-tab-pane label="优化建议" name="reports">
    <div class="toolbar">
      <el-input-number v-model="agentId" :min="1" placeholder="Agent ID" style="width:140px;" />
      <el-input-number v-model="windowHours" :min="1" :max="2160" style="width:140px;" />
      <span class="hint">小时窗口</span>
      <el-button type="primary" :loading="loading" @click="generateReport">生成优化报告</el-button>
    </div>
    <el-table :data="reports" v-loading="sectionLoading || loading" empty-text="暂无优化报告" size="small" stripe>
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

    <el-card shadow="never" style="margin-top:12px;">
      <h4>变更审计</h4>
      <el-table :data="changes" v-loading="sectionLoading || loading" empty-text="暂无版本变更" size="small" stripe>
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
    </el-card>
  </el-tab-pane>

  <!-- 报告详情 -->
  <el-dialog v-model="reportVisible" title="优化报告" width="760px" top="6vh">
    <div v-if="currentReport">
      <pre class="block">{{ currentReport.summary_text }}</pre>
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
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import {
  evolutionApi,
  type EvolutionReport, type EvolutionChange,
} from '@/api/evolution'
import { formatDate, confirmAction } from '../evaluation/shared'

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
}>()

// agentId / windowHours / loading 为外壳公共状态（v-model），数据状态全部自管
const agentId = computed({ get: () => props.agentId, set: v => emit('update:agentId', v) })
const windowHours = computed({ get: () => props.windowHours, set: v => emit('update:windowHours', v) })
const loading = computed({ get: () => props.loading, set: v => emit('update:loading', v) })

// ── 优化建议 ──
const reports = ref<EvolutionReport[]>([])
const changes = ref<EvolutionChange[]>([])
const reportVisible = ref(false)
const currentReport = ref<EvolutionReport | null>(null)

function verdictText(v: string) {
  return v === 'improved' ? '已验证·改善' : v === 'regressed' ? '已验证·回退(建议回滚)' : '已验证·持平'
}
function changeText(row: EvolutionChange) {
  const b = JSON.stringify(row.before?.value ?? row.before)
  const a = JSON.stringify(row.after?.value ?? row.after)
  return `${b.length > 40 ? b.slice(0, 40) + '…' : b} → ${a.length > 40 ? a.slice(0, 40) + '…' : a}`
}

// ── 加载 ──
async function loadReports() {
  reports.value = (await evolutionApi.listReports({ agent_id: agentId.value })).items
}
async function loadChanges() {
  changes.value = (await evolutionApi.listChanges({ agent_id: agentId.value })).items
}
// 分区数据加载（优化建议分区 = 报告 + 变更），任一失败向上抛出
async function loadAll() {
  const results = await Promise.allSettled([loadReports(), loadChanges()])
  if (results.some(result => result.status === 'rejected')) throw new Error('优化建议分区加载失败')
}

// ── 报告 / 变更操作 ──
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
  } catch {
    // 请求拦截器统一显示接口错误。
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
  if (!await confirmAction(
    `确认应用建议 [${s.type}/${s.target}]？将修改 Agent 配置并发布新版本。`,
    '一键应用')) return
  loading.value = true
  try {
    const res = await evolutionApi.applySuggestion(currentReport.value.id, index)
    ElMessage.success(`已应用并发布 v${res.new_version_no}（新 Run 自动生效）`)
    await loadChanges()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function rollback(row: EvolutionChange) {
  if (!await confirmAction(
    `确认回滚变更 #${row.id}（${row.target}）？将还原旧值并发布新版本。`,
    '回滚')) return
  loading.value = true
  try {
    const res = await evolutionApi.rollbackChange(row.id)
    ElMessage.success(`已回滚并发布 v${res.restored_version_no}`)
    await loadChanges()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function copyMarkdown(row: EvolutionReport) {
  const res = await evolutionApi.exportMarkdown(row.id)
  await navigator.clipboard.writeText(res.markdown)
  ElMessage.success('报告 Markdown 已复制到剪贴板')
}

defineExpose({ loadAll })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.hint { color: var(--ui-muted); font-size: var(--ui-size-sm); }
.block {
  background: var(--ui-subtle); color: var(--ui-text); border: var(--ui-line); border-radius: var(--ui-radius-md);
  padding: var(--ui-space-3); font-size: var(--ui-size-sm); margin: var(--ui-space-2) 0 var(--ui-space-4);
  max-height: 280px; white-space: pre-wrap; overflow-wrap: anywhere;
}
</style>
