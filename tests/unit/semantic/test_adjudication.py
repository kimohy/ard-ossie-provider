from __future__ import annotations

import json

import pytest

from ard_ossie.llm.contracts import (
    LLMImagePart,
    LLMMetadata,
    LLMMultimodalMessage,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
)
from ard_ossie.semantic.adjudication import (
    CandidateAdjudicator,
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


def _spacing_set(first_score: float, second_score: float) -> CandidateSet:
    characters = "데이터시맨틱"
    atom_ids = tuple(f"atom_{index:016x}" for index in range(1, len(characters) + 1))
    source_whitespace = tuple(() for _ in range(len(atom_ids) - 1))
    candidates = (
        make_spacing_candidate(
            region_id="region_0000000000000001",
            rendered_text="데이터 시맨틱",
            character_sequence=characters,
            atom_ids=atom_ids,
            source_whitespace=source_whitespace,
            score=first_score,
            features={"kiwi": first_score},
        ),
        make_spacing_candidate(
            region_id="region_0000000000000001",
            rendered_text="데이터시맨틱",
            character_sequence=characters,
            atom_ids=atom_ids,
            source_whitespace=source_whitespace,
            score=second_score,
            features={"source_spacing": second_score},
        ),
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
    first_provider = RecordingProvider(
        [{"candidate_id": selected_id, "confidence": 0.91}]
    )
    first = CandidateAdjudicator(first_provider).decide(candidate_set)
    reuse_provider = RecordingProvider()

    reused = CandidateAdjudicator(reuse_provider, trusted=(first,)).decide(candidate_set)

    assert reused.source == "cache"
    assert reused.selected_candidate_id == selected_id
    assert reuse_provider.calls == []

    mismatched = first.model_copy(update={"request_hash": "0" * 64})
    miss_provider = RecordingProvider(
        [{"candidate_id": selected_id, "confidence": 0.92}]
    )
    missed = CandidateAdjudicator(miss_provider, trusted=(mismatched,)).decide(candidate_set)
    assert missed.source == "model"
    assert len(miss_provider.calls) == 1

    mismatched_evidence = first.model_copy(update={"evidence_hash": "1" * 64})
    evidence_miss_provider = RecordingProvider(
        [{"candidate_id": selected_id, "confidence": 0.93}]
    )
    evidence_miss = CandidateAdjudicator(
        evidence_miss_provider,
        trusted=(mismatched_evidence,),
    ).decide(candidate_set)
    assert evidence_miss.source == "model"
    assert len(evidence_miss_provider.calls) == 1


def test_unavailable_provider_and_low_confidence_response_require_review() -> None:
    unavailable = CandidateAdjudicator(None).decide(_spacing_set(0.80, 0.75))
    assert unavailable.outcome == "review_required"
    assert unavailable.validation_codes == ("LLM_PROVIDER_UNAVAILABLE",)

    candidate_set = _spacing_set(0.80, 0.75)
    provider = RecordingProvider(
        [
            {
                "candidate_id": candidate_set.candidates[0].candidate_id,
                "confidence": 0.40,
            }
        ]
    )
    uncertain = CandidateAdjudicator(provider).decide(candidate_set)
    assert uncertain.outcome == "review_required"
    assert uncertain.validation_codes == ("LLM_CONFIDENCE_TOO_LOW",)
    assert len(provider.calls) == 1


def test_transient_provider_failure_propagates() -> None:
    failure = ProviderExecutionError(
        "LLM_PROVIDER_TIMEOUT",
        kind=ProviderFailureKind.TRANSIENT,
    )
    provider = RecordingProvider([failure, failure, failure])

    with pytest.raises(ProviderExecutionError, match="LLM_PROVIDER_TIMEOUT"):
        CandidateAdjudicator(provider, sleep=lambda _delay: None).decide(
            _spacing_set(0.80, 0.75)
        )

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
        part
        for message in messages
        for part in message.content
        if isinstance(part, LLMImagePart)
    ]
    assert len(images) == 1


def test_candidate_choice_schema_is_closed_and_minimal() -> None:
    schema = candidate_choice_schema()

    assert schema["required"] == ["candidate_id", "confidence"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"candidate_id", "confidence"}
