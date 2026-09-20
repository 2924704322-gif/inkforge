"""运行时装配：配置 → Registry/Store/Memory/Agents → 编译图。"""

from __future__ import annotations

from src.agents.architect import Architect
from src.agents.editor import Editor
from src.agents.prompt_loader import validate_templates
from src.agents.summarizer import Summarizer
from src.agents.writer import Writer
from src.config.app_config import get_app_config
from src.config.settings import get_settings, load_models_config
from src.llm.registry import ModelRegistry
from src.memory.embedder import build_embedder
from src.memory.memory_manager import MemoryManager
from src.memory.store_factory import open_store
from src.memory.vector_factory import build_vector_index
from src.orchestrator.graph import Pipeline, build_graph
from src.skills import image_gen  # noqa: F401  内置 Skill 导入即注册（@register）
from src.skills.base import SkillContext
from src.skills.memory_bus import MemoryBus
from src.skills.registry import SkillRegistry
from src.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


def build_pipeline(novel_id: str, target_words: int | None = None) -> Pipeline:
    """按 novel_id 装配全套运行时依赖。"""
    # 启动期模板自检（W1）：变量—参数不一致在启动即炸，不留到生成中途
    validate_templates()
    settings = get_settings()
    setup_logging(settings.log_level, settings.runtime_dir / "logs" / f"{novel_id}.log")

    config = load_models_config()
    registry = ModelRegistry(config)

    # 生成流水线是事实源写入路径 → 显式启用每本书独立 Git 仓库（写作历史）
    store = open_store(settings.novels_dir / novel_id, writable=True)
    embedder = build_embedder(config.embedding)
    app_config = get_app_config()
    index = build_vector_index(app_config.vector_store, novel_id, embedder)
    memory = MemoryManager(store, index)

    gen = app_config.generation
    words = target_words if target_words is not None else gen.default_target_words

    # Skill 扩展装配（T3.3）：总线 + 已启用 Skill 实例（订阅事件自动挂接）
    bus = MemoryBus()
    skills = SkillRegistry(
        SkillContext(novel_id=novel_id, store=store, memory=memory, registry=registry),
        bus=bus,
    ).build_enabled(app_config.skills)

    return Pipeline(
        registry=registry,
        store=store,
        memory=memory,
        architect=Architect(registry, store),
        writer=Writer(
            registry,
            target_words=words,
            # 非对称字数口径（下浮硬线 / 上浮放宽）：tolerance=最低可接受，ceiling=最高可接受
            tolerance=gen.length_floor(words),
            ceiling=gen.length_ceiling(words),
            max_continuation_attempts=gen.max_continuation_attempts,
        ),
        editor=Editor(registry, store, tolerance=gen.word_count_tolerance),
        summarizer=Summarizer(registry),
        bus=bus,
        skills=skills,
        gen_config=gen,
    )


def build_app(novel_id: str, target_words: int | None = None):
    """返回 (pipeline, compiled_graph)。"""
    settings = get_settings()
    pipe = build_pipeline(novel_id, target_words)
    graph = build_graph(pipe, settings.checkpoint_db)
    return pipe, graph
