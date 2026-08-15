from __future__ import annotations

import pytest
from pydantic import ValidationError

from ard_ossie.semantic.candidates import (
    TableCandidate,
    TableCellCandidate,
    assert_table_grid_complete,
)
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
)
from ard_ossie.semantic.layout import LayoutDocument, LayoutLine, LayoutRegion
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.structure import (
    StructureBlock,
    StructureCell,
    StructureDocument,
    StructureTable,
)
from ard_ossie.semantic.structure_candidates import build_table_candidate_set

SOURCE_HASH = "e" * 64
REGION_ID = "region_00000000000000ff"


class FixedSpacingScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        return ("테이블 결합",) if text == "테이 블 결 합" else (text,)


def _table_fixture(
    specs: tuple[tuple[int, int, int, int, str, SourceBox], ...],
    *,
    row_count: int,
    column_count: int,
    hinted_cells: tuple[StructureCell, ...] = (),
    page: int = 1,
) -> tuple[LayoutRegion, EvidenceDocument, LayoutDocument, StructureDocument]:
    atoms: list[EvidenceAtom] = []
    lines: list[LayoutLine] = []
    for line_number, (_row, _start_column, _end_column, _header, text, box) in enumerate(
        specs,
        start=1,
    ):
        atom_ids: list[str] = []
        width = (box.right - box.left) / len(text)
        for character_index, character in enumerate(text):
            atom_id = f"atom_{len(atoms) + 1:016x}"
            atom_ids.append(atom_id)
            atoms.append(
                EvidenceAtom(
                    atom_id=atom_id,
                    ordinal=len(atoms),
                    page=page,
                    bbox=SourceBox(
                        left=box.left + width * character_index,
                        bottom=box.bottom,
                        right=box.left + width * (character_index + 1),
                        top=box.top,
                    ),
                    text=character,
                    kind="whitespace" if character.isspace() else "character",
                    authority="embedded",
                    source_object=line_number,
                    source_index=character_index,
                )
            )
        lines.append(
            LayoutLine(
                line_id=f"line_{line_number:016x}",
                page=page,
                bbox=box,
                atom_ids=tuple(atom_ids),
                evidence_region_ids=("region_0000000000000001",),
                baseline=box.bottom,
                median_height=box.top - box.bottom,
            )
        )
    outer_box = SourceBox(left=0.1, bottom=0.3, right=0.9, top=0.9)
    atom_ids = tuple(atom.atom_id for atom in atoms)
    evidence = EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=page,
        parser_versions={"fixture": "1"},
        atoms=tuple(atoms),
        regions=(
            EvidenceRegion(
                region_id="region_0000000000000001",
                page=page,
                bbox=outer_box,
                atom_ids=atom_ids,
                authority="embedded",
            ),
        ),
    )
    region = LayoutRegion(
        region_id=REGION_ID,
        page=page,
        bbox=outer_box,
        line_ids=tuple(line.line_id for line in lines),
        atom_ids=atom_ids,
        evidence_region_ids=("region_0000000000000001",),
        column=0,
        hint="table",
    )
    layout = LayoutDocument(
        source_hash=SOURCE_HASH,
        page_count=page,
        lines=tuple(lines),
        regions=(region,),
        order_edges=(),
    )
    hint = StructureDocument(
        blocks=(
            StructureBlock(
                kind="table",
                order=0,
                page=page,
                bbox=outer_box,
                text_hint="table hint",
                table=StructureTable(
                    row_count=row_count,
                    column_count=column_count,
                    cells=hinted_cells,
                ),
            ),
        )
        if hinted_cells
        else ()
    )
    return region, evidence, layout, hint


def _box(row: int, column: int, *, end_column: int | None = None) -> SourceBox:
    lefts = (0.1, 0.4, 0.7)
    rights = (0.3, 0.6, 0.9)
    tops = (0.9, 0.7, 0.5)
    bottoms = (0.8, 0.6, 0.4)
    return SourceBox(
        left=lefts[column],
        bottom=bottoms[row],
        right=rights[(end_column or column + 1) - 1],
        top=tops[row],
    )


def test_borderless_table_forms_rectangular_grid_with_blank_cell() -> None:
    specs = tuple(
        (row, column, column + 1, int(row == 0), f"{row}{column}", _box(row, column))
        for row in range(3)
        for column in range(3)
        if (row, column) != (1, 2)
    )
    candidate_set = build_table_candidate_set(
        *_table_fixture(specs, row_count=3, column_count=3)
    )
    selected = max(candidate_set.candidates, key=lambda item: item.score)

    assert isinstance(selected, TableCandidate)
    assert (selected.row_count, selected.column_count) == (3, 3)
    assert {(cell.start_row, cell.start_column) for cell in selected.cells} == {
        (row, column) for row in range(3) for column in range(3)
    }
    assert selected.cells[5].atom_ids == ()
    assert_table_grid_complete(selected)


def test_merged_header_owns_atoms_once_and_covers_each_grid_coordinate() -> None:
    specs = (
        (0, 0, 3, 1, "병합 제목", _box(0, 0, end_column=3)),
        *((1, column, column + 1, 0, str(column), _box(1, column)) for column in range(3)),
    )
    hints = (
        StructureCell(0, 1, 0, 3, "병합 제목", True, _box(0, 0, end_column=3)),
        *(
            StructureCell(
                1,
                2,
                column,
                column + 1,
                str(column),
                False,
                _box(1, column),
            )
            for column in range(3)
        ),
    )
    selected = max(
        build_table_candidate_set(
            *_table_fixture(specs, row_count=2, column_count=3, hinted_cells=hints)
        ).candidates,
        key=lambda item: item.score,
    )

    assert isinstance(selected, TableCandidate)
    assert selected.cells[0].end_column == 3
    assert_table_grid_complete(selected)
    allocations = [atom_id for cell in selected.cells for atom_id in cell.atom_ids]
    assert len(allocations) == len(set(allocations))


def test_vertically_split_text_remains_in_one_grid_cell() -> None:
    specs = (
        (0, 0, 1, 1, "A", SourceBox(left=0.1, bottom=0.8, right=0.3, top=0.9)),
        (0, 1, 2, 1, "B", SourceBox(left=0.4, bottom=0.8, right=0.6, top=0.9)),
        (1, 0, 1, 0, "긴", SourceBox(left=0.1, bottom=0.61, right=0.3, top=0.65)),
        (1, 0, 1, 0, "텍스트", SourceBox(left=0.1, bottom=0.56, right=0.3, top=0.60)),
        (1, 1, 2, 0, "값", SourceBox(left=0.4, bottom=0.56, right=0.6, top=0.65)),
    )
    selected = max(
        build_table_candidate_set(
            *_table_fixture(specs, row_count=2, column_count=2)
        ).candidates,
        key=lambda item: item.score,
    )

    assert isinstance(selected, TableCandidate)
    assert (selected.row_count, selected.column_count) == (2, 2)
    split_cell = next(
        cell for cell in selected.cells if (cell.start_row, cell.start_column) == (1, 0)
    )
    assert len(split_cell.atom_ids) == len("긴텍스트")


def test_cell_hint_reorders_whole_line_fragments_without_changing_characters() -> None:
    specs = (
        (0, 0, 1, 1, "뒤", SourceBox(left=0.5, bottom=0.8, right=0.7, top=0.9)),
        (0, 0, 1, 1, "앞", SourceBox(left=0.1, bottom=0.8, right=0.3, top=0.9)),
    )
    hints = (
        StructureCell(
            0,
            1,
            0,
            1,
            "앞 뒤",
            True,
            SourceBox(left=0.1, bottom=0.8, right=0.7, top=0.9),
        ),
    )
    region, evidence, layout, structure = _table_fixture(
        specs,
        row_count=1,
        column_count=1,
        hinted_cells=hints,
    )

    selected = max(
        build_table_candidate_set(region, evidence, layout, structure).candidates,
        key=lambda item: item.score,
    )
    atom_text = {atom.atom_id: atom.text for atom in evidence.atoms}
    rendered = "".join(atom_text[atom_id] for atom_id in selected.cells[0].atom_ids)

    assert rendered == "앞뒤"
    assert selected.cells[0].rendered_text == "앞 뒤"


def test_table_candidates_include_bounded_language_spacing_variant() -> None:
    specs = (
        (
            0,
            0,
            1,
            1,
            "테이 블 결 합",
            SourceBox(left=0.1, bottom=0.8, right=0.7, top=0.9),
        ),
    )
    hints = (
        StructureCell(
            0,
            1,
            0,
            1,
            "테이 블 결 합",
            True,
            SourceBox(left=0.1, bottom=0.8, right=0.7, top=0.9),
        ),
    )
    region, evidence, layout, structure = _table_fixture(
        specs,
        row_count=1,
        column_count=1,
        hinted_cells=hints,
    )

    candidate_set = build_table_candidate_set(
        region,
        evidence,
        layout,
        structure,
        spacing_scorer=FixedSpacingScorer(),
    )

    assert 1 < len(candidate_set.candidates) <= 5
    assert {
        candidate.cells[0].rendered_text for candidate in candidate_set.candidates
    } >= {"테이 블 결 합", "테이블 결합"}


def test_genuinely_ambiguous_header_yields_two_complete_candidates() -> None:
    specs = (
        (0, 0, 2, 1, "AB", SourceBox(left=0.1, bottom=0.8, right=0.6, top=0.9)),
        (1, 0, 1, 0, "1", SourceBox(left=0.1, bottom=0.6, right=0.3, top=0.7)),
        (1, 1, 2, 0, "2", SourceBox(left=0.4, bottom=0.6, right=0.6, top=0.7)),
    )
    candidate_set = build_table_candidate_set(
        *_table_fixture(specs, row_count=2, column_count=2)
    )

    assert len(candidate_set.candidates) == 2
    assert {
        candidate.cells[0].end_column
        for candidate in candidate_set.candidates
        if isinstance(candidate, TableCandidate)
    } == {1, 2}
    assert all(
        assert_table_grid_complete(candidate) is None
        for candidate in candidate_set.candidates
        if isinstance(candidate, TableCandidate)
    )


def test_overlapping_or_incompletely_allocated_grid_is_rejected() -> None:
    overlapping = (
        TableCellCandidate(
            cell_id="cell_0000000000000001",
            start_row=0,
            end_row=1,
            start_column=0,
            end_column=2,
            atom_ids=("atom_0000000000000001",),
            column_header=True,
        ),
        TableCellCandidate(
            cell_id="cell_0000000000000002",
            start_row=0,
            end_row=1,
            start_column=1,
            end_column=2,
            atom_ids=("atom_0000000000000002",),
            column_header=True,
        ),
    )

    with pytest.raises(ValidationError, match="TABLE_GRID_OVERLAP"):
        TableCandidate(
            candidate_id="candidate_0000000000000001",
            region_id=REGION_ID,
            row_count=1,
            column_count=2,
            cells=overlapping,
            atom_ids=("atom_0000000000000001", "atom_0000000000000002"),
            score=0.8,
            features={},
        )

    with pytest.raises(ValidationError, match="TABLE_GRID_NOT_PARTITIONED"):
        TableCandidate(
            candidate_id="candidate_0000000000000002",
            region_id=REGION_ID,
            row_count=1,
            column_count=2,
            cells=(overlapping[1],),
            atom_ids=("atom_0000000000000002",),
            score=0.8,
            features={},
        )


def test_repeated_page_header_hint_marks_header_without_duplicate_allocation() -> None:
    specs = (
        (0, 0, 1, 1, "열1", _box(0, 0)),
        (0, 1, 2, 1, "열2", _box(0, 1)),
        (1, 0, 1, 0, "가", _box(1, 0)),
        (1, 1, 2, 0, "나", _box(1, 1)),
    )
    hints = tuple(
        StructureCell(row, row + 1, column, column + 1, text, row == 0, box)
        for row, column, _end_column, _header, text, box in specs
    )
    selected = max(
        build_table_candidate_set(
            *_table_fixture(
                specs,
                row_count=2,
                column_count=2,
                hinted_cells=hints,
                page=2,
            )
        ).candidates,
        key=lambda item: item.score,
    )

    assert isinstance(selected, TableCandidate)
    assert all(cell.column_header for cell in selected.cells if cell.start_row == 0)
    allocations = [atom_id for cell in selected.cells for atom_id in cell.atom_ids]
    assert sorted(allocations) == sorted(selected.atom_ids)
