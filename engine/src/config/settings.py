"""全局配置：.env 环境变量 + configs/models.yaml 模型绑定。

- AppSettings：应用级设置（路径、日志级别），来自 .env / 环境变量。
- ModelsConfig：providers / roles / embedding 三段式模型接入配置，
  加载时解析 ${ENV_VAR} 占位符并做 schema 校验（Fail-Fast）。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


class AppSettings(BaseSettings):
    """应用级设置，从 .env / 环境变量读取。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 路径约定
    project_root: Path = PROJECT_ROOT
    configs_dir: Path = PROJECT_ROOT / "configs"
    novels_dir: Path = PROJECT_ROOT / "data" / "novels"
    runtime_dir: Path = PROJECT_ROOT / "data" / "runtime"  # 派生数据：向量库/checkpoint

    @property
    def models_yaml(self) -> Path:
        return self.configs_dir / "models.yaml"

    @property
    def chroma_dir(self) -> Path:
        return self.runtime_dir / "chroma"

    @property
    def checkpoint_db(self) -> Path:
        return self.runtime_dir / "checkpoints.sqlite"


class ProviderConfig(BaseModel):
    """接入点定义。"""

    type: Literal["openai_compat", "anthropic"]
    base_url: str | None = None
    api_key: str = "none"
    #: 额外请求头（如 OpenCode Go 要求的 `x-opencode-session` 与自定义 User-Agent）。
    #: 由来（2026-09-21 用户实测）：只配 base_url + api_key 时，GET /models 能过、
    #: 但 /chat/completions 会被网关按 400 MissingSessionID 拒掉——「列表拉得到、
    #: 模型用不了」正是因为缺这几个头，而此前没有任何地方能配。
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_base_url(self) -> ProviderConfig:
        if self.type == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat 接入点必须提供 base_url")
        return self


class FallbackBinding(BaseModel):
    """降级接入点绑定：主接入点调用失败时切换到此 provider/model。

    独立携带 provider + model，避免复用主绑定的 model 名（异厂商模型名不同）。
    temperature / max_tokens 省略时沿用主绑定或调用方传入值。
    """

    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None


class RoleBinding(BaseModel):
    """角色 → 接入点/模型/参数 绑定。"""

    provider: str
    model: str
    temperature: float = 0.7
    max_tokens: int | None = None
    fallback: FallbackBinding | None = None  # 降级接入点（主接入点失败时切换）


class EmbeddingConfig(BaseModel):
    """Embedding 配置。"""

    type: Literal["chroma_default", "openai_compat", "local_bge"] = "chroma_default"
    base_url: str | None = None
    api_key: str = "none"
    model: str | None = None


class ModelsConfig(BaseModel):
    """configs/models.yaml 整体结构。"""

    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleBinding]
    embedding: EmbeddingConfig = EmbeddingConfig()

    REQUIRED_ROLES: tuple = ("architect", "writer", "editor")

    @model_validator(mode="after")
    def _validate_bindings(self) -> ModelsConfig:
        for role in self.REQUIRED_ROLES:
            if role not in self.roles:
                raise ValueError(f"models.yaml 缺少必需角色绑定: {role}")
        for role, binding in self.roles.items():
            if binding.provider not in self.providers:
                raise ValueError(
                    f"角色 [{role}] 绑定了未定义的接入点 [{binding.provider}]，"
                    f"可用接入点: {list(self.providers)}"
                )
            if binding.fallback and binding.fallback.provider not in self.providers:
                raise ValueError(
                    f"角色 [{role}] 的 fallback 接入点 "
                    f"[{binding.fallback.provider}] 未在 providers 中定义"
                )
        return self


def _resolve_env_placeholders(raw: object) -> object:
    """递归解析 ${ENV_VAR} 占位符；未设置的环境变量保留原文（探活时报错）。"""
    if isinstance(raw, dict):
        return {k: _resolve_env_placeholders(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [_resolve_env_placeholders(v) for v in raw]
    if isinstance(raw, str):

        def _sub(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))

        return _ENV_VAR_PATTERN.sub(_sub, raw)
    return raw


def load_models_config(path: Path | None = None) -> ModelsConfig:
    """加载并校验 models.yaml，Fail-Fast。"""
    # 每次加载前补读一次 .env：用户在界面上新增接入点并填 Key 时，Key 是写进 .env 的，
    # 而引擎进程只在启动时读过一次——旧行为下必须重启引擎，否则「测试」会报
    # 「占位符对应的环境变量未设置」，而文件里明明已经写好了（2026-09-21 实测）。
    # override=False：只补**新增**的键，绝不覆盖已在环境里的变量。
    _load_dotenv()
    settings = get_settings()
    yaml_path = path or settings.models_yaml
    if not yaml_path.exists():
        raise FileNotFoundError(f"模型配置文件不存在: {yaml_path}")
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    resolved = _resolve_env_placeholders(raw)
    return ModelsConfig.model_validate(resolved)


def _load_dotenv() -> None:
    """把 engine/.env 里的键注入 os.environ（只补缺失的键）。"""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache
def get_settings() -> AppSettings:
    # 将 .env 中的键值注入 os.environ，使 models.yaml 的 ${ENV_VAR} 占位符
    # （如 ${DEEPSEEK_API_KEY}）可被 _resolve_env_placeholders 解析。
    # pydantic-settings 只把 .env 读入已声明字段，不会写回 os.environ，故需显式加载。
    _load_dotenv()
    return AppSettings()
