<template>
  <div class="page-container">
    <PageHeader title="数据库管理" description="管理数据源连接，测试连通性并打开查询工作区。">
      <template #actions><UiButton variant="primary" @click="openDialog()"><Plus />添加连接</UiButton></template>
    </PageHeader>

    <div class="data-surface">
      <ErrorState v-if="loadError" title="无法加载数据库连接" @retry="loadConnections" />
      <EmptyState v-else-if="!loading && !connections.length" :icon="Database" title="暂无数据库连接" description="添加连接以查看表结构并执行查询。">
        <UiButton @click="openDialog()"><Plus />添加连接</UiButton>
      </EmptyState>
      <el-table v-else :data="connections" v-loading="loading">
        <el-table-column label="连接名称" min-width="180">
          <template #default="{ row }">
            <RouterLink class="resource-name connection-link" :to="`/databases/${row.id}/query`">{{ row.name }}</RouterLink>
            <p v-if="row.description" class="resource-meta">{{ row.description }}</p>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="115">
          <template #default="{ row }"><el-tag size="small" type="info">{{ row.db_type.toUpperCase() }}</el-tag></template>
        </el-table-column>
        <el-table-column label="目标" min-width="205" show-overflow-tooltip>
          <template #default="{ row }">
            <p class="mono">{{ row.database }}</p>
            <p v-if="row.db_type !== 'sqlite'" class="resource-meta mono">{{ row.host }}:{{ row.port }}</p>
          </template>
        </el-table-column>
        <el-table-column label="最近测试" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.last_tested_at" :type="row.last_test_ok ? 'success' : 'danger'" size="small">
              {{ row.last_test_ok ? '已连通' : '连接失败' }}
            </el-tag>
            <span v-else class="resource-meta">未测试</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="235">
          <template #default="{ row }">
            <div class="connection-actions">
              <el-button size="small" link :loading="testingId === row.id" @click="testConn(row)">测试连接</el-button>
              <el-button size="small" link type="primary" @click="$router.push(`/databases/${row.id}/query`)">查询</el-button>
              <el-button size="small" link @click="openDialog(row)">编辑</el-button>
              <el-button size="small" link type="danger" @click="deleteConn(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 添加/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editingConn ? '编辑连接' : '添加连接'" width="520px" destroy-on-close>
      <el-form :model="form" label-width="90px" :rules="rules" ref="formRef">
        <el-form-item label="连接名称" prop="name">
          <el-input v-model="form.name" placeholder="如: 生产数据库" />
        </el-form-item>
        <el-form-item label="数据库类型" prop="db_type">
          <el-select v-model="form.db_type" @change="onDbTypeChange">
            <el-option label="MySQL" value="mysql" />
            <el-option label="PostgreSQL" value="postgresql" />
            <el-option label="SQLite" value="sqlite" />
          </el-select>
        </el-form-item>
        <el-form-item label="主机地址" prop="host" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.host" placeholder="localhost" />
        </el-form-item>
        <el-form-item label="端口" prop="port" v-if="form.db_type !== 'sqlite'">
          <el-input-number v-model="form.port" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item :label="form.db_type === 'sqlite' ? '文件路径' : '数据库名'" prop="database">
          <el-input v-model="form.database" :placeholder="form.db_type === 'sqlite' ? '/path/to/db.sqlite' : 'my_database'" />
        </el-form-item>
        <el-form-item label="用户名" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.username" placeholder="root" />
        </el-form-item>
        <el-form-item label="密码" v-if="form.db_type !== 'sqlite'">
          <el-input v-model="form.password" type="password" show-password placeholder="密码" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选：描述此连接用途" />
        </el-form-item>
        <el-form-item label="额外参数">
          <el-input v-model="form.extra_params" placeholder="charset=utf8mb4&connect_timeout=10" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm" :loading="submitting">{{ editingConn ? '保存' : '创建' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Database, Plus } from '@lucide/vue'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { databaseApi } from '@/api'

const connections = ref<any[]>([])
const loading = ref(true)
const loadError = ref(false)
const dialogVisible = ref(false)
const editingConn = ref<any>(null)
const testingId = ref<number | null>(null)
const submitting = ref(false)
const formRef = ref()

const defaultForm = () => ({
  name: '',
  db_type: 'mysql',
  host: 'localhost',
  port: 3306,
  database: '',
  username: 'root',
  password: '',
  description: '',
  extra_params: '',
})
const form = ref(defaultForm())

const rules = {
  name: [{ required: true, message: '请输入连接名称', trigger: 'blur' }],
  db_type: [{ required: true, message: '请选择数据库类型', trigger: 'change' }],
  database: [{ required: true, message: '请输入数据库名', trigger: 'blur' }],
}

function onDbTypeChange(val: string) {
  if (val === 'mysql') form.value.port = 3306
  else if (val === 'postgresql') form.value.port = 5432
}

async function loadConnections() {
  loading.value = true
  loadError.value = false
  try {
    const res = await databaseApi.list()
    connections.value = res.items || []
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function openDialog(conn?: any) {
  if (conn) {
    editingConn.value = conn
    form.value = { ...conn }
  } else {
    editingConn.value = null
    form.value = defaultForm()
  }
  dialogVisible.value = true
}

async function submitForm() {
  try {
    await formRef.value?.validate()
  } catch { return }

  submitting.value = true
  try {
    if (editingConn.value) {
      await databaseApi.update(editingConn.value.id, form.value)
      ElMessage.success('已更新')
    } else {
      await databaseApi.create(form.value)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await loadConnections()
  } catch (e: any) {
    ElMessage.error(e?.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

async function testConn(conn: any) {
  testingId.value = conn.id
  try {
    const res = await databaseApi.test(conn.id)
    if (res.ok) {
      ElMessage.success('连接成功')
    } else {
      ElMessage.error(res.message || '连接失败')
    }
    await loadConnections()
  } catch (e: any) {
    ElMessage.error(e?.message || '测试失败')
  } finally {
    testingId.value = null
  }
}

async function deleteConn(conn: any) {
  try {
    await confirmDestructive(`确定删除连接「${conn.name}」？`, '删除数据库连接')
    await databaseApi.delete(conn.id)
    ElMessage.success('已删除')
    await loadConnections()
  } catch { /* cancel */ }
}

onMounted(loadConnections)
</script>

<style scoped>
.connection-link { text-decoration: none; }
.connection-link:hover { color: var(--ui-accent); text-decoration: underline; }
.connection-actions { display: flex; align-items: center; gap: 12px; }
.connection-actions :deep(.el-button) { margin: 0; }
</style>
