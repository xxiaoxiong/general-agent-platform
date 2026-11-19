<template>
  <div class="page-container">
    <PageHeader title="技能管理" description="维护可复用指令，按需注入一个或多个 Agent。">
      <template #actions><UiButton variant="primary" @click="openCreate"><Plus />新建技能</UiButton></template>
    </PageHeader>
    <details class="extension-help">
      <summary>技能注入说明</summary>
      <p>技能 = 可复用指令（SKILL.md）+ 附属文件（scripts/、references/ 等，标准 Skill 多文件形态），
          按 Agent 绑定注入 <code>/skills/**</code> 供模型按需加载；
          技能只做文件注入，不携带任何工具授权（工具面由能力包与安全策略决定）；
          附属脚本的执行需要沙箱（execute 能力），无执行能力时脚本可被读取作参考。</p>
    </details>

    <div class="data-surface">
      <ErrorState v-if="loadError" title="无法加载技能列表" @retry="load" />
    <el-table v-else :data="skills" v-loading="loading" empty-text="暂无技能">
      <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.name }}</span>
          <el-tag v-if="!row.managed" size="small" type="info" class="ml-6">{{ row.source }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip />
      <el-table-column prop="version" label="版本" width="80" />
      <el-table-column label="附属文件" width="90">
        <template #default="{ row }">
          <el-tag v-if="Object.keys(row.files || {}).length" size="small" type="info">
            {{ Object.keys(row.files || {}).length }} 个
          </el-tag>
          <el-text v-else type="info" size="small">无</el-text>
        </template>
      </el-table-column>
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
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <template v-if="row.managed">
            <el-button size="small" link @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :type="row.enabled ? 'warning' : 'success'" @click="handleToggle(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
          <el-text v-else type="info" size="small">内置/目录技能只读</el-text>
        </template>
      </el-table-column>
    </el-table>
    </div>

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
        <el-form-item label="附属文件">
          <div class="file-list">
            <div v-for="(f, i) in fileRows" :key="i" class="file-row">
              <el-input
                v-model="f.path"
                placeholder="相对路径，如 scripts/hello.py 或 references/guide.md"
                class="file-path"
              />
              <el-input
                v-model="f.content"
                type="textarea"
                :rows="4"
                placeholder="文件文本内容（脚本/引用文档等）"
              />
              <el-button size="small" type="danger" text @click="fileRows.splice(i, 1)">删除</el-button>
            </div>
            <el-button size="small" @click="fileRows.push({ path: '', content: '' })">
              <el-icon><Plus /></el-icon>添加附属文件
            </el-button>
            <div class="file-hint">
              标准 Skill 形态：SKILL.md 里可引用这些文件（如 scripts/xxx.py）；
              单文件 ≤ 256KB、单技能总量 ≤ 1MB、最多 50 个；路径禁止 ..、绝对路径与覆盖 SKILL.md。
            </div>
          </div>
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
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@lucide/vue'
import { agentOptionApi, skillApi, type AgentOption, type SkillItem } from '@/api/extensions'

const loading = ref(true)
const loadError = ref(false)
const saving = ref(false)
const skills = ref<SkillItem[]>([])
const agentOptions = ref<AgentOption[]>([])

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const bindMode = ref<'global' | 'agents' | 'none'>('global')
const form = ref<Partial<SkillItem>>({})
// 附属文件编辑行（提交时转 Record<string, string>）
const fileRows = ref<{ path: string; content: string }[]>([])

// 与后端 skills.py 注入上限同口径（仅提示用，后端为准）
const MAX_FILE_BYTES = 256 * 1024
const MAX_TOTAL_BYTES = 1024 * 1024
const MAX_FILE_COUNT = 50

async function load() {
  loading.value = true
  loadError.value = false
  try {
    skills.value = (await skillApi.list()).items
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  bindMode.value = 'global'
  form.value = { name: '', description: '', version: '1.0', body: '', enabled: true, agent_ids: null }
  fileRows.value = []
  dialogVisible.value = true
}

function openEdit(row: any) {
  editingId.value = row.id
  bindMode.value = row.agent_ids === null ? 'global' : row.agent_ids.length ? 'agents' : 'none'
  form.value = { ...row, agent_ids: row.agent_ids ? [...row.agent_ids] : null }
  fileRows.value = Object.entries(row.files || {}).map(([path, content]) => ({ path, content: String(content) }))
  dialogVisible.value = true
}

function buildFiles(): Record<string, string> | null {
  const files: Record<string, string> = {}
  const rows = fileRows.value.filter((f) => f.path.trim() || f.content)
  if (rows.length > MAX_FILE_COUNT) {
    ElMessage.warning(`附属文件数量超限（最多 ${MAX_FILE_COUNT} 个）`)
    return null
  }
  let total = 0
  for (const f of rows) {
    const path = f.path.trim()
    if (!path) {
      ElMessage.warning('附属文件路径不能为空')
      return null
    }
    if (path === 'SKILL.md' || path.includes('..') || path.startsWith('/') || path.includes('\\')) {
      ElMessage.warning(`附属文件路径非法: ${path}`)
      return null
    }
    const size = new TextEncoder().encode(f.content).length
    if (size > MAX_FILE_BYTES) {
      ElMessage.warning(`附属文件超出单文件上限（256KB）: ${path}`)
      return null
    }
    total += size
    files[path] = f.content
  }
  if (total > MAX_TOTAL_BYTES) {
    ElMessage.warning('附属文件总量超限（1MB）')
    return null
  }
  return files
}

async function handleSave() {
  if (!form.value.name) return ElMessage.warning('请填写技能名称')
  const files = buildFiles()
  if (files === null) return
  const agent_ids = bindMode.value === 'global' ? null : bindMode.value === 'none' ? [] : (form.value.agent_ids || [])
  const payload = { ...form.value, agent_ids, files }
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
.extension-help { width: fit-content; max-width: 760px; margin-bottom: var(--ui-space-4); color: var(--ui-muted); }
.extension-help summary { cursor: pointer; color: var(--ui-secondary); }
.extension-help p { padding: var(--ui-space-2) 0; }
.ml-6 {
  margin-left: 6px;
}
.mt-8 {
  margin-top: 8px;
  width: 100%;
}
.file-list {
  width: 100%;
}
.file-row {
  border: 1px solid var(--ui-border);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.file-hint {
  margin-top: 6px;
  color: var(--ui-muted);
  font-size: 12px;
  line-height: 1.6;
}
</style>
