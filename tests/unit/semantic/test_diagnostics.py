from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from ard_ossie.semantic.adjudication import (
    AdjudicationAttempt,
    DecisionRecord,
    DecisionReport,
)
from ard_ossie.semantic.diagnostics import (
    CandidateDiagnostic,
    EvidenceSummary,
    SemanticDiagnostics,
    load_trusted_decisions,
    masked_preview,
    write_semantic_diagnostics,
)

SOURCE_HASH = "3" * 64
CONFIG_HASH = "4" * 64


def _recovered_decision() -> DecisionRecord:
    selected = "candidate_0000000000000001"
    return DecisionRecord(
        decision_id="decision_0000000000000001",
        request_hash="0" * 64,
        source_hash=SOURCE_HASH,
        evidence_hash="2" * 64,
        candidate_set_id="candidate_set_0000000000000001",
        region_id="region_0000000000000001",
        decision_type="spacing",
        selected_candidate_id=selected,
        outcome="selected",
        source="recovered",
        confidence=0.92,
        provider="test",
        model="test",
        validation_codes=("LLM_LOW_CONFIDENCE_RECOVERED",),
        recovery_status="recovered",
        attempts=(
            AdjudicationAttempt(
                attempt_index=1,
                phase="primary",
                request_hash="3" * 64,
                candidate_id=selected,
                confidence=0.70,
                status="low_confidence",
                validation_codes=("LLM_CONFIDENCE_BELOW_THRESHOLD",),
            ),
            AdjudicationAttempt(
                attempt_index=2,
                phase="recovery",
                request_hash="4" * 64,
                candidate_id=selected,
                confidence=0.92,
                status="accepted",
            ),
        ),
        consensus_method="same_candidate",
        consensus_candidate_id=selected,
        recovery_count=1,
    )


def _diagnostics(
    secret_text: str = "민감한 원문 데이터 시맨틱 모델",
    *,
    publication_status: Literal["verified", "review_required", "failed"] = "failed",
    decisions: tuple[DecisionRecord, ...] = (),
) -> SemanticDiagnostics:
    findings = (
        [{"code": "INVARIANT_CHARACTER_LOSS", "message": "failed"}]
        if publication_status == "failed"
        else []
    )
    return SemanticDiagnostics(
        source_hash=SOURCE_HASH,
        configuration_hash=CONFIG_HASH,
        mode="candidate",
        publication_status=publication_status,
        stage="validation",
        evidence=EvidenceSummary(
            page_count=2,
            atom_count=100,
            whitespace_atom_count=12,
            region_count=4,
            hypothesis_count=0,
            extraction_mode="pdf_embedded",
        ),
        candidates=(
            CandidateDiagnostic(
                candidate_set_id="candidate_set_0000000000000001",
                region_id="region_0000000000000001",
                decision_type="spacing",
                candidate_count=2,
                scores=(0.8, 0.75),
            ),
        ),
        decisions=DecisionReport(source_hash=SOURCE_HASH, decisions=decisions),
        validation={
            "status": publication_status,
            "publishable": publication_status == "verified",
            "source_hash": SOURCE_HASH,
            "canonical_hash": "5" * 64,
            "findings": findings,
            "character_coverage": 0.99,
            "missing_atom_count": 1,
            "duplicate_atom_count": 0,
            "degraded_block_count": 0,
            "model_call_count": 0,
        },
        failure_codes=(("INVARIANT_CHARACTER_LOSS",) if publication_status == "failed" else ()),
        raw_previews=(secret_text,),
        raw_images=(b"\x89PNG\r\nsecret",),
    )


def test_default_diagnostics_do_not_contain_source_text_or_image_bytes(tmp_path: Path) -> None:
    write_semantic_diagnostics(tmp_path, _diagnostics())

    payload = "".join(path.read_text() for path in tmp_path.glob("*.json"))

    assert "민감한 원문 데이터 시맨틱 모델" not in payload
    assert "iVBOR" not in payload
    assert SOURCE_HASH in payload
    assert {path.name for path in tmp_path.glob("*.json")} == {
        "manifest.json",
        "evidence-summary.json",
        "candidate-report.json",
        "decision-report.json",
        "application-report.json",
        "validation-report.json",
        "failure-report.json",
    }
    assert not (tmp_path / "raw").exists()


def test_application_report_records_recovered_decision_as_applied(tmp_path: Path) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            publication_status="verified",
            decisions=(_recovered_decision(),),
        ),
    )

    report = json.loads((tmp_path / "application-report.json").read_text())

    assert report["primary_attempt_count"] == 1
    assert report["confidence_recovery_attempt_count"] == 1
    assert report["tie_break_attempt_count"] == 0
    assert report["recovered_decision_count"] == 1
    assert report["unresolved_low_confidence_count"] == 0
    assert report["applications"] == [
        {
            "candidate_set_id": "candidate_set_0000000000000001",
            "canonical_hash": "5" * 64,
            "decision_id": "decision_0000000000000001",
            "invariant_codes": [],
            "outcome": "applied",
            "selected_candidate_id": "candidate_0000000000000001",
            "validation_status": "verified",
        }
    ]


@pytest.mark.parametrize(
    ("publication_status", "expected_outcome", "expected_codes"),
    [
        ("review_required", "not_published", []),
        ("failed", "rejected_by_invariant", ["INVARIANT_CHARACTER_LOSS"]),
    ],
)
def test_application_report_distinguishes_deferred_and_rejected_recovery(
    tmp_path: Path,
    publication_status: Literal["review_required", "failed"],
    expected_outcome: str,
    expected_codes: list[str],
) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            publication_status=publication_status,
            decisions=(_recovered_decision(),),
        ),
    )

    application = json.loads((tmp_path / "application-report.json").read_text())["applications"][0]

    assert application["outcome"] == expected_outcome
    assert application["invariant_codes"] == expected_codes


def test_application_report_contains_no_raw_prompt_or_response(tmp_path: Path) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            decisions=(_recovered_decision(),),
        ),
    )

    payload = (tmp_path / "application-report.json").read_text()

    assert "민감한 원문 데이터 시맨틱 모델" not in payload
    assert '"prompt"' not in payload
    assert '"response"' not in payload


def test_masked_preview_is_bounded_and_short_values_are_disabled() -> None:
    assert masked_preview("short") is None
    preview = masked_preview("민감한 원문 데이터 시맨틱 모델")
    assert preview is not None
    assert len(preview) <= 24
    assert "…" in preview
    assert preview != "민감한 원문 데이터 시맨틱 모델"


def test_raw_diagnostics_require_explicit_argument(tmp_path: Path) -> None:
    diagnostics = _diagnostics()

    write_semantic_diagnostics(tmp_path, diagnostics, include_raw=True)

    assert "민감한 원문" in (tmp_path / "raw" / "previews.json").read_text()
    assert (tmp_path / "raw" / "image-0001.bin").read_bytes().startswith(b"\x89PNG")


def test_trusted_decision_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    write_semantic_diagnostics(tmp_path, _diagnostics())
    decision_path = tmp_path / "decision-report.json"
    expected_hash = hashlib.sha256(decision_path.read_bytes()).hexdigest()

    assert load_trusted_decisions(decision_path, expected_hash=expected_hash) == ()

    decision_path.write_text(json.dumps({"source_hash": SOURCE_HASH, "decisions": []}) + " ")
    with pytest.raises(ValueError, match="TRUSTED_DECISION_HASH_MISMATCH"):
        load_trusted_decisions(decision_path, expected_hash=expected_hash)
