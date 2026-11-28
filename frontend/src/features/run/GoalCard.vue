<template>
  <section class="goal-card" aria-label="目标契约与验收进度">
    <header class="goal-head">
      <span class="goal-symbol"><Target :size="19" aria-hidden="true" /></span>
      <div class="goal-title">
        <h3>目标契约 <small>v{{ local.revision }}</small></h3>
        <p role="status" :data-state="local.status">
          {{ statusText }}
        </p>
      </div>
      <div class="goal-meta">
        <span class="goal-percent">{{ local.progress.percent }}%</span>
        <span class="goal-count">
          {{ local.progress.required_passed }} / {{ local.progress.required_total }} 必需项
        </span>
      </div>
    </header>

    <div class="goal-progress" aria-hidden="true">
      <span :style="{ width: `${Math.max(0, Math.min(100, local.progress.percent))}%` }" />
    </div>

    <div class="goal-body">
      <p v-if="local.objective" class="goal-objective">{{ local.objective }}</p>

      <ul v-if="local.criteria.length" class="goal-criteria" aria-label="验收条件">
        <li v-for="item in local.criteria" :key="item.id" :data-state="item.status">
          <component :is="criterionIcon(item.status)" :size="15" aria-hidden="true" />
          <div class="goal-criterion-main">
            <div class="goal-criterion-head">
              <code>{{ item.id }}</code>
              <strong>{{ item.description }}</strong>
              <span class="goal-criterion-tag" :data-required="item.required">
                {{ item.required ? '必需' : '可选' }}
              </span>
              <span class="goal-criterion-status">{{ criterionLabel(item.status) }}</span>
            </div>
            <p v-if="item.failure_reason" class="goal-criterion-gap">{{ item.failure_reason }}</p>
          </div>
        </li>
      </ul>
      <p v-else class="goal-empty">验收条件正在生成…</p>

      <div v-if="local.blocking.length" class="goal-blocking">
        <TriangleExclamationPoint :size="15" aria-hidden="true" />
        <span>待处理：{{ local.blocking.map(item => item.description).join('；') }}</span>
      </div>

      <details v-if="local.iterationsDetail?.length" class="goal-history">
        <summary>执行历史（{{ local.iterationsDetail.length }} 轮）</summary>
        <ul>
          <li v-for="item in local.iterationsDetail.slice(0, 8)" :key="item.iterationNo" :data-state="item.status">
            <span>第 {{ item.iterationNo }} 轮</span>
            <em>{{ item.status }}</em>
            <span v-if="item.reason">{{ item.reason }}</span>
          </li>
        </ul>
      </details>

      <div v-if="local.research" class="goal-research">
        <header>
          <Search :size="15" aria-hidden="true" />
          <strong>Deep Research</strong>
          <span>{{ local.research.status }}</span>
          <span class="goal-research-counts">
            来源 {{ local.research.sourceCount }} · 证据 {{ local.research.evidenceCount }} · 主张 {{ local.research.claimCount }}
          </span>
        </header>
        <p v-if="local.research.topic" class="goal-research-topic">{{ local.research.topic }}</p>
        <p v-if="local.research.scope?.time_range" class="goal-research-topic">
          时间范围：{{ local.research.scope.time_range }}
        </p>
        <ul v-if="local.research.questions.length" class="goal-questions">
          <li v-for="question in local.research.questions" :key="question.id" :data-priority="question.priority">
            <code>{{ question.id }}</code>
            <span>{{ question.question }}</span>
            <em>{{ question.evidenceCount }} 证据 / {{ question.claimCount }} 主张</em>
          </li>
        </ul>
        <div v-if="local.research.sources.length" class="goal-sources">
          <span>已读来源</span>
          <a v-for="source in local.research.sources.slice(0, 6)" :key="source.sourceId"
            :href="source.url" target="_blank" rel="noreferrer noopener"
            :title="source.title">{{ source.sourceId }} {{ source.title || source.url }}</a>
        </div>
        <div v-if="local.research.claims.length" class="goal-claims">
          <span>主张核验</span>
          <em v-for="claim in local.research.claims.slice(0, 6)" :key="claim.claimId"
            :data-status="claim.status" :title="claim.statement">{{ claim.claimId }} · {{ claim.status }}</em>
        </div>
        <p v-if="local.research.gaps.length" class="goal-gaps">
          缺口：{{ local.research.gaps.join('；') }}
        </p>
        <div v-if="editable" class="goal-actions">
          <button type="button" class="goal-edit-toggle" @click="toggleResearchEditor">
            <PencilLine :size="14" /> {{ editingResearch ? '收起研究计划修改' : '调整研究范围 / 问题' }}
          </button>
        </div>
        <div v-if="editingResearch" class="goal-editor">
          <label>
            <span>研究主题</span>
            <textarea v-model="draftResearchTopic" rows="2" maxlength="1000" />
          </label>
          <label>
            <span>时间范围（可选，如 2024-2026）</span>
            <input v-model="draftTimeRange" maxlength="120" />
          </label>
          <label>
            <span>研究问题（每行一条）</span>
            <textarea v-model="draftQuestions" rows="4" />
          </label>
          <p v-if="researchError" class="goal-save-error">{{ researchError }}</p>
          <div class="goal-editor-actions">
            <button type="button" :disabled="researchSaving" @click="saveResearch">
              {{ researchSaving ? '保存中…' : '保存研究计划' }}
            </button>
            <span>保留已收集的来源与证据，计划 revision 递增。</span>
          </div>
        </div>
      </div>

      <div v-if="editable || pauseVisible" class="goal-actions">
        <button v-if="pauseVisible" type="button" class="goal-pause" :disabled="pausing" @click="pauseGoal">
          <CircleSlash :size="14" /> {{ pausing ? '暂停中…' : '暂停任务' }}
        </button>
        <button v-if="editable" type="button" class="goal-edit-toggle" @click="editing = !editing">
          <PencilLine :size="14" /> {{ editing ? '收起修改' : '修改目标 / 验收条件' }}
        </button>
      </div>
      <p v-if="pauseError" class="goal-save-error">{{ pauseError }}</p>

      <div v-if="editing" class="goal-editor">
        <label>
          <span>目标</span>
          <textarea v-model="draftObjective" rows="3" maxlength="20000" />
        </label>
        <label>
          <span>验收条件（每行一条）</span>
          <textarea v-model="draftCriteria" rows="4" />
        </label>
        <p v-if="saveError" class="goal-save-error">{{ saveError }}</p>
        <div class="goal-editor-actions">
          <button type="button" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存为新版本' }}
          </button>
          <span>保存后受影响项会重新验证。</span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  CheckCircle2, Circle, CircleSlash, PencilLine, Search, Target,
  TriangleExclamationPoint, XCircle,
} from '@lucide/vue'
import { goalApi, type GoalContract } from '@/api/goals'
import { runApi } from '@/api/runs'
import {
  criterionStatusLabel, goalStatusLabel, mergeResearch, type GoalView,
} from './goal'

const props = defineProps<{
  goal: GoalView
  editable?: boolean
  runId?: string
  active?: boolean
}>()
const emit = defineEmits<{ (event: 'revised', goal: GoalView): void }>()

const local = ref<GoalView>({ ...props.goal })
const editing = ref(false)
const saving = ref(false)
const saveError = ref('')
const pausing = ref(false)
const pauseError = ref('')
const draftObjective = ref('')
const draftCriteria = ref('')
const editingResearch = ref(false)
const researchSaving = ref(false)
const researchError = ref('')
const draftResearchTopic = ref('')
const draftTimeRange = ref('')
const draftQuestions = ref('')

const pauseVisible = computed(() => Boolean(
  props.active && props.runId && local.value.goalId && local.value.status === 'active',
))

watch(() => props.goal, (next) => {
  if (editing.value) return
  local.value = { ...next }
}, { deep: true })

watch(editing, (open) => {
  if (!open) return
  draftObjective.value = local.value.objective
  draftCriteria.value = local.value.criteria.map((item) => item.description).join('\n')
  saveError.value = ''
})

const statusText = computed(() => {
  const base = goalStatusLabel(local.value.status)
  return local.value.iteration ? `${base} · 第 ${local.value.iteration} 轮` : base
})

function criterionLabel(status: string): string {
  return criterionStatusLabel(status)
}

function criterionIcon(status: string) {
  if (status === 'passed') return CheckCircle2
  if (status === 'failed' || status === 'blocked') return XCircle
  if (status === 'in_progress') return CircleSlash
  return Circle
}

async function loadDetail() {
  if (!local.value.goalId || editing.value) return
  try {
    const detail = await goalApi.detail(local.value.goalId) as GoalContract
    local.value = {
      ...local.value,
      revision: detail.revision,
      objective: detail.objective || local.value.objective,
      status: detail.status || local.value.status,
      progress: detail.progress || local.value.progress,
      criteria: (detail.acceptance_criteria || local.value.criteria) as any,
      iterationsDetail: Array.isArray(detail.iterations_detail)
        ? detail.iterations_detail.map((item: any) => ({
          iterationNo: Number(item.iteration_no) || 0,
          status: String(item.status || ''),
          executor: String(item.executor || ''),
          reason: String(item.result?.reason || ''),
          startedAt: item.started_at,
          finishedAt: item.finished_at,
        }))
        : local.value.iterationsDetail,
    }
  } catch {
    // 历史目标不存在时不打扰当前对话
  }
}

async function loadResearch() {
  if (!local.value.goalId) return
  try {
    const snapshot = await goalApi.research(local.value.goalId)
    if (!snapshot?.plan) return
    local.value = mergeResearch(
      local.value,
      { ...snapshot.plan, questions: snapshot.plan.questions },
      snapshot as unknown as Record<string, any>,
    )
  } catch {
    // 非研究目标/历史数据缺失时忽略
  }
}

onMounted(() => {
  void loadDetail()
  void loadResearch()
})

async function pauseGoal() {
  if (pausing.value || !props.runId) return
  pausing.value = true
  pauseError.value = ''
  try {
    await runApi.pause(props.runId)
  } catch (error: any) {
    pauseError.value = String(error?.message || '暂停请求失败，请稍后重试')
  } finally {
    pausing.value = false
  }
}

async function save() {
  if (saving.value) return
  saving.value = true
  saveError.value = ''
  try {
    const criteria = draftCriteria.value.split('\n').map((line) => line.trim()).filter(Boolean)
    const updated = await goalApi.revise(local.value.goalId, {
      objective: draftObjective.value.trim() || undefined,
      acceptance_criteria: criteria.length ? criteria : undefined,
      note: '用户在目标面板修改目标',
    }) as GoalContract
    local.value = {
      ...local.value,
      revision: updated.revision,
      objective: updated.objective,
      criteria: (updated.acceptance_criteria || []) as any,
      status: updated.status,
      progress: updated.progress || local.value.progress,
    }
    editing.value = false
    emit('revised', local.value)
  } catch (error: any) {
    saveError.value = String(error?.message || '保存失败，请稍后重试')
  } finally {
    saving.value = false
  }
}

function toggleResearchEditor() {
  editingResearch.value = !editingResearch.value
  if (!editingResearch.value) return
  const research = local.value.research
  draftResearchTopic.value = research?.topic || ''
  draftTimeRange.value = String(research?.scope?.time_range || '')
  draftQuestions.value = (research?.questions || []).map((item) => item.question).join('\n')
  researchError.value = ''
}

async function saveResearch() {
  if (researchSaving.value || !local.value.goalId) return
  researchSaving.value = true
  researchError.value = ''
  try {
    const questions = draftQuestions.value.split('\n').map((line) => line.trim()).filter(Boolean)
    const updated = await goalApi.reviseResearch(local.value.goalId, {
      topic: draftResearchTopic.value.trim() || undefined,
      scope: draftTimeRange.value.trim() ? { time_range: draftTimeRange.value.trim() } : undefined,
      questions: questions.length ? questions : undefined,
      reason: '用户在目标面板调整研究计划',
    })
    if (updated?.plan || updated?.research_id) {
      local.value = mergeResearch(
        local.value,
        { ...(updated.plan || updated), questions: updated.questions || [] },
      )
    }
    editingResearch.value = false
  } catch (error: any) {
    researchError.value = String(error?.message || '保存失败，请稍后重试')
  } finally {
    researchSaving.value = false
  }
}
</script>

<style scoped>
.goal-card {
  min-width: 0; overflow: hidden; border: var(--ui-line); border-radius: var(--ui-radius-lg);
  background: var(--ui-surface);
}
.goal-head {
  display: flex; align-items: center; gap: 10px; padding: 11px 13px;
  background: var(--ui-subtle); border-bottom: var(--ui-line); flex-wrap: wrap;
}
.goal-symbol {
  display: grid; place-items: center; flex: none; width: 36px; height: 36px;
  border-radius: var(--ui-radius-md); background: var(--ui-accent-soft); color: var(--ui-accent);
}
.goal-title { flex: 1 1 160px; min-width: 0; }
.goal-title h3 { font-size: 14px; font-weight: 700; line-height: 1.4; }
.goal-title h3 small { color: var(--ui-secondary); font-weight: 500; font-size: 11px; }
.goal-title p { color: var(--ui-secondary); font-size: 12px; }
.goal-title p[data-state='completed'] { color: var(--ui-success); }
.goal-title p[data-state='blocked'], .goal-title p[data-state='failed'] { color: var(--ui-danger); }
.goal-title p[data-state='paused'] { color: var(--ui-warning, var(--ui-info)); }
.goal-meta { display: flex; align-items: baseline; gap: 8px; }
.goal-percent { font-size: 18px; font-weight: 700; color: var(--ui-accent); }
.goal-count { color: var(--ui-secondary); font-size: 12px; }
.goal-progress { height: 3px; background: var(--ui-subtle); }
.goal-progress span { display: block; height: 100%; background: var(--ui-accent); transition: width .3s ease; }
.goal-body { display: grid; gap: 10px; padding: 12px 13px; }
.goal-objective {
  margin: 0; padding: 8px 10px; border-radius: var(--ui-radius-md);
  background: var(--ui-subtle); color: var(--ui-primary); font-size: 13px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word; max-height: 130px; overflow: auto;
}
.goal-criteria { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.goal-criteria li { display: flex; gap: 7px; align-items: flex-start; font-size: 12px; color: var(--ui-secondary); }
.goal-criteria li[data-state='passed'] > svg { color: var(--ui-success); }
.goal-criteria li[data-state='failed'] > svg, .goal-criteria li[data-state='blocked'] > svg { color: var(--ui-danger); }
.goal-criterion-main { min-width: 0; display: grid; gap: 2px; }
.goal-criterion-head { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; }
.goal-criterion-head code { color: var(--ui-accent); font-size: 11px; }
.goal-criterion-head strong { color: var(--ui-primary); font-weight: 500; }
.goal-criterion-tag { font-size: 10px; padding: 0 4px; border: var(--ui-line); border-radius: 4px; }
.goal-criterion-tag[data-required='true'] { color: var(--ui-accent); }
.goal-criterion-status { font-size: 11px; }
.goal-criterion-gap { margin: 0; color: var(--ui-danger); font-size: 11px; }
.goal-blocking {
  display: flex; gap: 6px; align-items: flex-start; padding: 7px 9px;
  border-radius: var(--ui-radius-md); background: var(--ui-warning-soft, var(--ui-subtle));
  color: var(--ui-warning, var(--ui-primary)); font-size: 12px;
}
.goal-research { display: grid; gap: 6px; padding-top: 8px; border-top: var(--ui-line); }
.goal-research header { display: flex; gap: 6px; align-items: center; font-size: 12px; flex-wrap: wrap; }
.goal-research-counts { margin-left: auto; color: var(--ui-secondary); }
.goal-research-topic { margin: 0; color: var(--ui-secondary); font-size: 12px; }
.goal-questions { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.goal-questions li { display: flex; gap: 6px; align-items: baseline; font-size: 12px; color: var(--ui-secondary); }
.goal-questions li[data-priority='high'] code { color: var(--ui-danger); }
.goal-questions em { margin-left: auto; font-size: 11px; white-space: nowrap; }
.goal-gaps { margin: 0; color: var(--ui-warning, var(--ui-primary)); font-size: 11px; }
.goal-history summary { cursor: pointer; color: var(--ui-secondary); font-size: 12px; }
.goal-history ul { display: grid; gap: 3px; margin: 6px 0 0; padding: 0; list-style: none; }
.goal-history li { display: flex; gap: 6px; align-items: baseline; font-size: 11px; color: var(--ui-secondary); }
.goal-history li em { color: var(--ui-accent); font-style: normal; }
.goal-history li[data-state='blocked'] em, .goal-history li[data-state='failed'] em { color: var(--ui-danger); }
.goal-sources, .goal-claims { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; font-size: 11px; color: var(--ui-secondary); }
.goal-sources a { color: var(--ui-accent); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.goal-claims em { font-style: normal; padding: 0 4px; border: var(--ui-line); border-radius: 4px; }
.goal-claims em[data-status='supported'], .goal-claims em[data-status='verified'] { color: var(--ui-success); }
.goal-claims em[data-status='insufficient'], .goal-claims em[data-status='conflicting'] { color: var(--ui-danger); }
.goal-editor input {
  width: 100%; padding: 6px 8px; border: var(--ui-line); border-radius: var(--ui-radius-md);
  background: var(--ui-surface); color: var(--ui-primary); font-size: 12px;
}
.goal-actions { display: flex; justify-content: flex-end; gap: 10px; }
.goal-pause {
  display: inline-flex; align-items: center; gap: 4px; border: 0; background: none;
  color: var(--ui-secondary); font-size: 12px; cursor: pointer;
}
.goal-pause:disabled { opacity: .6; cursor: default; }
.goal-edit-toggle {
  display: inline-flex; align-items: center; gap: 4px; border: 0; background: none;
  color: var(--ui-accent); font-size: 12px; cursor: pointer;
}
.goal-editor { display: grid; gap: 8px; padding: 8px; border: var(--ui-line); border-radius: var(--ui-radius-md); }
.goal-editor label { display: grid; gap: 4px; font-size: 12px; color: var(--ui-secondary); }
.goal-editor textarea {
  width: 100%; resize: vertical; padding: 7px 8px; border: var(--ui-line);
  border-radius: var(--ui-radius-md); background: var(--ui-surface); color: var(--ui-primary);
  font-size: 12px; line-height: 1.5;
}
.goal-editor-actions { display: flex; gap: 8px; align-items: center; font-size: 11px; color: var(--ui-secondary); }
.goal-editor-actions button {
  padding: 5px 10px; border: 0; border-radius: var(--ui-radius-md);
  background: var(--ui-accent); color: #fff; font-size: 12px; cursor: pointer;
}
.goal-editor-actions button:disabled { opacity: .6; cursor: default; }
.goal-save-error { margin: 0; color: var(--ui-danger); font-size: 12px; }
.goal-empty { margin: 0; color: var(--ui-secondary); font-size: 12px; }
</style>
