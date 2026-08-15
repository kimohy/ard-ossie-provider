"""Deterministic-first adjudication over closed semantic candidate sets."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from ard_ossie.canonical import canonical_hash
from ard_ossie.llm.contracts import (
    LLMImagePart,
    LLMMultimodalMessage,
    LLMProvider,
    LLMTextPart,
    ProviderExecutionError,
)
from ard_ossie.llm.service import LLMService
from ard_ossie.models import Sha256
from ard_ossie.semantic.candidates import (
    BlockCandidate,
    Candidate,
    CandidateId,
    CandidateSet,
    CandidateSetId,
    ContinuationCandidate,
    ReadingOrderCandidate,
    RecognitionCandidate,
    SpacingBoundary,
    SpacingCandidate,
    TableCandidate,
    is_invariant_proven_table,
    make_spacing_candidate,
)
from ard_ossie.semantic.evidence import RegionId
from ard_ossie.semantic.models import ImmutableStrictModel
from ard_ossie.semantic.spacing_repair import (
    SpacingRepairProposal,
    SpacingVerification,
    build_generated_candidate,
    fallback_spacing_candidate,
    spacing_defect_codes,
    spacing_generation_messages,
    spacing_repair_schema,
    spacing_verification_messages,
    spacing_verification_schema,
)

PROMPT_VERSION = "semantic-candidate-adjudication-v2"
DecisionId = Annotated[str, StringConstraints(pattern=r"^decision_[0-9a-f]{16}$")]
ValidationCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


class AdjudicationPolicy(ImmutableStrictModel):
    auto_accept_score: float = Field(default=0.82, ge=0, le=1)
    auto_accept_margin: float = Field(default=0.12, ge=0, le=1)
    minimum_model_confidence: float = Field(default=0.80, ge=0, le=1)
    max_candidates: int = Field(default=5, ge=1, le=5)
    max_schema_attempts: int = Field(default=2, ge=1, le=2)
    max_confidence_recovery_attempts: int = Field(default=2, ge=0, le=2)
    consensus_votes_required: int = Field(default=2, ge=2, le=2)


class CandidateChoice(ImmutableStrictModel):
    candidate_id: CandidateId
    confidence: float = Field(ge=0, le=1)


class AdjudicationAttempt(ImmutableStrictModel):
    attempt_index: int = Field(ge=1, le=6)
    phase: Literal["primary", "recovery", "tiebreak", "generation", "verification"]
    request_hash: Sha256
    candidate_id: CandidateId | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    status: Literal[
        "accepted",
        "low_confidence",
        "candidate_unknown",
        "provider_rejected",
        "validation_rejected",
    ]
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)
    provider_retry_count: int = Field(default=0, ge=0, le=2)
    provider_repair_count: int = Field(default=0, ge=0, le=2)


class GeneratedSpacingSnapshot(ImmutableStrictModel):
    kind: Literal["spacing_snapshot"] = "spacing_snapshot"
    candidate_id: CandidateId
    region_id: RegionId
    atom_ids: tuple[str, ...] = Field(min_length=1)
    boundaries: tuple[SpacingBoundary, ...]
    score: float = Field(ge=0, le=1)
    features: dict[str, float]
    rendered_text_hash: Sha256
    character_sequence_hash: Sha256


class DecisionRecord(ImmutableStrictModel):
    decision_id: DecisionId
    request_hash: Sha256
    source_hash: Sha256
    evidence_hash: Sha256
    candidate_set_id: CandidateSetId
    region_id: RegionId
    decision_type: str = Field(min_length=1, max_length=40)
    selected_candidate_id: CandidateId | None = None
    outcome: Literal["selected", "review_required", "deferred_review"]
    source: Literal[
        "deterministic",
        "model",
        "recovered",
        "cache",
        "unavailable",
        "provider",
        "generated",
        "fallback",
    ]
    confidence: float = Field(ge=0, le=1)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)
    retry_count: int = Field(default=0, ge=0, le=12)
    repair_count: int = Field(default=0, ge=0, le=12)
    recovery_status: Literal[
        "not_needed",
        "recovered",
        "review_required",
        "generated",
        "deferred_review",
    ] = "not_needed"
    attempts: tuple[AdjudicationAttempt, ...] = Field(default=(), max_length=6)
    consensus_method: Literal["none", "same_candidate", "two_of_three"] = "none"
    consensus_candidate_id: CandidateId | None = None
    recovery_count: int = Field(default=0, ge=0, le=2)
    generated_candidate: SpacingCandidate | GeneratedSpacingSnapshot | None = None


class DecisionReport(ImmutableStrictModel):
    source_hash: Sha256
    decisions: tuple[DecisionRecord, ...]


@dataclass(frozen=True)
class _VotePhaseResult:
    choice: CandidateChoice | None
    attempts: tuple[AdjudicationAttempt, ...]
    validation_codes: tuple[str, ...]
    retry_count: int = 0
    repair_count: int = 0


def candidate_choice_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "candidate_id": {
                "type": "string",
                "pattern": r"^candidate_[0-9a-f]{16}$",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["candidate_id", "confidence"],
        "additionalProperties": False,
    }


class CandidateAdjudicator:
    def __init__(
        self,
        provider: LLMProvider | None,
        *,
        policy: AdjudicationPolicy | None = None,
        trusted: tuple[DecisionRecord, ...] = (),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.policy = policy or AdjudicationPolicy()
        self.trusted = trusted
        self._service = None if provider is None else LLMService(provider, sleep=sleep)

    def decide(
        self,
        candidate_set: CandidateSet,
        *,
        page_crop: bytes | None = None,
        evidence_hash: Sha256 | None = None,
    ) -> DecisionRecord:
        candidates = tuple(
            sorted(
                candidate_set.candidates,
                key=lambda item: (-item.score, item.candidate_id),
            )[: self.policy.max_candidates]
        )
        if not candidates:
            raise ValueError("ADJUDICATION_CANDIDATES_EMPTY")
        provider_name, model = _provider_identity(self.provider)
        resolved_evidence_hash = evidence_hash or canonical_hash(
            candidate_set.model_dump(mode="json")
        )
        request_hash = _request_hash(
            candidate_set,
            evidence_hash=resolved_evidence_hash,
            provider=provider_name,
            model=model,
            policy=self.policy,
            page_crop=page_crop,
        )

        proven_tables = tuple(
            candidate for candidate in candidates if is_invariant_proven_table(candidate)
        )
        if len(proven_tables) > 1:
            raise ValueError("ADJUDICATION_MULTIPLE_INVARIANT_PROOFS")
        if proven_tables:
            proven = proven_tables[0]
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=proven.candidate_id,
                outcome="selected",
                source="deterministic",
                confidence=1.0,
                provider=provider_name,
                model=model,
            )

        best = candidates[0]
        runner_score = candidates[1].score if len(candidates) > 1 else 0.0
        best_defects = (
            spacing_defect_codes(best) if isinstance(best, SpacingCandidate) else ()
        )
        if (
            best.score >= self.policy.auto_accept_score
            and best.score - runner_score >= self.policy.auto_accept_margin
            and not best_defects
        ):
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=best.candidate_id,
                outcome="selected",
                source="deterministic",
                confidence=best.score,
                provider=provider_name,
                model=model,
            )

        allowlist = {candidate.candidate_id for candidate in candidates}
        trusted = next(
            (
                current
                for item in self.trusted
                if (
                    current := _materialize_generated_candidate(
                        item,
                        candidate_set=candidate_set,
                    )
                )
                is not None
                and _trusted_decision_matches(
                    current,
                    candidate_set=candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    provider=provider_name,
                    model=model,
                    allowlist=allowlist,
                    candidates=candidates,
                    policy=self.policy,
                )
            ),
            None,
        )
        if trusted is not None:
            return _cached_record(trusted)

        if self._service is None:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                attempts=(),
                validation_codes=("LLM_PROVIDER_UNAVAILABLE",),
            )

        try:
            primary = self._run_vote_phase(
                candidate_set,
                candidates=candidates,
                allowlist=allowlist,
                request_hash=request_hash,
                phase="primary",
                prior_votes=(),
                page_crop=page_crop,
                start_index=1,
            )
        except ProviderExecutionError as error:
            primary_messages = _messages(
                candidate_set,
                candidates,
                phase="primary",
                prior_votes=(),
            )
            failed_attempt = AdjudicationAttempt(
                attempt_index=1,
                phase="primary",
                request_hash=_attempt_request_hash(
                    request_hash,
                    phase="primary",
                    messages=primary_messages,
                    attempt_index=1,
                ),
                status="provider_rejected",
                validation_codes=(error.code,),
                provider_retry_count=error.retry_count,
                provider_repair_count=error.repair_count,
            )
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                attempts=(failed_attempt,),
                validation_codes=(error.code,),
            )
        if primary.choice is None:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                validation_codes=primary.validation_codes,
                attempts=primary.attempts,
            )
        selected_primary = next(
            candidate
            for candidate in candidates
            if candidate.candidate_id == primary.choice.candidate_id
        )
        if isinstance(selected_primary, SpacingCandidate) and (
            primary.choice.confidence < self.policy.minimum_model_confidence
            or spacing_defect_codes(selected_primary)
        ):
            return self._run_spacing_repair(
                candidate_set,
                candidates=tuple(
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, SpacingCandidate)
                ),
                anchor=selected_primary,
                primary=primary,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
            )
        if primary.choice.confidence >= self.policy.minimum_model_confidence:
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=primary.choice.candidate_id,
                outcome="selected",
                source="model",
                confidence=primary.choice.confidence,
                provider=provider_name,
                model=model,
                retry_count=primary.retry_count,
                repair_count=primary.repair_count,
                attempts=primary.attempts,
            )
        if self.policy.max_confidence_recovery_attempts < 1:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONFIDENCE_TOO_LOW",),
                attempts=primary.attempts,
            )

        recovery = self._run_vote_phase(
            candidate_set,
            candidates=candidates,
            allowlist=allowlist,
            request_hash=request_hash,
            phase="recovery",
            prior_votes=primary.attempts,
            page_crop=page_crop,
            start_index=len(primary.attempts) + 1,
        )
        attempts = (*primary.attempts, *recovery.attempts)
        if recovery.choice is None:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                validation_codes=recovery.validation_codes,
                attempts=attempts,
            )
        if recovery.choice.confidence < self.policy.minimum_model_confidence:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONFIDENCE_RECOVERY_EXHAUSTED",),
                attempts=attempts,
            )
        if recovery.choice.candidate_id != primary.choice.candidate_id:
            if self.policy.max_confidence_recovery_attempts < 2:
                return _deferred_candidate_record(
                    candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    provider=provider_name,
                    model=model,
                    validation_codes=("LLM_CONSENSUS_NOT_REACHED",),
                    attempts=attempts,
                )
            tiebreak = self._run_vote_phase(
                candidate_set,
                candidates=candidates,
                allowlist=allowlist,
                request_hash=request_hash,
                phase="tiebreak",
                prior_votes=attempts,
                page_crop=page_crop,
                start_index=len(attempts) + 1,
            )
            all_attempts = (*attempts, *tiebreak.attempts)
            if tiebreak.choice is None:
                return _deferred_candidate_record(
                    candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    provider=provider_name,
                    model=model,
                    validation_codes=tiebreak.validation_codes,
                    attempts=all_attempts,
                )
            votes = Counter(
                (
                    primary.choice.candidate_id,
                    recovery.choice.candidate_id,
                    tiebreak.choice.candidate_id,
                )
            )
            majority_id, majority_count = votes.most_common(1)[0]
            qualified = (
                tiebreak.choice.confidence >= self.policy.minimum_model_confidence
                and majority_count >= self.policy.consensus_votes_required
                and tiebreak.choice.candidate_id == majority_id
            )
            if qualified:
                return _record(
                    candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    selected_candidate_id=majority_id,
                    outcome="selected",
                    source="recovered",
                    confidence=tiebreak.choice.confidence,
                    provider=provider_name,
                    model=model,
                    validation_codes=("LLM_LOW_CONFIDENCE_RECOVERED",),
                    retry_count=tiebreak.retry_count,
                    repair_count=tiebreak.repair_count,
                    recovery_status="recovered",
                    attempts=all_attempts,
                    consensus_method="two_of_three",
                    consensus_candidate_id=majority_id,
                    recovery_count=2,
                )
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONSENSUS_NOT_REACHED",),
                attempts=all_attempts,
            )
        return _record(
            candidate_set,
            request_hash=request_hash,
            evidence_hash=resolved_evidence_hash,
            selected_candidate_id=recovery.choice.candidate_id,
            outcome="selected",
            source="recovered",
            confidence=recovery.choice.confidence,
            provider=provider_name,
            model=model,
            validation_codes=("LLM_LOW_CONFIDENCE_RECOVERED",),
            retry_count=recovery.retry_count,
            repair_count=recovery.repair_count,
            recovery_status="recovered",
            attempts=attempts,
            consensus_method="same_candidate",
            consensus_candidate_id=recovery.choice.candidate_id,
            recovery_count=1,
        )

    def _run_spacing_repair(
        self,
        candidate_set: CandidateSet,
        *,
        candidates: tuple[SpacingCandidate, ...],
        anchor: SpacingCandidate,
        primary: _VotePhaseResult,
        request_hash: Sha256,
        evidence_hash: Sha256,
        provider: str,
        model: str,
    ) -> DecisionRecord:
        assert self._service is not None
        generation_messages = spacing_generation_messages(
            candidate_set,
            candidates,
            anchor,
        )
        generation_index = len(primary.attempts) + 1
        generation_hash = _attempt_request_hash(
            request_hash,
            phase="generation",
            messages=generation_messages,
            attempt_index=generation_index,
        )
        try:
            generation_result = self._service.generate_structured(
                schema=spacing_repair_schema(),
                messages=generation_messages,
            )
        except ProviderExecutionError as error:
            generation_attempt = AdjudicationAttempt(
                attempt_index=generation_index,
                phase="generation",
                request_hash=generation_hash,
                status="provider_rejected",
                validation_codes=(error.code,),
                provider_retry_count=error.retry_count,
                provider_repair_count=error.repair_count,
            )
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=evidence_hash,
                provider=provider,
                model=model,
                attempts=(*primary.attempts, generation_attempt),
                validation_codes=(error.code,),
            )

        proposal = SpacingRepairProposal.model_validate(generation_result.structured)
        try:
            generated = build_generated_candidate(
                anchor,
                proposal.rendered_text,
                proposal.confidence,
            )
            deterministic_codes = spacing_defect_codes(generated)
            if deterministic_codes:
                raise ValueError(deterministic_codes[0])
        except ValueError as error:
            code = str(error)
            generation_attempt = AdjudicationAttempt(
                attempt_index=generation_index,
                phase="generation",
                request_hash=generation_hash,
                confidence=proposal.confidence,
                status="validation_rejected",
                validation_codes=(code,),
                provider_retry_count=generation_result.metadata.retry_count,
                provider_repair_count=generation_result.metadata.repair_count,
            )
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=evidence_hash,
                provider=provider,
                model=model,
                attempts=(*primary.attempts, generation_attempt),
            )

        generation_low = proposal.confidence < self.policy.minimum_model_confidence
        generation_attempt = AdjudicationAttempt(
            attempt_index=generation_index,
            phase="generation",
            request_hash=generation_hash,
            candidate_id=generated.candidate_id,
            confidence=proposal.confidence,
            status="low_confidence" if generation_low else "accepted",
            validation_codes=("LLM_CONFIDENCE_TOO_LOW",) if generation_low else (),
            provider_retry_count=generation_result.metadata.retry_count,
            provider_repair_count=generation_result.metadata.repair_count,
        )

        verification_messages = spacing_verification_messages(
            candidate_set,
            candidates,
            generated,
        )
        verification_index = generation_index + 1
        verification_hash = _attempt_request_hash(
            request_hash,
            phase="verification",
            messages=verification_messages,
            attempt_index=verification_index,
        )
        verification_ids = tuple(
            dict.fromkeys(
                candidate.candidate_id for candidate in (*candidates, generated)
            )
        )
        try:
            verification_result = self._service.generate_structured(
                schema=spacing_verification_schema(verification_ids),
                messages=verification_messages,
            )
        except ProviderExecutionError as error:
            verification_attempt = AdjudicationAttempt(
                attempt_index=verification_index,
                phase="verification",
                request_hash=verification_hash,
                status="provider_rejected",
                validation_codes=(error.code,),
                provider_retry_count=error.retry_count,
                provider_repair_count=error.repair_count,
            )
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=evidence_hash,
                provider=provider,
                model=model,
                attempts=(*primary.attempts, generation_attempt, verification_attempt),
                validation_codes=(error.code,),
            )

        verification = SpacingVerification.model_validate(verification_result.structured)
        verification_low = verification.confidence < self.policy.minimum_model_confidence
        generated_selected = verification.candidate_id == generated.candidate_id
        verification_codes = list(verification.validation_codes)
        if verification_low and "LLM_CONFIDENCE_TOO_LOW" not in verification_codes:
            verification_codes.append("LLM_CONFIDENCE_TOO_LOW")
        if not generated_selected and len(verification_codes) < 4:
            verification_codes.append("LLM_GENERATED_CANDIDATE_REJECTED")
        accepted = (
            not generation_low
            and not verification_low
            and generated_selected
            and not verification.validation_codes
        )
        verification_attempt = AdjudicationAttempt(
            attempt_index=verification_index,
            phase="verification",
            request_hash=verification_hash,
            candidate_id=verification.candidate_id,
            confidence=verification.confidence,
            status=(
                "accepted"
                if accepted
                else "low_confidence"
                if verification_low
                else "validation_rejected"
            ),
            validation_codes=tuple(verification_codes),
            provider_retry_count=verification_result.metadata.retry_count,
            provider_repair_count=verification_result.metadata.repair_count,
        )
        attempts = (*primary.attempts, generation_attempt, verification_attempt)
        if not accepted:
            return _deferred_candidate_record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=evidence_hash,
                provider=provider,
                model=model,
                attempts=attempts,
            )
        return _record(
            candidate_set,
            request_hash=request_hash,
            evidence_hash=evidence_hash,
            selected_candidate_id=generated.candidate_id,
            outcome="selected",
            source="generated",
            confidence=min(proposal.confidence, verification.confidence),
            provider=provider,
            model=model,
            validation_codes=("LLM_SPACING_REPAIR_APPLIED",),
            recovery_status="generated",
            attempts=attempts,
            generated_candidate=generated,
        )

    def _run_vote_phase(
        self,
        candidate_set: CandidateSet,
        *,
        candidates: tuple[Candidate, ...],
        allowlist: set[CandidateId],
        request_hash: Sha256,
        phase: Literal["primary", "recovery", "tiebreak"],
        prior_votes: tuple[AdjudicationAttempt, ...],
        page_crop: bytes | None,
        start_index: int,
    ) -> _VotePhaseResult:
        assert self._service is not None
        messages = _messages(
            candidate_set,
            candidates,
            phase=phase,
            prior_votes=prior_votes,
        )
        attempts: list[AdjudicationAttempt] = []
        validation_codes: list[str] = []
        for schema_attempt in range(self.policy.max_schema_attempts):
            active_messages = (
                _corrective_messages(messages, validation_codes[-1])
                if validation_codes
                else messages
            )
            attempt_index = start_index + schema_attempt
            attempt_hash = _attempt_request_hash(
                request_hash,
                phase=phase,
                messages=active_messages,
                attempt_index=attempt_index,
            )
            try:
                result = _generate(
                    self._service,
                    active_messages,
                    page_crop=page_crop,
                )
            except ProviderExecutionError as error:
                attempts.append(
                    AdjudicationAttempt(
                        attempt_index=attempt_index,
                        phase=phase,
                        request_hash=attempt_hash,
                        status="provider_rejected",
                        validation_codes=(error.code,),
                        provider_retry_count=error.retry_count,
                        provider_repair_count=error.repair_count,
                    )
                )
                return _VotePhaseResult(
                    choice=None,
                    attempts=tuple(attempts),
                    validation_codes=(error.code,),
                    retry_count=error.retry_count,
                    repair_count=error.repair_count,
                )

            choice = CandidateChoice.model_validate(result.structured)
            if choice.candidate_id not in allowlist:
                validation_codes.append("LLM_CANDIDATE_UNKNOWN")
                attempts.append(
                    AdjudicationAttempt(
                        attempt_index=attempt_index,
                        phase=phase,
                        request_hash=attempt_hash,
                        confidence=choice.confidence,
                        status="candidate_unknown",
                        validation_codes=("LLM_CANDIDATE_UNKNOWN",),
                        provider_retry_count=result.metadata.retry_count,
                        provider_repair_count=result.metadata.repair_count,
                    )
                )
                if schema_attempt + 1 < self.policy.max_schema_attempts:
                    continue
                return _VotePhaseResult(
                    choice=None,
                    attempts=tuple(attempts),
                    validation_codes=tuple(validation_codes),
                    retry_count=result.metadata.retry_count,
                    repair_count=result.metadata.repair_count,
                )

            low_confidence = choice.confidence < self.policy.minimum_model_confidence
            attempts.append(
                AdjudicationAttempt(
                    attempt_index=attempt_index,
                    phase=phase,
                    request_hash=attempt_hash,
                    candidate_id=choice.candidate_id,
                    confidence=choice.confidence,
                    status="low_confidence" if low_confidence else "accepted",
                    validation_codes=("LLM_CONFIDENCE_TOO_LOW",) if low_confidence else (),
                    provider_retry_count=result.metadata.retry_count,
                    provider_repair_count=result.metadata.repair_count,
                )
            )
            return _VotePhaseResult(
                choice=choice,
                attempts=tuple(attempts),
                validation_codes=("LLM_CONFIDENCE_TOO_LOW",) if low_confidence else (),
                retry_count=result.metadata.retry_count,
                repair_count=result.metadata.repair_count,
            )
        raise AssertionError("unreachable")


def _generate(
    service: LLMService,
    messages: list[dict[str, str]],
    *,
    page_crop: bytes | None,
):
    if page_crop is None:
        return service.generate_structured(
            schema=candidate_choice_schema(),
            messages=messages,
        )
    prompt = "\n".join(message["content"] for message in messages)
    return service.generate_multimodal_structured(
        schema=candidate_choice_schema(),
        messages=[
            LLMMultimodalMessage(
                role="user",
                content=(
                    LLMTextPart(text=prompt),
                    LLMImagePart(type="image", mime_type="image/png", data=page_crop),
                ),
            )
        ],
    )


def _messages(
    candidate_set: CandidateSet,
    candidates: tuple[Candidate, ...],
    *,
    phase: Literal["primary", "recovery", "tiebreak"],
    prior_votes: tuple[AdjudicationAttempt, ...],
) -> list[dict[str, str]]:
    instruction = {
        "task": "Select exactly one allowlisted candidate ID.",
        "decision_type": candidate_set.decision_type,
        "phase": phase,
        "rules": [
            "Do not transcribe or rewrite text.",
            "Return only candidate_id and confidence.",
        ],
    }
    if phase == "recovery":
        instruction["recovery_rules"] = [
            "Reassess the exact differences among candidates independently.",
            (
                "For spacing, consider Korean morphology, particles, compounds, "
                "punctuation, and identifiers."
            ),
            "Do not raise confidence merely because this is a recovery request.",
        ]
    elif phase == "tiebreak":
        instruction["recovery_rules"] = [
            "Prior valid votes disagree; make an independent selection.",
            "Do not prefer a candidate because it appeared in a prior vote.",
        ]
    request = {
        "phase": phase,
        "candidate_set_id": candidate_set.candidate_set_id,
        "region_id": candidate_set.region_id,
        "candidates": [_candidate_summary(candidate) for candidate in candidates],
        "prior_votes": [
            {
                "phase": attempt.phase,
                "candidate_id": attempt.candidate_id,
                "confidence": attempt.confidence,
                "validation_codes": attempt.validation_codes,
            }
            for attempt in prior_votes
            if attempt.candidate_id is not None
        ],
    }
    return [
        {"role": "system", "content": _json(instruction)},
        {"role": "user", "content": _json(request)},
    ]


def _corrective_messages(
    original: list[dict[str, str]],
    code: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": _json(
                {
                    "validation_code": code,
                    "instruction": "Choose only an ID present in the request allowlist.",
                }
            ),
        },
        *original,
    ]


def _candidate_summary(candidate: Candidate) -> dict[str, object]:
    summary: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "score": candidate.score,
        "features": dict(sorted(candidate.features.items())),
    }
    if isinstance(candidate, SpacingCandidate):
        summary["rendering"] = candidate.rendered_text
    elif isinstance(candidate, RecognitionCandidate):
        summary["rendering"] = candidate.text
        summary["engine"] = candidate.engine
    elif isinstance(candidate, BlockCandidate):
        summary["block_kind"] = candidate.block_kind
        summary["heading_level"] = candidate.heading_level
        summary["list_kind"] = candidate.list_kind
        summary["list_depth"] = candidate.list_depth
    elif isinstance(candidate, ReadingOrderCandidate):
        summary["region_order"] = candidate.region_ids
    elif isinstance(candidate, ContinuationCandidate):
        summary["continue_previous"] = candidate.continue_previous
    elif isinstance(candidate, TableCandidate):
        summary["row_count"] = candidate.row_count
        summary["column_count"] = candidate.column_count
        summary["cells"] = [
            {
                "row": [cell.start_row, cell.end_row],
                "column": [cell.start_column, cell.end_column],
                "column_header": cell.column_header,
                "has_text": bool(cell.atom_ids),
            }
            for cell in candidate.cells
        ]
        summary["cell_renderings"] = [
            {
                "cell_id": cell.cell_id,
                "rendering": cell.rendered_text[:96],
            }
            for cell in candidate.cells
            if cell.rendered_text
        ][:32]
    return summary


def _provider_identity(provider: LLMProvider | None) -> tuple[str, str]:
    if provider is None:
        return "none", "none"
    capabilities = provider.capabilities()
    return str(capabilities.get("provider", "unknown")), str(capabilities.get("model", "unknown"))


def _request_hash(
    candidate_set: CandidateSet,
    *,
    evidence_hash: Sha256,
    provider: str,
    model: str,
    policy: AdjudicationPolicy,
    page_crop: bytes | None,
) -> Sha256:
    return canonical_hash(
        {
            "source_hash": candidate_set.source_hash,
            "evidence_hash": evidence_hash,
            "region_id": candidate_set.region_id,
            "candidate_set_id": candidate_set.candidate_set_id,
            "prompt_hash": canonical_hash(PROMPT_VERSION),
            "schema_hash": canonical_hash(candidate_choice_schema()),
            "provider": provider,
            "model": model,
            "policy": policy.model_dump(mode="json"),
            "page_crop_hash": (
                hashlib.sha256(page_crop).hexdigest() if page_crop is not None else None
            ),
        }
    )


def _attempt_request_hash(
    base_request_hash: Sha256,
    *,
    phase: str,
    messages: list[dict[str, str]],
    attempt_index: int,
) -> Sha256:
    return canonical_hash(
        {
            "base_request_hash": base_request_hash,
            "phase": phase,
            "attempt_index": attempt_index,
            "messages_hash": canonical_hash(messages),
        }
    )


def _trusted_decision_matches(
    decision: DecisionRecord,
    *,
    candidate_set: CandidateSet,
    request_hash: Sha256,
    evidence_hash: Sha256,
    provider: str,
    model: str,
    allowlist: set[CandidateId],
    candidates: tuple[Candidate, ...],
    policy: AdjudicationPolicy,
) -> bool:
    generated_candidate = decision.generated_candidate
    selected_is_current = decision.selected_candidate_id in allowlist or (
        isinstance(generated_candidate, SpacingCandidate)
        and generated_candidate.candidate_id == decision.selected_candidate_id
        and generated_candidate.region_id == candidate_set.region_id
        and candidate_set.decision_type == "spacing"
        and all(
            isinstance(candidate, SpacingCandidate)
            and candidate.character_sequence == generated_candidate.character_sequence
            for candidate in candidate_set.candidates
        )
        and not spacing_defect_codes(generated_candidate)
    )
    if not (
        decision.request_hash == request_hash
        and decision.source_hash == candidate_set.source_hash
        and decision.evidence_hash == evidence_hash
        and decision.candidate_set_id == candidate_set.candidate_set_id
        and decision.region_id == candidate_set.region_id
        and decision.provider == provider
        and decision.model == model
        and decision.outcome == "selected"
        and selected_is_current
    ):
        return False
    if not _attempt_request_hashes_match(
        decision,
        candidate_set=candidate_set,
        candidates=candidates,
        request_hash=request_hash,
    ):
        return False
    if decision.recovery_status == "generated":
        return _generated_audit_matches(
            decision,
            allowlist=allowlist,
            policy=policy,
        ) and _decision_identity_matches(decision)
    if decision.recovery_status != "recovered":
        return (
            decision.consensus_method == "none"
            and decision.recovery_count == 0
            and _primary_audit_matches(
                decision,
                allowlist=allowlist,
                policy=policy,
            )
            and _decision_identity_matches(decision)
        )
    if not (
        decision.consensus_method in {"same_candidate", "two_of_three"}
        and decision.consensus_candidate_id == decision.selected_candidate_id
        and decision.consensus_candidate_id in allowlist
        and decision.attempts
    ):
        return False
    return _recovery_audit_matches(
        decision,
        allowlist=allowlist,
        policy=policy,
    ) and _decision_identity_matches(decision)


def _attempt_request_hashes_match(
    decision: DecisionRecord,
    *,
    candidate_set: CandidateSet,
    candidates: tuple[Candidate, ...],
    request_hash: Sha256,
) -> bool:
    if not decision.attempts:
        return True
    attempts = decision.attempts
    if tuple(attempt.attempt_index for attempt in attempts) != tuple(
        range(1, len(attempts) + 1)
    ):
        return False

    prior: tuple[AdjudicationAttempt, ...] = ()
    consumed = 0
    for phase in ("primary", "recovery", "tiebreak"):
        phase_attempts = tuple(attempt for attempt in attempts if attempt.phase == phase)
        if not phase_attempts:
            continue
        if attempts[consumed : consumed + len(phase_attempts)] != phase_attempts:
            return False
        base_messages = _messages(
            candidate_set,
            candidates,
            phase=phase,
            prior_votes=prior,
        )
        correction_code: str | None = None
        for attempt in phase_attempts:
            active_messages = (
                _corrective_messages(base_messages, correction_code)
                if correction_code is not None
                else base_messages
            )
            if attempt.request_hash != _attempt_request_hash(
                request_hash,
                phase=phase,
                messages=active_messages,
                attempt_index=attempt.attempt_index,
            ):
                return False
            correction_code = (
                attempt.validation_codes[-1]
                if attempt.status == "candidate_unknown" and attempt.validation_codes
                else None
            )
        consumed += len(phase_attempts)
        prior = (*prior, *phase_attempts)

    remaining = attempts[consumed:]
    if not remaining:
        return True
    if candidate_set.decision_type != "spacing" or not all(
        isinstance(candidate, SpacingCandidate) for candidate in candidates
    ):
        return False
    primary_vote = next(
        (
            attempt
            for attempt in reversed(prior)
            if attempt.phase == "primary" and attempt.candidate_id is not None
        ),
        None,
    )
    if primary_vote is None:
        return False
    anchor = next(
        (
            candidate
            for candidate in candidates
            if candidate.candidate_id == primary_vote.candidate_id
            and isinstance(candidate, SpacingCandidate)
        ),
        None,
    )
    if anchor is None or remaining[0].phase != "generation":
        return False
    generation_messages = spacing_generation_messages(
        candidate_set,
        tuple(candidate for candidate in candidates if isinstance(candidate, SpacingCandidate)),
        anchor,
    )
    generation = remaining[0]
    if generation.request_hash != _attempt_request_hash(
        request_hash,
        phase="generation",
        messages=generation_messages,
        attempt_index=generation.attempt_index,
    ):
        return False
    if len(remaining) == 1:
        return True
    generated = decision.generated_candidate
    verification = remaining[1]
    if generated is None or verification.phase != "verification" or len(remaining) != 2:
        return False
    verification_messages = spacing_verification_messages(
        candidate_set,
        tuple(candidate for candidate in candidates if isinstance(candidate, SpacingCandidate)),
        generated,
    )
    return verification.request_hash == _attempt_request_hash(
        request_hash,
        phase="verification",
        messages=verification_messages,
        attempt_index=verification.attempt_index,
    )


def _generated_audit_matches(
    decision: DecisionRecord,
    *,
    allowlist: set[CandidateId],
    policy: AdjudicationPolicy,
) -> bool:
    generated = decision.generated_candidate
    if not isinstance(generated, SpacingCandidate) or decision.source not in {
        "generated",
        "cache",
    }:
        return False
    if len(decision.attempts) < 3:
        return False
    primary_attempts = decision.attempts[:-2]
    primary = primary_attempts[-1]
    expected_primary_status = (
        "accepted"
        if primary.confidence >= policy.minimum_model_confidence
        else "low_confidence"
    )
    expected_primary_codes = (
        () if expected_primary_status == "accepted" else ("LLM_CONFIDENCE_TOO_LOW",)
    )
    if (
        any(attempt.phase != "primary" for attempt in primary_attempts)
        or any(attempt.status != "candidate_unknown" for attempt in primary_attempts[:-1])
        or primary.candidate_id not in allowlist
        or primary.status != expected_primary_status
        or primary.validation_codes != expected_primary_codes
    ):
        return False
    generation, verification = decision.attempts[-2:]
    if (
        generation.phase != "generation"
        or verification.phase != "verification"
        or generation.status != "accepted"
        or verification.status != "accepted"
        or generation.candidate_id != generated.candidate_id
        or verification.candidate_id != generated.candidate_id
        or generation.confidence < policy.minimum_model_confidence
        or verification.confidence < policy.minimum_model_confidence
        or generation.validation_codes
        or verification.validation_codes
    ):
        return False
    return (
        decision.confidence == min(generation.confidence, verification.confidence)
        and decision.validation_codes == ("LLM_SPACING_REPAIR_APPLIED",)
        and decision.retry_count
        == sum(attempt.provider_retry_count for attempt in decision.attempts)
        and decision.repair_count
        == sum(attempt.provider_repair_count for attempt in decision.attempts)
    )


def _primary_audit_matches(
    decision: DecisionRecord,
    *,
    allowlist: set[CandidateId],
    policy: AdjudicationPolicy,
) -> bool:
    if decision.source not in {"model", "cache"} or not decision.attempts:
        return False
    if any(attempt.phase != "primary" for attempt in decision.attempts):
        return False
    terminal = decision.attempts[-1]
    if any(attempt.status != "candidate_unknown" for attempt in decision.attempts[:-1]):
        return False
    return (
        terminal.status == "accepted"
        and terminal.candidate_id in allowlist
        and terminal.candidate_id == decision.selected_candidate_id
        and terminal.confidence >= policy.minimum_model_confidence
        and terminal.confidence == decision.confidence
        and not terminal.validation_codes
        and not decision.validation_codes
        and decision.retry_count
        == sum(attempt.provider_retry_count for attempt in decision.attempts)
        and decision.repair_count
        == sum(attempt.provider_repair_count for attempt in decision.attempts)
    )


def _recovery_audit_matches(
    decision: DecisionRecord,
    *,
    allowlist: set[CandidateId],
    policy: AdjudicationPolicy,
) -> bool:
    indexes = tuple(item.attempt_index for item in decision.attempts)
    if indexes != tuple(range(1, len(indexes) + 1)):
        return False
    if len({item.request_hash for item in decision.attempts}) != len(decision.attempts):
        return False
    expected_phases = (
        ("primary", "recovery")
        if decision.consensus_method == "same_candidate"
        else ("primary", "recovery", "tiebreak")
    )
    actual_phases = tuple(dict.fromkeys(item.phase for item in decision.attempts))
    if actual_phases != expected_phases:
        return False

    votes: dict[str, AdjudicationAttempt] = {}
    for phase in expected_phases:
        phase_attempts = tuple(item for item in decision.attempts if item.phase == phase)
        valid_votes = tuple(
            item for item in phase_attempts if item.status in {"accepted", "low_confidence"}
        )
        if len(valid_votes) != 1 or phase_attempts[-1] != valid_votes[0]:
            return False
        if any(item.status != "candidate_unknown" for item in phase_attempts[:-1]):
            return False
        vote = valid_votes[0]
        if vote.candidate_id not in allowlist:
            return False
        expected_status = (
            "accepted" if vote.confidence >= policy.minimum_model_confidence else "low_confidence"
        )
        if vote.status != expected_status:
            return False
        votes[phase] = vote

    primary = votes["primary"]
    recovery = votes["recovery"]
    if primary.status != "low_confidence" or recovery.status != "accepted":
        return False
    terminal = recovery
    if decision.consensus_method == "same_candidate":
        if primary.candidate_id != recovery.candidate_id or decision.recovery_count != 1:
            return False
    else:
        tiebreak = votes["tiebreak"]
        if (
            primary.candidate_id == recovery.candidate_id
            or tiebreak.status != "accepted"
            or decision.recovery_count != 2
        ):
            return False
        vote_counts = Counter((primary.candidate_id, recovery.candidate_id, tiebreak.candidate_id))
        majority_id, majority_count = vote_counts.most_common(1)[0]
        if majority_count < policy.consensus_votes_required or tiebreak.candidate_id != majority_id:
            return False
        terminal = tiebreak
    return (
        terminal.candidate_id == decision.consensus_candidate_id
        and terminal.confidence == decision.confidence
        and decision.validation_codes == ("LLM_LOW_CONFIDENCE_RECOVERED",)
        and decision.retry_count == sum(item.provider_retry_count for item in decision.attempts)
        and decision.repair_count == sum(item.provider_repair_count for item in decision.attempts)
    )


def _cached_record(trusted: DecisionRecord) -> DecisionRecord:
    cached = trusted.model_copy(update={"source": "cache"})
    return cached.model_copy(
        update={"decision_id": _decision_id(cached)}
    )


def _failed_vote_source(
    result: _VotePhaseResult,
) -> Literal["model", "provider"]:
    return (
        "provider"
        if result.attempts and result.attempts[-1].status == "provider_rejected"
        else "model"
    )


def _last_confidence(result: _VotePhaseResult) -> float:
    return result.attempts[-1].confidence if result.attempts else 0.0


def _record(
    candidate_set: CandidateSet,
    *,
    request_hash: Sha256,
    evidence_hash: Sha256,
    selected_candidate_id: CandidateId | None,
    outcome: Literal["selected", "review_required", "deferred_review"],
    source: Literal[
        "deterministic",
        "model",
        "recovered",
        "cache",
        "unavailable",
        "provider",
        "generated",
        "fallback",
    ],
    confidence: float,
    provider: str,
    model: str,
    validation_codes: tuple[str, ...] = (),
    retry_count: int = 0,
    repair_count: int = 0,
    recovery_status: Literal[
        "not_needed",
        "recovered",
        "review_required",
        "generated",
        "deferred_review",
    ] = "not_needed",
    attempts: tuple[AdjudicationAttempt, ...] = (),
    consensus_method: Literal["none", "same_candidate", "two_of_three"] = "none",
    consensus_candidate_id: CandidateId | None = None,
    recovery_count: int = 0,
    generated_candidate: SpacingCandidate | GeneratedSpacingSnapshot | None = None,
) -> DecisionRecord:
    if attempts:
        retry_count = sum(item.provider_retry_count for item in attempts)
        repair_count = sum(item.provider_repair_count for item in attempts)
    record = DecisionRecord(
        decision_id="decision_0000000000000000",
        request_hash=request_hash,
        source_hash=candidate_set.source_hash,
        evidence_hash=evidence_hash,
        candidate_set_id=candidate_set.candidate_set_id,
        region_id=candidate_set.region_id,
        decision_type=candidate_set.decision_type,
        selected_candidate_id=selected_candidate_id,
        outcome=outcome,
        source=source,
        confidence=confidence,
        provider=provider,
        model=model,
        validation_codes=validation_codes,
        retry_count=retry_count,
        repair_count=repair_count,
        recovery_status=recovery_status,
        attempts=attempts,
        consensus_method=consensus_method,
        consensus_candidate_id=consensus_candidate_id,
        recovery_count=recovery_count,
        generated_candidate=generated_candidate,
    )
    return record.model_copy(update={"decision_id": _decision_id(record)})


def _decision_identity_matches(decision: DecisionRecord) -> bool:
    return decision.decision_id == _decision_id(decision)


def _decision_id(decision: DecisionRecord) -> str:
    digest = canonical_hash(
        {
            "request_hash": decision.request_hash,
            "selected_candidate_id": decision.selected_candidate_id,
            "outcome": decision.outcome,
            "source": decision.source,
            "validation_codes": decision.validation_codes,
            "confidence": decision.confidence,
            "provider": decision.provider,
            "model": decision.model,
            "retry_count": decision.retry_count,
            "repair_count": decision.repair_count,
            "attempts": [item.model_dump(mode="json") for item in decision.attempts],
            "recovery_status": decision.recovery_status,
            "consensus_method": decision.consensus_method,
            "consensus_candidate_id": decision.consensus_candidate_id,
            "recovery_count": decision.recovery_count,
            "generated_candidate": _generated_candidate_identity_payload(
                decision.generated_candidate
            ),
        }
    )
    return f"decision_{digest[:16]}"


def _deferred_candidate_record(
    candidate_set: CandidateSet,
    *,
    request_hash: Sha256,
    evidence_hash: Sha256,
    provider: str,
    model: str,
    attempts: tuple[AdjudicationAttempt, ...],
    validation_codes: tuple[str, ...] = (),
) -> DecisionRecord:
    try:
        fallback = (
            fallback_spacing_candidate(candidate_set)
            if candidate_set.decision_type == "spacing"
            else max(candidate_set.candidates, key=lambda item: (item.score, item.candidate_id))
        )
    except ValueError as error:
        codes = tuple(dict.fromkeys((*validation_codes, str(error))))
        source: Literal["model", "provider", "unavailable"] = (
            "unavailable"
            if provider == "none"
            else "provider"
            if attempts and attempts[-1].status == "provider_rejected"
            else "model"
        )
        return _record(
            candidate_set,
            request_hash=request_hash,
            evidence_hash=evidence_hash,
            selected_candidate_id=None,
            outcome="review_required",
            source=source,
            confidence=0.0,
            provider=provider,
            model=model,
            validation_codes=codes,
            recovery_status="review_required",
            attempts=attempts,
        )
    deferred_code = (
        "LLM_SPACING_REPAIR_DEFERRED"
        if candidate_set.decision_type == "spacing"
        else "LLM_CANDIDATE_SELECTION_DEFERRED"
    )
    return _record(
        candidate_set,
        request_hash=request_hash,
        evidence_hash=evidence_hash,
        selected_candidate_id=fallback.candidate_id,
        outcome="deferred_review",
        source="fallback",
        confidence=fallback.score,
        provider=provider,
        model=model,
        validation_codes=tuple(
            dict.fromkeys((*validation_codes, deferred_code))
        ),
        recovery_status="deferred_review",
        attempts=attempts,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def diagnostic_decision_record(decision: DecisionRecord) -> DecisionRecord:
    generated = decision.generated_candidate
    if not isinstance(generated, SpacingCandidate):
        return decision
    snapshot = GeneratedSpacingSnapshot(
        candidate_id=generated.candidate_id,
        region_id=generated.region_id,
        atom_ids=generated.atom_ids,
        boundaries=generated.boundaries,
        score=generated.score,
        features=generated.features,
        rendered_text_hash=_text_hash(generated.rendered_text),
        character_sequence_hash=_text_hash(generated.character_sequence),
    )
    return decision.model_copy(update={"generated_candidate": snapshot})


def _materialize_generated_candidate(
    decision: DecisionRecord,
    *,
    candidate_set: CandidateSet,
) -> DecisionRecord | None:
    generated = decision.generated_candidate
    if generated is None or isinstance(generated, SpacingCandidate):
        return decision
    if candidate_set.decision_type != "spacing" or generated.region_id != candidate_set.region_id:
        return None
    spacing_candidates = tuple(
        candidate
        for candidate in candidate_set.candidates
        if isinstance(candidate, SpacingCandidate)
    )
    if not spacing_candidates:
        return None
    character_sequence = spacing_candidates[0].character_sequence
    if any(
        candidate.character_sequence != character_sequence
        for candidate in spacing_candidates
    ) or _text_hash(character_sequence) != generated.character_sequence_hash:
        return None
    rendered_text = _render_spacing_snapshot(character_sequence, generated.boundaries)
    if _text_hash(rendered_text) != generated.rendered_text_hash:
        return None
    try:
        materialized = make_spacing_candidate(
            region_id=generated.region_id,
            rendered_text=rendered_text,
            character_sequence=character_sequence,
            atom_ids=generated.atom_ids,
            source_whitespace=tuple(
                boundary.source_whitespace_atom_ids
                for boundary in generated.boundaries
            ),
            score=generated.score,
            features=generated.features,
        )
    except ValueError:
        return None
    if materialized.candidate_id != generated.candidate_id:
        return None
    return decision.model_copy(update={"generated_candidate": materialized})


def _generated_candidate_identity_payload(
    generated: SpacingCandidate | GeneratedSpacingSnapshot | None,
) -> dict[str, object] | None:
    if generated is None:
        return None
    if isinstance(generated, SpacingCandidate):
        rendered_text_hash = _text_hash(generated.rendered_text)
        character_sequence_hash = _text_hash(generated.character_sequence)
    else:
        rendered_text_hash = generated.rendered_text_hash
        character_sequence_hash = generated.character_sequence_hash
    return {
        "candidate_id": generated.candidate_id,
        "region_id": generated.region_id,
        "atom_ids": generated.atom_ids,
        "boundaries": [item.model_dump(mode="json") for item in generated.boundaries],
        "score": generated.score,
        "features": generated.features,
        "rendered_text_hash": rendered_text_hash,
        "character_sequence_hash": character_sequence_hash,
    }


def _render_spacing_snapshot(
    character_sequence: str,
    boundaries: tuple[SpacingBoundary, ...],
) -> str:
    rendered: list[str] = []
    for index, character in enumerate(character_sequence):
        rendered.append(character)
        if index >= len(boundaries):
            continue
        state = boundaries[index].state
        rendered.append("\n" if state == "hard_break" else " " if state == "space" else "")
    return "".join(rendered)


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
