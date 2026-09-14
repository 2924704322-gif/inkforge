"""混合检索（M2 / T2.3）：向量召回 + BM25 稀疏召回 → RRF 融合 → 时间衰减重排。

- BM25 使用 jieba 中文分词，语料从 VectorIndex 全量导出（MD 唯一事实源的派生物）。
- RRF（Reciprocal Rank Fusion）：score = Σ 1/(k + rank)，无需归一化两路分数。
- 时间衰减（仅对含章节号的块）：越靠近当前章权重越高，
  decay = 1 / (1 + λ * chapter_gap)，与 RRF 分数相乘。
"""

from __future__ import annotations

from typing import Optional

import jieba
from rank_bm25 import BM25Okapi

from src.memory.retriever import RetrievedChunk, VectorIndex
from src.utils.logger import get_logger

logger = get_logger(__name__)

# RRF 平滑常数与时间衰减系数由分层配置驱动（T3.1），加载失败时回退到标准默认值
try:
    from src.config.app_config import get_app_config

    _RC = get_app_config().retrieval
    RRF_K = _RC.rrf_k                    # RRF 平滑常数（信息检索标准值）
    TIME_DECAY_LAMBDA = _RC.time_decay_lambda  # 时间衰减系数：相隔 20 章权重约打 5 折
except Exception:  # noqa: BLE001 - 配置缺失不应阻断检索
    RRF_K = 60
    TIME_DECAY_LAMBDA = 0.05



def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut_for_search(text) if t.strip()]


class HybridRetriever:
    """向量 + BM25 融合检索器。BM25 库按 (kind, max_chapter) 惰性构建并缓存。"""

    def __init__(self, index: VectorIndex):
        self._index = index
        self._bm25_cache: dict[tuple, tuple[BM25Okapi, list[RetrievedChunk]]] = {}

    def invalidate(self) -> None:
        """语料变更（回写/重嵌入）后调用，丢弃 BM25 缓存。"""
        self._bm25_cache.clear()

    def _bm25_for(
        self, kind: Optional[str], max_chapter: Optional[int]
    ) -> Optional[tuple[BM25Okapi, list[RetrievedChunk]]]:
        key = (kind, max_chapter)
        if key not in self._bm25_cache:
            corpus = self._index.get_corpus(kind=kind, max_chapter=max_chapter)
            if not corpus:
                return None
            bm25 = BM25Okapi([_tokenize(c.text) for c in corpus])
            self._bm25_cache[key] = (bm25, corpus)
        return self._bm25_cache[key]

    def query(
        self,
        text: str,
        top_k: int = 5,
        kind: Optional[str] = None,
        max_chapter: Optional[int] = None,
        current_chapter: Optional[int] = None,
    ) -> list[RetrievedChunk]:
        """混合检索。current_chapter 非空时启用时间衰减重排。"""
        # 1) 向量召回（超采样）
        dense = self._index.query(
            text, top_k=top_k * 2, kind=kind, max_chapter=max_chapter
        )

        # 2) BM25 稀疏召回
        sparse: list[RetrievedChunk] = []
        pack = self._bm25_for(kind, max_chapter)
        if pack:
            bm25, corpus = pack
            scores = bm25.get_scores(_tokenize(text))
            ranked = sorted(
                range(len(corpus)), key=lambda i: scores[i], reverse=True
            )[: top_k * 2]
            sparse = [corpus[i] for i in ranked if scores[i] > 0]

        # 3) RRF 融合（以块文本为身份键去重）
        fused: dict[str, dict] = {}
        for rank, hit in enumerate(dense):
            entry = fused.setdefault(hit.text, {"hit": hit, "score": 0.0})
            entry["score"] += 1.0 / (RRF_K + rank + 1)
        for rank, hit in enumerate(sparse):
            entry = fused.setdefault(hit.text, {"hit": hit, "score": 0.0})
            entry["score"] += 1.0 / (RRF_K + rank + 1)

        # 4) 时间衰减重排
        if current_chapter is not None:
            for entry in fused.values():
                ch = entry["hit"].metadata.get("chapter", 0) or 0
                if ch > 0:
                    gap = max(0, current_chapter - ch)
                    entry["score"] *= 1.0 / (1.0 + TIME_DECAY_LAMBDA * gap)

        ordered = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
        results = []
        for entry in ordered[:top_k]:
            hit = entry["hit"]
            results.append(
                RetrievedChunk(
                    text=hit.text,
                    metadata=hit.metadata,
                    distance=-entry["score"],  # 分数越高距离越小（沿用字段语义）
                )
            )
        return results
