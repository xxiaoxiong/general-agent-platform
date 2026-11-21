# General Agent Platform UI/UX 改造交付

已在本地 Compose 环境部署前后端新镜像。`/login`、生产前端资源和 `/api/health` 均返回 HTTP 200。MySQL、Redis 和 PostgreSQL Checkpointer 继续使用原有数据卷。

## 体验变化

- 全局外壳采用按业务分组的折叠导航，窄屏使用抽屉。同一份可见路由数据驱动侧栏和命令面板，`Ctrl + K` 或 `Cmd + K` 可搜索并键盘打开。主题与导航状态按设备保存。
- 智能体工作空间将会话历史、消息、Composer 和文件预览分区。会话列表显示执行状态，消息区提供可展开工具记录、审批、复制、Markdown 与最新消息定位。用户上翻后可继续阅读，输入时支持中文选词和多行键盘操作。
- 流程编排使用节点库、中央画布、配置检查器和调试区。支持搜索、拖放、连线、框选、缩放、小地图、运行结果、发布和导出。
- 概览呈现真实资源计数和近期会话。模型、知识库、提示词、应用、数据库和系统管理的表格、状态、表单和反馈沿用同一主题。
- 评测页面区分数据集、任务、结果、运行评估、开发简报和优化建议。结果抽屉将服务端 Run、事件、工具、输入、输出和评分理由分层呈现。开发简报沿用客观数据接口。

## 页面覆盖

| 模块 | 实际页面 |
| --- | --- |
| 认证与工作空间 | 登录、概览、智能体对话 |
| 资源与构建 | 模型配置及供应商、知识库列表、知识库详情、提示词库、提示词调试器、数据库连接、数据库查询 |
| 流程编排 | 工作流列表、编辑器、独立运行页 |
| 应用 | 应用市场、我的应用 |
| 评测与管理 | Agent 测评及 Evolution、用户管理、审计日志、技能管理、MCP 服务 |

共覆盖 20 个路由页面。审计文件附有入口和原业务链路。[现状审计](ui-ux-audit.md)

## 设计系统与依赖

- `tokens.css` 集中管理颜色、字号、间距、圆角、边框、阴影、动效与层级，提供独立的浅色和暗色映射。Tailwind 工具类与 Element Plus 兼容层使用相同语义变量。
- 基础组件包括 `UiButton`、`UiDialog`、`EmptyState`、`ErrorState` 和 `PageHeader`。外壳复用 `BrandMark`、`AppSidebar`、`AccountMenu` 和 `CommandDialog`。表格使用统一数据表面、工具栏和分页样式，复杂表格、选择器、上传、日期、抽屉、Skeleton、Toast 和确认窗由主题化的 Element Plus 提供。
- Tailwind CSS 4.3 和 Vite 适配插件负责主题工具类及响应式布局，Reka UI 2.11 负责可访问的对话框、菜单和提示。基础组件按 shadcn-vue 的本地源码模式维护，使用 CVA、clsx 和 tailwind-merge 组织样式变体。Lucide Vue 1.76 提供正式图标。
- 保留 Vue、TypeScript、Vite、Pinia、Vue Router 和 Element Plus。Element Plus 使用组件解析器按使用点接入，移除旧图标包的直接依赖及全局图标注册。Docker 前端用 `npm ci` 锁定依赖。
- Vue Flow core 1.48、background 1.3 和 minimap 1.5 直接复用锁文件中的版本，移除闲置的 controls 包。旧 SVG 迁移至 Vue Flow 是因为图形交互可以通过适配层映射到现有 `definition.nodes` 和 `definition.edges`。适配层保留节点坐标、配置、边顺序、扩展字段及旧版 `from/to`。保存、发布和运行沿用既有 API。

## 回归结果

| 范围 | 真实环境验证 |
| --- | --- |
| 认证与导航 | 登录失败与成功、管理员及普通账号菜单、越权深链接跳转、命令面板搜索与键盘、主题持久化、移动端导航、账号弹窗焦点 |
| 工作空间 | 新建、筛选、打开深链接、恢复历史、删除已运行会话、附件上传与预览和下载、实际 SSE、工具明细、代码复制、停止、批准或拒绝后恢复 |
| 模型、知识库与提示词 | 供应商和模型增删改及连通性探测，知识库增删改、文档上传和索引、真实检索，提示词变量、版本恢复、调试和删除 |
| 工作流与部署 | 创建、拖放、多选、移动、连接、持久化、发布、发布为应用、运行、调试与真实 Word 文件下载，应用部署、配置、SSE 和删除 |
| 数据库与平台管理 | 只读 SQL 结果及查询上下文、用户新建与修改、禁用与删除、审计详情、评测任务执行、评分、真实事件时间线与客观开发简报 |
| 工程 | `npm run build` 通过，包含 Vue 类型检查。Node 22 `npm test` 8 项通过。后端 Windows UTF-8 模式全套测试 554 通过、6 跳过。Docker 前后端重建、更新和健康检查通过 |
| 响应式 | Chrome 巡检 1920×1080、1440×900、1366×768、1024×768 和 390×844。覆盖 20 个页面入口，巡检中未出现主页面横向溢出或浏览器严重错误。宽表格在组件区域内滚动 |

端到端测试使用隔离 MySQL、Redis、PostgreSQL 和浏览器真实请求。模型验证使用本地 OpenAI 兼容测试服务，覆盖平台真实 SSE、工具、审批和评测链路。

回归中修复了四处已有业务故障。删除提示词模板时，在同一事务清理其历史版本。删除终态会话时，解除会话外键并保留 Run、事件、Trace 和评测记录。账号有关联业务数据时返回明确的冲突响应，可通过禁用保留历史归属。评测任务先提交数据，再在应用事件循环中启动后台执行。

## 验收边界

- 现有工作流 API 同步返回节点结果。执行中显示工作流级状态，节点结果在响应返回后更新。
- 工作空间深链接从当前账号最近 20 条会话加载。附件上传反馈包含待完成数量，知识库上传显示逐文件进度。
- 应用 API 支持更换已有绑定，清空已有绑定需要 API 支持。
- Langfuse 本地服务健康检查通过，已有 Trace URL 生成、呈现和跳转链路继续使用项目配置。浏览器验收覆盖平台内的真实 Run 事件详情。Langfuse 项目级页面和生产模型供应商需要相应凭据和配额才能执行额外验收。

## 实机截图

改造前以原前端镜像拍摄，改造后以更新的前端镜像拍摄。补充的执行截图来自浏览器与隔离真实后端的端到端用例。各截图包含采集当时的验收记录。

| 页面 | 改造前 | 改造后 |
| --- | --- | --- |
| 登录 | [查看](ui-ux-evidence/before/login.png) | [查看](ui-ux-evidence/after/login.png) |
| 概览 | [查看](ui-ux-evidence/before/dashboard.png) | [查看](ui-ux-evidence/after/dashboard.png) |
| 智能体工作空间 | [查看](ui-ux-evidence/before/workspace.png) | [查看](ui-ux-evidence/after/workspace.png) |
| 工作流编辑器 | [查看](ui-ux-evidence/before/workflow-editor.png) | [查看](ui-ux-evidence/after/workflow-editor.png) |
| 模型配置 | [查看](ui-ux-evidence/before/models.png) | [查看](ui-ux-evidence/after/models.png) |
| 知识库详情 | [查看](ui-ux-evidence/before/knowledge-detail.png) | [查看](ui-ux-evidence/after/knowledge-detail.png) |
| 提示词调试器 | [查看](ui-ux-evidence/before/prompt-debugger.png) | [查看](ui-ux-evidence/after/prompt-debugger.png) |
| Agent 测评 | [查看](ui-ux-evidence/before/evaluation.png) | [查看](ui-ux-evidence/after/evaluation.png) |

补充截图涵盖 [命令面板](ui-ux-evidence/after/command.png)、[暗色概览](ui-ux-evidence/after/dashboard-dark.png)、[移动端工作空间](ui-ux-evidence/after/workspace-390.png)、[Agent 运行消息](ui-ux-evidence/after/workspace-run.png)、[移动端工作流编辑器](ui-ux-evidence/after/workflow-editor-390.png)、[评测轨迹](ui-ux-evidence/after/evaluation-trace.png)、[应用市场](ui-ux-evidence/after/apps.png) 和 [查询工作区](ui-ux-evidence/after/database-query.png)。
