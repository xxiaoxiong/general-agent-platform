<template>
  <!-- ── 数据集 ── -->
  <el-tab-pane label="数据集" name="datasets">
    <div class="toolbar">
      <el-button type="primary" @click="openDatasetDialog()">新建数据集</el-button>
      <el-button @click="importVisible = true">导入 JSONL</el-button>
      <el-button type="success" :loading="loading" @click="seedCommunity">导入社区通用数据集</el-button>
      <span class="hint">社区数据集：GSM8K / BBH（多步推理）/ BFCL（函数调用·对抗）/ IFEval（指令遵循）/ 工具链·多轮·对抗鲁棒 + 长程任务（工程构建 / 长文档生成修改 / 复杂数学 / 深度研究），标注复杂度 L1-L4</span>
    </div>
    <el-table :data="datasets" v-loading="sectionLoading || loading" empty-text="暂无数据集" size="small" stripe>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" min-width="180" show-overflow-tooltip />
      <el-table-column label="来源" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.source === 'community' ? 'success' : 'info'">
            {{ row.source === 'community' ? '社区' : row.source === 'import' ? '导入' : '手工' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="category" label="分类" width="100" />
      <el-table-column prop="scoring_method" label="默认评分" width="110" />
      <el-table-column prop="item_count" label="用例数" width="80" />
      <el-table-column prop="reference" label="出处" min-width="180" show-overflow-tooltip />
      <el-table-column label="操作" width="170">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="viewDataset(row as EvalDataset)">用例</el-button>
          <el-button size="small" link @click="openDatasetDialog(row as EvalDataset)">编辑</el-button>
          <el-button size="small" link type="danger" @click="deleteDataset(row as EvalDataset)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-tab-pane>

  <!-- 数据集详情（用例管理） -->
  <el-dialog v-model="datasetVisible" :title="`数据集用例：${currentDataset?.name || ''}`" width="860px" top="5vh">
    <div class="toolbar" v-if="currentDataset">
      <el-button type="primary" size="small" @click="openItemDialog()">添加用例</el-button>
      <span class="hint">默认评分：{{ currentDataset.scoring_method }}</span>
    </div>
    <el-table :data="currentDataset?.items || []" size="small" stripe max-height="480">
      <el-table-column prop="position" label="#" width="50" />
      <el-table-column label="复杂度" width="76">
        <template #default="{ row }">
          <el-tag :type="complexityTagType(row.complexity)" size="small">{{ row.complexity }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="任务类型" width="92">
        <template #default="{ row }">{{ taskTypeText[row.task_type] || row.task_type }}</template>
      </el-table-column>
      <el-table-column prop="input" label="输入" min-width="220" show-overflow-tooltip />
      <el-table-column prop="expected" label="期望" min-width="160" show-overflow-tooltip />
      <el-table-column prop="scoring_method" label="评分" width="100">
        <template #default="{ row }">{{ row.scoring_method || '（数据集默认）' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openItemDialog(row as EvalDatasetItem)">编辑</el-button>
          <el-button size="small" link type="danger" @click="deleteItem(row as EvalDatasetItem)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>

  <!-- 用例编辑 -->
  <el-dialog v-model="itemEditVisible" :title="itemForm.id ? '编辑用例' : '添加用例'" width="640px">
    <el-form :model="itemForm" label-width="90px">
      <el-form-item label="输入" required>
        <el-input v-model="itemForm.input" type="textarea" :rows="4" />
      </el-form-item>
      <el-form-item label="期望输出">
        <el-input v-model="itemForm.expected" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="评分标准">
        <el-input v-model="itemForm.criteria" type="textarea" :rows="2" placeholder="给 LLM 裁判的补充说明" />
      </el-form-item>
      <el-form-item label="复杂度">
        <el-select v-model="itemForm.complexity" style="width:100%;">
          <el-option label="L1 简单（直接回答）" value="L1" />
          <el-option label="L2 中等（单步工具/简单推理）" value="L2" />
          <el-option label="L3 复杂（多步推理/多工具协同）" value="L3" />
          <el-option label="L4 困难（长程复合/对抗鲁棒）" value="L4" />
        </el-select>
      </el-form-item>
      <el-form-item label="任务类型">
        <el-select v-model="itemForm.task_type" style="width:100%;">
          <el-option label="直接问答" value="direct_qa" />
          <el-option label="多步推理" value="multi_step_reasoning" />
          <el-option label="工具使用" value="tool_use" />
          <el-option label="指令遵循" value="instruction_following" />
          <el-option label="多轮任务" value="multi_turn" />
          <el-option label="对抗鲁棒" value="adversarial" />
        </el-select>
      </el-form-item>
      <el-form-item label="必需工具">
        <el-input v-model="itemForm.required_tools_text"
          placeholder="任务声明要求调用的工具名，逗号分隔（客观校验），如：calculator,datetime_now" />
      </el-form-item>
      <el-form-item label="期望步数">
        <el-input-number v-model="itemForm.expected_min_steps" :min="0" :max="20" />
        <span class="hint" style="margin-left:8px;">期望最少执行步数，0 = 不限</span>
      </el-form-item>
      <el-form-item label="多轮消息">
        <el-input v-model="itemForm.turns_text" type="textarea" :rows="3"
          placeholder="多轮任务：每行一条后续用户消息（「输入」为首轮）" />
      </el-form-item>
      <el-form-item label="评分方式">
        <el-select v-model="itemForm.scoring_method" clearable placeholder="数据集默认" style="width:100%;">
          <el-option v-for="m in scoringMethods" :key="m" :label="m" :value="m" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="itemEditVisible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="saveItem">保存</el-button>
    </template>
  </el-dialog>

  <!-- 数据集编辑 -->
  <el-dialog v-model="datasetEditVisible" :title="datasetForm.id ? '编辑数据集' : '新建数据集'" width="560px">
    <el-form :model="datasetForm" label-width="90px">
      <el-form-item label="名称" required>
        <el-input v-model="datasetForm.name" />
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="datasetForm.description" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="分类">
        <el-input v-model="datasetForm.category" placeholder="general / math / tool-use ..." />
      </el-form-item>
      <el-form-item label="默认评分">
        <el-select v-model="datasetForm.scoring_method" style="width:100%;">
          <el-option v-for="m in scoringMethods" :key="m" :label="m" :value="m" />
        </el-select>
      </el-form-item>
      <el-form-item label="出处">
        <el-input v-model="datasetForm.reference" placeholder="社区数据集 URL / 论文 / license" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="datasetEditVisible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="saveDataset">保存</el-button>
    </template>
  </el-dialog>

  <!-- JSONL 导入 -->
  <el-dialog v-model="importVisible" title="导入 JSONL 用例" width="640px">
    <el-form :model="importForm" label-width="90px">
      <el-form-item label="目标数据集">
        <el-select v-model="importForm.dataset_id" clearable placeholder="留空则新建数据集" style="width:100%;">
          <el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="新集名称">
        <el-input v-model="importForm.name" :disabled="!!importForm.dataset_id" placeholder="留空自动生成" />
      </el-form-item>
      <el-form-item label="JSONL 内容" required>
        <el-input v-model="importForm.content" type="textarea" :rows="10"
          placeholder='每行一个 JSON：{"input": "问题", "expected": "期望答案", "criteria": "评分标准"}' />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="importVisible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="submitImport">导入</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import {
  evaluationApi,
  type EvalDataset, type EvalDatasetItem,
} from '@/api/evaluation'
import { complexityTagType, taskTypeText, confirmAction } from './shared'

const props = defineProps<{
  loading: boolean
  sectionLoading: boolean
}>()
const emit = defineEmits<{
  'update:loading': [value: boolean]
}>()

// loading 为外壳公共状态（v-model），数据状态全部自管
const loading = computed({ get: () => props.loading, set: v => emit('update:loading', v) })

// ── 数据集 ──
const datasets = ref<EvalDataset[]>([])
const datasetVisible = ref(false)
const currentDataset = ref<EvalDataset | null>(null)
const datasetEditVisible = ref(false)
const datasetForm = ref({ id: 0, name: '', description: '', category: 'general', scoring_method: 'llm_judge', reference: '' })
const itemEditVisible = ref(false)
const itemForm = ref({
  id: 0, input: '', expected: '', criteria: '', scoring_method: '',
  complexity: 'L2', task_type: 'direct_qa',
  required_tools_text: '', expected_min_steps: 0, turns_text: '',
})
const importVisible = ref(false)
const importForm = ref({ dataset_id: undefined as number | undefined, name: '', content: '' })
const scoringMethods = ['llm_judge', 'exact_match', 'contains', 'keyword', 'regex', 'none']

// ── 加载 ──
async function loadDatasets() {
  datasets.value = (await evaluationApi.listDatasets()).items
}
// 分区数据加载（数据集分区），失败向上抛出
async function loadAll() {
  await loadDatasets()
}

// ── 数据集操作 ──
function openDatasetDialog(row?: EvalDataset) {
  datasetForm.value = row
    ? { id: row.id, name: row.name, description: row.description, category: row.category, scoring_method: row.scoring_method, reference: row.reference || '' }
    : { id: 0, name: '', description: '', category: 'general', scoring_method: 'llm_judge', reference: '' }
  datasetEditVisible.value = true
}
async function saveDataset() {
  loading.value = true
  try {
    if (datasetForm.value.id) {
      await evaluationApi.updateDataset(datasetForm.value.id, datasetForm.value)
    } else {
      await evaluationApi.createDataset(datasetForm.value)
    }
    datasetEditVisible.value = false
    ElMessage.success('已保存')
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function deleteDataset(row: EvalDataset) {
  if (!await confirmAction(`确认删除数据集「${row.name}」及其全部用例？`, '删除数据集', true)) return
  loading.value = true
  try {
    await evaluationApi.deleteDataset(row.id)
    ElMessage.success('已删除')
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function viewDataset(row: EvalDataset) {
  currentDataset.value = await evaluationApi.getDataset(row.id)
  datasetVisible.value = true
}
function openItemDialog(row?: EvalDatasetItem) {
  itemForm.value = row
    ? {
        id: row.id, input: row.input, expected: row.expected, criteria: row.criteria,
        scoring_method: row.scoring_method || '',
        complexity: row.complexity || 'L2', task_type: row.task_type || 'direct_qa',
        required_tools_text: (row.required_tools || []).join(','),
        expected_min_steps: row.expected_min_steps ?? 0,
        turns_text: (row.turns || []).join('\n'),
      }
    : {
        id: 0, input: '', expected: '', criteria: '', scoring_method: '',
        complexity: 'L2', task_type: 'direct_qa',
        required_tools_text: '', expected_min_steps: 0, turns_text: '',
      }
  itemEditVisible.value = true
}
async function saveItem() {
  if (!currentDataset.value) return
  loading.value = true
  try {
    const f = itemForm.value
    const payload = {
      input: f.input, expected: f.expected, criteria: f.criteria,
      scoring_method: f.scoring_method || null,
      complexity: f.complexity, task_type: f.task_type,
      required_tools: f.required_tools_text.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      expected_min_steps: f.expected_min_steps || null,
      turns: f.turns_text.split('\n').map(s => s.trim()).filter(Boolean),
    }
    if (itemForm.value.id) {
      await evaluationApi.updateItem(currentDataset.value.id, itemForm.value.id, payload)
    } else {
      await evaluationApi.addItem(currentDataset.value.id, payload)
    }
    itemEditVisible.value = false
    ElMessage.success('已保存')
    await viewDataset(currentDataset.value)
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function deleteItem(row: EvalDatasetItem) {
  if (!currentDataset.value) return
  if (!await confirmAction('确认删除该用例？', '删除用例', true)) return
  loading.value = true
  try {
    await evaluationApi.deleteItem(currentDataset.value.id, row.id)
    await viewDataset(currentDataset.value)
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function submitImport() {
  loading.value = true
  try {
    await evaluationApi.importItems({
      dataset_id: importForm.value.dataset_id || undefined,
      name: importForm.value.name || undefined,
      content: importForm.value.content,
    })
    importVisible.value = false
    ElMessage.success('导入成功')
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}
async function seedCommunity() {
  loading.value = true
  try {
    const res = await evaluationApi.seedCommunity()
    const parts: string[] = []
    if (res.created.length) parts.push(`新增：${res.created.join('、')}`)
    if (res.updated.length) parts.push(`刷新：${res.updated.join('、')}`)
    if (parts.length) {
      ElMessage.success(parts.join('；'))
    } else {
      ElMessage.info('社区数据集无变化（按名称幂等）')
    }
    await loadDatasets()
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    loading.value = false
  }
}

defineExpose({ loadAll })
</script>

<style scoped>
.toolbar { display: flex; gap: var(--ui-space-2); align-items: center; margin-bottom: var(--ui-space-3); flex-wrap: wrap; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.hint { color: var(--ui-muted); font-size: var(--ui-size-sm); }
</style>
