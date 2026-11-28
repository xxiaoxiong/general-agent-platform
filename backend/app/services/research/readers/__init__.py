"""Source Reader 工厂：当前默认网页/文本读取；后续可按配置接入 Browser/Docling。"""
from __future__ import annotations

from .base import SourceDocument, SourceReader
from .web import WebSourceReader, read_url

__all__ = ["SourceDocument", "SourceReader", "WebSourceReader", "read_url"]
