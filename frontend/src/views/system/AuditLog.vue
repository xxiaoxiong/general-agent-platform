<template>
  <div class="page-container audit-page">
    <PageHeader title="审计日志" description="按操作者和资源回查平台请求与数据变更。" />

    <section class="data-surface audit-filters" aria-label="审计筛选">
      <div class="event-switch" role="group" aria-label="审计类型">
        <button v-for="category in categories" :key="category.key" type="button"
          :aria-pressed="filters.event_type === category.key" @click="chooseCategory(category.key)">
          <component :is="category.icon" :size="15" aria-hidden="true" />{{ category.label }}
        </button>
      </div>
      <form @submit.prevent="applyFilters">
        <div class="filter-row">
          <label class="filter-field">
            <span>操作者</span>
            <el-input v-model="filters.username" aria-label="操作者" placeholder="用户名" maxlength="50" clearable>
              <template #prefix><Search :size="15" aria-hidden="true" /></template>
            </el-input>
          </label>
          <label class="filter-field">
            <span>模块</span>
            <el-select v-model="filters.module" aria-label="模块" placeholder="全部模块" filterable allow-create clearable>
              <el-option v-for="(label, value) in auditModuleLabels" :key="value" :label="label" :value="value" />
            </el-select>
          </label>
          <label class="filter-field">
            <span>结果</span>
            <el-select v-model="filters.status" aria-label="结果" placeholder="全部结果" clearable>
              <el-option label="成功" value="success" />
              <el-option label="失败" value="failed" />
              <el-option label="拒绝" value="denied" />
            </el-select>
          </label>
          <div class="filter-field period-field">
            <span>时间范围</span>
            <el-date-picker v-model="dateRange" type="datetimerange" range-separator="至"
              start-placeholder="开始时间" end-placeholder="结束时间" class="audit-period" />
          </div>
          <div class="filter-actions">
            <UiButton variant="primary" type="submit" :loading="loading">查询</UiButton>
            <UiButton variant="ghost" @click="resetFilters">重置</UiButton>
          </div>
        </div>
        <details ref="advancedFilters" class="advanced-filters">
          <summary><ListFilter :size="14" aria-hidden="true" />高级筛选</summary>
          <div class="advanced-row">
            <label class="filter-field"><span>操作代码</span>
              <el-input v-model="filters.action" aria-label="操作代码" placeholder="例如 update_conversation" maxlength="100" clearable />
            </label>
            <label class="filter-field"><span>资源类型</span>
              <el-input v-model="filters.resource_type" aria-label="资源类型" placeholder="例如 agent_sessions" maxlength="50" clearable />
            </label>
            <label class="filter-field"><span>资源 ID</span>
              <el-input v-model="filters.resource_id" aria-label="资源 ID" maxlength="100" clearable />
            </label>
            <label class="filter-field"><span>请求 ID</span>
              <el-input v-model="filters.request_id" aria-label="请求 ID" maxlength="64" clearable />
            </label>
          </div>
        </details>
      </form>
    </section>

    <section class="data-surface records" aria-label="审计记录" :aria-busy="loading">
      <div class="records-heading">
        <h2>审计记录</h2>
        <span v-if="!loadError" aria-live="polite">{{ loading ? '正在加载…' : `${total.toLocaleString()} 条记录` }}</span>
      </div>
      <ErrorState v-if="loadError" title="无法加载审计日志" @retry="loadData" />
      <div v-else-if="loading && !rows.length" class="records-state" role="status">
        <LoaderCircle :size="18" class="animate-spin motion-reduce:animate-none" aria-hidden="true" />正在读取审计记录…
      </div>
      <EmptyState v-else-if="!rows.length" title="没有符合条件的审计记录" description="可以调整筛选条件，或重置后查看全部记录。" :icon="ClipboardList" />
      <template v-else>
        <div class="table-scroll">
          <table class="audit-table">
            <thead>
              <tr>
                <th scope="col">操作</th><th scope="col">操作者</th><th scope="col" class="resource-column">资源</th>
                <th scope="col">结果</th><th scope="col" class="source-column">来源 IP</th><th scope="col" class="time-column">时间</th>
                <th scope="col"><span class="sr-only">详情</span></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.id">
                <td class="operation-cell">
                  <button type="button" class="operation-name" :title="row.action"
                    :aria-label="`查看审计记录 ${row.id} ${row.actionLabel}`" @click="showDetail(row)">
                    {{ row.actionLabel }}
                  </button>
                  <div class="row-meta">
                    <span class="event-chip" :data-event="row.event_type || 'legacy'">{{ row.eventLabel }}</span>
                    <span>{{ row.moduleLabel }}</span>
                    <span v-if="row.changed_fields?.length" class="count-label">{{ row.changed_fields.length }} 个字段</span>
                  </div>
                  <time class="compact-time" :datetime="row.created_at">{{ row.timeLabel }}</time>
                </td>
                <td class="actor-cell">
                  <span class="actor-avatar" aria-hidden="true"><UserRound :size="15" /></span>
                  <div><span class="actor-name">{{ row.actorLabel }}</span><small v-if="row.username && row.user_id != null">ID {{ row.user_id }}</small></div>
                </td>
                <td class="resource-column">
                  <span class="resource-label">{{ row.resourceLabel }}</span>
                  <code v-if="row.resource_id" :title="row.resource_id" class="resource-id">{{ row.resource_id }}</code>
                </td>
                <td>
                  <span class="status-chip" :data-tone="row.outcome.tone"><i aria-hidden="true"></i>{{ row.outcome.text }}</span>
                  <small v-if="row.status_code != null" class="http-status">HTTP {{ row.status_code }}</small>
                </td>
                <td class="source-column"><code>{{ row.ip_address || '—' }}</code></td>
                <td class="time-column"><time :datetime="row.created_at">{{ row.timeLabel }}</time></td>
                <td class="detail-cell">
                  <UiButton variant="ghost" size="icon" :aria-label="`审计详情 #${row.id}`" title="查看详情" @click="showDetail(row)">
                    <ChevronRight />
                  </UiButton>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <nav v-if="pageCount > 1" class="records-pagination" aria-label="审计记录分页">
          <span>第 {{ page }} 页，共 {{ pageCount }} 页</span>
          <div>
            <UiButton size="sm" :disabled="page <= 1 || loading" @click="goToPage(page - 1)"><ChevronLeft />上一页</UiButton>
            <UiButton size="sm" :disabled="page >= pageCount || loading" @click="goToPage(page + 1)">下一页<ChevronRight /></UiButton>
          </div>
        </nav>
      </template>
    </section>

    <el-drawer v-model="detailVisible" title="审计详情" size="min(780px, 100vw)" destroy-on-close>
      <article v-if="selected" class="audit-detail" aria-label="操作详情">
        <header class="detail-overview">
          <div class="event-chip" :data-event="selected.event_type || 'legacy'">{{ selected.eventLabel }}</div>
          <div class="detail-heading"><h2>{{ selected.actionLabel }}</h2>
            <span class="status-chip" :data-tone="selected.outcome.tone"><i aria-hidden="true"></i>{{ selected.outcome.text }}</span>
          </div>
          <p>{{ selected.moduleLabel }} · {{ selected.timeLabel }}</p>
        </header>

        <dl class="detail-metadata">
          <div><dt>操作者</dt><dd>{{ selected.actorLabel }}<small v-if="selected.username && selected.user_id != null">ID {{ selected.user_id }}</small></dd></div>
          <div><dt>资源</dt><dd>{{ selected.resourceLabel }} <code v-if="selected.resource_id">{{ selected.resource_id }}</code><small v-if="selected.resource_type && selected.resource_type !== selected.resourceLabel">{{ selected.resource_type }}</small></dd></div>
          <div><dt>来源 IP</dt><dd><code>{{ selected.ip_address || '未记录' }}</code></dd></div>
          <div><dt>处理结果</dt><dd>{{ selected.outcome.text }}<small v-if="selected.status_code != null">HTTP {{ selected.status_code }}</small><small v-if="selected.duration_ms != null">{{ selected.duration_ms }} ms</small></dd></div>
          <div v-if="selected.request_path || selected.http_method" class="wide-meta"><dt>请求</dt>
            <dd><code class="request-route"><strong v-if="selected.http_method">{{ selected.http_method }}</strong><span>{{ selected.request_path || '—' }}</span></code></dd>
          </div>
          <div class="wide-meta"><dt>操作代码</dt><dd><code>{{ selected.action }}</code></dd></div>
          <div v-if="selected.request_id" class="wide-meta request-meta"><dt>请求 ID</dt>
            <dd><code>{{ selected.request_id }}</code><UiButton size="sm" variant="ghost" @click="showRelated(selected.request_id)"><Link2 />查看同一请求</UiButton></dd>
          </div>
          <div v-if="detail?.user_agent" class="wide-meta"><dt>客户端</dt><dd>{{ detail.user_agent }}</dd></div>
        </dl>

        <section class="snapshot-section" aria-label="操作前后数据">
          <div class="snapshot-heading">
            <h3>操作前后数据</h3>
            <div v-if="hasSnapshot && changedCount && changedCount < fields.length" class="diff-switch" role="group" aria-label="字段显示范围">
              <button type="button" :aria-pressed="onlyChanged" @click="onlyChanged = true">仅变更</button>
              <button type="button" :aria-pressed="!onlyChanged" @click="onlyChanged = false">全部字段</button>
            </div>
          </div>
          <div v-if="detailLoading" role="status" class="snapshot-state"><LoaderCircle :size="16" class="animate-spin motion-reduce:animate-none" aria-hidden="true" />正在读取操作详情…</div>
          <ErrorState v-else-if="detailError" title="无法加载操作详情" @retry="loadDetail" />
          <template v-else-if="hasSnapshot">
            <p class="snapshot-count">{{ changedCount ? `涉及 ${changedCount} 个字段` : '快照中的字段值一致' }}</p>
            <div v-if="displayedFields.length" class="field-compare">
              <div class="field-head"><span>字段</span><span>操作前</span><span>操作后</span></div>
              <div v-for="entry in displayedFields" :key="entry.field" class="field-row" :class="{ changed: entry.changed }">
                <div class="field-key">
                  <strong>{{ auditField(entry.field) }}</strong>
                  <code v-if="auditField(entry.field) !== entry.field">{{ entry.field }}</code>
                </div>
                <div class="field-value before-value" :class="{ 'missing-value': entry.from === undefined }">
                  <span class="stack-label">操作前</span><pre>{{ formatAuditValue(entry.from) }}</pre>
                </div>
                <div class="field-value after-value" :class="{ 'missing-value': entry.to === undefined }">
                  <span class="stack-label">操作后</span><pre>{{ formatAuditValue(entry.to) }}</pre>
                </div>
              </div>
            </div>
            <p v-else class="snapshot-state">此快照没有字段。</p>
          </template>
          <p v-else class="snapshot-state">
            {{ selected.event_type === 'request' ? (selected.request_id ? '此处记录请求结果。查看同一请求，可查找关联的数据变更。' : '此处记录请求结果。') : '前后数据未采集。' }}
          </p>
        </section>

        <div v-if="detail?.error_msg" class="error-detail"><strong>错误信息</strong><p>{{ detail.error_msg }}</p></div>
        <details v-if="detail?.detail && Object.keys(detail.detail).length" class="extra-detail">
          <summary>附加信息</summary><pre>{{ formatAuditValue(detail.detail) }}</pre>
        </details>
      </article>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { Activity, ChevronLeft, ChevronRight, ClipboardList, FileClock, GitCompareArrows, Link2, ListFilter, LoaderCircle, Search, UserRound } from '@lucide/vue'
import { auditApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import type { AuditLog, AuditLogDetail, AuditLogQuery } from '@/types'
import { auditAction, auditActor, auditEventLabels, auditField, auditFields, auditModule, auditModuleLabels, auditResource, auditStatus, formatAuditValue } from './auditPresentation'

type Category = NonNullable<AuditLogQuery['event_type']> | ''
const categories = [
  { key: '', label: '全部记录', icon: ClipboardList },
  { key: 'change', label: auditEventLabels.change, icon: GitCompareArrows },
  { key: 'request', label: auditEventLabels.request, icon: Activity },
  { key: 'legacy', label: auditEventLabels.legacy, icon: FileClock },
] as const

function emptyFilters() {
  return { username: '', module: '', status: '', action: '', resource_type: '', resource_id: '', request_id: '', event_type: '' as Category }
}
const filters = reactive(emptyFilters())
const applied = shallowRef<Omit<AuditLogQuery, 'page' | 'page_size'>>({})
const advancedFilters = ref<HTMLDetailsElement>()
const dateRange = ref<[Date, Date] | null>(null)
const logs = shallowRef<AuditLog[]>([])
const loading = ref(true)
const loadError = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 30
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
let listController: AbortController | undefined
let detailController: AbortController | undefined

const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

function formatDate(value: string) {
  if (!value) return '—'
  const date = new Date(/T/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value + 'Z' : value)
  return Number.isNaN(date.valueOf()) ? value : dateFormat.format(date)
}

function present(log: AuditLog) {
  return {
    ...log,
    actorLabel: auditActor(log),
    actionLabel: auditAction(log.action, log.resource_type),
    moduleLabel: auditModule(log.module),
    eventLabel: auditEventLabels[log.event_type ?? 'legacy'],
    resourceLabel: auditResource(log.resource_type),
    outcome: auditStatus(log.status),
    timeLabel: formatDate(log.created_at),
  }
}

const rows = computed(() => logs.value.map(present))
const detailVisible = ref(false)
const selectedRow = shallowRef<AuditLog>()
const detail = shallowRef<AuditLogDetail>()
const selected = computed(() => (detail.value || selectedRow.value) ? present((detail.value || selectedRow.value)!) : undefined)
const detailLoading = ref(false)
const detailError = ref(false)
const onlyChanged = ref(true)
const hasSnapshot = computed(() => !!(detail.value?.before_data || detail.value?.after_data))
const fields = computed(() => detail.value ? auditFields(detail.value, false) : [])
const changedCount = computed(() => fields.value.filter((item) => item.changed).length)
const displayedFields = computed(() => onlyChanged.value && changedCount.value ? fields.value.filter((item) => item.changed) : fields.value)

function chooseCategory(key: Category) {
  if (key === filters.event_type) return
  filters.event_type = key
  applyFilters()
}

function applyFilters() {
  applied.value = {
    username: filters.username.trim() || undefined,
    module: filters.module || undefined,
    status: filters.status || undefined,
    action: filters.action.trim() || undefined,
    resource_type: filters.resource_type.trim() || undefined,
    resource_id: filters.resource_id.trim() || undefined,
    request_id: filters.request_id.trim() || undefined,
    event_type: filters.event_type || undefined,
    start_time: dateRange.value?.[0]?.toISOString(),
    end_time: dateRange.value?.[1]?.toISOString(),
  }
  goToPage(1)
}

function resetFilters() {
  Object.assign(filters, emptyFilters())
  dateRange.value = null
  applyFilters()
}

function goToPage(number: number) {
  page.value = number
  void loadData()
}

async function loadData() {
  listController?.abort()
  const controller = new AbortController()
  listController = controller
  loading.value = true
  loadError.value = false
  try {
    const response = await auditApi.list({ ...applied.value, page: page.value, page_size: pageSize }, controller.signal)
    if (controller.signal.aborted) return
    logs.value = response.items
    total.value = response.total
  } catch {
    if (!controller.signal.aborted) loadError.value = true
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

function showDetail(log: AuditLog) {
  selectedRow.value = log
  detail.value = undefined
  onlyChanged.value = true
  detailVisible.value = true
  void loadDetail()
}

async function loadDetail() {
  if (!selectedRow.value) return
  detailController?.abort()
  const controller = new AbortController()
  detailController = controller
  detailLoading.value = true
  detailError.value = false
  try {
    const response = await auditApi.get(selectedRow.value.id, controller.signal)
    if (!controller.signal.aborted) detail.value = response
  } catch {
    if (!controller.signal.aborted) detailError.value = true
  } finally {
    if (!controller.signal.aborted) detailLoading.value = false
  }
}

function showRelated(requestId: string) {
  Object.assign(filters, emptyFilters(), { request_id: requestId })
  dateRange.value = null
  if (advancedFilters.value) advancedFilters.value.open = true
  detailVisible.value = false
  applyFilters()
}

watch(detailVisible, (visible) => { if (!visible) detailController?.abort() })
onMounted(loadData)
onUnmounted(() => { listController?.abort(); detailController?.abort() })
</script>

<style scoped>
.audit-filters, .records { margin-bottom: var(--ui-space-4); }
.audit-filters { padding: 14px 16px; }
.event-switch { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 14px; padding-bottom: 11px; border-bottom: var(--ui-line); }
.event-switch button, .diff-switch button {
  min-height: 32px; display: inline-flex; align-items: center; gap: 6px; border: 0; border-radius: var(--ui-radius-md);
  padding: 5px 10px; background: transparent; color: var(--ui-secondary); cursor: pointer; font-weight: var(--ui-weight-medium);
}
.event-switch button:hover, .diff-switch button:hover { background: var(--ui-hover); }
.event-switch button[aria-pressed="true"], .diff-switch button[aria-pressed="true"] { color: var(--ui-accent); background: var(--ui-accent-soft); }
.filter-row, .advanced-row { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(120px, 1fr) minmax(100px, .75fr) minmax(305px, 2.2fr) auto; gap: 12px; align-items: end; }
.filter-field { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.filter-field > span, .advanced-filters { color: var(--ui-secondary); font-size: var(--ui-size-sm); }
.filter-field :deep(.el-select), .period-field :deep(.el-date-editor) { width: 100%; }
.filter-actions { display: flex; align-items: center; gap: 4px; }
.advanced-filters { margin-top: 10px; }
.advanced-filters summary { width: fit-content; cursor: pointer; }
.advanced-filters summary svg { display: inline-block; vertical-align: -2px; margin-inline-end: 5px; }
.advanced-row { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 11px; }
.records-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; border-bottom: var(--ui-line); }
.records-heading h2, .snapshot-heading h3 { margin: 0; color: var(--ui-text); font-size: var(--ui-size-base); font-weight: var(--ui-weight-semibold); }
.records-heading span, .records-pagination, .records-state { color: var(--ui-secondary); font-size: var(--ui-size-sm); }
.records-state { min-height: 180px; display: flex; align-items: center; justify-content: center; gap: 8px; }
.table-scroll { overflow-x: auto; }
.audit-table { width: 100%; border-collapse: collapse; table-layout: auto; text-align: start; }
.audit-table th, .audit-table td { padding: 10px 12px; text-align: start; }
.audit-table th { color: var(--ui-secondary); background: var(--ui-subtle); font-weight: var(--ui-weight-semibold); font-size: var(--ui-size-sm); white-space: nowrap; }
.audit-table td { border-top: var(--ui-line); color: var(--ui-secondary); }
.audit-table tbody tr:hover { background: var(--ui-bg); }
.audit-table .operation-cell { min-width: 180px; }
.operation-name { appearance: none; cursor: pointer; border: 0; background: transparent; padding: 0; color: var(--ui-text); font: inherit; font-weight: var(--ui-weight-semibold); text-align: start; overflow-wrap: anywhere; }
.operation-name:hover { color: var(--ui-accent); text-decoration: underline; text-underline-offset: 3px; }
.row-meta { margin-top: 4px; display: flex; flex-wrap: wrap; align-items: center; gap: 3px 7px; font-size: var(--ui-size-xs); }
.count-label, .actor-cell small, .http-status { color: var(--ui-muted); }
.event-chip, .status-chip { display: inline-flex; align-items: center; gap: 5px; width: fit-content; border-radius: var(--ui-radius-sm); padding: 2px 6px; white-space: nowrap; color: var(--ui-secondary); background: var(--ui-subtle); font-size: var(--ui-size-xs); line-height: 1.5; font-weight: var(--ui-weight-medium); }
.event-chip[data-event="change"] { color: var(--ui-accent); background: var(--ui-accent-soft); }
.event-chip[data-event="request"] { color: var(--ui-info); background: var(--ui-info-soft); }
.status-chip i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.status-chip[data-tone="success"] { color: var(--ui-success); background: var(--ui-success-soft); }
.status-chip[data-tone="danger"] { color: var(--ui-danger); background: var(--ui-danger-soft); }
.status-chip[data-tone="warning"] { color: var(--ui-warning); background: var(--ui-warning-soft); }
.actor-avatar { width: 28px; height: 28px; display: inline-grid; place-items: center; vertical-align: middle; border-radius: 50%; margin-inline-end: 7px; color: var(--ui-muted); background: var(--ui-subtle); }
.actor-cell > div { display: inline-block; vertical-align: middle; max-width: 125px; overflow-wrap: anywhere; }
.actor-name, .resource-label { color: var(--ui-text); }
.actor-cell small, .http-status { display: block; font-size: var(--ui-size-xs); }
.resource-id { display: block; max-width: 105px; text-overflow: ellipsis; white-space: nowrap; overflow: hidden; color: var(--ui-muted); }
.source-column code, .time-column time, .compact-time { white-space: nowrap; font-size: var(--ui-size-xs); }
.compact-time { display: none; margin-top: 4px; color: var(--ui-muted); }
.audit-table .detail-cell { text-align: end; width: 48px; }
.records-pagination { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 16px; border-top: var(--ui-line); }
.records-pagination > div { display: flex; gap: 6px; }
.audit-detail { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.detail-overview { padding: 15px 16px; border-radius: var(--ui-radius-lg); border: var(--ui-line); background: var(--ui-subtle); }
.detail-heading { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-block: 5px; }
.detail-heading h2 { margin: 0; font-size: var(--ui-size-lg); overflow-wrap: anywhere; color: var(--ui-text); font-weight: var(--ui-weight-semibold); }
.detail-overview p { margin: 0; font-size: var(--ui-size-sm); color: var(--ui-muted); }
.detail-metadata { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin: 0; padding: 0 2px; }
.detail-metadata > div { min-width: 0; }
.detail-metadata dt, .field-head, .stack-label { font-weight: var(--ui-weight-semibold); color: var(--ui-secondary); font-size: var(--ui-size-xs); }
.detail-metadata dd { margin: 3px 0 0; padding: 0; color: var(--ui-text); overflow-wrap: anywhere; }
.detail-metadata dd small { margin-inline-start: 7px; color: var(--ui-muted); font-size: var(--ui-size-xs); }
.wide-meta { grid-column: 1 / -1; }
.request-route strong { margin-inline-end: 8px; }
.request-meta dd { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
.request-meta code { min-width: 0; }
.snapshot-section { border: var(--ui-line); border-radius: var(--ui-radius-lg); overflow: hidden; }
.snapshot-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding: 10px 13px; border-bottom: var(--ui-line); }
.diff-switch { display: inline-flex; gap: 2px; padding: 2px; border-radius: var(--ui-radius-md); background: var(--ui-subtle); }
.diff-switch button { min-height: 27px; font-size: var(--ui-size-xs); }
.snapshot-state, .snapshot-count { display: flex; align-items: center; gap: 7px; margin: 0; padding: 12px 13px; color: var(--ui-secondary); font-size: var(--ui-size-sm); }
.snapshot-count { padding-block: 8px; }
.field-head, .field-row { display: grid; grid-template-columns: minmax(95px, .75fr) repeat(2, minmax(0, 1.65fr)); }
.field-head { background: var(--ui-subtle); }
.field-head > span, .field-row > div { padding: 8px 10px; min-width: 0; }
.field-row { border-top: var(--ui-line); }
.field-row > div + div, .field-head > span + span { border-inline-start: var(--ui-line); }
.field-key { font-size: var(--ui-size-sm); color: var(--ui-secondary); overflow-wrap: anywhere; }
.field-key code { display: block; margin-top: 2px; color: var(--ui-muted); font-size: var(--ui-size-xs); }
.field-value pre, .extra-detail pre { margin: 0; max-height: 180px; overflow: auto; font: var(--ui-size-xs)/1.6 var(--ui-font-mono); white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ui-text); }
.field-row.changed .before-value { background: var(--ui-danger-soft); }
.field-row.changed .after-value { background: var(--ui-success-soft); }
.field-value.missing-value pre { color: var(--ui-muted); }
.field-row.changed .missing-value { background: var(--ui-subtle); }
.stack-label { display: none; }
.error-detail, .extra-detail { padding: 10px 12px; border-radius: var(--ui-radius-md); overflow-wrap: anywhere; }
.error-detail { color: var(--ui-danger); background: var(--ui-danger-soft); }
.error-detail p { margin: 4px 0 0; white-space: pre-wrap; }
.extra-detail { background: var(--ui-subtle); color: var(--ui-secondary); }
.extra-detail pre { margin-top: 8px; }
.extra-detail summary { cursor: pointer; }

@media (max-width: 1150px) {
  .filter-row { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .period-field { grid-column: span 2; }
  .source-column { display: none; }
}
@media (max-width: 760px) {
  .filter-row, .advanced-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .resource-column, .time-column { display: none; }
  .compact-time { display: block; }
  .actor-avatar { display: none; }
  .audit-table th, .audit-table td { padding-inline: 8px; }
  .audit-table .operation-cell { min-width: 120px; }
  .actor-cell > div { max-width: 85px; }
}
@media (max-width: 490px) {
  .audit-filters { padding-inline: 11px; }
  .period-field { grid-column: 1 / -1; }
  .event-switch button { font-size: var(--ui-size-xs); padding-inline: 6px; }
  .field-head { display: none; }
  .field-row { grid-template-columns: 1fr 1fr; }
  .field-key { grid-column: 1 / -1; border-bottom: var(--ui-line); }
  .field-row .before-value { border-inline-start: 0; }
  .stack-label { display: block; margin-bottom: 3px; }
  .detail-metadata { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
}
</style>
