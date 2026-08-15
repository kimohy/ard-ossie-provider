from __future__ import annotations

import pytest
from pydantic import ValidationError

from ard_ossie.semantic.candidates import (
    CandidateSet,
    SpacingBoundary,
    SpacingCandidate,
)

SOURCE_HASH = "d" * 64
REGION_ID = "region_3000000000000001"


def spacing_candidate(
    authoritative: str,
    rendered: str,
    *,
    candidate_id: str = "candidate_3000000000000001",
    score: float = 0.8,
) -> SpacingCandidate:
    atom_ids = tuple(f"atom_{index + 1:016x}" for index in range(len(authoritative)))
    boundaries = tuple(
        SpacingBoundary(
            left_atom_id=atom_ids[index],
            right_atom_id=atom_ids[index + 1],
            state="none",
        )
        for index in range(len(atom_ids) - 1)
    )
    return SpacingCandidate(
        candidate_id=candidate_id,
        region_id=REGION_ID,
        rendered_text=rendered,
        character_sequence=authoritative,
        atom_ids=atom_ids,
        boundaries=boundaries,
        score=score,
        features={"fixture": score},
    )


def test_spacing_candidate_rejects_non_whitespace_character_change() -> None:
    with pytest.raises(ValidationError, match="CANDIDATE_CHARACTER_CONSERVATION_FAILED"):
        spacing_candidate("데이터시맨틱모델", "데이터 의미 모델")


def test_spacing_candidate_rejects_boundary_that_skips_an_atom() -> None:
    candidate = spacing_candidate("가나다", "가나다")
    invalid = candidate.model_dump()
    invalid["boundaries"][0]["right_atom_id"] = invalid["atom_ids"][2]

    with pytest.raises(ValidationError, match="CANDIDATE_BOUNDARY_SEQUENCE_INVALID"):
        SpacingCandidate.model_validate(invalid)


def test_candidate_set_rejects_more_than_five_candidates() -> None:
    candidates = tuple(
        spacing_candidate(
            "가나",
            "가나",
            candidate_id=f"candidate_{index + 1:016x}",
            score=0.9 - index / 100,
        )
        for index in range(6)
    )

    with pytest.raises(ValidationError):
        CandidateSet(
            candidate_set_id="candidate_set_3000000000000001",
            source_hash=SOURCE_HASH,
            region_id=REGION_ID,
            decision_type="spacing",
            candidates=candidates,
        )
