"""Bounded generation and verification contracts for whitespace-only repair."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from ard_ossie.semantic.candidates import (
    CandidateId,
    CandidateSet,
    SpacingCandidate,
    make_spacing_candidate,
)
from ard_ossie.semantic.models import ImmutableStrictModel

ValidationCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]

GENERATION_SYSTEM_CONTRACT = """You repair whitespace only. Return one JSON object matching the
schema.
Preserve the exact non-whitespace Unicode sequence and every hard line boundary. Never add, remove,
replace, or reorder a character. Keep identifiers containing underscores contiguous. Do not flatten
or reinterpret tables. Use Korean morphology, punctuation, and line context only to place spaces."""

VERIFICATION_SYSTEM_CONTRACT = """You independently verify whitespace candidates. Return one JSON
object matching the schema and select only an offered candidate ID. Reject character mutation,
identifier splitting, punctuation defects, or changed hard line boundaries. Confidence measures
whether the selected rendering is correct, not whether its characters are conserved."""

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")
_IDENTIFIER_WHITESPACE = re.compile(r"(?:\s_|_\s)")
_PROTECTED_TOKEN_PATTERNS = (
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}"),
    re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"),
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
    _IDENTIFIER,
    re.compile(
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]{4,})(?=[A-Za-z0-9-]*[A-Za-z])"
        r"(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?"
        r"(?:%|℃|°[CF]|kg|mg|g|km|cm|mm|m|ms|s|h|Hz|KB|MB|GB|TB)"
        r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
)
_LOOSE_PROTECTED_TOKEN_PATTERNS = (
    re.compile(
        r"[A-Za-z][A-Za-z0-9+.-]*\s*:\s*/\s*/\s*"
        r"[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+"
    ),
    re.compile(
        r"[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9-]+"
        r"(?:\s*\.\s*[A-Za-z0-9-]+)+"
    ),
    re.compile(r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"),
    re.compile(r"\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?"),
)
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+[%℃°,:;.!?)}\]]")
_SPACE_AFTER_OPEN = re.compile(r"[({\[]\s+")


class SpacingRepairProposal(ImmutableStrictModel):
    rendered_text: str
    confidence: float = Field(ge=0, le=1)
    repair_reasons: tuple[
        Literal[
            "korean_morphology",
            "identifier_integrity",
            "punctuation_boundary",
            "table_cell_boundary",
            "line_boundary",
        ],
        ...,
    ] = Field(max_length=5)


class SpacingVerification(ImmutableStrictModel):
    candidate_id: CandidateId
    confidence: float = Field(ge=0, le=1)
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)


def spacing_repair_schema() -> dict[str, object]:
    return SpacingRepairProposal.model_json_schema()


def spacing_verification_schema(candidate_ids: tuple[CandidateId, ...]) -> dict[str, object]:
    if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("SPACING_VERIFICATION_ALLOWLIST_INVALID")
    return {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "enum": list(candidate_ids)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "validation_codes": {
                "type": "array",
                "items": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]{0,127}$"},
                "maxItems": 4,
            },
        },
        "required": ["candidate_id", "confidence", "validation_codes"],
        "additionalProperties": False,
    }


def spacing_defect_codes(candidate: SpacingCandidate) -> tuple[ValidationCode, ...]:
    codes: list[str] = []
    if any(
        character.isspace() and character not in {" ", "\n"}
        for character in candidate.rendered_text
    ):
        codes.append("CONTROL_WHITESPACE_SEPARATOR")
    identifier_split = _IDENTIFIER_WHITESPACE.search(candidate.rendered_text) is not None
    if identifier_split:
        codes.append("IDENTIFIER_WHITESPACE_SPLIT")
    elif _has_protected_token_split(candidate.rendered_text):
        codes.append("PROTECTED_TOKEN_WHITESPACE_SPLIT")
    if _SPACE_BEFORE_PUNCTUATION.search(candidate.rendered_text):
        codes.append("PUNCTUATION_WHITESPACE_BEFORE")
    if _SPACE_AFTER_OPEN.search(candidate.rendered_text):
        codes.append("PUNCTUATION_WHITESPACE_AFTER_OPEN")
    return tuple(codes)


def build_generated_candidate(
    anchor: SpacingCandidate,
    rendered_text: str,
    confidence: float,
) -> SpacingCandidate:
    if _without_whitespace(rendered_text) != anchor.character_sequence:
        raise ValueError("SPACING_REPAIR_CHARACTER_MISMATCH")
    if any(character.isspace() and character not in {" ", "\n"} for character in rendered_text):
        raise ValueError("SPACING_REPAIR_CONTROL_SEPARATOR")
    if _IDENTIFIER_WHITESPACE.search(rendered_text):
        raise ValueError("SPACING_REPAIR_IDENTIFIER_SPLIT")
    if _has_protected_token_split(
        rendered_text,
        expected_tokens=_protected_token_occurrences(anchor.rendered_text),
    ):
        raise ValueError("SPACING_REPAIR_PROTECTED_TOKEN_SPLIT")
    generated = make_spacing_candidate(
        region_id=anchor.region_id,
        rendered_text=rendered_text,
        character_sequence=anchor.character_sequence,
        atom_ids=anchor.atom_ids,
        source_whitespace=tuple(
            boundary.source_whitespace_atom_ids for boundary in anchor.boundaries
        ),
        score=max(0.0, min(1.0, confidence)),
        features={"llm_generated_spacing": max(0.0, min(1.0, confidence))},
        mutable_boundary_indexes=anchor.mutable_boundary_indexes,
    )
    if _hard_break_pairs(generated) != _hard_break_pairs(anchor):
        raise ValueError("SPACING_REPAIR_HARD_LINE_BOUNDARY_MISMATCH")
    mutable = (
        set(range(len(anchor.boundaries)))
        if anchor.mutable_boundary_indexes is None
        else set(anchor.mutable_boundary_indexes)
    )
    if any(
        generated.boundaries[index].state != boundary.state
        for index, boundary in enumerate(anchor.boundaries)
        if index not in mutable
    ):
        raise ValueError("SPACING_REPAIR_IMMUTABLE_BOUNDARY_MISMATCH")
    return generated


def fallback_spacing_candidate(candidate_set: CandidateSet) -> SpacingCandidate:
    candidates = tuple(
        candidate
        for candidate in candidate_set.candidates
        if isinstance(candidate, SpacingCandidate)
    )
    if candidate_set.decision_type != "spacing" or not candidates:
        raise ValueError("SPACING_REPAIR_FALLBACK_UNAVAILABLE")
    valid = tuple(candidate for candidate in candidates if not spacing_defect_codes(candidate))
    if not valid:
        raise ValueError("SPACING_REPAIR_SAFE_FALLBACK_UNAVAILABLE")
    return max(
        valid,
        key=lambda candidate: (
            "source_spacing" in candidate.features,
            candidate.score,
            candidate.candidate_id,
        ),
    )


def spacing_generation_messages(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    anchor: SpacingCandidate,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": GENERATION_SYSTEM_CONTRACT},
        {"role": "user", "content": _json(_generation_request(candidate_set, candidates, anchor))},
    ]


def spacing_verification_messages(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    generated: SpacingCandidate,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VERIFICATION_SYSTEM_CONTRACT},
        {
            "role": "user",
            "content": _json(_verification_request(candidate_set, candidates, generated)),
        },
    ]


def _generation_request(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    anchor: SpacingCandidate,
) -> dict[str, object]:
    _validate_scope(candidate_set, candidates, anchor)
    anchor_states = tuple(boundary.state for boundary in anchor.boundaries)
    return {
        "task": "generate_whitespace_repair",
        "candidate_set_id": candidate_set.candidate_set_id,
        "region_id": candidate_set.region_id,
        "exact_character_sequence": anchor.character_sequence,
        "protected_identifiers": _protected_tokens(anchor.rendered_text),
        "hard_line_boundary_indexes": [
            index for index, state in enumerate(anchor_states) if state == "hard_break"
        ],
        "mutable_boundary_indexes": anchor.mutable_boundary_indexes,
        "anchor_candidate_id": anchor.candidate_id,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "rendered_text": candidate.rendered_text,
                "score": candidate.score,
                "defect_codes": spacing_defect_codes(candidate),
                "boundary_changes_from_anchor": [
                    {
                        "index": index,
                        "anchor": anchor_state,
                        "candidate": candidate.boundaries[index].state,
                    }
                    for index, anchor_state in enumerate(anchor_states)
                    if candidate.boundaries[index].state != anchor_state
                ],
            }
            for candidate in candidates
        ],
    }


def _verification_request(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    generated: SpacingCandidate,
) -> dict[str, object]:
    anchor = candidates[0]
    _validate_scope(candidate_set, candidates, anchor)
    if generated.region_id != candidate_set.region_id:
        raise ValueError("SPACING_REPAIR_SCOPE_MISMATCH")
    offered = tuple(
        {candidate.candidate_id: candidate for candidate in (*candidates, generated)}.values()
    )
    return {
        "task": "verify_whitespace_repair",
        "candidate_set_id": candidate_set.candidate_set_id,
        "region_id": candidate_set.region_id,
        "exact_character_sequence": generated.character_sequence,
        "protected_identifiers": _protected_tokens(generated.rendered_text),
        "mutable_boundary_indexes": generated.mutable_boundary_indexes,
        "generated_candidate_id": generated.candidate_id,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "rendered_text": candidate.rendered_text,
                "defect_codes": spacing_defect_codes(candidate),
                "generated": candidate.candidate_id == generated.candidate_id,
            }
            for candidate in offered
        ],
    }


def _validate_scope(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    anchor: SpacingCandidate,
) -> None:
    if candidate_set.decision_type != "spacing" or anchor.region_id != candidate_set.region_id:
        raise ValueError("SPACING_REPAIR_SCOPE_MISMATCH")
    if not candidates or any(
        candidate.region_id != candidate_set.region_id for candidate in candidates
    ):
        raise ValueError("SPACING_REPAIR_SCOPE_MISMATCH")
    if any(candidate.character_sequence != anchor.character_sequence for candidate in candidates):
        raise ValueError("SPACING_REPAIR_CHARACTER_MISMATCH")
    if any(
        candidate.mutable_boundary_indexes != anchor.mutable_boundary_indexes
        for candidate in candidates
    ):
        raise ValueError("SPACING_REPAIR_MUTABLE_SCOPE_MISMATCH")


def _hard_break_pairs(candidate: SpacingCandidate) -> tuple[tuple[str, str], ...]:
    return tuple(
        (boundary.left_atom_id, boundary.right_atom_id)
        for boundary in candidate.boundaries
        if boundary.state == "hard_break"
    )


def _without_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _protected_tokens(rendered_text: str) -> list[str]:
    return sorted(
        set(_protected_token_occurrences(rendered_text)),
        key=lambda token: (-len(token), token),
    )


def _protected_token_occurrences(rendered_text: str) -> list[str]:
    matches = {
        (match.start(), match.end(), match.group(0))
        for pattern in _PROTECTED_TOKEN_PATTERNS
        for match in pattern.finditer(rendered_text)
    }
    return [token for _, _, token in sorted(matches)]


def _has_protected_token_split(
    rendered_text: str,
    *,
    expected_tokens: list[str] | None = None,
) -> bool:
    if expected_tokens:
        observed_tokens = _protected_token_occurrences(rendered_text)
        if any(
            observed_tokens.count(token) < expected_tokens.count(token)
            for token in set(expected_tokens)
        ):
            return True
    return any(
        any(character.isspace() for character in match.group(0))
        for pattern in _LOOSE_PROTECTED_TOKEN_PATTERNS
        for match in pattern.finditer(rendered_text)
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
