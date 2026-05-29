"""Configuration loader: environment + standards YAML + prompt registry.

Usage:
    from packages.core.config import settings, standards
    settings.anthropic_api_key
    standards.quality.accuracy_baselines[...]
"""
from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"
STANDARDS_DIR = CONFIG_DIR / "standards"
# When packaged as a macOS .app, the launcher sets AITK_PROMPTS_DIR to a
# user-writable copy under ~/Library/Application Support/AITestToolkit/.
# This lets the override system + future prompt edits persist outside the
# read-only .app bundle. Dev mode falls through to ./configs/prompts.
import os as _bootstrap_os
_OVERRIDE_PROMPTS = _bootstrap_os.environ.get("AITK_PROMPTS_DIR")
PROMPTS_DIR = Path(_OVERRIDE_PROMPTS) if _OVERRIDE_PROMPTS else (CONFIG_DIR / "prompts")

# pydantic-settings 2.13 + Python 3.14 fails to hydrate fields from `env_file`
# when `extra="ignore"` is set (silent dropoff). Load .env into os.environ
# explicitly at import time so the fallback EnvSettingsSource path picks it up.
#
# We can't use `override=False` blindly: macOS Claude Desktop pre-sets
# `ANTHROPIC_API_KEY=` (empty string) in the child shell, which would block
# .env from winning. So: drop any empty-string env vars first, then load
# .env without clobbering real non-empty user overrides.
import os as _os
_env_path = ROOT / ".env"
if _env_path.exists():
    from dotenv import dotenv_values as _dotenv_values
    for _k in _dotenv_values(_env_path).keys():
        if _os.environ.get(_k, None) == "":
            _os.environ.pop(_k, None)
    load_dotenv(_env_path, override=False)


class Settings(BaseSettings):
    """Environment-driven settings (mapped from .env)."""

    model_config = SettingsConfigDict(
        # Anchor at project root so the CLI works regardless of cwd.
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # Note: with `case_sensitive=False`, pydantic-settings auto-maps
    # field_name → UPPER_CASE env var, so `alias=` is unnecessary and was
    # actually breaking .env loading in 2.x.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    # 档位别名 — 不写死版本号,由 Claude CLI 自动解析到账号最新版本(4.8/4.9…)
    model_opus: str = "opus"
    model_sonnet: str = "sonnet"
    model_haiku: str = "haiku"

    database_url: str = "sqlite+aiosqlite:///./toolkit.db"

    memory_backend: str = "sqlite"
    memory_max_session_turns: int = 50

    report_output_dir: Path = Path("./output/reports")
    evidence_output_dir: Path = Path("./output/evidence")

    max_tokens_per_call: int = 8000
    daily_budget_usd: float = 50.0


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Standard config missing: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class Standards:
    """Lazy accessor for the 5 standards YAML files (process/quality/data/tdr/team)."""

    @cached_property
    def process(self) -> dict[str, Any]:
        return _load_yaml(STANDARDS_DIR / "process.yaml")

    @cached_property
    def quality(self) -> dict[str, Any]:
        return _load_yaml(STANDARDS_DIR / "quality.yaml")

    @cached_property
    def data(self) -> dict[str, Any]:
        return _load_yaml(STANDARDS_DIR / "data.yaml")

    @cached_property
    def tdr(self) -> dict[str, Any]:
        return _load_yaml(STANDARDS_DIR / "tdr.yaml")

    @cached_property
    def team(self) -> dict[str, Any]:
        return _load_yaml(STANDARDS_DIR / "team.yaml")

    def reload(self) -> None:
        """Drop cached YAMLs so next access re-reads disk."""
        for attr in ("process", "quality", "data", "tdr", "team"):
            self.__dict__.pop(attr, None)


settings = Settings()

# 公网/Docker 部署:$AITK_DATA_DIR 显式覆盖所有数据落盘位置。
# .env 里可能写死了 ./output/reports,这里在创建 settings 之后强制重定向 — 这样开发
# 模式照旧用 ./output,生产/Docker 自动用挂载卷,不需要改 .env。
import os as _os_post
_aitk_data = _os_post.environ.get("AITK_DATA_DIR")
if _aitk_data:
    _base = Path(_aitk_data).expanduser()
    settings.report_output_dir = _base / "output" / "reports"
    settings.evidence_output_dir = _base / "output" / "evidence"
    settings.report_output_dir.mkdir(parents=True, exist_ok=True)
    settings.evidence_output_dir.mkdir(parents=True, exist_ok=True)

standards = Standards()
