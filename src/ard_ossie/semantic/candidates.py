"""Typed, bounded candidates for semantic PDF decisions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.models import Sha256
from ard_ossie.semantic.evidence import AtomId, HypothesisId, RegionId
from ard_ossie.semantic.models import ImmutableStrictModel

CandidateId = Annotated[str, StringConstraints(pattern=r"^candidate_[0-9a-f]{16}$")]
CandidateSetId = Annotated[
    str,
    StringConstraints(pattern=r"^candidate_set_[0-9a-f]{16}$"),
]


class SpacingBoundary(ImmutableStrictModel):
    left_atom_id: AtomId
    right_atom_id: AtomId
    state: Literal["none", "space", "hard_break"]
    source_whitespace_atom_ids: tuple[AtomId, ...] = ()


class SpacingCandidate(ImmutableStrictModel):
    kind: Literal["spacing"] = "spacing"
    candidate_id: CandidateId
    region_id: RegionId
    rendered_text: str
    character_sequence: str = Field(min_length=1)
    atom_ids: tuple[AtomId, ...] = Field(min_length=1)
    boundaries: tuple[SpacingBoundary, ...]
    score: float = Field(ge=0, le=1)
    features: dict[str, float]

    @model_validator(mode="after")
    def validate_spacing(self) -> SpacingCandidate:
        if any(character.isspace() for character in self.character_sequence):
            raise ValueError("CANDIDATE_CHARACTER_SEQUENCE_INVALID")
        if _without_whitespace(self.rendered_text) != self.character_sequence:
            raise ValueError("CANDIDATE_CHARACTER_CONSERVATION_FAILED")
        if len(self.atom_ids) != len(self.character_sequence) or len(self.atom_ids) != len(
            set(self.atom_ids)
        ):
            raise ValueError("CANDIDATE_ATOM_SEQUENCE_INVALID")
        if len(self.boundaries) != max(0, len(self.atom_ids) - 1):
            raise ValueError("CANDIDATE_BOUNDARY_SEQUENCE_INVALID")
        expected_pairs = tuple(zip(self.atom_ids, self.atom_ids[1:], strict=False))
        actual_pairs = tuple(
            (boundary.left_atom_id, boundary.right_atom_id) for boundary in self.boundaries
        )
        if actual_pairs != expected_pairs:
            raise ValueError("CANDIDATE_BOUNDARY_SEQUENCE_INVALID")
        if _render_boundaries(self.character_sequence, self.boundaries) != self.rendered_text:
            raise ValueError("CANDIDATE_BOUNDARY_RENDER_INVALID")
        return self


class RecognitionCandidate(ImmutableStrictModel):
    kind: Literal["recognition"] = "recognition"
    candidate_id: CandidateId
    region_id: RegionId
    hypothesis_id: HypothesisId
    text: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    features: dict[str, float]


class BlockCandidate(ImmutableStrictModel):
    kind: Literal["block"] = "block"
    candidate_id: CandidateId
    region_id: RegionId
    block_kind: Literal[
        "heading",
        "paragraph",
        "list_item",
        "table",
        "caption",
        "figure",
    ]
    atom_ids: tuple[AtomId, ...] = Field(min_length=1)
    heading_level: int | None = Field(default=None, ge=1, le=6)
    list_kind: Literal["ordered", "unordered"] | None = None
    list_depth: int | None = Field(default=None, ge=1, le=8)
    score: float = Field(ge=0, le=1)
    features: dict[str, float]

    @model_validator(mode="after")
    def validate_block_metadata(self) -> BlockCandidate:
        if len(self.atom_ids) != len(set(self.atom_ids)):
            raise ValueError("CANDIDATE_ATOM_SEQUENCE_INVALID")
        if (self.heading_level is not None) != (self.block_kind == "heading"):
            raise ValueError("CANDIDATE_HEADING_METADATA_INVALID")
        has_list_metadata = self.list_kind is not None and self.list_depth is not None
        if has_list_metadata != (self.block_kind == "list_item"):
            raise ValueError("CANDIDATE_LIST_METADATA_INVALID")
        return self


class ReadingOrderCandidate(ImmutableStrictModel):
    kind: Literal["reading_order"] = "reading_order"
    candidate_id: CandidateId
    region_id: RegionId
    region_ids: tuple[RegionId, ...] = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    features: dict[str, float]

    @model_validator(mode="after")
    def validate_region_order(self) -> ReadingOrderCandidate:
        if len(self.region_ids) != len(set(self.region_ids)):
            raise ValueError("CANDIDATE_READING_ORDER_DUPLICATE")
        return self


class ContinuationCandidate(ImmutableStrictModel):
    kind: Literal["continuation"] = "continuation"
    candidate_id: CandidateId
    region_id: RegionId
    previous_region_id: RegionId
    current_region_id: RegionId
    continue_previous: bool
    score: float = Field(ge=0, le=1)
    features: dict[str, float]

    @model_validator(mode="after")
    def validate_region_pair(self) -> ContinuationCandidate:
        if self.region_id != self.current_region_id:
            raise ValueError("CANDIDATE_CONTINUATION_SCOPE_INVALID")
        if self.previous_region_id == self.current_region_id:
            raise ValueError("CANDIDATE_CONTINUATION_PAIR_INVALID")
        return self


Candidate = Annotated[
    SpacingCandidate
    | RecognitionCandidate
    | BlockCandidate
    | ReadingOrderCandidate
    | ContinuationCandidate,
    Field(discriminator="kind"),
]


class CandidateSet(ImmutableStrictModel):
    candidate_set_id: CandidateSetId
    source_hash: Sha256
    region_id: RegionId
    decision_type: Literal[
        "spacing",
        "recognition",
        "block",
        "reading_order",
        "continuation",
    ]
    candidates: tuple[Candidate, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_candidates(self) -> CandidateSet:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("CANDIDATE_IDS_NOT_UNIQUE")
        if any(
            candidate.region_id != self.region_id or candidate.kind != self.decision_type
            for candidate in self.candidates
        ):
            raise ValueError("CANDIDATE_SET_SCOPE_INVALID")
        return self


def make_candidate_id(kind: str, region_id: RegionId, payload: object) -> str:
    digest = canonical_hash({"kind": kind, "region_id": region_id, "payload": payload})
    return f"candidate_{digest[:16]}"


def make_candidate_set_id(
    source_hash: str,
    region_id: RegionId,
    candidate_ids: tuple[CandidateId, ...],
) -> str:
    digest = canonical_hash(
        {
            "source_hash": source_hash,
            "region_id": region_id,
            "candidate_ids": candidate_ids,
        }
    )
    return f"candidate_set_{digest[:16]}"


def make_spacing_candidate(
    *,
    region_id: RegionId,
    rendered_text: str,
    character_sequence: str,
    atom_ids: tuple[AtomId, ...],
    source_whitespace: tuple[tuple[AtomId, ...], ...],
    score: float,
    features: dict[str, float],
) -> SpacingCandidate:
    boundaries = _boundaries_from_rendered(
        rendered_text,
        atom_ids,
        source_whitespace,
    )
    candidate_id = make_candidate_id(
        "spacing",
        region_id,
        {
            "rendered_text": rendered_text,
            "atom_ids": atom_ids,
            "boundaries": boundaries,
        },
    )
    return SpacingCandidate(
        candidate_id=candidate_id,
        region_id=region_id,
        rendered_text=rendered_text,
        character_sequence=character_sequence,
        atom_ids=atom_ids,
        boundaries=boundaries,
        score=score,
        features=features,
    )


def _boundaries_from_rendered(
    rendered_text: str,
    atom_ids: tuple[AtomId, ...],
    source_whitespace: tuple[tuple[AtomId, ...], ...],
) -> tuple[SpacingBoundary, ...]:
    characters = [character for character in rendered_text if not character.isspace()]
    gaps: list[str] = []
    seen_character = False
    current_gap = ""
    for character in rendered_text:
        if character.isspace():
            if seen_character:
                current_gap += character
            continue
        if seen_character:
            gaps.append(current_gap)
        current_gap = ""
        seen_character = True
    if len(characters) != len(atom_ids) or len(source_whitespace) != max(0, len(atom_ids) - 1):
        raise ValueError("CANDIDATE_BOUNDARY_SEQUENCE_INVALID")
    return tuple(
        SpacingBoundary(
            left_atom_id=atom_ids[index],
            right_atom_id=atom_ids[index + 1],
            state=("hard_break" if "\n" in gap else "space" if gap else "none"),
            source_whitespace_atom_ids=source_whitespace[index],
        )
        for index, gap in enumerate(gaps)
    )


def _render_boundaries(
    character_sequence: str,
    boundaries: tuple[SpacingBoundary, ...],
) -> str:
    result: list[str] = []
    for index, character in enumerate(character_sequence):
        result.append(character)
        if index < len(boundaries):
            state = boundaries[index].state
            result.append("\n" if state == "hard_break" else " " if state == "space" else "")
    return "".join(result)


def _without_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())
