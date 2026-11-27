"""Work Orchestrator：跨执行引擎的工作任务编排层。

对外入口：
- ``integration.prepare_plan`` / ``integration.drive_plan``：与现有 ``_drive`` 集成；
- ``schemas.WorkPlan`` / ``WorkPlanRecord``：计划与运行时状态契约；
- ``graph.WorkOrchestrator``：Durable 编排图（任务书 §7）。
"""
