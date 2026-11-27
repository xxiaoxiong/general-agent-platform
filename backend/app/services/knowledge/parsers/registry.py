"""Parser 选择：显式 docling / native，或 auto（Docling 可用则优先）。"""
from __future__ import annotations

from app.core.config import settings

from ..model import ParseError
from .base import DocumentParser
from .docling_parser import DoclingParser, docling_available
from .native import NativeParser

_NATIVE = NativeParser()
_DOCLING = DoclingParser()


def active_parser_name() -> str:
    provider = (settings.KNOWLEDGE_PARSER_PROVIDER or "auto").lower()
    if provider == "native":
        return "native"
    if provider == "docling":
        if not docling_available():
            raise ParseError(
                "配置要求 Docling 解析（KNOWLEDGE_PARSER_PROVIDER=docling），"
                "但当前环境未安装 docling",
                code="docling_unavailable",
            )
        return "docling"
    return "docling" if docling_available() else "native"


def get_parser(provider: str = "") -> DocumentParser:
    provider = (provider or settings.KNOWLEDGE_PARSER_PROVIDER or "auto").lower()
    if provider == "native":
        return _NATIVE
    if provider == "docling":
        if not docling_available():
            raise ParseError(
                "配置要求 Docling 解析（KNOWLEDGE_PARSER_PROVIDER=docling），"
                "但当前环境未安装 docling",
                code="docling_unavailable",
            )
        return _DOCLING
    return _DOCLING if docling_available() else _NATIVE


__all__ = [
    "DocumentParser", "NativeParser", "DoclingParser",
    "active_parser_name", "get_parser", "docling_available",
]
