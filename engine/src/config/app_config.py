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

import yaml
from pydantic import BaseModel

from src.config.settings import PROJECT_ROOT, _resolve_env_placeholders

VALID_ENVS = ("dev", "prod")
DEFAULT_ENV = "dev"


# ---------- schema ----------

class VectorStoreConfig(BaseModel):
    type: str
    path: str | None = None
    host: str | None = None
    port: str | None = None


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

    # ---------- 字数控制门禁（非对称口径 · 用户要求）----------
    # 用户要求（原话）："我要求至少要有 5000 字的描写，实际输出会远小于这个数字；
    # 向下浮动最多只能有 500 字，向上浮动在保证内容完整的情况下可以浮动 2000 字"。
    #
    # 旧口径是**对称容差** `max(500, target*0.15)`：5000 字的章既容许少 750 字、
    # 也容许只写到 4250 字，而用户要的是"**至少** 5000"；上浮侧又被同一把尺子
    # 卡成 5750，逼着 Writer 去压缩已经完整的内容。两者方向相反，必须拆成两条边：
    #   可接受区间 = [target - floor_offset, target + ceiling_offset]
    #   下边（字数不足）是硬线：低于它一律判不合格；
    #   上边（字数超出）放宽：内容完整性优先，只要没超到"灌水"就不打回。
    word_count_floor_offset: int = 500       # 下浮上限（目标 - 500 = 最低可接受字数）
    word_count_ceiling_offset: int = 2000    # 上浮上限（目标 + 2000 = 最高可接受字数）
    # 兼容旧配置键（历史 yaml/测试仍可能带它）：仅作为下浮上限的兜底来源，
    # 不再参与上浮侧计算——上浮一律走 word_count_ceiling_offset。
    word_count_tolerance: int = 500
    # 下浮侧的**比例兜底**：默认关闭（0.0）。作者的要求是"向下浮动最多 500 字"，
    # 是按绝对值给的硬线；若再按 15% 放大，5000 字目标就会放宽到 4250（下浮 750），
    # 与要求不符。这个键保留给"短章需要按比例放宽"的场景，需要时再调。
    word_count_tolerance_ratio: float = 0.0

    # 单次模型调用的产出上限折算成中文正文只有约 3000-4000 字，一次生成必然够不到
    # 5000 字目标；因此续写轮数是"能否达标"的一等参数，不是可选调优。默认 3 轮
    # （每次只要求补差额，每轮约 2000-3000 字，3 轮足以覆盖 5000-7000 字的章）。
    max_continuation_attempts: int = 3       # Writer 扩写/压缩追加调用上限
    max_length_retries: int = 2              # 字数门禁打回重写上限
    length_gate_enabled: bool = True         # 字数门禁总开关

    def length_floor(self, target_words: int) -> int:
        """最低可接受字数（目标 - 下浮上限，按比例兜底）。"""
        try:
            target = int(target_words)
        except (TypeError, ValueError):
            return 0
        try:
            scaled = round(target * float(self.word_count_tolerance_ratio))
        except (TypeError, ValueError):
            scaled = 0
        offset = max(int(self.word_count_floor_offset), scaled, int(self.word_count_tolerance))
        return max(target - offset, 1)

    def length_ceiling(self, target_words: int) -> int:
        """最高可接受字数（目标 + 上浮上限）。"""
        try:
            target = int(target_words)
        except (TypeError, ValueError):
            return 0
        return target + max(int(self.word_count_ceiling_offset), 0)

    def length_bounds(self, target_words: int) -> tuple[int, int]:
        """(最低可接受, 最高可接受)：写作、评分、门禁、前端展示共用同一对边界。"""
        return self.length_floor(target_words), self.length_ceiling(target_words)

    def tolerance_for(self, target_words: int) -> int:
        """给定本章目标字数，算出允许偏差（绝对值与比例取较大者）。

        保留本方法是为了兼容既有调用点与历史配置语义（对称容差）；**字数门禁与
        Writer 的重写指令已改用 length_floor/length_ceiling 的非对称口径**，
        新代码不要再用它描述"允许误差 ±N"。
        """
        try:
            scaled = round(int(target_words) * float(self.word_count_tolerance_ratio))
        except (TypeError, ValueError):
            scaled = 0
        return max(int(self.word_count_tolerance), scaled)


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


def load_app_config(env: str | None = None, configs_dir: Path | None = None) -> AppConfig:
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
