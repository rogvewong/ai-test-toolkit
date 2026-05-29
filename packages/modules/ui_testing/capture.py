"""Playwright-based page capture.

Gracefully no-ops if playwright is unavailable — the pipeline then accepts
externally captured PNGs supplied via the "actual_snapshots" input.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

try:
    from playwright.async_api import async_playwright  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]


@dataclass
class CaptureResult:
    url: str
    path: Path
    viewport: tuple[int, int]


async def capture_page(
    url: str, out_path: Path, *, viewport: tuple[int, int] = (1280, 720)
) -> CaptureResult | None:
    if async_playwright is None:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]}
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle")
            await page.screenshot(path=str(out_path), full_page=True)
            return CaptureResult(url=url, path=out_path, viewport=viewport)
        finally:
            await browser.close()


async def capture_many(
    urls: list[str], out_dir: Path, *, viewport: tuple[int, int] = (1280, 720)
) -> list[CaptureResult | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)

    async def _one(i: int, url: str) -> CaptureResult | None:
        async with sem:
            return await capture_page(
                url, out_dir / f"page_{i:03d}.png", viewport=viewport
            )

    return await asyncio.gather(*(_one(i, u) for i, u in enumerate(urls)))
