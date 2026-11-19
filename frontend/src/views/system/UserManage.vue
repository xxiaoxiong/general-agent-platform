<template>
  <div class="page-container">
    <PageHeader title="用户管理" description="管理平台账号、资料与启用状态。">
      <template #actions><UiButton variant="primary" @click="openDialog()"><Plus />新建用户</UiButton></template>
    </PageHeader>
    <form class="page-toolbar" @submit.prevent="applyFilters">
      <el-input v-model="keyword" aria-label="搜索用户" placeholder="搜索用户名或邮箱" clearable style="max-width:240px;" @clear="applyFilters">
        <template #prefix><Search :size="15" /></template>
      </el-input>
      <el-select v-model="filterActive" aria-label="状态" placeholder="全部状态" clearable style="width:132px;" @change="applyFilters">
        <el-option label="启用" :value="true" />
        <el-option label="禁用" :value="false" />
      </el-select>
      <UiButton type="submit" :loading="loading">搜索</UiButton>
    </form>

    <div class="data-surface">
      <ErrorState v-if="loadError" title="无法加载用户" @retry="loadData" />
      <el-table v-else :data="users" v-loading="loading" empty-text="没有符合条件的用户">
        <el-table-column label="用户" min-width="160">
          <template #default="{ row }">
            <div class="user-cell">
              <el-avatar :size="30" class="user-avatar">
                {{ row.username.charAt(0).toUpperCase() }}
              </el-avatar>
              <div>
                <div class="resource-name">{{ row.full_name || row.username }}</div>
                <div class="resource-meta">{{ row.username }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column prop="department" label="部门" width="120" />
        <el-table-column label="角色" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_superuser" type="primary" size="small">管理员</el-tag>
            <span v-else>成员</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="150">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" link @click="openDialog(row as User)">编辑</el-button>
              <el-button size="small" link :loading="busyUser === row.id" :type="row.is_active ? 'warning' : 'success'" @click="toggleActive(row as User)">
                {{ row.is_active ? '禁用' : '启用' }}
              </el-button>
              <el-button size="small" link type="danger" @click="deleteUser(row as User)">删除</el-button>
            </div>
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

    <!-- Dialog -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '新建用户'" width="480px">
      <el-form :model="form" label-position="top">
        <el-form-item label="用户名" v-if="!editing" required>
          <el-input v-model="form.username" placeholder="3–50 位字母、数字或下划线" />
        </el-form-item>
        <el-form-item label="邮箱" v-if="!editing" required>
          <el-input v-model="form.email" type="email" />
        </el-form-item>
        <el-form-item label="密码" v-if="!editing" required>
          <el-input v-model="form.password" type="password" autocomplete="new-password" placeholder="至少 8 位" show-password />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.full_name" />
        </el-form-item>
        <el-form-item label="部门">
          <el-input v-model="form.department" />
        </el-form-item>
        <el-form-item label="手机">
          <el-input v-model="form.phone" />
        </el-form-item>
        <el-form-item v-if="editing" label="状态">
          <el-switch v-model="form.is_active" active-text="启用" inactive-text="禁用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@lucide/vue'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { userApi } from '@/api'
import type { User } from '@/types'
import dayjs from 'dayjs'

const users = ref<User[]>([])
const loading = ref(true)
const loadError = ref(false)
const busyUser = ref<number>()
let request = 0
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const filterActive = ref<boolean | null>(null)

const dialogVisible = ref(false)
const editing = ref<User | null>(null)
const saving = ref(false)
const form = reactive({ username: '', email: '', password: '', full_name: '', department: '', phone: '', is_active: true })

function formatDate(d: string) { return dayjs(d).format('YYYY-MM-DD HH:mm') }

function applyFilters() {
  page.value = 1
  void loadData()
}

async function loadData() {
  const current = ++request
  loading.value = true
  loadError.value = false
  try {
    const res = await userApi.list({
      page: page.value, page_size: pageSize,
      keyword: keyword.value.trim() || undefined,
      is_active: filterActive.value ?? undefined,
    })
    if (current !== request) return
    users.value = res.items
    total.value = res.total
  } catch {
    if (current === request) loadError.value = true
  } finally {
    if (current === request) loading.value = false
  }
}

function openDialog(user?: User) {
  editing.value = user || null
  Object.assign(form, {
    username: '', email: '', password: '',
    full_name: user?.full_name || '', department: user?.department || '',
    phone: user?.phone || '', is_active: user?.is_active ?? true,
  })
  dialogVisible.value = true
}

async function save() {
  if (saving.value) return
  const { username, email, password, full_name, department, phone, is_active } = form
  if (!editing.value && !(username.trim() && email.trim() && password)) {
    ElMessage.warning('请填写用户名、邮箱和密码')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await userApi.update(editing.value.id, { full_name, department, phone, is_active })
    } else {
      await userApi.create({ username: username.trim(), email: email.trim(), password, full_name, department, phone })
    }
    ElMessage.success(editing.value ? '已更新用户' : '已创建用户')
    dialogVisible.value = false
    await loadData()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    saving.value = false
  }
}

async function toggleActive(user: User) {
  if (busyUser.value) return
  busyUser.value = user.id
  try {
    if (user.is_active) await ElMessageBox.confirm(
      `禁用「${user.username}」后，该用户无法登录平台。`, '禁用账号',
      { type: 'warning', confirmButtonText: '禁用', cancelButtonText: '取消' },
    )
    await userApi.update(user.id, { is_active: !user.is_active })
    ElMessage.success(user.is_active ? '已禁用' : '已启用')
    await loadData()
  } catch {
    // 取消确认后继续显示当前列表。
  } finally {
    busyUser.value = undefined
  }
}

async function deleteUser(user: User) {
  try {
    await ElMessageBox.confirm(
      `确认删除用户「${user.username}」？`, '删除用户',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
    await userApi.delete(user.id)
    if (users.value.length === 1 && page.value > 1) page.value -= 1
    ElMessage.success('已删除用户')
    await loadData()
  } catch {
    // 取消确认后继续显示当前列表。
  }
}

onMounted(loadData)
</script>

<style scoped>
.user-cell { display: flex; align-items: center; gap: 10px; }
.user-avatar { flex: none; background: var(--ui-accent-soft); color: var(--ui-accent); border-radius: var(--ui-radius-md); }
.action-btns { display: flex; flex-wrap: nowrap; gap: 10px; white-space: nowrap; }
.action-btns :deep(.el-button) { margin: 0; }
</style>
