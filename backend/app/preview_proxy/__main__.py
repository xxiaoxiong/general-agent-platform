"""`python -m app.preview_proxy`：启动 Preview Proxy。"""
from __future__ import annotations

import uvicorn

from app.core.config import settings


def main() -> None:
    uvicorn.run(
        "app.preview_proxy.app:app",
        host="0.0.0.0",
        port=int(settings.PREVIEW_PROXY_PORT or 8081),
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
