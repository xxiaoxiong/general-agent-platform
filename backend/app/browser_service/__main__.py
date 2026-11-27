"""``python -m app.browser_service``：启动内置浏览器服务。"""
from __future__ import annotations

import logging
import os

import uvicorn

from .main import app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("BROWSER_SERVICE_PORT", "") or 9300)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
