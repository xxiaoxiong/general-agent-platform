# Knowledge Ingestion & RAG —— Phase 0 现状审查

> 审查基线：`main` @ `28cd6ca`（在任务书基线 `3a5c4f4` 之后仅文档调整）
> 审查时间：2026-11-27
> 结论：现有知识库链路只有"文件上传 + 字符切分 + 词法检索"，没有真正的
> Embedding、向量索引、结构感知切分、来源定位、Durable Ingestion 与质量评测。

## 1. 代码路径（真实文件）

| 层 | 文件 | 现状 |
| --- | --- | --- |
| API | `backend/app/api/knowledge/knowledge.py` | 知识库/文档 CRUD、上传、网页爬取、检索共 9 个端点 |
| Service | `backend/app/services/knowledge_service.py` | 文本提取、字符切分、SQLite Chunk 表、BM25+TF-IDF |
| Model | `backend/app/models/knowledge.py` | `KnowledgeBase` / `KnowledgeDocument` 两张表 |
| Schema | `backend/app/schemas/knowledge.py` | 创建/更新/输出/检索请求响应 |
| Agent Tool | `backend/app/services/tools/knowledge_tools.py` | `knowledge_search`（仅单库 `agent_config.knowledge_base_id`） |
| Agent 绑定 | `backend/app/services/agent_runtime/source_bindings.py` | 校验 `knowledge_base_id` 所有者 + `is_active` |
| Agent 配置 | `backend/app/services/agent_runtime/service/config.py` | `_resolve_tool_specs` 按 capability 解析工具 |
| Capability | `backend/app/services/assistant/capability.py` | `knowledge` 能力要求绑定 `knowledge_base` 资源 |
| 运行适配 | `backend/app/services/agent_runtime/tools/adapter.py` | 工具统一过 ToolPolicy / 账本 / 事件 |
| 前端 | `frontend/src/views/knowledge/{KnowledgeList,KnowledgeDetail,KnowledgeRetrieval}.vue` | 列表/上传/检索测试 |

## 2. 当前能力事实（逐项核实）

| 模块 | 当前实现 | 结论 |
| --- | --- | --- |
| 上传 | `UploadFile` 先 `await file.read()` 全量读内存再检查大小；扩展名白名单 12 种 | P1：大文件内存压力；无 MIME/Magic Bytes 校验 |
| PDF | `pypdf` 逐页 `extract_text()` | 无页码来源、无表格、无 OCR、无阅读顺序 |
| DOCX/DOC | `python-docx` 仅 `doc.paragraphs` | 表格/图片/标题层级全部丢失；旧版 `.doc` 实际解析失败 |
| XLSX/XLS | `openpyxl` 拼接 TSV 字符串 | 丢失 Sheet/行列/表头/公式语义 |
| PPTX/PPT | `python-pptx` 仅 Shape 文本 | 无 Slide 号/表格/备注 |
| TXT/MD/CSV | UTF-8 `errors="ignore"` | 编码错误被吞；CSV 无表头/行记录结构 |
| 图片/扫描 PDF | 无 | 不能构建可检索知识 |
| 网页 | `httpx.get(url)` + `HTMLParser` | 无 SSRF 防护（可访问内网/云元数据） |
| Chunking | 固定 500 字符、overlap 50 | 结构丢失、无 Token 计量、无来源位置；`overlap >= size` 时步长 ≤ 0 死循环 |
| Embedding | `embedding_fn` 参数从未被调用 | 无真实向量 |
| Vector Store | requirements 安装 `chromadb`，实际从未使用 | 真实索引是 SQLite `chunks` 表 |
| Retrieval | 全表读入内存 → BM25 + TF-IDF → 按本次查询最高分归一化 | 无 Dense、无 RRF、无 Reranker、无绝对相关性、跨库分数不可比 |
| Agent Tool | `knowledge_search` 返回纯文本前 600 字 | 无来源/页码/分数；无低相关拒答；无注入隔离 |
| Ingestion | FastAPI `BackgroundTasks` | 无持久化 Job / Lease / Heartbeat / Recovery；重启后 `processing` 悬挂 |
| 删除 | 先删索引再删数据库，删除方法 `except: pass` | 索引/源文件/数据库可能不一致，异常被吞 |
| 评测 | 无 | 无 Recall / MRR / nDCG / 引用质量 |

## 3. API 权限审查（IDOR 风险）

现状：只有 `list_knowledge_bases` 按 `owner_id` 过滤，**其余端点仅按 ID 查询**。

| 端点 | 当前校验 | 风险 |
| --- | --- | --- |
| `GET /bases/{id}` | 无 owner 校验 | 跨用户读取知识库元数据 |
| `PUT /bases/{id}` | 无 owner 校验 | 跨用户改名/改配置 |
| `DELETE /bases/{id}` | 无 owner 校验 | 跨用户删除 |
| `GET /bases/{id}/documents` | 只校验库存在 | 跨用户枚举文档 |
| `POST /bases/{id}/documents` | 只校验库存在 | 跨用户上传 |
| `DELETE /bases/{id}/documents/{doc_id}` | 只校验文档属于该库 | 跨用户删除文档与源文件 |
| `POST /bases/{id}/crawl` | 只校验库存在 + 无 SSRF 防护 | 跨用户写入 + SSRF |
| `POST /search` | 无 owner 校验 | 跨用户检索（数据泄露） |

修复方案：统一 `knowledge_acl.require_base_access(db, kb_id, user, action)`，
owner / 项目成员（`visibility=project` 且 `ProjectMember` 角色）/ 平台超管
三类主体；Agent 绑定校验不能替代 API 自身校验。

## 4. Agent Tool 调用链

```
Run 创建
  -> config_from_version()（AgentVersion 快照，resource_bindings.knowledge_base_id）
  -> _resolve_tool_specs()（capability knowledge → knowledge_search）
  -> ToolAdapterFactory._execute()
      -> ToolPolicy.check_tool_allowed
      -> Tool Execution Ledger（prepared → executing → succeeded/failed）
      -> ToolExecutor.execute()
          -> _AGENT_HANDLERS["knowledge_search"](db, agent_config, params)
              -> KnowledgeService.search(collection_name, query)
  -> TOOL_COMPLETED 事件（output 文本前 2000 字）
  -> DeepAgents 组装最终回答
```

关键缺口：工具只能访问单个 `knowledge_base_id`；无法接收 Run 授权范围；
返回无结构化引用；文档内容未按不可信证据隔离。

## 5. 数据存储与规模（实测）

| 项 | 位置 | 当前规模 |
| --- | --- | --- |
| 知识库/文档元数据 | MySQL `knowledge_bases` / `knowledge_documents` | 1 个知识库、1 个文档（status=done） |
| Chunk 索引 | `CHROMA_PERSIST_DIR/knowledge.db`（SQLite） | 144 chunks / 1 collection / 237 KB |
| 源文件 | `UPLOAD_DIR/{kb_id}/{uuid}_{filename}` | 1 个 PDF / 520 KB |
| 向量库 | 无 | ChromaDB 依赖未使用，`vector_count=144` 实为 Chunk 数 |

迁移风险：数据量极小，可无损重放；旧 Chunk 为字符切分，不能作为新质量基线；
源文件完整，可全部重新解析。旧检索链路保留一个版本作为回滚。

## 6. 检索算法说明（现状）

1. `_tokenize`：中文单字 + 英文/数字词 + 中文相邻双字；
2. 每次查询读取目标 collection 全部 chunk；
3. 在 Python 内重新计算 DF、平均文档长度、BM25 与 TF-IDF；
4. `hybrid = (0.6*BM25 + 0.4*TFIDF) * 命中率`；
5. `score = hybrid / 本次查询最高分`，再按阈值过滤 Top K。

缺陷：`score` 是相对值（最高分恒为 1.0）；无绝对相关性；跨库合并时各自
归一化后直接比较；语料增长后每次查询全量计算；无同义词/语义召回。

## 7. 迁移与测试集方案

**迁移（Phase 6）**

1. 备份 `data/chroma/knowledge.db` 与 `data/uploads/`；
2. 保留旧 `KnowledgeService` 词法检索作为短期回滚开关
   （`KNOWLEDGE_LEGACY_FALLBACK=true`）；
3. 对每个旧文档从源文件重新解析、生成新 Generation；
4. 源文件缺失 → 文档标记 `migration_required`；
5. 新索引验证通过后切 Active Generation；
6. 账目核对：`doc_count` / `indexed_chunk_count` / `vector_count` 与
   Chunk / Qdrant Point 实数一致。

**解析测试集**：普通 PDF、双栏 PDF、扫描 PDF、复杂表格 PDF、DOCX（标题/
表格/图片）、XLSX（多 Sheet/合并单元格/公式）、PPTX（多 Slide/表格）、
CSV（GBK/UTF-8 BOM）、Markdown（代码块/多级标题）、PNG/JPG OCR。

**检索 Golden Dataset**：精确关键词、同义词、自然语言问法、编号/型号、
数值、表格、跨章节、跨文档、无答案、相似干扰项、多知识库权限过滤；
指标 Recall@5/10、Precision@K、MRR@10、nDCG@10、No-answer Accuracy、
Citation Precision、P50/P95 延迟。
