<template>
  <section class="team-card" aria-label="智能体团队进度">
    <header class="team-head">
      <span class="team-symbol"><UsersRound :size="19" aria-hidden="true" /></span>
      <div class="team-title">
        <h3>智能体团队</h3>
        <p role="status" :data-phase="progress.phase">
          <LoaderCircle v-if="active && !finished" class="phase-spinner" :size="13" aria-hidden="true" />
          {{ phaseText }}
        </p>
      </div>
      <div v-if="progress.tasks.length" class="team-count">
        <span>{{ terminalCount }} / {{ progress.tasks.length }} 项已处理</span>
        <progress :value="terminalCount" :max="progress.tasks.length" aria-label="已处理任务数"></progress>
      </div>
    </header>
    <div class="team-board">
      <div class="lead-row">
        <span class="lead-symbol"><UserRound :size="15" aria-hidden="true" /></span>
        <div><strong>{{ progress.lead }}</strong><p>分派任务、协调依赖并汇总结果</p></div>
      </div>
      <p v-if="progress.summary" class="team-summary">{{ progress.summary }}</p>
      <ol v-if="progress.tasks.length" class="team-tasks" aria-label="团队角色与分工">
        <li v-for="task in progress.tasks" :key="task.id">
          <details class="team-task">
            <summary>
              <ChevronRight class="task-chevron" :size="15" aria-hidden="true" />
              <span class="task-labels">
                <span class="task-role">{{ task.role }}</span>
                <strong>{{ task.title }}</strong>
              </span>
              <span class="task-status" :data-state="task.status"><i aria-hidden="true"></i>{{ teamTaskLabels[task.status] }}</span>
            </summary>
            <div class="task-content">
              <p v-if="task.brief" class="task-brief">{{ task.brief }}</p>
              <div v-if="task.depends_on.length" class="task-dependencies">
                <GitBranch :size="14" aria-hidden="true" />
                <span>依赖</span>
                <ul aria-label="依赖任务">
                  <li v-for="id in task.depends_on" :key="id">{{ taskTitles.get(id) ?? id }}</li>
                </ul>
              </div>
              <p v-if="task.error" class="task-error">{{ task.error }}</p>
              <div v-if="task.result" class="task-result">
                <h4>角色成果</h4>
                <MessageMarkdown :text="task.result" />
              </div>
            </div>
          </details>
        </li>
      </ol>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, GitBranch, LoaderCircle, UserRound, UsersRound } from '@lucide/vue'
import MessageMarkdown from './MessageMarkdown.vue'
import { teamPhaseLabels, teamTaskLabels, type TeamProgress } from './teamProgress'

const props = defineProps<{ progress: TeamProgress; active: boolean }>()
const finished = computed(() => ['completed', 'failed', 'cancelled'].includes(props.progress.phase))
const phaseText = computed(() => !props.active && !finished.value ? '等待同步团队进度' : teamPhaseLabels[props.progress.phase])
const taskTitles = computed(() => new Map(props.progress.tasks.map((task) => [task.id, task.title])))
const terminalCount = computed(() => props.progress.tasks.filter((task) => task.status !== 'pending' && task.status !== 'running').length)
</script>

<style scoped>
.team-card {
  min-width: 0; overflow: hidden; border: var(--ui-line); border-radius: var(--ui-radius-lg); background: var(--ui-surface);
}
.team-head, .lead-row { display: flex; align-items: center; gap: 10px; }
.team-head { padding: 11px 13px; background: var(--ui-subtle); border-bottom: var(--ui-line); flex-wrap: wrap; }
.team-symbol, .lead-symbol { display: grid; place-items: center; flex: none; border-radius: var(--ui-radius-md); }
.team-symbol { width: 36px; height: 36px; background: var(--ui-accent-soft); color: var(--ui-accent); }
.team-title { flex: 1 1 108px; min-width: 0; }
.team-title h3 { font-size: 14px; font-weight: 700; line-height: 1.4; }
.team-title p { display: flex; align-items: center; gap: 5px; color: var(--ui-secondary); font-size: 12px; }
.team-title p[data-phase='completed'] { color: var(--ui-success); }
.team-title p[data-phase='failed'] { color: var(--ui-danger); }
.phase-spinner { flex: none; animation: team-spin 1s linear infinite; }
@keyframes team-spin { to { transform: rotate(360deg); } }
.team-count { display: grid; gap: 4px; min-width: 88px; align-items: center; color: var(--ui-secondary); font-size: 11px; }
.team-count progress { width: 100%; height: 4px; accent-color: var(--ui-accent); }
.team-board { padding: 11px 13px 13px; }
.lead-row { color: var(--ui-text); font-size: 13px; }
.lead-row strong, .task-role, .task-labels strong { overflow-wrap: anywhere; }
.lead-row p, .team-summary { color: var(--ui-secondary); font-size: 12px; }
.lead-symbol { width: 30px; height: 30px; background: var(--ui-info-soft); color: var(--ui-info); }
.team-summary { margin: 7px 0 0 40px; overflow-wrap: anywhere; }
.team-tasks { margin: 9px 0 0 14px; padding: 0 0 0 16px; border-inline-start: 1px solid var(--ui-border-strong); list-style: none; display: grid; gap: 7px; }
.team-tasks > li { min-width: 0; position: relative; }
.team-tasks > li::before { content: ''; position: absolute; inset: 24px auto auto -16px; width: 15px; border-top: 1px solid var(--ui-border-strong); }
.team-task { border: var(--ui-line); border-radius: var(--ui-radius-md); }
.team-task summary { display: flex; align-items: center; min-width: 0; gap: 8px; padding: 8px 10px; list-style: none; cursor: pointer; border-radius: var(--ui-radius-md); }
.team-task summary::-webkit-details-marker { display: none; }
.team-task summary:hover { background: var(--ui-hover); }
.team-task summary:focus-visible { outline: var(--ui-focus); outline-offset: -2px; }
.task-chevron { flex: none; color: var(--ui-muted); transition: transform var(--ui-duration) var(--ui-ease); }
.team-task[open] .task-chevron { transform: rotate(90deg); }
.task-labels { flex: 1; min-width: 0; display: grid; gap: 1px; }
.task-labels strong { font-size: 13px; }
.task-role { color: var(--ui-muted); font-size: 11px; }
.task-status { display: inline-flex; align-items: center; gap: 4px; padding: 2px 6px; flex: none; border-radius: 4px; background: var(--ui-subtle); color: var(--ui-secondary); font-size: 11px; }
.task-status i { width: 6px; height: 6px; background: currentColor; border-radius: 50%; }
.task-status[data-state='running'] { background: var(--ui-info-soft); color: var(--ui-info); }
.task-status[data-state='completed'] { background: var(--ui-success-soft); color: var(--ui-success); }
.task-status[data-state='failed'] { background: var(--ui-danger-soft); color: var(--ui-danger); }
.task-content { padding: 8px 10px 11px 33px; border-top: var(--ui-line); color: var(--ui-secondary); font-size: 13px; overflow-wrap: anywhere; }
.task-content > * + * { margin-top: 7px; }
.task-brief { white-space: pre-wrap; }
.task-dependencies, .task-dependencies ul { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px; }
.task-dependencies svg { align-self: center; flex: none; }
.task-dependencies ul { padding: 0; list-style: none; }
.task-dependencies li { padding: 2px 5px; background: var(--ui-subtle); border-radius: 4px; font-size: 12px; }
.task-error { color: var(--ui-danger); white-space: pre-line; }
.task-result h4 { font-size: 12px; color: var(--ui-muted); font-weight: 600; }
.task-result :deep(.md-body) { font-size: 13px; }
@media (prefers-reduced-motion: reduce) { .phase-spinner { animation: none; } }
</style>
