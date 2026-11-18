<template>
  <div class="page-container">
    <div class="page-header">
      <div>
        <h2>技能管理</h2>
        <p class="page-desc">
          技能 = 可复用指令（SKILL.md），按 Agent 绑定注入 <code>/skills/**</code> 供模型按需加载；
          技能只做指令注入，不携带任何工具授权（工具面由能力包与安全策略决定）。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>新建技能
      </el-button>
    </div>

    <el-table :data="skills" v-loading="loading" border stripe>
      <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.name }}</span>
          <el-tag v-if="!row.managed" size="small" type="info" class="ml-6">{{ row.source }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip />
      <el-table-column prop="version" label="版本" width="80" />
      <el-table-column label="注入范围" width="150">
        <template #default="{ row }">
          <el-tag v-if="!row.managed" size="small" type="info">全局（内置）</el-tag>
          <el-tag v-else-if="row.agent_ids === null" size="small">全局注入</el-tag>
          <el-tag v-else-if="!row.agent_ids.length" size="small" type="warning">不注入</el-tag>
          <el-tag v-else size="small" type="success">{{ row.agent_ids.length }} 个 Agent</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <template v-if="row.managed">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :type="row.enabled ? 'warning' : 'success'" @click="handleToggle(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
          <el-text v-else type="info" size="small">内置/目录技能只读</el-text>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑技能' : '新建技能'" width="720px" destroy-on-close>
      <el-form :model="form" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" :disabled="!!editingId" placeholder="kebab-case，如 pdf-processing" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" placeholder="一句话说明技能用途" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="form.version" placeholder="1.0" style="width: 160px" />
        </el-form-item>
        <el-form-item label="注入范围">
          <el-radio-group v-model="bindMode">
            <el-radio value="global">全局（所有 Agent）</el-radio>
            <el-radio value="agents">指定 Agent</el-radio>
            <el-radio value="none">不注入</el-radio>
          </el-radio-group>
          <el-select
            v-if="bindMode === 'agents'"
            v-model="form.agent_ids"
            multiple
            filterable
            placeholder="选择 Agent"
            class="mt-8"
          >
            <el-option v-for="a in agentOptions" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="SKILL.md">
          <el-input
            v-model="form.body"
            type="textarea"
            :rows="12"
            placeholder="YAML frontmatter（name/description/version）+ Markdown 正文；缺 frontmatter 时自动生成"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { agentOptionApi, skillApi, type AgentOption, type SkillItem } from '@/api/extensions'

const loading = ref(false)
const saving = ref(false)
const skills = ref<SkillItem[]>([])
const agentOptions = ref<AgentOption[]>([])

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const bindMode = ref<'global' | 'agents' | 'none'>('global')
const form = ref<Partial<SkillItem>>({})

async function load() {
  loading.value = true
  try {
    skills.value = (await skillApi.list()).items
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  bindMode.value = 'global'
  form.value = { name: '', description: '', version: '1.0', body: '', enabled: true, agent_ids: null }
  dialogVisible.value = true
}

function openEdit(row: any) {
  editingId.value = row.id
  bindMode.value = row.agent_ids === null ? 'global' : row.agent_ids.length ? 'agents' : 'none'
  form.value = { ...row, agent_ids: row.agent_ids ? [...row.agent_ids] : null }
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.value.name) return ElMessage.warning('请填写技能名称')
  const agent_ids = bindMode.value === 'global' ? null : bindMode.value === 'none' ? [] : (form.value.agent_ids || [])
  const payload = { ...form.value, agent_ids }
  saving.value = true
  try {
    if (editingId.value) {
      await skillApi.update(editingId.value, payload)
    } else {
      await skillApi.create(payload)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function handleToggle(row: any) {
  await skillApi.toggle(row.id!)
  await load()
}

async function handleDelete(row: any) {
  await ElMessageBox.confirm(`确认删除技能「${row.name}」？`, '提示', { type: 'warning' })
  await skillApi.remove(row.id!)
  ElMessage.success('已删除')
  await load()
}

onMounted(async () => {
  await load()
  try {
    agentOptions.value = await agentOptionApi.list()
  } catch {
    agentOptions.value = []
  }
})
</script>

<style scoped>
.page-container {
  padding: 20px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}
.page-desc {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
  max-width: 720px;
}
.mono {
  font-family: monospace;
}
.ml-6 {
  margin-left: 6px;
}
.mt-8 {
  margin-top: 8px;
  width: 100%;
}
</style>
