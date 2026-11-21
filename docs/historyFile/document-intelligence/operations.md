# 文档智能部署与验证

## 部署

使用现有平台 Compose，保留 MySQL、Checkpoint PostgreSQL 及平台应用卷。生产后端入口先执行 Alembic，再启动 Worker。新迁移 `e4d9074233fa` 新增规范、规范版本、资产、文档版本与运行补充请求表。

```powershell
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d backend frontend
docker compose -f docker/docker-compose.yml exec backend alembic current
Invoke-RestMethod http://localhost/api/health
```

工作区和文档资产常驻 `AGENT_FILES_ROOT`。使用默认容器工作目录时位于 `/app/data/agent_files`，随应用数据卷持久化。备份时同步保留 MySQL、Checkpoint PostgreSQL 和应用卷。通过静默窗口取得一致的备份，恢复后对照版本哈希检查文档下载。在需要恢复旧镜像时，优先将服务回退到同一份备份对应的镜像和数据组合。

## 手工验证

1. 在智能体对话上传一份 DOCX，发送“帮我总结一下这份文件”，检查对话回答。随后发送“根据这份文件提炼一个可复用的项目总结报告规范”。
2. 查看规范草稿，调整章节，再确认保存。在文档中心发布规范。使用数据库作为事实来源时，选择已绑定数据库且具备数据分析能力的助手，模型应支持工具调用。
3. 在新对话选择规范并要求生成报告，故意省略一个必填字段。界面进入等待补充。用独立环境验证后端重启，重新加载会话，填写该字段并继续同一个 Run。
4. 校验文档产物卡片可在对话内预览和下载。在 Word 或 LibreOffice 打开实际文件，检查标题、正文和分页。提出修改第三章的要求，确认文档中心出现下一版本并显示章节差异。

## 回归命令

```powershell
Set-Location backend
$env:PYTHONUTF8 = "1"
python -m pytest -q
Set-Location ../frontend
npm test
npm run build
```

容器端到端脚本位于 `backend/tests/e2e/document_intelligence.cjs`。准备独立的 `agent-platform-document-qa` Compose JSON，包含后端、前端及专用 MySQL、Redis、Checkpoint PostgreSQL 和应用卷。为后端提供隔离的管理员测试凭据，在单独的 JSON 中给出 `backend`（宿主机访问的 HTTP 地址，默认 `http://127.0.0.1:8002`）。Docker Desktop 容器需能访问 `host.docker.internal` 上临时启动的测试模型。脚本重启自己的后端容器并留下场景数据用于浏览器检查。

```powershell
node backend/tests/e2e/document_intelligence.cjs <qa-compose.json> <qa-credentials.json>
```

该脚本使用真实平台 HTTP、MySQL、PostgreSQL Checkpointer、绑定 SQLite 数据源和 DOCX 引擎。模型响应来自本地可重复的 OpenAI 兼容测试服务。凭据文件和生成的验收文档留在隔离测试目录。

如需同时验证界面，设置 `DOCUMENT_QA_PLAYWRIGHT` 为本机已安装的 Playwright 模块路径。可用 `DOCUMENT_QA_BROWSER_CHANNEL=msedge` 选择系统 Edge，在凭据 JSON 中设置 `frontend`（默认 `http://127.0.0.1:18080`）。浏览器会在重启后提交日期，随后验证产物回放、预览、下载字节和历史版本选择。

## 排障与使用边界

- `waiting_input` 表示需要人工补充。查询 Run 的 `pending_input` 与持久事件来排查表单恢复；遇到取消、超时或终态冲突时可以从原文档或规范新发任务。
- 扫描版 PDF 缺乏可提取文本时需用户提供文字材料。Word 预览展示结构化内容和来源，下载文件用于查看实际分页和字体。
- 配有工具调用模型的助手可使用绑定数据库、知识库或已授权的只读网络工具。普通用户的本地 SQLite 数据源须位于自己的平台文件目录，管理员可配置受信任的部署挂载路径。文档工作流可采集管理员已绑定、标记为只读且无需审批的 MCP 工具。来源缺失时，任务请求补充可核实的资料。
- 通过 `/metrics` 查看模型、工具、Checkpoint 和 Run 指标，通过 Run Trace 跟踪文档阶段。文档子 Span 使用阶段、版本和计数等元数据。
- 回收因进程退出残留的老化文件时，先运行 `docker compose -f docker/docker-compose.yml exec -T backend python -m app.services.documents.cleanup` 查看候选项。确认后追加 `--apply`。默认只检查超过 24 小时的文件，清理程序再次核查已提交版本和活跃运行。
