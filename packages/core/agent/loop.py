"""通用 agentic 循环 — 手动 tool-use(LLM 输出 JSON 动作 → 执行器跑 → 回灌结果)。

LlmClient 是一次性补全(无原生 tool_use),所以这里用"手动循环":
  每轮把累积的对话 + 上一步执行结果喂给 LLM,LLM 输出下一步 JSON 动作,
  本模块执行对应 handler,把真实结果拼回对话,继续 — 直到 LLM 说 done 或到上限。

handlers: dict[str, async (args:dict) -> str]   工具名 → 执行函数(返回真实结果文本)
返回: {transcript:[...], findings:[...], steps:int}
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


async def agent_loop(
    llm: Any,
    system_prompt: str,
    task: str,
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str]]],
    max_steps: int = 16,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    max_result_chars: int = 2500,
) -> dict[str, Any]:
    transcript: list[dict[str, Any]] = []
    findings: list[Any] = []
    convo = task
    for step in range(max_steps):
        try:
            resp = await llm.complete(
                system=system_prompt,
                messages=[{"role": "user", "content": convo}],
                max_tokens=2000, allow_degrade=False,
            )
        except Exception as exc:
            transcript.append({"step": step, "error": f"LLM 调用失败: {str(exc)[:200]}"})
            break
        try:
            decision = resp.json()
        except Exception:
            # 不是合法 JSON → 当作结束,把文本留作 finding
            findings.append({"note": (resp.text or "")[:500]})
            break
        if not isinstance(decision, dict):
            break

        thought = decision.get("thought") or decision.get("reason") or ""
        for f in (decision.get("findings") or ([decision["finding"]] if decision.get("finding") else [])):
            findings.append(f)

        # 动作派发:不用"tool"措辞(会撞 LLM client 的"禁止 tool_use"注入)。
        # 约定:decision 里出现哪个 handler 名作为字段(值为 dict)就执行哪个。
        action_name = None
        action_args: dict[str, Any] = {}
        for name in handlers:
            v = decision.get(name)
            if isinstance(v, dict) and v:
                action_name, action_args = name, v
                break
        done = bool(decision.get("done")) or (action_name is None)

        rec: dict[str, Any] = {"step": step, "thought": thought, "action": action_name, "args": action_args}
        if done:
            rec["result"] = "(结束)"
            transcript.append(rec)
            if on_step:
                on_step(rec)
            break

        try:
            result = await handlers[action_name](action_args)
        except Exception as exc:
            result = f"[执行错误] {type(exc).__name__}: {str(exc)[:300]}"
        result = (result or "")[:max_result_chars]
        rec["result"] = result
        transcript.append(rec)
        if on_step:
            on_step(rec)

        convo += (
            f"\n\n=== 第 {step} 步 ===\n"
            f"你的判断: {thought}\n"
            f"系统替你执行了 {action_name}: {json.dumps(action_args, ensure_ascii=False)[:400]}\n"
            f"真实结果:\n{result}\n\n"
            f"基于这个真实结果继续(发现问题放进 findings;覆盖够了 done=true)。"
        )
    return {"transcript": transcript, "findings": findings, "steps": len(transcript)}
