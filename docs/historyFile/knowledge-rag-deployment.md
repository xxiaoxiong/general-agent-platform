# Knowledge Ingestion & RAG 部署与运维手册

> 适用版本：`main`（2026-11-27 之后，包含 Knowledge RAG Runtime）
> 组件：Backend（API/检索）+ knowledge-worker（解析/OCR/Embedding/索引）+
> Qdrant（向量库）+ MySQL（元数据/Chunk/Job）+ 可选 OpenAI-compatible Embedding

## 1. 架构与数据流

```
上传/网页 ──> Backend API ──> MySQL(knowledge_documents/versions/jobs)
                                   │
                                   ▼
                        knowledge-worker（独立进程）
                    检测 → 解析(Docling/Native) → OCR(RapidOCR)
                    → 归一化 ParsedDocument → 结构感知 Chunking
                    → Embedding(fastembed / OpenAI API) → Qdrant Upsert
                    → 质量校验 → 激活 Index Generation
                                   │
          Agent / 检索测试 ──> Backend Retrieval
                    Query Embedding + BM25 → RRF → 可选 Reranker
                    → Parent/邻居扩展 → 引用（页码/Sheet/Slide/章节）
```

- **API 进程不执行解析 / OCR / Embedding**；只做控制面与检索查询。
- **Job 队列以 MySQL 为权威**：`knowledge_ingestion_jobs` + Lease / Heartbeat /
  attempt；Worker 崩溃后租约过期由其他 Worker 接管。
- **索引代际**：每个知识库有 Active Generation；重建生成新代际，全部 Job
  成功后才原子切换，旧代际保留回滚窗口。
- **Qdrant 只经服务端 Adapter 访问**，Filter（知识库 / 代际 / 文档）由服务端注入。

## 2. 快速部署

```bash
cd <repo>
# 1) 准备 docker/.env（参考 docker/.env.example）
#    - 必填：MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / POSTGRES_PASSWORD / SECRET_KEY / ADMIN_PASSWORD
#    - 建议：KNOWLEDGE_QDRANT_API_KEY=$(openssl rand -hex 32)
# 2) 构建（backend / frontend / knowledge-worker）
docker compose -f docker/docker-compose.yml build backend frontend knowledge-worker
# 3) 启动（qdrant / knowledge-worker 默认随主栈启动）
docker compose -f docker/docker-compose.yml up -d
# 4) 校验
docker compose -f docker/docker-compose.yml ps
curl -s http://127.0.0.1:8000/health
docker exec ai_plat_backend python -c "import urllib.request;print(urllib.request.urlopen('http://qdrant:6333/healthz').read().decode())"
docker logs --tail 50 ai_plat_knowledge_worker
```

数据库迁移由 Backend entrypoint 的 `alembic upgrade head` 执行；新增表：
`knowledge_document_versions` / `knowledge_ingestion_jobs` /
`knowledge_index_generations` / `knowledge_chunks` / `knowledge_evaluations`，
并为 `knowledge_bases` / `knowledge_documents` / `agents` 增加可空/带默认值列。

## 3. 配置项（`docker/.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KNOWLEDGE_ENABLED` | `true` | 知识库总开关 |
| `KNOWLEDGE_STORAGE_DIR` | `/app/data/knowledge` | 源文件/解析产物目录（服务端生成路径） |
| `KNOWLEDGE_MAX_UPLOAD_SIZE_MB` | `100` | 流式上传大小上限 |
| `KNOWLEDGE_ALLOWED_FORMATS` | PDF/Office/MD/CSV/HTML/图片… | 未列出格式明确拒绝（不按文本兜底） |
| `KNOWLEDGE_PARSER_PROVIDER` | `auto` | auto=Docling 可用则优先；可强制 `docling` / `native` |
| `KNOWLEDGE_OCR_ENABLED` | `true` | 扫描 PDF / 图片 OCR（RapidOCR） |
| `KNOWLEDGE_OCR_LANGUAGES` | `ch,en` | OCR 语言（中英混合模型） |
| `KNOWLEDGE_OCR_MIN_TEXT_CHARS` | `24` | PDF 单页文本低于此值触发 OCR |
| `KNOWLEDGE_OCR_MIN_SCORE` | `0.5` | OCR 平均置信度低于此值标记 needs_review（partial） |
| `KNOWLEDGE_PARSE_TIMEOUT_SECONDS` | `300` | 单文档解析超时 |
| `KNOWLEDGE_MAX_PAGES` | `500` | PDF 页数上限 |
| `KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES` | `1GiB` | Office ZIP 解压上限（压缩炸弹防护） |
| `KNOWLEDGE_CHUNK_SIZE_TOKENS` | `512` | 结构感知 Chunk Token 上限（知识库可覆盖） |
| `KNOWLEDGE_CHUNK_OVERLAP_TOKENS` | `64` | Overlap（必须 < size，否则 422） |
| `KNOWLEDGE_PARENT_CHUNK_MAX_TOKENS` | `1536` | Parent Chunk 上限 |
| `KNOWLEDGE_CONTEXT_TOKEN_BUDGET` | `2400` | 检索结果上下文扩展预算 |
| `KNOWLEDGE_WORKER_CONCURRENCY` | `2` | Worker 并发 Job 数（SQLite 强制 1） |
| `KNOWLEDGE_WORKER_POLL_INTERVAL` | `2` | 空闲轮询间隔（秒） |
| `KNOWLEDGE_WORKER_HEARTBEAT_INTERVAL` | `15` | 心跳续租间隔 |
| `KNOWLEDGE_JOB_LEASE_SECONDS` | `120` | Job 租约时长；超时未心跳会被其他 Worker 接管 |
| `KNOWLEDGE_JOB_MAX_ATTEMPTS` | `3` | 最大尝试次数（超过标记 failed） |
| `KNOWLEDGE_EMBEDDING_PROVIDER` | `local` | `local`=fastembed（离线）/ `openai`=兼容 /embeddings |
| `KNOWLEDGE_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 中文/多语言候选；更换后需重建 Generation |
| `KNOWLEDGE_EMBEDDING_DIMENSION` | `512` | 维度不匹配会明确失败，不会静默写错集合 |
| `KNOWLEDGE_EMBEDDING_BASE_URL` | 空 | `openai` 模式必填（如内网 Embedding 网关） |
| `KNOWLEDGE_EMBEDDING_API_KEY` | 空 | OpenAI-compatible 服务密钥 |
| `KNOWLEDGE_EMBEDDING_BATCH_SIZE` | `16` | 批量大小 |
| `KNOWLEDGE_QDRANT_URL` | `http://qdrant:6333` | 留空=本地文件模式（仅单进程开发） |
| `KNOWLEDGE_QDRANT_API_KEY` | 空 | Qdrant 内部认证（建议生产设置） |
| `KNOWLEDGE_QDRANT_COLLECTION_PREFIX` | `gap_knowledge` | Collection 按模型+维度共享 |
| `KNOWLEDGE_RETRIEVAL_MODE` | `hybrid` | dense / bm25 / hybrid |
| `KNOWLEDGE_DENSE_TOP_K` / `BM25_TOP_K` | `30` | 两路召回上限 |
| `KNOWLEDGE_RRF_K` | `60` | RRF 融合常数 |
| `KNOWLEDGE_MIN_DENSE_SCORE` | `0.35` | Dense 绝对阈值（无可靠证据时拒答）；需按评测校准 |
| `KNOWLEDGE_MIN_LEXICAL_COVERAGE` | `0.2` | BM25 查询词 IDF 覆盖率下限（过滤仅命中常见字的噪声） |
| `KNOWLEDGE_RERANKER_ENABLED` | `false` | 可选 CrossEncoder 二阶段排序 |
| `KNOWLEDGE_RERANKER_MODEL` | `BAAI/bge-reranker-base` | 首次开启会加载/下载模型 |
| `KNOWLEDGE_LEGACY_FALLBACK` | `true` | 新索引未就绪时回退旧 SQLite 词法检索（迁移期回滚） |
| `HF_ENDPOINT` | `https://huggingface.co` | 构建期模型下载镜像（内网可改 hf-mirror） |
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cpu` | Worker 构建期 CPU-only torch（避免拉取 GB 级 CUDA 依赖） |
| `DOCLING_ARTIFACTS_PATH` | `/root/.cache/docling/models`（Worker 镜像内置） | Docling 预下载模型目录；离线解析必须指向该目录 |

## 4. Qdrant 持久化 / 备份 / 恢复

- 数据卷固定为 `ai-generalplat_qdrant_data:/qdrant/storage`（不要"顺手清理"）。
- **备份**（停机或低峰）：
  ```bash
  docker run --rm -v ai-generalplat_qdrant_data:/data -v "$PWD":/backup alpine \
    tar czf /backup/qdrant-$(date +%Y%m%d).tgz -C /data .
  ```
  或使用 Qdrant Snapshot API：
  ```bash
  curl -X POST http://127.0.0.1:6333/collections/<collection>/snapshots
  ```
- **恢复**：停止 backend / worker → 恢复 Volume 内容 → 启动 → 在
  「知识库 → 文档」核对 Chunk / 向量计数；如代际账目不一致，执行「重建索引」。
- MySQL 中的 `knowledge_chunks` 是 BM25 与父子扩展的权威来源；Qdrant 只存向量与
  Payload。两者不一致时以 MySQL 重放（重建）为准。

## 5. Worker 运维

- 日志：`docker logs -f ai_plat_knowledge_worker`。
- 队列深度 / 失败：`knowledge_ingestion_jobs_total`、`knowledge_ingestion_jobs_pending`。
- 重启恢复：Worker 重启不会丢 Job；租约过期后由存活 Worker 接管（`attempt+1`）。
- 手动推进单个 Job（离线场景）：
  ```bash
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend \
    python -c "import asyncio; from app.knowledge_worker import run_once; asyncio.run(run_once())"
  ```
- 幂等：同一文档版本 + Pipeline/模型/Chunker 版本只创建一个 Job；重跑先清理该版本
  同代际 Chunk/Point 再写入，不会产生重复 Chunk。

## 6. 解析能力与限制

| 格式 | 处理 |
| --- | --- |
| PDF | Docling（版面/表格/OCR）或 Native（pypdf 逐页 + 页码 + 页眉页脚去重） |
| 扫描 PDF / 图片 | RapidOCR（中英），记录 OCR 页码/置信度，低质量标记 partial |
| DOCX / DOC | 标题层级 / 段落 / 列表 / 表格 / 内嵌图片 OCR；旧版 doc 经 LibreOffice |
| XLSX / XLS | Sheet / 行列 / 表头 / 合并单元格 / 公式与缓存值；旧版 xls 经 LibreOffice |
| PPTX / PPT | Slide 序号 / 标题 / 文本 / 表格 / 图片 OCR / 备注；旧版 ppt 经 LibreOffice |
| TXT / MD / CSV / HTML | 编码探测（UTF-8/GBK/Big5）、标题、代码块、表格、列表 |
| ODT/ODS/ODP/RTF | 基础结构提取 |

不支持格式（含音频/视频）明确拒绝或标记失败，不会按 UTF-8 文本兜底冒充成功。

## 7. API 一览（`/api/v1/knowledge`）

```
GET/POST            /bases
GET/PUT/DELETE      /bases/{kb_id}
GET/POST            /bases/{kb_id}/documents
GET/DELETE          /bases/{kb_id}/documents/{doc_id}
GET                 /documents/{doc_id}/ingestion
POST                /documents/{doc_id}/reindex
GET                 /documents/{doc_id}/parsed-preview?offset&limit
GET                 /documents/{doc_id}/chunks?page&page_size
POST                /bases/{kb_id}/crawl
POST                /bases/{kb_id}/rebuild
POST                /search
POST                /bases/{kb_id}/evaluations
GET                 /bases/{kb_id}/evaluations
GET                 /evaluations/{evaluation_id}
```

所有端点统一 ACL：owner / 组织共享（只读）/ 项目成员（viewer 只读、operator 写入、
admin 管理）；无权限与不存在统一 404。

## 8. 检索质量评测

- UI：知识库详情 →「质量评测」，添加 Golden 用例并运行，查看
  Recall@5/10、Precision@K、MRR@10、nDCG@10、Citation Precision、
  No-answer Accuracy、P50/P95。
- CLI（可重复）：
  ```bash
  cd backend
  python -m scripts.knowledge_eval --kb-id 1 --dataset golden.json --mode hybrid --output result.json
  python -m scripts.knowledge_eval --kb-id 1 --dataset golden.json --mode hybrid --rerank
  ```
- 阈值校准：先用评测集观察 `KNOWLEDGE_MIN_DENSE_SCORE` 在正确命中与无关问题上的
  分布，再调整；不要用"本次查询最高分"推断相关性。

## 9. 旧索引迁移（Phase 6）

```bash
cd backend
python -m scripts.migrate_legacy_knowledge                    # 体检（不写库）
python -m scripts.migrate_legacy_knowledge --apply            # 备份 + 创建版本/Job
python -m scripts.migrate_legacy_knowledge --apply --run      # 备份 + 入队 + 立即构建
```

- 备份目录默认 `data/knowledge/backups/knowledge-backup-<时间>/`；
- 源文件缺失 / 格式不支持 → 文档标记 `migration_required`，不伪造成功；
- 新 Generation 构建完成后自动切换；旧 SQLite 索引保留，
  `KNOWLEDGE_LEGACY_FALLBACK=true` 时可短期回滚；
- 回滚：将 `KNOWLEDGE_LEGACY_FALLBACK` 保持 `true`，停止 Worker，删除新 Generation
  Collection/Points 或直接切换 `knowledge_bases.active_index_generation_id` 到旧值。

## 10. 故障排查

| 现象 | 排查 |
| --- | --- |
| 文档一直 `queued` | `docker logs ai_plat_knowledge_worker`；`KNOWLEDGE_WORKER_ENABLED`；Worker 能否连接 MySQL/Qdrant |
| `Qdrant Collection ... not found` | 尚无文档完成索引；先上传并等待 Job 成功 |
| `Embedding 维度不匹配` | `KNOWLEDGE_EMBEDDING_DIMENSION` 与模型实际维度不一致；修正后重建 Generation |
| `source_missing` | 旧文档源文件缺失，需重新上传 |
| `empty_extraction` | 扫描件未启用 OCR / 图片质量过低 / 空 PDF；检查 `KNOWLEDGE_OCR_ENABLED` 与 `warnings` |
| `docling_unavailable` | Worker 镜像未安装 docling；改 `KNOWLEDGE_PARSER_PROVIDER=native` 或重建 Worker 镜像 |
| `legacy_conversion_unavailable` | 旧版 doc/xls/ppt 需要 LibreOffice（Worker 镜像已内置 writer/calc/impress） |
| 检索总是返回空 | 检查 Active Generation / 文档状态；`KNOWLEDGE_MIN_DENSE_SCORE` 过高；查看检索测试的 timings/notes |
| 无关问题也返回结果 | Dense 阈值过低或词法噪声；用评测集校准 `KNOWLEDGE_MIN_DENSE_SCORE` / `KNOWLEDGE_MIN_LEXICAL_COVERAGE` |
| 文档 `partial` | OCR 置信度低或解析有警告；查看解析预览中的 warnings |
| 越权 404 | ACL 按 404 隐藏；确认知识库 owner / 项目成员 / 可见性 |
| 网页爬取 400 | SSRF 拦截（内网/回环/元数据）、内容类型不支持或超过 5MB |

## 11. 安全清单

- 知识库 API 全部走 ACL（owner / 项目角色 / 组织只读），无权限统一 404。
- 上传流式写入 + Magic Bytes 校验 + Office ZIP 解压上限，拒绝压缩炸弹。
- 网页抓取逐跳校验（禁内网/回环/链路本地/云元数据/DNS 解析后校验真实 IP），
  限制 5MB / 20s / 仅 text 类型。
- Qdrant 仅容器网络，不映射宿主/公网端口；生产设置 `KNOWLEDGE_QDRANT_API_KEY`。
- 文档内容与检索结果视为不可信证据：Agent 提示词明确不得执行文档内指令。
- Worker 非 root（容器默认），不挂 Docker Socket；Qdrant 数据卷独立持久化。
