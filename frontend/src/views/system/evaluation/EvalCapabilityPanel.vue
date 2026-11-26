<template>
  <el-tab-pane label="能力总览" name="capability">
    <div class="toolbar">
      <el-select v-model="selectedRunId" placeholder="选择已完成测评" style="width:340px" size="small">
        <el-option v-for="r in completedRuns" :key="r.id"
          :label="`${r.name}（${agentName(r.agent_id)} · v${r.agent_version_id ?? '未知'}）`"
          :value="r.id" />
      </el-select>
      <span class="hint">按最近完成的测评展示 Agent 能力边界；空白表示尚无证据。</span>
    </div>

    <template v-if="run">
      <div class="metric-cards">
        <el-card shadow="never">
          <div class="metric-label">Trial / Case 通过率</div>
          <div class="metric-value">{{ pct(run.aggregate?.pass_rate) }} / {{ pct(run.aggregate?.case_pass_rate) }}</div>
          <div class="hint">有效 {{ run.aggregate?.effective_cases ?? 0 }} Case · {{ run.aggregate?.effective_sample ?? 0 }} Trial</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">稳定性</div>
          <div class="metric-value">{{ passKText(run.aggregate) }}</div>
          <div class="hint">pass@k / pass^k（k = 重复执行次数）</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">结果分解</div>
          <div class="outcome-line">
            <el-tag type="success" size="small">通过 {{ run.aggregate?.outcomes?.pass ?? 0 }}</el-tag>
            <el-tag type="danger" size="small">失败 {{ run.aggregate?.outcomes?.fail ?? 0 }}</el-tag>
            <el-tag type="warning" size="small">证据不足 {{ run.aggregate?.outcomes?.inconclusive ?? 0 }}</el-tag>
            <el-tag size="small">未评分 {{ run.aggregate?.outcomes?.unscored ?? 0 }}</el-tag>
          </div>
          <div class="hint">基础设施错误 {{ run.aggregate?.outcomes?.infra_error ?? 0 }}</div>
        </el-card>
        <el-card shadow="never">
          <div class="metric-label">环境</div>
          <div class="metric-value">v{{ run.agent_version_id ?? '未知' }}</div>
          <div class="hint">{{ run.environment?.model?.model_display_name || '模型未记录' }} ·
            {{ run.environment?.runtime?.app_version || '-' }}</div>
        </el-card>
      </div>

      <div class="cap-grid">
        <el-card shadow="never">
          <h4>能力维度（样本量 + 通过情况）</h4>
          <el-table :data="capabilityRows" size="small" stripe max-height="320">
            <el-table-column prop="text" label="维度" width="120" />
            <el-table-column label="样本" width="70">
              <template #default="{ row }">{{ row.total }}</template>
            </el-table-column>
            <el-table-column label="通过率" width="90">
              <template #default="{ row }">{{ pct(row.pass_rate) }}</template>
            </el-table-column>
            <el-table-column label="均分" width="70">
              <template #default="{ row }">{{ row.avg_score ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="证据" min-width="110">
              <template #default="{ row }">
                <el-tag :type="row.evidence_sufficient ? 'success' : 'info'" size="small">
                  {{ row.evidence_sufficient ? '充分' : '不足' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
        <el-card shadow="never">
          <h4>低通过率维度（客观排序）</h4>
          <div v-if="weakRows.length" class="weak-list">
            <div v-for="row in weakRows" :key="row.cap" class="weak-row">
              <span>{{ row.text }}</span>
              <el-progress :percentage="pctNum(row.pass_rate)" :stroke-width="10" class="weak-bar" />
              <span class="hint">{{ row.passed }}/{{ row.total }} 通过 · 均分 {{ row.avg_score ?? '—' }}</span>
            </div>
          </div>
          <div v-else class="hint">暂无足够证据形成结论</div>
          <h4 style="margin-top:14px;">复杂度分布</h4>
          <div v-for="row in complexityRows" :key="row.lvl" class="weak-row">
            <span>{{ row.text }}</span>
            <el-progress :percentage="pctNum(row.pass_rate)" :stroke-width="10" class="weak-bar" />
            <span class="hint">{{ row.passed }}/{{ row.total }} 通过</span>
          </div>
        </el-card>
      </div>
    </template>
    <div v-else-if="!sectionLoading" class="data-surface">
      <EmptyState :icon="ChartColumn" title="暂无已完成的测评"
        description="先创建并完成一次测评任务，这里会按能力维度展示证据与短板。" />
    </div>
  </el-tab-pane>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChartColumn } from '@lucide/vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { evaluationApi, type EvalRun } from '@/api/evaluation'
import { agentOptionApi } from '@/api/assistants'
import { pct, pctNum, complexityText, capabilityText } from './shared'

defineProps<{ sectionLoading: boolean }>()

const runs = ref<EvalRun[]>([])
const agents = ref<Array<{ id: number; name: string }>>([])
const selectedRunId = ref<number | undefined>(undefined)

const completedRuns = computed(() => runs.value
  .filter(r => r.status === 'completed')
  .sort((a, b) => String(b.finished_at || b.created_at || '')
    .localeCompare(String(a.finished_at || a.created_at || ''))))
const run = computed(() => completedRuns.value.find(r => r.id === selectedRunId.value)
  || completedRuns.value[0] || null)
const capabilityRows = computed(() => Object.entries(run.value?.aggregate?.by_capability || {})
  .map(([cap, s]) => ({ cap, text: s.name || capabilityText[cap] || cap, ...s })))
const weakRows = computed(() => capabilityRows.value
  .filter(r => r.evidence_sufficient && (r.pass_rate ?? 1) < 1)
  .sort((a, b) => (a.pass_rate ?? 0) - (b.pass_rate ?? 0))
  .slice(0, 4))
const complexityRows = computed(() => Object.entries(run.value?.aggregate?.by_complexity || {})
  .map(([lvl, s]) => ({ lvl, text: complexityText[lvl] || lvl, ...s })))

function agentName(id: number) { return agents.value.find(a => a.id === id)?.name || `#${id}` }
function passKText(agg: any) {
  const pk = agg?.pass_k
  if (!pk || !pk.k) return '-'
  return `${pct(pk.pass_at_k)} / ${pct(pk.pass_hat_k)}`
}

async function loadAll() {
  const runRes = await evaluationApi.listRuns()
  runs.value = runRes.items
  try { agents.value = await agentOptionApi.list() } catch { agents.value = [] }
}

defineExpose({ loadAll })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.hint { color: var(--ui-muted); font-size: var(--ui-size-sm); }
.metric-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--ui-space-3); margin-bottom: var(--ui-space-4); }
.metric-label { color: var(--ui-muted); font-size: var(--ui-size-xs); }
.metric-value { font-size: var(--ui-size-xl); font-weight: var(--ui-weight-semibold); margin-top: 5px; font-variant-numeric: tabular-nums; }
.outcome-line { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.cap-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--ui-space-3); }
.weak-row { display: grid; grid-template-columns: minmax(5rem, auto) 1fr; gap: 4px 8px; margin-bottom: 8px; align-items: center; }
.weak-bar { grid-column: 2; }
.weak-row .hint { grid-column: 2; }
@media (max-width: 1100px) { .cap-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 680px) { .metric-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
