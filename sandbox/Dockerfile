# Workspace Sandbox 基础镜像（可版本化 / 可回滚）
#
# 安全基线（任务书 §7.3）：
# - 默认非 root 用户 sandbox（uid 10001）；
# - 可在只读 rootfs + tmpfs 下运行（Runner 创建容器时配置）；
# - 不内置平台生产密钥，Agent Token 仅由 Runner 经环境变量注入。
#
# 预装 Node / Python / 常用构建工具，避免每次创建 Sandbox 重复安装系统依赖。
FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_USER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    NPM_CONFIG_PREFIX=/home/sandbox/.npm-global \
    PATH=/home/sandbox/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    WORKSPACE_ROOT=/workspace \
    WORKSPACE_AGENT_PORT=19999 \
    WORKSPACE_AGENT_MAX_OUTPUT=200000 \
    WORKSPACE_AGENT_MAX_FILE_MB=8 \
    WORKSPACE_AGENT_LOG_MAX_BYTES=5000000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        build-essential \
        git \
        curl \
        ca-certificates \
        tini \
        procps \
        unzip \
        jq \
        ripgrep \
        less \
        vim-tiny \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash sandbox \
    && mkdir -p /opt/workspace-agent /workspace \
        /home/sandbox/.npm /home/sandbox/.npm-global \
        /home/sandbox/.cache /home/sandbox/.local \
    && chown -R sandbox:sandbox /workspace /home/sandbox

COPY workspace_agent.py /opt/workspace-agent/workspace_agent.py
RUN chmod 0755 /opt/workspace-agent/workspace_agent.py

USER sandbox
WORKDIR /workspace
EXPOSE 19999

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/workspace-agent/workspace_agent.py"]
