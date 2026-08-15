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
    atom_ids = tuple(f"atom_{index + 1:016x}" for index in range(len(character_sequence)))
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


def _table_scoped_candidate(
    *,
    mutable_boundary_indexes: tuple[int, ...] = (2,),
) -> SpacingCandidate:
    return make_spacing_candidate(
        region_id=REGION_ID,
        rendered_text="AB\n가 나",
        character_sequence="AB가나",
        atom_ids=tuple(f"atom_{index + 1:016x}" for index in range(4)),
        source_whitespace=((), (), ()),
        score=0.70,
        features={"table_cell_composite": 1.0},
        mutable_boundary_indexes=mutable_boundary_indexes,
    )


def test_generated_table_spacing_changes_only_allowlisted_cell_boundaries() -> None:
    anchor = _table_scoped_candidate()

    generated = build_generated_candidate(anchor, "AB\n가나", confidence=0.92)

    assert generated.rendered_text == "AB\n가나"
    assert generated.mutable_boundary_indexes == (2,)
    with pytest.raises(ValueError, match="SPACING_REPAIR_IMMUTABLE_BOUNDARY_MISMATCH"):
        build_generated_candidate(anchor, "A B\n가나", confidence=0.92)
    with pytest.raises(ValueError, match="SPACING_REPAIR_HARD_LINE_BOUNDARY_MISMATCH"):
        build_generated_candidate(anchor, "AB 가나", confidence=0.92)


@pytest.mark.parametrize("indexes", [(-1,), (3,), (2, 2), (2, 1), (1,)])
def test_table_spacing_mutable_boundary_allowlist_is_bounded(
    indexes: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="CANDIDATE_MUTABLE"):
        _table_scoped_candidate(mutable_boundary_indexes=indexes)


def test_identifier_gap_is_a_defect_even_when_characters_are_conserved() -> None:
    candidate = _spacing_candidate("marketing _campaign")

    assert spacing_defect_codes(candidate) == ("IDENTIFIER_WHITESPACE_SPLIT",)


@pytest.mark.parametrize(
    "rendered_text",
    [
        "marketing_campaign.ca mpaign_id",
        "marketing_creative.crea tive_id",
        "marketing_delivery.spe nd_units",
    ],
)
def test_qualified_identifier_fragment_is_a_deterministic_defect(
    rendered_text: str,
) -> None:
    candidate = _spacing_candidate(rendered_text)

    assert "QUALIFIED_IDENTIFIER_WHITESPACE_SPLIT" in spacing_defect_codes(candidate)


@pytest.mark.parametrize(
    ("source", "damaged"),
    [
        ("user@example.com 안내", "user@ example.com 안내"),
        ("https://example.com 경로", "https:// example.com 경로"),
        ("2026-08-15 기준", "2026- 08-15 기준"),
        ("AB12CD 상태", "AB12 CD 상태"),
        ("10kg 제한", "10 kg 제한"),
    ],
)
def test_generated_candidate_rejects_whitespace_inside_protected_tokens(
    source: str,
    damaged: str,
) -> None:
    anchor = _spacing_candidate(source)

    with pytest.raises(ValueError, match="SPACING_REPAIR_PROTECTED_TOKEN_SPLIT"):
        build_generated_candidate(anchor, damaged, confidence=0.91)


@pytest.mark.parametrize(
    ("source", "damaged"),
    [
        ("AB12CD and AB12CD", "AB12CD and AB12 CD"),
        ("10kg and 10kg", "10kg and 10 kg"),
    ],
)
def test_generated_candidate_rejects_one_split_occurrence_of_a_repeated_token(
    source: str,
    damaged: str,
) -> None:
    anchor = _spacing_candidate(source)

    with pytest.raises(ValueError, match="SPACING_REPAIR_PROTECTED_TOKEN_SPLIT"):
        build_generated_candidate(anchor, damaged, confidence=0.91)


@pytest.mark.parametrize("source", ["AB12CD and AB12CD", "10kg and 10kg"])
def test_generated_candidate_accepts_all_intact_occurrences_of_a_repeated_token(
    source: str,
) -> None:
    anchor = _spacing_candidate(source)

    generated = build_generated_candidate(anchor, source, confidence=0.91)

    assert generated.rendered_text == source


@pytest.mark.parametrize(
    ("source", "damaged"),
    [
        ("AB12 XAB12Y", "AB 12 XAB12Y"),
        ("10kg X10kgY", "10 kg X10kgY"),
    ],
)
def test_generated_candidate_does_not_count_token_substrings_as_intact_occurrences(
    source: str,
    damaged: str,
) -> None:
    anchor = _spacing_candidate(source)

    with pytest.raises(ValueError, match="SPACING_REPAIR_PROTECTED_TOKEN_SPLIT"):
        build_generated_candidate(anchor, damaged, confidence=0.91)


def test_protected_token_gap_is_a_deterministic_candidate_defect() -> None:
    candidate = _spacing_candidate("user@ example.com")

    assert "PROTECTED_TOKEN_WHITESPACE_SPLIT" in spacing_defect_codes(candidate)


def test_hard_cell_boundary_before_punctuation_is_not_a_spacing_defect() -> None:
    candidate = _spacing_candidate("SUM(value)\n%")

    assert "PUNCTUATION_WHITESPACE_BEFORE" not in spacing_defect_codes(candidate)


@pytest.mark.parametrize(
    "rendered_text",
    [
        "email user@example.com now",
        "visit https://example.com now",
        "due 2026-08-15 today",
        "code AB12CD status",
        "limit 10kg total",
        "period 2026 08 summary",
        "status CODE AB12 pending",
        "version X12 BETA",
        "model AB12 PRO",
        "code AB12 STATUS",
        "invoice A1 PAID",
        "measurement 10 kg total",
    ],
)
def test_normal_context_whitespace_around_protected_tokens_is_not_a_defect(
    rendered_text: str,
) -> None:
    candidate = _spacing_candidate(rendered_text)

    assert "PROTECTED_TOKEN_WHITESPACE_SPLIT" not in spacing_defect_codes(candidate)


@pytest.mark.parametrize(
    ("source", "dense"),
    [
        ("email user@example.com now", "emailuser@example.comnow"),
        ("visit https://example.com now", "visithttps://example.comnow"),
        ("due 2026-08-15 today", "due2026-08-15today"),
        ("code AB12CD status", "codeAB12CDstatus"),
        ("limit 10kg total", "limit10kgtotal"),
    ],
)
def test_fallback_preserves_valid_context_whitespace_around_protected_tokens(
    source: str,
    dense: str,
) -> None:
    source_candidate = _spacing_candidate(
        source,
        feature="source_spacing",
        score=0.45,
    )
    dense_candidate = _spacing_candidate(dense, feature="dense", score=0.88)

    fallback = fallback_spacing_candidate(_candidate_set(source_candidate, dense_candidate))

    assert fallback.candidate_id == source_candidate.candidate_id


@pytest.mark.parametrize(
    "source",
    [
        "version X12 BETA",
        "model AB12 PRO",
        "code AB12 STATUS",
        "invoice A1 PAID",
    ],
)
def test_fallback_does_not_flatten_identifier_followed_by_uppercase_word(
    source: str,
) -> None:
    source_candidate = _spacing_candidate(
        source,
        feature="source_spacing",
        score=0.45,
    )
    dense_candidate = _spacing_candidate(
        source.replace(" ", ""),
        feature="dense",
        score=0.88,
    )

    fallback = fallback_spacing_candidate(_candidate_set(source_candidate, dense_candidate))

    assert fallback.candidate_id == source_candidate.candidate_id


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


def test_fallback_rejects_defective_source_when_a_valid_candidate_exists() -> None:
    source = _spacing_candidate(
        "marketing _campaign",
        feature="source_spacing",
        score=0.45,
    )
    dense = _spacing_candidate("marketing_campaign", feature="dense", score=0.20)

    fallback = fallback_spacing_candidate(_candidate_set(source, dense))

    assert fallback.candidate_id == dense.candidate_id


def test_fallback_is_unavailable_when_every_candidate_has_a_deterministic_defect() -> None:
    before = _spacing_candidate(
        "marketing _campaign",
        feature="source_spacing",
        score=0.45,
    )
    after = _spacing_candidate("marketing_ campaign", feature="alternate", score=0.40)

    with pytest.raises(ValueError, match="SPACING_REPAIR_SAFE_FALLBACK_UNAVAILABLE"):
        fallback_spacing_candidate(_candidate_set(before, after))
