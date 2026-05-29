"""UI 一致性测试（Step 5 主力）: 设计稿 vs 实现 diff。

Pipeline:
  collect  → pair (baseline, actual) images (capture via Playwright if URLs given)
  process  → pixel diff + perceptual delta
  analyze  → bucket by severity, attach evidence
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.core.models.common import Severity
from packages.modules.base import Evidence, Executor, ExecutorInput, ExecutorOutput, Finding
from packages.modules.ui_testing.capture import capture_page
from packages.modules.ui_testing.diff import PixelDiff, compute_diff


def _severity_from_pct(pct: float) -> Severity:
    if pct > 0.2:
        return Severity.CRITICAL
    if pct > 0.1:
        return Severity.HIGH
    if pct > 0.02:
        return Severity.MEDIUM
    return Severity.LOW


class UiTestingExecutor(Executor):
    module_name = "ui_testing"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        """Expect inp.params['pairs']: list[dict(baseline, actual, url?)]."""
        pairs: list[dict[str, Any]] = inp.params.get("pairs", [])
        out_dir = Path(inp.params.get("capture_dir", "evidence/ui/captures"))
        out_dir.mkdir(parents=True, exist_ok=True)

        resolved: list[dict[str, Any]] = []
        for i, p in enumerate(pairs):
            baseline = Path(p["baseline"])
            actual_path = p.get("actual")
            if not actual_path and p.get("url"):
                target = out_dir / f"live_{i:03d}.png"
                cap = await capture_page(p["url"], target)
                if cap is not None:
                    actual_path = str(cap.path)
            if actual_path is None:
                continue
            resolved.append(
                {
                    "page_id": p.get("page_id", f"PAGE-UI-{i:04d}"),
                    "baseline": baseline,
                    "actual": Path(actual_path),
                }
            )
        return {"pairs": resolved, "diff_dir": Path(inp.params.get("diff_dir", "evidence/ui/diffs"))}

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        diffs: list[dict[str, Any]] = []
        diff_dir: Path = raw["diff_dir"]
        for pair in raw.get("pairs", []):
            out = diff_dir / f"{pair['page_id']}_diff.png"
            pd: PixelDiff = compute_diff(pair["baseline"], pair["actual"], out)
            diffs.append(
                {
                    "page_id": pair["page_id"],
                    "pixel_diff": pd,
                    "diff_pct": pd.diff_pct,
                    "severity": _severity_from_pct(pd.diff_pct),
                }
            )
        return {"diffs": diffs}

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        diffs = processed.get("diffs", [])
        max_pct = 0.0
        for d in diffs:
            pd: PixelDiff = d["pixel_diff"]
            if pd.diff_image:
                evidence.append(Evidence(kind="screenshot", path=str(pd.diff_image)))
            evidence.append(Evidence(kind="screenshot", path=str(pd.actual)))
            max_pct = max(max_pct, pd.diff_pct)
            if d["severity"] in {Severity.CRITICAL, Severity.HIGH}:
                findings.append(
                    Finding(
                        id=f"UID-{d['page_id']}",
                        title=f"{d['page_id']} 偏差 {pd.diff_pct:.2%}",
                        severity=d["severity"],
                        evidence=[Evidence(kind="screenshot", path=str(pd.actual))],
                        suggested_action="定位差异区域并修正",
                    )
                )
        metrics = {
            "pages": float(len(diffs)),
            "max_diff_pct": max_pct,
            "critical_pages": float(
                sum(1 for d in diffs if d["severity"] == Severity.CRITICAL)
            ),
        }
        return ExecutorOutput(
            module=self.module_name,
            metrics=metrics,
            findings=findings,
            evidence=evidence,
        )
