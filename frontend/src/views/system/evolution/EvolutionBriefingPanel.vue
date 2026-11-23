<template>
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
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import {
  evolutionApi,
  type DevBriefing,
} from '@/api/evolution'
import { formatDate, pct } from '../evaluation/shared'

const props = defineProps<{
  agentId: number | undefined
  windowHours: number
  loading: boolean
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

// ── 开发简报 ──
const briefing = ref<DevBriefing | null>(null)
const dimRows = computed(() => {
  const dims = briefing.value?.evaluations?.avg_dimensions || {}
  return Object.entries(dims).map(([dim, avg]) => ({ dim, avg: avg ?? '-' }))
})
const verdictDist = computed(() => {
  const v = briefing.value?.evaluations?.verdicts || {}
  const text = Object.entries(v).map(([k, n]) => `${k}=${n}`).join(', ')
  return text || '-'
})

async function loadBriefing() {
  loading.value = true
  try {
    briefing.value = await evolutionApi.getBriefing({
      agent_id: agentId.value, hours: windowHours.value,
    })
    ElMessage.success('简报已生成')
  } catch {
    // 请求拦截器统一显示接口错误。
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
