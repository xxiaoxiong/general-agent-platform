# General Agent Platform --- Knowledge Ingestion & RAG Runtime 架构设计与实施任务书

> **交付对象：** Codex / Trae\
> **仓库：** `xxiaoxiong/general-agent-platform`\
> **审查基线：** `main`，Commit
> `3a5c4f40c323a415c1219e3f05134a6bae9f6ffc`（2026-11-27）\
> **目标：**
> 将当前基础文件上传和关键词检索模块升级为多格式文档解析、持久化索引、Hybrid
> Retrieval 和 Agent 可调用的企业级 RAG 子系统。

------------------------------------------------------------------------

## 0. 总体目标

最终系统必须支持：

1.  PDF、Word、Excel、PowerPoint、TXT、Markdown、CSV、HTML、图片和扫描件等常见资料。
2.  正确提取文本、标题、章节、表格、列表、页码、Sheet、Slide
    等结构和来源信息。
3.  扫描 PDF / 图片 OCR。
4.  结构化、Token-aware Chunking，而不是简单按字符截断。
5.  真正生成 Embedding 并建立向量索引。
6.  Dense Semantic Search + BM25 Keyword Search 的 Hybrid Retrieval。
7.  RRF 融合排序、可选 Reranker、去重和上下文扩展。
8.  Agent 在需要企业内部知识时，通过 Tool Calling 自主检索。
9.  Agent 只能检索当前 Run 已授权的知识库。
10. 检索结果包含可靠来源定位，最终回答能够引用原始文件、页码、章节、表格或
    Sheet。
11. 构建任务有持久化、幂等、重试、故障恢复。
12. 提供可重复运行的检索质量评测。

**最终验收：上传复杂 PDF / Word / Excel
后能正确解析和索引；用户用与原文不同的自然语言提问，Agent
能找到正确证据、引用来源；知识库没有可靠证据时明确说明没有找到。**

------------------------------------------------------------------------

## 1. 当前代码审查结论

审查文件：

-   `backend/app/services/knowledge_service.py`
-   `backend/app/api/knowledge/knowledge.py`
-   `backend/app/models/knowledge.py`
-   `backend/app/schemas/knowledge.py`
-   `backend/app/services/tools/knowledge_tools.py`
-   `backend/app/services/assistant/capability.py`
-   `backend/app/services/agent_runtime/source_bindings.py`
-   `backend/app/services/agent_runtime/service/config.py`
-   `backend/app/services/agent_runtime/tools/adapter.py`
-   `backend/app/core/config.py`
-   `backend/requirements.txt`
-   `frontend/src/api/knowledge.ts`
-   `frontend/src/views/knowledge/KnowledgeList.vue`
-   `frontend/src/views/knowledge/KnowledgeDetail.vue`
-   `frontend/src/views/knowledge/KnowledgeRetrieval.vue`

### 1.1 当前能力事实

  -----------------------------------------------------------------------------------------
  模块                    当前实现                   结论
  ----------------------- -------------------------- --------------------------------------
  上传                    FastAPI                    基础功能，先完整读入内存再检查大小
                          UploadFile，扩展名白名单   

  PDF                     pypdf 提取页面文本         无完整 OCR / 复杂布局 / 表格结构

  DOCX                    python-docx 读取段落       未完整处理表格、图片、文本框

  DOC                     使用 python-docx           不支持旧版二进制 Word

  XLSX                    openpyxl 拼接单元格        丢失较多表格语义和位置关系

  XLS                     使用 openpyxl              不支持旧版二进制 Excel

  PPTX                    python-pptx 读取文本 Shape 图片、图表、备注等不完整

  PPT                     使用 python-pptx           不支持旧版二进制 PowerPoint

  TXT / MD / CSV          UTF-8 文本读取             CSV
                                                     没有结构化表格处理，编码错误会被忽略

  图片 / 扫描 PDF         无 OCR 主流程              无法可靠构建可检索知识

  音频                    存在 faster-whisper        上传白名单不允许音频
                          转写代码                   

  网页                    HTMLParser 提取文本        不支持 JS 渲染，SSRF 防护不足

  Chunking                默认 500 字符，Overlap 50  无结构感知、Token-aware 和来源定位

  Embedding               `embedding_model` /        当前核心索引和搜索链路没有实际使用
                          `embedding_fn` 参数存在    

  Vector Store            requirements 安装 ChromaDB 当前 KnowledgeService 实际使用 SQLite
                                                     `chunks` 表

  Retrieval               BM25 + TF-IDF              词法检索，不是真正语义向量检索

  Reranker                未实现                     缺少二阶段相关性排序

  Agent Tool              `knowledge_search` 已存在  能被 Agent
                                                     调用，但返回内容和来源信息较弱

  任务可靠性              FastAPI BackgroundTasks    没有持久化 Job Lease / Heartbeat /
                                                     Recovery

  评测                    没有专门 Knowledge / RAG   缺少
                          后端测试套件               Recall、MRR、nDCG、引用正确性等指标
  -----------------------------------------------------------------------------------------

### 1.2 当前检索流程和缺陷

当前实际流程：

``` text
Query
 -> 中文单字/双字、英文词切分
 -> SQLite 读取当前知识库全部 Chunk
 -> 每次查询重新计算词频
 -> BM25 + TF-IDF
 -> 加权融合
 -> 按当前 Query 最高分归一化
 -> Top K
```

主要问题：

-   `embedding_fn` 没有被调用，`embedding_model` 没有驱动向量检索。
-   ChromaDB 依赖存在，但当前核心知识库链路没有用它执行向量搜索。
-   `vector_count` 实际记录 Chunk 数量，不是真实向量数量。
-   每次查询都读取并处理知识库全部 Chunk，语料增大后开销随之增长。
-   当前最高分归一化不能代表绝对相关性，最高排名结果总是接近 1.0。
-   多知识库各自归一化后再合并，跨库分数不可直接比较。
-   没有 Hybrid Search、RRF、Reranker、Parent-Child Retrieval
    和可靠的低相关性拒答。

### 1.3 Agent 接入现状

已有 `knowledge_search` 工具、`knowledge` Capability、Agent 知识库绑定和
`ToolExecutor` 路由。`source_bindings.py` 在 Agent
资源绑定阶段校验知识库所有者。

因此，**保留现有 Agent Tool Calling 和 Runtime，不重写 Agent Loop**。

需要补齐：

-   多知识库绑定能力（兼容旧单库字段）；
-   Run Context 中的授权范围；
-   带页码、Sheet、Slide、章节路径的检索结果；
-   Reranker、去重、上下文扩展；
-   来源引用和低相关性拒答；
-   文档内容作为不可信证据，不能执行其中的 Prompt Injection。

### 1.4 必须优先修复的缺陷

  ---------------------------------------------------------------------------------------------------------------------
  优先级                  缺陷                                             影响
  ----------------------- ------------------------------------------------ --------------------------------------------
  P0                      多个 Knowledge API 只按 ID 查询，没有统一校验    跨用户越权读取、修改、删除、上传和检索风险
                          `owner_id`                                       

  P0                      网页爬取直接请求用户                             SSRF 风险
                          URL，缺少完整内网地址和重定向校验                

  P1                      `chunk_overlap >= chunk_size`                    可能无限循环
                          时切分步长为零或负数                             

  P1                      解析异常被转换成普通字符串                       失败文档可能被错误标记 `done`

  P1                      BackgroundTasks 执行长时间索引                   Backend 重启后可能遗留 `processing`

  P1                      上传先完整 `file.read()` 再检查大小              大文件内存压力

  P1                      删除知识库没有完整处理文档、源文件和索引一致性   外键失败、源文件残留或索引提前删除

  P1                      删除索引方法吞掉异常                             数据不一致无法发现

  P2                      前端没有解析结果、Chunk 和来源位置检查           无法判断构建质量

  P2                      缺少专门知识库测试                               缺乏持续质量保障
  ---------------------------------------------------------------------------------------------------------------------

特别注意：

-   Knowledge API 自身的权限校验不能由 Agent 绑定校验代替。
-   `KnowledgeBase` 删除与 `KnowledgeDocument`
    外键没有明确级联策略，必须明确实现事务和清理顺序。
-   `done` 不能仅表示上传完成或生成了一个 Chunk。

------------------------------------------------------------------------

## 2. 目标架构

将系统拆成两个核心子系统：

-   **Knowledge Ingestion：**
    文件接收、解析、结构化、切分、Embedding、索引。
-   **Knowledge Retrieval：**
    查询、混合召回、排序、上下文组装、来源引用。

Agent Runtime 只通过稳定的 Retrieval Tool 使用知识服务。

``` text
Frontend
  Knowledge Base / Documents / Parsed Preview / Retrieval Test
                         |
                         v
Knowledge API / ACL / Document Registry / Version Manager
                         |
                   Durable Job Queue
                         |
                         v
Knowledge Ingestion Worker
  File Detector
    -> Docling Parser
    -> OCR / Table / Image Extraction
    -> Normalized Document Model
    -> Structure-aware Chunker
    -> Embedding Provider
    -> Qdrant Index Writer
    -> Quality Validator
          |                         |
          v                         v
   Relational DB                  Qdrant
   KB / Document / Job            Dense / Sparse / Payload
          ^                         |
          |                         v
Knowledge Retrieval Service
  Query Validation
    -> Dense Search + BM25
    -> RRF Fusion
    -> Reranker
    -> Dedup / Context Expansion
    -> Citation
          |
          v
Existing Agent Runtime
  DeepAgents + LangGraph + Tool Calling
  knowledge_search
          |
          v
Final Answer + Source Citations
```

### 2.1 服务边界

**Knowledge API / Control Plane**

负责 CRUD、ACL、Document 元数据、索引版本、Job 创建、任务状态、Agent
资源绑定、审计。不要在 API 请求进程中直接执行 CPU 密集型 OCR
和文档解析。

**Knowledge Ingestion Worker**

独立运行，负责 Parser、OCR、结构化、Chunking、Embedding、Qdrant
写入、质量检查和任务恢复。

**Parser Adapter**

统一接口，不允许所有格式解析逻辑继续堆在 `KnowledgeService`：

``` python
class DocumentParser(Protocol):
    async def parse(self, source, options) -> ParsedDocument:
        ...

    def supported_formats(self) -> list[str]:
        ...
```

第一阶段实现 Docling Adapter，后续允许增加 PDF / OCR 专用 Adapter。

**Embedding Provider**

统一接口，支持 OpenAI-compatible API
和本地模型服务。模型名称、维度、版本和批处理参数由配置管理。

**Vector Store Adapter**

第一阶段实现 Qdrant Adapter，API 和 Agent Tool 不直接依赖 Qdrant SDK。

------------------------------------------------------------------------

## 3. 多格式文档解析

### 3.1 主解析引擎：Docling

采用 `docling` 作为默认多格式文档解析框架。

参考：

-   https://github.com/docling-project/docling
-   https://docling-project.github.io/docling/usage/supported_formats/

主要原因：

-   多种格式归一化为统一 Document Model；
-   支持 PDF、DOCX、PPTX、XLSX、Markdown、HTML、CSV、图片；
-   旧版 DOC / PPT / XLS 可以结合 LibreOffice 转换；
-   保留标题、段落、表格、图片和来源信息；
-   支持结构化 JSON、Markdown 和 Chunk 输出；
-   支持 OCR 与复杂 PDF 处理。

不能仅调用纯文本导出后丢弃全部结构，必须保留 Parsed Document
结构化表示。

### 3.2 文件格式矩阵

  格式                    第一阶段要求   关键处理
  ----------------------- -------------- -----------------------------------
  PDF                     必须           文本、页码、阅读顺序、表格
  扫描 PDF                必须           OCR、页码、质量信息
  DOCX / DOC              必须           标题、段落、表格、列表、图片
  XLSX / XLS              必须           Sheet、单元格、表头、行列位置
  PPTX / PPT              必须           Slide、标题、文本、表格、图片说明
  TXT / Markdown          必须           编码、标题、代码块、列表
  CSV                     必须           编码、分隔符、表头、行记录
  HTML / HTM              建议           标题、正文、表格、清理脚本和导航
  RTF / ODT / ODS / ODP   建议           文本和结构
  PNG / JPG / TIFF        必须           OCR、图片来源
  EML / MSG               后续           邮件正文、附件
  MP3 / WAV / M4A         后续           ASR 转写、时间戳
  MP4 / MOV               后续           音轨 ASR、时间戳、关键帧可选

不支持的格式必须明确拒绝，不能按 UTF-8 文本兜底后标记成功。

### 3.3 OCR

必须支持扫描 PDF 和图片：

1.  先判断 PDF 是否有可提取文本层；
2.  文本层缺失或明显不足时触发 OCR；
3.  保存 OCR 语言、页码和质量信息；
4.  低质量 OCR 标记 `partial` / `needs_review`；
5.  OCR 失败保留原因和可重试状态；
6.  OCR 有独立并发限制。

优先使用 Docling 自身 OCR。若中文识别质量不足，再评估 PaddleOCR
Adapter，不要第一阶段同时集成多个重复 OCR 框架。

### 3.4 结构化解析要求

**PDF：**
标题、正文、列表、页码、表格、图片图注、阅读顺序；避免重复索引页眉页脚。

**Word：** Heading 层级、段落、编号列表、表格表头、图片及 OCR /
Caption。

**Excel：** Sheet
名称、单元格坐标、表头、行列关系、合并单元格、日期/百分比/货币格式、公式和缓存值状态。不能把整个
Sheet 简单拼成 TSV。

**PowerPoint：** Slide 序号、标题、Shape
文本、表格、图片、图注和可提取图表标签。

检索结果必须能定位到 PDF 页码、Excel Sheet / 行列、PPT Slide。

------------------------------------------------------------------------

## 4. 统一文档表示与来源追踪

解析器输出统一 `ParsedDocument`，不能每种格式返回完全不同的字符串结构。

目标语义示例：

``` json
{
  "document_id": "doc_001",
  "source_name": "设备运行手册.pdf",
  "source_type": "pdf",
  "title": "设备运行手册",
  "elements": [
    {
      "element_id": "el_001",
      "type": "heading",
      "text": "第三章 设备维护",
      "heading_path": ["设备运行手册", "第三章 设备维护"],
      "provenance": {"page": 12}
    },
    {
      "element_id": "el_002",
      "type": "paragraph",
      "text": "设备应每隔六个月进行一次检查。",
      "heading_path": ["设备运行手册", "第三章 设备维护"],
      "provenance": {"page": 12}
    }
  ],
  "parse_stats": {
    "page_count": 20,
    "element_count": 135,
    "table_count": 2,
    "image_count": 4,
    "ocr_pages": [3, 4]
  }
}
```

必须保存：

-   原始文件；
-   结构化解析结果；
-   Chunk 来源 Element；
-   文件版本和 Parser 版本；
-   Parsed Preview 所需数据。

------------------------------------------------------------------------

## 5. Chunking：结构感知 + Token-aware

当前 500 字符 / 50 字符重叠方案必须替换。

新 Chunker 必须具备：

-   Token-aware，而不是只按字符；
-   章节、标题、段落边界；
-   列表、表格、代码块和公式边界；
-   来源位置继承；
-   Chunk 去重和 Content Hash；
-   可配置 Chunk Size / Overlap。

后端强制校验：

``` text
chunk_size > 0
chunk_overlap >= 0
chunk_overlap < chunk_size
```

非法配置返回 422，不进入 Worker。

### 5.1 Chunk 策略

  类型    切分方式
  ------- ---------------------------------------
  正文    按标题和段落合并，再按 Token 上限拆分
  标题    作为后续正文上下文
  列表    尽量保持列表项完整
  表格    按表头和行切分，必要时重复表头
  Excel   按 Sheet / 表格区域 / 行组
  PPT     按 Slide 和内容区域
  代码    按函数、类和代码块
  公式    保留公式与上下文说明
  OCR     继承页码和 OCR 质量信息

### 5.2 Parent-Child Chunk

``` text
Parent Chunk
  ├── Child Chunk 1
  ├── Child Chunk 2
  └── Child Chunk 3
```

Child 用于精确召回，命中后可按 Token Budget 扩展 Parent 或相邻
Child，不要把整章全部返回给模型。

### 5.3 Chunk Metadata

至少保留：

``` text
chunk_id
knowledge_base_id
document_id
document_version_id
index_generation_id
filename
file_type
title
heading_path
element_type
page_start / page_end
sheet_name
row_start / row_end
slide_number
table_index
source_element_ids
content_hash
parser_version
chunker_version
```

------------------------------------------------------------------------

## 6. Embedding 与 Qdrant

### 6.1 Embedding Provider

实现统一 Provider：

-   OpenAI-compatible Embedding API；
-   本地部署模型；
-   批量请求；
-   超时和有限重试；
-   维度检测；
-   模型版本追踪；
-   模型不可用时任务重试。

BGE-M3、Qwen Embedding 等可作为中文 /
多语言候选，必须通过真实中文企业文档和问题集评测后确定。

如果现有 LiteLLM / OpenAI-compatible 网关能提供 Embedding 服务，优先通过
Adapter 复用，不在业务代码中耦合厂商。

### 6.2 Qdrant

第一阶段使用独立 Docker 部署 Qdrant。

参考：

-   https://qdrant.tech/documentation/overview/
-   https://qdrant.tech/documentation/hybrid-queries/
-   https://qdrant.tech/documentation/search-tuning/hybrid-search/

要求：

-   持久化 Volume；
-   Health Check；
-   内部网络访问；
-   API Key / 内部认证；
-   不直接暴露公网；
-   Snapshot / Backup；
-   CPU / RAM / Disk 限制；
-   通过 Qdrant Adapter 访问。

Collection 按 Embedding Model / Vector Dimension / Index Generation
的兼容性管理，不要每个文件一个 Collection。

Payload 至少包含：

``` text
owner_id
project_id
knowledge_base_id
document_id
document_version_id
index_generation_id
chunk_id
filename
source_type
page / sheet / slide / heading_path
element_type
content_hash
text
```

所有过滤条件由服务端根据 Run Context 和 ACL
注入。模型不得传入任意用户、租户或知识库 ID 改变范围。

------------------------------------------------------------------------

## 7. Hybrid Retrieval + Reranker

标准检索流程：

``` text
Query
  ├── Dense Embedding -> Qdrant Dense Search Top 30
  └── BM25 / Full-text Search Top 30
                  |
                  v
               RRF Fusion
                  |
                  v
              Candidate Dedup
                  |
                  v
              Reranker Top 20
                  |
                  v
        Parent / Neighbor Expansion
                  |
                  v
             Context Budget
                  |
                  v
           Top K Evidence + Citation
```

### Dense Retrieval

解决同义词、自然语言表达差异、语义相似和问法不一致。

### BM25 / Keyword Retrieval

解决设备型号、产品编号、参数名、政策编号、错误码、版本号和精确术语。

不要删除当前 BM25 的价值，而是把它作为 Hybrid Retrieval 的一条召回路径。

### RRF

按不同检索器的排名融合，不能直接比较 Dense 和 BM25 原始分数。

### Reranker

-   输入 Query + Candidate Chunks；
-   返回相关性分数和排序；
-   只处理有限候选；
-   支持本地模型或兼容服务；
-   可配置关闭以便性能对比。

### 低相关性处理

-   阈值通过离线评测校准；
-   不允许用当前 Query 的最大分数推断相关性；
-   没有可靠证据时返回空结果；
-   Agent 必须能够说明知识库没有找到足够证据。

------------------------------------------------------------------------

## 8. 数据模型和版本管理

先复用现有 ORM 和迁移链，以下是逻辑字段，不要求照搬命名。

### 8.1 KnowledgeBase

``` text
id
owner_id
project_id
visibility                 private / project / organization
name
description
status
active_index_generation
embedding_profile_id
chunking_profile_id
parser_profile_id
document_count
indexed_chunk_count
created_at / updated_at
```

保留 `owner_id`；共享时复用现有 Project / User / Role
权限，不要另造冲突的 RBAC。原 `vector_count` 应迁移为准确的 Chunk /
Vector 计数语义。

### 8.2 KnowledgeDocument

``` text
id
knowledge_base_id
filename
storage_key
file_type
mime_type
file_size
sha256
status
active_version_id
page_count
sheet_count
slide_count
element_count
chunk_count
parse_stats_json
warning_json
error_code
error_msg
created_at / updated_at
```

存储路径由服务端生成，不能由用户指定。

### 8.3 KnowledgeDocumentVersion

``` text
id
document_id
version_no
source_sha256
parser_name
parser_version
pipeline_version
parsed_artifact_key
index_generation_id
status
created_at
```

同名文件更新不直接覆盖旧版本。

### 8.4 KnowledgeIngestionJob

``` text
id
document_id
document_version_id
knowledge_base_id
job_type
stage
status
progress
attempt
max_attempts
idempotency_key
lease_owner
lease_token
lease_expires_at
heartbeat_at
error_code
error_message
created_at / started_at / finished_at
```

状态建议：

``` text
queued / detecting / parsing / ocr / normalizing
chunking / embedding / indexing / validating
ready / partial / failed / cancelled
```

### 8.5 KnowledgeIndexGeneration

``` text
id
knowledge_base_id
generation_no
embedding_model
embedding_dimension
chunker_version
parser_version
collection_name
status
point_count
created_at
activated_at
```

索引版本必须明确依赖哪个 Embedding Model、维度和 Chunker 版本。

------------------------------------------------------------------------

## 9. Durable Ingestion Worker

当前 FastAPI BackgroundTasks 不再承担核心索引任务。

Worker 必须：

1.  注册 Worker 身份；
2.  从数据库原子领取 Job；
3.  获取 Lease；
4.  周期 Heartbeat；
5.  执行当前 Stage；
6.  成功后提交 Stage 结果；
7.  失败按错误类型重试或终止；
8.  进程重启后接管过期 Job；
9.  不重复创建 Chunk / Vector。

第一阶段可以使用现有关系数据库作为 Job 权威队列，不必为了知识库引入
Kafka。Redis 可用于事件唤醒，但不能成为唯一任务状态来源。

### 9.1 幂等性

-   相同 Document Version + Pipeline Version 不得产生多个索引副本；
-   Chunk ID 使用稳定来源和版本生成；
-   Embedding 按内容 Hash / Model Version 去重；
-   Qdrant Upsert 使用稳定 Point ID；
-   Qdrant 写入后、DB 提交前崩溃，重试能识别已有 Point；
-   删除和重试可重复执行。

### 9.2 Generation 原子切换

``` text
Generation 1 (Active)
        |
        v
创建 Generation 2
        |
        v
解析 / Chunk / Embedding / Index
        |
        v
质量验证
        |
        v
切换 Active Generation
        |
        v
保留旧 Generation 回滚窗口
        |
        v
确认稳定后清理旧 Generation
```

新索引失败时继续使用旧索引，不允许知识库直接不可用。

### 9.3 资源控制

-   Parser、OCR、Embedding 分别限制并发；
-   大 PDF 不阻塞所有轻量文本；
-   限制页数、图片数、解压大小和处理时间；
-   OCR 不允许无限创建线程；
-   记录各 Stage 耗时和资源使用量。

------------------------------------------------------------------------

## 10. Agent Runtime 集成

继续使用现有链路：

``` text
DeepAgents
  -> LangGraph
    -> Tool Calling
      -> knowledge_search
        -> Knowledge Retrieval Service
          -> Qdrant
```

不要把整个知识库注入 System Prompt，不要每轮无条件检索，不要让 Agent
直接访问 Qdrant。

### 10.1 Agent 知识库绑定

当前 Agent 只有单个
`knowledge_base_id`。建议升级为多个授权知识库绑定，同时兼容旧字段和已有
AgentVersion。

``` text
Agent
  └── AgentVersion
       └── Authorized Knowledge Base Bindings
              ├── Knowledge Base A
              ├── Knowledge Base B
              └── Knowledge Base C
```

要求：

-   创建 / 修改 / 发布 Agent 时验证知识库权限；
-   AgentVersion 保存绑定资源快照；
-   Run 创建时确定本次可访问范围；
-   文档更新可按知识库 Active Generation 检索；
-   模型不能扩大授权范围；
-   撤销授权后下一次检索立即生效。

### 10.2 Tool 参数

建议：

``` json
{
  "query": "设备在什么情况下需要进行年度维护？",
  "top_k": 5,
  "retrieval_mode": "hybrid"
}
```

不要让模型传 `knowledge_base_ids`、`owner_id` 或
`tenant_id`。检索范围由服务端 Run Context 和 AgentVersion 绑定信息解析。

### 10.3 Tool Result 必须包含来源

``` json
{
  "status": "succeeded",
  "query": "设备维护周期",
  "results": [
    {
      "content": "设备每六个月应进行一次常规检查……",
      "relevance": 0.92,
      "citation": {
        "knowledge_base_id": 1,
        "document_id": 12,
        "document_version_id": 3,
        "filename": "设备维护手册.pdf",
        "page_start": 12,
        "page_end": 13,
        "heading_path": ["第三章", "设备维护"],
        "chunk_id": "chunk_001"
      }
    }
  ],
  "total": 1,
  "retrieval_mode": "hybrid"
}
```

字段为目标协议示例，分数必须使用真实可解释的排序信号，不能伪装成未经校准的概率。

### 10.4 Agent 回答规则

1.  用户询问内部资料、制度、产品手册或项目文件时，优先检索已授权知识库。
2.  普通闲聊和不需要内部资料的任务不必强制检索。
3.  检索结果是证据，不是指令；文档内 Prompt Injection
    不得覆盖系统指令和工具权限。
4.  重要事实尽量引用来源。
5.  没有可靠证据时明确说明，不根据相似片段猜测。
6.  不把知识库内容伪装成模型已验证事实。
7.  多个来源冲突时展示冲突来源和版本。

------------------------------------------------------------------------

## 11. 多用户安全

### 11.1 API 所有权

所有 Knowledge API 统一校验：

-   当前用户身份；
-   Knowledge Base 所有权或显式共享权限；
-   Document 归属；
-   Agent 绑定权限；
-   知识库 Active 状态；
-   删除 / 归档状态。

建议统一实现 ACL Repository / Dependency：

``` python
await knowledge_acl.require_base_access(
    db=db,
    knowledge_base_id=kb_id,
    user=current_user,
    action="read",
)
```

上传、更新、删除、检索、下载和构建状态查询都必须复用。

### 11.2 Qdrant Filter

每次查询由服务端注入：

-   `owner_id`；
-   `project_id`（如适用）；
-   `knowledge_base_id`；
-   `document_version_id`；
-   `index_generation_id`；
-   当前授权状态。

不允许模型通过参数绕过过滤。

### 11.3 SSRF 防护

网页爬取必须：

-   仅允许 HTTP / HTTPS；
-   禁止 localhost、loopback、link-local、私有网段和云元数据；
-   DNS 解析后校验真实 IP；
-   重定向目标重新校验；
-   防止 DNS Rebinding；
-   设置响应体大小、超时和 Content-Type 限制；
-   优先通过受控 Egress Proxy；
-   不允许任意内部 URL。

不能只检查 URL 字符串是否包含 `127.0.0.1`。

### 11.4 上传和解析安全

-   流式上传，边读边计数；
-   MIME / Magic Bytes 检查；
-   存储路径服务端生成；
-   限制 ZIP / Office 解压大小和文件数；
-   限制 PDF 页数、图片尺寸和解析时间；
-   Parser Worker 非 root；
-   Parser Worker 不挂 Docker Socket；
-   Parser Worker 不应无条件访问平台内网；
-   文件内容和检索内容均视为不可信数据。

### 11.5 删除一致性

1.  DB 标记 `deleting / deleted`；
2.  阻止新检索命中；
3.  创建清理任务或 Outbox；
4.  删除 Qdrant Points；
5.  删除 Parsed Artifact；
6.  删除源文件；
7.  更新统计；
8.  记录清理结果；
9.  失败可重试。

不能先删索引，再因数据库事务失败留下一个表面正常但无法检索的知识库。

------------------------------------------------------------------------

## 12. API 和前端体验

### 12.1 API 语义

遵循现有 `/api/v1/knowledge`
路由，以下为建议语义，避免重复创建已有接口：

``` text
GET    /knowledge/bases
POST   /knowledge/bases
GET    /knowledge/bases/{kb_id}
PUT    /knowledge/bases/{kb_id}
DELETE /knowledge/bases/{kb_id}

POST   /knowledge/bases/{kb_id}/documents
GET    /knowledge/bases/{kb_id}/documents
GET    /knowledge/bases/{kb_id}/documents/{doc_id}
DELETE /knowledge/bases/{kb_id}/documents/{doc_id}

GET    /knowledge/documents/{doc_id}/ingestion
POST   /knowledge/documents/{doc_id}/reindex
GET    /knowledge/documents/{doc_id}/parsed-preview
GET    /knowledge/documents/{doc_id}/chunks

POST   /knowledge/search
POST   /knowledge/evaluations
GET    /knowledge/evaluations/{evaluation_id}
```

### 12.2 知识库列表

展示名称、文档数、已索引数、Chunk / Vector 数量、Active
Generation、Embedding 模型、最近构建时间和失败数。

### 12.3 知识库详情

文档列表展示文件名、格式、大小、构建阶段、页数 / Sheet / Slide 数、Chunk
数、OCR 情况、警告、错误和最近索引版本。

不要把上传完成显示成索引完成。

### 12.4 Parsed Preview / Chunk Inspector

必须能够查看：

-   提取文本；
-   标题结构；
-   表格；
-   页码 / Sheet / Slide；
-   图片 OCR；
-   解析警告；
-   Chunk 切分；
-   来源 Element。

这是排查"为什么 Agent 检索不到内容"的必要能力。

### 12.5 Retrieval Test

支持 Dense / Keyword / Hybrid、Top K、Reranker 开关、结果来源、页码 /
Sheet / Slide、Chunk、Dense / BM25 / Fusion / Reranker 排序信息和耗时。

不能把未经校准的相对分数显示为"准确率 99%"。

### 12.6 Agent 引用

引用可点击打开原文件并定位 PDF 页码、Excel Sheet / 行列、PPT
Slide；如果第一阶段暂时不能精确跳转，至少展示文件名、来源位置和原文片段。

------------------------------------------------------------------------

## 13. 知识库质量评测

不能只测试 API 是否返回数据。

### 13.1 解析测试集

至少覆盖普通 PDF、双栏 PDF、扫描 PDF、复杂表格 PDF、DOCX
标题/表格/图片、XLSX 多 Sheet / 合并单元格 / 公式、PPTX 多 Slide、CSV
中文编码、Markdown 代码块和图片 OCR。

检查提取内容、表格完整性和来源位置，不仅检查 `chunk_count > 0`。

### 13.2 Retrieval Golden Dataset

每条测试数据至少包含：

``` text
question
expected_knowledge_base
expected_document
expected_page / section
relevant_chunk_ids
expected_answer_points
```

覆盖精确关键词、同义词、自然语言、编号、数值、表格、跨章节、跨文档、无答案和相似干扰项。

### 13.3 核心指标

  指标                   目的
  ---------------------- ----------------------
  Parse Success Rate     解析成功率
  Extraction Coverage    有效内容提取覆盖率
  Recall@5 / Recall@10   相关证据召回
  Precision@K            返回结果相关性
  MRR@10                 首个正确结果排名
  nDCG@10                多个相关结果排序质量
  Reranker Gain          Reranker 提升
  Citation Precision     引用是否支持回答
  Citation Recall        关键事实是否有来源
  Faithfulness           回答是否忠于证据
  No-answer Accuracy     无答案是否正确拒答
  Retrieval Latency      P50 / P95
  Ingestion Latency      P50 / P95

先建立真实基线，再根据企业业务数据确定门槛。不能只挑少量容易命中的问题证明系统质量。

### 13.4 Agent 端到端评测

至少验证：

1.  需要内部知识时 Agent 正确调用工具；
2.  同义表达能召回正确内容；
3.  型号和编号能精确召回；
4.  多知识库时只搜索授权范围；
5.  无答案时不编造；
6.  多版本冲突时说明来源；
7.  最终引用能定位原始文件；
8.  普通问题不强制 RAG；
9.  文档恶意指令不能控制 Agent；
10. 多用户检索不串库。

------------------------------------------------------------------------

## 14. 可观测性

新增 Prometheus 指标：

-   `knowledge_ingestion_jobs_total`
-   `knowledge_ingestion_jobs_pending`
-   `knowledge_ingestion_job_duration_seconds`
-   `knowledge_parse_duration_seconds`
-   `knowledge_ocr_duration_seconds`
-   `knowledge_embedding_duration_seconds`
-   `knowledge_index_write_duration_seconds`
-   `knowledge_parse_failure_total`
-   `knowledge_ocr_failure_total`
-   `knowledge_empty_extraction_total`
-   `knowledge_chunk_count`
-   `knowledge_vector_count`
-   `knowledge_retrieval_duration_seconds`
-   `knowledge_retrieval_candidates`
-   `knowledge_retrieval_empty_total`
-   `knowledge_reranker_duration_seconds`

日志关联
`user_id / project_id / agent_id / run_id / knowledge_base_id / document_id / document_version_id / ingestion_job_id / index_generation_id / retrieval_id`。

Langfuse Trace 可记录：

``` text
knowledge_search
  ├── query_validation
  ├── embedding
  ├── dense_search
  ├── bm25_search
  ├── rrf_fusion
  ├── reranking
  └── context_assembly
```

不得将完整文档正文、隐私、API Key 或密码写入普通日志。

------------------------------------------------------------------------

## 15. 分阶段实施顺序

### Phase 0：现状审查与质量基线

交付：

-   `docs/architecture/knowledge-rag-current-state.md`
-   文件格式能力矩阵；
-   Knowledge API 权限审查；
-   Agent Tool 调用链；
-   SQLite 数据和上传文件位置；
-   检索算法说明；
-   迁移风险；
-   解析与检索测试集方案。

验收：代码路径真实；明确旧数据规模；确认 ChromaDB
实际使用情况和文件持久化方式；不重复建设现有 Agent ACL / Capability /
Run Context。

### Phase 1：安全和正确性修复

实现：

1.  所有 Knowledge API 统一校验所有权和共享权限；
2.  修复网页爬取 SSRF；
3.  上传改为流式写入；
4.  校验 MIME / Magic Bytes；
5.  修复 Chunk Size / Overlap 无限循环；
6.  解析失败不能冒充成功；
7.  空文档、OCR 失败和部分解析分别标记；
8.  修复文档 / 知识库删除一致性；
9.  删除异常不再被静默吞掉；
10. 新增 Knowledge API 和安全测试。

验收：IDOR、SSRF 测试通过；非法 Chunk 配置返回 422；解析失败不能标记
done；删除不遗留可检索数据或失控源文件。

### Phase 2：Docling + Durable Ingestion

实现 Parser Adapter、Docling、OCR、ParsedDocument、结构感知
Chunker、Token-aware Chunking、Parent-Child Chunk、文件 Hash /
Version、KnowledgeIngestionJob、独立 Worker、Lease / Heartbeat / Retry /
Recovery、解析预览和 Chunk Inspector。

验收：支持矩阵中的文件有真实样本测试；来源位置保留；表格结构可用；Worker
重启能恢复任务；重复 Job 不产生重复 Chunk；失败原因可见。

### Phase 3：Qdrant + Embedding + Hybrid Retrieval

实现 Qdrant Compose、Adapter、Embedding Provider、Model Profile、Dense
Index、BM25 / Sparse Retrieval、RRF、可选 Reranker、去重、Parent Context
Expansion、Metadata Filter、Index Generation 和质量评测。

验收：真实
Embedding；维度和版本可追踪；同义词和精确术语都能召回；无证据可返回空结果；Qdrant
重启后索引仍在。

### Phase 4：Agent RAG Runtime

实现多知识库绑定、Run Context 授权范围、结构化 Tool
Result、Citation、低相关性拒答、Agent 检索规则、Prompt Injection
防护、AgentVersion 快照和 Langfuse Trace。

验收：Agent
自主决定是否检索；只访问授权库；引用可定位；无证据不编造；普通对话不强制
RAG；现有 Agent Runtime 回归通过。

### Phase 5：前端与评测

实现构建进度、解析预览、Chunk Inspector、OCR
警告、检索模式、来源位置、Reranker 调试、Golden Dataset、Recall / MRR /
nDCG 展示和引用跳转。

验收：用户能判断文件是否解析成功、Chunk
如何切分、检索为什么命中，以及引用来自哪里。

### Phase 6：旧数据迁移和灰度

1.  备份旧 `knowledge.db` 和上传文件；
2.  统计旧知识库 / 文档 / Chunk；
3.  从原文件重新解析；
4.  构建新 Index Generation；
5.  运行解析和检索质量验证；
6.  新索引通过后切换；
7.  旧 SQLite 检索保留短期回滚；
8.  源文件缺失的文档标记 `migration_required`；
9.  稳定后再清理旧索引和无用依赖。

验收：旧知识库不会升级后直接失效；新索引失败可回退；文档和索引账目一致；无跨用户数据泄露。

------------------------------------------------------------------------

## 16. 测试矩阵

必须包含真实解析、真实 Embedding、真实 Qdrant、Agent Tool Calling
和浏览器 E2E，不能全部 Mock。

### 文件解析

普通 PDF、扫描 PDF、双栏 PDF、表格 PDF、DOCX、DOC、XLSX 多
Sheet、XLS、PPTX、PPT、Markdown、CSV 中文编码、HTML、PNG / JPG OCR。

### Chunking

长段落、多级标题、编号列表、多页表格、Excel 行列、PPT
多页、代码块、中英混合、超长 Token、Chunk 边界、Overlap
边界、重复文本、Parent-Child 扩展。

### Retrieval

精确关键词、同义词、自然语言、编号、数值、多条件、表格、跨章节、跨文档、无答案、干扰项、多知识库、Reranker
对比、权限过滤。

### Agent

需要时主动检索、普通问题不强制检索、只搜索绑定知识库、引用定位、无结果不编造、版本冲突说明、Prompt
Injection 防护、Run 恢复。

### 多用户安全

跨用户读取 / 上传 / 删除 / 搜索、无权绑定知识库、Qdrant Filter
篡改、SSRF localhost / RFC1918 / DNS Rebinding / 重定向、超大文件、错误
MIME、压缩炸弹、路径穿越和 Parser 资源耗尽。

### Worker 故障

领取后被杀、Parser 崩溃、Embedding 超时、Qdrant 写入后 DB
提交前崩溃、重复 Job、Lease 过期、旧 Fencing Token、删除重试、新
Generation 失败、Qdrant / Backend 重启和索引回滚。

### 前端 E2E

创建知识库、上传多格式文件、查看构建进度、查看失败原因、解析预览、Chunk、Retrieval
Test、来源、Agent 引用、重建索引和版本切换。

------------------------------------------------------------------------

## 17. 部署与配置

### 17.1 Docker Compose

按现有 Compose 结构新增：

-   `qdrant`：向量检索；
-   `knowledge-ingestion-worker`：独立 Worker；
-   可选 `knowledge-ocr-worker`：OCR 单独扩容；
-   复用现有 Backend、数据库、Redis 和模型网关。

Docling 及解析依赖放在 Worker 镜像，不要把大型 OCR /
文档解析依赖全部塞进 Backend 镜像。

### 17.2 配置项

``` text
KNOWLEDGE_ENABLED
KNOWLEDGE_STORAGE_DIR
KNOWLEDGE_MAX_UPLOAD_SIZE_MB
KNOWLEDGE_ALLOWED_FORMATS
KNOWLEDGE_PARSER_PROVIDER=docling
KNOWLEDGE_OCR_ENABLED
KNOWLEDGE_OCR_LANGUAGES
KNOWLEDGE_PARSE_TIMEOUT_SECONDS
KNOWLEDGE_MAX_PAGES
KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES

KNOWLEDGE_WORKER_ENABLED
KNOWLEDGE_WORKER_CONCURRENCY
KNOWLEDGE_WORKER_HEARTBEAT_INTERVAL
KNOWLEDGE_JOB_LEASE_SECONDS
KNOWLEDGE_JOB_MAX_ATTEMPTS

KNOWLEDGE_EMBEDDING_PROVIDER
KNOWLEDGE_EMBEDDING_BASE_URL
KNOWLEDGE_EMBEDDING_MODEL
KNOWLEDGE_EMBEDDING_DIMENSION
KNOWLEDGE_EMBEDDING_BATCH_SIZE

KNOWLEDGE_QDRANT_URL
KNOWLEDGE_QDRANT_API_KEY
KNOWLEDGE_QDRANT_COLLECTION_PREFIX

KNOWLEDGE_RETRIEVAL_MODE=hybrid
KNOWLEDGE_DENSE_TOP_K
KNOWLEDGE_BM25_TOP_K
KNOWLEDGE_RERANKER_ENABLED
KNOWLEDGE_RERANKER_MODEL
KNOWLEDGE_RERANK_TOP_K
KNOWLEDGE_CONTEXT_TOKEN_BUDGET
```

配置必须有默认值、启动校验和部署文档，敏感配置不得提交仓库。

------------------------------------------------------------------------

## 18. 迁移与兼容

当前旧索引位于 `CHROMA_PERSIST_DIR/knowledge.db`，实际是 SQLite Chunk
表。

-   备份旧数据库和源文件；
-   统计旧 KB / Document / Chunk；
-   保留旧搜索链路作为短期回滚；
-   从原始文件重新解析；
-   不把旧字符 Chunk 直接作为新质量基线；
-   缺失源文件标记 `migration_required`；
-   新索引通过后才切换；
-   不破坏 Agent 绑定；
-   保持现有 API 兼容或提供明确迁移；
-   确认没有其他模块依赖 ChromaDB 后再决定是否移除；
-   校准旧 `vector_count`，不能继续当真实向量数。

------------------------------------------------------------------------

## 19. 本次明确不做

-   知识图谱或完整 Ontology；
-   自动生成复杂企业本体；
-   训练自己的 Embedding 模型；
-   微调 Reranker；
-   第一阶段视频帧级视觉理解；
-   复杂多模态跨模态检索；
-   把全文放入 Agent Prompt；
-   重写 DeepAgents / LangGraph；
-   用知识库替代 Agent Memory；
-   引入独立 Agent Runtime；
-   为 RAG 引入 Kafka 等不必要基础设施。

------------------------------------------------------------------------

## 20. Codex / Trae 执行要求

1.  重新拉取最新 `main`，确认 Commit 和工作树。
2.  先完成 Phase 0。
3.  Phase 1 安全问题优先修复。
4.  Phase 2 跑通多格式解析、来源追踪和 Durable Worker。
5.  Phase 3 再引入 Qdrant、Embedding、Hybrid 和 Reranker。
6.  Phase 4 复用现有 Agent Tool Calling。
7.  Phase 5 完成质量可视化和评测。
8.  Phase 6 迁移旧索引并灰度。
9.  每个 Phase 独立提交。
10. 必须有真实文件、真实 Qdrant 和 Agent 集成测试。
11. 发现任务书与代码冲突，先报告差异和兼容方案。
12. 不得用 Mock 冒充真实解析、Embedding 或向量检索。
13. 不得以 API 200 或 Chunk 数大于 0 作为唯一成功标准。
14. 不得把检索分数宣传成回答准确率。
15. 每阶段输出变更文件、架构变化、测试命令、测试结果、质量指标、限制和回滚方式。

------------------------------------------------------------------------

## 21. Definition of Done

1.  所有承诺格式都有真实解析测试。
2.  扫描 PDF / 图片可以 OCR。
3.  文档结构、表格和来源位置保留。
4.  Chunking 结构感知、Token-aware。
5.  Embedding 真正生成并保存。
6.  Qdrant 有持久化和恢复能力。
7.  Dense + BM25 Hybrid 正常。
8.  RRF 和可选 Reranker 可评测。
9.  无答案时可靠返回无结果。
10. Agent 按需自主调用知识检索。
11. Agent 只能访问授权知识库。
12. 检索结果可引用原始文件位置。
13. Knowledge API 不存在跨用户越权。
14. 网页爬取有 SSRF 防护。
15. Worker 有 Lease、Heartbeat、Retry、Idempotency 和 Recovery。
16. 新索引失败不破坏旧索引。
17. 删除能清理源文件、Parsed Artifact 和 Qdrant Points。
18. 前端能看到构建、解析、Chunk 和检索证据。
19. 有可重复的 Retrieval Golden Dataset。
20. 有 Recall@K、MRR、nDCG、Citation Precision、No-answer Accuracy
    等评测。
21. 现有 Agent Runtime / Capability / AgentVersion / ToolPolicy /
    Checkpoint 回归通过。
22. Docker 部署、备份、恢复和故障排查文档完整。

------------------------------------------------------------------------

## 22. 官方参考资料

### 文档解析

-   Docling GitHub: https://github.com/docling-project/docling
-   Docling Supported Formats:
    https://docling-project.github.io/docling/usage/supported_formats/
-   Docling Architecture:
    https://docling-project.github.io/docling/concepts/architecture/
-   Unstructured GitHub: https://github.com/Unstructured-IO/unstructured
-   Unstructured Partitioning:
    https://unstructured.readthedocs.io/en/latest/core/partition.html
-   Unstructured Parsing Strategies:
    https://unstructured.readthedocs.io/en/main/best_practices/strategies.html

### 向量检索

-   Qdrant GitHub: https://github.com/qdrant/qdrant
-   Qdrant Overview: https://qdrant.tech/documentation/overview/
-   Qdrant Hybrid Queries:
    https://qdrant.tech/documentation/hybrid-queries/
-   Qdrant Hybrid Search:
    https://qdrant.tech/documentation/search-tuning/hybrid-search/
-   Qdrant Reranking:
    https://qdrant.tech/documentation/tutorials-basics/reranking-hybrid-search/

### 质量评测

-   Ragas: https://github.com/vibrantlabsai/ragas
-   Ragas Documentation: https://docs.ragas.io/
-   BEIR Benchmark: https://github.com/beir-cellar/beir

------------------------------------------------------------------------

**最终目标架构：Docling + OCR + Structure-aware Chunking + Embedding
Provider + Qdrant + Dense/BM25 Hybrid Search + RRF + Reranker +
Citation-aware Agent Tool + Durable Ingestion Worker + Retrieval
Evaluation。**
