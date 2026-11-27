"""Embedding / Reranker Provider（任务书 §6.1 / §7）。

- ``local``：fastembed（ONNX，CPU）真实本地语义向量，默认 BGE-small-zh；
- ``openai``：任意 OpenAI-compatible /embeddings 服务（批量、超时、维度校验）；
- 模型名 / 维度随 Generation 落库，索引代际可追踪；
- Reranker 使用 fastembed CrossEncoder（默认关闭，便于性能对比）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import List, Optional, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 失败（``retryable=True`` 时 Worker 按退避重试）。"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    async def embed(self, texts: List[str]) -> List[List[float]]:
        ...


class LocalFastEmbedProvider:
    name = "local"

    def __init__(self) -> None:
        self.model = settings.KNOWLEDGE_EMBEDDING_MODEL
        self.dimension = int(settings.KNOWLEDGE_EMBEDDING_DIMENSION or 0)
        self._engine = None
        self._lock = threading.Lock()

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                from fastembed import TextEmbedding
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(
                    "本地 Embedding 需要 fastembed（pip install fastembed），当前不可用",
                    retryable=False,
                ) from exc
            kwargs = {"model_name": self.model}
            cache_dir = (settings.KNOWLEDGE_EMBEDDING_CACHE_DIR or "").strip()
            if cache_dir:
                kwargs["cache_dir"] = cache_dir
            try:
                self._engine = TextEmbedding(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"加载本地 Embedding 模型失败：{exc}") from exc
            logger.info("本地 Embedding 模型已加载：%s", self.model)
        return self._engine

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        engine = await asyncio.to_thread(self._get_engine)
        batch_size = max(1, int(settings.KNOWLEDGE_EMBEDDING_BATCH_SIZE or 16))
        vectors: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            try:
                result = await asyncio.to_thread(lambda b: list(engine.embed(b)), batch)
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"本地 Embedding 推理失败：{exc}") from exc
            vectors.extend([[float(x) for x in vector] for vector in result])
        self._verify_dimension(vectors)
        return vectors

    def _verify_dimension(self, vectors: List[List[float]]) -> None:
        if not vectors:
            return
        actual = len(vectors[0])
        if self.dimension and actual != self.dimension:
            raise EmbeddingError(
                f"Embedding 维度不匹配：配置 {self.dimension}，模型 {self.model} 实际 {actual}；"
                "请修正 KNOWLEDGE_EMBEDDING_DIMENSION 或更换模型后重建索引",
                retryable=False,
            )
        self.dimension = actual


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(self) -> None:
        self.model = settings.KNOWLEDGE_EMBEDDING_MODEL
        self.dimension = int(settings.KNOWLEDGE_EMBEDDING_DIMENSION or 0)
        self.base_url = (settings.KNOWLEDGE_EMBEDDING_BASE_URL or "").rstrip("/")
        self.api_key = settings.KNOWLEDGE_EMBEDDING_API_KEY

    async def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if not self.base_url:
            raise EmbeddingError(
                "KNOWLEDGE_EMBEDDING_PROVIDER=openai 但未配置 KNOWLEDGE_EMBEDDING_BASE_URL",
                retryable=False,
            )
        batch_size = max(1, int(settings.KNOWLEDGE_EMBEDDING_BATCH_SIZE or 16))
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        vectors: List[List[float]] = []
        async with httpx.AsyncClient(
            timeout=float(settings.KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS or 60),
            headers=headers,
        ) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                try:
                    response = await client.post(
                        f"{self.base_url}/embeddings",
                        json={"model": self.model, "input": batch},
                    )
                except httpx.HTTPError as exc:
                    raise EmbeddingError(f"Embedding 服务请求失败：{exc}") from exc
                if response.status_code >= 400:
                    retryable = response.status_code >= 500 or response.status_code == 429
                    raise EmbeddingError(
                        f"Embedding 服务返回 HTTP {response.status_code}：{response.text[:300]}",
                        retryable=retryable,
                    )
                try:
                    payload = response.json()
                    data = sorted(payload.get("data") or [], key=lambda item: item.get("index", 0))
                    vectors.extend([[float(x) for x in item["embedding"]] for item in data])
                except (KeyError, TypeError, ValueError) as exc:
                    raise EmbeddingError(f"Embedding 响应格式非法：{exc}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"Embedding 返回数量不匹配：期望 {len(texts)}，实际 {len(vectors)}"
            )
        actual = len(vectors[0]) if vectors else 0
        if self.dimension and actual != self.dimension:
            raise EmbeddingError(
                f"Embedding 维度不匹配：配置 {self.dimension}，服务实际 {actual}",
                retryable=False,
            )
        self.dimension = actual
        return vectors


_PROVIDER: Optional[EmbeddingProvider] = None


def reset_provider_for_tests() -> None:
    """测试隔离：清空 Provider / Reranker 单例（切换模型或配置后重新加载）。"""
    global _PROVIDER, _RERANKER
    _PROVIDER = None
    _RERANKER = None


def get_embedding_provider() -> EmbeddingProvider:
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    provider = (settings.KNOWLEDGE_EMBEDDING_PROVIDER or "local").lower()
    _PROVIDER = OpenAIEmbeddingProvider() if provider == "openai" else LocalFastEmbedProvider()
    return _PROVIDER


def embedding_signature() -> dict:
    provider = get_embedding_provider()
    return {
        "model": provider.model,
        "dimension": provider.dimension,
        "provider": provider.name,
    }


# ── Reranker ──────────────────────────────────────────────────────────────

class CrossEncoderReranker:
    """fastembed CrossEncoder（本地 ONNX）；不可用时明确报错，不伪造分数。"""

    def __init__(self) -> None:
        self.model = settings.KNOWLEDGE_RERANKER_MODEL
        self._engine = None
        self._lock = threading.Lock()

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(
                    "Reranker 需要 fastembed（TextCrossEncoder），当前不可用", retryable=False
                ) from exc
            try:
                self._engine = TextCrossEncoder(model_name=self.model)
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"加载 Reranker 模型失败：{exc}") from exc
        return self._engine

    async def rerank(self, query: str, documents: List[str]) -> List[float]:
        if not documents:
            return []
        engine = await asyncio.to_thread(self._get_engine)
        try:
            scores = await asyncio.to_thread(
                lambda: list(engine.rerank(query, documents))
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"Reranker 推理失败：{exc}") from exc
        return [float(score) for score in scores]


_RERANKER: Optional[CrossEncoderReranker] = None


def get_reranker() -> CrossEncoderReranker:
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = CrossEncoderReranker()
    return _RERANKER


async def embed_query(text: str) -> List[float]:
    vectors = await get_embedding_provider().embed([text])
    return vectors[0] if vectors else []
