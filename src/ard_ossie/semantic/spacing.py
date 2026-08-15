"""Character-conserving Korean and multilingual whitespace candidates."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from ard_ossie.semantic.candidates import (
    CandidateSet,
    SpacingCandidate,
    TableCandidate,
    make_candidate_set_id,
    make_spacing_candidate,
)
from ard_ossie.semantic.evidence import AtomId, EvidenceAtom, EvidenceDocument
from ard_ossie.semantic.layout import LayoutDocument, LayoutRegion
from ard_ossie.semantic.spacing_repair import spacing_defect_codes

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
_QUALIFIED_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+"
)
_HANGUL_FRAGMENT = re.compile(r"(?<![가-힣A-Za-z0-9_])([가-힣]+)\s+([가-힣]+)")


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


def build_table_spacing_candidate_set(
    *,
    table: TableCandidate,
    evidence: EvidenceDocument,
    scorer: KoreanSpacingScorer,
) -> CandidateSet | None:
    catalog = {atom.atom_id: atom for atom in evidence.atoms}
    if not set(table.atom_ids).issubset(catalog):
        raise ValueError("TABLE_SPACING_ATOM_UNKNOWN")
    unresolved_spacing = table.features.get("cell_spacing_integrity") != 1.0
    ordered_cells = sorted(
        table.cells,
        key=lambda cell: (
            cell.start_row,
            cell.start_column,
            cell.end_row,
            cell.end_column,
            cell.cell_id,
        ),
    )
    source_parts: list[str] = []
    repaired_parts: list[str] = []
    atom_ids: list[str] = []
    mutable_indexes: list[int] = []
    used_language_repair = False
    for cell in ordered_cells:
        character_ids = tuple(
            atom_id for atom_id in cell.atom_ids if not catalog[atom_id].text.isspace()
        )
        if not character_ids:
            continue
        character_sequence = "".join(catalog[atom_id].text for atom_id in character_ids)
        current = (cell.rendered_text or "").strip()
        if _without_whitespace(current) != character_sequence:
            current = "".join(catalog[atom_id].text for atom_id in cell.atom_ids).strip()
        if _without_whitespace(current) != character_sequence:
            current = character_sequence

        deterministic = _qualified_identifier_repair(current)
        language = None
        if deterministic is None and not _looks_like_formula(current):
            language = next(
                (
                    normalized
                    for proposal in scorer.propose(current, (current,))
                    if (normalized := _canonicalize_whitespace(str(proposal)).strip())
                    and normalized != current
                    and _without_whitespace(normalized) == character_sequence
                    and not _text_spacing_defects(
                        table.region_id,
                        normalized,
                        character_sequence,
                        character_ids,
                    )
                ),
                None,
            )
        fragmented = _has_spacing_fragmentation(current)
        suspicious = deterministic is not None or (
            language is not None and (fragmented or unresolved_spacing)
        )
        replacement = deterministic or language if suspicious else None
        repaired = replacement or current
        start_index = len(atom_ids)
        atom_ids.extend(character_ids)
        if suspicious:
            mutable_indexes.extend(range(start_index, start_index + len(character_ids) - 1))
            used_language_repair = used_language_repair or deterministic is None
        source_parts.append(current)
        repaired_parts.append(repaired)

    if not mutable_indexes or source_parts == repaired_parts:
        return None
    character_sequence = "".join(catalog[atom_id].text for atom_id in atom_ids)
    source_whitespace = tuple(() for _ in range(max(0, len(atom_ids) - 1)))
    mutable_boundary_indexes = tuple(mutable_indexes)
    source = make_spacing_candidate(
        region_id=table.region_id,
        rendered_text="\n".join(source_parts),
        character_sequence=character_sequence,
        atom_ids=tuple(atom_ids),
        source_whitespace=source_whitespace,
        score=0.45,
        features={"source_spacing": 0.45, "table_cell_composite": 1.0},
        mutable_boundary_indexes=mutable_boundary_indexes,
    )
    repaired_score = 0.78 if used_language_repair else 0.95
    repaired = make_spacing_candidate(
        region_id=table.region_id,
        rendered_text="\n".join(repaired_parts),
        character_sequence=character_sequence,
        atom_ids=tuple(atom_ids),
        source_whitespace=source_whitespace,
        score=repaired_score,
        features={
            "table_cell_composite": 1.0,
            "table_cell_repair": repaired_score,
        },
        mutable_boundary_indexes=mutable_boundary_indexes,
    )
    if spacing_defect_codes(repaired):
        return None
    candidates = tuple(
        sorted((source, repaired), key=lambda item: (-item.score, item.candidate_id))
    )
    return CandidateSet(
        candidate_set_id=make_candidate_set_id(
            evidence.source_hash,
            table.region_id,
            tuple(candidate.candidate_id for candidate in candidates),
        ),
        source_hash=evidence.source_hash,
        region_id=table.region_id,
        decision_type="spacing",
        candidates=candidates,
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


def _qualified_identifier_repair(value: str) -> str | None:
    dense = _without_whitespace(value)
    if dense != value and _QUALIFIED_IDENTIFIER.fullmatch(dense) is not None:
        return dense
    return None


def _has_spacing_fragmentation(value: str) -> bool:
    if _qualified_identifier_repair(value) is not None:
        return True
    return any(
        len(match.group(1)) == 1 or len(match.group(2)) == 1
        for match in _HANGUL_FRAGMENT.finditer(value)
    )


def _looks_like_formula(value: str) -> bool:
    return any(character in value for character in "()=")


def _text_spacing_defects(
    region_id: str,
    rendered_text: str,
    character_sequence: str,
    atom_ids: tuple[str, ...],
) -> tuple[str, ...]:
    candidate = make_spacing_candidate(
        region_id=region_id,
        rendered_text=rendered_text,
        character_sequence=character_sequence,
        atom_ids=atom_ids,
        source_whitespace=tuple(() for _ in range(max(0, len(atom_ids) - 1))),
        score=0.5,
        features={"table_cell_probe": 1.0},
    )
    return spacing_defect_codes(candidate)


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
