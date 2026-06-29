from __future__ import annotations

from bytedesk_omnigent.engine.proof import run_controlled_flywheel_proof


def test_controlled_flywheel_proof_exercises_full_loop(tmp_path) -> None:
    report = run_controlled_flywheel_proof(f"sqlite:///{tmp_path / 'proof.db'}")

    assert report["scenario"] == "controlled-goal-engine-flywheel"
    assert report["spawnedInitial"] == 1
    assert report["bookedOutcomeCents"] == 1_500
    assert report["parentRealizedValueCents"] == 1_500
    assert report["reallocatedCents"] > 0
    assert report["hotBudgetAfterCents"] > report["hotBudgetBeforeCents"]
    assert "funded" in report["decisionReasons"]
    assert "missing_value_rollup" in report["decisionReasons"]
    assert "rebalance_redeploy" in report["decisionReasons"]
    assert report["guardrails"] == {
        "syntheticOnly": True,
        "networkCalls": 0,
        "customerWrites": 0,
        "approvalRiskGateExercised": True,
    }
    assert report["failureProbe"] == {
        "failureClass": "provider",
        "status": "failed",
        "httpStatus": 409,
        "retryable": True,
        "detail": "unresolved goal correlation",
        "realizedValueUnchanged": True,
        "bookedOutcome": False,
    }
