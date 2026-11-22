# 独立的离线工程验收运行时，只复制工作进程和报文协议。
FROM node:22-bookworm-slim AS node_runtime
WORKDIR /opt/project-node
COPY docker/project-verifier-npm/package.json docker/project-verifier-npm/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM python:3.11-slim-bookworm
WORKDIR /opt/project-verifier

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
RUN node --version

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" \
    Flask==3.1.2 Flask-SQLAlchemy==3.1.1 Flask-Cors==6.0.2 PyJWT==2.10.1 \
    fastapi==0.125.0 uvicorn==0.38.0 pytest==8.4.2 requests==2.32.5 httpx==0.28.1 playwright==1.65.2
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: -i "${PIP_INDEX_URL}" \
    --target /opt/project-python/pydantic-2.8 pydantic==2.8.2 pydantic-core==2.20.1
COPY --from=node_runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY --from=node_runtime /opt/project-node /opt/project-node
COPY backend/app/services/agent_runtime/project_verifier_protocol.py runner/
COPY backend/app/services/agent_runtime/project_browser_contract.py runner/
COPY backend/app/services/agent_runtime/project_browser_probe.py runner/
COPY backend/app/services/agent_runtime/project_node_toolchain.py runner/
COPY backend/app/services/agent_runtime/project_verifier_worker.py runner/
CMD ["python", "-B", "-u", "-m", "runner.project_verifier_worker"]
