"""TDR workstation — orchestrates submission → comments → decision → signatures.

Produces a TdrReviewReport ready for schema validation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packages.core.config import settings
from packages.core.models.common import ReportMeta, fingerprint
from packages.tdr.review import TdrReview, compute_dimension_scores, decide, overall_score
from packages.tdr.signing import artifact_hash, sign_artifact


class TdrWorkstation:
    """Thin façade that a CLI / API layer can drive."""

    def __init__(self, *, run_id: str, project_id: str, tenant_id: str) -> None:
        self.run_id = run_id
        self.project_id = project_id
        self.tenant_id = tenant_id
        self.review = TdrReview(run_id=run_id)
        self._storage = Path(settings.report_output_dir) / "tdr" / run_id
        self._storage.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def submit_artifacts(self, artifacts: list[str]) -> None:
        self.review.artifacts_reviewed = artifacts

    def score(self, reviewer_scores: list[dict[str, dict[str, float]]]) -> None:
        self.review.dimension_scores = compute_dimension_scores(reviewer_scores)

    def add_comment(self, **kwargs: Any) -> None:
        self.review.add_comment(**kwargs)

    def resolve_comment(self, comment_id: str, note: str) -> None:
        self.review.resolve_comment(comment_id, note)

    def sign(
        self,
        *,
        signer_role: str,
        signer_id: str,
        private_key_pem: bytes,
    ) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "artifacts": self.review.artifacts_reviewed,
            "dimension_scores": self.review.dimension_scores,
            "comments": self.review.comments,
        }
        sig = sign_artifact(payload, private_key_pem=private_key_pem)
        entry = {
            "signer_role": signer_role,
            "signer_id": signer_id,
            "signed_at": datetime.now(timezone.utc).isoformat(),
            "artifact_hash": artifact_hash(payload),
            "signature": sig,
        }
        self.review.signatures.append(entry)
        return entry

    # ------------------------------------------------------------------
    def finalize(self) -> dict[str, Any]:
        decision, reasons = decide(
            dimension_scores=self.review.dimension_scores,
            comments=self.review.comments,
            signatures=self.review.signatures,
        )
        report = {
            "meta": ReportMeta(
                run_id=self.run_id,
                step_id="tdr",
                model_id="n/a (human review)",
                input_fingerprint=fingerprint(self.review.artifacts_reviewed),
                tenant_id=self.tenant_id,
                project_id=self.project_id,
                actor="tdr_committee",
            ).model_dump(mode="json"),
            "artifacts_reviewed": self.review.artifacts_reviewed,
            "dimension_scores": self.review.dimension_scores,
            "overall_score": overall_score(self.review.dimension_scores),
            "comments": self.review.comments,
            "decision": decision,
            "follow_up_items": [reason for reason in reasons if "未闭环" in reason],
            "signatures": self.review.signatures,
        }
        out = self._storage / "review.json"
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return report
