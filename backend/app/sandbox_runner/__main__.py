"""`python -m app.sandbox_runner`：启动 Runner 内部 API / Gateway（单进程）。"""
from __future__ import annotations

import uvicorn

from app.core.config import settings


def main() -> None:
    uvicorn.run(
        "app.sandbox_runner.api:app",
        host="0.0.0.0",
        port=int(settings.SANDBOX_RUNNER_PORT or 9100),
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
