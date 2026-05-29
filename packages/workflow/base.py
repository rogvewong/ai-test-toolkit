"""Shared scaffolding for SOP step orchestrators.

Each step orchestrator:
  1. Loads its prompt set.
  2. Runs substeps in order, feeding outputs forward.
  3. Persists intermediate + final artifacts to layered memory.
  4. Validates final report against a Pydantic schema.
  5. Computes confidence + gate decision per configs/standards.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.core.config import settings
from packages.core.llm import LlmClient, LlmResponse, Usage
from packages.core.memory import LayeredMemory
from packages.core.models.common import ReportMeta, fingerprint
from packages.core.prompts import PromptStep, PromptTemplate, load_step
from packages.core.telemetry import bind_run_context, get_logger

logger = get_logger(__name__)

# ── Evidence 归档策略 ──
# off          : 完全不写 system_rendered / response_raw 到磁盘（仅 parsed.json + meta.json）
# failure_only : 仅在解析失败 / 工具失败时写完整 prompt+response，平时只写 parsed
# full (默认)  : 始终写所有内容（向后兼容旧行为）
# 控制：环境变量 EVIDENCE_ARCHIVE_MODE
EVIDENCE_ARCHIVE_MODE = os.environ.get("EVIDENCE_ARCHIVE_MODE", "full").lower()
if EVIDENCE_ARCHIVE_MODE not in ("off", "failure_only", "full"):
    EVIDENCE_ARCHIVE_MODE = "full"

# 脱敏正则：匹配 sk-ant-..., Bearer token, 长串 token，cookie 值等
# 替换成 [REDACTED:...] 保留可读性
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'sk-ant-[A-Za-z0-9_\-]{30,}'),                 '[REDACTED:anthropic-key]'),
    (re.compile(r'sk-[A-Za-z0-9_\-]{30,}'),                     '[REDACTED:api-key]'),
    (re.compile(r'(?i)Bearer\s+[A-Za-z0-9._\-]{20,}'),          'Bearer [REDACTED:token]'),
    (re.compile(r'(?i)Authorization:\s*[A-Za-z0-9._\-\s]{20,}'),'Authorization: [REDACTED]'),
    (re.compile(r'(?i)(api[_\-]?key|access[_\-]?token|password|secret)\s*[:=]\s*["\']?[A-Za-z0-9._\-]{8,}["\']?'),
                                                                 r'\1: [REDACTED]'),
    (re.compile(r'(?i)cookie:\s*[^\s\n]{20,}'),                 'cookie: [REDACTED]'),
]


def _redact_secrets(text: str) -> str:
    """对常见敏感字段做脱敏 — 保留可读上下文但不泄露实际值。"""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


@dataclass
class StepContext:
    run_id: str
    project_id: str
    tenant_id: str
    inputs: dict[str, Any]
    memory: LayeredMemory
    llm: LlmClient
    evidence_dir: Path
    usage: Usage = field(default_factory=Usage)
    # Optional stream log callback. Set by the API layer so the front-end
    # can render real-time progress. orchestrators / LlmClient call
    # ctx.emit("event", **fields) at key checkpoints.
    log_callback: Any = None

    def bind_logs(self, step_id: str) -> None:
        bind_run_context(
            run_id=self.run_id,
            project_id=self.project_id,
            tenant_id=self.tenant_id,
            step_id=step_id,
        )

    def emit(self, event: str, **fields: Any) -> None:
        cb = self.log_callback
        if not cb:
            return
        try:
            cb(event, fields)
        except Exception:
            pass


class StepOrchestrator(ABC):
    step_dir: str
    step_id: str

    def __init__(self, ctx: StepContext) -> None:
        self.ctx = ctx
        self.prompts: PromptStep = load_step(self.step_dir)

    async def run_substep(
        self,
        sub_id: str,
        placeholder_values: dict[str, Any],
        *,
        user_instruction: str | None = None,
    ) -> tuple[LlmResponse | None, Any]:
        """Render + call + parse for a single substep. Returns (raw_response, parsed_json).

        If `__enabled_substeps` is present in ctx.inputs and `sub_id` is NOT
        in the list, the call is skipped and (None, None) is returned. The
        caller's downstream prompts will then render the missing input as
        "(未提供)" via the prompt template fallback. Concrete orchestrators
        that synthesize a strict Pydantic report at the end may need to
        tolerate Nones for skipped substeps; the generic orchestrator already
        does.
        """
        enabled = self.ctx.inputs.get("__enabled_substeps")
        if enabled is not None and sub_id not in enabled:
            logger.info("substep.skipped", sub_id=sub_id, reason="user_disabled")
            self.ctx.emit("substep.skipped", sub_id=sub_id, reason="user_disabled")
            return None, None
        tpl: PromptTemplate = self.prompts.get(sub_id)
        self.ctx.emit(
            "substep.start",
            sub_id=sub_id,
            name=tpl.name,
            model_tier=tpl.model_tier.value,
            max_tokens=tpl.max_tokens,
        )
        system_body = tpl.render(placeholder_values)
        system_full = self.prompts.compose_system(
            PromptTemplate(**{**tpl.__dict__, "body": system_body})
        )
        user_msg = user_instruction or "请严格按 output 格式输出合法 JSON。"
        messages = [{"role": "user", "content": user_msg}]

        # Rate-limited stream forwarder so we don't spam state["logs"] with
        # one entry per token. Emit at most every 400 chars OR every 0.5s.
        import time as _time_mod
        stream_state = {
            "text": {"chars": 0, "ts": 0.0},
            "thinking": {"chars": 0, "ts": 0.0},
        }

        def _stream_cb(kind: str, accumulated: str) -> None:
            st = stream_state.get(kind)
            if not st:
                return
            now = _time_mod.time()
            chars = len(accumulated)
            if chars - st["chars"] < 400 and (now - st["ts"]) < 0.5:
                return
            st["chars"] = chars
            st["ts"] = now
            tail = accumulated[-160:].replace("\n", " ⏎ ")
            self.ctx.emit(
                f"llm.{kind}",
                sub_id=sub_id,
                chars=chars,
                tail=tail,
            )

        # If page screenshots were captured for this run (step5/h5_adapt),
        # attach them as image content so the model can SEE the page.
        images = None
        shots = getattr(self.ctx, "screenshots", None) or []
        if shots:
            from pathlib import Path as _Path
            ev_dir = _Path(self.ctx.evidence_dir)
            # Resolve filenames against the screenshots subdir of the user data root
            # (api/main.py saves to <evidence_root>/screenshots/).
            # Walk up from evidence_dir to find the shared screenshots/ dir.
            cand = ev_dir.parent / "screenshots"
            if not cand.exists():
                cand = ev_dir / "screenshots"
            images = []
            for s in shots:
                if s.get("error") or not s.get("filename"):
                    continue
                p = cand / s["filename"]
                if p.exists():
                    # caption 标清角色:设计稿(Figma 基线) vs 实拍(APP/Web 实际界面)。
                    # step5 据此做「实拍 vs 设计稿」对照,只在实拍图上标偏差。
                    _u = s.get("url", "") or ""
                    if s.get("is_design") or "figma.com" in _u.lower():
                        _role = "设计稿(Figma 设计基线 — 目标设计,勿在此图上标问题)"
                    elif _u.startswith("app://"):
                        _role = "实拍(APP 真机实际界面 — 在此图对照设计稿标偏差)"
                    else:
                        _role = "实拍(Web 实际页面 — 在此图对照设计稿标偏差)"
                    images.append({
                        "path": p,
                        "mime": "image/png",
                        "caption": (
                            f"role={_role} | viewport_filename={s['filename']} | "
                            f"viewport={s.get('viewport','?')} ({s.get('width','?')}×{s.get('height','?')}) | "
                            f"url={_u}"
                        ),
                    })
            if not images:
                images = None

        resp = await self.ctx.llm.complete(
            system=system_full,
            messages=messages,
            tier=tpl.model_tier,
            max_tokens=tpl.max_tokens,
            temperature=tpl.temperature,
            stream_callback=_stream_cb,
            images=images,
        )
        self.ctx.usage.merge(resp.usage)
        # Final flush so the very last chunk shows up
        for kind, st in stream_state.items():
            if st["chars"] > 0:
                self.ctx.emit(
                    f"llm.{kind}.final",
                    sub_id=sub_id,
                    total_chars=st["chars"],
                )
        self.ctx.emit(
            "llm.call",
            sub_id=sub_id,
            model=resp.model_id,
            stop_reason=resp.stop_reason,
            output_tokens=resp.usage.output_tokens,
            cache_read=resp.usage.cache_read_tokens,
            cost_usd=round(resp.usage.cost_usd, 4),
        )

        # Archive the raw response FIRST so that even if JSON parse fails
        # (malformed output or hard truncation) we still have the evidence
        # on disk to diagnose the failure.
        await self._archive_substep(sub_id, tpl, system_full, resp, parsed=None)

        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"{sub_id} hit max_tokens ({tpl.max_tokens}) — response truncated "
                f"even after auto-escalation. Raw response archived under evidence/."
            )
        try:
            parsed = resp.json() if tpl.output_format == "json" else resp.text
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "substep.parse_failed.retrying",
                sub_id=sub_id,
                model=resp.model_id,
                error=str(exc)[:200],
                text_head=resp.text[:500],
            )
            self.ctx.emit(
                "substep.parse_failed",
                sub_id=sub_id,
                error=str(exc)[:120],
                action="retrying with stricter directive",
            )
            # Self-correct: feed the failed response back and force JSON-only.
            messages_retry = [
                *messages,
                {"role": "assistant", "content": resp.text or "(空响应)"},
                {
                    "role": "user",
                    "content": (
                        "你刚才的输出不是合法 JSON——可能含有前导说明、解释性陈述、或 markdown 代码块。\n"
                        "请**仅**输出一份合法 JSON（首字符必须是 `{` 或 `[`），严格遵循上文规定的输出 schema。\n"
                        "不要任何前导文字、不要 ```json 标记、不要解释你的决策过程。\n"
                        "如果信息不足，请在 JSON 内字段中标记为 'unknown' 或 'needs_clarification'，"
                        "**不要拒绝输出 JSON**。"
                    ),
                },
            ]
            try:
                resp = await self.ctx.llm.complete(
                    system=system_full,
                    messages=messages_retry,
                    tier=tpl.model_tier,
                    max_tokens=tpl.max_tokens,
                    temperature=tpl.temperature,
                )
                self.ctx.usage.merge(resp.usage)
                parsed = resp.json() if tpl.output_format == "json" else resp.text
                logger.info("substep.parse_recovered", sub_id=sub_id)
                self.ctx.emit("substep.parse_recovered", sub_id=sub_id)
                await self._archive_substep(sub_id, tpl, system_full, resp, parsed=None)
            except (ValueError, json.JSONDecodeError) as exc2:
                logger.error(
                    "substep.parse_failed_after_retry",
                    sub_id=sub_id,
                    model=resp.model_id,
                    error=str(exc2)[:200],
                    text_head=resp.text[:500],
                )
                raise RuntimeError(
                    f"{sub_id} produced non-JSON output (after 1 retry). "
                    f"Raw response archived under evidence/ for diagnosis. Error: {exc2}"
                ) from exc2
        # Re-archive with parsed payload now that we have it.
        await self._archive_substep(sub_id, tpl, system_full, resp, parsed)
        logger.info(
            "substep.done",
            sub_id=sub_id,
            cost_usd=round(resp.usage.cost_usd, 4),
            model=resp.model_id,
        )
        self.ctx.emit(
            "substep.done",
            sub_id=sub_id,
            model=resp.model_id,
            cost_usd=round(resp.usage.cost_usd, 4),
            output_tokens=resp.usage.output_tokens,
        )

        await self.ctx.memory.session.save(
            kind="substep_result",
            key=f"{self.step_id}/{sub_id}",
            value={"parsed": parsed, "model_id": resp.model_id},
        )
        return resp, parsed

    async def _archive_substep(
        self,
        sub_id: str,
        tpl: PromptTemplate,
        system_rendered: str,
        resp: LlmResponse,
        parsed: Any,
    ) -> None:
        """归档 substep 的 system prompt + response + parsed。

        受 EVIDENCE_ARCHIVE_MODE 环境变量控制：
          - off          : 不写 prompt/response，仅写 parsed.json + meta.json
          - failure_only : 仅在 parsed=None（解析失败）时写完整 prompt+response
          - full (默认)  : 全写（旧行为，兼容现有用户）
        所有写入文件的 prompt/response 都自动脱敏 sk-/Bearer/Cookie 等敏感串。
        """
        base = self.ctx.evidence_dir / self.ctx.run_id / self.step_id / sub_id
        base.mkdir(parents=True, exist_ok=True)

        # 决定是否写 prompt+response
        write_full = (
            EVIDENCE_ARCHIVE_MODE == "full"
            or (EVIDENCE_ARCHIVE_MODE == "failure_only" and parsed is None)
        )
        if write_full:
            (base / "system_rendered.md").write_text(
                _redact_secrets(system_rendered), encoding="utf-8"
            )
            (base / "response_raw.txt").write_text(
                _redact_secrets(resp.text), encoding="utf-8"
            )

        if parsed is not None:
            (base / "parsed.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (base / "meta.json").write_text(
            json.dumps(
                {
                    "prompt_id": tpl.id,
                    "prompt_version": tpl.version,
                    "model_id": resp.model_id,
                    "usage": {
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                        "cache_read": resp.usage.cache_read_tokens,
                        "cost_usd": resp.usage.cost_usd,
                    },
                    "stop_reason": resp.stop_reason,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def build_meta(self, model_id: str, inputs: dict[str, Any]) -> ReportMeta:
        # project_code / project_name 由 inputs 透传(api 层校验过必填)
        pc = inputs.get("project_code") if isinstance(inputs, dict) else None
        pn = inputs.get("project_name") if isinstance(inputs, dict) else None
        return ReportMeta(
            run_id=self.ctx.run_id,
            sop_version="1.0.0",
            prompt_version=self.prompts.version,
            model_id=model_id,
            input_fingerprint=fingerprint(inputs),
            step_id=self.step_id,
            tenant_id=self.ctx.tenant_id,
            project_id=self.ctx.project_id,
            toolkit_version="0.1.0",
            project_code=str(pc).strip() if pc else None,
            project_name=str(pn).strip() if pn else None,
        )

    @abstractmethod
    async def execute(self) -> Any:
        """Run the full step and return the final validated report."""


def resolve_evidence_dir() -> Path:
    path = Path(settings.evidence_output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_report_dir() -> Path:
    path = Path(settings.report_output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path
