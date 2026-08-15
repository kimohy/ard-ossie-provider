from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ard_ossie.llm.contracts import (
    LLMImagePart,
    LLMMetadata,
    LLMMultimodalMessage,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
)
from ard_ossie.semantic.adjudication import (
    AdjudicationAttempt,
    CandidateAdjudicator,
    DecisionRecord,
    candidate_choice_schema,
)
from ard_ossie.semantic.candidates import (
    CandidateSet,
    make_candidate_set_id,
    make_spacing_candidate,
)

SOURCE_HASH = "f" * 64


class RecordingProvider:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = iter(responses or [])
        self.calls: list[tuple[str, object]] = []

    def health_check(self) -> bool:
        return True

    def capabilities(self) -> dict[str, str]:
        return {
            "structured_output": "json_schema",
            "provider": "openai_compatible",
            "model": "semantic-judge",
        }

    def generate_text(self, *, messages: list[dict[str, str]]) -> LLMResult:
        raise AssertionError("text generation is not allowed")

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        self.calls.append(("structured", messages))
        return self._next()

    def generate_multimodal_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[LLMMultimodalMessage],
    ) -> LLMResult:
        self.calls.append(("multimodal", messages))
        return self._next()

    def _next(self) -> LLMResult:
        value = next(self.responses)
        if isinstance(value, ProviderExecutionError):
            raise value
        structured = value if isinstance(value, dict) else None
        text = json.dumps(value) if isinstance(value, dict) else str(value)
        return LLMResult(
            text=text,
            structured=structured,
            metadata=LLMMetadata(
                profile="semantic-judge",
                provider="openai_compatible",
                model="semantic-judge",
                elapsed_ms=1,
            ),
        )


def _spacing_set(*scores: float) -> CandidateSet:
    characters = "데이터시맨틱"
    atom_ids = tuple(f"atom_{index:016x}" for index in range(1, len(characters) + 1))
    source_whitespace = tuple(() for _ in range(len(atom_ids) - 1))
    renderings = ("데이터 시맨틱", "데이터시맨틱", "데이터 시맨 틱")
    feature_names = ("kiwi", "source_spacing", "alternate")
    candidates = tuple(
        make_spacing_candidate(
            region_id="region_0000000000000001",
            rendered_text=renderings[index],
            character_sequence=characters,
            atom_ids=atom_ids,
            source_whitespace=source_whitespace,
            score=score,
            features={feature_names[index]: score},
        )
        for index, score in enumerate(scores)
    )
    return CandidateSet(
        candidate_set_id=make_candidate_set_id(
            SOURCE_HASH,
            "region_0000000000000001",
            tuple(item.candidate_id for item in candidates),
        ),
        source_hash=SOURCE_HASH,
        region_id="region_0000000000000001",
        decision_type="spacing",
        candidates=candidates,
    )


def _legacy_decision_payload() -> dict[str, object]:
    return {
        "decision_id": "decision_0000000000000001",
        "request_hash": "1" * 64,
        "source_hash": SOURCE_HASH,
        "evidence_hash": "2" * 64,
        "candidate_set_id": "candidate_set_0000000000000001",
        "region_id": "region_0000000000000001",
        "decision_type": "spacing",
        "selected_candidate_id": "candidate_0000000000000001",
        "outcome": "selected",
        "source": "model",
        "confidence": 0.91,
        "provider": "openai_compatible",
        "model": "semantic-judge",
        "validation_codes": [],
        "retry_count": 0,
        "repair_count": 0,
    }


def test_decision_record_loads_legacy_payload_with_recovery_defaults() -> None:
    decision = DecisionRecord.model_validate(_legacy_decision_payload())

    assert decision.recovery_status == "not_needed"
    assert decision.attempts == ()
    assert decision.consensus_method == "none"
    assert decision.consensus_candidate_id is None
    assert decision.recovery_count == 0


def test_adjudication_attempt_is_closed_bounded_and_content_addressed() -> None:
    attempt = AdjudicationAttempt(
        attempt_index=1,
        phase="primary",
        request_hash="1" * 64,
        candidate_id="candidate_0000000000000001",
        confidence=0.70,
        status="low_confidence",
        validation_codes=("LLM_CONFIDENCE_TOO_LOW",),
        provider_retry_count=0,
        provider_repair_count=0,
    )

    assert attempt.model_dump(mode="json")["phase"] == "primary"
    with pytest.raises(ValidationError):
        AdjudicationAttempt.model_validate({**attempt.model_dump(mode="json"), "attempt_index": 7})


def test_clear_score_margin_selects_without_provider_call() -> None:
    provider = RecordingProvider()
    candidate_set = _spacing_set(0.94, 0.60)

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.source == "deterministic"
    assert decision.outcome == "selected"
    assert decision.selected_candidate_id == candidate_set.candidates[0].candidate_id
    assert provider.calls == []


def test_ambiguous_request_contains_bounded_candidate_text_but_no_raw_catalog() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected_id = candidate_set.candidates[1].candidate_id
    provider = RecordingProvider([{"candidate_id": selected_id, "confidence": 0.91}])

    decision = CandidateAdjudicator(provider).decide(candidate_set)
    request = provider.calls[0]
    payload = json.dumps(request[1], ensure_ascii=False, default=str)

    assert decision.source == "model"
    assert decision.selected_candidate_id == selected_id
    assert "데이터 시맨틱" in payload
    assert "atom_ids" not in payload
    assert "source_object" not in payload
    assert "original_page_catalog" not in payload


def test_unknown_candidate_is_retried_once_then_requires_review() -> None:
    invalid = {"candidate_id": "candidate_deadbeefdeadbeef", "confidence": 0.99}
    provider = RecordingProvider([invalid, invalid])

    decision = CandidateAdjudicator(provider).decide(_spacing_set(0.80, 0.75))

    assert decision.outcome == "review_required"
    assert decision.validation_codes == (
        "LLM_CANDIDATE_UNKNOWN",
        "LLM_CANDIDATE_UNKNOWN",
    )
    assert len(provider.calls) == 2


def test_trusted_decision_is_reused_only_when_every_request_hash_matches() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected_id = candidate_set.candidates[0].candidate_id
    first_provider = RecordingProvider([{"candidate_id": selected_id, "confidence": 0.91}])
    first = CandidateAdjudicator(first_provider).decide(candidate_set)
    reuse_provider = RecordingProvider()

    reused = CandidateAdjudicator(reuse_provider, trusted=(first,)).decide(candidate_set)

    assert reused.source == "cache"
    assert reused.selected_candidate_id == selected_id
    assert reuse_provider.calls == []

    mismatched = first.model_copy(update={"request_hash": "0" * 64})
    miss_provider = RecordingProvider([{"candidate_id": selected_id, "confidence": 0.92}])
    missed = CandidateAdjudicator(miss_provider, trusted=(mismatched,)).decide(candidate_set)
    assert missed.source == "model"
    assert len(miss_provider.calls) == 1

    mismatched_evidence = first.model_copy(update={"evidence_hash": "1" * 64})
    evidence_miss_provider = RecordingProvider([{"candidate_id": selected_id, "confidence": 0.93}])
    evidence_miss = CandidateAdjudicator(
        evidence_miss_provider,
        trusted=(mismatched_evidence,),
    ).decide(candidate_set)
    assert evidence_miss.source == "model"
    assert len(evidence_miss_provider.calls) == 1


def test_unavailable_provider_requires_review() -> None:
    unavailable = CandidateAdjudicator(None).decide(_spacing_set(0.80, 0.75))
    assert unavailable.outcome == "review_required"
    assert unavailable.validation_codes == ("LLM_PROVIDER_UNAVAILABLE",)


def test_low_confidence_is_recovered_when_second_vote_matches() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    provider = RecordingProvider(
        [
            {"candidate_id": selected, "confidence": 0.70},
            {"candidate_id": selected, "confidence": 0.92},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "selected"
    assert decision.source == "recovered"
    assert decision.selected_candidate_id == selected
    assert decision.recovery_status == "recovered"
    assert decision.consensus_method == "same_candidate"
    assert decision.recovery_count == 1
    assert [attempt.phase for attempt in decision.attempts] == ["primary", "recovery"]
    assert [attempt.confidence for attempt in decision.attempts] == [0.70, 0.92]
    assert decision.validation_codes == ("LLM_LOW_CONFIDENCE_RECOVERED",)
    assert len(provider.calls) == 2


def test_disagreement_uses_high_confidence_two_of_three_tiebreak() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    first, second = [item.candidate_id for item in candidate_set.candidates]
    provider = RecordingProvider(
        [
            {"candidate_id": first, "confidence": 0.70},
            {"candidate_id": second, "confidence": 0.91},
            {"candidate_id": first, "confidence": 0.93},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "selected"
    assert decision.selected_candidate_id == first
    assert decision.consensus_method == "two_of_three"
    assert decision.consensus_candidate_id == first
    assert decision.recovery_count == 2
    assert [item.phase for item in decision.attempts] == [
        "primary",
        "recovery",
        "tiebreak",
    ]


def test_second_low_confidence_vote_exhausts_recovery_without_tiebreak() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    provider = RecordingProvider(
        [
            {"candidate_id": selected, "confidence": 0.70},
            {"candidate_id": selected, "confidence": 0.74},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "review_required"
    assert decision.selected_candidate_id is None
    assert decision.recovery_status == "review_required"
    assert decision.validation_codes == ("LLM_CONFIDENCE_RECOVERY_EXHAUSTED",)
    assert len(provider.calls) == 2


def test_tiebreak_without_majority_requires_review() -> None:
    candidate_set = _spacing_set(0.80, 0.75, 0.74)
    first, second, third = [item.candidate_id for item in candidate_set.candidates]
    provider = RecordingProvider(
        [
            {"candidate_id": first, "confidence": 0.70},
            {"candidate_id": second, "confidence": 0.91},
            {"candidate_id": third, "confidence": 0.94},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "review_required"
    assert decision.validation_codes == ("LLM_CONSENSUS_NOT_REACHED",)
    assert len(provider.calls) == 3


def test_transient_provider_failure_propagates() -> None:
    failure = ProviderExecutionError(
        "LLM_PROVIDER_TIMEOUT",
        kind=ProviderFailureKind.TRANSIENT,
    )
    provider = RecordingProvider([failure, failure, failure])

    with pytest.raises(ProviderExecutionError, match="LLM_PROVIDER_TIMEOUT"):
        CandidateAdjudicator(provider, sleep=lambda _delay: None).decide(_spacing_set(0.80, 0.75))

    assert len(provider.calls) == 3


def test_schema_invalid_free_text_is_never_accepted() -> None:
    provider = RecordingProvider(["자유 텍스트", "여전히 텍스트", "마지막 텍스트"])

    decision = CandidateAdjudicator(provider).decide(_spacing_set(0.80, 0.75))

    assert decision.outcome == "review_required"
    assert decision.selected_candidate_id is None
    assert decision.validation_codes == ("LLM_INVALID_JSON",)


def test_optional_crop_uses_exactly_one_multimodal_image() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    provider = RecordingProvider(
        [
            {
                "candidate_id": candidate_set.candidates[0].candidate_id,
                "confidence": 0.90,
            }
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set, page_crop=b"png")

    assert decision.outcome == "selected"
    kind, messages = provider.calls[0]
    assert kind == "multimodal"
    images = [
        part for message in messages for part in message.content if isinstance(part, LLMImagePart)
    ]
    assert len(images) == 1


def test_candidate_choice_schema_is_closed_and_minimal() -> None:
    schema = candidate_choice_schema()

    assert schema["required"] == ["candidate_id", "confidence"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"candidate_id", "confidence"}
