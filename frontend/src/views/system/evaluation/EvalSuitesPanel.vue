<template>
  <el-tab-pane label="测评套件" name="suites">
    <div class="toolbar">
      <span class="hint">套件用于组合多个数据集并发布不可变版本，保证历史测评可复现。</span>
      <UiButton variant="primary" @click="openCreate">新建套件</UiButton>
    </div>
    <div class="data-surface">
      <el-table :data="suites" v-loading="sectionLoading" size="small" empty-text="暂无测评套件">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="name" label="套件名称" min-width="160" show-overflow-tooltip />
        <el-table-column label="数据集" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ datasetNames(row as EvalSuite) }}</template>
        </el-table-column>
        <el-table-column label="最新版本" width="190">
          <template #default="{ row }">
            <template v-if="row.latest_version">
              v{{ row.latest_version.version_no }} · {{ row.latest_version.total_items ?? '-' }} 条
              <div class="hint">{{ formatDate(row.latest_version.created_at) }}</div>
            </template>
            <span v-else class="hint">未发布</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="180" show-overflow-tooltip />
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="publish(row as EvalSuite)">发布版本</el-button>
            <el-button size="small" link @click="openEdit(row as EvalSuite)">编辑</el-button>
            <el-button size="small" link type="danger" @click="remove(row as EvalSuite)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑套件' : '新建套件'" width="560px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="120" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="数据集" required>
          <el-select v-model="form.dataset_ids" multiple style="width:100%"
            placeholder="按执行顺序选择数据集">
            <el-option v-for="d in datasets" :key="d.id"
              :label="`${d.name}（${d.item_count ?? 0} 条）`" :value="d.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </el-tab-pane>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import UiButton from '@/components/ui/UiButton.vue'
import {
  evaluationApi, type EvalDataset, type EvalSuite,
} from '@/api/evaluation'
import { formatDate, confirmAction } from './shared'

defineProps<{ sectionLoading: boolean }>()

const suites = ref<EvalSuite[]>([])
const datasets = ref<EvalDataset[]>([])
const dialogVisible = ref(false)
const saving = ref(false)
const editing = ref<EvalSuite | null>(null)
const form = ref<{ name: string; description: string; dataset_ids: number[] }>({
  name: '', description: '', dataset_ids: [],
})

function datasetName(id: number) {
  return datasets.value.find(d => d.id === id)?.name || `#${id}`
}
function datasetNames(row: EvalSuite) {
  return (row.dataset_ids || []).map(datasetName).join(' → ') || '-'
}

async function loadAll() {
  const [suiteRes, datasetRes] = await Promise.all([
    evaluationApi.listSuites(), evaluationApi.listDatasets(),
  ])
  suites.value = suiteRes.items
  datasets.value = datasetRes.items
}

function openCreate() {
  editing.value = null
  form.value = { name: '', description: '', dataset_ids: [] }
  dialogVisible.value = true
}
function openEdit(row: EvalSuite) {
  editing.value = row
  form.value = {
    name: row.name, description: row.description || '',
    dataset_ids: [...(row.dataset_ids || [])],
  }
  dialogVisible.value = true
}
async function submit() {
  if (!form.value.name.trim() || !form.value.dataset_ids.length) {
    ElMessage.warning('请填写名称并至少选择一个数据集')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await evaluationApi.updateSuite(editing.value.id, form.value)
    } else {
      await evaluationApi.createSuite(form.value)
    }
    dialogVisible.value = false
    ElMessage.success('已保存')
    await loadAll()
  } finally {
    saving.value = false
  }
}
async function publish(row: EvalSuite) {
  saving.value = true
  try {
    const version = await evaluationApi.publishSuiteVersion(row.id)
    ElMessage.success(`已发布 v${version.version_no}`)
    await loadAll()
  } finally {
    saving.value = false
  }
}
async function remove(row: EvalSuite) {
  if (!await confirmAction(`确认删除套件「${row.name}」？`, '删除套件', true)) return
  try {
    await evaluationApi.deleteSuite(row.id)
    ElMessage.success('已删除')
    await loadAll()
  } catch {
    // 请求拦截器统一提示
  }
}

defineExpose({ loadAll })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.hint { color: var(--ui-muted); font-size: var(--ui-size-sm); }
</style>
