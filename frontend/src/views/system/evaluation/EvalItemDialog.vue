<template>
  <!-- 用例结果详情 -->
  <el-drawer v-model="itemVisible" title="用例结果详情" size="min(870px, 100vw)">
    <el-descriptions v-if="currentItem" :column="2" border size="small">
      <el-descriptions-item label="结果">
        <el-tag :type="outcomeTagType(currentItem.outcome || legacyOutcome(currentItem.status))" size="small">
          {{ outcomeText[currentItem.outcome || legacyOutcome(currentItem.status)] || currentItem.status }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="得分">{{ currentItem.score ?? '-' }}</el-descriptions-item>
      <el-descriptions-item label="执行状态">{{ executionText[currentItem.execution_status || ''] || currentItem.execution_status || '-' }}</el-descriptions-item>
      <el-descriptions-item label="验证 / 评分">
        {{ verificationText[currentItem.verification_status || ''] || currentItem.verification_status || '-' }} /
        {{ gradingText[currentItem.grading_status || ''] || currentItem.grading_status || '-' }}
      </el-descriptions-item>
      <el-descriptions-item label="复杂度">
        <el-tag :type="complexityTagType(currentItem.complexity)" size="small">
          {{ complexityText[currentItem.complexity] || currentItem.complexity }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="任务 / 能力">
        {{ taskTypeText[currentItem.task_type] || currentItem.task_type }} ·
        {{ capabilityText[currentItem.capability || ''] || currentItem.capability || '-' }}
      </el-descriptions-item>
      <el-descriptions-item label="评分方式">{{ currentItem.snap_scoring_method }}</el-descriptions-item>
      <el-descriptions-item label="Run">{{ currentItem.run_id || '-' }}</el-descriptions-item>
    </el-descriptions>
    <div v-if="currentItem" class="toolbar" style="margin-top:12px;">
      <UiButton size="sm" :loading="regrading" @click="regrade">重新评分（不重跑 Agent）</UiButton>
      <UiButton v-if="!isPassed" size="sm" :loading="archiving" @click="archiveRegression">
        沉淀为回归用例
      </UiButton>
      <el-dropdown size="small" @command="review">
        <el-button size="small">人工复核<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="needs_review">标记需复核</el-dropdown-item>
            <el-dropdown-item command="confirm_pass">人工判通过</el-dropdown-item>
            <el-dropdown-item command="confirm_fail">人工判失败</el-dropdown-item>
            <el-dropdown-item command="reject_score">拒绝当前评分</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <span class="hint">重新评分只执行 Verifier / Judge；沉淀会把当前用例快照复制到「回归用例（自动沉淀）」数据集。</span>
    </div>
    <template v-if="currentItem">
      <h4>执行事实（客观）</h4>
      <div class="fact-grid">
        <span>步数：<b>{{ currentItem.stats?.steps ?? '-' }}</b></span>
        <span>模型调用：{{ currentItem.stats?.model_calls ?? '-' }}</span>
        <span>工具调用：<b>{{ currentItem.stats?.tool_calls ?? '-' }}</b></span>
        <span>Tokens：{{ currentItem.stats?.tokens ?? '-' }}</span>
        <span>耗时：{{ currentItem.stats?.duration_s ?? '-' }}s</span>
        <span>输出字数：<b>{{ currentItem.stats?.output_chars ?? '-' }}</b></span>
        <span v-if="currentItem.stats?.artifact_chars">交付物字数：<b>{{ currentItem.stats.artifact_chars }}</b></span>
        <span v-if="currentItem.stats?.turns_count">轮次：{{ currentItem.stats.turns_count }}</span>
        <span>工具成功率：{{ currentItem.stats?.tool_success_rate != null ? pct(currentItem.stats.tool_success_rate) : '-' }}</span>
        <span>使用工具：{{ (currentItem.stats?.used_tools || []).join('、') || '无' }}</span>
        <span v-if="currentItem.snap_meta?.required_tools?.length">
          必需工具：{{ currentItem.snap_meta.required_tools.join('、') }}
          <b v-if="currentItem.stats?.missing_tools?.length" class="warn">
            （未成功执行：{{ currentItem.stats.missing_tools.join('、') }}）
          </b>
          <b v-else class="ok">（已调用）</b>
        </span>
        <span v-if="currentItem.snap_meta?.expected_min_steps">
          期望最少步数：{{ currentItem.snap_meta.expected_min_steps }}
          <b :class="currentItem.stats?.steps_met === false ? 'warn' : 'ok'">
            （{{ currentItem.stats?.steps_met === false ? '未达到' : '已达到' }}）
          </b>
        </span>
        <span v-if="currentItem.stats?.limit_exceeded" class="warn">超出执行上限</span>
      </div>
      <!-- 客观约束检查清单（constraint 评分） -->
      <template v-if="constraintChecks.length">
        <h4>客观约束检查（{{ constraintPassedCount }}/{{ constraintChecks.length }} 通过）</h4>
        <el-table :data="constraintChecks" size="small" stripe max-height="220">
          <el-table-column prop="type" label="约束类型" width="130" />
          <el-table-column label="结果" width="70">
            <template #default="{ row }">
              <el-tag :type="row.passed ? 'success' : 'danger'" size="small">{{ row.passed ? '通过' : '未过' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="240">
            <template #default="{ row }">{{ row.desc }}</template>
          </el-table-column>
        </el-table>
      </template>
      <template v-if="currentItem.stats?.tool_chain?.length">
        <h4>工具调用链</h4>
        <el-table :data="currentItem.stats.tool_chain" size="small" stripe max-height="220">
          <el-table-column prop="tool_name" label="工具" width="130" />
          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <el-tag :type="row.status === 'succeeded' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="attempt_count" label="尝试" width="60" />
          <el-table-column prop="duration_s" label="耗时(s)" width="80" />
          <el-table-column label="参数（摘录）" min-width="200">
            <template #default="{ row }">{{ row.args || '-' }}</template>
          </el-table-column>
          <el-table-column label="错误" min-width="140">
            <template #default="{ row }">{{ row.error || '-' }}</template>
          </el-table-column>
        </el-table>
      </template>
      <template v-if="(currentItem.stats?.artifacts || []).length">
        <h4>最终交付产物（工作区 / 工具调用）</h4>
        <el-table :data="currentItem.stats.artifacts" size="small" stripe max-height="200">
          <el-table-column prop="path" label="文件" min-width="240" show-overflow-tooltip />
          <el-table-column prop="size" label="字节" width="90" />
          <el-table-column prop="source" label="来源" width="100" />
          <el-table-column prop="sha256" label="SHA256" min-width="200" show-overflow-tooltip />
          <el-table-column label="操作" width="76">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="openArtifact(row.path)">查看</el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <template v-if="(currentItem.grades || []).length">
        <h4>评分记录（Verifier / Judge 历史）</h4>
        <el-table :data="currentItem.grades" size="small" stripe max-height="220">
          <el-table-column prop="kind" label="类型" width="90" />
          <el-table-column prop="name" label="评分器" width="130" />
          <el-table-column prop="status" label="状态" width="90" />
          <el-table-column prop="score" label="得分" width="70" />
          <el-table-column label="证据/理由" min-width="260">
            <template #default="{ row }">
              {{ row.detail?.reasoning || row.detail?.error || JSON.stringify(row.evidence || {}) }}
            </template>
          </el-table-column>
        </el-table>
      </template>
      <template v-if="(currentItem.stats?.turns || []).length > 1">
        <h4>多轮对话（逐轮）</h4>
        <div v-for="t in currentItem.stats.turns" :key="t.turn" class="turn-block">
          <div class="turn-title">第 {{ t.turn }} 轮 · {{ itemStatusText(t.status) }} · {{ t.steps ?? '-' }} 步 · {{ t.tool_calls ?? 0 }} 次工具</div>
          <pre class="block">用户：{{ t.input }}
助手：{{ t.output }}</pre>
        </div>
      </template>
      <h4>输入</h4>
      <pre class="block">{{ currentItem.snap_input }}</pre>
      <h4>期望输出</h4>
      <pre class="block">{{ currentItem.snap_expected || '（无明确期望）' }}</pre>
      <h4>实际输出</h4>
      <pre class="block">{{ currentItem.actual_output || '（空）' }}</pre>
      <h4>评分理由</h4>
      <pre class="block">{{ currentItem.score_detail?.reasoning || JSON.stringify(currentItem.score_detail || {}, null, 2) }}</pre>
      <template v-if="currentItem.error">
        <h4>错误</h4>
        <pre class="block error">{{ currentItem.error }}</pre>
      </template>
      <section class="trace-section" aria-label="执行轨迹">
        <div class="toolbar trace-heading">
          <h4>执行轨迹</h4>
          <UiButton size="sm" :loading="traceLoading" @click="loadTrace">{{ itemTrace ? '刷新轨迹' : '加载轨迹' }}</UiButton>
        </div>
        <p v-if="traceError" class="hint" role="alert">轨迹加载失败，请重试。</p>
        <template v-if="itemTrace">
          <div v-for="rt in itemTrace.runs" :key="rt.run_id" class="trace-block">
            <header class="trace-run-meta"><code>{{ rt.run_id }}</code><span>{{ rt.events.length }} 条事件 · {{ rt.tools.length }} 次工具调用</span></header>
            <ol v-if="rt.events.length" class="trace-timeline">
              <li v-for="event in rt.events" :key="event.sequence">
                <details>
                  <summary><code>{{ event.event_type }}</code><time>{{ formatDate(event.created_at) }}</time></summary>
                  <pre class="block">{{ event.payload || '无载荷' }}</pre>
                </details>
              </li>
            </ol>
            <p v-else class="hint">无事件记录</p>
            <details v-if="rt.tools.length" class="trace-tools">
              <summary>工具执行明细</summary>
              <article v-for="(tool, index) in rt.tools" :key="index" class="trace-tool">
                <div class="trace-tool-title">
                  <strong>{{ tool.tool_name }}</strong>
                  <el-tag :type="tool.status === 'succeeded' ? 'success' : tool.error ? 'danger' : 'info'" size="small">{{ tool.status }}</el-tag>
                  <span class="hint">尝试 {{ tool.attempt_count }} 次 · {{ formatDate(tool.started_at) }}</span>
                </div>
                <template v-if="tool.args"><div class="hint">参数</div><pre class="block">{{ tool.args }}</pre></template>
                <template v-if="tool.result"><div class="hint">结果</div><pre class="block">{{ tool.result }}</pre></template>
                <template v-if="tool.error"><div class="hint">错误</div><pre class="block error">{{ tool.error }}</pre></template>
              </article>
            </details>
          </div>
          <p v-if="!itemTrace.runs.length" class="hint">无轨迹数据</p>
        </template>
      </section>
    </template>
  </el-drawer>

  <!-- 最终产物内容预览 -->
  <el-dialog v-model="artifactVisible" :title="`产物预览：${artifactPath}`" width="min(900px, 96vw)">
    <pre class="block" style="max-height:60vh;">{{ artifactText }}</pre>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@lucide/vue'
import UiButton from '@/components/ui/UiButton.vue'
import {
  evaluationApi,
  type EvalRunItemBrief, type EvalRunItemDetail, type EvalRunItemTrace,
} from '@/api/evaluation'
import {
  formatDate, pct, complexityText, complexityTagType, taskTypeText, itemStatusText,
  capabilityText, outcomeText, outcomeTagType, executionText, verificationText, gradingText,
} from './shared'

const itemVisible = ref(false)
const currentItem = ref<EvalRunItemDetail | null>(null)
const itemTrace = ref<EvalRunItemTrace | null>(null)
const traceLoading = ref(false)
const traceError = ref(false)
const regrading = ref(false)
const archiving = ref(false)
const artifactVisible = ref(false)
const artifactPath = ref('')
const artifactText = ref('')

const isPassed = computed(() =>
  (currentItem.value?.outcome || legacyOutcome(currentItem.value?.status || '')) === 'pass')

function legacyOutcome(status: string) {
  return ({ passed: 'pass', failed: 'fail', error: 'inconclusive',
    skipped: 'cancelled', unscored: 'unscored' } as any)[status] || 'pending'
}

// 客观约束检查清单（constraint 评分明细）
const constraintChecks = computed(() => {
  const d = currentItem.value?.score_detail as any
  return Array.isArray(d?.checks) ? d.checks : []
})
const constraintPassedCount = computed(
  () => constraintChecks.value.filter((c: any) => c.passed).length)

// 打开用例结果详情（轨迹按需加载）
async function open(row: EvalRunItemBrief) {
  currentItem.value = await evaluationApi.getRunItem(row.id)
  itemTrace.value = null
  traceError.value = false
  itemVisible.value = true
}
async function loadTrace() {
  if (!currentItem.value) return
  traceLoading.value = true
  traceError.value = false
  try {
    itemTrace.value = await evaluationApi.getRunItemTrace(currentItem.value.id)
  } catch {
    traceError.value = true
  } finally {
    traceLoading.value = false
  }
}

async function regrade() {
  if (!currentItem.value) return
  regrading.value = true
  try {
    await evaluationApi.regradeItem(currentItem.value.id)
    currentItem.value = await evaluationApi.getRunItem(currentItem.value.id)
    ElMessage.success('已重新评分（执行结果未改动）')
  } catch {
    // 请求拦截器统一提示
  } finally {
    regrading.value = false
  }
}

async function archiveRegression() {
  if (!currentItem.value) return
  archiving.value = true
  try {
    const result = await evaluationApi.trialToRegression(currentItem.value.id)
    ElMessage.success(result.created ? '已沉淀为回归用例' : '回归用例已存在，未重复创建')
  } catch {
    // 请求拦截器统一提示
  } finally {
    archiving.value = false
  }
}

async function openArtifact(path: string) {
  if (!currentItem.value) return
  try {
    artifactText.value = await evaluationApi.getArtifact(currentItem.value.id, path)
    artifactPath.value = path
    artifactVisible.value = true
  } catch {
    // 请求拦截器统一提示
  }
}

async function review(decision: string) {
  if (!currentItem.value) return
  let note = ''
  try {
    const result = await ElMessageBox.prompt(
      '可填写复核说明（留空也可）', '人工复核', {
        confirmButtonText: '提交', cancelButtonText: '取消',
        inputPlaceholder: '复核理由/备注',
      }) as any
    note = result?.value || ''
  } catch {
    return
  }
  try {
    await evaluationApi.reviewItem(currentItem.value.id, decision, note)
    currentItem.value = await evaluationApi.getRunItem(currentItem.value.id)
    ElMessage.success('已提交人工复核')
  } catch {
    // 请求拦截器统一提示
  }
}

defineExpose({ open })
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
.block.error { background: var(--ui-danger-soft); color: var(--ui-danger); }
.fact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 16px; margin-bottom: 8px; overflow-wrap: anywhere; }
.warn { color: var(--ui-danger); }
.ok { color: var(--ui-success); }
.turn-block { margin-bottom: 8px; }
.turn-title { font-size: var(--ui-size-xs); color: var(--ui-muted); margin-bottom: 4px; }
.trace-section { border-top: var(--ui-line); padding-top: var(--ui-space-4); }
.trace-heading { justify-content: space-between; }
.trace-block { border: var(--ui-line); border-radius: var(--ui-radius-lg); padding: var(--ui-space-3); margin-bottom: var(--ui-space-3); }
.trace-run-meta, .trace-tool-title { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
.trace-run-meta { margin-bottom: var(--ui-space-3); overflow-wrap: anywhere; }
.trace-run-meta span { color: var(--ui-muted); font-size: var(--ui-size-xs); }
.trace-timeline { padding: 0 0 0 16px; list-style: none; border-inline-start: var(--ui-line-strong); max-height: 420px; overflow: auto; }
.trace-timeline li { position: relative; }
.trace-timeline li::before { content: ''; position: absolute; left: -20px; top: 14px; width: 7px; height: 7px; border-radius: 50%; background: var(--ui-accent); }
.trace-timeline summary, .trace-tools summary { cursor: pointer; padding: 7px 4px; overflow-wrap: anywhere; }
.trace-timeline time { margin-inline-start: 10px; font-size: var(--ui-size-xs); color: var(--ui-muted); }
.trace-tools { border-top: var(--ui-line); margin-top: var(--ui-space-3); }
.trace-tool { padding: var(--ui-space-2); border-top: var(--ui-line); }
.trace-tool-title { justify-content: flex-start; margin-bottom: 6px; }
@media (max-width: 680px) {
  .fact-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
