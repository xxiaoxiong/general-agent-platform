"""pytest 全局配置：在任何 app 导入之前把测试环境指向本地隔离资源。

- 业务库用临时 SQLite 文件（不依赖外部 MySQL 服务）
- Checkpoint 用内存模式（可按需覆盖）
"""
import os
import tempfile

_TEST_DIR = tempfile.mkdtemp(prefix="ai_general_plat_test_")

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DIR}/test.db")
os.environ.setdefault("DATABASE_SYNC_URL", f"sqlite:///{_TEST_DIR}/test.db")
# 强制关闭 DEBUG（真实 env 优先于 .env 文件）：DEBUG=true 会让 engine echo=True，
# SQL 日志写到 stdout，污染多进程测试子进程的协议行解析（CLAIM:/OK: 等）
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("AGENT_CHECKPOINT_DSN", "memory")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("AGENT_FILES_ROOT", os.path.join(_TEST_DIR, "agent_files"))
# 测试夹具域 *.example.com 显式信任（服务级流程测试的假 Webhook/HTTP 目标，
# 由 ToolPolicy.from_settings 读取）；安全基线测试直接构造 ToolPolicy，不受影响。
os.environ.setdefault("AGENT_NETWORK_ALLOWLIST", "example.com")
# 自进化评估循环不在测试进程内后台运行（各测试直接调用评估函数）
os.environ.setdefault("EVOLUTION_ENABLED", "false")
# 语义决策默认关闭：测试进程不发起真实模型调用；需要覆盖决策行为的用例
# 直接 monkeypatch decision.service.invoke_decision_model 或调用纯函数
os.environ.setdefault("AGENT_DECISION_MODE", "off")
# 测试进程禁用真实 Langfuse 导出：密钥经 .env 注入会让 SDK span 导出/flush/
# LangChain 回调真实打 HTTP（localhost:3100），扰动租约/并发等时序敏感用例；
# 需要 Langfuse 行为的测试自行 monkeypatch settings（见 test_observability_langfuse）
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "")
