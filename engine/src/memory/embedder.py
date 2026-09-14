"""Embedding 抽象：chroma_default / openai_compat / local_bge 三种实现可配置切换。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.config.settings import EmbeddingConfig
from src.utils.logger import get_logger

logger = get_logger(__name__)


class Embedder(ABC):
    """向量化统一接口。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def identity(self) -> str:
        """嵌入模型标识（切换模型后需 rebuild-index，据此判断）。"""


class ChromaDefaultEmbedder(Embedder):
    """ChromaDB 内置 ONNX MiniLM（零依赖，开发调试用；中文效果一般）。"""

    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._fn(texts)]

    @property
    def identity(self) -> str:
        return "chroma-default-minilm"


class OpenAICompatEmbedder(Embedder):
    """任意 OpenAI 兼容 embedding API（如硅基流动 BAAI/bge-m3）。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key or "none")
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        # 按 index 排序保证与输入对齐
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]

    @property
    def identity(self) -> str:
        return f"openai-compat/{self._model}"


class LocalBgeEmbedder(Embedder):
    """本地 BGE-M3（中文优化，需 pip install FlagEmbedding）。"""

    def __init__(self, model: str = "BAAI/bge-m3"):
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as e:
            raise RuntimeError(
                "embedding.type=local_bge 需要安装 FlagEmbedding: "
                "pip install FlagEmbedding"
            ) from e
        self._model_name = model
        self._model = BGEM3FlagModel(model, use_fp16=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = self._model.encode(texts, return_dense=True)
        return [v.tolist() for v in out["dense_vecs"]]

    @property
    def identity(self) -> str:
        return f"local-bge/{self._model_name}"


def build_embedder(cfg: EmbeddingConfig) -> Embedder:
    if cfg.type == "chroma_default":
        logger.info("Embedding: ChromaDB 内置模型（开发档；正式建议切换 BGE-M3）")
        return ChromaDefaultEmbedder()
    if cfg.type == "openai_compat":
        if not (cfg.base_url and cfg.model):
            raise ValueError("embedding.type=openai_compat 需要 base_url 与 model")
        return OpenAICompatEmbedder(cfg.base_url, cfg.api_key, cfg.model)
    if cfg.type == "local_bge":
        return LocalBgeEmbedder(cfg.model or "BAAI/bge-m3")
    raise ValueError(f"未知 embedding 类型: {cfg.type}")
