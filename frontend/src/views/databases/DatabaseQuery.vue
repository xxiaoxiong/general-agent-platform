<template>
  <div class="page-container">
    <PageHeader :title="connInfo?.name || '数据库查询'" description="查看表结构并执行只读 SELECT 查询。">
      <template #eyebrow><RouterLink to="/databases">数据库管理</RouterLink></template>
    </PageHeader>

    <ErrorState v-if="connectionError" title="无法加载数据库连接" @retry="loadConnection" />
    <div v-else class="query-grid" :aria-busy="loadingConnection">
      <aside>
        <el-card shadow="never" class="schema-card">
          <template #header><span class="card-title">表结构</span></template>
          <el-skeleton v-if="loadingSchema" :rows="3" animated />
          <ErrorState v-else-if="schemaError" title="无法读取表结构" @retry="loadSchema" />
          <div v-else>
            <button v-for="t in tables" :key="t" type="button" class="table-item" :aria-pressed="selectedTable === t" @click="selectTable(t)">
              <Table2 :size="15" /> <span>{{ t }}</span>
            </button>
            <div v-if="!tables.length" class="resource-meta">暂无表</div>
          </div>
          <!-- 列信息 -->
          <div v-if="selectedTable" class="columns-panel" :aria-busy="loadingColumns">
            <div class="columns-title">{{ selectedTable }} 列信息</div>
            <el-skeleton v-if="loadingColumns" :rows="2" animated />
            <p v-else-if="columnError" class="resource-meta" role="status">无法读取列信息，请重新选择表。</p>
            <div v-for="col in columns" :key="col.name" class="column-item">
              <span class="col-name">{{ col.name }}</span>
              <el-tag size="small" type="info">{{ col.type }}</el-tag>
              <el-tag v-if="col.key === 'PRI'" size="small" type="warning">PK</el-tag>
            </div>
          </div>
        </el-card>
      </aside>

      <section class="query-workbench" aria-label="SQL 查询">
        <el-card shadow="never" class="query-card">
          <template #header>
            <div class="sql-header">
              <span class="card-title">SQL 查询</span>
              <div class="sql-header-actions">
                <label for="query-limit" class="resource-meta">最大行数</label>
                <el-input-number id="query-limit" v-model="queryLimit" :min="1" :max="500" size="small" style="width:100px;" />
                <el-button type="primary" :icon="Play" @click="runQuery" :loading="querying" :disabled="!sqlInput.trim()">执行查询</el-button>
              </div>
            </div>
          </template>
          <el-input v-model="sqlInput" type="textarea" :rows="4" aria-label="SQL 查询语句" placeholder="输入 SELECT 查询语句…" class="sql-input" @keydown.ctrl.enter="runQuery" />

          <div v-if="queryError" class="query-error">
            <el-alert :title="queryError" type="error" :closable="false" show-icon />
          </div>

          <!-- 结果表格 -->
          <div v-if="resultColumns.length" class="result-section">
            <div class="result-header">
              <span>查询结果（{{ resultRows.length }} 行）</span>
            </div>
            <el-table :data="resultRows" stripe border max-height="460" style="width:100%;" size="small">
              <el-table-column v-for="col in resultColumns" :key="col" :prop="col" :label="col" min-width="120" show-overflow-tooltip />
            </el-table>
          </div>
        </el-card>

        <!-- 自然语言查询 -->
        <el-card shadow="never" class="nl-card">
          <template #header><span class="card-title">智能体查询上下文</span></template>
          <p class="resource-meta">将问题与当前表结构整理成上下文，可复制到智能体对话。</p>
          <div class="nl-input-row">
            <el-input v-model="nlQuestion" aria-label="查询问题" placeholder="例如：最近 7 天注册了多少用户？" @keydown.enter="prepareQueryContext" />
            <UiButton @click="prepareQueryContext" :loading="nlQuerying" :disabled="!nlQuestion.trim()">整理上下文</UiButton>
          </div>
          <div v-if="queryContext" class="nl-result">
            <el-input v-model="queryContext" class="sql-input" aria-label="查询上下文" type="textarea" :rows="4" readonly />
            <div class="nl-actions">
              <UiButton size="sm" @click="copyContext"><Copy />复制</UiButton>
              <UiButton size="sm" as-child><RouterLink to="/workspace">打开智能体对话</RouterLink></UiButton>
            </div>
          </div>
        </el-card>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { Copy, Play, Table2 } from '@lucide/vue'
import PageHeader from '@/components/layout/PageHeader.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import UiButton from '@/components/ui/UiButton.vue'
import { ElMessage } from 'element-plus'
import { databaseApi } from '@/api'

const route = useRoute()
const connId = Number(route.params.id)

const connInfo = ref<any>(null)
const loadingConnection = ref(true)
const connectionError = ref(false)
const tables = ref<string[]>([])
const selectedTable = ref('')
const columns = ref<any[]>([])
const loadingSchema = ref(false)
const schemaError = ref(false)
const columnError = ref(false)
const loadingColumns = ref(false)

const sqlInput = ref('')
const queryLimit = ref(100)
const querying = ref(false)
const queryError = ref('')
const resultColumns = ref<string[]>([])
const resultRows = ref<any[]>([])

const nlQuestion = ref('')
const nlQuerying = ref(false)
const queryContext = ref('')

async function loadConnection() {
  loadingConnection.value = true
  connectionError.value = false
  try {
    connInfo.value = await databaseApi.get(connId)
    await loadSchema()
  } catch {
    connectionError.value = true
  } finally {
    loadingConnection.value = false
  }
}

async function loadSchema() {
  loadingSchema.value = true
  schemaError.value = false
  try {
    const res = await databaseApi.getSchema(connId)
    tables.value = res.tables || []
  } catch {
    schemaError.value = true
  } finally {
    loadingSchema.value = false
  }
}

async function selectTable(table: string) {
  selectedTable.value = table
  columns.value = []
  loadingColumns.value = true
  columnError.value = false
  try {
    const res = await databaseApi.getSchema(connId, table)
    if (selectedTable.value === table) columns.value = res.columns || []
  } catch {
    if (selectedTable.value === table) columnError.value = true
  } finally {
    if (selectedTable.value === table) loadingColumns.value = false
  }
}

async function runQuery() {
  if (querying.value || !sqlInput.value.trim()) return
  querying.value = true
  queryError.value = ''
  resultColumns.value = []
  resultRows.value = []
  try {
    const res = await databaseApi.query(connId, { sql: sqlInput.value, limit: queryLimit.value })
    resultColumns.value = res.columns || []
    resultRows.value = res.rows || []
  } catch (e: any) {
    queryError.value = e?.response?.data?.detail || e?.message || '查询失败'
  } finally {
    querying.value = false
  }
}

async function prepareQueryContext() {
  if (nlQuerying.value || !nlQuestion.value.trim()) return
  const question = nlQuestion.value.trim()
  nlQuerying.value = true
  queryContext.value = ''
  try {
    // 获取表结构作为上下文
    let schemaContext = ''
    if (tables.value.length) {
      const tableDetails = []
      for (const t of tables.value.slice(0, 10)) {
        try {
          const r = await databaseApi.getSchema(connId, t)
          const cols = (r.columns || []).map((c: any) => `${c.name}(${c.type})`).join(', ')
          tableDetails.push(`${t}: ${cols}`)
        } catch { tableDetails.push(t) }
      }
      schemaContext = tableDetails.join('\n')
    }
    queryContext.value = schemaContext ? `${question}\n\n表结构\n${schemaContext}` : question
  } catch (e: any) {
    ElMessage.error(e?.message || '生成失败')
  } finally {
    nlQuerying.value = false
  }
}

async function copyContext() {
  try {
    await navigator.clipboard.writeText(queryContext.value)
    ElMessage.success('已复制查询上下文')
  } catch {
    ElMessage.warning('无法自动复制，可手动选中上下文')
  }
}

onMounted(loadConnection)
</script>

<style scoped>
.query-grid { display: grid; grid-template-columns: minmax(200px, 248px) minmax(0, 1fr); align-items: start; gap: var(--ui-space-4); }
.query-grid > * { min-width: 0; }
.card-title { font-weight: var(--ui-weight-semibold); }
.table-item {
  width: 100%; display: flex; align-items: center; gap: 8px; min-height: 34px; padding: 6px 8px;
  text-align: start; background: transparent; border: 0; outline-offset: -2px; cursor: pointer;
  border-radius: var(--ui-radius-sm); color: var(--ui-secondary);
}
.table-item span { overflow-wrap: anywhere; }
.table-item svg { flex: none; }
.table-item:hover { background: var(--ui-subtle); }
.table-item[aria-pressed="true"] { background: var(--ui-accent-soft); color: var(--ui-accent); }
.columns-panel { border-top: var(--ui-line); margin-top: var(--ui-space-3); padding-top: var(--ui-space-3); }
.columns-title { font-weight: var(--ui-weight-semibold); margin-bottom: var(--ui-space-2); overflow-wrap: anywhere; }
.column-item { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 4px 0; font-size: var(--ui-size-xs); }
.col-name { overflow-wrap: anywhere; font-family: var(--ui-font-mono); }
.sql-header, .sql-header-actions { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.sql-input :deep(.el-textarea__inner) { font-family: var(--ui-font-mono); }
.query-error, .result-section, .nl-card { margin-top: var(--ui-space-4); }
.result-header { font-size: var(--ui-size-sm); color: var(--ui-muted); margin-bottom: var(--ui-space-2); }
.nl-input-row, .nl-actions { display: flex; gap: var(--ui-space-2); margin-top: var(--ui-space-3); flex-wrap: wrap; }
.nl-input-row :deep(.el-input) { flex: 1 1 210px; }
.nl-result { margin-top: var(--ui-space-3); }
@media (max-width: 800px) {
  .query-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
