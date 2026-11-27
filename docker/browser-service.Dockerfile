# 内置浏览器服务：平台侧无头 Chromium，经 Preview Proxy 访问 Sandbox Service。
# 独立镜像/进程，不挂载 Docker Socket，不映射宿主机端口。
FROM python:3.11-slim-bookworm

WORKDIR /opt/browser

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/browser

# uvicorn[standard] 必须带 WebSocket 实现（帧流经 /sessions/{id}/stream 推送）
RUN python -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" \
    fastapi==0.125.0 "uvicorn[standard]==0.38.0" playwright==1.65.2
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY backend/app/__init__.py app/__init__.py
COPY backend/app/browser_service app/browser_service

EXPOSE 9300
CMD ["python", "-m", "app.browser_service"]
