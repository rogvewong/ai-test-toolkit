"""Claude LLM client — talks to the LOCAL Claude Code via the Python SDK.

The toolkit no longer needs ANTHROPIC_API_KEY and no longer shells out to
`claude -p`. It uses `claude-agent-sdk` and points it at the user's locally
installed Claude Code (NOT the SDK's bundled copy, which has no login state).
Auth is whatever Claude Code is logged into (subscription / token / etc.).

Public interface preserved (orchestrators import these unchanged):
    * LlmClient(...).complete(system=..., messages=[...], tier=...) -> LlmResponse
    * LlmResponse with .text / .model_id / .stop_reason / .usage / .json()
    * Usage / ModelTier / DEGRADATION_ORDER
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import json_repair

from pathlib import Path as _PathFromImg

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    query,
)

from packages.core.telemetry import get_logger

logger = get_logger(__name__)

MessageRole = Literal["user", "assistant"]


class ModelTier(str, Enum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def merge(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cost_usd += other.cost_usd


@dataclass
class LlmResponse:
    text: str
    model_id: str
    stop_reason: str | None
    usage: Usage
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """Extract the first JSON object/array from the response text.

        Strategy: ```json``` fenced block first, then balanced brace match,
        with json_repair as fallback for slightly-malformed LLM output.
        """
        candidates: list[str] = []
        fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", self.text, re.DOTALL)
        if fence:
            candidates.append(fence.group(1))
        brace = re.search(r"(\{.*\}|\[.*\])", self.text, re.DOTALL)
        if brace:
            candidates.append(brace.group(1))
        if not candidates:
            raise ValueError(f"no JSON in response: {self.text[:200]}")

        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_exc = exc
                repaired = json_repair.repair_json(candidate, return_objects=True)
                if repaired != "" and repaired is not None:
                    return repaired
        raise ValueError(
            f"no parseable JSON in response (last_error={last_exc}): {self.text[:200]}"
        )


# Tier → model alias passed via ClaudeAgentOptions(model=...)
_TIER_ALIAS: dict[ModelTier, str] = {
    ModelTier.OPUS: "opus",
    ModelTier.SONNET: "sonnet",
    ModelTier.HAIKU: "haiku",
}

# Per-process.yaml degradation chain. Triggers only on SDK-side failures
# (auth, server overload, etc.).
DEGRADATION_ORDER: list[ModelTier] = [ModelTier.OPUS, ModelTier.SONNET, ModelTier.HAIKU]


def _resolve_local_claude() -> str | None:
    """Locate the user's logged-in Claude Code, NOT the SDK's bundled copy.

    SDK by default uses its own bundled `claude` under
    site-packages/claude_agent_sdk/_bundled/claude — that binary has no
    login state and will fail. We force the SDK to use the user's CLI via
    ClaudeAgentOptions(cli_path=...).

    Resolution order:
      1. $CLAUDE_BIN
      2. $PATH entry that does NOT contain `_bundled` (skip SDK's shim)
      3. Hard-path fallbacks for macOS Finder-launched .app where PATH
         is sanitized down to /usr/bin:/bin:... and `which` returns None
    """
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("claude")
    if found and "_bundled" not in found:
        return found
    home = os.path.expanduser("~")
    fallbacks = [
        f"{home}/.local/bin/claude",
        f"{home}/bin/claude",
        f"{home}/.npm-global/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    for p in fallbacks:
        if os.path.exists(p):
            return p
    return None


_LOCAL_CLAUDE_BIN: str | None = _resolve_local_claude()


class AuthNotConfiguredError(RuntimeError):
    """用户没在设置页选过认证模式，禁止跑工具。

    防止「自动复用本机 ~/.claude/account.json」造成的"全部人手动操作"原则被
    绕过 — 即使本机有 OAuth 凭据，没在设置页主动选过 OAuth 都不能跑。
    """


def _build_auth_env() -> dict[str, str]:
    """根据用户在设置页选的认证模式，返回要传给 CLI 子进程的 env。

    完整认证校验在这里 — API 路由层的 412 闸门只是更早返回友好错误，
    任何绕过路由的入口（直接调用 LlmClient / orchestrator）都靠这里兜底。

    - unset                : 抛 AuthNotConfiguredError
    - oauth + 无 token      : 抛 AuthNotConfiguredError
    - api_key + 无 key      : 抛 AuthNotConfiguredError
    - oauth                : 把 toolkit 自己存的 access_token 注入 CLI
                             （不读 ~/.claude/，凭据完全在 toolkit auth.json）
    - api_key              : 把用户存的 sk-ant-... 注入子进程
    """
    try:
        from packages.core.auth_config import (
            get_api_key, get_auth_mode, get_oauth_access_token,
        )
    except Exception:
        raise AuthNotConfiguredError(
            "认证模块未加载 — 请到「设置 → 模型接入」选择登录方式"
        )
    mode = get_auth_mode()
    if mode == "unset":
        raise AuthNotConfiguredError(
            "尚未登录 — 请到「设置 → 模型接入」选择 OAuth 登录或填 API Key"
        )
    if mode == "api_key":
        key = get_api_key()
        if key:
            return {"ANTHROPIC_API_KEY": key, "ANTHROPIC_AUTH_TOKEN": key}
        raise AuthNotConfiguredError(
            "API Key 模式已选但 key 为空 — 请到「设置」填入 sk-ant-... 或切到 OAuth"
        )
    if mode == "oauth":
        # 用 toolkit 自己存的 OAuth access_token（web OAuth flow 拿到的）
        # 不读 ~/.claude/ 任何东西
        token = get_oauth_access_token()
        if not token:
            raise AuthNotConfiguredError(
                "OAuth 模式但未完成授权 — 请到「设置 → 模型接入」点击 OAuth 授权"
            )
        # ANTHROPIC_API_KEY 清空（Claude CLI 优先用 AUTH_TOKEN）
        return {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": token}
    # 未知 mode 当 unset 处理
    raise AuthNotConfiguredError(f"未知的认证模式：{mode}")


def _flatten_system(system: str | list[dict[str, Any]]) -> str:
    """System input may be a string or a list of cache-controlled blocks.

    SDK takes a single `system_prompt` string; we drop cache-control metadata
    and concatenate text blocks.
    """
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n\n".join(parts)


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse messages array into a single prompt string.

    Orchestrators always pass a single user turn, but multi-turn is handled
    defensively by labeling each turn.
    """
    if len(messages) == 1 and messages[0].get("role") == "user":
        content = messages[0].get("content", "")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    chunks: list[str] = []
    for m in messages:
        role = m.get("role", "user").upper()
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        chunks.append(f"[{role}]\n{content}")
    return "\n\n".join(chunks)


class LlmClient:
    """Async wrapper over `claude-agent-sdk.query()`.

    Construct without arguments — auth comes from the local Claude Code login.
    """

    # Output ceilings retained for callers that inspect them.
    MAX_OUTPUT_CEILINGS: dict[str, int] = {
        "opus": 32000,
        "sonnet": 64000,
        "haiku": 64000,
    }
    MAX_OUTPUT_CEILING = 32000

    def __init__(
        self,
        api_key: str | None = None,  # ignored — kept for interface compat
        base_url: str | None = None,  # ignored
        effort: str | None = None,  # low|medium|high|xhigh|max
        model_override: str | None = None,  # alias OR full SDK model id
        thinking: str | None = None,  # disabled|adaptive|enabled
        betas: list[str] | None = None,  # SDK beta flags, e.g. ["context-1m-2025-08-07"]
    ) -> None:
        self.effort = effort
        self.model_override = model_override
        self.thinking = thinking
        self.betas = betas or []

    @classmethod
    def _ceiling_for(cls, model_id: str) -> int:
        mid = model_id.lower()
        if "opus" in mid:
            return cls.MAX_OUTPUT_CEILINGS["opus"]
        if "haiku" in mid:
            return cls.MAX_OUTPUT_CEILINGS["haiku"]
        if "sonnet" in mid:
            return cls.MAX_OUTPUT_CEILINGS["sonnet"]
        return cls.MAX_OUTPUT_CEILING

    async def complete(
        self,
        *,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tier: ModelTier = ModelTier.SONNET,
        max_tokens: int = 4096,  # noqa: ARG002 — interface compat, unused
        temperature: float = 0.2,  # noqa: ARG002 — interface compat, unused
        cacheable_system: bool = True,  # noqa: ARG002 — SDK auto-caches
        tools: list[dict[str, Any]] | None = None,  # noqa: ARG002 — disabled; pure completion
        tool_choice: dict[str, Any] | None = None,  # noqa: ARG002
        allow_degrade: bool = True,
        stream_callback: Any = None,  # callable(kind: str, accumulated: str)
        images: list[dict[str, Any]] | None = None,  # [{path:Path, mime:"image/png", caption:str}]
    ) -> LlmResponse:
        """Single completion — falls down the degradation chain on failure.

        images: optional list of screenshot attachments. Each dict has
                {path, mime, caption}. When present, the prompt is sent as
                a multimodal content-block message so vision-capable models
                actually SEE the page (used by step5 / h5_adapt).
        """
        # 用户在 UI 显式选了模型(model_override)→ 严格只用这个模型,
        # 不做跨模型降级。"用户选什么就跑什么",失败也只重试同一个模型。
        if self.model_override:
            allow_degrade = False
        tiers = (
            DEGRADATION_ORDER[DEGRADATION_ORDER.index(tier):]
            if allow_degrade
            else [tier]
        )
        # 每个模型重试 3 次,指数退避 — Anthropic 偶发限流(429)/过载(529)/
        # CLI 子进程瞬时错误,立即重试同模型往往就好,不该让整个 run 挂掉。
        _RETRIES_PER_TIER = 3
        _BACKOFF_BASE = 4.0  # 秒:4, 8, 16
        import asyncio as _aio
        last_exc: Exception | None = None
        for current_tier in tiers:
            for attempt in range(_RETRIES_PER_TIER):
                try:
                    return await self._call_once(
                        tier=current_tier, system=system, messages=messages,
                        stream_callback=stream_callback, images=images,
                    )
                except AuthNotConfiguredError:
                    # 认证错误不重试不降级 — 换模型也救不了
                    raise
                except Exception as exc:
                    last_exc = exc
                    is_last_attempt = attempt == _RETRIES_PER_TIER - 1
                    logger.warning(
                        "llm.retry" if not is_last_attempt else "llm.degrade",
                        tier=current_tier.value,
                        attempt=attempt + 1,
                        reason=type(exc).__name__,
                        detail=str(exc)[:200],
                    )
                    if not is_last_attempt:
                        # 退避后重试同一个模型
                        await _aio.sleep(_BACKOFF_BASE * (2 ** attempt))
                        continue
                    # 本模型 3 次都挂 → 跳到降级链下一个模型
                    break
        raise RuntimeError(f"all models in degradation chain failed: {last_exc}")

    async def _call_once(
        self,
        *,
        tier: ModelTier,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        stream_callback: Any = None,
        images: list[dict[str, Any]] | None = None,
    ) -> LlmResponse:
        # User-side model override beats the per-substep tier.
        model_alias = self.model_override or _TIER_ALIAS[tier]
        system_text = _flatten_system(system)
        user_text = _flatten_messages(messages)

        # Capture stderr from the spawned claude process so when the SDK
        # surfaces "Command failed exit 1" we have the real reason.
        stderr_lines: list[str] = []
        # Force the model to skip tools — even with tools=[] the SDK injects
        # its bundled system context, and Opus tends to attempt tool calls
        # which terminate the turn with stop_reason="tool_use" and no text.
        if system_text:
            has_images = bool(images)
            image_filenames = [
                _PathFromImg(im.get("path")).name if im.get("path") else None
                for im in (images or [])
            ]
            image_filenames = [f for f in image_filenames if f]
            link_rule = (
                "2. 用户输入中出现的任何外部链接（包括 https / Notion / Google Drive / Figma / "
                "Confluence / GitHub / Jira / Lark / 飞书 / 钉钉 / 企业微信 / 在线文档 / 图床等），"
                "【只能视作字符串引用】，绝不尝试访问、绝不假装访问，也不要说『我将访问』或『我会去查』。\n"
                if not has_images else
                "2. 本次请求已附带页面真实截图（image content blocks）作为视觉证据 — "
                "你必须基于截图内容进行像素级分析（布局、配色、文案、按钮位置、组件状态），"
                "并在结论中明确指出每个差异点的视觉表现与所在视口。"
                "对于未截图的链接（无 image block 的），只视作字符串引用，不假装访问。\n"
            )
            text_only_rule = (
                "3. 即使输入看起来不完整、信息不足、链接无法点开——你也只能基于【已粘贴的字面文本】"
                "进行分析，不能拒绝任务、不能反问、不能要求补充材料。\n"
                if not has_images else
                "3. 必须基于附带的截图 + 文本输入进行分析，不能拒绝任务、不能反问、不能要求补充材料；"
                "如确实无法从截图判断某项，请在 issue 里明示『需要 hover/click 才能验证的态』并标记 confidence。\n"
            )
            system_text = (
                system_text.rstrip()
                + "\n\n[严格运行规则 — 必须遵守，否则结果作废]\n"
                + "1. 你目前没有任何可调用的工具（tools 列表为空）。禁止任何工具调用、function call、"
                  "网页抓取、文件读取、URL 访问。\n"
                + link_rule
                + text_only_rule
                + "4. 【响应必须以 { 或 [ 开头，是合法 JSON】。\n"
                + "   严禁任何前导说明（『我将…』、『首先…』、『接下来…』、『好的，』、『明白，』 等）。\n"
                + "   严禁任何后置注释（除非 JSON 内部）。\n"
                + "   严禁 markdown 代码块标记 ```json 或 ``` —— 直接输出裸 JSON。\n"
                + "5. 不要产生 tool_use block。所有内容都必须是文本块（text block）。\n"
                + "6. 【全部使用中文输出】：所有 issue / risk / description / recommendation / "
                + "expected / scenario 等字段值都必须是中文自然语言；只有以下技术枚举可以保留英文："
                + "P0/P1/P2/P3、severity 枚举（critical/high/medium/low/info）、"
                + "kind/action/status 等枚举值、URL、HTTP 方法、API 路径、技术框架名（如 React、Vue）。"
                + "禁止整段英文描述，禁止英文解释技术问题（必须用中文）。\n"
                + "7. 【问题字段标准 — 必须完整以便开发直接领走修复】\n"
                + "   每个 issue / problem / finding 对象必须包含以下字段（缺一不可）：\n"
                + "   - issue_id: 形如 'PAY-CRIT-001'（领域代号 + 严重度 + 三位序号）\n"
                + "   - title: 一句话问题陈述（≤30 字）\n"
                + "   - severity: critical/high/medium/low/info\n"
                + "   - module: 具体代码位置 — 如 'backend/payment_service.py:callback_handler' 或 "
                + "'POST /api/orders' 或 '前端 OrderConfirmPage 组件'，绝不能只写 '支付模块'\n"
                + "   - current_behavior: 当前/PRD 现状（一句话；引用源文档关键句更佳）\n"
                + "   - expected_behavior: 期望应做到什么（一句话，可量化）\n"
                + "   - fix_suggestion: 【必须具体可执行】写清楚怎么改 — "
                + "'在 X 函数加 Redis SETNX 锁，key 是 lock:pay:{txid}，过期 60s' 这样；"
                + "不能写 '建议加幂等'、'考虑增加容错' 这种空话\n"
                + "   - reproduce_steps: 复现步骤数组，编号列表，每步 ≤ 25 字\n"
                + "   - acceptance_criteria: 验收标准 — 这条改完了如何客观验证（必须可观察/可测）\n"
                + "   - related_test_cases: 关联用例 ID 列表（若已知）\n"
                + "   - owner_role: backend / frontend / product / test / devops / security / "
                + "data 之一（小写英文枚举）\n"
                + "   - estimated_hours: 修复估时（数字，含单测）\n"
                + "   - impact_scope: 影响面 — 哪些其他模块或业务流程受牵连（一句话）\n"
                + "   - evidence: 证据来源（PRD 第 X 节 / 截图视口名 / 接口契约段落）"
                + (
                    "\n\n[截图标注规则 — 仅当本次有附图时生效]\n"
                    "对每个发现的 UI 问题，issue 对象必须额外包含两个字段以便服务端在截图上画框：\n"
                    "- viewport_filename: 该问题所在截图的文件名（必须是下面列出的之一），\n"
                    "- bbox: [x, y, w, h] 像素坐标，相对该截图原始尺寸的整数（x,y 是左上角；w,h 是宽高）；\n"
                    "可附加字段 severity: critical/high/medium/low/cosmetic（默认 medium）。\n"
                    f"本次可用的截图文件名：\n  - " + "\n  - ".join(image_filenames)
                    + "\n\n示例 issue 对象：\n"
                    + '{"id":"ui-1","title":"主 CTA 按钮在 iPhone SE 被键盘遮挡","viewport_filename":"' + (image_filenames[0] if image_filenames else 'xxx.png') + '","bbox":[24,640,328,52],"severity":"high","expected":"键盘弹出后 CTA 应保持可见"}'
                    if has_images and image_filenames else ""
                )
            )
        opt_kwargs: dict[str, Any] = dict(
            system_prompt=system_text or None,
            model=model_alias,
            tools=[],  # pure completion — no agent tools
            permission_mode="bypassPermissions",
            # Load 'user' setting source so the local Claude Code finds its
            # subscription/OAuth credentials. With None, subprocess gets no
            # auth context and falls back to env-var auth.
            setting_sources=["user"],
            max_turns=1,
            stderr=lambda line: stderr_lines.append(line),
            # CRITICAL: point at the user's logged-in Claude Code, not the
            # SDK's bundled copy (which has no auth context).
            cli_path=_LOCAL_CLAUDE_BIN,
            # 认证 env 由 auth_config 决定：oauth 强清 / api_key 注入
            env=_build_auth_env(),
        )
        if self.effort in ("low", "medium", "high", "xhigh", "max"):
            opt_kwargs["effort"] = self.effort
        if self.thinking == "adaptive":
            opt_kwargs["thinking"] = {"type": "adaptive"}
        elif self.thinking == "disabled":
            opt_kwargs["thinking"] = {"type": "disabled"}
        if self.betas:
            # SDK accepts a literal list of supported beta flags
            opt_kwargs["betas"] = self.betas
        # Enable streaming partial messages so we can emit text/thinking deltas
        # as they arrive (powers the live "AI 思考过程" log view).
        if stream_callback is not None:
            opt_kwargs["include_partial_messages"] = True
        options = ClaudeAgentOptions(**opt_kwargs)

        text_parts: list[str] = []
        result_msg: ResultMessage | None = None
        last_assistant: AssistantMessage | None = None
        # Streaming buffers — accumulate text/thinking deltas across StreamEvents.
        text_buf = ""
        thinking_buf = ""

        # Build prompt: simple string for text-only, AsyncIterable[dict] when images present
        prompt_arg: Any
        if images:
            import base64 as _b64
            content_blocks: list[dict[str, Any]] = []
            for img in images:
                try:
                    p = img.get("path")
                    mime = img.get("mime") or "image/png"
                    caption = img.get("caption") or ""
                    if p is None:
                        continue
                    raw = open(p, "rb").read()
                    if len(raw) > 5 * 1024 * 1024:  # 5MB per image cap
                        continue
                    b64 = _b64.b64encode(raw).decode("ascii")
                    if caption:
                        content_blocks.append({"type": "text", "text": f"[截图标识] {caption}"})
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    })
                except Exception:
                    continue
            content_blocks.append({"type": "text", "text": user_text})

            async def _msg_stream():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": content_blocks},
                    "parent_tool_use_id": None,
                    "session_id": "",
                }
            prompt_arg = _msg_stream()
        else:
            prompt_arg = user_text

        try:
            async for msg in query(prompt=prompt_arg, options=options):
                if isinstance(msg, AssistantMessage):
                    last_assistant = msg
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(msg, StreamEvent):
                    # Anthropic streaming events. We care about content_block_delta with
                    # text_delta or thinking_delta. Each delta is appended to the running
                    # buffer and the callback is invoked with the accumulated text.
                    if stream_callback is None:
                        continue
                    ev = getattr(msg, "event", None) or {}
                    if ev.get("type") != "content_block_delta":
                        continue
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        chunk = delta.get("text") or ""
                        if chunk:
                            text_buf += chunk
                            try:
                                stream_callback("text", text_buf)
                            except Exception:
                                pass
                    elif dtype == "thinking_delta":
                        chunk = delta.get("thinking") or ""
                        if chunk:
                            thinking_buf += chunk
                            try:
                                stream_callback("thinking", thinking_buf)
                            except Exception:
                                pass
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
        except ClaudeSDKError as exc:
            tail = "\n".join(stderr_lines[-30:]) if stderr_lines else "(no stderr)"
            raise RuntimeError(
                f"claude SDK error (model={model_alias}): {exc}\nstderr:\n{tail}"
            ) from exc
        except Exception as exc:
            tail = "\n".join(stderr_lines[-30:]) if stderr_lines else "(no stderr)"
            raise RuntimeError(
                f"claude SDK invocation failed (model={model_alias}): {type(exc).__name__}: {exc}\nstderr:\n{tail}"
            ) from exc

        if result_msg is None:
            raise RuntimeError(f"claude SDK returned no ResultMessage (model={model_alias})")
        if result_msg.is_error:
            errors = result_msg.errors or []
            raise RuntimeError(
                f"claude SDK reported error: {result_msg.subtype} :: {'; '.join(errors)[:300]}"
            )

        # Prefer collected AssistantMessage text; fall back to ResultMessage.result
        text = "\n".join(text_parts) if text_parts else (result_msg.result or "")
        stop_reason = result_msg.stop_reason or (
            last_assistant.stop_reason if last_assistant else None
        )
        # If the model tried a tool call and produced no text, the downstream
        # JSON parser will fail with "no JSON in response". Surface a more
        # specific error so the degradation chain can try a smaller model.
        if not text.strip() and stop_reason == "tool_use":
            raise RuntimeError(
                f"model={model_alias} stopped on tool_use with no text — "
                "tool injection by SDK; retrying with degraded tier"
            )

        u = result_msg.usage or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
            cost_usd=float(result_msg.total_cost_usd or 0.0),
        )

        # Pick the concrete model the SDK routed to.
        if last_assistant and last_assistant.model:
            model_id = last_assistant.model
        elif result_msg.model_usage:
            model_id = next(iter(result_msg.model_usage.keys()))
        else:
            model_id = model_alias

        logger.info(
            "llm.call",
            model=model_id,
            stop_reason=stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            cost_usd=round(usage.cost_usd, 4),
        )

        return LlmResponse(
            text=text,
            model_id=model_id,
            stop_reason=stop_reason,
            usage=usage,
            raw={
                "subtype": result_msg.subtype,
                "session_id": result_msg.session_id,
                "duration_ms": result_msg.duration_ms,
                "duration_api_ms": result_msg.duration_api_ms,
                "num_turns": result_msg.num_turns,
                "usage": u,
                "model_usage": result_msg.model_usage,
            },
        )
