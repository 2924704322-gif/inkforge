"""ModelRegistry：角色 → Provider 映射、启动探活、按角色发起调用。"""

from __future__ import annotations

from src.config.settings import ModelsConfig, ProviderConfig
from src.llm.anthropic_provider import AnthropicProvider
from src.llm.base import ChatMessage, ChatResult, ModelProvider, ProviderError
from src.llm.openai_compat import OpenAICompatProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _build_provider(name: str, cfg: ProviderConfig) -> ModelProvider:
    if cfg.type == "openai_compat":
        return OpenAICompatProvider(name, base_url=cfg.base_url, api_key=cfg.api_key)
    if cfg.type == "anthropic":
        return AnthropicProvider(name, api_key=cfg.api_key, base_url=cfg.base_url)
    raise ValueError(f"未知接入点类型: {cfg.type}")


class ModelRegistry:
    """按 models.yaml 实例化所有接入点，提供 chat_as(role, ...) 统一入口。"""

    def __init__(self, config: ModelsConfig):
        self._config = config
        self._providers: dict[str, ModelProvider] = {}
        # 未解析的 ${ENV_VAR} 占位符 Fail-Fast（对应环境变量未设置）
        for name, pcfg in config.providers.items():
            if pcfg.api_key.startswith("${"):
                raise ProviderError(
                    name,
                    f"api_key 占位符 {pcfg.api_key} 对应的环境变量未设置，"
                    f"请在 .env 中填写",
                )
            self._providers[name] = _build_provider(name, pcfg)

    @property
    def roles(self) -> list[str]:
        return list(self._config.roles)

    def provider_for(self, role: str) -> ModelProvider:
        binding = self._binding(role)
        return self._providers[binding.provider]

    def probe_all(self) -> dict[str, str]:
        """探活所有角色绑定的 provider+model 组合，全部通过才返回；失败 Fail-Fast。

        返回 {角色: "provider/model"} 映射用于展示。
        """
        result: dict[str, str] = {}
        probed: set[tuple[str, str]] = set()
        for role, binding in self._config.roles.items():
            key = (binding.provider, binding.model)
            if key not in probed:
                logger.info(
                    "探活: 角色[%s] → %s / %s", role, binding.provider, binding.model
                )
                try:
                    self._providers[binding.provider].probe(binding.model)
                except ProviderError as e:
                    raise ProviderError(
                        binding.provider,
                        f"角色[{role}]绑定的接入点探活失败 → {e}",
                    ) from e
                probed.add(key)
            result[role] = f"{binding.provider}/{binding.model}"
            # 降级接入点探活：仅告警不阻断（fallback 是韧性兜底，不应因备用点不通而拒绝启动）
            fb = binding.fallback
            if fb:
                fb_key = (fb.provider, fb.model)
                if fb_key not in probed:
                    try:
                        self._providers[fb.provider].probe(fb.model)
                        logger.info(
                            "探活: 角色[%s] 降级点 → %s / %s",
                            role,
                            fb.provider,
                            fb.model,
                        )
                        probed.add(fb_key)
                    except ProviderError as e:
                        logger.warning(
                            "角色[%s]降级接入点[%s/%s]探活失败（不阻断启动）: %s",
                            role,
                            fb.provider,
                            fb.model,
                            e,
                        )
                result[f"{role} (fallback)"] = f"{fb.provider}/{fb.model}"
        return result

    def chat_as(
        self,
        role: str,
        messages: list[ChatMessage],
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """以指定角色的绑定配置发起调用；主接入点失败时尝试 fallback。"""
        binding = self._binding(role)
        kwargs = dict(
            model=binding.model,
            temperature=temperature if temperature is not None else binding.temperature,
            max_tokens=max_tokens or binding.max_tokens,
            json_mode=json_mode,
        )
        try:
            return self._providers[binding.provider].chat(messages, **kwargs)
        except ProviderError as e:
            fb = binding.fallback
            if not fb:
                raise
            logger.warning(
                "角色[%s]主接入点[%s]调用失败，降级到[%s/%s]: %s",
                role,
                binding.provider,
                fb.provider,
                fb.model,
                e,
            )
            fb_kwargs = dict(
                model=fb.model,
                temperature=(
                    fb.temperature
                    if fb.temperature is not None
                    else kwargs["temperature"]
                ),
                max_tokens=fb.max_tokens or kwargs["max_tokens"],
                json_mode=json_mode,
            )
            try:
                res = self._providers[fb.provider].chat(messages, **fb_kwargs)
                res.used_fallback = True  # 标记本次产出来自降级接入点（可追溯）
                return res
            except ProviderError as e2:
                raise ProviderError(
                    fb.provider,
                    f"角色[{role}]主接入点[{binding.provider}]与降级接入点"
                    f"[{fb.provider}]均失败；主: {e}；降级: {e2}",
                ) from e2

    def json_mode_available(self, role: str) -> bool:
        return self.provider_for(role).supports_json_mode()

    def _binding(self, role: str):
        if role not in self._config.roles:
            raise KeyError(f"未在 models.yaml 中绑定的角色: {role}")
        return self._config.roles[role]
