"""向量库后端工厂（M3 / T3.2）：按 AppConfig.vector_store.type 选择 ChromaDB 或 Milvus。

dev 档 → ChromaDB（本地持久化）；prod 档 → Milvus（host/port 从环境变量注入）。
两种后端接口完全一致，故上层（MemoryManager 等）零改动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from src.config.app_config import VectorStoreConfig
from src.config.settings import PROJECT_ROOT
from src.memory.embedder import Embedder
from src.memory.milvus_index import MilvusIndex
from src.memory.retriever import VectorIndex
from src.utils.logger import get_logger

logger = get_logger(__name__)

VectorBackend = Union[VectorIndex, MilvusIndex]


def build_vector_index(
    vs: VectorStoreConfig, novel_id: str, embedder: Embedder
) -> VectorBackend:
    """按配置构造向量后端；类型非法时 Fail-Fast。"""
    vtype = (vs.type or "chroma").strip().lower()
    if vtype == "chroma":
        path = Path(vs.path) if vs.path else PROJECT_ROOT / "data" / "runtime" / "chroma"
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        logger.info("向量后端: ChromaDB @ %s", path)
        return VectorIndex(path, novel_id, embedder)
    if vtype == "milvus":
        if not (vs.host and vs.port):
            raise ValueError("vector_store.type=milvus 需要 host 与 port")
        logger.info("向量后端: Milvus @ %s:%s", vs.host, vs.port)
        return MilvusIndex(vs.host, vs.port, novel_id, embedder)
    raise ValueError(f"未知 vector_store.type: {vs.type!r}（可选 chroma / milvus）")
