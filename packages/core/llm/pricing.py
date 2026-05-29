"""Token pricing table (USD per 1M tokens). Keep in sync with anthropic.com/pricing."""
from __future__ import annotations

from typing import TypedDict


class ModelPricing(TypedDict):
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-7": {
        "input_per_mtok": 15.0,
        "output_per_mtok": 75.0,
        "cache_write_per_mtok": 18.75,
        "cache_read_per_mtok": 1.50,
    },
    "claude-sonnet-4-6": {
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input_per_mtok": 1.0,
        "output_per_mtok": 5.0,
        "cache_write_per_mtok": 1.25,
        "cache_read_per_mtok": 0.10,
    },
}


def cost_usd(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    p = PRICING.get(model_id)
    if not p:
        return 0.0
    return (
        input_tokens * p["input_per_mtok"]
        + output_tokens * p["output_per_mtok"]
        + cache_write_tokens * p["cache_write_per_mtok"]
        + cache_read_tokens * p["cache_read_per_mtok"]
    ) / 1_000_000
