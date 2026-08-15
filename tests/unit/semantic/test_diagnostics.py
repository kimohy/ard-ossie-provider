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
from ard_ossie.semantic.candidates import make_spacing_candidate
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


def _deferred_decision() -> DecisionRecord:
    return DecisionRecord(
        decision_id="decision_0000000000000002",
        request_hash="6" * 64,
        source_hash=SOURCE_HASH,
        evidence_hash="2" * 64,
        candidate_set_id="candidate_set_0000000000000001",
        region_id="region_0000000000000001",
        decision_type="spacing",
        selected_candidate_id="candidate_0000000000000002",
        outcome="deferred_review",
        source="fallback",
        confidence=0.45,
        provider="test",
        model="test",
        validation_codes=("LLM_SPACING_REPAIR_DEFERRED",),
        recovery_status="deferred_review",
        attempts=(
            AdjudicationAttempt(
                attempt_index=1,
                phase="generation",
                request_hash="7" * 64,
                confidence=0.91,
                status="validation_rejected",
                validation_codes=("SPACING_REPAIR_CHARACTER_MISMATCH",),
            ),
        ),
    )


def _deterministic_decision() -> DecisionRecord:
    return _recovered_decision().model_copy(
        update={
            "decision_id": "decision_0000000000000003",
            "source": "deterministic",
            "confidence": 1.0,
            "validation_codes": (),
            "recovery_status": "not_needed",
            "attempts": (),
            "consensus_method": "none",
            "consensus_candidate_id": None,
            "recovery_count": 0,
        }
    )


def _diagnostics(
    secret_text: str = "민감한 원문 데이터 시맨틱 모델",
    *,
    publication_status: Literal[
        "verified", "review_pending", "review_required", "failed"
    ] = "failed",
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
                candidate_ids=(
                    "candidate_0000000000000001",
                    "candidate_0000000000000002",
                ),
                scores=(0.8, 0.75),
            ),
        ),
        decisions=DecisionReport(source_hash=SOURCE_HASH, decisions=decisions),
        validation={
            "status": publication_status,
            "publishable": publication_status in {"verified", "review_pending"},
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


def test_generated_decision_report_persists_hashes_without_generated_text(
    tmp_path: Path,
) -> None:
    rendered = "민감한 생성 간격"
    character_sequence = "민감한생성간격"
    atom_ids = tuple(
        f"atom_{index + 1:016x}" for index in range(len(character_sequence))
    )
    generated = make_spacing_candidate(
        region_id="region_0000000000000001",
        rendered_text=rendered,
        character_sequence=character_sequence,
        atom_ids=atom_ids,
        source_whitespace=tuple(() for _ in range(len(atom_ids) - 1)),
        score=0.91,
        features={"llm_generated_spacing": 0.91},
    )
    decision = _recovered_decision().model_copy(
        update={
            "selected_candidate_id": generated.candidate_id,
            "source": "generated",
            "confidence": 0.91,
            "validation_codes": ("LLM_SPACING_REPAIR_APPLIED",),
            "recovery_status": "generated",
            "generated_candidate": generated,
        }
    )

    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(publication_status="verified", decisions=(decision,)),
    )

    payload = (tmp_path / "decision-report.json").read_text()
    persisted = json.loads(payload)["decisions"][0]["generated_candidate"]
    assert rendered not in payload
    assert character_sequence not in payload
    assert persisted["kind"] == "spacing_snapshot"
    assert persisted["rendered_text_hash"] == hashlib.sha256(rendered.encode()).hexdigest()


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
            "outcome": "applied_existing_candidate",
            "selected_candidate_id": "candidate_0000000000000001",
            "validation_status": "verified",
        }
    ]


def test_application_report_records_ordinary_selected_decisions(tmp_path: Path) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            publication_status="verified",
            decisions=(_deterministic_decision(),),
        ),
    )

    applications = json.loads(
        (tmp_path / "application-report.json").read_text()
    )["applications"]

    assert applications == [
        {
            "candidate_set_id": "candidate_set_0000000000000001",
            "canonical_hash": "5" * 64,
            "decision_id": "decision_0000000000000003",
            "invariant_codes": [],
            "outcome": "applied_existing_candidate",
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


def test_review_pending_writes_durable_bounded_review_debt(tmp_path: Path) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            publication_status="review_pending",
            decisions=(_deferred_decision(),),
        ),
    )

    review = json.loads((tmp_path / "semantic-review.json").read_text())
    application = json.loads((tmp_path / "application-report.json").read_text())

    assert review["schema_version"] == "semantic-review-v1"
    entry = review["entries"][0]
    assert entry["candidate_set_id"] == "candidate_set_0000000000000001"
    assert entry["fallback_candidate_id"] == "candidate_0000000000000002"
    assert entry["fallback_policy_version"] == "safe-candidate-fallback-v1"
    assert entry["candidate_options"] == [
        {"candidate_id": "candidate_0000000000000001", "score": 0.8},
        {"candidate_id": "candidate_0000000000000002", "score": 0.75},
    ]
    assert entry["attempts"] == [
        {
            "attempt_index": 1,
            "candidate_id": None,
            "confidence": 0.91,
            "phase": "generation",
            "provider_repair_count": 0,
            "provider_retry_count": 0,
            "request_hash": "7" * 64,
            "status": "validation_rejected",
            "validation_codes": ["SPACING_REPAIR_CHARACTER_MISMATCH"],
        }
    ]
    assert entry["invariant_rejection_codes"] == [
        "SPACING_REPAIR_CHARACTER_MISMATCH"
    ]
    assert entry["replay_identity"] == {
        "evidence_hash": "2" * 64,
        "model": "test",
        "provider": "test",
        "request_hash": "6" * 64,
    }
    assert application["applications"][0]["outcome"] == (
        "applied_fallback_pending_review"
    )
    assert "민감한 원문" not in (tmp_path / "semantic-review.json").read_text()


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
