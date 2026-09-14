"""pytest 全局夹具：目录隔离 + 应用工厂 + Fake 向量索引。

设计要点：
- **零真实 LLM 调用**：本套件只覆盖纯函数、数据层、API 契约与安全边界；
  需要模型的路径用 FakeIndex / 直接构造请求体绕过。
- **目录隔离**：通过环境变量 `NOVELS_DIR` / `RUNTIME_DIR` / `PROJECT_ROOT` 覆盖
  AppSettings 字段并清空 `get_settings` 的 lru_cache，使测试不触碰真实作品数据。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把引擎的数据根目录重定向到临时目录。"""
    novels = tmp_path / "novels"
    runtime = tmp_path / "runtime"
    configs = tmp_path / "configs"
    novels.mkdir()
    runtime.mkdir()
    # configs 必须在临时目录内：模型配置端点会写 models.yaml，
    # 不能污染真实 engine/configs/models.yaml
    configs.mkdir()
    engine_dir = Path(__file__).resolve().parents[1]
    for name in ("base.yaml", "dev.yaml", "prod.yaml", "models.yaml"):
        src = engine_dir / "configs" / name
        if src.exists():
            (configs / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("NOVELS_DIR", str(novels))
    monkeypatch.setenv("RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CONFIGS_DIR", str(configs))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    # 测试内默认关闭「每本书 Git 仓库」以提速；需要验证 Git 的用例自行开启
    monkeypatch.delenv("INKFORGE_GIT_HISTORY", raising=False)
    monkeypatch.delenv("INKFORGE_ENGINE_TOKEN", raising=False)
    monkeypatch.delenv("INKFORGE_UPLOAD_ROOTS", raising=False)

    from src.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    yield SimpleNamespace(
        root=tmp_path, novels=novels, runtime=runtime, configs=configs, engine_dir=engine_dir
    )
    settings_mod.get_settings.cache_clear()


@pytest.fixture()
def app_client(sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    """返回 (TestClient, create_app 工厂)，默认书为 demo-web。"""
    from fastapi.testclient import TestClient

    from src.web import server as server_mod

    _seed_book(sandbox.novels / "demo-web", title="样例书", chapters=2)

    app = server_mod.create_app("demo-web")
    with TestClient(app) as client:
        yield client, server_mod


def _seed_book(novel_dir: Path, title: str = "测试书", chapters: int = 1) -> Path:
    """在指定目录造一本最小可用的书（大纲 + 若干章节）。"""
    novel_dir.mkdir(parents=True, exist_ok=True)
    (novel_dir / "settings").mkdir(exist_ok=True)
    (novel_dir / "chapters" / "vol-01").mkdir(parents=True, exist_ok=True)
    (novel_dir / "summaries").mkdir(exist_ok=True)
    (novel_dir / "reviews").mkdir(exist_ok=True)

    volumes = [
        {
            "volume": 1,
            "title": "第一卷",
            "depends_on": [],
            "chapters": [
                {"chapter": i, "title": f"第{i}章", "outline": f"第{i}章的事件", "characters": ["林墨"]}
                for i in range(1, chapters + 1)
            ],
        }
    ]
    import frontmatter

    (novel_dir / "settings" / "outline.md").write_text(
        frontmatter.dumps(frontmatter.Post("# 大纲", title=title, theme="测试主题", volumes=volumes)),
        encoding="utf-8",
        newline="\n",
    )
    for i in range(1, chapters + 1):
        (novel_dir / "chapters" / "vol-01" / f"ch-{i:03d}.md").write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    f"第{i}章正文内容。" * 20,
                    chapter=i,
                    volume=1,
                    title=f"第{i}章",
                    status="approved",
                    characters=["林墨"],
                    score=8.5,
                    first_review_passed=True,
                )
            ),
            encoding="utf-8",
            newline="\n",
        )
    return novel_dir


@pytest.fixture()
def seed_book():
    return _seed_book


class FakeIndex:
    """VectorIndex 的最小替身：记录 upsert/clear，query 返回空。"""

    def __init__(self) -> None:
        self.upserted: list[tuple[str, str]] = []
        self.cleared = 0

    def upsert_document(self, doc, kind: str) -> int:
        self.upserted.append((doc.doc_id, kind))
        return 1

    def delete_document(self, rel_path: str) -> None:
        self.upserted = [u for u in self.upserted if u[0] != rel_path]

    def clear(self) -> None:
        self.cleared += 1
        self.upserted.clear()

    def query(self, *args, **kwargs):
        return []

    def get_corpus(self, *args, **kwargs):
        return []

    def count(self) -> int:
        return len(self.upserted)


@pytest.fixture()
def fake_index() -> FakeIndex:
    return FakeIndex()


@pytest.fixture()
def make_memory(sandbox: SimpleNamespace, fake_index: FakeIndex):
    """构造 (MdStore, MemoryManager)，使用 FakeIndex 避免真实向量库依赖。"""
    from src.memory.md_store import MdStore
    from src.memory.memory_manager import MemoryManager

    def _make(novel_id: str = "memory-book"):
        store = MdStore(sandbox.novels / novel_id, auto_git=False)
        return store, MemoryManager(store, fake_index)

    return _make


# 兼容：确保 os 被引用（部分用例通过 env 直接断言）
_ = os
