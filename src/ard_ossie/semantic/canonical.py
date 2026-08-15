"""Canonical semantic PDF IR, assembly, and publication invariants."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.models import Sha256, StrictModel
from ard_ossie.semantic.adjudication import DecisionId, DecisionRecord
from ard_ossie.semantic.candidates import (
    BlockCandidate,
    Candidate,
    CandidateSet,
    ContinuationCandidate,
    ReadingOrderCandidate,
    SpacingCandidate,
    TableCandidate,
)
from ard_ossie.semantic.correction import has_raw_html
from ard_ossie.semantic.evidence import AtomId, EvidenceAtom, EvidenceDocument, RegionId
from ard_ossie.semantic.layout import LayoutDocument, LayoutRegion, topological_region_order
from ard_ossie.semantic.models import (
    MAX_LIST_DEPTH,
    MAX_TABLE_CELLS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_GRID_AREA,
    MAX_TABLE_ROWS,
    ImmutableStrictModel,
    SourceBox,
)

BlockId = Annotated[str, StringConstraints(pattern=r"^block_[0-9a-f]{16}$")]
FindingCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


class SemanticPipelineStatus(StrEnum):
    VERIFIED = "verified"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


class CanonicalCell(ImmutableStrictModel):
    cell_id: str = Field(pattern=r"^cell_[0-9a-f]{16}$")
    start_row: int = Field(ge=0, le=MAX_TABLE_ROWS)
    end_row: int = Field(gt=0, le=MAX_TABLE_ROWS)
    start_column: int = Field(ge=0, le=MAX_TABLE_COLUMNS)
    end_column: int = Field(gt=0, le=MAX_TABLE_COLUMNS)
    text: str
    atom_ids: tuple[AtomId, ...] = ()
    column_header: bool = False

    @model_validator(mode="after")
    def validate_span(self) -> CanonicalCell:
        if self.start_row >= self.end_row or self.start_column >= self.end_column:
            raise ValueError("CANONICAL_CELL_SPAN_INVALID")
        return self


class CanonicalBlock(ImmutableStrictModel):
    block_id: BlockId
    order: int = Field(ge=0)
    region_id: RegionId
    page: int = Field(ge=1)
    kind: Literal["heading", "paragraph", "list_item", "table", "caption"]
    text: str
    atom_ids: tuple[AtomId, ...]
    heading_level: int | None = Field(default=None, ge=1, le=6)
    list_kind: Literal["ordered", "unordered"] | None = None
    list_depth: int | None = Field(default=None, ge=1, le=MAX_LIST_DEPTH)
    row_count: int | None = Field(default=None, gt=0, le=MAX_TABLE_ROWS)
    column_count: int | None = Field(default=None, gt=0, le=MAX_TABLE_COLUMNS)
    cells: tuple[CanonicalCell, ...] = Field(default=(), max_length=MAX_TABLE_CELLS)
    decision_ids: tuple[DecisionId, ...] = ()

    @model_validator(mode="after")
    def validate_metadata(self) -> CanonicalBlock:
        if (self.heading_level is not None) != (self.kind == "heading"):
            raise ValueError("CANONICAL_HEADING_METADATA_INVALID")
        list_metadata = self.list_kind is not None and self.list_depth is not None
        if list_metadata != (self.kind == "list_item"):
            raise ValueError("CANONICAL_LIST_METADATA_INVALID")
        table_metadata = self.row_count is not None and self.column_count is not None
        if table_metadata != (self.kind == "table") or bool(self.cells) != (self.kind == "table"):
            raise ValueError("CANONICAL_TABLE_METADATA_INVALID")
        return self


class CanonicalFigure(ImmutableStrictModel):
    figure_id: str = Field(pattern=r"^figure_[0-9a-f]{16}$")
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    caption: str | None = None
    caption_atom_ids: tuple[AtomId, ...] = ()
    decision_ids: tuple[DecisionId, ...] = ()


class ExcludedEvidence(ImmutableStrictModel):
    region_id: RegionId
    atom_ids: tuple[AtomId, ...] = Field(min_length=1)
    reason: Literal["repeated_page_header", "repeated_page_footer", "page_number"]
    proven_repeated_edge: Literal[True] = True


class WhitespaceDisposition(ImmutableStrictModel):
    atom_id: AtomId
    outcome: Literal["retained", "normalized", "layout_only"]
    boundary_state: Literal["none", "space", "hard_break"]
    decision_id: DecisionId


class CanonicalContinuation(ImmutableStrictModel):
    previous_region_id: RegionId
    current_region_id: RegionId
    decision_id: DecisionId


class CanonicalSemanticDocument(ImmutableStrictModel):
    schema_version: Literal["canonical-semantic-v2"] = "canonical-semantic-v2"
    source_hash: Sha256
    region_order: tuple[RegionId, ...]
    blocks: tuple[CanonicalBlock, ...]
    figures: tuple[CanonicalFigure, ...] = ()
    excluded_evidence: tuple[ExcludedEvidence, ...] = ()
    whitespace_dispositions: tuple[WhitespaceDisposition, ...] = ()
    continuations: tuple[CanonicalContinuation, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()


class ValidationFinding(StrictModel):
    code: FindingCode
    message: str = Field(min_length=1)
    region_id: RegionId | None = None


class SemanticValidationReport(StrictModel):
    status: SemanticPipelineStatus
    publishable: bool
    source_hash: Sha256
    canonical_hash: Sha256
    findings: list[ValidationFinding]
    character_coverage: float = Field(ge=0, le=1)
    missing_atom_count: int = Field(ge=0)
    duplicate_atom_count: int = Field(ge=0)
    degraded_block_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)


def assemble_canonical(
    *,
    evidence: EvidenceDocument,
    layout: LayoutDocument,
    candidate_sets: tuple[CandidateSet, ...],
    decisions: tuple[DecisionRecord, ...],
) -> tuple[CanonicalSemanticDocument, SemanticValidationReport]:
    if evidence.source_hash != layout.source_hash:
        raise ValueError("CANONICAL_SOURCE_HASH_MISMATCH")
    sets_by_scope = {
        (candidate_set.region_id, candidate_set.decision_type): candidate_set
        for candidate_set in candidate_sets
    }
    decisions_by_set = {item.candidate_set_id: item for item in decisions}
    atom_catalog = {atom.atom_id: atom for atom in evidence.atoms}

    order = topological_region_order(layout)
    reading_set = next(
        (item for item in candidate_sets if item.decision_type == "reading_order"),
        None,
    )
    if reading_set is not None:
        selected_order = _selected_candidate(reading_set, decisions_by_set)
        if isinstance(selected_order, ReadingOrderCandidate):
            order = selected_order.region_ids

    regions = {region.region_id: region for region in layout.regions}
    blocks: list[CanonicalBlock] = []
    figures: list[CanonicalFigure] = []
    exclusions: list[ExcludedEvidence] = []
    whitespace: list[WhitespaceDisposition] = []
    for region_id in order:
        region = regions[region_id]
        if region.repeated_edge:
            reason = {
                "page_header": "repeated_page_header",
                "page_footer": "repeated_page_footer",
                "page_number": "page_number",
            }.get(region.hint)
            if reason is not None:
                exclusions.append(
                    ExcludedEvidence(
                        region_id=region_id,
                        atom_ids=region.atom_ids,
                        reason=reason,
                    )
                )
                whitespace.extend(
                    _layout_only_whitespace(region, atom_catalog, "decision_0000000000000000")
                )
                continue

        spacing_set = sets_by_scope.get((region_id, "spacing"))
        spacing = (
            _selected_candidate(spacing_set, decisions_by_set)
            if spacing_set is not None
            else None
        )
        block_set = sets_by_scope.get((region_id, "block"))
        block = (
            _selected_candidate(block_set, decisions_by_set)
            if block_set is not None
            else None
        )
        spacing_candidate = spacing if isinstance(spacing, SpacingCandidate) else None
        block_candidate = block if isinstance(block, BlockCandidate) else None
        text = (
            spacing_candidate.rendered_text
            if spacing_candidate is not None
            else "".join(atom_catalog[item].text for item in region.atom_ids)
        )
        atom_ids = tuple(
            atom_id for atom_id in region.atom_ids if not atom_catalog[atom_id].text.isspace()
        )
        decision_ids = tuple(
            item.decision_id
            for item in (
                decisions_by_set.get(spacing_set.candidate_set_id) if spacing_set else None,
                decisions_by_set.get(block_set.candidate_set_id) if block_set else None,
            )
            if item is not None
        )
        if block_candidate is not None and block_candidate.block_kind == "figure":
            digest = canonical_hash({"region_id": region_id, "atom_ids": atom_ids})
            figures.append(
                CanonicalFigure(
                    figure_id=f"figure_{digest[:16]}",
                    region_id=region_id,
                    page=region.page,
                    bbox=region.bbox,
                    decision_ids=decision_ids,
                )
            )
        else:
            kind = block_candidate.block_kind if block_candidate is not None else "paragraph"
            if kind == "figure":
                kind = "paragraph"
            table_set = sets_by_scope.get((region_id, "table"))
            table = (
                _selected_candidate(table_set, decisions_by_set)
                if table_set is not None and kind == "table"
                else None
            )
            table_candidate = table if isinstance(table, TableCandidate) else None
            if table_set is not None:
                table_decision = decisions_by_set.get(table_set.candidate_set_id)
                if table_decision is not None:
                    decision_ids = (*decision_ids, table_decision.decision_id)
            canonical_cells = (
                _canonical_cells(table_candidate, atom_catalog, spacing_candidate)
                if table_candidate is not None
                else ()
            )
            if kind == "table" and table_candidate is None:
                kind = "paragraph"
            digest = canonical_hash({"region_id": region_id, "kind": kind, "atom_ids": atom_ids})
            blocks.append(
                CanonicalBlock(
                    block_id=f"block_{digest[:16]}",
                    order=len(blocks),
                    region_id=region_id,
                    page=region.page,
                    kind=kind,
                    text=text,
                    atom_ids=atom_ids,
                    heading_level=(
                        block_candidate.heading_level
                        if block_candidate is not None and kind == "heading"
                        else None
                    ),
                    list_kind=(
                        block_candidate.list_kind
                        if block_candidate is not None and kind == "list_item"
                        else None
                    ),
                    list_depth=(
                        block_candidate.list_depth
                        if block_candidate is not None and kind == "list_item"
                        else None
                    ),
                    row_count=table_candidate.row_count if table_candidate is not None else None,
                    column_count=(
                        table_candidate.column_count if table_candidate is not None else None
                    ),
                    cells=canonical_cells,
                    decision_ids=decision_ids,
                )
            )
        whitespace.extend(
            _whitespace_dispositions(
                region,
                atom_catalog,
                spacing_candidate,
                decisions_by_set.get(spacing_set.candidate_set_id) if spacing_set else None,
            )
        )

    continuations: list[CanonicalContinuation] = []
    for candidate_set in candidate_sets:
        if candidate_set.decision_type != "continuation":
            continue
        candidate = _selected_candidate(candidate_set, decisions_by_set)
        decision = decisions_by_set.get(candidate_set.candidate_set_id)
        if (
            isinstance(candidate, ContinuationCandidate)
            and candidate.continue_previous
            and decision is not None
        ):
            continuations.append(
                CanonicalContinuation(
                    previous_region_id=candidate.previous_region_id,
                    current_region_id=candidate.current_region_id,
                    decision_id=decision.decision_id,
                )
            )

    document = CanonicalSemanticDocument(
        source_hash=evidence.source_hash,
        region_order=order,
        blocks=tuple(blocks),
        figures=tuple(figures),
        excluded_evidence=tuple(exclusions),
        whitespace_dispositions=tuple(whitespace),
        continuations=tuple(continuations),
        decisions=decisions,
    )
    return document, validate_canonical(evidence, document, layout=layout)


def validate_canonical(
    evidence: EvidenceDocument,
    document: CanonicalSemanticDocument,
    *,
    layout: LayoutDocument | None = None,
) -> SemanticValidationReport:
    findings: list[ValidationFinding] = []
    if evidence.source_hash != document.source_hash or (
        layout is not None and layout.source_hash != evidence.source_hash
    ):
        findings.append(
            _finding(
                "INVARIANT_SOURCE_BINDING",
                "Canonical source binding does not match evidence.",
            )
        )
    atom_catalog = {atom.atom_id: atom for atom in evidence.atoms}
    known_ids = set(atom_catalog)
    excluded_ids = [
        atom_id for exclusion in document.excluded_evidence for atom_id in exclusion.atom_ids
    ]
    allocated_ids = [atom_id for block in document.blocks for atom_id in block.atom_ids]
    allocated_ids.extend(
        atom_id for figure in document.figures for atom_id in figure.caption_atom_ids
    )
    all_references = [*allocated_ids, *excluded_ids]
    if any(atom_id not in known_ids for atom_id in all_references):
        findings.append(
            _finding("INVARIANT_EVIDENCE_REFERENCE", "Unknown evidence atom reference.")
        )

    counts = Counter(all_references)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    if duplicate_count:
        findings.append(
            _finding(
                "INVARIANT_ATOM_ALLOCATION_DUPLICATE",
                "Evidence atoms are allocated more than once.",
            )
        )
    source_non_whitespace = [atom.atom_id for atom in evidence.atoms if not atom.text.isspace()]
    missing_ids = set(source_non_whitespace) - set(all_references)
    if missing_ids:
        findings.append(
            _finding("INVARIANT_ATOM_ALLOCATION_MISSING", "Evidence atoms are not allocated.")
        )

    character_mismatch = any(
        _non_whitespace(block.text)
        != _atom_text_sequence(block.atom_ids, atom_catalog)
        or (
            block.kind == "table"
            and any(
                _non_whitespace(cell.text)
                != _atom_text_sequence(cell.atom_ids, atom_catalog)
                for cell in block.cells
            )
        )
        for block in document.blocks
    ) or any(
        _non_whitespace(figure.caption or "")
        != _atom_text_sequence(figure.caption_atom_ids, atom_catalog)
        for figure in document.figures
    )
    if character_mismatch:
        findings.append(
            _finding(
                "INVARIANT_CHARACTER_LOSS",
                "Canonical output differs from authoritative source characters.",
            )
        )

    source_whitespace = {atom.atom_id for atom in evidence.atoms if atom.text.isspace()}
    dispositions = [item.atom_id for item in document.whitespace_dispositions]
    if set(dispositions) != source_whitespace or len(dispositions) != len(set(dispositions)):
        findings.append(
            _finding(
                "INVARIANT_WHITESPACE_ACCOUNTING",
                "Source whitespace is not accounted for exactly once.",
            )
        )

    if document.excluded_evidence and (
        layout is None or not _valid_exclusions(document.excluded_evidence, layout)
    ):
        findings.append(
            _finding(
                "INVARIANT_EXCLUSION_PROOF",
                "Excluded evidence lacks repeated-edge layout proof.",
            )
        )

    if layout is not None and not _valid_region_order(document.region_order, layout):
        findings.append(
            _finding("INVARIANT_READING_ORDER", "Canonical order violates the layout DAG.")
        )

    if any(not _valid_table(block) for block in document.blocks if block.kind == "table"):
        findings.append(
            _finding("INVARIANT_TABLE_GRID", "Canonical table grid is incomplete or overlapping.")
        )

    if layout is not None and any(
        not _valid_continuation(item, document.region_order, layout)
        for item in document.continuations
    ):
        findings.append(
            _finding("INVARIANT_CONTINUATION", "Cross-page continuation is not source-adjacent.")
        )

    review_decisions = [item for item in document.decisions if item.outcome == "review_required"]
    from ard_ossie.semantic.render import render_canonical_markdown

    rendered = render_canonical_markdown(document)
    if not rendered or has_raw_html(rendered):
        findings.append(
            _finding("INVARIANT_MARKDOWN_OUTPUT", "Markdown is empty or contains raw HTML.")
        )

    failed = bool(findings)
    status = (
        SemanticPipelineStatus.FAILED
        if failed
        else SemanticPipelineStatus.REVIEW_REQUIRED
        if review_decisions
        else SemanticPipelineStatus.VERIFIED
    )
    preserved = len(set(source_non_whitespace) & set(all_references))
    coverage = 1.0 if not source_non_whitespace else preserved / len(source_non_whitespace)
    document_hash = canonical_hash(_canonical_content_payload(document))
    return SemanticValidationReport(
        status=status,
        publishable=status is SemanticPipelineStatus.VERIFIED,
        source_hash=evidence.source_hash,
        canonical_hash=document_hash,
        findings=findings,
        character_coverage=round(coverage, 6),
        missing_atom_count=len(missing_ids),
        duplicate_atom_count=duplicate_count,
        degraded_block_count=len(
            {
                decision.region_id for decision in review_decisions if decision.region_id
            }
        ),
        model_call_count=sum(item.source in {"model", "provider"} for item in document.decisions),
    )


def _non_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _canonical_content_payload(document: CanonicalSemanticDocument) -> dict[str, object]:
    payload = document.model_dump(mode="json")
    payload.pop("decisions", None)
    for block in payload["blocks"]:
        block.pop("decision_ids", None)
    for figure in payload["figures"]:
        figure.pop("decision_ids", None)
    for disposition in payload["whitespace_dispositions"]:
        disposition.pop("decision_id", None)
    for continuation in payload["continuations"]:
        continuation.pop("decision_id", None)
    return payload


def _atom_text_sequence(
    atom_ids: tuple[AtomId, ...],
    atom_catalog: dict[AtomId, EvidenceAtom],
) -> str:
    return "".join(
        atom_catalog[atom_id].text
        for atom_id in atom_ids
        if atom_id in atom_catalog and not atom_catalog[atom_id].text.isspace()
    )


def _selected_candidate(
    candidate_set: CandidateSet,
    decisions: dict[str, DecisionRecord],
) -> Candidate:
    decision = decisions.get(candidate_set.candidate_set_id)
    if decision is not None and decision.selected_candidate_id is not None:
        selected = next(
            (
                item
                for item in candidate_set.candidates
                if item.candidate_id == decision.selected_candidate_id
            ),
            None,
        )
        if selected is not None:
            return selected
    return max(candidate_set.candidates, key=lambda item: (item.score, item.candidate_id))


def _canonical_cells(
    table: TableCandidate,
    atom_catalog: dict[AtomId, EvidenceAtom],
    spacing: SpacingCandidate | None,
) -> tuple[CanonicalCell, ...]:
    return tuple(
        CanonicalCell(
            cell_id=cell.cell_id,
            start_row=cell.start_row,
            end_row=cell.end_row,
            start_column=cell.start_column,
            end_column=cell.end_column,
            text=(
                cell.rendered_text
                if cell.rendered_text is not None
                else _project_cell_spacing(cell.atom_ids, atom_catalog, spacing)
            ),
            atom_ids=tuple(
                item for item in cell.atom_ids if not atom_catalog[item].text.isspace()
            ),
            column_header=cell.column_header,
        )
        for cell in table.cells
    )


def _project_cell_spacing(
    atom_ids: tuple[AtomId, ...],
    atom_catalog: dict[AtomId, EvidenceAtom],
    spacing: SpacingCandidate | None,
) -> str:
    characters = [atom_id for atom_id in atom_ids if not atom_catalog[atom_id].text.isspace()]
    if not characters:
        return ""
    selected = (
        {
            (boundary.left_atom_id, boundary.right_atom_id): boundary.state
            for boundary in spacing.boundaries
        }
        if spacing is not None
        else {}
    )
    position = {atom_id: index for index, atom_id in enumerate(atom_ids)}
    result: list[str] = []
    for index, atom_id in enumerate(characters):
        result.append(atom_catalog[atom_id].text)
        if index == len(characters) - 1:
            continue
        following = characters[index + 1]
        state = selected.get((atom_id, following))
        if state is None:
            source = atom_ids[position[atom_id] + 1 : position[following]]
            if any(atom_catalog[item].kind == "line_break" for item in source):
                state = "hard_break"
            elif any(atom_catalog[item].text.isspace() for item in source):
                state = "space"
            else:
                state = "none"
        if state == "space":
            result.append(" ")
        elif state == "hard_break":
            result.append("\n")
    return "".join(result)


def _whitespace_dispositions(
    region: LayoutRegion,
    atom_catalog: dict[AtomId, EvidenceAtom],
    spacing: SpacingCandidate | None,
    decision: DecisionRecord | None,
) -> tuple[WhitespaceDisposition, ...]:
    decision_id = decision.decision_id if decision else "decision_0000000000000000"
    boundary_by_atom = {
        atom_id: boundary
        for boundary in spacing.boundaries
        for atom_id in boundary.source_whitespace_atom_ids
    } if spacing is not None else {}
    result: list[WhitespaceDisposition] = []
    for atom_id in region.atom_ids:
        atom = atom_catalog[atom_id]
        if not atom.text.isspace():
            continue
        boundary = boundary_by_atom.get(atom_id)
        if boundary is None:
            result.append(
                WhitespaceDisposition(
                    atom_id=atom_id,
                    outcome="layout_only" if spacing is not None else "retained",
                    boundary_state=("hard_break" if atom.text == "\n" else "space"),
                    decision_id=decision_id,
                )
            )
            continue
        source_state = "hard_break" if atom.text == "\n" else "space"
        result.append(
            WhitespaceDisposition(
                atom_id=atom_id,
                outcome="retained" if boundary.state == source_state else "normalized",
                boundary_state=boundary.state,
                decision_id=decision_id,
            )
        )
    return tuple(result)


def _layout_only_whitespace(
    region: LayoutRegion,
    atom_catalog: dict[AtomId, EvidenceAtom],
    decision_id: DecisionId,
) -> tuple[WhitespaceDisposition, ...]:
    return tuple(
        WhitespaceDisposition(
            atom_id=atom_id,
            outcome="layout_only",
            boundary_state="none",
            decision_id=decision_id,
        )
        for atom_id in region.atom_ids
        if atom_catalog[atom_id].text.isspace()
    )


def _valid_region_order(order: tuple[RegionId, ...], layout: LayoutDocument) -> bool:
    if len(order) != len(set(order)) or set(order) != {item.region_id for item in layout.regions}:
        return False
    position = {region_id: index for index, region_id in enumerate(order)}
    return all(
        position[edge.before_region_id] < position[edge.after_region_id]
        for edge in layout.order_edges
    )


def _valid_table(block: CanonicalBlock) -> bool:
    if block.row_count is None or block.column_count is None:
        return False
    if block.row_count * block.column_count > MAX_TABLE_GRID_AREA:
        return False
    cell_atom_ids = [atom_id for cell in block.cells for atom_id in cell.atom_ids]
    if len(cell_atom_ids) != len(set(cell_atom_ids)) or set(cell_atom_ids) != set(
        block.atom_ids
    ):
        return False
    occupied: set[tuple[int, int]] = set()
    for cell in block.cells:
        for row in range(cell.start_row, cell.end_row):
            for column in range(cell.start_column, cell.end_column):
                if row >= block.row_count or column >= block.column_count:
                    return False
                coordinate = (row, column)
                if coordinate in occupied:
                    return False
                occupied.add(coordinate)
    return occupied == {
        (row, column)
        for row in range(block.row_count)
        for column in range(block.column_count)
    }


def _valid_continuation(
    continuation: CanonicalContinuation,
    order: tuple[RegionId, ...],
    layout: LayoutDocument,
) -> bool:
    try:
        previous_index = order.index(continuation.previous_region_id)
        current_index = order.index(continuation.current_region_id)
    except ValueError:
        return False
    regions = {item.region_id: item for item in layout.regions}
    previous = regions.get(continuation.previous_region_id)
    current = regions.get(continuation.current_region_id)
    if previous is None or current is None or current.page != previous.page + 1:
        return False
    between = order[previous_index + 1 : current_index]
    if current_index <= previous_index or any(
        not regions[region_id].repeated_edge for region_id in between if region_id in regions
    ):
        return False
    overlap = max(
        0.0,
        min(previous.bbox.right, current.bbox.right)
        - max(previous.bbox.left, current.bbox.left),
    )
    minimum_width = min(
        previous.bbox.right - previous.bbox.left,
        current.bbox.right - current.bbox.left,
    )
    return bool(
        minimum_width > 0
        and overlap / minimum_width >= 0.75
    )


def _valid_exclusions(
    exclusions: tuple[ExcludedEvidence, ...],
    layout: LayoutDocument,
) -> bool:
    regions = {item.region_id: item for item in layout.regions}
    expected_hint = {
        "repeated_page_header": "page_header",
        "repeated_page_footer": "page_footer",
        "page_number": "page_number",
    }
    return all(
        (region := regions.get(exclusion.region_id)) is not None
        and region.repeated_edge
        and region.hint == expected_hint[exclusion.reason]
        and tuple(region.atom_ids) == exclusion.atom_ids
        for exclusion in exclusions
    )


def _finding(code: str, message: str) -> ValidationFinding:
    return ValidationFinding(code=code, message=message)
