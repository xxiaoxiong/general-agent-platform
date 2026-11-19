<template>
  <el-form label-position="top" size="small">
    <el-form-item label="节点标签">
      <el-input v-model="selectedNode.config.label" @input="markDirty" />
    </el-form-item>

    <template v-if="selectedNode.type === 'ai_chat'">
      <el-form-item label="选择模型">
        <el-select v-model="selectedNode.config.model_config_id" @change="markDirty">
          <el-option v-for="m in chatModels" :key="m.id" :label="m.display_name" :value="m.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="提示词模板">
        <el-select
          v-model="selectedNode.config._template_id"
          clearable
          placeholder="从模板库选择（可选）"
          @change="applyPromptTemplate"
        >
          <el-option v-for="t in promptTemplates" :key="t.id" :label="t.name" :value="t.id">
            <span>{{ t.name }}</span>
            <span class="option-meta">{{ t.category }}</span>
          </el-option>
        </el-select>
      </el-form-item>
      <el-form-item label="系统提示">
        <el-input v-model="selectedNode.config.system_prompt" type="textarea" :rows="2" @input="markDirty" />
      </el-form-item>
      <el-form-item label="提示词">
        <el-input v-model="selectedNode.config.prompt" type="textarea" :rows="5"
          placeholder="使用 {{变量名}} 引用上下文变量" @input="markDirty" />
      </el-form-item>
    </template>

    <template v-if="selectedNode.type === 'condition'">
      <el-form-item label="条件表达式">
        <el-input v-model="selectedNode.config.expression" placeholder="如: score > 0.8" @input="markDirty" />
      </el-form-item>
    </template>

    <template v-if="selectedNode.type === 'http'">
      <el-form-item label="请求方法">
        <el-select v-model="selectedNode.config.method" @change="markDirty">
          <el-option label="GET" value="GET" /><el-option label="POST" value="POST" />
          <el-option label="PUT" value="PUT" /><el-option label="DELETE" value="DELETE" />
        </el-select>
      </el-form-item>
      <el-form-item label="URL">
        <el-input v-model="selectedNode.config.url" @input="markDirty" />
      </el-form-item>
    </template>

    <template v-if="selectedNode.type === 'knowledge_search'">
      <el-form-item label="知识库">
        <el-select v-model="selectedNode.config.knowledge_base_id" @change="markDirty">
          <el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="检索问题">
        <el-input v-model="selectedNode.config.query" type="textarea" :rows="2"
          placeholder="使用 {{变量名}} 引用变量" @input="markDirty" />
      </el-form-item>
      <el-form-item label="返回数量">
        <el-input-number v-model="selectedNode.config.top_k" :min="1" :max="10" @input="markDirty" />
      </el-form-item>
    </template>

    <template v-if="selectedNode.type === 'delay'">
      <el-form-item label="延迟秒数">
        <el-input-number v-model="selectedNode.config.seconds" :min="1" :max="30" @input="markDirty" />
      </el-form-item>
    </template>

    <template v-if="selectedNode.type === 'doc_output'">
      <el-form-item label="文档标题">
        <el-input v-model="selectedNode.config.doc_title" placeholder="生成的 Word 文档标题" @input="markDirty" />
      </el-form-item>
      <el-form-item label="内容来源">
        <el-select v-model="selectedNode.config.source_node_id" clearable 
          placeholder="选择 AI 节点（留空=自动取最后一个AI输出）" @change="markDirty">
          <el-option
            v-for="n in nodes.filter(n => n.type === 'ai_chat')"
            :key="n.id"
            :label="n.config?.label || 'AI 对话节点'"
            :value="n.id"
          />
        </el-select>
      </el-form-item>
      <div class="field-hint">
        运行后在结果页点击「下载 Word」即可获取文档。
      </div>
    </template>

    <!-- Data Source Node -->
    <template v-if="selectedNode.type === 'data_source'">
      <el-form-item label="数据源类型">
        <el-select v-model="selectedNode.config.source_type" @change="markDirty">
          <el-option label="CSV 文件" value="csv" />
          <el-option label="Excel 文件" value="excel" />
          <el-option label="MySQL 数据库" value="mysql" />
        </el-select>
      </el-form-item>
      <template v-if="selectedNode.config.source_type === 'csv' || selectedNode.config.source_type === 'excel'">
        <el-form-item label="文件路径">
          <el-input v-model="selectedNode.config.file_path" placeholder="服务器上的绝对路径" @input="markDirty" />
        </el-form-item>
        <el-form-item v-if="selectedNode.config.source_type === 'excel'" label="工作表">
          <el-input v-model="selectedNode.config.sheet" placeholder="工作表名或索引(0开始)" @input="markDirty" />
        </el-form-item>
      </template>
      <template v-if="selectedNode.config.source_type === 'mysql'">
        <el-form-item label="主机">
          <el-input v-model="selectedNode.config.host" placeholder="localhost" @input="markDirty" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input v-model="selectedNode.config.port" placeholder="3306" @input="markDirty" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="selectedNode.config.user" @input="markDirty" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="selectedNode.config.password" type="password" show-password @input="markDirty" />
        </el-form-item>
        <el-form-item label="数据库名">
          <el-input v-model="selectedNode.config.database" @input="markDirty" />
        </el-form-item>
        <el-form-item label="SQL 查询">
          <el-input v-model="selectedNode.config.sql" type="textarea" :rows="3"
            placeholder="SELECT * FROM table WHERE id = {{input_id}}" @input="markDirty" />
        </el-form-item>
      </template>
      <div class="field-hint">
        读取结果存入 rows 数组，可在 AI 节点提示词中引用上游节点数据。
      </div>
    </template>

    <!-- DB Write Node -->
    <template v-if="selectedNode.type === 'db_write'">
      <el-form-item label="主机">
        <el-input v-model="selectedNode.config.host" placeholder="localhost" @input="markDirty" />
      </el-form-item>
      <el-form-item label="端口">
        <el-input v-model="selectedNode.config.port" placeholder="3306" @input="markDirty" />
      </el-form-item>
      <el-form-item label="用户名">
        <el-input v-model="selectedNode.config.user" @input="markDirty" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="selectedNode.config.password" type="password" show-password @input="markDirty" />
      </el-form-item>
      <el-form-item label="数据库名">
        <el-input v-model="selectedNode.config.database" @input="markDirty" />
      </el-form-item>
      <el-form-item label="目标表名">
        <el-input v-model="selectedNode.config.table" placeholder="target_table" @input="markDirty" />
      </el-form-item>
      <el-form-item label="数据来源节点">
        <el-select v-model="selectedNode.config.source_node_id" clearable @change="markDirty">
          <el-option
            v-for="n in nodes.filter(n => n.id !== selectedNode.id)"
            :key="n.id"
            :label="n.config?.label || getNodeType(n.type)?.label || n.id"
            :value="n.id"
          />
        </el-select>
      </el-form-item>
    </template>

    <!-- Send Email Node -->
    <template v-if="selectedNode.type === 'send_email'">
      <el-form-item label="SMTP 服务器">
        <el-input v-model="selectedNode.config.smtp_host" placeholder="smtp.qq.com" @input="markDirty" />
      </el-form-item>
      <el-form-item label="SMTP 端口">
        <el-input v-model="selectedNode.config.smtp_port" placeholder="465" @input="markDirty" />
      </el-form-item>
      <el-form-item label="发件人邮箱">
        <el-input v-model="selectedNode.config.smtp_user" @input="markDirty" />
      </el-form-item>
      <el-form-item label="邮箱密码">
        <el-input v-model="selectedNode.config.smtp_password" type="password" show-password @input="markDirty" />
      </el-form-item>
      <el-form-item label="SSL">
        <el-switch v-model="selectedNode.config.use_ssl" @change="markDirty" />
      </el-form-item>
      <el-form-item label="收件人">
        <el-input v-model="selectedNode.config.to" placeholder="a@x.com, b@x.com" @input="markDirty" />
      </el-form-item>
      <el-form-item label="邮件主题">
        <el-input v-model="selectedNode.config.subject" placeholder="支持 {{变量}}" @input="markDirty" />
      </el-form-item>
      <el-form-item label="邮件正文">
        <el-input v-model="selectedNode.config.body" type="textarea" :rows="4"
          placeholder="支持 HTML 和 {{节点ID.content}} 变量" @input="markDirty" />
      </el-form-item>
    </template>

    <!-- Agent Invoke Node -->
    <template v-if="selectedNode.type === 'agent_invoke'">
      <el-form-item label="Agent ID">
        <el-input v-model="selectedNode.config.agent_id" placeholder="Agent ID（数字）" @input="markDirty" />
      </el-form-item>
      <el-form-item label="输入内容">
        <el-input v-model="selectedNode.config.input" type="textarea" :rows="3"
          placeholder="支持 {{变量名}} 引用上游节点输出，如 {{n_ai_analyze.content}}" @input="markDirty" />
      </el-form-item>
      <div class="field-hint">
        Agent 将自主思考并调用工具完成任务，结果存入 content 字段。
      </div>
    </template>

    <template v-if="selectedNode.type === 'set_variable'">
      <el-form-item label="变量名称">
        <el-input v-model="selectedNode.config.key" placeholder="result" @input="markDirty" />
      </el-form-item>
      <el-form-item label="变量值">
        <el-input v-model="selectedNode.config.value" type="textarea" :rows="3" @input="markDirty" />
      </el-form-item>
    </template>

    <el-form-item v-if="selectedNode.type === 'web_crawl'" label="网页地址">
      <el-input v-model="selectedNode.config.url" placeholder="https://example.com" @input="markDirty" />
    </el-form-item>

    <template v-if="selectedNode.type === 'start'">
      <div class="field-hint">
        在此定义用户运行工作流时需要填写的输入字段，运行时将自动生成表单。
      </div>
      <div
        v-for="(field, idx) in (selectedNode.config.input_fields || [])"
        :key="idx"
        class="input-schema-item"
      >
        <div class="input-schema-row">
          <el-input v-model="field.key" placeholder="字段名(英文)" size="small" aria-label="字段名" @input="markDirty" />
          <el-select v-model="field.type" size="small" aria-label="字段类型" @change="markDirty">
            <el-option label="单行文本" value="text" />
            <el-option label="多行文本" value="textarea" />
            <el-option label="下拉选择" value="select" />
          </el-select>
          <el-button size="small" type="danger" text @click="removeInputField(Number(idx))">删除</el-button>
        </div>
        <el-input v-model="field.label" placeholder="显示标签" size="small"  @input="markDirty" />
        <el-input v-model="field.placeholder" placeholder="占位提示文字" size="small"  @input="markDirty" />
        <el-input v-if="field.type === 'select'" v-model="field.options" placeholder="选项,用逗号分隔,如: yes,no" size="small" @input="markDirty" />
      </div>
      <el-button size="small" type="primary" text @click="addInputField">添加输入字段</el-button>
    </template>
  </el-form>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { getNodeType, type WorkflowPromptTemplate } from './workflowNodes'
import type { ModelConfig, KnowledgeBase } from '@/types'
import type { WorkflowNode } from './workflowGraph'

const props = defineProps<{
  selectedNode: WorkflowNode & { config: Record<string, any> }
  nodes: WorkflowNode[]
  chatModels: ModelConfig[]
  knowledgeBases: KnowledgeBase[]
  promptTemplates: WorkflowPromptTemplate[]
}>()
const emit = defineEmits<{ change: [] }>()

function markDirty() {
  emit('change')
}

function addInputField() {
  const { config } = props.selectedNode
  if (!config.input_fields) config.input_fields = []
  config.input_fields.push({ key: '', label: '', type: 'text', placeholder: '', options: '' })
  markDirty()
}

function removeInputField(index: number) {
  props.selectedNode.config.input_fields.splice(index, 1)
  markDirty()
}

function applyPromptTemplate(templateId: number | null) {
  markDirty()
  if (!templateId) return
  const template = props.promptTemplates.find(item => item.id === templateId)
  if (!template) return
  props.selectedNode.config.prompt = template.content
  ElMessage.success('已应用模板「' + template.name + '」')
}
</script>

<style scoped>
:deep(.el-select) { width: 100%; }
:deep(.el-form-item) { margin-bottom: 20px; }
:deep(.el-form-item__label) { font-weight: 550; }
.field-hint { color: var(--ui-secondary); font-size: 12px; line-height: 1.55; margin-bottom: 12px; }
.option-meta { float: right; font-size: 12px; color: var(--ui-secondary); }
.input-schema-item { display: grid; gap: 6px; margin-bottom: 8px; padding: 8px; background: var(--ui-subtle); border-radius: var(--ui-radius-md); }
.input-schema-row { display: flex; gap: 4px; align-items: center; }
</style>
