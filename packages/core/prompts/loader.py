"""Prompt library loader.

Each prompt file is Markdown with YAML frontmatter:
    ---
    id: step1.1
    name: ...
    model_tier: sonnet
    temperature: 0.3
    max_tokens: 4000
    placeholders: [PRD, ...]
    output_format: json
    output_schema: <name>
    ---
    <prompt body — uses {{Name}} placeholders, CJK-friendly>

Step directories also contain a `meta.yaml` with:
    step_id, order, common_system_suffix
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from packages.core.config import PROMPTS_DIR
from packages.core.llm import ModelTier

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


@dataclass
class PromptTemplate:
    id: str
    name: str
    version: str
    model_tier: ModelTier
    temperature: float
    max_tokens: int
    placeholders: list[str]
    output_format: str
    output_schema: str | None
    body: str
    path: Path

    def render(self, values: dict[str, Any]) -> str:
        """Replace {{Name}} in body with values[Name]. Missing keys → literal '(未提供)'."""
        missing: list[str] = []

        def sub(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key in values and values[key] not in (None, ""):
                v = values[key]
                if isinstance(v, (dict, list)):
                    import json as _json

                    return _json.dumps(v, ensure_ascii=False, indent=2)
                return str(v)
            if key not in values:
                missing.append(key)
            return "(未提供)"

        rendered = PLACEHOLDER_RE.sub(sub, self.body)
        if missing:
            rendered += f"\n\n[注意：以下占位符未提供，请审慎处理：{', '.join(sorted(set(missing)))}]"
        return rendered


@dataclass
class PromptStep:
    step_id: str
    name: str
    version: str
    order: list[str]
    common_system_suffix: str
    templates: dict[str, PromptTemplate]  # by substep id e.g. step1.1

    def get(self, sub_id: str) -> PromptTemplate:
        if sub_id not in self.templates:
            raise KeyError(f"prompt {sub_id} not in step {self.step_id}")
        return self.templates[sub_id]

    def compose_system(self, template: PromptTemplate) -> str:
        """Attach the step-wide common suffix to a rendered template body."""
        return f"{template.body}\n\n{self.common_system_suffix}".strip()


def _parse_prompt_file(path: Path) -> PromptTemplate:
    raw = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"prompt file missing YAML frontmatter: {path}")
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    return PromptTemplate(
        id=fm["id"],
        name=fm["name"],
        version=fm.get("version", "0.0.0"),
        model_tier=ModelTier(fm.get("model_tier", "sonnet")),
        temperature=float(fm.get("temperature", 0.2)),
        max_tokens=int(fm.get("max_tokens", 4096)),
        placeholders=fm.get("placeholders", []),
        output_format=fm.get("output_format", "json"),
        output_schema=fm.get("output_schema"),
        body=body,
        path=path,
    )


OVERRIDES_DIR = PROMPTS_DIR.parent / "prompts_overrides"


def _override_path(step_dir_name: str, fname: str) -> Path:
    return OVERRIDES_DIR / step_dir_name / fname


def _resolve_prompt_path(step_dir_name: str, fname: str) -> Path:
    """Override file under configs/prompts_overrides/ takes precedence."""
    o = _override_path(step_dir_name, fname)
    if o.exists():
        return o
    return PROMPTS_DIR / step_dir_name / fname


def load_step(step_dir_name: str) -> PromptStep:
    """Load every prompt in a step directory plus its meta.yaml.

    A file in `configs/prompts_overrides/{step}/{filename}.md` (same name)
    transparently replaces the corresponding `configs/prompts/...` file.
    Cache disabled so edits-then-rerun pick up immediately.
    """
    step_dir = PROMPTS_DIR / step_dir_name
    meta_path = step_dir / "meta.yaml"
    if not meta_path.exists():
        raise FileNotFoundError(f"meta.yaml missing in {step_dir}")
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

    templates: dict[str, PromptTemplate] = {}
    for fname in meta["order"]:
        tpl = _parse_prompt_file(_resolve_prompt_path(step_dir_name, fname))
        templates[tpl.id] = tpl

    return PromptStep(
        step_id=meta["step_id"],
        name=meta["name"],
        version=meta.get("version", "0.0.0"),
        order=meta["order"],
        common_system_suffix=meta.get("common_system_suffix", ""),
        templates=templates,
    )


def write_prompt_override(step_dir_name: str, fname: str, body_or_md: str) -> Path:
    """Persist an edited prompt body as an override file.

    Accepts either a full markdown (with frontmatter) or just the body —
    if no frontmatter is detected we copy the original frontmatter and
    splice in the new body. Returns the override path.
    """
    target_dir = OVERRIDES_DIR / step_dir_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / fname

    raw = body_or_md
    if not raw.lstrip().startswith("---"):
        # Body-only edit: re-attach the original frontmatter.
        original = (PROMPTS_DIR / step_dir_name / fname).read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(original)
        if m:
            raw = f"---\n{m.group(1)}\n---\n{body_or_md.strip()}\n"
    target.write_text(raw, encoding="utf-8")
    return target


def reset_prompt_override(step_dir_name: str, fname: str) -> bool:
    """Delete an override file. Returns True if a file was removed."""
    p = _override_path(step_dir_name, fname)
    if p.exists():
        p.unlink()
        return True
    return False


def list_prompt_overrides() -> list[dict[str, Any]]:
    """List all currently-active overrides (for /settings page)."""
    if not OVERRIDES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for step_dir in OVERRIDES_DIR.iterdir():
        if not step_dir.is_dir():
            continue
        for f in step_dir.iterdir():
            if f.suffix == ".md":
                out.append({
                    "step_dir": step_dir.name,
                    "filename": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                })
    return out
