# Knowledge Ingestion & RAG Runtime 交付说明

> 基线：`main` @ `3a5c4f4`（任务书审查基线）；实施后新增 Knowledge Ingestion
> & RAG Runtime（Phase 0–6）。
> 相关文档：`docs/architecture/knowledge-rag-current-state.md`（Phase 0 审查）、
> `docs/knowledge-rag-deployment.md`（部署/备份/恢复/排障）。

## 1. 完成内容（按任务书阶段）

### Phase 0：现状审查与质量基线
- `docs/architecture/knowledge-rag-current-state.md`：真实代码路径、格式矩阵、
  API 权限审查（8 个端点 IDOR 风险）、Agent Tool 调用链、存储规模实测
  （1 KB / 1 doc / 144 旧 Chunk / 1 PDF）、检索算法缺陷、迁移与测试集方案。

### Phase 1：安全与正确性
- 所有 Knowledge API 统一 ACL（`services/knowledge/acl.py`）：owner / 组织只读 /
  项目成员角色；无权限与不存在统一 404，修复 IDOR。
- 网页爬取 SSRF（`security.fetch_webpage_safely`）：仅 http(s)、禁回环/内网/
  链路本地/保留地址/云元数据、DNS 解析后校验真实 IP、重定向逐跳复核、
  5MB/20s/Content-Type 限制。
- 上传流式写入（边读边计数 + SHA256），Magic Bytes 校验，Office ZIP 解压
  上限/条目数（压缩炸弹），未支持格式明确拒绝。
- `chunk_overlap >= chunk_size` 死循环修复（API 422 + 启动校验 + 旧函数防御）。
- 解析失败不再冒充成功：`ready` 需要 Chunk 与 Qdrant Point 数校验通过；
  `empty_extraction` / `source_missing` / `unsupported_format` 明确 failed 或
  migration_required。
- 删除一致性：`deleting` → 取消 Job → 删 Points → 删 Chunk → 删解析产物/源文件
  → 删版本/Job/文档 → 重算统计；清理异常保留状态可重试。
- 删除方法不再 `except: pass` 吞异常。

### Phase 2：Docling + Durable Ingestion
- `ParsedDocument` 统一模型（元素类型 / 标题路径 / Provenance：页码、Sheet、
  行列、Slide、表格序号、OCR 置信度）。
- Parser Adapter：`DoclingParser`（Worker 镜像，版面/表格/OCR）+ `NativeParser`
  （PDF/DOCX/DOC/XLSX/XLS/PPTX/PPT/TXT/MD/CSV/HTML/图片/ODT/RTF；旧版二进制
  Office 经 LibreOffice 转换）。
- OCR：RapidOCR（中英），扫描 PDF 页渲染（pypdfium2）+ 图片 OCR，记录页码/
  平均置信度，低质量标记 partial/needs_review。
- 结构感知 + Token-aware Chunker：标题/段落/列表/表格行组/代码边界；
  Parent-Child；来源继承；Content Hash 去重；稳定 Chunk ID。
- 文档版本 / 文件 SHA256 / Parser 版本 / Pipeline 版本落库；同名更新不覆盖旧版本。
- `KnowledgeIngestionJob` + 独立 `knowledge-worker`：CAS 领取、Lease、Heartbeat、
  attempt/backoff、进程重启后过期租约接管、删除时取消 Job、幂等重跑。
- 解析预览 / Chunk Inspector API。

### Phase 3：Qdrant + Embedding + Hybrid Retrieval
- Qdrant Compose（持久化卷、内部网络、可选 API Key、Healthcheck）+ Adapter
  （Collection 按模型+维度共享；服务端注入 KB/Generation/Document Filter；
  稳定 Point ID 幂等 Upsert；本地模式用于测试/单机）。
- Embedding Provider：`local`（fastembed / BGE-small-zh，离线预下载）与
  `openai`（兼容 /embeddings，批量/超时/维度校验）；模型/维度随 Generation 落库。
- Dense（Qdrant）+ BM25（MySQL Chunk 表）→ RRF → 去重 → 可选 CrossEncoder
  Reranker → Parent/邻居上下文扩展 → 引用；低相关返回空（绝对阈值 + IDF 覆盖率）。
- Index Generation 原子切换：重建全部 Job 成功后才激活，旧代际 `retired`
  保留回滚窗口；新索引失败继续使用旧索引。
- 检索指标：耗时、候选数、无结果计数、Reranker 耗时；Ingestion 指标：
  Job 总数/待处理/耗时、解析/OCR/Embedding/写入耗时、失败计数、Chunk/向量数。

### Phase 4：Agent RAG Runtime
- Agent 多知识库绑定（`knowledge_base_ids`，兼容旧 `knowledge_base_id`）；
  AgentVersion 快照包含授权列表；创建/修改/发布时校验知识库权限。
- `knowledge_search` 工具：Run Context 再次复核用户 ACL（撤销授权立即生效），
  模型不能传知识库/用户/租户 ID；返回结构化引用（文件名、页码/Sheet/Slide/
  章节、chunk_id、分数信号）。
- 低相关拒答：无证据时明确回答未找到；证据包裹为不可信 `<knowledge_evidence>`；
  系统提示注入检索规则、引用规则与 Prompt Injection 防护。
- 工作流 knowledge_search 节点同步使用新 Hybrid Retrieval（有 Active Generation 时）。

### Phase 5：前端与评测
- 知识库列表：文档数、已索引 Chunk、向量数、Embedding 模型/维度、Active
  Generation、失败数；创建/编辑支持 Token Chunk 配置、解析器、可见性。
- 知识库详情：构建阶段/进度/错误/警告实时轮询；解析预览 Drawer（元素类型、
  标题层级、表格、页码/Sheet/Slide、OCR 置信度、警告、分页加载）；
  Chunk Inspector（Token 数、父子关系、来源位置、分页）。
- 检索测试：Dense/BM25/Hybrid、Top K、Reranker 开关、阈值、来源位置、融合/
  Dense/BM25/Rerank 排序信号与耗时/候选数（不把相对分数显示为准确率）。
- 质量评测页：Golden 用例编辑、运行、Recall@5/10、Precision@K、MRR@10、
  nDCG@10、Citation Precision、No-answer Accuracy、P50/P95、逐条命中详情。
- Agent 工具来源卡片：知识库引用显示文件名/页码，不可点击伪装成网页链接。

### Phase 6：旧数据迁移与灰度
- `scripts/migrate_legacy_knowledge.py`：备份旧 SQLite/上传目录、统计、从原文件
  重新解析、源文件缺失标 `migration_required`、幂等入队、可选立即构建。
- 保留旧 SQLite 词法检索回退（`KNOWLEDGE_LEGACY_FALLBACK=true`），新索引通过后
  切换 Generation；旧代际保留回滚。
- `scripts/knowledge_eval.py`：可重复运行的 Golden Dataset 评测 CLI。

## 2. 主要新增/修改文件

**后端**
- `app/services/knowledge/{model,tokens,chunking,embedding,vector_store,retrieval,
  ingestion,evaluation,acl,security,storage}.py`
- `app/services/knowledge/parsers/{base,native,docling_parser,registry}.py`
- `app/knowledge_worker/{__init__,main,__main__}.py`
- `app/api/knowledge/knowledge.py`、`app/schemas/knowledge.py`
- `app/models/knowledge.py`（5 张新表 + 扩展字段）、`alembic/versions/c3d4e5f6a7b8_*`
- `app/services/tools/knowledge_tools.py`、`app/services/tools/__init__.py`
- `app/services/assistant/{capability,versions}.py`、`app/api/assistant/assistants.py`
- `app/services/agent_runtime/{source_bindings,factory}.py`、`service/{config,stream}.py`
- `app/api/run/common.py`、`app/api/common/{schemas,serializers}.py`
- `app/services/workflow_service.py`、`app/services/evaluation/runner.py`
- `app/observability/metrics.py`、`app/core/config.py`
- `backend/requirements.txt`、`backend/requirements-worker.txt`、`scripts/*.py`

**前端**
- `src/api/knowledge.ts`、`src/types/index.ts`
- `src/views/knowledge/{KnowledgeList,KnowledgeDetail,KnowledgeRetrieval}.vue`
- `src/features/knowledge/{ParsedPreviewDrawer,ChunkInspectorDrawer,KnowledgeEvaluationPanel}.vue`
- `src/features/run/ToolStep.vue`、`src/features/run/blocks.ts`

**部署/文档**
- `docker/docker-compose.yml`（Qdrant + knowledge-worker + 共享配置锚点）、
  `docker/knowledge-worker.Dockerfile`、`docker/backend.Dockerfile`、
  `docker/.env.example`、`AGENTS.md`、本目录三份文档。

## 3. 测试与验证

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `cd backend && python -m pytest -q` | **1101 passed, 13 skipped**（113.8s） |
| Knowledge RAG（真实解析/Embedding/Qdrant） | `python -m pytest tests/test_knowledge_rag.py -q` | 17 passed |
| 前端类型检查 + 构建 | `cd frontend && npm run build` | 通过 |
| Alembic upgrade/downgrade | `test_agent_runtime_durable.py::test_alembic_migration_upgrade_downgrade` | 通过 |
| Compose 配置 | `docker compose -f docker/docker-compose.yml config -q` | 通过 |
| 真实 Docker 部署 | `docker compose build && up -d` | 见第 4 节 |

已覆盖的真实数据场景：
- Markdown 结构化（标题/表格/代码块/列表）、PDF 页码来源、扫描件 OCR、
  DOCX 标题/表格、XLSX 多 Sheet/合并单元格/公式、PPTX Slide/表格、GBK CSV；
- 语义问法（"多久检查一次" vs "每隔六个月"）召回、精确编号（E-2048）BM25 召回；
- 无答案拒答；多知识库/跨用户/项目共享/组织共享 ACL；Magic Bytes 与 SSRF；
- Job Lease 过期接管、重复入队幂等、可重试失败重试到上限、重跑 Chunk 去重；
- Qdrant 本地模式客户端重启后索引仍在；重建 Generation 原子切换与旧代际保留；
- 删除一致性（Chunk/文档/知识库清理）。

## 4. Docker 部署验证

构建与启动：

- `general-agent-platform-backend` / `general-agent-platform-frontend` /
  `general-agent-platform-knowledge-worker`（5.7GB，含 Docling layout/tableformer/
  OCR 模型、CPU-only torch、LibreOffice、fastembed 模型）均构建成功；
- `docker compose up -d` 后 MySQL 迁移 `f0a1b2c3d4e5 -> c3d4e5f6a7b8` 成功，
  Qdrant `v1.20.0` healthy，`knowledge-worker` 正常运行；
- Qdrant 重启（Force Recreate）后已写入的 Point 仍可检索，索引持久化有效。

真实旧数据迁移（Phase 6）：

- `scripts/migrate_legacy_knowledge` 体检：1 KB / 1 文档 / 旧 SQLite 144 Chunk；
  `--apply` 自动备份到 `data/backups/knowledge/...` 并入队；
- Worker 使用 **Docling** 解析真实中文 PDF（GJB 1378A-2007）：
  83 页、813 元素、59 个表格、76,112 字符、204 个 Child Chunk、237 个向量点；
  7 张图片无 OCR 文本 → 文档标记 `partial` 并保留警告（不冒充完整成功）；
- 解析产物在 **`--network none` 离线容器**中复现成功（模型已烘焙，无外网下载）。

真实检索 / 评测：

| 验证 | 结果 |
| --- | --- |
| Hybrid 检索（"以可靠性为中心的维修分析包括哪些主要内容？"） | Dense 0.8388 + BM25 62.005 → RRF 0.0325；引用第 5-6 页、章节"4.3 以可靠性为中心的维修分析内容"；耗时 ~500ms（首次）/ ~110ms（热） |
| 无答案（"量子纠缠煎饼的配方"） | Dense 0.31 低于 0.35 阈值 → 返回空结果；No-answer Accuracy = 1.0 |
| 评测（2 条 Golden 用例） | Recall@5 = 0.5（唯一正例命中，无答案用例按拒答计 0）、No-answer Accuracy = 1.0、P50 52ms / P95 461ms |
| 解析预览 | 解析预览接口返回 813 元素（标题 138 / 段落 202 / 列表 374 / 表格 59 / 图注 40） |
| Chunk Inspector | 204 个 Child Chunk，含页码 / 标题路径 / Token 数 |
| 旧版 Office | Worker 内 LibreOffice 真实转换 doc→docx / xls→xlsx / ppt→pptx 后解析成功（文本/表格/Sheet/Slide 均保留） |
| 浏览器 E2E | browser-service 内 Playwright：知识库详情 → 解析预览 → 检索测试 全流程通过（页面显示引用页码、章节、RRF/Dense/BM25 信号与耗时） |

> 说明：旧 PDF 文件名在迁移后已正确落库（UTF-8），控制台出现乱码仅为 PowerShell
> 显示编码问题，API/数据库内容正确。

## 5. 已知限制

1. **Docling 仅在 Worker 镜像**：Backend/宿主机测试环境使用 Native 结构化解析器；
   复杂 PDF 的阅读顺序、跨页表格合并、公式语义不如 Docling，生产建议保持
   `KNOWLEDGE_PARSER_PROVIDER=auto` 并在 Worker 中使用 Docling。
2. **Native PDF 表格**：pypdf 不提供表格结构；复杂表格 PDF 依赖 Docling。
   Native 解析会保留文本与页码，但不会伪造表格结构。
3. **Reranker 默认关闭**：开启需加载 CrossEncoder 模型；未在真实企业语料上做
   全量性能/质量对比。
4. **BM25 为应用层计算**：当前按知识库加载 Chunk 后在 Python 计算（窄列投影），
   超大规模语料（百万级 Chunk）需引入全文索引或 Qdrant Sparse Vector。
5. **Embedding 模型**：默认 BGE-small-zh（512 维）；更换模型/维度需新建 Generation
   并重建全部文档。未评测 BGE-M3 / Qwen Embedding 的中文企业数据表现。
6. **父子扩展为 Token 预算贪心**：不做语义重排；复杂跨章节问题可能扩展到弱相关
   邻居。
7. **文档上传不支持音频/视频**（任务书列为后续）；不支持 EML/MSG。
8. **文件上传大小/页数/解压上限可配置**，未做超大文件（>100MB PDF）压力测试。
9. **Qdrant 本地文件模式仅限单进程开发**；生产必须使用 Compose 内的 Qdrant 服务。
10. **前端未做移动端专项适配**；解析预览/Chunk 大列表按 100/20 条分页。
11. **Reranker / OCR / Docling 模型下载依赖网络**：镜像构建期已预下载（Worker
    的 Docling 模型下载失败时会在首次解析时下载，需外网或预置缓存）。
12. **旧索引迁移脚本默认只体检**：需显式 `--apply`；迁移后仍建议人工抽查解析
    预览与评测指标再下线旧索引。

## 6. 回滚方式

- 新索引失败：旧 Generation 保留，KB Active Generation 不变；可重新触发
  `POST /knowledge/bases/{id}/rebuild`。
- 旧检索回滚：保持 `KNOWLEDGE_LEGACY_FALLBACK=true`，将
  `knowledge_bases.active_index_generation_id` 切回旧 Generation（或停止 Worker，
  使用旧 SQLite 词法链路）。
- 部署回滚：`docker compose` 回退到上一镜像 tag；数据库新增列均为可空/带默认值，
  新增表不影响旧代码（旧代码不读取新表）。
- Agent 回滚：`knowledge_base_ids` 为空时自动回退旧 `knowledge_base_id` 语义。
