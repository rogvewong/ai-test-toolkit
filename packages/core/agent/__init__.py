"""通用 agentic 执行引擎 — LLM 决策 → 真实执行器 → 观察 → 循环。

8 个工具统一复用:各工具只配「执行提示词 + 可用工具(执行器)」,
AI 看真实结果自己决定下一步,而不是 Python 写死流程。
"""
from packages.core.agent.loop import agent_loop

__all__ = ["agent_loop"]
