<template>
  <section class="work-plan-card" aria-label="工作任务计划">
    <header class="work-head">
      <span class="work-symbol"><ListChecks :size="19" aria-hidden="true" /></span>
      <div class="work-title">
        <h3>工作任务计划</h3>
        <p role="status" :data-state="plan.status">
          <LoaderCircle v-if="running" class="work-spinner" :size="13" aria-hidden="true" />
          {{ headline }}
        </p>
      </div>
      <div class="work-meta">
        <span class="work-mode" :data-mode="plan.mode">{{ modeLabel }}</span>
        <span v-if="plan.steps.length" class="work-count">{{ plan.completedCount }} / {{ plan.stepCount }} 步</span>
      </div>
    </header>

    <div class="work-body">
      <p v-if="plan.objective" class="work-objective">{{ plan.objective }}</p>
      <p v-if="plan.degradedReason" class="work-degraded">
        <TriangleExclamationPoint :size="15" aria-hidden="true" />
        <span>计划未启用：{{ plan.degradedReason }}</span>
      </p>

      <ol v-if="plan.steps.length" class="work-steps" aria-label="工作步骤">
        <li v-for="step in plan.steps" :key="step.id" :data-state="step.status" :class="{ current: step.id === plan.currentStepId }">
          <span class="work-dot" aria-hidden="true">
            <component :is="stepIcon(step.status)" :size="15" :class="{ spin: step.status === 'running' }" />
          </span>
          <div class="work-step-main">
            <div class="work-step-head">
              <strong>{{ step.title }}</strong>
              <span class="work-executor">{{ step.executorLabel || step.executorProfile }}</span>
              <span class="work-status" :data-state="step.status">{{ statusLabel(step.status) }}</span>
            </div>
            <div v-if="step.dependsOn.length" class="work-deps">
              <GitBranch :size="13" aria-hidden="true" />
              <span>依赖</span>
              <em v-for="dep in step.dependsOn" :key="dep">{{ stepTitles.get(dep) ?? dep }}</em>
            </div>
            <p v-if="step.summary" class="work-summary">{{ step.summary }}</p>
            <p v-if="step.verification && step.verification.status !== 'passed'" class="work-check-failed">
              验收：{{ verificationText(step.verification) }}
            </p>
            <p v-if="step.artifactCount > 0" class="work-artifacts">产物 {{ step.artifactCount }} 个</p>
          </div>
        </li>
      </ol>

      <div v-if="plan.verification" class="work-verification" :data-state="plan.verification.status">
        <ShieldCheck v-if="plan.verification.status === 'passed'" :size="15" aria-hidden="true" />
        <ShieldX v-else :size="15" aria-hidden="true" />
        <span>最终验收{{ plan.verification.status === 'passed' ? '通过' : '未通过' }}：{{ verificationText(plan.verification) }}</span>
        <span v-if="plan.verification.artifactCount" class="work-artifact-count">
          {{ plan.verification.artifactCount }} 个交付物
        </span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  CheckCircle2, Circle, CircleSlash, GitBranch, ListChecks, LoaderCircle,
  ShieldCheck, ShieldQuestionMark, ShieldX, TriangleExclamationPoint, XCircle,
} from '@lucide/vue'
import {
  planLastActivity, workPlanStatusLabels, workStepStatusLabels,
  type WorkPlanView, type WorkVerificationView,
} from './workPlan'

const props = defineProps<{ plan: WorkPlanView }>()

const running = computed(() => ['planning', 'ready', 'running', 'verifying'].includes(props.plan.status)
  && props.plan.steps.some((step) => step.status === 'running' || step.status === 'waiting_approval'))
const headline = computed(() => planLastActivity(props.plan))
const modeLabel = computed(() => {
  if (props.plan.mode === 'shadow') return '影子计划'
  if (props.plan.mode === 'auto') return '自动编排'
  return props.plan.mode
})
const stepTitles = computed(() => new Map(props.plan.steps.map((step) => [step.id, step.title])))

function statusLabel(status: string): string {
  return workStepStatusLabels[status] ?? status
}

function verificationText(verification: WorkVerificationView): string {
  const passed = verification.checks.filter((check) => check.passed).length
  const failed = verification.checks.filter((check) => !check.passed)
  const base = verification.checks.length ? `${passed}/${verification.checks.length} 项检查通过` : (workPlanStatusLabels[verification.status] ?? verification.status)
  if (failed.length) return `${base}；${failed[0].detail || failed[0].id}`
  return base
}

function stepIcon(status: string) {
  if (status === 'running') return LoaderCircle
  if (status === 'completed') return CheckCircle2
  if (status === 'failed') return XCircle
  if (status === 'skipped') return CircleSlash
  if (status === 'waiting_approval' || status === 'needs_reconciliation') return ShieldQuestionMark
  return Circle
}
</script>

<style scoped>
.work-plan-card {
  min-width: 0; overflow: hidden; border: var(--ui-line); border-radius: var(--ui-radius-lg); background: var(--ui-surface);
}
.work-head { display: flex; align-items: center; gap: 10px; padding: 11px 13px; background: var(--ui-subtle); border-bottom: var(--ui-line); flex-wrap: wrap; }
.work-symbol { display: grid; place-items: center; flex: none; width: 36px; height: 36px; border-radius: var(--ui-radius-md); background: var(--ui-accent-soft); color: var(--ui-accent); }
.work-title { flex: 1 1 140px; min-width: 0; }
.work-title h3 { font-size: 14px; font-weight: 700; line-height: 1.4; }
.work-title p { display: flex; align-items: center; gap: 5px; color: var(--ui-secondary); font-size: 12px; }
.work-title p[data-state='completed'] { color: var(--ui-success); }
.work-title p[data-state='failed'], .work-title p[data-state='degraded'] { color: var(--ui-danger); }
.work-title p[data-state='paused'] { color: var(--ui-warning, var(--ui-info)); }
.work-spinner, .spin { animation: work-spin 1s linear infinite; }
@keyframes work-spin { to { transform: rotate(360deg); } }
.work-meta { display: flex; align-items: center; gap: 7px; color: var(--ui-secondary); font-size: 11px; }
.work-mode { padding: 2px 7px; border-radius: 999px; background: var(--ui-info-soft); color: var(--ui-info); }
.work-mode[data-mode='shadow'] { background: var(--ui-subtle); color: var(--ui-muted); }
.work-body { padding: 11px 13px 13px; }
.work-objective { color: var(--ui-text); font-size: 13px; overflow-wrap: anywhere; }
.work-degraded { display: flex; align-items: start; gap: 6px; margin-top: 7px; color: var(--ui-danger); font-size: 12px; overflow-wrap: anywhere; }
.work-degraded svg { flex: none; margin-top: 1px; }
.work-steps { margin: 10px 0 0; padding: 0; list-style: none; display: grid; gap: 0; }
.work-steps > li { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 8px; position: relative; padding-bottom: 11px; }
.work-steps > li:not(:last-child)::before { content: ''; position: absolute; inset: 20px auto 0 10px; border-inline-start: 1px solid var(--ui-border-strong); }
.work-dot { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--ui-subtle); color: var(--ui-muted); }
.work-steps > li[data-state='running'] .work-dot { background: var(--ui-info-soft); color: var(--ui-info); }
.work-steps > li[data-state='completed'] .work-dot { background: var(--ui-success-soft); color: var(--ui-success); }
.work-steps > li[data-state='failed'] .work-dot { background: var(--ui-danger-soft); color: var(--ui-danger); }
.work-steps > li[data-state='waiting_approval'] .work-dot { background: var(--ui-info-soft); color: var(--ui-info); }
.work-step-main { min-width: 0; }
.work-step-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: 0; }
.work-step-head strong { font-size: 13px; overflow-wrap: anywhere; }
.work-executor { padding: 1px 6px; border-radius: 4px; background: var(--ui-subtle); color: var(--ui-secondary); font-size: 11px; }
.work-status { margin-inline-start: auto; padding: 1px 6px; border-radius: 4px; background: var(--ui-subtle); color: var(--ui-muted); font-size: 11px; }
.work-status[data-state='running'] { background: var(--ui-info-soft); color: var(--ui-info); }
.work-status[data-state='completed'] { background: var(--ui-success-soft); color: var(--ui-success); }
.work-status[data-state='failed'] { background: var(--ui-danger-soft); color: var(--ui-danger); }
.work-status[data-state='waiting_approval'] { background: var(--ui-info-soft); color: var(--ui-info); }
.work-deps { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-top: 3px; color: var(--ui-muted); font-size: 11px; }
.work-deps em { padding: 1px 5px; border-radius: 4px; background: var(--ui-subtle); color: var(--ui-secondary); font-style: normal; }
.work-summary { margin-top: 3px; color: var(--ui-secondary); font-size: 12px; overflow-wrap: anywhere; }
.work-check-failed { margin-top: 3px; color: var(--ui-danger); font-size: 12px; overflow-wrap: anywhere; }
.work-artifacts { margin-top: 3px; color: var(--ui-muted); font-size: 11px; }
.work-verification { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; padding: 7px 9px; border-radius: var(--ui-radius-md); background: var(--ui-success-soft); color: var(--ui-success); font-size: 12px; }
.work-verification[data-state='failed'], .work-verification[data-state='partial'] { background: var(--ui-danger-soft); color: var(--ui-danger); }
.work-artifact-count { margin-inline-start: auto; color: var(--ui-secondary); }
.work-steps > li.current .work-step-main { border-radius: var(--ui-radius-sm); outline: 1px dashed var(--ui-border-strong); outline-offset: 4px; }
@media (prefers-reduced-motion: reduce) { .work-spinner, .spin { animation: none; } }
</style>
