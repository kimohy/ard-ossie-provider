"""Character-conserving Korean and multilingual whitespace candidates."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from ard_ossie.semantic.candidates import (
    CandidateSet,
    SpacingCandidate,
    make_candidate_set_id,
    make_spacing_candidate,
)
from ard_ossie.semantic.evidence import AtomId, EvidenceAtom, EvidenceDocument
from ard_ossie.semantic.layout import LayoutDocument, LayoutRegion

DEFAULT_KOREAN_TECH_TERMS = (
    "데이터",
    "메타데이터",
    "시맨틱",
    "모델",
    "캠페인",
    "마케팅",
    "개인정보",
    "매체명",
    "결과값",
    "임계값",
    "선집계",
)
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([%℃°,:;.!?)}\]])")
_SPACE_AFTER_OPEN = re.compile(r"([({\[])\s+")
_MULTIPLE_WHITESPACE = re.compile(r"[\t \f\v]+")


class KoreanSpacingScorer(Protocol):
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]: ...


class KiwiSpacingScorer:
    def __init__(self, *, user_terms: Iterable[str] = DEFAULT_KOREAN_TECH_TERMS) -> None:
        from kiwipiepy import Kiwi

        self._kiwi = Kiwi()
        self._kiwi.global_config.space_tolerance = 2
        for term in user_terms:
            if term and not any(character.isspace() for character in term):
                self._kiwi.add_user_word(term, "NNG", 3.0)

    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        dense = _without_whitespace(text)
        proposals = [
            self._kiwi.space(text, reset_whitespace=True),
            self._kiwi.space(dense, reset_whitespace=True),
        ]
        if len(line_chunks) > 1:
            proposals.append(self._kiwi.glue(list(line_chunks)))
        return tuple(dict.fromkeys(str(value).strip() for value in proposals if str(value).strip()))


def build_spacing_candidate_set(
    *,
    region: LayoutRegion,
    evidence: EvidenceDocument,
    layout: LayoutDocument,
    scorer: KoreanSpacingScorer,
) -> CandidateSet:
    if region.region_id not in {item.region_id for item in layout.regions}:
        raise ValueError("SPACING_REGION_UNKNOWN")
    catalog = {atom.atom_id: atom for atom in evidence.atoms}
    ordered = [catalog[atom_id] for atom_id in region.atom_ids]
    characters = [atom for atom in ordered if not atom.text.isspace()]
    if not characters:
        raise ValueError("SPACING_REGION_EMPTY")
    character_sequence = "".join(atom.text for atom in characters)
    atom_ids = tuple(atom.atom_id for atom in characters)
    source_whitespace = _source_whitespace(ordered, characters)
    source_text = _canonicalize_whitespace("".join(atom.text for atom in ordered))
    geometry_text = _geometry_text(characters, source_whitespace, catalog)
    rule_text = _punctuation_rules(source_text)
    dense_text = character_sequence
    line_chunks = tuple(
        "".join(catalog[atom_id].text for atom_id in line.atom_ids)
        for line in layout.lines
        if line.line_id in region.line_ids
    )

    proposal_features: dict[str, dict[str, float]] = {}
    _add_proposal(proposal_features, source_text, "source_spacing", 0.45)
    _add_proposal(proposal_features, geometry_text, "geometry", 0.62)
    _add_proposal(proposal_features, rule_text, "punctuation_rules", 0.70)
    _add_proposal(proposal_features, dense_text, "dense", 0.20)
    for proposal in scorer.propose(source_text, line_chunks):
        _add_proposal(proposal_features, _punctuation_rules(proposal), "kiwi", 0.88)

    candidates: list[SpacingCandidate] = []
    for rendered_text, features in proposal_features.items():
        if _without_whitespace(rendered_text) != character_sequence:
            continue
        score = min(0.99, max(features.values()) + 0.04 * (len(features) - 1))
        candidates.append(
            make_spacing_candidate(
                region_id=region.region_id,
                rendered_text=rendered_text,
                character_sequence=character_sequence,
                atom_ids=atom_ids,
                source_whitespace=source_whitespace,
                score=score,
                features=features,
            )
        )
    ordered_candidates = tuple(
        sorted(candidates, key=lambda item: (-item.score, item.candidate_id))[:5]
    )
    if not ordered_candidates:
        raise ValueError("SPACING_CANDIDATES_EMPTY")
    candidate_ids = tuple(item.candidate_id for item in ordered_candidates)
    return CandidateSet(
        candidate_set_id=make_candidate_set_id(
            evidence.source_hash,
            region.region_id,
            candidate_ids,
        ),
        source_hash=evidence.source_hash,
        region_id=region.region_id,
        decision_type="spacing",
        candidates=ordered_candidates,
    )


def _source_whitespace(
    ordered: list[EvidenceAtom],
    characters: list[EvidenceAtom],
) -> tuple[tuple[AtomId, ...], ...]:
    position = {atom.atom_id: index for index, atom in enumerate(ordered)}
    return tuple(
        tuple(
            atom.atom_id
            for atom in ordered[position[first.atom_id] + 1 : position[second.atom_id]]
            if atom.text.isspace()
        )
        for first, second in zip(characters, characters[1:], strict=False)
    )


def _geometry_text(
    characters: list[EvidenceAtom],
    source_whitespace: tuple[tuple[AtomId, ...], ...],
    catalog: dict[AtomId, EvidenceAtom],
) -> str:
    widths = [
        atom.bbox.right - atom.bbox.left
        for atom in characters
        if atom.bbox is not None and atom.bbox.right > atom.bbox.left
    ]
    typical_width = sorted(widths)[len(widths) // 2] if widths else 0.0
    result: list[str] = []
    for index, atom in enumerate(characters):
        result.append(atom.text)
        if index >= len(characters) - 1:
            continue
        following = characters[index + 1]
        source_gap = source_whitespace[index]
        has_line_break = any(catalog[item].kind == "line_break" for item in source_gap)
        geometric_gap = (
            following.bbox.left - atom.bbox.right
            if atom.bbox is not None and following.bbox is not None
            else 0.0
        )
        if has_line_break:
            result.append("\n")
        elif typical_width > 0 and geometric_gap >= typical_width * 0.60:
            result.append(" ")
    return "".join(result)


def _add_proposal(
    proposals: dict[str, dict[str, float]],
    text: str,
    feature: str,
    score: float,
) -> None:
    normalized = _canonicalize_whitespace(text).strip()
    if normalized:
        proposals.setdefault(normalized, {})[feature] = score


def _canonicalize_whitespace(value: str) -> str:
    lines = [_MULTIPLE_WHITESPACE.sub(" ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _punctuation_rules(value: str) -> str:
    value = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", value)
    return _SPACE_AFTER_OPEN.sub(r"\1", value)


def _without_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())
