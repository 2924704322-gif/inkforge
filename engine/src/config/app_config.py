"""应用分层配置体系（M3 / T3.1）：base/dev/prod YAML 继承 + Deep Merge + 版本号 + Fail-Fast。

加载顺序：
  1) 读取 configs/base.yaml 作为基线。
  2) 由 APP_ENV（dev/prod，默认 dev）选择 configs/<env>.yaml 覆盖层。
  3) Deep Merge：覆盖层字典递归合并进基线（标量/列表覆盖，字典递归）。
  4) 解析 ${ENV_VAR} 占位符（复用 settings 的解析器）。
  5) pydantic schema 校验（Fail-Fast）：字段缺失/类型错误/环境非法立即抛错。

与 AppSettings（.env 路径/密钥）互补：AppConfig 负责环境相关的运行调参与后端选择。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

from src.config.settings import PROJECT_ROOT, _resolve_env_placeholders

VALID_ENVS = ("dev", "prod")
DEFAULT_ENV = "dev"


# ---------- schema ----------

class VectorStoreConfig(BaseModel):
    type: str
    path: Optional[str] = None
    host: Optional[str] = None
    port: Optional[str] = None


class RetrievalConfig(BaseModel):
    hybrid_top_k: int = 5
    rrf_k: int = 60
    time_decay_lambda: float = 0.05
    foreshadow_due_window: int = 2


class GenerationConfig(BaseModel):
    default_target_words: int = 3000
    max_partial_retries: int = 2
    max_full_retries: int = 2
    # 对话协商协作模式（v2.0 P4-A）：partial_rewrite 时 Writer↔Editor 磋稿后定向修订
    negotiation_enabled: bool = False
    # 字数控制门禁
    word_count_tolerance: int = 500          # 章节字数硬容差 ±500 字
    max_continuation_attempts: int = 1       # Writer 续写/压缩追加调用上限
    max_length_retries: int = 1              # 字数门禁打回重写上限
    length_gate_enabled: bool = True         # 字数门禁总开关


class AppConfig(BaseModel):
    version: str
    environment: str
    log_level: str
    vector_store: VectorStoreConfig
    retrieval: RetrievalConfig = RetrievalConfig()
    generation: GenerationConfig = GenerationConfig()
    # Skill 扩展配置（T3.3）：{skill_name: {enabled: bool, ...}}，
    # 具体字段由各 Skill 的 SettingsModel 校验（装配时 Fail-Fast）
    skills: dict = {}


# ---------- Deep Merge ----------

def deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并 overlay 到 base 之上。字典递归合并，其余类型（标量/列表）整体覆盖。

    不修改入参，返回新字典。
    """
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------- 加载 ----------

def _configs_dir() -> Path:
    return PROJECT_ROOT / "configs"


def load_app_config(env: Optional[str] = None, configs_dir: Optional[Path] = None) -> AppConfig:
    """加载分层配置并校验（Fail-Fast）。

    env 为空时取 APP_ENV，仍为空取 DEFAULT_ENV。非法 env 立即抛 ValueError。
    """
    env = (env or os.environ.get("APP_ENV") or DEFAULT_ENV).strip().lower()
    if env not in VALID_ENVS:
        raise ValueError(f"非法环境 APP_ENV={env!r}，可选：{VALID_ENVS}")

    cdir = configs_dir or _configs_dir()
    base_path = cdir / "base.yaml"
    env_path = cdir / f"{env}.yaml"
    if not base_path.exists():
        raise FileNotFoundError(f"基线配置不存在: {base_path}")
    if not env_path.exists():
        raise FileNotFoundError(f"环境配置不存在: {env_path}")

    base_raw = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    env_raw = yaml.safe_load(env_path.read_text(encoding="utf-8")) or {}
    merged = deep_merge(base_raw, env_raw)
    resolved = _resolve_env_placeholders(merged)
    return AppConfig.model_validate(resolved)


@lru_cache
def get_app_config() -> AppConfig:
    return load_app_config()
