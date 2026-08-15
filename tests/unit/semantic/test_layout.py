from __future__ import annotations

from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
)
from ard_ossie.semantic.layout import normalize_layout, topological_region_order
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.structure import (
    StructureBlock,
    StructureCell,
    StructureDocument,
    StructureTable,
)

SOURCE_HASH = "c" * 64


def document_from_regions(
    entries: list[tuple[int, str, SourceBox, tuple[int, ...]]],
    *,
    page_count: int = 1,
) -> EvidenceDocument:
    atoms: list[EvidenceAtom] = []
    regions: list[EvidenceRegion] = []
    for region_index, (page, text, box, source_objects) in enumerate(entries, start=1):
        atom_ids: list[str] = []
        character_width = (box.right - box.left) / len(text)
        for character_index, (character, source_object) in enumerate(
            zip(text, source_objects, strict=True)
        ):
            ordinal = len(atoms)
            atom_id = f"atom_{ordinal + 1:016x}"
            atom_ids.append(atom_id)
            atoms.append(
                EvidenceAtom(
                    atom_id=atom_id,
                    ordinal=ordinal,
                    page=page,
                    bbox=SourceBox(
                        left=box.left + character_width * character_index,
                        bottom=box.bottom,
                        right=box.left + character_width * (character_index + 1),
                        top=box.top,
                    ),
                    text=character,
                    kind="whitespace" if character.isspace() else "character",
                    authority="embedded",
                    source_object=source_object,
                    source_index=character_index,
                )
            )
        regions.append(
            EvidenceRegion(
                region_id=f"region_{region_index:016x}",
                page=page,
                bbox=box,
                atom_ids=tuple(atom_ids),
                authority="embedded",
            )
        )
    return EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=page_count,
        parser_versions={"fixture": "1"},
        atoms=tuple(atoms),
        regions=tuple(regions),
    )


def region_texts(document: EvidenceDocument, region_ids: tuple[str, ...], layout) -> list[str]:
    catalog = {atom.atom_id: atom.text for atom in document.atoms}
    regions = {region.region_id: region for region in layout.regions}
    return ["".join(catalog[atom_id] for atom_id in regions[item].atom_ids) for item in region_ids]


def test_text_object_fragmentation_does_not_create_words_or_paragraphs() -> None:
    evidence = document_from_regions(
        [
            (
                1,
                "데이터시맨틱",
                SourceBox(left=0.1, bottom=0.7, right=0.5, top=0.75),
                (0, 0, 1, 2, 2, 3),
            )
        ]
    )

    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    assert len(layout.lines) == 1
    assert len(layout.regions) == 1
    assert layout.regions[0].atom_ids == tuple(atom.atom_id for atom in evidence.atoms)


def test_two_columns_do_not_interleave_rows_in_reading_order() -> None:
    evidence = document_from_regions(
        [
            (1, "A", SourceBox(left=0.1, bottom=0.8, right=0.2, top=0.85), (0,)),
            (1, "B", SourceBox(left=0.1, bottom=0.6, right=0.2, top=0.65), (1,)),
            (1, "C", SourceBox(left=0.6, bottom=0.8, right=0.7, top=0.85), (2,)),
            (1, "D", SourceBox(left=0.6, bottom=0.6, right=0.7, top=0.65), (3,)),
        ]
    )

    layout = normalize_layout(evidence, StructureDocument(blocks=()))
    order = topological_region_order(layout)

    assert region_texts(evidence, order, layout) == ["A", "B", "C", "D"]
    assert [region.column for region in layout.regions] == [0, 0, 1, 1]


def test_repeated_page_edge_text_is_marked_as_header_only() -> None:
    entries: list[tuple[int, str, SourceBox, tuple[int, ...]]] = []
    for page in range(1, 6):
        entries.extend(
            [
                (
                    page,
                    "H",
                    SourceBox(left=0.1, bottom=0.94, right=0.2, top=0.98),
                    (page * 2,),
                ),
                (
                    page,
                    "B",
                    SourceBox(left=0.1, bottom=0.5, right=0.2, top=0.55),
                    (page * 2 + 1,),
                ),
            ]
        )
    evidence = document_from_regions(entries, page_count=5)

    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    repeated = [region for region in layout.regions if region.repeated_edge]
    assert len(repeated) == 5
    assert {region.hint for region in repeated} == {"page_header"}
    assert all(region.bbox.top >= 0.9 for region in repeated)


def test_unique_numeric_page_labels_are_marked_as_page_numbers() -> None:
    evidence = document_from_regions(
        [
            (
                page,
                str(page),
                SourceBox(left=0.9, bottom=0.02, right=0.92, top=0.04),
                (page,),
            )
            for page in range(1, 4)
        ],
        page_count=3,
    )

    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    assert all(region.repeated_edge for region in layout.regions)
    assert {region.hint for region in layout.regions} == {"page_number"}


def test_normalization_never_synthesizes_or_drops_atom_ids() -> None:
    evidence = document_from_regions(
        [
            (1, "AB", SourceBox(left=0.1, bottom=0.7, right=0.3, top=0.75), (0, 1)),
            (2, "CD", SourceBox(left=0.1, bottom=0.7, right=0.3, top=0.75), (2, 3)),
        ],
        page_count=2,
    )

    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    allocated = [atom_id for region in layout.regions for atom_id in region.atom_ids]
    assert sorted(allocated) == sorted(atom.atom_id for atom in evidence.atoms)


def test_degenerate_trailing_whitespace_is_owned_by_layout() -> None:
    evidence = document_from_regions(
        [
            (
                1,
                "A ",
                SourceBox(left=0.1, bottom=0.7, right=0.3, top=0.75),
                (0, 1),
            )
        ]
    )
    trailing = evidence.atoms[-1].model_copy(
        update={"bbox": SourceBox(left=0.3, bottom=0.7, right=0.3, top=0.7)}
    )
    evidence = evidence.model_copy(update={"atoms": (*evidence.atoms[:-1], trailing)})

    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    allocated = [atom_id for region in layout.regions for atom_id in region.atom_ids]
    assert allocated == [atom.atom_id for atom in evidence.atoms]


def test_table_hint_merges_cell_columns_into_one_layout_region() -> None:
    evidence = document_from_regions(
        [
            (1, "A", SourceBox(left=0.1, bottom=0.7, right=0.3, top=0.8), (0,)),
            (1, "B", SourceBox(left=0.5, bottom=0.7, right=0.7, top=0.8), (1,)),
            (1, "C", SourceBox(left=0.1, bottom=0.5, right=0.3, top=0.6), (2,)),
            (1, "D", SourceBox(left=0.5, bottom=0.5, right=0.7, top=0.6), (3,)),
        ]
    )
    table_box = SourceBox(left=0.05, bottom=0.45, right=0.75, top=0.85)
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="table",
                order=0,
                page=1,
                bbox=table_box,
                text_hint="A B C D",
                table=StructureTable(
                    row_count=2,
                    column_count=2,
                    cells=(
                        StructureCell(0, 1, 0, 1, "A", True, None),
                        StructureCell(0, 1, 1, 2, "B", True, None),
                        StructureCell(1, 2, 0, 1, "C", False, None),
                        StructureCell(1, 2, 1, 2, "D", False, None),
                    ),
                ),
            ),
        )
    )

    layout = normalize_layout(evidence, hints)
    table_regions = [region for region in layout.regions if region.hint == "table"]

    assert len(table_regions) == 1
    assert set(table_regions[0].atom_ids) == {atom.atom_id for atom in evidence.atoms}
    assert len(table_regions[0].line_ids) == 4


def test_heading_hint_merges_fragments_across_layout_columns() -> None:
    evidence = document_from_regions(
        [
            (1, "(비즈니스 로직)", SourceBox(left=0.6, bottom=0.8, right=0.9, top=0.85), (1,) * 9),
            (1, "5. 업무 규칙", SourceBox(left=0.1, bottom=0.8, right=0.4, top=0.85), (0,) * 8),
        ]
    )
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="heading",
                order=0,
                page=1,
                bbox=SourceBox(left=0.05, bottom=0.75, right=0.95, top=0.9),
                text_hint="5. 업무 규칙 (비즈니스 로직)",
                heading_level=1,
            ),
        )
    )

    layout = normalize_layout(evidence, hints)

    assert len(layout.regions) == 1
    assert layout.regions[0].hint == "heading"
    assert set(layout.regions[0].atom_ids) == {atom.atom_id for atom in evidence.atoms}
    atom_text = {atom.atom_id: atom.text for atom in evidence.atoms}
    rendered = "".join(atom_text[atom_id] for atom_id in layout.regions[0].atom_ids)
    assert "".join(rendered.split()) == "".join(hints.blocks[0].text_hint.split())


def test_incidental_table_edge_overlap_does_not_reclassify_heading() -> None:
    evidence = document_from_regions(
        [
            (
                1,
                "제목",
                SourceBox(left=0.1, bottom=0.76, right=0.4, top=0.84),
                (0, 0),
            ),
            (
                1,
                "표",
                SourceBox(left=0.1, bottom=0.50, right=0.4, top=0.60),
                (1,),
            ),
        ]
    )
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="table",
                order=0,
                page=1,
                bbox=SourceBox(left=0.1, bottom=0.50, right=0.4, top=0.77),
                text_hint="표",
                table=StructureTable(
                    row_count=1,
                    column_count=1,
                    cells=(StructureCell(0, 1, 0, 1, "표", True, None),),
                ),
            ),
        )
    )

    layout = normalize_layout(evidence, hints)
    atom_text = {atom.atom_id: atom.text for atom in evidence.atoms}
    by_text = {
        "".join(atom_text[atom_id] for atom_id in region.atom_ids): region
        for region in layout.regions
    }

    assert by_text["제목"].hint is None
    assert by_text["표"].hint == "table"
