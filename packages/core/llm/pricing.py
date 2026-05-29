"""Token pricing table (USD per 1M tokens). Keep in sync with anthropic.com/pricing."""
from __future__ import annotations

from typing import TypedDict


class ModelPricing(TypedDict):
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


# 按档位(tier)定价 — 不绑定具体版本号,Anthropic 出新版(4.8/4.9…)自动沿用同档价。
# 同档位涨价时只需改这里一处。
TIER_PRICING: dict[str, ModelPricing] = {
    "opus": {
        "input_per_mtok": 15.0,
        "output_per_mtok": 75.0,
        "cache_write_per_mtok": 18.75,
        "cache_read_per_mtok": 1.50,
    },
    "sonnet": {
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "haiku": {
        "input_per_mtok": 1.0,
        "output_per_mtok": 5.0,
        "cache_write_per_mtok": 1.25,
        "cache_read_per_mtok": 0.10,
    },
}

# 精确版本覆盖(可选)：仅当某个版本价格偏离档位标准价时才在此登记。
# 留空即可——默认全部走档位前缀匹配。
PRICING: dict[str, ModelPricing] = {}


def _tier_of(model_id: str) -> str | None:
    """从任意模型 ID / 别名里识别档位(opus/sonnet/haiku),不依赖版本号。

    支持: "opus" / "claude-opus-4-8" / "claude-opus-4-7-1m" / "Claude Opus 4.8" 等。
    """
    m = (model_id or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return None


def cost_usd(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    # 1) 精确版本覆盖优先;2) 否则按档位前缀匹配(支持任意新版本)
    p = PRICING.get(model_id)
    if not p:
        tier = _tier_of(model_id)
        p = TIER_PRICING.get(tier) if tier else None
    if not p:
        return 0.0
    return (
        input_tokens * p["input_per_mtok"]
        + output_tokens * p["output_per_mtok"]
        + cache_write_tokens * p["cache_write_per_mtok"]
        + cache_read_tokens * p["cache_read_per_mtok"]
    ) / 1_000_000
