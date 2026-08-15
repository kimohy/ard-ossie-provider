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
    "시뮬레이션",
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
_INLINE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")


class KoreanSpacingScorer(Protocol):
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]: ...


class KiwiSpacingScorer:
    def __init__(self, *, user_terms: Iterable[str] = DEFAULT_KOREAN_TECH_TERMS) -> None:
        from kiwipiepy import Kiwi

        self._kiwi = Kiwi()
        self._kiwi.global_config.space_tolerance = 2
        self._user_terms = tuple(
            term
            for term in user_terms
            if term and not any(character.isspace() for character in term)
        )
        for term in self._user_terms:
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

    def supports_join(self, left: str, right: str) -> bool:
        dense = left + right
        if any(dense in term for term in self._user_terms):
            return True
        tokens = tuple(self._kiwi.tokenize(dense))
        left_is_noun = any(
            token.start == 0
            and token.len == len(left)
            and str(token.tag).startswith("NN")
            for token in tokens
        )
        follows_with_derivational_hada = any(
            token.start == len(left)
            and token.form == "하"
            and token.tag == "XSV"
            for token in tokens
        )
        return left_is_noun and follows_with_derivational_hada


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
        language: tuple[str, tuple[int, ...]] | None = None
        if deterministic is None and not _looks_like_formula(current):
            language = _first_language_repair(
                scorer=scorer,
                value=current,
                region_id=table.region_id,
                character_sequence=character_sequence,
                atom_ids=character_ids,
            )
        replacement = deterministic or (language[0] if language is not None else None)
        local_mutable = (
            _changed_boundary_indexes(current, deterministic)
            if deterministic is not None
            else language[1]
            if language is not None
            else ()
        )
        repaired = replacement or current
        start_index = len(atom_ids)
        atom_ids.extend(character_ids)
        if local_mutable:
            mutable_indexes.extend(start_index + index for index in local_mutable)
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


def _protected_language_proposals(
    scorer: KoreanSpacingScorer,
    value: str,
) -> tuple[str, ...]:
    protected: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        sentinel = chr(0xE000 + len(protected))
        protected.append((sentinel, match.group(0)))
        return sentinel

    masked = _INLINE_IDENTIFIER.sub(replace, value)
    proposals: list[str] = []
    for proposal in scorer.propose(masked, (masked,)):
        restored = str(proposal)
        if any(sentinel not in restored for sentinel, _token in protected):
            continue
        for sentinel, token in protected:
            restored = restored.replace(sentinel, token)
        proposals.append(restored)
    return tuple(proposals)


def _first_language_repair(
    *,
    scorer: KoreanSpacingScorer,
    value: str,
    region_id: str,
    character_sequence: str,
    atom_ids: tuple[str, ...],
) -> tuple[str, tuple[int, ...]] | None:
    for proposal in _protected_language_proposals(scorer, value):
        normalized = _punctuation_rules(
            _canonicalize_whitespace(str(proposal)).strip()
        )
        if (
            not normalized
            or _without_whitespace(normalized) != character_sequence
            or _text_spacing_defects(
                region_id,
                normalized,
                character_sequence,
                atom_ids,
            )
        ):
            continue
        if normalized == value:
            return None
        source_states = _spacing_states(value)
        proposal_states = _spacing_states(normalized)
        changed = {
            index
            for index, (source, proposed) in enumerate(
                zip(source_states, proposal_states, strict=True)
            )
            if source != proposed
        }
        hangul_removals = {
            index
            for index in changed
            if source_states[index] == "space"
            and proposal_states[index] == "none"
            and _hangul_pair_at_boundary(value, index)
        }
        authorized = _supported_fragment_removals(
            scorer=scorer,
            value=value,
            removal_indexes=hangul_removals,
        )
        if not authorized:
            return None
        merged_states = list(source_states)
        for index in authorized:
            merged_states[index] = proposal_states[index]
        repaired = _render_spacing_states(_without_whitespace(value), merged_states)
        if _text_spacing_defects(
            region_id,
            repaired,
            character_sequence,
            atom_ids,
        ):
            return None
        return repaired, tuple(sorted(authorized))
    return None


def _changed_boundary_indexes(source: str, repaired: str) -> tuple[int, ...]:
    return tuple(
        index
        for index, (before, after) in enumerate(
            zip(_spacing_states(source), _spacing_states(repaired), strict=True)
        )
        if before != after
    )


def _spacing_states(value: str) -> tuple[str, ...]:
    states: list[str] = []
    seen_character = False
    gap = ""
    for character in value:
        if character.isspace():
            if seen_character:
                gap += character
            continue
        if seen_character:
            states.append("hard_break" if "\n" in gap else "space" if gap else "none")
        gap = ""
        seen_character = True
    return tuple(states)


def _render_spacing_states(character_sequence: str, states: list[str]) -> str:
    rendered: list[str] = []
    for index, character in enumerate(character_sequence):
        rendered.append(character)
        if index >= len(states):
            continue
        if states[index] == "space":
            rendered.append(" ")
        elif states[index] == "hard_break":
            rendered.append("\n")
    return "".join(rendered)


def _supported_fragment_removals(
    *,
    scorer: KoreanSpacingScorer,
    value: str,
    removal_indexes: set[int],
) -> set[int]:
    indexes: set[int] = set()
    for gap in re.finditer(r"\s+", value):
        boundary_index = (
            sum(not character.isspace() for character in value[: gap.start()]) - 1
        )
        if boundary_index not in removal_indexes:
            continue
        left = re.search(r"([가-힣]+)$", value[: gap.start()])
        right = re.match(r"([가-힣]+)", value[gap.end() :])
        if left is None or right is None:
            continue
        left_token, right_token = left.group(1), right.group(1)
        if right_token == "수" and len(left_token) > 1:
            continue
        supports_join = getattr(scorer, "supports_join", None)
        morphology_support = callable(supports_join) and bool(
            supports_join(left_token, right_token)
        )
        if len(left_token) > 1 and len(right_token) > 1 and not morphology_support:
            continue
        probe = f"{left_token} {right_token}"
        dense = left_token + right_token
        if morphology_support or any(
            _without_whitespace(normalized) == dense
            and _spacing_states(normalized)[len(left_token) - 1] == "none"
            for proposal in _protected_language_proposals(scorer, probe)
            if (normalized := _canonicalize_whitespace(proposal).strip())
        ):
            indexes.add(boundary_index)
    return indexes


def _hangul_pair_at_boundary(value: str, boundary_index: int) -> bool:
    characters = [character for character in value if not character.isspace()]
    return (
        0 <= boundary_index < len(characters) - 1
        and "가" <= characters[boundary_index] <= "힣"
        and "가" <= characters[boundary_index + 1] <= "힣"
    )


def _looks_like_formula(value: str) -> bool:
    return "=" in value or re.search(r"\b[A-Z][A-Z0-9_]*\s*\(", value) is not None


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
