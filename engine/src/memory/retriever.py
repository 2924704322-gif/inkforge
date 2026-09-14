"""向量索引与检索：ChromaDB 持久化 + MD 分块 + 元数据过滤（角色标签 M1）。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import chromadb

from src.memory.embedder import Embedder
from src.memory.md_store import MdDocument
from src.utils.logger import get_logger

logger = get_logger(__name__)

MAX_CHUNK_CHARS = 600


def chunk_markdown(content: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """按标题 → 段落两级切分，合并至不超过 max_chars 的块。"""
    if not content.strip():
        return []
    # 先按 heading 分节
    sections = re.split(r"(?m)^(?=#{1,4}\s)", content)
    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append(section)
            continue
        # 段落级合并
        buf = ""
        for para in re.split(r"\n{2,}", section):
            para = para.strip()
            if not para:
                continue
            if buf and len(buf) + len(para) + 2 > max_chars:
                chunks.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
            # 超长单段硬切
            while len(buf) > max_chars:
                chunks.append(buf[:max_chars])
                buf = buf[max_chars:]
        if buf:
            chunks.append(buf)
    return chunks


@dataclass
class RetrievedChunk:
    text: str
    metadata: dict
    distance: float


class VectorIndex:
    """单部小说的向量索引（MD 的派生物，可随时重建）。"""

    def __init__(self, chroma_dir: Path, novel_id: str, embedder: Embedder):
        self._embedder = embedder
        self._client = chromadb.PersistentClient(path=str(chroma_dir))
        name = "novel-" + hashlib.sha1(novel_id.encode("utf-8")).hexdigest()[:12]
        self._collection = self._client.get_or_create_collection(
            name=name,
            metadata={"novel_id": novel_id, "embedder": embedder.identity},
            configuration={"hnsw": {"space": "cosine"}},
        )
        stored = (self._collection.metadata or {}).get("embedder")
        if stored and stored != embedder.identity:
            logger.warning(
                "索引由 [%s] 构建，当前 embedder 为 [%s]，请执行 rebuild-index",
                stored,
                embedder.identity,
            )

    def upsert_document(self, doc: MdDocument, kind: str) -> int:
        """（重新）嵌入一份 MD 文档，返回块数。"""
        rel_path = doc.doc_id
        self.delete_document(rel_path)
        chunks = chunk_markdown(doc.content)
        if not chunks:
            return 0
        meta_base = {
            "rel_path": rel_path,
            "kind": kind,
            "chapter": int(doc.metadata.get("chapter", 0) or 0),
            # Chroma 元数据仅支持标量：角色列表转为定界字符串
            "characters": "|".join(doc.metadata.get("characters", []) or []),
            "title": str(doc.metadata.get("title", "")),
        }
        self._collection.upsert(
            ids=[f"{rel_path}#{i}" for i in range(len(chunks))],
            documents=chunks,
            embeddings=self._embedder.embed(chunks),
            metadatas=[meta_base] * len(chunks),
        )
        return len(chunks)

    def delete_document(self, rel_path: str) -> None:
        self._collection.delete(where={"rel_path": rel_path})

    def clear(self) -> None:
        """rebuild-index 前清空。"""
        name = self._collection.name
        meta = self._collection.metadata
        self._client.delete_collection(name)
        self._collection = self._client.get_or_create_collection(
            name=name,
            metadata={**(meta or {}), "embedder": self._embedder.identity},
            configuration={"hnsw": {"space": "cosine"}},
        )

    def query(
        self,
        text: str,
        top_k: int = 5,
        kind: str | None = None,
        character: str | None = None,
        max_chapter: int | None = None,
    ) -> list[RetrievedChunk]:
        """向量检索。kind/character/max_chapter 为元数据过滤条件。

        max_chapter：只检索该章之前的内容，防止泄露未来剧情。
        """
        conditions: list[dict] = []
        if kind:
            conditions.append({"kind": kind})
        if max_chapter is not None:
            conditions.append({"chapter": {"$lte": max_chapter}})
        where: dict | None = None
        if len(conditions) == 1:
            where = conditions[0]
        elif conditions:
            where = {"$and": conditions}

        kwargs: dict = {
            "query_embeddings": self._embedder.embed([text]),
            "n_results": max(top_k * 3, top_k),  # 超采样，角色过滤后再截断
        }
        if where:
            kwargs["where"] = where
        res = self._collection.query(**kwargs)

        hits: list[RetrievedChunk] = []
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            # 角色标签过滤（M1）：含该角色标签的块优先保留
            if character and meta.get("characters"):
                if character not in meta["characters"].split("|"):
                    continue
            hits.append(RetrievedChunk(text=doc, metadata=dict(meta), distance=dist))
            if len(hits) >= top_k:
                break
        return hits

    def get_corpus(
        self, kind: str | None = None, max_chapter: int | None = None
    ) -> list[RetrievedChunk]:
        """导出（过滤后的）全部块，供 BM25 等稀疏检索建库（M2 混合检索）。"""
        conditions: list[dict] = []
        if kind:
            conditions.append({"kind": kind})
        if max_chapter is not None:
            conditions.append({"chapter": {"$lte": max_chapter}})
        where: dict | None = None
        if len(conditions) == 1:
            where = conditions[0]
        elif conditions:
            where = {"$and": conditions}
        kwargs: dict = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        res = self._collection.get(**kwargs)
        return [
            RetrievedChunk(text=doc, metadata=dict(meta), distance=0.0)
            for doc, meta in zip(res["documents"], res["metadatas"])
        ]

    def count(self) -> int:
        return self._collection.count()
