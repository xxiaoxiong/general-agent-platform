from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit
import json

# P0-04：生产环境拒绝的默认/弱凭据（大小写不敏感）
_INSECURE_SECRET_KEYS = frozenset({
    "change-me-in-production-min-32-chars-long!!",
    "change-me-in-production-please-use-a-long-random-key",
})
_INSECURE_ADMIN_PASSWORDS = frozenset({
    "admin@123456", "admin123", "admin123456", "password", "123456",
    "password123", "changeme", "root", "test",
})


class Settings(BaseSettings):
    APP_NAME: str = "General Agent Platform"
    APP_VERSION: str = "2.0.0"
    APP_ENV: str = "development"            # development | staging | production
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production-min-32-chars-long!!"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str = "mysql+aiomysql://root:password@localhost:3307/ai_plat"
    DATABASE_SYNC_URL: str = "mysql+pymysql://root:password@localhost:3307/ai_plat"

    REDIS_URL: str = "redis://localhost:6379/0"

    CHROMA_PERSIST_DIR: str = "./data/chroma"

    UPLOAD_DIR: str = "./data/uploads"
    MAX_UPLOAD_SIZE_MB: int = 100

    # 语音听写。未配置地址时，对话可继续使用普通文本输入。
    ASR_BASE_URL: str = ""
    ASR_MODEL: str = "sensevoice"
    ASR_TIMEOUT_SECONDS: float = Field(default=60.0, ge=1, le=120)
    ASR_MAX_UPLOAD_MB: int = Field(default=10, ge=1, le=16)
    ASR_MAX_AUDIO_SECONDS: int = Field(default=60, ge=1, le=120)

    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "Admin@123456"
    ADMIN_USERNAME: str = "admin"

    CORS_ORIGINS: str = '["http://localhost:3000","http://localhost:5173"]'

    # ── Agent 执行引擎配置 ────────────────────────────────────────────────────
    AGENT_MAX_ITERATIONS: int = 15              # 全局默认最大 ReAct 迭代
    AGENT_MAX_TOOL_RETRIES: int = 2             # 工具调用失败重试次数
    AGENT_TOOL_TIMEOUT_SECONDS: int = 30        # 单次工具执行超时
    AGENT_MAX_SAME_TOOL_CALLS: int = 3          # 同一工具最大连续调用次数
    AGENT_TOOL_MAX_CONSECUTIVE_FAILURES: int = 6  # 同一工具连续失败熔断阈值（防误用死循环）
    AGENT_TEMPERATURE: float = 0.3              # 30B 模型建议低温度
    AGENT_ENABLE_SELF_VERIFY: bool = True       # 启用自我校验
    AGENT_ENABLE_SOURCE_CITATION: bool = True   # 启用来源引用

    # ── 推理层配置（30B 模型优化）──────────────────────────────────────────────
    INFERENCE_MAX_CONTEXT_TOKENS: int = 16384   # 最大上下文窗口 token 数
    INFERENCE_MAX_OUTPUT_TOKENS: int = 8192     # 最大输出 token 数
    INFERENCE_KV_CACHE_MAX_MB: int = 4096       # KV Cache 最大显存占用 MB
    INFERENCE_CONCURRENT_REQUESTS: int = 2      # 最大并发推理请求
    INFERENCE_REQUEST_QUEUE_SIZE: int = 20      # 请求队列大小
    INFERENCE_REQUEST_TIMEOUT: int = 120        # 单次推理请求超时秒数
    INFERENCE_BATCH_SIZE: int = 1               # 批处理大小

    # ── 上下文管理配置 ────────────────────────────────────────────────────────
    CONTEXT_SLIDING_WINDOW_TURNS: int = 6       # 滑动窗口保留最近 N 轮完整对话
    CONTEXT_MAX_HISTORY_TOKENS: int = 2048      # 历史信息蒸馏后最大 token 数
    CONTEXT_SUMMARY_THRESHOLD: int = 4096       # 超过此 token 数触发历史摘要
    CONTEXT_PIN_SYSTEM_PROMPT: bool = True       # 系统提示词永远置顶
    CONTEXT_PIN_ORIGINAL_TASK: bool = True       # 原始任务永不丢失

    # ── 记忆系统配置 ──────────────────────────────────────────────────────────
    MEMORY_SHORT_TERM_MAX_TURNS: int = 6        # 短期记忆保留完整轮数
    MEMORY_LONG_TERM_MAX_ITEMS: int = 200       # 长期记忆最大条目
    MEMORY_IMPORTANCE_THRESHOLD: float = 0.5    # 记忆重要性阈值
    MEMORY_AUTO_DISTILL: bool = True            # 自动蒸馏旧历史
    MEMORY_STRUCTURED_STORE: bool = True        # 启用结构化记忆（知识/规则/技能）
    MEMORY_CONFLICT_STRATEGY: str = "newer"     # 记忆冲突策略：newer / higher_importance / merge
    MEMORY_DB_PATH: str = "./data/memory"       # 记忆数据库路径

    # ── 幻觉抑制配置 ──────────────────────────────────────────────────────────
    HALLUCINATION_FACT_CHECK: bool = True       # 启用事实校验
    HALLUCINATION_MAX_VERIFY_ROUNDS: int = 2    # 重要结果验证轮数
    HALLUCINATION_FORCE_SOURCE: bool = True     # 强制来源引用
    HALLUCINATION_UNCERTAINTY_EXPR: bool = True  # 允许不确定性表达

    # ── 监控与日志配置 ────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./data/logs"
    LOG_AGENT_TRACE: bool = True                # 记录 Agent 执行轨迹
    LOG_TOOL_CALLS: bool = True                 # 记录工具调用详情
    LOG_PERFORMANCE: bool = True                # 记录性能指标
    MONITOR_ENABLE: bool = True                 # 启用性能监控
    MONITOR_SLOW_THRESHOLD_MS: int = 5000       # 慢请求阈值

    # ── 搜索引擎配置（内网环境）────────────────────────────────────────────────
    SEARXNG_URL: str = ""                        # SearXNG 实例地址，如 http://localhost:8888
    SEARCH_TIMEOUT: int = 25                     # 搜索超时秒数
    # 提示词中"当前时间"的时区（容器通常为 UTC，直接取本地日期会在凌晨差一天）
    AGENT_TIMEZONE: str = "Asia/Shanghai"

    # ── 稳定性配置 ────────────────────────────────────────────────────────────
    HOT_RELOAD_TOOLS: bool = True               # 工具热重载
    HOT_RELOAD_PROMPTS: bool = True             # 提示词热重载
    SESSION_ISOLATION: bool = True              # 多会话内存隔离
    DETERMINISTIC_MODE: bool = False            # 确定性模式（固定 seed）
    DETERMINISTIC_SEED: int = 42                # 确定性模式种子
    CHECKPOINT_ENABLED: bool = True             # 启用断点续跑
    CHECKPOINT_DIR: str = "./data/checkpoints"  # 断点存储目录

    # ── DeepAgents Runtime（阶段一新内核）────────────────────────────────────
    AGENT_MAX_STEPS: int = 1000                 # 主图模型轮数上限（0=不限制）
    AGENT_MAX_TOOL_CALLS: int = 1000            # 工具调用总次数预算（0=不限制）
    AGENT_MAX_MODEL_CALLS: int = 1000           # 模型调用次数预算（0=不限制）
    AGENT_RUN_TIMEOUT: float = 0.0              # 单次运行总时限（秒），0=不限制
    # P1-05 §13：HITL 等待时间是否计入 Run Timeout。True=计入（deadline 绝对、含等待）；
    # False=不计入（HITL 恢复时按等待时长顺延 deadline，只耗执行时间不耗人工等待）。
    AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT: bool = False
    AGENT_CHECKPOINT_DSN: str = "memory"        # memory | sqlite:///./data/agent_checkpoints.db | postgresql+asyncpg://...
    AGENT_STORE_DSN: str = ""                   # 跨线程记忆 Store（空=不启用）
    AGENT_ENABLE_HITL: bool = True              # 人工审批（高危工具 interrupt_on）
    # 模型驱动的语义决策（Decision Architecture）：
    # off=只用规则路由；shadow=同时计算模型决策但执行规则结果；llm=校验后执行模型决策
    AGENT_DECISION_MODE: str = "llm"
    AGENT_DECISION_TIMEOUT: float = 12.0        # 秒；超时/非法输出回退规则路由
    AGENT_DECISION_MAX_TOKENS: int = 600        # 结构化决策输出上限
    # 自动模式下是否允许软件项目意图自动进入团队工程路线（默认关闭，保持兼容）
    AGENT_AUTO_TEAM_ENABLED: bool = False
    AGENT_SANDBOX_MODE: str = "restricted"      # restricted / disabled / external（disabled 时禁用高危执行工具）
    AGENT_ALLOW_UNSAFE_PYTHON: bool = False     # 代码执行逃生开关（仅开发）：restricted 不是安全沙箱，生产强制禁用 python_exec/execute
    AGENT_EGRESS_PROXY: str = ""                # 受控 Egress Proxy（HTTP 出口统一收口；DNS Rebinding 防护的等价机制）
    AGENT_ALLOW_NETWORK: bool = True            # 允许工具网络访问
    AGENT_TLS_VERIFY: bool = True               # TLS 证书校验（禁止生产关闭）
    AGENT_CA_BUNDLE: str = ""                   # 自定义 CA 证书路径（内网自签）
    AGENT_NETWORK_ALLOWLIST: str = ""           # 逗号分隔受信任主机；配置后仅允许名单内（可含内网）
    AGENT_NETWORK_BLOCKED_CIDRS: str = ""       # 逗号分隔封禁网段
    AGENT_FILES_ROOT: str = "./data/agent_files"  # 工具文件工作区根目录
    AGENT_MAX_FILE_SIZE_MB: int = 10            # 单文件大小上限
    AGENT_DISABLED_SKILLS: str = ""             # 逗号分隔禁用的 skill 名
    AGENT_SKILLS_DIR: str = ""                  # 外部技能目录（<name>/SKILL.md，组织范围）
    AGENT_MCP_TIMEOUT_SECONDS: float = 30.0     # MCP 单次工具调用/发现超时
    AGENT_EVENT_RETENTION_DAYS: int = 30        # 运行记录保留天数
    AGENT_EVENT_CLEANUP_INTERVAL: float = 3600.0  # 事件保留清理执行间隔（秒，仅清理终态超期 Run）
    # ── P4-01/02：Task Contract + Verification ────────────────────────────
    AGENT_VERIFICATION_ENABLED: bool = True     # 复杂任务完成条件验证（确定性，不调模型）
    AGENT_VERIFICATION_MAX_ROUNDS: int = 2      # 原运行中的补充轮数（0=只验证不补充）
    AGENT_PROJECT_VERIFIER_DIR: str = ""       # 离线工程验收共享队列（Compose 默认接入）
    # ── P4-03：工具瞬态失败重试（按风险策略；非幂等绝不盲目重放）──────────
    AGENT_TOOL_MAX_RETRIES: int = 2             # 运行内瞬态失败重试上限（0=不重试）
    AGENT_TOOL_RETRY_BACKOFF_SECONDS: float = 0.5  # 重试退避基数（线性退避 attempt*基数）
    AGENT_TOOL_TIMEOUT_SECONDS: float = 120.0   # 单次工具执行超时（超时按瞬态失败处理）
    AGENT_INNER_TIMEOUT_RETRIES: int = 1        # 单步内层请求超时（模型流停滞等）从断点重试次数

    # ── Durable Execution（Phase 2）：Worker 租约协调 ────────────────────
    AGENT_RUNTIME_WORKER_ID: str = ""           # Worker 标识（空=自动生成 hostname-pid-随机）
    AGENT_RUNTIME_LEASE_SECONDS: int = 60       # Run 租约时长；过期后可被其他 Worker 接管
    AGENT_RUNTIME_HEARTBEAT_INTERVAL: float = 15.0  # 续租间隔（应小于租约的 1/2）
    AGENT_RUNTIME_POLL_INTERVAL: float = 1.0    # 扫描待执行/孤儿 Run 的间隔
    AGENT_SSE_POLL_INTERVAL: float = 0.3        # SSE 事件下发轮询兜底间隔（同进程由事件唤醒立即推送）
    AGENT_DELTA_FLUSH_INTERVAL: float = 0.2     # MODEL_DELTA 落库时间上限（秒）：SSE 只读 DB，
                                                # 增量攒批不能只按条数（32 条≈1s），必须按时间兜底才够"流式"
    AGENT_RUNTIME_SHUTDOWN_GRACE: float = 30.0  # 优雅关闭时等待在途 Run 的秒数

    # ── 可观测性（Phase 1）：Langfuse / OpenTelemetry ─────────────────────
    LANGFUSE_PUBLIC_KEY: str = ""               # 未配置时整条 Langfuse 链路为 no-op
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = "https://cloud.langfuse.com"
    LANGFUSE_TRACING_SAMPLE_RATE: float = 1.0   # 0.0~1.0 采样率（P1-04）
    LANGFUSE_RELEASE: str = ""                  # 版本关联（缺省用 APP_VERSION）
    LANGFUSE_PUBLIC_URL: str = ""               # 浏览器可达的 Langfuse 地址（Trace 跳转链接）；
                                                # 缺省回落 LANGFUSE_BASE_URL（容器内外地址不同时需单独配置）
    LANGFUSE_PROJECT_ID: str = ""               # Trace 页面路由需要的项目 ID（/project/{id}/traces/...）；
                                                # 缺省经 Public API 自动发现并缓存

    # ── 自进化闭环（评估引擎 + 优化建议）──────────────────────────────────
    EVOLUTION_ENABLED: bool = True              # 总开关（关闭时不启动周期评估）
    EVOLUTION_EVAL_INTERVAL_SECONDS: int = 300  # 周期评估扫描间隔
    EVOLUTION_EVAL_BATCH_SIZE: int = 10         # 每轮最多评估的 Run 数
    EVOLUTION_JUDGE_MODEL_CONFIG_ID: int = 0    # LLM-as-judge 模型（0=复用 Run 的模型配置）
    EVOLUTION_VALIDATION_MIN_RUNS: int = 5      # 变更验证所需的新评估样本数

    # ── Agent 测评（Evaluation Worker / Campaign）────────────────────────
    EVALUATION_WORKER_ENABLED: bool = True      # 持久化 Campaign Worker 总开关
    EVALUATION_WORKER_ID: str = ""              # 显式 Worker ID（缺省自动生成）
    EVALUATION_POLL_INTERVAL: float = 5.0       # 扫描 pending / 租约过期 Campaign 间隔
    EVALUATION_WORKER_BATCH: int = 2            # 单轮最多领取的 Campaign 数
    EVALUATION_LEASE_SECONDS: int = 120         # Campaign 租约时长
    EVALUATION_HEARTBEAT_INTERVAL: float = 20.0  # 心跳续租间隔
    EVALUATION_INFRA_RETRY_MAX: int = 1         # 基础设施故障保守重试次数（无成功副作用时）
    EVALUATION_JUDGE_MODEL_CONFIG_ID: int = 0   # 独立 Judge 模型（0=回退 EVOLUTION_JUDGE_MODEL_CONFIG_ID）

    # ── Workspace Sandbox Runtime（项目级持久化工作空间，任务书 §13.4）──
    SANDBOX_ENABLED: bool = False               # 总开关（关闭时全部 Sandbox API/工具/面板不可用）
    SANDBOX_PROVIDER: str = "docker"            # 第一阶段仅 docker
    SANDBOX_RUNNER_ID: str = ""                 # Runner 节点标识（缺省 hostname）
    SANDBOX_RUNNER_TOKEN: str = ""              # Runner 内部服务令牌（启用时必填，fail-closed）
    # 灰度白名单：为空=全部；非空=仅列出的用户/项目启用（任务书 §18 Feature Flag）
    SANDBOX_ROLLOUT_USER_IDS: str = ""          # 逗号分隔 user_id
    SANDBOX_ROLLOUT_PROJECT_IDS: str = ""       # 逗号分隔 project_id
    SANDBOX_RUNNER_CONTROL_URL: str = "http://sandbox-runner:9100"
    SANDBOX_RUNNER_PORT: int = 9100             # Runner 内部 API / Gateway 监听端口
    SANDBOX_BACKEND_URL: str = "http://backend:8000"  # Runner 回调 Backend 内部 API
    SANDBOX_RUNNER_DISK_GB: int = 100           # Runner 节点磁盘总量（用于预留记账）
    SANDBOX_RUNNER_HEARTBEAT_INTERVAL: float = 15.0
    SANDBOX_JOB_LEASE_SECONDS: int = 90         # Job 租约时长
    SANDBOX_JOB_HEARTBEAT_INTERVAL: float = 20.0
    SANDBOX_RECONCILE_INTERVAL: float = 30.0    # 对账 / 空闲回收 / 租约回收扫描间隔
    SANDBOX_DEFAULT_PROFILE: str = "standard"
    SANDBOX_MAX_ACTIVE_PER_USER: int = 2        # 每用户最大活动 Sandbox 数（0=不限）
    SANDBOX_MAX_ACTIVE_PER_TENANT: int = 20     # 每租户/组织最大活动 Sandbox 数（0=不限）
    SANDBOX_IDLE_TIMEOUT: int = 1800            # 空闲回收秒数（0=不自动回收）
    SANDBOX_IMAGE: str = "general-agent-platform/workspace-sandbox:1.0.0"
    SANDBOX_NETWORK_POLICY: str = "egress"      # egress=允许公网出站；none=完全断网
    SANDBOX_VOLUME_QUOTA_MODE: str = "soft"     # soft=监控预警；hard=宿主支持时硬配额
    SANDBOX_CPU_RESERVE: float = 2.0            # 宿主机为 OS/平台保留的 CPU 核数
    SANDBOX_MEMORY_RESERVE: int = 2048          # 宿主机保留内存（MB）
    SANDBOX_DISK_RESERVE: int = 20              # 宿主机保留磁盘（GB）
    SANDBOX_DOCKER_SOCKET: str = "/var/run/docker.sock"
    SANDBOX_AGENT_PORT: int = 19999             # Sandbox 内 workspace-agent 端口
    SANDBOX_COMMAND_TIMEOUT: int = 600          # 长命令最大运行时长（秒）
    SANDBOX_COMMAND_MAX_OUTPUT_BYTES: int = 200_000  # 单次命令输出上限（截断）
    SANDBOX_SYNC_COMMAND_WAIT: float = 30.0     # execute_command 同步等待上限（超过转异步）
    SANDBOX_VOLUME_PREFIX: str = "gap-ws-"
    SANDBOX_CONTAINER_PREFIX: str = "gap-sbx-"
    SANDBOX_RUNNER_CONTAINER: str = ""          # 为空时使用容器内 HOSTNAME（= 容器 ID）
    SANDBOX_WRITE_LEASE_SECONDS: int = 120      # Workspace 写租约时长
    SANDBOX_WRITE_LEASE_HEARTBEAT_INTERVAL: float = 30.0
    SANDBOX_JOB_MAX_ATTEMPTS: int = 3
    SANDBOX_JOB_AGING_SECONDS: float = 60.0     # 每等待 60s 提升一级优先级（防饥饿）
    PREVIEW_BASE_DOMAIN: str = "localhost"      # 生产配置独立 Preview 域名（含 TLS）
    PREVIEW_PUBLIC_BASE_URL: str = ""           # 空=按 {token}.{PREVIEW_BASE_DOMAIN}:{端口} 生成
    PREVIEW_TOKEN_TTL: int = 3600               # Preview Grant 有效期（秒）
    PREVIEW_MAX_CONNECTIONS: int = 16           # 单 Preview 并发连接上限
    PREVIEW_PROXY_PORT: int = 8081              # Preview Proxy 容器端口
    PREVIEW_PROXY_HOST_PORT: int = 8081         # 浏览器可达端口（compose 映射）

    OTEL_ENABLED: bool = False                  # OpenTelemetry 导出（P1-02）
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""       # 如 http://localhost:4317
    OTEL_SERVICE_NAME: str = "general-agent-platform"

    @property
    def cors_origins_list(self) -> List[str]:
        return json.loads(self.CORS_ORIGINS)

    @property
    def asr_enabled(self) -> bool:
        return bool(self.ASR_BASE_URL.strip())

    @property
    def asr_max_file_bytes(self) -> int:
        return self.ASR_MAX_UPLOAD_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return (self.APP_ENV or "").lower() in ("production", "prod")

    def validate_startup(self) -> None:
        """启动期配置校验（P0-01）：不合法配置直接抛错拒绝启动，而不是运行到故障才发现。

        生产环境禁止 Durable Runtime 使用 Memory Checkpointer——Memory 是进程内
        单例，多 Worker / 重启后 Graph State 无法恢复。
        """
        if self.AGENT_DECISION_MODE not in {"off", "shadow", "llm"}:
            raise RuntimeError(
                "配置非法：AGENT_DECISION_MODE 只能为 off / shadow / llm。"
            )
        if self.asr_enabled:
            self._validate_asr_settings()
        self._validate_sandbox_settings()
        verifier_directory = self.AGENT_PROJECT_VERIFIER_DIR.strip()
        if verifier_directory and not Path(verifier_directory).is_absolute():
            raise RuntimeError("配置非法：AGENT_PROJECT_VERIFIER_DIR 须为共享队列的绝对路径。")
        if self.is_production:
            dsn = (self.AGENT_CHECKPOINT_DSN or "").strip().lower()
            if dsn in ("", "memory"):
                raise RuntimeError(
                    "配置非法，拒绝启动：APP_ENV=production 时"
                    "禁止使用 Memory Checkpointer（AGENT_CHECKPOINT_DSN=memory）。"
                    "请配置持久化后端（postgresql://... 或 sqlite:///...），"
                    "多 Worker 部署必须使用 PostgreSQL 以共享 Graph State。"
                )
            self._validate_production_secrets()

    def _validate_sandbox_settings(self) -> None:
        """Workspace Sandbox 配置校验：启用时 fail-closed（缺内部令牌拒绝启动）。"""
        if self.SANDBOX_PROVIDER not in {"docker"}:
            raise RuntimeError("配置非法：SANDBOX_PROVIDER 第一阶段仅支持 docker。")
        if self.SANDBOX_NETWORK_POLICY not in {"egress", "none"}:
            raise RuntimeError("配置非法：SANDBOX_NETWORK_POLICY 只能为 egress / none。")
        if self.SANDBOX_VOLUME_QUOTA_MODE not in {"soft", "hard"}:
            raise RuntimeError("配置非法：SANDBOX_VOLUME_QUOTA_MODE 只能为 soft / hard。")
        if not self.SANDBOX_ENABLED:
            return
        if len((self.SANDBOX_RUNNER_TOKEN or "").strip()) < 16:
            raise RuntimeError(
                "配置非法，拒绝启动：SANDBOX_ENABLED=true 时 SANDBOX_RUNNER_TOKEN 必须为"
                "至少 16 位的内部服务令牌（Runner / Preview Proxy / Backend 共用）。"
            )
        if self.APP_ENV.lower() in ("production", "prod"):
            base = (self.PREVIEW_PUBLIC_BASE_URL or "").strip()
            if base and not base.startswith("https://"):
                raise RuntimeError(
                    "配置非法：生产环境 PREVIEW_PUBLIC_BASE_URL 必须为 https:// 地址"
                    "（Preview 与控制台必须不同 Origin）。"
                )

    def _validate_asr_settings(self) -> None:
        error = "配置非法：ASR_BASE_URL 必须是无认证、查询参数和片段的 HTTP(S) 服务地址。"
        try:
            url = urlsplit(self.ASR_BASE_URL.strip())
            port = url.port
        except ValueError:
            raise RuntimeError(error) from None
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname or url.username is not None
            or url.query or url.fragment or port == 0
        ):
            raise RuntimeError(error)
        if not self.ASR_MODEL.strip():
            raise RuntimeError("配置非法：ASR_MODEL 不能为空。")

    def _validate_production_secrets(self) -> None:
        """P0-04 部署安全：生产环境拒绝默认凭据，强制经环境变量/Secret 注入真实值。"""
        secret = (self.SECRET_KEY or "").strip()
        if secret.lower() in _INSECURE_SECRET_KEYS or "change-me" in secret.lower() or len(secret) < 32:
            raise RuntimeError(
                "配置非法，拒绝启动：APP_ENV=production 时 SECRET_KEY 必须为"
                "至少 32 位的随机密钥，不得使用默认占位值。"
                "请通过环境变量或 Secret 注入（如 openssl rand -hex 32 生成）。"
            )
        admin_pwd = (self.ADMIN_PASSWORD or "").strip()
        if admin_pwd.lower() in _INSECURE_ADMIN_PASSWORDS or len(admin_pwd) < 12:
            raise RuntimeError(
                "配置非法，拒绝启动：APP_ENV=production 时 ADMIN_PASSWORD 必须"
                "更换为至少 12 位的强初始口令，不得使用默认值 Admin@123456 等。"
                "请通过环境变量或 Secret 注入，首次登录后立即修改。"
            )

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
