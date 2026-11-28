"""Agent Team Runtime 领域异常（服务层统一映射，不向模型泄露内部细节）。"""


class TeamError(Exception):
    """团队运行错误基类。"""


class TeamNotFound(TeamError):
    """团队 / 成员 / 任务 / 消息 / 产物不存在或不属于当前团队。"""


class TaskConflict(TeamError):
    """CAS 版本冲突 / 租约失效 / 任务已被其他成员认领。"""


class TaskStateError(TeamError):
    """非法任务状态迁移。"""


class PlanValidationError(TeamError):
    """团队计划结构/依赖校验失败（self/duplicate/missing/cycle）。"""


class MessageError(TeamError):
    """消息发送/确认失败。"""


class ArtifactError(TeamError):
    """产物创建/读取失败（权限、大小、校验）。"""


class MemberStateError(TeamError):
    """成员非法状态迁移或重复启动。"""
