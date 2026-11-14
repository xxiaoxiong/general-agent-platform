"""API v2：产品工作台契约（任务书 §7）。

- /api/v2/workspace/bootstrap   默认助手、用户能力、工作区配置
- /api/v2/assistants            助手列表/创建/草稿/修改/发布
- /api/v2/conversations         会话列表/创建/消息历史
- /api/v2/runs                  Run 创建/状态/事件 SSE/取消/恢复
- /api/v2/capabilities          可用能力包
- /api/v2/artifacts/{id}        产物安全下载
- /api/v2/admin/observability   监控概览

v1 在迁移期保留；前端产品工作台切换到 v2。
"""
