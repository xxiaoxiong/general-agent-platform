"""Source Reader 抽象：统一网页/文件/知识库读取接口与元数据（任务书 Phase 4）。

先稳定接口、来源元数据与正文质量，再按评测结果接入 Browser / Docling；
Research Runtime 只依赖 ``SourceDocument``，不与具体抓取实现强耦合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@dataclass
class SourceDocument:
    """一次来源读取结果（正文 + 可追溯元数据）。"""

    url: str
    final_url: str
    title: str = ""
    text: str = ""
    # 全文（用于服务端证据定位；模型只拿到当前分块 text）
    full_text: str = ""
    content_type: str = ""
    http_status: int = 0
    retrieved_at: str = ""
    content_hash: str = ""
    # 长内容分块读取：offset / next_offset / has_more / total_chars
    offset: int = 0
    next_offset: int = 0
    has_more: bool = False
    total_chars: int = 0
    # 读取失败时明确区分：http_error / empty_content / unsupported_type / blocked / timeout
    error_code: str = ""
    error_detail: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error_code and bool(self.text.strip())


class SourceReader(Protocol):
    """统一来源读取接口。"""

    async def read(self, url: str, *, max_chars: int = 12000) -> SourceDocument:
        ...
