# 文档智能架构

## Phase 0 现状审查

实施基线为 `main` 的 `79405691d3f99826153e1b11f3b957f5998060f8`。工作树中的 Runtime 和 Skill 本地变更纳入审查，后续在现有实现上增量集成。

- `POST /api/v2/runs` 校验会话与 Agent 归属、读取 AgentVersion、构造消息和附件注记，随后调用 `start_run_with_session`。`_begin_run` 将会话写入、Run 行与输入快照提交后交给 RunWorker。`run_claimed` 恢复 Lease、句柄和事件总线，`_job_from_row` 还原输入。`_drive` 当前主要构建 DeepAgents 对话图。
- 普通图的会话共用 `agent-{agent_id}-session-{id}`，线程配置也带 run_id 和 user_id。现有 CheckpointerFactory 支持开发 Memory、SQLite 和生产 PostgreSQL。新增图采用稳定的 Run 级文档线程，沿用同一个 Factory、Worker 和 Lease。
- DB Skill 由管理员维护，支持多文件、全局或按 Agent 绑定。运行时根据绑定注入 Skill 文件。AgentVersion 冻结 Agent 构型，Skill 的绑定仍由当前 Registry 解析。企业文档规范需要独立的所有权和不可变版本。
- 附件在会话工作区上传，Office/PDF 预览主要来自抽取文本。工作区 Artifact ID 解析后校验会话归属和路径。长期文档需要独立资产目录、数据库归属和不可变对象。
- Workspace 接收 V2 SSE，将事件归约为消息块，并按已有消息的 source_run_id 回放历史。等待补充时可能还没有助手消息，需恢复会话的 active_run_id。
- AgentRun 事件已通过数据库持久化，可按 Last-Event-ID 继续订阅。已有普通工具审批负责高风险动作。用户补充使用独立请求状态和 schema，并在事务内持久化后由原 Run 恢复。
- Alembic 迁移链当前头为 `f3a4b5c6d7e8`，运行环境为 MySQL。pytest 默认隔离 SQLite 和内存 Checkpointer。真实接管验收需要持久化 Checkpointer 和重新启动的 Worker。
- 可视化 WorkflowEngine 当前自定义节点调度和导出，不接入该链路。文档能力以现有 Worker 内的专业 LangGraph 执行。程序逻辑验证真实文件，结构化语义校验提供独立证据。

## 实施设计

```text
对话 POST /api/v2/runs
  -> 显式模式或 TaskRouter
  -> 绑定已授权的规范版本、附件指纹、文档版本
  -> 原 Run 和 Worker 获取 Lease
  -> 专业文档图 / 原通用对话图
  -> 资料不足时 LangGraph interrupt
  -> RunInputRequest + waiting_input + 持久化事件
  -> 同 Run、同文档线程恢复
  -> 程序化和语义验证、生成或局部编辑 DOCX
  -> 防栅栏失效的提交 + 不可变版本 + Artifact 事件
  -> 对话回放和文档中心
```

文档图分别负责规范提炼、生成与编辑。规范定义是章节、字段和校验的结构化依据。通用 `document-authoring` Skill 提供执行方法。样例、Profile 内容、用户补充和检索结果作为待处理数据传入，采集权限由当前 Agent 的绑定、Capability 和 ToolPolicy 决定。

### 执行与版本

- TaskRouter 优先使用显式任务模式；自动模式结合操作词、已有文档上下文和附件。明确的文档请求进入指定路线，普通聊天和附件总结继续使用 DeepAgents。路由不调用模型。入口检查请求身份和资源归属，将路线、附件指纹及目标版本保存到 Run 输入。
- 专用图使用 `agent-{agent_id}-document-run-{run_id}` 作为线程。数据查询经现有工具适配器、预算和执行账本。数据库查询连接已绑定的外部数据库并启用只读事务。Checkpoint 与平台其余 Run 一样交由已有工厂和 Worker 管理。
- LangGraph 先保存中断 checkpoint，Runtime 再提交等待状态、输入请求和事件。输入经过类型验证后，以一个事务确认请求并重新排队。Worker 按原线程恢复，中断之前已经持久化的成功查询由 checkpoint 和工具账本保留。
- 生成按节写入结构化草稿。编辑通过稳定锚点对已选版本应用 Patch，保留范围之外的章节。发布验证包括必填项、内容来源、排版规则、重读 DOCX 和比较实际标题与正文结构。
- 文件先写入私有暂存目录。提交事务锁定有效 Run、所有者和目标资产，比较当前版本，把 Word 原子移动后记录文件哈希及不可变版本。提交结果不明时查询真实数据库状态，超时残留由维护命令回收。
- 版本提交成功后持久化 `document.version.created` 与 `artifact.created`。SSE 可续读，刷新会话用持久事件重建历史产物卡和等待表单。Trace 子 Span 记录阶段及版本，Prometheus 记录阶段耗时与校验结果。
