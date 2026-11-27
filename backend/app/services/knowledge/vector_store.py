"""Qdrant Vector Store Adapter（任务书 §6.2）。

- 生产：``KNOWLEDGE_QDRANT_URL`` 指向独立 Qdrant 服务（持久化 Volume、内部网络）；
- 测试 / 单机开发：``KNOWLEDGE_QDRANT_URL`` 为空时使用 qdrant-client 本地模式
  （仍然是真实 Qdrant 引擎，非 Mock）；
- Collection 按 Embedding 模型 / 维度共享（不是每个文件一个 Collection），
  知识库 / 代际 / 文档由 Payload Filter 隔离，过滤条件只由服务端生成。
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from app.core.config import settings

logger = logging.getLogger(__name__)

_CLIENT: Optional[AsyncQdrantClient] = None


def _client() -> AsyncQdrantClient:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    url = (settings.KNOWLEDGE_QDRANT_URL or "").strip()
    if url == ":memory:":
        _CLIENT = AsyncQdrantClient(location=":memory:")
    elif url:
        _CLIENT = AsyncQdrantClient(url=url, api_key=settings.KNOWLEDGE_QDRANT_API_KEY or None)
    else:
        local_dir = Path(settings.KNOWLEDGE_STORAGE_DIR) / "qdrant_local"
        local_dir.mkdir(parents=True, exist_ok=True)
        _CLIENT = AsyncQdrantClient(path=str(local_dir))
    return _CLIENT


def reset_client_for_tests() -> None:
    global _CLIENT
    _CLIENT = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:48] or "model"


def collection_name_for(model: str, dimension: int) -> str:
    prefix = (settings.KNOWLEDGE_QDRANT_COLLECTION_PREFIX or "gap_knowledge").strip("_")
    return f"{prefix}_{_slug(model)}_{int(dimension)}"


def point_id(chunk_id: str) -> str:
    """稳定 Point ID：Chunk ID 的 32 位十六进制前缀转为 UUID（幂等 Upsert）。"""
    digest = re.sub(r"[^0-9a-fA-F]", "", chunk_id)
    digest = (digest + "0" * 32)[:32]
    return str(uuid.UUID(hex=digest))


def build_filter(
    *,
    knowledge_base_ids: Sequence[int],
    generation_ids: Sequence[int],
    document_id: Optional[int] = None,
    owner_id: Optional[int] = None,
    exclude_parents: bool = False,
) -> qm.Filter:
    must: List[qm.Condition] = []
    if knowledge_base_ids:
        must.append(qm.FieldCondition(
            key="knowledge_base_id", match=qm.MatchAny(any=[int(x) for x in knowledge_base_ids])
        ))
    if generation_ids:
        must.append(qm.FieldCondition(
            key="index_generation_id", match=qm.MatchAny(any=[int(x) for x in generation_ids])
        ))
    if document_id is not None:
        must.append(qm.FieldCondition(key="document_id", match=qm.MatchValue(value=int(document_id))))
    if owner_id is not None:
        must.append(qm.FieldCondition(key="owner_id", match=qm.MatchValue(value=int(owner_id))))
    must_not: List[qm.Condition] = []
    if exclude_parents:
        must_not.append(qm.FieldCondition(key="is_parent", match=qm.MatchValue(value=True)))
    return qm.Filter(must=must, must_not=must_not or None)


class QdrantVectorStore:
    async def health(self) -> bool:
        try:
            await _client().get_collections()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_collection(self, name: str, dimension: int) -> None:
        client = _client()
        try:
            exists = await client.collection_exists(name)
        except Exception:  # noqa: BLE001
            exists = False
        if exists:
            return
        try:
            await client.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=int(dimension), distance=qm.Distance.COSINE),
            )
        except Exception as exc:  # noqa: BLE001
            # 并发创建：另一方已创建视为成功
            try:
                if await client.collection_exists(name):
                    return
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Qdrant Collection 创建失败：{exc}") from exc

    async def upsert(self, collection: str, points: Iterable[Dict[str, Any]]) -> int:
        client = _client()
        structs = [
            qm.PointStruct(
                id=point_id(str(point["chunk_id"])),
                vector=[float(x) for x in point["vector"]],
                payload=dict(point["payload"]),
            )
            for point in points
        ]
        if not structs:
            return 0
        await client.upsert(collection_name=collection, points=structs, wait=True)
        return len(structs)

    async def delete_points(self, collection: str, flt: qm.Filter) -> None:
        client = _client()
        try:
            await client.delete(
                collection_name=collection,
                points_selector=qm.FilterSelector(filter=flt),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            # Collection 尚未创建（文档未完成过一次索引）视为已清理
            if "not found" in message or "doesn't exist" in message or "does not exist" in message:
                return
            raise

    async def search(
        self, collection: str, vector: List[float], *, limit: int, flt: qm.Filter,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        client = _client()
        try:
            response = await client.query_points(
                collection_name=collection,
                query=[float(x) for x in vector],
                limit=max(1, int(limit)),
                query_filter=flt,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Qdrant 检索失败：{exc}") from exc
        results: List[Tuple[str, float, Dict[str, Any]]] = []
        for point in response.points or []:
            payload = dict(point.payload or {})
            chunk_id = str(payload.get("chunk_id") or point.id)
            results.append((chunk_id, float(point.score or 0.0), payload))
        return results

    async def count(self, collection: str, flt: Optional[qm.Filter] = None) -> int:
        client = _client()
        try:
            result = await client.count(
                collection_name=collection, count_filter=flt, exact=True
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Qdrant 计数失败：{exc}") from exc
        return int(result.count or 0)

    async def delete_collection(self, name: str) -> None:
        client = _client()
        try:
            if await client.collection_exists(name):
                await client.delete_collection(name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Qdrant Collection 删除失败：{exc}") from exc


_STORE: Optional[QdrantVectorStore] = None


def get_vector_store() -> QdrantVectorStore:
    global _STORE
    if _STORE is None:
        _STORE = QdrantVectorStore()
    return _STORE
