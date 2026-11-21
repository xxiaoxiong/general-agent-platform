# 文档智能 API

所有业务请求使用 `Authorization: Bearer <token>`。文档文件使用同样的认证方式，数据与文件响应采用私有缓存策略。

## 创建与续跑

`POST /api/v2/runs` 接收现有消息、会话、助手和附件字段，以及可选的文档参数。

| 字段 | 类型与用途 |
| --- | --- |
| `task_mode` | `auto` 默认，可选 `chat`、`document_analyze`、`profile_extract`、`document_generate`、`document_edit` |
| `document_profile_id` | 所选规范 ID |
| `document_id` | 分析或编辑的文档 ID |
| `task_options.profile_version_no` | 选择可以访问的历史规范版本 |
| `task_options.document_version_no` | 选择文档历史版本 |
| `attachments` | 当前会话上传完成后返回的 `path` 和可选 `text_path` |

创建成功后使用 `run_id`、`conversation_id`、`events_url` 关联会话。可通过 `Idempotency-Key` 重试同一请求。服务固定已校验的来源引用、版本和解析路线，使用同一个键提交不同请求返回 409。

通过 `GET /api/v2/runs/{run_id}` 查询执行状态、固定的版本与 `pending_input`。当状态为 `waiting_input` 时，向 `POST /api/v2/runs/{run_id}/input` 提交匹配的请求 ID 与字段。

```json
{
  "request_id": "<pending_input.request_id>",
  "values": {"finish_date": "2026-09-30"}
}
```

根据 `pending_input.input_schema` 构建表单。规范确认使用 `values.definition` 与布尔 `values.confirm`。输入校验失败返回 422。重发相同内容返回已受理状态，改用不同值提交同一请求返回 409。退出会话以后可按原 Run ID 继续。

`GET /api/v2/runs/{run_id}/events` 使用 SSE，按 `Last-Event-ID` 或 `after_seq` 从对应序号继续读取。文档事件包括 `document.profile_draft.created`、`document.profile_saved`、`document.section.completed`、`document.validation.completed`、`input.required`、`input.received`、`document.version.created`、`artifact.created`。产物事件携带文档 ID、不可变版本 ID、版本号、标题、文件名、类型、大小、规范版本和已认证的预览与下载路径。

## 文档规范

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v2/document-profiles` | 列出可见规范，支持 `page`、`page_size` 和 `status` |
| `POST /api/v2/document-profiles` | 用 `definition`、可选 `visibility`、`change_summary` 创建草稿和版本 1 |
| `GET /api/v2/document-profiles/{id}` | 返回调用者可见的规范与定义 |
| `PUT /api/v2/document-profiles/{id}` | 传入完整定义与可选 `expected_version`，创建新版本 |
| `GET /api/v2/document-profiles/{id}/versions` | 管理者查看历史版本，其他组织成员查看已发布版本 |
| `POST /api/v2/document-profiles/{id}/publish` | 发布当前版本，可传 `{"expected_version": 2}` 防止并发更新后误发布 |
| `POST /api/v2/document-profiles/{id}/archive` | 归档，阻止新任务选用 |
| `DELETE /api/v2/document-profiles/{id}` | 隐藏规范并保留版本追溯 |

规范定义包括 `name`、`document_type`、`sections`，以及可选的 `required_inputs`、`language`、`audience`、`tone`、`formatting`、`quality_rules` 和 `uncertainties`。章节锚点与顺序必须唯一。创建或管理组织规范需要超管权限。

## 文档和版本

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v2/documents` | 分页列出当前用户的文档 |
| `GET /api/v2/documents/tasks` | 分页列出当前用户的文档任务 |
| `GET /api/v2/documents/{id}` | 资产详情及当前版本 |
| `GET /api/v2/documents/{id}/versions` | 版本的文件信息、生成 Run、规范版本、校验和差异 |
| `GET /api/v2/documents/{id}/versions/{version}/preview` | 经过文件完整性检查的结构化正文 |
| `GET /api/v2/documents/{id}/versions/{version}/download` | 下载该版本的 DOCX |
| `DELETE /api/v2/documents/{id}` | 隐藏文档，清除全部版本二进制文件 |

跨用户的私有资源访问返回 404。当前版本已推进、文件指纹异常或写入目标冲突时返回 409。文档生成与编辑均通过 Run 发起。
