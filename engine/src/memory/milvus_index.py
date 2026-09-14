"""Milvus 向量索引适配层（M3 / T3.2）：与 ChromaDB 的 VectorIndex 同接口。

设计原则：
- 公共接口与 `retriever.VectorIndex` 完全一致（upsert_document / delete_document /
  clear / query / get_corpus / count），故 MemoryManager 等上层零改动即可切换后端。
- `pymilvus` 惰性导入：未安装时构造抛出清晰 RuntimeError（对齐 embedder 的降级风格），
  不影响 dev 档（ChromaDB）导入与运行。
- 采用 MilvusClient 高层 API + dynamic field 承载标量元数据（kind/chapter/
  characters/title/rel_path），过滤表达式与 Chroma 的 where 条件语义对齐。
- 余弦度量：Milvus COSINE 返回相似度（越大越近），统一折算 distance = 1 - score，
  与 Chroma 的 cosine distance（越小越近）语义一致，保证上层排序不变。
"""

from __future__ import annotations

import hashlib
from typing import Optional

from src.memory.embedder import Embedder
from src.memory.md_store import MdDocument
from src.memory.retriever import RetrievedChunk, chunk_markdown
from src.utils.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_FIELDS = ["text", "rel_path", "kind", "chapter", "characters", "title"]


def _quote(val: str) -> str:
    """转义字符串用于 Milvus filter 表达式。"""
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'


class MilvusIndex:
    """单部小说的 Milvus 向量索引（PROD 档，与 ChromaDB 同接口）。"""

    def __init__(
        self,
        host: str,
        port: str,
        novel_id: str,
        embedder: Embedder,
        uri: Optional[str] = None,
    ):
        try:
            from pymilvus import MilvusClient
        except ImportError as e:  # 惰性依赖：未安装时清晰报错，不拖累 dev 档
            raise RuntimeError(
                "vector_store.type=milvus 需要安装 pymilvus: pip install pymilvus"
            ) from e

        self._embedder = embedder
        self._novel_id = novel_id
        self._uri = uri or f"http://{host}:{port}"
        # Milvus 集合名规则：字母/数字/下划线，故用 hash 前缀而非原始 id
        self._collection = "novel_" + hashlib.sha1(novel_id.encode("utf-8")).hexdigest()[:16]
        self._client = MilvusClient(uri=self._uri)
        self._dim = len(self._embedder.embed(["维度探测"])[0])
        self._ensure_collection()

    # ---------- 集合管理 ----------

    def _ensure_collection(self) -> None:
        if self._client.has_collection(self._collection):
            self._client.load_collection(self._collection)
            return
        self._client.create_collection(
            collection_name=self._collection,
            dimension=self._dim,
            metric_type="COSINE",
            auto_id=False,
            id_type="string",
            max_length=512,
            primary_field_name="id",
            vector_field_name="vector",
            enable_dynamic_field=True,  # 标量元数据以动态字段存储，支持 filter
            # 强一致：insert/delete 后立即可见，与 Chroma 的同步语义对齐；
            # 默认 Bounded 会使 upsert 后的 count/query 读到旧快照（P2 端到端实测）
            consistency_level="Strong",
        )
        logger.info("Milvus 集合已创建: %s (dim=%d)", self._collection, self._dim)

    def clear(self) -> None:
        """rebuild-index 前清空：删除并重建集合。"""
        if self._client.has_collection(self._collection):
            self._client.drop_collection(self._collection)
        self._ensure_collection()

    # ---------- 写入 ----------

    def upsert_document(self, doc: MdDocument, kind: str) -> int:
        rel_path = doc.doc_id
        self.delete_document(rel_path)
        chunks = chunk_markdown(doc.content)
        if not chunks:
            return 0
        embeddings = self._embedder.embed(chunks)
        rows = [
            {
                "id": f"{rel_path}#{i}",
                "vector": embeddings[i],
                "text": chunks[i],
                "rel_path": rel_path,
                "kind": kind,
                "chapter": int(doc.metadata.get("chapter", 0) or 0),
                "characters": "|".join(doc.metadata.get("characters", []) or []),
                "title": str(doc.metadata.get("title", "")),
            }
            for i in range(len(chunks))
        ]
        self._client.insert(collection_name=self._collection, data=rows)
        return len(chunks)

    def delete_document(self, rel_path: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            filter=f"rel_path == {_quote(rel_path)}",
        )

    # ---------- 过滤表达式 ----------

    @staticmethod
    def _build_filter(kind: Optional[str], max_chapter: Optional[int]) -> str:
        conds: list[str] = []
        if kind:
            conds.append(f"kind == {_quote(kind)}")
        if max_chapter is not None:
            conds.append(f"chapter <= {int(max_chapter)}")
        return " and ".join(conds)

    # ---------- 检索 ----------

    def query(
        self,
        text: str,
        top_k: int = 5,
        kind: Optional[str] = None,
        character: Optional[str] = None,
        max_chapter: Optional[int] = None,
    ) -> list[RetrievedChunk]:
        expr = self._build_filter(kind, max_chapter)
        res = self._client.search(
            collection_name=self._collection,
            data=self._embedder.embed([text]),
            limit=max(top_k * 3, top_k),  # 超采样，角色过滤后再截断
            filter=expr,
            output_fields=_OUTPUT_FIELDS,
        )
        hits: list[RetrievedChunk] = []
        for hit in (res[0] if res else []):
            entity = hit.get("entity", {})
            if character and entity.get("characters"):
                if character not in entity["characters"].split("|"):
                    continue
            # COSINE 相似度 → 距离（与 Chroma cosine distance 语义对齐）
            distance = 1.0 - float(hit.get("distance", 0.0))
            hits.append(RetrievedChunk(text=entity.get("text", ""),
                                       metadata=dict(entity), distance=distance))
            if len(hits) >= top_k:
                break
        return hits

    def get_corpus(
        self, kind: Optional[str] = None, max_chapter: Optional[int] = None
    ) -> list[RetrievedChunk]:
        """导出（过滤后）全部块，供 BM25 稀疏检索建库（对齐 Chroma get_corpus）。"""
        expr = self._build_filter(kind, max_chapter)
        res = self._client.query(
            collection_name=self._collection,
            filter=expr or "",
            output_fields=_OUTPUT_FIELDS,
            limit=16384,
        )
        return [
            RetrievedChunk(text=row.get("text", ""), metadata=dict(row), distance=0.0)
            for row in res
        ]

    def count(self) -> int:
        res = self._client.query(
            collection_name=self._collection, filter="", output_fields=["count(*)"]
        )
        if res and isinstance(res[0], dict):
            return int(res[0].get("count(*)", 0))
        return 0
