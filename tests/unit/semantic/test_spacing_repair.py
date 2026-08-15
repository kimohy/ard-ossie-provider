from __future__ import annotations

import pytest

from ard_ossie.semantic.candidates import (
    CandidateSet,
    SpacingCandidate,
    make_candidate_set_id,
    make_spacing_candidate,
)
from ard_ossie.semantic.spacing_repair import (
    build_generated_candidate,
    fallback_spacing_candidate,
    spacing_defect_codes,
)

SOURCE_HASH = "f" * 64
REGION_ID = "region_5000000000000001"


def _spacing_candidate(
    rendered_text: str,
    *,
    feature: str = "kiwi",
    score: float = 0.88,
) -> SpacingCandidate:
    character_sequence = "".join(
        character for character in rendered_text if not character.isspace()
    )
    atom_ids = tuple(
        f"atom_{index + 1:016x}" for index in range(len(character_sequence))
    )
    return make_spacing_candidate(
        region_id=REGION_ID,
        rendered_text=rendered_text,
        character_sequence=character_sequence,
        atom_ids=atom_ids,
        source_whitespace=tuple(() for _ in range(max(0, len(atom_ids) - 1))),
        score=score,
        features={feature: score},
    )


def _candidate_set(*candidates: SpacingCandidate) -> CandidateSet:
    return CandidateSet(
        candidate_set_id=make_candidate_set_id(
            SOURCE_HASH,
            REGION_ID,
            tuple(candidate.candidate_id for candidate in candidates),
        ),
        source_hash=SOURCE_HASH,
        region_id=REGION_ID,
        decision_type="spacing",
        candidates=candidates,
    )


def test_identifier_gap_is_a_defect_even_when_characters_are_conserved() -> None:
    candidate = _spacing_candidate("marketing _campaign")

    assert spacing_defect_codes(candidate) == ("IDENTIFIER_WHITESPACE_SPLIT",)


def test_generated_candidate_rejects_character_mutation() -> None:
    anchor = _spacing_candidate("마케팅 캠페인")

    with pytest.raises(ValueError, match="SPACING_REPAIR_CHARACTER_MISMATCH"):
        build_generated_candidate(anchor, "마케팅 캠페인!", confidence=0.91)


def test_generated_candidate_preserves_hard_line_boundaries() -> None:
    anchor = _spacing_candidate("첫째 줄\n둘째 줄")

    with pytest.raises(ValueError, match="SPACING_REPAIR_HARD_LINE_BOUNDARY_MISMATCH"):
        build_generated_candidate(anchor, "첫째 줄 둘째 줄", confidence=0.91)


def test_fallback_prefers_valid_source_spacing() -> None:
    language = _spacing_candidate("마케팅캠페인", score=0.88)
    source = _spacing_candidate("마케팅 캠페인", feature="source_spacing", score=0.45)
    candidate_set = _candidate_set(language, source)

    fallback = fallback_spacing_candidate(candidate_set)

    assert fallback.candidate_id == source.candidate_id
    assert fallback.features["source_spacing"] == 0.45
