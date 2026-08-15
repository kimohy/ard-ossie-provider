"""Privacy-safe, atomic diagnostics for semantic PDF candidate runs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import Field

from ard_ossie.models import Sha256
from ard_ossie.semantic.adjudication import (
    DecisionId,
    DecisionRecord,
    DecisionReport,
    diagnostic_decision_record,
)
from ard_ossie.semantic.candidates import CandidateId, CandidateSetId
from ard_ossie.semantic.canonical import SemanticValidationReport
from ard_ossie.semantic.evidence import RegionId
from ard_ossie.semantic.models import ImmutableStrictModel

DIAGNOSTIC_REPORT_NAMES = (
    "manifest.json",
    "evidence-summary.json",
    "candidate-report.json",
    "decision-report.json",
    "application-report.json",
    "validation-report.json",
    "failure-report.json",
)


class EvidenceSummary(ImmutableStrictModel):
    page_count: int = Field(ge=1)
    atom_count: int = Field(ge=0)
    whitespace_atom_count: int = Field(ge=0)
    region_count: int = Field(ge=0)
    hypothesis_count: int = Field(ge=0)
    extraction_mode: Literal["pdf_embedded", "pdf_ocr", "pdf_mixed"]


class CandidateDiagnostic(ImmutableStrictModel):
    candidate_set_id: CandidateSetId
    region_id: RegionId
    decision_type: str = Field(min_length=1, max_length=40)
    candidate_count: int = Field(ge=1, le=5)
    candidate_ids: tuple[CandidateId, ...] = Field(min_length=1, max_length=5)
    scores: tuple[float, ...] = Field(min_length=1, max_length=5)


class DecisionApplicationRecord(ImmutableStrictModel):
    decision_id: DecisionId
    candidate_set_id: CandidateSetId
    selected_candidate_id: CandidateId
    canonical_hash: Sha256
    validation_status: Literal[
        "verified", "review_pending", "review_required", "failed"
    ]
    outcome: Literal[
        "applied_existing_candidate",
        "applied_generated_repair",
        "applied_fallback_pending_review",
        "not_published",
        "rejected_by_invariant",
    ]
    invariant_codes: tuple[str, ...] = ()


class ApplicationReport(ImmutableStrictModel):
    source_hash: Sha256
    primary_attempt_count: int = Field(ge=0)
    confidence_recovery_attempt_count: int = Field(ge=0)
    tie_break_attempt_count: int = Field(ge=0)
    generation_attempt_count: int = Field(ge=0)
    verification_attempt_count: int = Field(ge=0)
    recovered_decision_count: int = Field(ge=0)
    generated_decision_count: int = Field(ge=0)
    deferred_review_count: int = Field(ge=0)
    unresolved_low_confidence_count: int = Field(ge=0)
    applications: tuple[DecisionApplicationRecord, ...] = ()


class SemanticDiagnostics(ImmutableStrictModel):
    source_hash: Sha256
    configuration_hash: Sha256
    mode: Literal["legacy", "shadow", "candidate"]
    publication_status: Literal[
        "verified", "review_pending", "review_required", "failed"
    ]
    stage: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    evidence: EvidenceSummary
    candidates: tuple[CandidateDiagnostic, ...]
    decisions: DecisionReport
    validation: SemanticValidationReport
    failure_codes: tuple[str, ...] = ()
    raw_previews: tuple[str, ...] = Field(default=(), exclude=True)
    raw_images: tuple[bytes, ...] = Field(default=(), exclude=True)


def build_semantic_diagnostics(
    result: object,
    *,
    configuration_hash: Sha256,
    stage: str = "validation",
) -> SemanticDiagnostics:
    evidence = result.evidence
    validation = result.validation
    return SemanticDiagnostics(
        source_hash=evidence.source_hash,
        configuration_hash=configuration_hash,
        mode=result.mode.value,
        publication_status=validation.status.value,
        stage=stage,
        evidence=EvidenceSummary(
            page_count=evidence.page_count,
            atom_count=len(evidence.atoms),
            whitespace_atom_count=sum(atom.text.isspace() for atom in evidence.atoms),
            region_count=len(evidence.regions),
            hypothesis_count=len(evidence.hypotheses),
            extraction_mode=evidence.extraction_mode.value,
        ),
        candidates=tuple(
            CandidateDiagnostic(
                candidate_set_id=candidate_set.candidate_set_id,
                region_id=candidate_set.region_id,
                decision_type=candidate_set.decision_type,
                candidate_count=len(candidate_set.candidates),
                candidate_ids=tuple(
                    candidate.candidate_id for candidate in candidate_set.candidates
                ),
                scores=tuple(candidate.score for candidate in candidate_set.candidates),
            )
            for candidate_set in result.candidate_sets
        ),
        decisions=result.decisions,
        validation=validation,
        failure_codes=tuple(
            dict.fromkeys(
                [
                    *(finding.code for finding in validation.findings),
                    *(
                        code
                        for decision in result.decisions.decisions
                        for code in decision.validation_codes
                        if code != "LLM_LOW_CONFIDENCE_RECOVERED"
                    ),
                ]
            )
        ),
        raw_previews=tuple(block.text for block in result.canonical.blocks),
    )


def write_semantic_diagnostics(
    destination: Path,
    diagnostics: SemanticDiagnostics,
    *,
    include_raw: bool = False,
) -> tuple[Path, ...]:
    if destination.is_symlink():
        raise ValueError("SEMANTIC_DIAGNOSTICS_SYMLINK_NOT_ALLOWED")
    destination.mkdir(parents=True, exist_ok=True)
    encoded = semantic_diagnostic_payloads(diagnostics)
    written: list[Path] = []
    report_names = (
        *DIAGNOSTIC_REPORT_NAMES,
        *(("semantic-review.json",) if "semantic-review.json" in encoded else ()),
    )
    for name in report_names:
        path = destination / name
        _atomic_write(path, encoded[name])
        written.append(path)
    if include_raw:
        raw = destination / "raw"
        if raw.is_symlink():
            raise ValueError("SEMANTIC_DIAGNOSTICS_SYMLINK_NOT_ALLOWED")
        raw.mkdir(exist_ok=True)
        _atomic_write(raw / "previews.json", _json_bytes(list(diagnostics.raw_previews)))
        for index, value in enumerate(diagnostics.raw_images, start=1):
            _atomic_write(raw / f"image-{index:04d}.bin", value)
    return tuple(written)


def semantic_diagnostic_payloads(
    diagnostics: SemanticDiagnostics,
) -> dict[str, bytes]:
    application_report = _application_report(diagnostics)
    reports: dict[str, object] = {
        "evidence-summary.json": {
            "source_hash": diagnostics.source_hash,
            "configuration_hash": diagnostics.configuration_hash,
            **diagnostics.evidence.model_dump(mode="json"),
        },
        "candidate-report.json": {
            "source_hash": diagnostics.source_hash,
            "candidate_sets": [item.model_dump(mode="json") for item in diagnostics.candidates],
            "masked_previews": [
                preview
                for value in diagnostics.raw_previews
                if (preview := masked_preview(value)) is not None
            ],
            "image_hashes": [hashlib.sha256(value).hexdigest() for value in diagnostics.raw_images],
        },
        "decision-report.json": DecisionReport(
            source_hash=diagnostics.decisions.source_hash,
            decisions=tuple(
                diagnostic_decision_record(decision)
                for decision in diagnostics.decisions.decisions
            ),
        ).model_dump(mode="json"),
        "application-report.json": application_report.model_dump(mode="json"),
        "validation-report.json": diagnostics.validation.model_dump(mode="json"),
        "failure-report.json": {
            "source_hash": diagnostics.source_hash,
            "stage": diagnostics.stage,
            "publication_status": diagnostics.publication_status,
            "failure_codes": diagnostics.failure_codes,
        },
    }
    deferred = tuple(
        decision
        for decision in diagnostics.decisions.decisions
        if decision.outcome == "deferred_review"
    )
    if deferred:
        candidates_by_set = {
            item.candidate_set_id: item for item in diagnostics.candidates
        }
        reports["semantic-review.json"] = {
            "schema_version": "semantic-review-v1",
            "source_hash": diagnostics.source_hash,
            "entries": [
                {
                    "decision_id": decision.decision_id,
                    "candidate_set_id": decision.candidate_set_id,
                    "region_id": decision.region_id,
                    "decision_type": decision.decision_type,
                    "terminal_status": decision.outcome,
                    "fallback_candidate_id": decision.selected_candidate_id,
                    "fallback_policy_version": "safe-candidate-fallback-v1",
                    "confidence": decision.confidence,
                    "validation_codes": decision.validation_codes,
                    "candidate_options": [
                        {"candidate_id": candidate_id, "score": score}
                        for candidate_id, score in zip(
                            candidates_by_set[decision.candidate_set_id].candidate_ids,
                            candidates_by_set[decision.candidate_set_id].scores,
                            strict=True,
                        )
                    ],
                    "attempts": [
                        {
                            "attempt_index": attempt.attempt_index,
                            "phase": attempt.phase,
                            "request_hash": attempt.request_hash,
                            "candidate_id": attempt.candidate_id,
                            "confidence": attempt.confidence,
                            "status": attempt.status,
                            "validation_codes": attempt.validation_codes,
                            "provider_retry_count": attempt.provider_retry_count,
                            "provider_repair_count": attempt.provider_repair_count,
                        }
                        for attempt in decision.attempts
                    ],
                    "invariant_rejection_codes": list(
                        dict.fromkeys(
                            code
                            for attempt in decision.attempts
                            if attempt.status == "validation_rejected"
                            for code in attempt.validation_codes
                        )
                    ),
                    "replay_identity": {
                        "request_hash": decision.request_hash,
                        "evidence_hash": decision.evidence_hash,
                        "provider": decision.provider,
                        "model": decision.model,
                    },
                }
                for decision in deferred
            ],
        }
    encoded = {name: _json_bytes(payload) for name, payload in reports.items()}
    manifest = {
        "schema_version": "semantic-diagnostics-v1",
        "source_hash": diagnostics.source_hash,
        "configuration_hash": diagnostics.configuration_hash,
        "mode": diagnostics.mode,
        "publication_status": diagnostics.publication_status,
        "reports": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(encoded.items())
        },
    }
    encoded["manifest.json"] = _json_bytes(manifest)
    return encoded


def _application_report(diagnostics: SemanticDiagnostics) -> ApplicationReport:
    attempts = tuple(
        attempt for decision in diagnostics.decisions.decisions for attempt in decision.attempts
    )
    tracked = tuple(
        decision
        for decision in diagnostics.decisions.decisions
        if decision.selected_candidate_id is not None
    )
    invariant_codes = (
        tuple(finding.code for finding in diagnostics.validation.findings)
        if diagnostics.publication_status == "failed"
        else ()
    )
    applications: list[DecisionApplicationRecord] = []
    for decision in tracked:
        if decision.selected_candidate_id is None:
            raise ValueError("RECOVERED_DECISION_CANDIDATE_MISSING")
        if diagnostics.publication_status == "failed":
            outcome = "rejected_by_invariant"
        elif diagnostics.publication_status == "review_required":
            outcome = "not_published"
        elif decision.recovery_status == "generated":
            outcome = "applied_generated_repair"
        elif decision.recovery_status == "deferred_review":
            outcome = "applied_fallback_pending_review"
        else:
            outcome = "applied_existing_candidate"
        applications.append(
            DecisionApplicationRecord(
                decision_id=decision.decision_id,
                candidate_set_id=decision.candidate_set_id,
                selected_candidate_id=decision.selected_candidate_id,
                canonical_hash=diagnostics.validation.canonical_hash,
                validation_status=diagnostics.publication_status,
                outcome=outcome,
                invariant_codes=invariant_codes,
            )
        )
    return ApplicationReport(
        source_hash=diagnostics.source_hash,
        primary_attempt_count=sum(attempt.phase == "primary" for attempt in attempts),
        confidence_recovery_attempt_count=sum(attempt.phase == "recovery" for attempt in attempts),
        tie_break_attempt_count=sum(attempt.phase == "tiebreak" for attempt in attempts),
        generation_attempt_count=sum(attempt.phase == "generation" for attempt in attempts),
        verification_attempt_count=sum(
            attempt.phase == "verification" for attempt in attempts
        ),
        recovered_decision_count=sum(
            decision.recovery_status == "recovered" for decision in tracked
        ),
        generated_decision_count=sum(
            decision.recovery_status == "generated" for decision in tracked
        ),
        deferred_review_count=sum(
            decision.recovery_status == "deferred_review" for decision in tracked
        ),
        unresolved_low_confidence_count=sum(
            decision.recovery_status == "review_required"
            for decision in diagnostics.decisions.decisions
        ),
        applications=tuple(applications),
    )


def masked_preview(value: str) -> str | None:
    normalized = " ".join(value.split())
    if len(normalized) < 8:
        return None
    bounded = normalized[:24]
    if len(bounded) <= 9:
        return f"{bounded[:4]}…{bounded[-4:]}"
    return f"{bounded[:4]}…{bounded[-4:]}"


def load_trusted_decisions(
    path: Path,
    *,
    expected_hash: Sha256,
) -> tuple[DecisionRecord, ...]:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("TRUSTED_DECISION_HASH_MISMATCH")
    try:
        report = DecisionReport.model_validate_json(payload)
    except ValueError as error:
        raise ValueError("TRUSTED_DECISION_SCHEMA_INVALID") from error
    return report.decisions


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
