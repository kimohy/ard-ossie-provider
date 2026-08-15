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
    ProviderFailureKind,
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
    SpacingCandidate,
    TableCandidate,
)
from ard_ossie.semantic.evidence import RegionId
from ard_ossie.semantic.models import ImmutableStrictModel

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
    phase: Literal["primary", "recovery", "tiebreak"]
    request_hash: Sha256
    candidate_id: CandidateId | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    status: Literal[
        "accepted",
        "low_confidence",
        "candidate_unknown",
        "provider_rejected",
    ]
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)
    provider_retry_count: int = Field(default=0, ge=0, le=2)
    provider_repair_count: int = Field(default=0, ge=0, le=2)


class DecisionRecord(ImmutableStrictModel):
    decision_id: DecisionId
    request_hash: Sha256
    source_hash: Sha256
    evidence_hash: Sha256
    candidate_set_id: CandidateSetId
    region_id: RegionId
    decision_type: str = Field(min_length=1, max_length=40)
    selected_candidate_id: CandidateId | None = None
    outcome: Literal["selected", "review_required"]
    source: Literal[
        "deterministic",
        "model",
        "recovered",
        "cache",
        "unavailable",
        "provider",
    ]
    confidence: float = Field(ge=0, le=1)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)
    retry_count: int = Field(default=0, ge=0, le=2)
    repair_count: int = Field(default=0, ge=0, le=2)
    recovery_status: Literal["not_needed", "recovered", "review_required"] = "not_needed"
    attempts: tuple[AdjudicationAttempt, ...] = Field(default=(), max_length=6)
    consensus_method: Literal["none", "same_candidate", "two_of_three"] = "none"
    consensus_candidate_id: CandidateId | None = None
    recovery_count: int = Field(default=0, ge=0, le=2)


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
            page_crop=page_crop,
        )

        best = candidates[0]
        runner_score = candidates[1].score if len(candidates) > 1 else 0.0
        if (
            best.score >= self.policy.auto_accept_score
            and best.score - runner_score >= self.policy.auto_accept_margin
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
                item
                for item in self.trusted
                if item.request_hash == request_hash
                and item.source_hash == candidate_set.source_hash
                and item.evidence_hash == resolved_evidence_hash
                and item.candidate_set_id == candidate_set.candidate_set_id
                and item.region_id == candidate_set.region_id
                and item.provider == provider_name
                and item.model == model
                and item.outcome == "selected"
                and item.selected_candidate_id in allowlist
            ),
            None,
        )
        if trusted is not None:
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=trusted.selected_candidate_id,
                outcome="selected",
                source="cache",
                confidence=trusted.confidence,
                provider=provider_name,
                model=model,
                retry_count=trusted.retry_count,
                repair_count=trusted.repair_count,
            )

        if self._service is None:
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source="unavailable",
                confidence=0.0,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_PROVIDER_UNAVAILABLE",),
            )

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
        if primary.choice is None:
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source=_failed_vote_source(primary),
                confidence=_last_confidence(primary),
                provider=provider_name,
                model=model,
                validation_codes=primary.validation_codes,
                retry_count=primary.retry_count,
                repair_count=primary.repair_count,
                attempts=primary.attempts,
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
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source="model",
                confidence=primary.choice.confidence,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONFIDENCE_TOO_LOW",),
                retry_count=primary.retry_count,
                repair_count=primary.repair_count,
                recovery_status="review_required",
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
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source=_failed_vote_source(recovery),
                confidence=_last_confidence(recovery),
                provider=provider_name,
                model=model,
                validation_codes=recovery.validation_codes,
                retry_count=recovery.retry_count,
                repair_count=recovery.repair_count,
                recovery_status="review_required",
                attempts=attempts,
                recovery_count=1,
            )
        if recovery.choice.confidence < self.policy.minimum_model_confidence:
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source="model",
                confidence=recovery.choice.confidence,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONFIDENCE_RECOVERY_EXHAUSTED",),
                retry_count=recovery.retry_count,
                repair_count=recovery.repair_count,
                recovery_status="review_required",
                attempts=attempts,
                recovery_count=1,
            )
        if recovery.choice.candidate_id != primary.choice.candidate_id:
            if self.policy.max_confidence_recovery_attempts < 2:
                return _record(
                    candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    selected_candidate_id=None,
                    outcome="review_required",
                    source="model",
                    confidence=recovery.choice.confidence,
                    provider=provider_name,
                    model=model,
                    validation_codes=("LLM_CONSENSUS_NOT_REACHED",),
                    retry_count=recovery.retry_count,
                    repair_count=recovery.repair_count,
                    recovery_status="review_required",
                    attempts=attempts,
                    recovery_count=1,
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
                return _record(
                    candidate_set,
                    request_hash=request_hash,
                    evidence_hash=resolved_evidence_hash,
                    selected_candidate_id=None,
                    outcome="review_required",
                    source=_failed_vote_source(tiebreak),
                    confidence=_last_confidence(tiebreak),
                    provider=provider_name,
                    model=model,
                    validation_codes=tiebreak.validation_codes,
                    retry_count=tiebreak.retry_count,
                    repair_count=tiebreak.repair_count,
                    recovery_status="review_required",
                    attempts=all_attempts,
                    recovery_count=2,
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
            return _record(
                candidate_set,
                request_hash=request_hash,
                evidence_hash=resolved_evidence_hash,
                selected_candidate_id=None,
                outcome="review_required",
                source="model",
                confidence=tiebreak.choice.confidence,
                provider=provider_name,
                model=model,
                validation_codes=("LLM_CONSENSUS_NOT_REACHED",),
                retry_count=tiebreak.retry_count,
                repair_count=tiebreak.repair_count,
                recovery_status="review_required",
                attempts=all_attempts,
                recovery_count=2,
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
                if error.kind is not ProviderFailureKind.OUTPUT:
                    raise
                attempts.append(
                    AdjudicationAttempt(
                        attempt_index=attempt_index,
                        phase=phase,
                        request_hash=attempt_hash,
                        status="provider_rejected",
                        validation_codes=(error.code,),
                    )
                )
                return _VotePhaseResult(
                    choice=None,
                    attempts=tuple(attempts),
                    validation_codes=(error.code,),
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
    outcome: Literal["selected", "review_required"],
    source: Literal[
        "deterministic",
        "model",
        "recovered",
        "cache",
        "unavailable",
        "provider",
    ],
    confidence: float,
    provider: str,
    model: str,
    validation_codes: tuple[str, ...] = (),
    retry_count: int = 0,
    repair_count: int = 0,
    recovery_status: Literal["not_needed", "recovered", "review_required"] = "not_needed",
    attempts: tuple[AdjudicationAttempt, ...] = (),
    consensus_method: Literal["none", "same_candidate", "two_of_three"] = "none",
    consensus_candidate_id: CandidateId | None = None,
    recovery_count: int = 0,
) -> DecisionRecord:
    digest = canonical_hash(
        {
            "request_hash": request_hash,
            "selected_candidate_id": selected_candidate_id,
            "outcome": outcome,
            "source": source,
            "validation_codes": validation_codes,
            "attempt_request_hashes": [item.request_hash for item in attempts],
            "recovery_status": recovery_status,
            "consensus_method": consensus_method,
            "consensus_candidate_id": consensus_candidate_id,
        }
    )
    return DecisionRecord(
        decision_id=f"decision_{digest[:16]}",
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
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
