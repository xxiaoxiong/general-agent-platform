"""Knowledge Ingestion & RAG 服务包。

模块：
- ``model``：ParsedDocument / Provenance 统一文档模型；
- ``parsers``：Docling / Native Parser Adapter；
- ``chunking``：结构感知 + Token-aware Chunker（Parent-Child）；
- ``embedding``：Embedding Provider（fastembed / OpenAI-compatible）与 Reranker；
- ``vector_store``：Qdrant Adapter（真实引擎，Filter 由服务端注入）；
- ``retrieval``：Dense + BM25 + RRF + Reranker + 上下文扩展 + 引用；
- ``ingestion``：Durable Job、Generation、删除一致性；
- ``acl``：知识库 ACL；
- ``security``：Magic Bytes / 压缩炸弹 / SSRF；
- ``storage``：服务端生成存储路径与流式写入；
- ``evaluation``：Recall / MRR / nDCG / No-answer 等确定性评测。
"""
from .acl import (
    KnowledgeAccessDenied, KnowledgeNotFoundError, accessible_bases_filter,
    filter_authorized_ids, require_base_access,
)
from .embedding import EmbeddingError, embedding_signature, get_embedding_provider
from .model import ParsedDocument, ParsedElement, ParseError, Provenance
from .retrieval import KnowledgeRetrievalService, RetrievalOptions, RetrievalResult
from .vector_store import collection_name_for, get_vector_store

__all__ = [
    "KnowledgeAccessDenied", "KnowledgeNotFoundError", "accessible_bases_filter",
    "filter_authorized_ids", "require_base_access",
    "EmbeddingError", "embedding_signature", "get_embedding_provider",
    "ParsedDocument", "ParsedElement", "ParseError", "Provenance",
    "KnowledgeRetrievalService", "RetrievalOptions", "RetrievalResult",
    "collection_name_for", "get_vector_store",
]
