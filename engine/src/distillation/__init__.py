"""小说蒸馏模块（Novel Distillation）——将长篇文本转化为16维结构化知识技能包。

复用现有基础设施：
- LLM 调用经 src.llm.registry.ModelRegistry（多提供商 + fallback）
- 编排经 LangGraph + SQLite checkpoint（断点续跑）
- 技能包以 MD+JSON 文件存储（与项目 MD 唯一事实源一致）
"""
