<template>
  <div class="page-container">
    <PageHeader title="审计日志" description="按操作者、模块和时间范围回查平台活动。" />
    <form class="page-toolbar" @submit.prevent="applyFilters">
        <el-input v-model="filters.username" aria-label="操作者" placeholder="用户名" clearable style="width:140px;" />
        <el-select v-model="filters.module" aria-label="模块" placeholder="全部模块" clearable style="width:120px;">
          <el-option label="认证" value="auth" />
          <el-option label="用户" value="user" />
          <el-option label="模型" value="model" />
          <el-option label="知识库" value="knowledge" />
          <el-option label="流程" value="workflow" />
          <el-option label="应用" value="app" />
        </el-select>
        <el-select v-model="filters.status" aria-label="状态" placeholder="全部状态" clearable style="width:100px;">
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          class="audit-period"
        />
        <UiButton variant="primary" type="submit" :loading="loading">查询</UiButton>
        <UiButton @click="resetFilters">重置</UiButton>
    </form>

    <div class="data-surface">
      <ErrorState v-if="loadError" title="无法加载审计日志" @retry="loadData" />
      <el-table v-else :data="logs" v-loading="loading" empty-text="没有符合条件的审计记录">
        <el-table-column prop="username" label="用户" width="140" />
        <el-table-column prop="action" label="操作" min-width="145" show-overflow-tooltip />
        <el-table-column prop="module" label="模块" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.module }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resource_type" label="资源类型" width="120" />
        <el-table-column prop="resource_id" label="资源ID" width="100" />
        <el-table-column prop="ip_address" label="IP" width="120" />
        <el-table-column label="状态" width="70">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="120">
          <template #default="{ row }">
            {{ row.duration_ms != null ? row.duration_ms + ' ms' : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="时间" width="175">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="详情" width="100">
          <template #default="{ row }">
            <el-button size="small" link @click="showDetail(row as AuditLog)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="table-pagination" v-if="!loadError && total > 0">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next" :pager-count="5"
          @current-change="loadData"
        />
      </div>
    </div>

    <!-- Detail Dialog -->
    <el-drawer v-model="detailVisible" title="日志详情" size="480px">
      <el-descriptions :column="1" border size="small" v-if="detailLog">
        <el-descriptions-item label="用户">{{ detailLog.username }}</el-descriptions-item>
        <el-descriptions-item label="操作">{{ detailLog.action }}</el-descriptions-item>
        <el-descriptions-item label="模块">{{ detailLog.module }}</el-descriptions-item>
        <el-descriptions-item label="IP">{{ detailLog.ip_address }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ detailLog.status }}</el-descriptions-item>
        <el-descriptions-item label="错误信息" v-if="detailLog.error_msg">{{ detailLog.error_msg }}</el-descriptions-item>
        <el-descriptions-item label="时间">{{ formatDate(detailLog.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="详细数据" v-if="detailLog.detail">
          <pre class="log-payload">{{ JSON.stringify(detailLog.detail, null, 2) }}</pre>
        </el-descriptions-item>
      </el-descriptions>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { auditApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import type { AuditLog } from '@/types'
import dayjs from 'dayjs'

const logs = ref<AuditLog[]>([])
const loading = ref(true)
const loadError = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 50
const dateRange = ref<[Date, Date] | null>(null)

const filters = reactive({
  username: '',
  module: '',
  status: '',
})

const detailVisible = ref(false)
const detailLog = ref<AuditLog | null>(null)

function formatDate(d: string) { return dayjs(d).format('YYYY-MM-DD HH:mm:ss') }

function showDetail(log: AuditLog) {
  detailLog.value = log
  detailVisible.value = true
}

function resetFilters() {
  filters.username = ''
  filters.module = ''
  filters.status = ''
  dateRange.value = null
  applyFilters()
}

function applyFilters() {
  page.value = 1
  void loadData()
}

async function loadData() {
  loading.value = true
  loadError.value = false
  try {
    const params: any = {
      page: page.value,
      page_size: pageSize,
      username: filters.username || undefined,
      module: filters.module || undefined,
      status: filters.status || undefined,
    }
    if (dateRange.value) {
      params.start_time = dateRange.value[0].toISOString()
      params.end_time = dateRange.value[1].toISOString()
    }
    const res = await auditApi.list(params)
    logs.value = res.items
    total.value = res.total
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.page-toolbar .audit-period { width: min(360px, 100%); }
.log-payload { margin: 0; font-size: var(--ui-size-xs); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 280px; }
</style>
