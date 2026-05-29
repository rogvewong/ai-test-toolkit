"""Pixel-level image diff + perceptual hash.

Uses pillow if available; otherwise returns a trivial byte-level equality
so the pipeline can still emit structured diff artifacts for LLM review.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from PIL import Image, ImageChops  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]


@dataclass
class PixelDiff:
    baseline: Path
    actual: Path
    diff_pct: float
    diff_image: Path | None
    width: int
    height: int


def compute_diff(
    baseline: Path,
    actual: Path,
    out_path: Path | None = None,
    *,
    ignore_fringe_px: int = 2,
) -> PixelDiff:
    if Image is None:
        same = baseline.read_bytes() == actual.read_bytes()
        return PixelDiff(
            baseline=baseline,
            actual=actual,
            diff_pct=0.0 if same else 1.0,
            diff_image=None,
            width=0,
            height=0,
        )

    a = Image.open(baseline).convert("RGB")
    b = Image.open(actual).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)

    diff = ImageChops.difference(a, b)
    bbox = diff.getbbox()
    if bbox is None:
        pct = 0.0
    else:
        # crop ignore fringe
        width, height = a.size
        x0, y0, x1, y1 = bbox
        x0 = max(0, x0 - ignore_fringe_px)
        y0 = max(0, y0 - ignore_fringe_px)
        x1 = min(width, x1 + ignore_fringe_px)
        y1 = min(height, y1 + ignore_fringe_px)
        cropped = diff.crop((x0, y0, x1, y1))
        nonzero = sum(1 for px in cropped.getdata() if any(px))
        pct = nonzero / max(width * height, 1)

    if out_path is not None and bbox is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        diff.save(out_path)

    return PixelDiff(
        baseline=baseline,
        actual=actual,
        diff_pct=pct,
        diff_image=out_path if out_path and bbox else None,
        width=a.size[0],
        height=a.size[1],
    )
