"""立绘生成 Skill（M3 / T3.4）：读取角色外貌 → 生图 API → 立绘 URL 回写角色 MD。

作为 Skill 扩展框架（T3.3）的示范实现：
- 外貌来源：角色 frontmatter 的 appearance/外貌 字段；缺失则回退解析正文「## 外貌」小节。
- 生图：OpenAI 兼容 images API（DALL·E / SD-WebUI / 硅基流动等）；默认 dry_run
  产出占位 URL，无需真实 API 即可演示与测试。
- 回写：把 URL 写入角色 MD 的 portrait frontmatter 字段。
- 事件：可订阅 character_updated，在配置 auto_on_update 时自动补生成。
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from src.skills.base import Skill, SkillContext, SkillSettings
from src.skills.memory_bus import EVENT_CHARACTER_UPDATED
from src.skills.registry import register
from src.utils.logger import get_logger

logger = get_logger(__name__)

_APPEARANCE_KEYS = ("appearance", "外貌", "portrait_prompt")
_SECTION_RE = re.compile(r"(?m)^#{1,4}\s*(?:外貌|外观|appearance)\s*\n(.+?)(?=\n#{1,4}\s|\Z)", re.S)


class ImageGenSettings(SkillSettings):
    enabled: bool = False           # 立绘为可选扩展，默认关闭
    base_url: str | None = None
    api_key: str = "none"
    model: str = "dall-e-3"
    size: str = "1024x1024"
    style_suffix: str = "，动漫风格人物立绘，半身，白色背景，高清"
    dry_run: bool = True            # 默认不真调用，产占位 URL
    auto_on_update: bool = False    # 角色更新事件是否自动补图


@register
class ImageGenSkill(Skill):
    name: ClassVar[str] = "image_gen"
    SettingsModel: ClassVar[type[SkillSettings]] = ImageGenSettings

    def __init__(self, settings: ImageGenSettings, context: SkillContext):
        super().__init__(settings, context)
        self._client = None  # 惰性初始化

    # ---------- 外貌抽取 ----------

    @staticmethod
    def _extract_appearance(meta: dict, content: str) -> str:
        for key in _APPEARANCE_KEYS:
            val = meta.get(key)
            if val and str(val).strip():
                return str(val).strip()
        m = _SECTION_RE.search(content or "")
        if m:
            return m.group(1).strip()
        return ""

    # ---------- 生图 ----------

    def _generate_image(self, prompt: str) -> str:
        if self.settings.dry_run or not self.settings.base_url:
            # 占位 URL：演示/测试路径，不产生 API 成本
            import hashlib
            digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
            return f"placeholder://portrait/{digest}.png"
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.settings.base_url,
                                  api_key=self.settings.api_key or "none")
        resp = self._client.images.generate(
            model=self.settings.model, prompt=prompt, size=self.settings.size, n=1
        )
        return resp.data[0].url

    # ---------- 主逻辑 ----------

    def _char_rel(self, name: str) -> str:
        return f"settings/characters/{name}.md"

    def _gen_for(self, name: str) -> str | None:
        rel = self._char_rel(name)
        store = self.context.store
        if not store.exists(rel):
            logger.warning("立绘：角色档案不存在 %s", rel)
            return None
        doc = store.read(rel)
        appearance = self._extract_appearance(doc.metadata, doc.content)
        if not appearance:
            logger.warning("立绘：角色 %s 无外貌描述，跳过", name)
            return None
        prompt = appearance + self.settings.style_suffix
        url = self._generate_image(prompt)
        store.update_metadata(rel, {"portrait": url},
                              commit_message=f"立绘生成: {name}")
        logger.info("立绘：%s → %s", name, url)
        return url

    def run(self, character: str | None = None,
            characters: list[str] | None = None, **kwargs: Any) -> dict:
        """为指定角色（或全部角色）生成立绘并回写 URL。"""
        names: list[str]
        if character:
            names = [character]
        elif characters:
            names = list(characters)
        else:  # 全量：扫描 settings/characters 下所有角色
            names = [
                doc.metadata.get("title") or doc.metadata["_rel_path"].split("/")[-1][:-3]
                for doc in self.context.store.iter_documents("settings/characters")
            ]
        results = {name: self._gen_for(name) for name in names}
        done = {k: v for k, v in results.items() if v}
        return {"generated": done, "count": len(done), "requested": len(names)}

    # ---------- 记忆总线钩子 ----------

    def subscribed_events(self) -> list[str]:
        return [EVENT_CHARACTER_UPDATED] if self.settings.auto_on_update else []

    def handle_event(self, event: str, payload: dict) -> None:
        name = payload.get("name")
        if name:
            self._gen_for(name)
