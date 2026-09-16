"""服务层：跨调用方复用的能力实现（HTTP 端点 / 墨师动作层共用）。

本包只放"纯业务与数据"实现，不 import fastapi、不持有会话状态；
端点与动作执行器都是它的薄包装，保证行为不漂移。
"""

from __future__ import annotations
