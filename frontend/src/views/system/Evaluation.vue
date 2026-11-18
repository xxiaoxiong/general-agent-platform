<template>
  <div class="evaluation-page">
    <div class="page-header">
      <h2>Agent 测评</h2>
      <div class="header-actions">
        <el-button type="primary" @click="openCreateRun">
          <el-icon><Plus /></el-icon>新建测评
        </el-button>
        <el-button :loading="loading" @click="refreshAll">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="evaluation-tabs">
      <!-- ── 测评任务 ── -->
      <el-tab-pane label="测评任务" name="runs">
        <el-table :data="runs" v-loading="loading" size="small" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="任务名称" min-width="160" show-overflow-tooltip />
          <el-table-column label="数据集" width="170" show-overflow-tooltip>
            <template #default="{ row }">{{ datasetName(row.dataset_id) }}</template>
          </el-table-column>
          <el-table-column label="Agent" width="130" show-overflow-tooltip>
            <template #default="{ row }">{{ agentName(row.agent_id) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="runStatusType(row.status)" size="small">{{ runStatusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" width="150">
            <template #default="{ row }">
              <el-progress :percentage="runProgress(row as EvalRun)" :stroke-width="10"
                :status="row.status === 'failed' ? 'exception' : undefined" />
              <span class="progress-text">{{ row.aggregate?.completed ?? 0 }}/{{ row.aggregate?.total ?? 0 }}</span>
            </template>
          </el-table-column>
          <el-table-column label="通过率" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.aggregate?.pass_rate != null" size="small"
                :type="row.aggregate.pass_rate >= 0.8 ? 'success' : row.aggregate.pass_rate >= 0.5 ? 'warning' : 'danger'">
                {{ pct(row.aggregate.pass_rate) }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="平均分" width="80">
            <template #default="{ row }">{{ row.aggregate?.avg_score ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="155">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="230">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="openResult(row as EvalRun)">结果</el-button>
              <el-button size="small" link @click="exportMd(row as EvalRun)">导出MD</el-button>
              <el-button v-if="['pending', 'running', 'cancel_requested'].includes(row.status)"
                size="small" link type="danger" @click="cancelRun(row as EvalRun)">取消</el-button>
              <el-button v-if="['cancelled', 'failed', 'completed'].includes(row.status)"
                size="small" link type="warning" @click="resumeRun(row as EvalRun)">重跑失败项</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && !runs.length" description="还没有测评任务：用社区数据集一键创建，快速验证 Agent 能力">
          <el-button type="primary" @click="openCreateRun">新建测评</el-button>
        </el-empty>
      </el-tab-pane>

      <!-- ── 测评结果 ── -->
      <el-tab-pane label="测评结果" name="results">
        <template v-if="resultRun">
          <div class="toolbar">
            <span class="hint">任务：{{ resultRun.name }}（{{ datasetName(resultRun.dataset_id) }} × {{ agentName(resultRun.agent_id) }}）</span>
            <el-button size="small" :loading="loading" @click="loadResultItems">刷新</el-button>
            <el-button size="small" @click="exportMd(resultRun)">导出 Markdown</el-button>
          </div>
          <div class="metric-cards">
            <el-card shadow="never">
              <div class="metric-label">通过率</div>
              <div class="metric-value">{{ pct(resultRun.aggregate?.pass_rate) }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">通过 / 完成</div>
              <div class="metric-value">{{ resultRun.aggregate?.passed ?? 0 }} / {{ resultRun.aggregate?.completed ?? 0 }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">平均分</div>
              <div class="metric-value">{{ resultRun.aggregate?.avg_score ?? '-' }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">总 Tokens</div>
              <div class="metric-value">{{ resultRun.aggregate?.total_tokens ?? 0 }}</div>
            </el-card>
          </div>
          <el-table :data="resultItems" v-loading="loading" size="small" stripe
            @row-click="(row: EvalRunItemBrief) => openItem(row)">
            <el-table-column prop="position" label="#" width="50">
              <template #default="{ row }">{{ row.position + 1 }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="itemStatusType(row.status)" size="small">{{ itemStatusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="得分" width="80">
              <template #default="{ row }">{{ row.score ?? '-' }}</template>
            </el-table-column>
            <el-table-column prop="input_preview" label="输入" min-width="200" show-overflow-tooltip />
            <el-table-column prop="output_preview" label="实际输出" min-width="200" show-overflow-tooltip />
            <el-table-column prop="run_id" label="Run" width="140" show-overflow-tooltip />
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openItem(row as EvalRunItemBrief)">详情</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <el-empty v-else description="先在「测评任务」中点击「结果」查看某个测评的逐用例明细" />
      </el-tab-pane>

      <!-- ── 数据集 ── -->
      <el-tab-pane label="数据集" name="datasets">
        <div class="toolbar">
          <el-button type="primary" @click="openDatasetDialog()">新建数据集</el-button>
          <el-button @click="importVisible = true">导入 JSONL</el-button>
          <el-button type="success" :loading="loading" @click="seedCommunity">导入社区通用数据集</el-button>
          <span class="hint">社区数据集：GSM8K（数学推理）/ BFCL（函数调用·工具选择），注明出处</span>
        </div>
        <el-table :data="datasets" v-loading="loading" size="small" stripe>
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

      <!-- ── 运行评估（概览 + 评估记录） ── -->
      <el-tab-pane label="运行评估" name="overview">
        <div class="toolbar">
          <el-input-number v-model="agentId" :min="1" placeholder="Agent ID" style="width:140px;" />
          <el-button type="primary" :loading="loading" @click="loadOverview">查询</el-button>
          <el-button :loading="loading" @click="triggerEval">触发评估</el-button>
        </div>
        <div class="metric-cards" v-if="overview">
          <el-card shadow="never" v-for="card in metricCards" :key="card.label">
            <div class="metric-label">{{ card.label }}</div>
            <div class="metric-value">{{ card.value }}</div>
          </el-card>
        </div>
        <el-card shadow="never" v-if="overview?.by_version?.length">
          <h4>分版本对比（回归检测）</h4>
          <el-table :data="overview.by_version" size="small" stripe>
            <el-table-column prop="version_no" label="版本" width="70" />
            <el-table-column prop="sample_count" label="样本" width="70" />
            <el-table-column prop="success_rate" label="成功率" width="90" />
            <el-table-column prop="avg_overall" label="综合分" width="90" />
            <el-table-column prop="avg_steps" label="平均步数" width="90" />
            <el-table-column prop="avg_tokens" label="平均 Tokens" width="110" />
            <el-table-column prop="tool_error_rate" label="工具错误率" width="100" />
            <el-table-column prop="limit_exceeded_rate" label="超限率" width="90" />
          </el-table>
        </el-card>
        <el-card shadow="never" style="margin-top:12px;">
          <h4>评估记录（LLM 裁判）</h4>
          <el-table :data="evaluations" v-loading="loading" size="small" stripe>
            <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
            <el-table-column prop="agent_version_id" label="版本ID" width="80" />
            <el-table-column label="成功" width="70">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                  {{ row.success ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="overall" label="综合分" width="80" />
            <el-table-column label="维度分（完成/质量/工具/效率/事实/格式）" min-width="220">
              <template #default="{ row }">{{ dimText(row.judge_scores) }}</template>
            </el-table-column>
            <el-table-column label="时间" width="160">
              <template #default="{ row }">{{ formatDate(row.evaluated_at) }}</template>
            </el-table-column>
            <el-table-column label="Trace" width="90">
              <template #default="{ row }">
                <el-button v-if="row.langfuse_trace_url" size="small" link type="primary"
                  @click="openUrl(row.langfuse_trace_url)">Langfuse</el-button>
                <span v-else>-</span>
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-top:12px;text-align:right;" v-if="evalTotal > evalPageSize">
            <el-pagination v-model:current-page="evalPage" :page-size="evalPageSize" :total="evalTotal"
              layout="total, prev, pager, next" @current-change="loadEvaluations" />
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ── 开发简报（客观数据） ── -->
      <el-tab-pane label="开发简报" name="briefing">
        <div class="toolbar">
          <el-input-number v-model="windowHours" :min="1" :max="2160" style="width:140px;" />
          <span class="hint">小时窗口</span>
          <el-button type="primary" :loading="loading" @click="loadBriefing">生成简报</el-button>
          <el-button :disabled="!briefing" @click="copyBriefingMd">复制 Markdown</el-button>
          <el-button :disabled="!briefing" @click="downloadBriefingMd">下载 .md</el-button>
          <span class="hint">仅客观数据，不含 AI 主观建议，可直接喂给开发 AI</span>
        </div>
        <template v-if="briefing">
          <div class="metric-cards">
            <el-card shadow="never">
              <div class="metric-label">Run 总数</div>
              <div class="metric-value">{{ briefing.runs?.total ?? 0 }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">成功率</div>
              <div class="metric-value">{{ pct(briefing.runs?.success_rate) }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">工具错误率</div>
              <div class="metric-value">{{ pct(briefing.tools?.error_rate) }}</div>
            </el-card>
            <el-card shadow="never">
              <div class="metric-label">HITL 驳回</div>
              <div class="metric-value">{{ briefing.hitl?.by_decision?.reject ?? 0 }}/{{ briefing.hitl?.total ?? 0 }}</div>
            </el-card>
          </div>

          <el-card shadow="never" v-if="briefing.tools?.per_tool?.length">
            <h4>工具调用统计</h4>
            <el-table :data="briefing.tools.per_tool" size="small" stripe>
              <el-table-column prop="tool_name" label="工具" width="140" />
              <el-table-column prop="calls" label="调用" width="80" />
              <el-table-column prop="failed" label="失败" width="80" />
              <el-table-column prop="retried" label="重试" width="80" />
              <el-table-column prop="repeat_calls" label="重复调用" width="90" />
              <el-table-column prop="error_rate" label="错误率" width="90" />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.failed_runs?.length">
            <h4>失败 Run 记录</h4>
            <el-table :data="briefing.failed_runs" size="small" stripe>
              <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
              <el-table-column prop="agent_id" label="Agent" width="70" />
              <el-table-column prop="status" label="状态" width="110" />
              <el-table-column prop="error" label="错误" min-width="220" show-overflow-tooltip />
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.by_version?.length">
            <h4>分版本趋势</h4>
            <el-table :data="briefing.by_version" size="small" stripe>
              <el-table-column label="版本" width="90">
                <template #default="{ row }">{{ row.version_no ?? '(无版本)' }}</template>
              </el-table-column>
              <el-table-column prop="runs" label="Run 数" width="80" />
              <el-table-column prop="completed" label="完成" width="70" />
              <el-table-column prop="failed" label="失败" width="70" />
              <el-table-column prop="tokens_avg" label="平均 Tokens" width="110" />
              <el-table-column prop="duration_avg_s" label="平均耗时(s)" width="110" />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.hitl?.rejected_records?.length">
            <h4>HITL 驳回记录</h4>
            <el-table :data="briefing.hitl.rejected_records" size="small" stripe>
              <el-table-column prop="run_id" label="Run" width="150" show-overflow-tooltip />
              <el-table-column prop="tool_name" label="工具" width="110" />
              <el-table-column prop="decision" label="决策" width="80" />
              <el-table-column prop="action_summary" label="动作摘要" min-width="180" show-overflow-tooltip />
              <el-table-column prop="message" label="备注" min-width="150" show-overflow-tooltip />
            </el-table>
          </el-card>

          <el-card shadow="never" v-if="briefing.evaluations?.count">
            <h4>评估数据（LLM 裁判记录）</h4>
            <div class="hint" style="margin-bottom:8px;">
              样本 {{ briefing.evaluations.count }} | 平均综合分 {{ briefing.evaluations.avg_overall ?? '-' }}
              | 裁决：{{ verdictDist }}
            </div>
            <el-table :data="dimRows" size="small" stripe>
              <el-table-column prop="dim" label="维度" width="200" />
              <el-table-column prop="avg" label="平均分" width="100" />
            </el-table>
          </el-card>
        </template>
        <el-empty v-else description="点击「生成简报」汇总窗口内的客观执行数据" />
      </el-tab-pane>

      <!-- ── 优化建议（报告 + 变更审计） ── -->
      <el-tab-pane label="优化建议" name="reports">
        <div class="toolbar">
          <el-input-number v-model="agentId" :min="1" placeholder="Agent ID" style="width:140px;" />
          <el-input-number v-model="windowHours" :min="1" :max="2160" style="width:140px;" />
          <span class="hint">小时窗口</span>
          <el-button type="primary" :loading="loading" @click="generateReport">生成优化报告</el-button>
        </div>
        <el-table :data="reports" v-loading="loading" size="small" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="agent_id" label="Agent" width="70" />
          <el-table-column label="窗口" min-width="200">
            <template #default="{ row }">
              {{ formatDate(row.window_start) }} ~ {{ formatDate(row.window_end) }}
            </template>
          </el-table-column>
          <el-table-column label="建议数" width="80">
            <template #default="{ row }">{{ row.suggestions?.length || 0 }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="showReport(row as EvolutionReport)">查看</el-button>
              <el-button size="small" link @click="copyMarkdown(row as EvolutionReport)">导出MD</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-card shadow="never" style="margin-top:12px;">
          <h4>变更审计</h4>
          <el-table :data="changes" v-loading="loading" size="small" stripe>
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="change_type" label="类型" width="90" />
            <el-table-column prop="target" label="目标" width="130" show-overflow-tooltip />
            <el-table-column label="变更（before → after）" min-width="220">
              <template #default="{ row }">{{ changeText(row as EvolutionChange) }}</template>
            </el-table-column>
            <el-table-column prop="applied_version_no" label="发布版本" width="90" />
            <el-table-column label="验证" width="130">
              <template #default="{ row }">
                <el-tag v-if="row.validation?.verdict" size="small"
                  :type="row.validation.verdict === 'improved' ? 'success' : row.validation.verdict === 'regressed' ? 'danger' : 'info'">
                  {{ verdictText(row.validation.verdict) }}
                </el-tag>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="90" />
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button v-if="row.status === 'applied' || row.status === 'validated'" size="small"
                  link type="danger" @click="rollback(row as EvolutionChange)">回滚</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 新建测评 -->
    <el-dialog v-model="createRunVisible" title="新建测评" width="560px">
      <el-form :model="runForm" label-width="110px">
        <el-form-item label="任务名称">
          <el-input v-model="runForm.name" placeholder="留空自动生成" />
        </el-form-item>
        <el-form-item label="数据集" required>
          <el-select v-model="runForm.dataset_id" placeholder="选择测评数据集" style="width:100%;">
            <el-option v-for="d in datasets" :key="d.id"
              :label="`${d.name}（${d.item_count ?? 0} 条）`" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="Agent" required>
          <el-select v-model="runForm.agent_id" placeholder="选择被测 Agent" style="width:100%;">
            <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="审批模式">
          <el-radio-group v-model="runForm.approval_mode">
            <el-radio value="auto">自动（推荐，测评无需人工审批）</el-radio>
            <el-radio value="manual">手动</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="启用记忆">
          <el-switch v-model="runForm.use_memory" />
          <span class="hint" style="margin-left:8px;">默认关闭，保证测评可复现</span>
        </el-form-item>
        <el-form-item label="单用例超时">
          <el-input-number v-model="runForm.timeout_s" :min="10" :max="3600" />
          <span class="hint" style="margin-left:8px;">秒</span>
        </el-form-item>
        <el-form-item label="用例上限">
          <el-input-number v-model="runForm.limit" :min="0" :max="500" />
          <span class="hint" style="margin-left:8px;">0 = 全部用例</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createRunVisible = false">取消</el-button>
        <el-button type="primary" :loading="loading" @click="submitRun">创建并开始</el-button>
      </template>
    </el-dialog>

    <!-- 用例结果详情 -->
    <el-dialog v-model="itemVisible" title="用例结果详情" width="760px" top="6vh">
      <el-descriptions v-if="currentItem" :column="2" border size="small">
        <el-descriptions-item label="状态">{{ itemStatusText(currentItem.status) }}</el-descriptions-item>
        <el-descriptions-item label="得分">{{ currentItem.score ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="评分方式">{{ currentItem.snap_scoring_method }}</el-descriptions-item>
        <el-descriptions-item label="Run">{{ currentItem.run_id || '-' }}</el-descriptions-item>
        <el-descriptions-item label="耗时(s)">{{ currentItem.stats?.duration_s ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="Tokens">{{ currentItem.stats?.tokens ?? '-' }}</el-descriptions-item>
      </el-descriptions>
      <template v-if="currentItem">
        <h4>输入</h4>
        <pre class="block">{{ currentItem.snap_input }}</pre>
        <h4>期望输出</h4>
        <pre class="block">{{ currentItem.snap_expected || '（无明确期望）' }}</pre>
        <h4>实际输出</h4>
        <pre class="block">{{ currentItem.actual_output || '（空）' }}</pre>
        <h4>评分理由</h4>
        <pre class="block">{{ currentItem.score_detail?.reasoning || JSON.stringify(currentItem.score_detail || {}, null, 2) }}</pre>
        <template v-if="currentItem.error">
          <h4>错误</h4>
          <pre class="block error">{{ currentItem.error }}</pre>
        </template>
      </template>
    </el-dialog>

    <!-- 数据集详情（用例管理） -->
    <el-dialog v-model="datasetVisible" :title="`数据集用例：${currentDataset?.name || ''}`" width="860px" top="5vh">
      <div class="toolbar" v-if="currentDataset">
        <el-button type="primary" size="small" @click="openItemDialog()">添加用例</el-button>
        <span class="hint">默认评分：{{ currentDataset.scoring_method }}</span>
      </div>
      <el-table :data="currentDataset?.items || []" size="small" stripe max-height="480">
        <el-table-column prop="position" label="#" width="50" />
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

    <!-- 报告详情 -->
    <el-dialog v-model="reportVisible" title="优化报告" width="760px" top="6vh">
      <div v-if="currentReport">
        <pre class="block">{{ currentReport.summary_text }}</pre>
        <h4>建议（可一键应用）</h4>
        <el-table :data="currentReport.suggestions" size="small" stripe>
          <el-table-column prop="type" label="类型" width="80" />
          <el-table-column prop="target" label="目标" width="120" show-overflow-tooltip />
          <el-table-column prop="rationale" label="依据" min-width="180" show-overflow-tooltip />
          <el-table-column label="预期" width="110">
            <template #default="{ row }">{{ JSON.stringify(row.expected_impact || {}) }}</template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="70" />
          <el-table-column label="操作" width="90">
            <template #default="{ row, $index }">
              <el-button v-if="row.auto_applicable" size="small" type="primary"
                :loading="loading" @click="applySuggestion($index)">应用</el-button>
              <el-tag v-else size="small" type="info">人工实施</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import {
  evaluationApi,
  type EvalDataset, type EvalDatasetItem,
  type EvalRun, type EvalRunItemBrief, type EvalRunItemDetail,
} from '@/api/evaluation'
import {
  evolutionApi,
  type EvolutionReport, type EvolutionChange,
  type EvaluationItem, type DevBriefing,
} from '@/api/evolution'
import { agentOptionApi } from '@/api/extensions'

const tab = ref('runs')
const loading = ref(false)
const agentId = ref<number | undefined>(undefined)
const windowHours = ref(168)

// ── 测评任务 / 结果 ──
const runs = ref<EvalRun[]>([])
const datasets = ref<EvalDataset[]>([])
const agents = ref<Array<{ id: number; name: string }>>([])
const createRunVisible = ref(false)
const runForm = ref({
  name: '', dataset_id: undefined as number | undefined,
  agent_id: undefined as number | undefined,
  approval_mode: 'auto', use_memory: false, timeout_s: 180, limit: 0,
})

const resultRun = ref<EvalRun | null>(null)
const resultItems = ref<EvalRunItemBrief[]>([])
const itemVisible = ref(false)
const currentItem = ref<EvalRunItemDetail | null>(null)

// ── 数据集 ──
const datasetVisible = ref(false)
const currentDataset = ref<EvalDataset | null>(null)
const datasetEditVisible = ref(false)
const datasetForm = ref({ id: 0, name: '', description: '', category: 'general', scoring_method: 'llm_judge', reference: '' })
const itemEditVisible = ref(false)
const itemForm = ref({ id: 0, input: '', expected: '', criteria: '', scoring_method: '' })
const importVisible = ref(false)
const importForm = ref({ dataset_id: undefined as number | undefined, name: '', content: '' })
const scoringMethods = ['llm_judge', 'exact_match', 'contains', 'keyword', 'regex', 'none']

// ── 运行评估 / 简报 / 报告（平移自自进化） ──
const overview = ref<any>(null)
const evaluations = ref<EvaluationItem[]>([])
const evalTotal = ref(0)
const evalPage = ref(1)
const evalPageSize = 20
const reports = ref<EvolutionReport[]>([])
const changes = ref<EvolutionChange[]>([])
const briefing = ref<DevBriefing | null>(null)
const reportVisible = ref(false)
const currentReport = ref<EvolutionReport | null>(null)

const metricCards = computed(() => {
  const o = overview.value?.overall || {}
  return [
    { label: '样本数', value: o.sample_count ?? 0 },
    { label: '成功率', value: pct(o.success_rate) },
    { label: '综合分', value: o.avg_overall ?? '-' },
    { label: '平均步数', value: o.avg_steps ?? '-' },
    { label: '工具错误率', value: pct(o.tool_error_rate) },
    { label: '超限率', value: pct(o.limit_exceeded_rate) },
    { label: '平均耗时(s)', value: o.avg_duration_s ?? '-' },
    { label: 'HITL驳回', value: `${o.hitl_rejected ?? 0}/${o.hitl_total ?? 0}` },
  ]
})
const dimRows = computed(() => {
  const dims = briefing.value?.evaluations?.avg_dimensions || {}
  return Object.entries(dims).map(([dim, avg]) => ({ dim, avg: avg ?? '-' }))
})
const verdictDist = computed(() => {
  const v = briefing.value?.evaluations?.verdicts || {}
  const text = Object.entries(v).map(([k, n]) => `${k}=${n}`).join(', ')
  return text || '-'
})

// ── 工具函数 ──
function pct(v: any) { return v == null ? '-' : `${(v * 100).toFixed(1)}%` }
function formatDate(d: string | null) { return d ? dayjs(d).format('YYYY-MM-DD HH:mm') : '-' }
function openUrl(url: string) { window.open(url, '_blank') }
function datasetName(id: number) { return datasets.value.find(d => d.id === id)?.name || `#${id}` }
function agentName(id: number) { return agents.value.find(a => a.id === id)?.name || `#${id}` }
function runProgress(row: EvalRun) {
  const total = row.aggregate?.total || 0
  const done = row.aggregate?.completed ?? 0
  return total ? Math.round((done / total) * 100) : 0
}
function runStatusType(s: string) {
  return s === 'completed' ? 'success' : s === 'failed' ? 'danger'
    : s === 'running' ? 'warning' : 'info'
}
function runStatusText(s: string) {
  return ({ pending: '待执行', running: '执行中', completed: '已完成',
    failed: '失败', cancelled: '已取消', cancel_requested: '取消中' } as any)[s] || s
}
function itemStatusType(s: string) {
  return s === 'passed' ? 'success' : s === 'failed' ? 'danger'
    : s === 'error' ? 'danger' : s === 'running' ? 'warning' : 'info'
}
function itemStatusText(s: string) {
  return ({ pending: '待执行', running: '执行中', passed: '通过',
    failed: '未通过', error: '异常', skipped: '跳过' } as any)[s] || s
}
function verdictText(v: string) {
  return v === 'improved' ? '已验证·改善' : v === 'regressed' ? '已验证·回退(建议回滚)' : '已验证·持平'
}
function dimText(judge: any) {
  const d = judge?.dimensions
  if (!d) return '-'
  return [d.task_completion, d.answer_quality, d.tool_selection,
    d.efficiency, d.groundedness, d.format_compliance].map(v => v ?? '-').join(' / ')
}
function changeText(row: EvolutionChange) {
  const b = JSON.stringify(row.before?.value ?? row.before)
  const a = JSON.stringify(row.after?.value ?? row.after)
  return `${b.length > 40 ? b.slice(0, 40) + '…' : b} → ${a.length > 40 ? a.slice(0, 40) + '…' : a}`
}

// ── 加载 ──
async function loadRuns() {
  runs.value = (await evaluationApi.listRuns()).items
  schedulePolling()
}
async function loadDatasets() {
  datasets.value = (await evaluationApi.listDatasets()).items
}
async function loadAgents() {
  try { agents.value = await agentOptionApi.list() } catch { agents.value = [] }
}
async function refreshAll() {
  loading.value = true
  try {
    await Promise.all([loadRuns(), loadDatasets(), loadAgents(), loadOverview(), loadEvaluations(), loadReports(), loadChanges()])
  } finally {
    loading.value = false
  }
}

// 运行中任务自动轮询
let pollTimer: number | undefined
function schedulePolling() {
  if (pollTimer) window.clearTimeout(pollTimer)
  const active = runs.value.some(r => ['pending', 'running', 'cancel_requested'].includes(r.status))
  if (active) {
    pollTimer = window.setTimeout(async () => {
      await loadRuns()
      if (resultRun.value && tab.value === 'results') await loadResultItems()
    }, 5000)
  }
}
onUnmounted(() => { if (pollTimer) window.clearTimeout(pollTimer) })

// ── 测评任务操作 ──
function openCreateRun() {
  if (!datasets.value.length) {
    ElMessage.warning('请先在「数据集」中导入用例或社区数据集')
    tab.value = 'datasets'
    return
  }
  createRunVisible.value = true
}
async function submitRun() {
  if (!runForm.value.dataset_id || !runForm.value.agent_id) {
    ElMessage.warning('请选择数据集与 Agent')
    return
  }
  loading.value = true
  try {
    const run = await evaluationApi.createRun({
      name: runForm.value.name || undefined,
      dataset_id: runForm.value.dataset_id,
      agent_id: runForm.value.agent_id,
      approval_mode: runForm.value.approval_mode,
      use_memory: runForm.value.use_memory,
      timeout_s: runForm.value.timeout_s,
      limit: runForm.value.limit,
    })
    createRunVisible.value = false
    ElMessage.success('测评任务已创建并开始执行')
    tab.value = 'runs'
    await loadRuns()
    openResult(run)
  } finally {
    loading.value = false
  }
}
async function cancelRun(row: EvalRun) {
  await ElMessageBox.confirm(`确认取消测评「${row.name}」？未开始的用例将标记为跳过。`, '取消测评', { type: 'warning' })
  loading.value = true
  try {
    await evaluationApi.cancelRun(row.id)
    ElMessage.success('已请求取消')
    await loadRuns()
  } finally {
    loading.value = false
  }
}
async function resumeRun(row: EvalRun) {
  loading.value = true
  try {
    await evaluationApi.resumeRun(row.id)
    ElMessage.success('已重新执行未完成用例')
    await loadRuns()
  } finally {
    loading.value = false
  }
}
async function exportMd(row: EvalRun) {
  const md = await evaluationApi.exportRunMarkdown(row.id)
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `eval-report-${row.id}-${dayjs().format('YYYYMMDD-HHmm')}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

// ── 测评结果 ──
async function openResult(row: EvalRun) {
  resultRun.value = row
  tab.value = 'results'
  await loadResultItems()
}
async function loadResultItems() {
  if (!resultRun.value) return
  loading.value = true
  try {
    resultRun.value = await evaluationApi.getRun(resultRun.value.id)
    resultItems.value = (await evaluationApi.listRunItems(resultRun.value.id)).items
  } finally {
    loading.value = false
  }
}
async function openItem(row: EvalRunItemBrief) {
  currentItem.value = await evaluationApi.getRunItem(row.id)
  itemVisible.value = true
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
  } finally {
    loading.value = false
  }
}
async function deleteDataset(row: EvalDataset) {
  await ElMessageBox.confirm(`确认删除数据集「${row.name}」及其全部用例？`, '删除数据集', { type: 'warning' })
  loading.value = true
  try {
    await evaluationApi.deleteDataset(row.id)
    ElMessage.success('已删除')
    await loadDatasets()
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
    ? { id: row.id, input: row.input, expected: row.expected, criteria: row.criteria, scoring_method: row.scoring_method || '' }
    : { id: 0, input: '', expected: '', criteria: '', scoring_method: '' }
  itemEditVisible.value = true
}
async function saveItem() {
  if (!currentDataset.value) return
  loading.value = true
  try {
    const payload = { ...itemForm.value, scoring_method: itemForm.value.scoring_method || null }
    if (itemForm.value.id) {
      await evaluationApi.updateItem(currentDataset.value.id, itemForm.value.id, payload)
    } else {
      await evaluationApi.addItem(currentDataset.value.id, payload)
    }
    itemEditVisible.value = false
    ElMessage.success('已保存')
    await viewDataset(currentDataset.value)
    await loadDatasets()
  } finally {
    loading.value = false
  }
}
async function deleteItem(row: EvalDatasetItem) {
  if (!currentDataset.value) return
  await ElMessageBox.confirm('确认删除该用例？', '删除用例', { type: 'warning' })
  loading.value = true
  try {
    await evaluationApi.deleteItem(currentDataset.value.id, row.id)
    await viewDataset(currentDataset.value)
    await loadDatasets()
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
  } finally {
    loading.value = false
  }
}
async function seedCommunity() {
  loading.value = true
  try {
    const res = await evaluationApi.seedCommunity()
    if (res.created.length) {
      ElMessage.success(`已导入：${res.created.join('、')}`)
    } else {
      ElMessage.info('社区数据集已全部导入过（按名称幂等）')
    }
    await loadDatasets()
  } finally {
    loading.value = false
  }
}

// ── 运行评估（平移） ──
async function loadOverview() {
  overview.value = await evolutionApi.overview({
    agent_id: agentId.value, hours: windowHours.value,
  })
}
async function loadEvaluations() {
  const res = await evolutionApi.listEvaluations({
    agent_id: agentId.value, page: evalPage.value, page_size: evalPageSize,
  })
  evaluations.value = res.items
  evalTotal.value = res.total
}
async function triggerEval() {
  if (!agentId.value) {
    ElMessage.warning('请先填写 Agent ID')
    return
  }
  loading.value = true
  try {
    const res = await evolutionApi.triggerEvaluations({ agent_id: agentId.value })
    ElMessage.success(`已评估 ${res.queued} 个 Run`)
    await loadEvaluations()
  } finally {
    loading.value = false
  }
}

// ── 优化建议（平移） ──
async function loadReports() {
  reports.value = (await evolutionApi.listReports({ agent_id: agentId.value })).items
}
async function loadChanges() {
  changes.value = (await evolutionApi.listChanges({ agent_id: agentId.value })).items
}
async function generateReport() {
  if (!agentId.value) {
    ElMessage.warning('请先填写 Agent ID')
    return
  }
  loading.value = true
  try {
    const report = await evolutionApi.generateReport({
      agent_id: agentId.value, window_hours: windowHours.value,
    })
    ElMessage.success('报告已生成')
    await loadReports()
    showReport(report)
  } finally {
    loading.value = false
  }
}
function showReport(report: EvolutionReport) {
  currentReport.value = report
  reportVisible.value = true
}
async function applySuggestion(index: number) {
  if (!currentReport.value) return
  const s = currentReport.value.suggestions[index]
  await ElMessageBox.confirm(
    `确认应用建议 [${s.type}/${s.target}]？将修改 Agent 配置并发布新版本。`,
    '一键应用', { type: 'warning' })
  loading.value = true
  try {
    const res = await evolutionApi.applySuggestion(currentReport.value.id, index)
    ElMessage.success(`已应用并发布 v${res.new_version_no}（新 Run 自动生效）`)
    await loadChanges()
  } finally {
    loading.value = false
  }
}
async function rollback(row: EvolutionChange) {
  await ElMessageBox.confirm(
    `确认回滚变更 #${row.id}（${row.target}）？将还原旧值并发布新版本。`,
    '回滚', { type: 'warning' })
  loading.value = true
  try {
    const res = await evolutionApi.rollbackChange(row.id)
    ElMessage.success(`已回滚并发布 v${res.restored_version_no}`)
    await loadChanges()
  } finally {
    loading.value = false
  }
}
async function copyMarkdown(row: EvolutionReport) {
  const res = await evolutionApi.exportMarkdown(row.id)
  await navigator.clipboard.writeText(res.markdown)
  ElMessage.success('报告 Markdown 已复制到剪贴板')
}

// ── 开发简报（平移） ──
async function loadBriefing() {
  loading.value = true
  try {
    briefing.value = await evolutionApi.getBriefing({
      agent_id: agentId.value, hours: windowHours.value,
    })
    ElMessage.success('简报已生成')
  } finally {
    loading.value = false
  }
}
async function copyBriefingMd() {
  const res = await evolutionApi.getBriefingMarkdown({
    agent_id: agentId.value, hours: windowHours.value,
  })
  await navigator.clipboard.writeText(res.markdown)
  ElMessage.success('简报 Markdown 已复制到剪贴板')
}
async function downloadBriefingMd() {
  const res = await evolutionApi.getBriefingMarkdown({
    agent_id: agentId.value, hours: windowHours.value,
  })
  const blob = new Blob([res.markdown], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `dev-briefing-${dayjs().format('YYYYMMDD-HHmm')}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

onMounted(async () => {
  await Promise.all([loadRuns(), loadDatasets(), loadAgents()])
})
</script>

<style scoped>
.evaluation-page {
  padding: 24px;
  background: #fff;
  height: calc(100vh - 56px);
  box-sizing: border-box;
  overflow-y: auto;
}
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { font-size: 20px; font-weight: 700; color: #0d0d0d; }
.header-actions { display: flex; gap: 10px; align-items: center; }
.toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.hint { color: #909399; font-size: 13px; }
.metric-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.metric-label { color: #909399; font-size: 12px; }
.metric-value { font-size: 22px; font-weight: 700; color: #0d0d0d; margin-top: 4px; }
.progress-text { color: #909399; font-size: 12px; }
.block {
  background: #f7f8fa;
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: inherit;
}
.block.error { background: #fef0f0; color: #c45656; }
</style>
