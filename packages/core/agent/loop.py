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
    files: list[dict[str, Any]] | None = None,
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
                files=files,  # 用户上传文件作为内容块直传(规划阶段也能读真实文件)
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

        # 动作派发(三种写法都兼容,避免"tool"措辞撞 LLM client 的 no-tool 注入):
        #  ① {"action":"inspect","args":{...}}  ② {"inspect":{...}}(handler 名作字段)
        #  允许空参动作(如 inspect:{}) — 用"键是否存在"判断,而非值真假。
        action_name = None
        action_args: dict[str, Any] = {}
        a = decision.get("action")
        if isinstance(a, str) and a in handlers:
            action_name = a
            action_args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        else:
            # 先挑有非空参数的 handler 字段
            for name in handlers:
                if isinstance(decision.get(name), dict) and decision[name]:
                    action_name, action_args = name, decision[name]
                    break
            # 再兜底:存在该 handler 字段(即便空 dict,如 inspect:{})
            if action_name is None:
                for name in handlers:
                    if name in decision and isinstance(decision[name], dict):
                        action_name, action_args = name, decision[name]
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
