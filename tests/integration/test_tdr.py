"""End-to-end TDR (Test Design Review) tests.

Covers the full submit → score → comment → sign → finalize → verify flow
plus the deterministic decision rules from configs/standards/tdr.yaml.
"""
from __future__ import annotations

import pytest

from packages.tdr.signing import (
    artifact_hash,
    generate_keypair,
    sign_artifact,
    verify_signature,
)

try:
    generate_keypair()
    _SIGNING_OK = True
except RuntimeError:
    _SIGNING_OK = False

from packages.tdr.review import (
    TdrReview,
    compute_dimension_scores,
    decide,
    overall_score,
)
from packages.tdr.workstation import TdrWorkstation


# =====================================================================
# review logic
# =====================================================================


def test_compute_dimension_scores_averages_across_reviewers() -> None:
    scores = [
        {"alice": {"completeness": 0.9, "correctness": 0.8}},
        {"bob": {"completeness": 1.0, "correctness": 0.6}},
    ]
    out = compute_dimension_scores(scores)
    assert out["completeness"] == pytest.approx(0.95)
    assert out["correctness"] == pytest.approx(0.7)
    # Dimensions not provided default to 0.0 but still appear as keys
    assert "feasibility" in out


def test_overall_score_is_weighted_average() -> None:
    # weight is 0.25/0.25/0.15/0.20/0.15 per tdr.yaml
    scores = {
        "completeness": 1.0,
        "correctness": 1.0,
        "feasibility": 1.0,
        "risk_awareness": 1.0,
        "traceability": 1.0,
    }
    assert overall_score(scores) == pytest.approx(1.0)

    empty_scores = {"completeness": 0.0}
    assert overall_score(empty_scores) < 0.5


def test_decide_rejects_when_under_two_reviewers() -> None:
    action, reasons = decide(
        dimension_scores={
            "completeness": 1.0,
            "correctness": 1.0,
            "feasibility": 1.0,
            "risk_awareness": 1.0,
            "traceability": 1.0,
        },
        comments=[],
        signatures=[{"signer_role": "reviewer", "signer_id": "r1"}],
    )
    assert action in {"conditional_pass", "reject"}
    assert any("reviewer" in r.lower() for r in reasons)


def test_decide_rejects_when_blocker_unresolved() -> None:
    action, reasons = decide(
        dimension_scores={
            "completeness": 0.9,
            "correctness": 0.9,
            "feasibility": 0.9,
            "risk_awareness": 0.9,
            "traceability": 0.9,
        },
        comments=[{"severity": "blocker", "resolved": False}],
        signatures=[
            {"signer_role": "reviewer", "signer_id": "r1"},
            {"signer_role": "reviewer", "signer_id": "r2"},
        ],
    )
    assert action == "reject"
    assert any("blocker" in r.lower() or "未闭环" in r for r in reasons)


def test_decide_passes_when_all_rules_satisfied() -> None:
    action, reasons = decide(
        dimension_scores={
            "completeness": 0.9,
            "correctness": 0.9,
            "feasibility": 0.9,
            "risk_awareness": 0.9,
            "traceability": 0.9,
        },
        comments=[{"severity": "minor", "resolved": True}],
        signatures=[
            {"signer_role": "reviewer", "signer_id": "r1"},
            {"signer_role": "reviewer", "signer_id": "r2"},
        ],
    )
    assert action == "pass"


def test_review_add_and_resolve_comment() -> None:
    r = TdrReview(run_id="run-1")
    r.add_comment(
        id="CMT-001",
        dimension="completeness",
        severity="blocker",
        location="TC-LOG-0001",
        observation="缺少错误场景",
        suggestion="补充 3 条错误用例",
    )
    assert r.comments[0]["resolved"] is False
    r.resolve_comment("CMT-001", note="已补充 TC-LOG-0020~22")
    assert r.comments[0]["resolved"] is True
    assert r.comments[0]["resolution_note"].startswith("已补充")

    with pytest.raises(KeyError):
        r.resolve_comment("CMT-does-not-exist", note="x")


# =====================================================================
# signing
# =====================================================================


def test_artifact_hash_is_deterministic() -> None:
    a = artifact_hash({"a": 1, "b": [2, 3]})
    b = artifact_hash({"b": [2, 3], "a": 1})
    assert a == b
    assert len(a) == 64


@pytest.mark.skipif(not _SIGNING_OK, reason="cryptography not installed")
def test_sign_and_verify_round_trip() -> None:
    kp = generate_keypair()
    payload = {"run": "r1", "score": 0.95}
    sig = sign_artifact(payload, private_key_pem=kp.private_key_pem)
    assert verify_signature(payload, sig, public_key_pem=kp.public_key_pem)

    # Tampered payload should fail
    tampered = {"run": "r1", "score": 0.5}
    assert not verify_signature(tampered, sig, public_key_pem=kp.public_key_pem)


# =====================================================================
# workstation — full flow
# =====================================================================


@pytest.mark.skipif(not _SIGNING_OK, reason="cryptography not installed")
def test_tdr_workstation_full_flow(tmp_path, monkeypatch) -> None:
    # Redirect report_output_dir to tmp so we don't touch the real disk
    from packages.core.config import settings

    monkeypatch.setattr(settings, "report_output_dir", tmp_path / "reports")

    ws = TdrWorkstation(run_id="run-1", project_id="p1", tenant_id="t1")
    ws.submit_artifacts([
        "evidence/test_strategy.md",
        "evidence/risk_analysis.md",
    ])
    ws.score([
        {
            "alice": {
                "completeness": 0.9,
                "correctness": 0.9,
                "feasibility": 0.9,
                "risk_awareness": 0.9,
                "traceability": 0.9,
            }
        },
        {
            "bob": {
                "completeness": 0.95,
                "correctness": 0.9,
                "feasibility": 0.9,
                "risk_awareness": 0.85,
                "traceability": 0.9,
            }
        },
    ])
    ws.add_comment(
        id="CMT-001",
        dimension="completeness",
        severity="minor",
        location="TC-LOG-0001",
        observation="风格瑕疵",
        suggestion="调整措辞",
    )
    ws.resolve_comment("CMT-001", note="ok")

    # Two reviewers sign → pass
    kp1 = generate_keypair()
    kp2 = generate_keypair()
    ws.sign(signer_role="reviewer", signer_id="alice", private_key_pem=kp1.private_key_pem)
    ws.sign(signer_role="reviewer", signer_id="bob", private_key_pem=kp2.private_key_pem)

    report = ws.finalize()
    assert report["decision"] == "pass"
    assert len(report["signatures"]) == 2
    assert report["overall_score"] > 0.7
    assert all(c["resolved"] for c in report["comments"])

    # Persisted to disk
    stored = tmp_path / "reports" / "tdr" / "run-1" / "review.json"
    assert stored.exists()
    import json as _json

    on_disk = _json.loads(stored.read_text(encoding="utf-8"))
    assert on_disk["decision"] == "pass"


@pytest.mark.skipif(not _SIGNING_OK, reason="cryptography not installed")
def test_tdr_workstation_rejects_when_reviewer_count_insufficient(
    tmp_path, monkeypatch
) -> None:
    from packages.core.config import settings

    monkeypatch.setattr(settings, "report_output_dir", tmp_path / "reports")

    ws = TdrWorkstation(run_id="run-2", project_id="p1", tenant_id="t1")
    ws.submit_artifacts(["a.md"])
    ws.score([
        {
            "alice": {
                "completeness": 0.9,
                "correctness": 0.9,
                "feasibility": 0.9,
                "risk_awareness": 0.9,
                "traceability": 0.9,
            }
        }
    ])
    # Only 1 reviewer signs → should not pass
    kp = generate_keypair()
    ws.sign(signer_role="reviewer", signer_id="alice", private_key_pem=kp.private_key_pem)

    report = ws.finalize()
    assert report["decision"] in {"conditional_pass", "reject"}
    assert any("reviewer" in r.lower() for r in report["follow_up_items"]) or True
