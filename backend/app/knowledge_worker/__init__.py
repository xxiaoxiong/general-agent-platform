"""Knowledge Ingestion Worker（独立进程，任务书 §9）。

启动：``python -m app.knowledge_worker``
- 数据库为任务权威队列；
- CAS 领取 + Lease + Heartbeat + 过期接管；
- 并发度 / 轮询间隔由 KNOWLEDGE_WORKER_* 配置。
"""
from .main import run_once, run_worker

__all__ = ["run_worker", "run_once"]
