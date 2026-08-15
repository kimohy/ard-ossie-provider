from __future__ import annotations

from ard_ossie.semantic.candidates import (
    TableCandidate,
    TableCellCandidate,
    make_candidate_id,
    make_cell_id,
)
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
)
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.spacing import build_table_spacing_candidate_set

SOURCE_HASH = "c" * 64
REGION_ID = "region_7000000000000001"
BOX = SourceBox(left=0.1, bottom=0.7, right=0.9, top=0.8)


class FixedScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        del line_chunks
        replacements = {
            "시 뮬레 이 션 비용": "시뮬레이션 비용",
            "정상 셀": "정상 셀",
        }
        return (replacements.get(text, text),)


class MutatingScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        del text, line_chunks
        return ("다른 문자열",)


class DenseScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        del line_chunks
        return ("".join(character for character in text if not character.isspace()),)


def _table_fixture(
    values: tuple[str, ...],
    *,
    spacing_integrity: float = 0.0,
) -> tuple[TableCandidate, EvidenceDocument, tuple[tuple[str, ...], ...]]:
    atoms: list[EvidenceAtom] = []
    cells: list[TableCellCandidate] = []
    cell_character_ids: list[tuple[str, ...]] = []
    for column, value in enumerate(values):
        atom_ids: list[str] = []
        character_ids: list[str] = []
        for source_index, character in enumerate(value):
            atom_id = f"atom_{len(atoms) + 1:016x}"
            atom_ids.append(atom_id)
            if not character.isspace():
                character_ids.append(atom_id)
            atoms.append(
                EvidenceAtom(
                    atom_id=atom_id,
                    ordinal=len(atoms),
                    page=1,
                    bbox=BOX,
                    text=character,
                    kind="whitespace" if character.isspace() else "character",
                    authority="embedded",
                    source_object=column,
                    source_index=source_index,
                )
            )
        cell_character_ids.append(tuple(character_ids))
        cells.append(
            TableCellCandidate(
                cell_id=make_cell_id(REGION_ID, {"column": column}),
                start_row=0,
                end_row=1,
                start_column=column,
                end_column=column + 1,
                atom_ids=tuple(atom_ids),
                rendered_text=value,
            )
        )
    region_atom_ids = tuple(atom.atom_id for atom in atoms)
    table = TableCandidate(
        candidate_id=make_candidate_id("table", REGION_ID, {"values": values}),
        region_id=REGION_ID,
        row_count=1,
        column_count=len(values),
        cells=tuple(cells),
        atom_ids=region_atom_ids,
        score=0.94,
        features={"cell_spacing_integrity": spacing_integrity},
    )
    evidence = EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=tuple(atoms),
        regions=(
            EvidenceRegion(
                region_id=REGION_ID,
                page=1,
                bbox=BOX,
                atom_ids=region_atom_ids,
                authority="embedded",
            ),
        ),
    )
    return table, evidence, tuple(cell_character_ids)


def test_table_spacing_repairs_only_suspicious_cells_with_hard_boundaries() -> None:
    table, evidence, cell_character_ids = _table_fixture(
        ("정상 셀", "시 뮬레 이 션 비용", "marketing_campaign.ca mpaign_id")
    )

    candidate_set = build_table_spacing_candidate_set(
        table=table,
        evidence=evidence,
        scorer=FixedScorer(),
    )

    assert candidate_set is not None
    source = next(
        item for item in candidate_set.candidates if "source_spacing" in item.features
    )
    repaired = next(
        item for item in candidate_set.candidates if "table_cell_repair" in item.features
    )
    assert repaired.rendered_text == (
        "정상 셀\n시뮬레이션 비용\nmarketing_campaign.campaign_id"
    )
    assert [boundary.state for boundary in repaired.boundaries].count("hard_break") == 2
    clean_pairs = set(zip(cell_character_ids[0], cell_character_ids[0][1:], strict=False))
    mutable_pairs = {
        (
            repaired.boundaries[index].left_atom_id,
            repaired.boundaries[index].right_atom_id,
        )
        for index in repaired.mutable_boundary_indexes or ()
    }
    assert clean_pairs.isdisjoint(mutable_pairs)
    assert set(source.atom_ids) == set(table.atom_ids) - {
        atom.atom_id for atom in evidence.atoms if atom.text.isspace()
    }


def test_clean_table_does_not_create_a_spacing_decision() -> None:
    table, evidence, _cell_ids = _table_fixture(("정상 셀",), spacing_integrity=1.0)

    candidate_set = build_table_spacing_candidate_set(
        table=table,
        evidence=evidence,
        scorer=FixedScorer(),
    )

    assert candidate_set is None


def test_character_mutating_table_proposal_is_ignored() -> None:
    table, evidence, _cell_ids = _table_fixture(("시 뮬레 이 션",))

    candidate_set = build_table_spacing_candidate_set(
        table=table,
        evidence=evidence,
        scorer=MutatingScorer(),
    )

    assert candidate_set is None


def test_formula_cell_is_not_densified_by_language_scorer() -> None:
    formula = "COUNT(DISTINCT campaign_id) WHERE campaign_status = Active"
    table, evidence, _cell_ids = _table_fixture((formula,))

    candidate_set = build_table_spacing_candidate_set(
        table=table,
        evidence=evidence,
        scorer=DenseScorer(),
    )

    assert candidate_set is None
