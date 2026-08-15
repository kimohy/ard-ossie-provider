from __future__ import annotations

import random

from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
)
from ard_ossie.semantic.layout import normalize_layout
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.spacing import KiwiSpacingScorer, build_spacing_candidate_set
from ard_ossie.semantic.structure import StructureDocument

SOURCE_HASH = "e" * 64
REGION_ID = "region_4000000000000001"


class ReviewedSpacingScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        return ("데이터 시맨틱 모델을 구성한다",)


def korean_evidence(text: str) -> EvidenceDocument:
    atoms: list[EvidenceAtom] = []
    width = 0.8 / len(text)
    for index, character in enumerate(text):
        atoms.append(
            EvidenceAtom(
                atom_id=f"atom_{index + 1:016x}",
                ordinal=index,
                page=1,
                bbox=SourceBox(
                    left=0.1 + index * width,
                    bottom=0.7,
                    right=0.1 + (index + 1) * width,
                    top=0.75,
                ),
                text=character,
                kind="whitespace" if character.isspace() else "character",
                authority="embedded",
                source_object=index,
                source_index=index,
            )
        )
    return EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=tuple(atoms),
        regions=(
            EvidenceRegion(
                region_id=REGION_ID,
                page=1,
                bbox=SourceBox(left=0.1, bottom=0.7, right=0.9, top=0.75),
                atom_ids=tuple(atom.atom_id for atom in atoms),
                authority="embedded",
            ),
        ),
    )


def test_kiwi_candidate_removes_pdf_inserted_korean_spaces() -> None:
    evidence = korean_evidence("데 이 터 시 맨 틱 모 델 을 구 성 한 다")
    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    candidate_set = build_spacing_candidate_set(
        region=layout.regions[0],
        evidence=evidence,
        layout=layout,
        scorer=KiwiSpacingScorer(),
    )

    assert "데이터 시맨틱 모델을 구성한다" in {
        candidate.rendered_text for candidate in candidate_set.candidates
    }
    assert all(
        "".join(candidate.rendered_text.split()) == "데이터시맨틱모델을구성한다"
        for candidate in candidate_set.candidates
    )


def test_spacing_candidates_keep_latin_number_unit_and_punctuation_characters() -> None:
    evidence = korean_evidence("CTR 10 % ( 합 성 )")
    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    candidate_set = build_spacing_candidate_set(
        region=layout.regions[0],
        evidence=evidence,
        layout=layout,
        scorer=KiwiSpacingScorer(),
    )

    assert all(
        "".join(candidate.rendered_text.split()) == "CTR10%(합성)"
        for candidate in candidate_set.candidates
    )
    assert any("10%" in candidate.rendered_text for candidate in candidate_set.candidates)


def test_clear_geometry_and_kiwi_agreement_is_ranked_first() -> None:
    evidence = korean_evidence("데이터 시맨틱 모델")
    layout = normalize_layout(evidence, StructureDocument(blocks=()))

    candidate_set = build_spacing_candidate_set(
        region=layout.regions[0],
        evidence=evidence,
        layout=layout,
        scorer=KiwiSpacingScorer(),
    )

    assert candidate_set.candidates[0].rendered_text == "데이터 시맨틱 모델"
    assert candidate_set.candidates[0].score >= 0.82


def test_domain_compounds_are_not_split_during_korean_spacing_repair() -> None:
    scorer = KiwiSpacingScorer()

    proposals = scorer.propose(
        "개 인정보와 실제 매체명, 결과값과 임계값을 선 집계한다",
        ("개 인정보와 실제 매체명, 결과값과 임계값을 선 집계한다",),
    )

    assert "개인정보와 실제 매체명, 결과값과 임계값을 선집계한다" in proposals


def test_object_fragmentation_and_small_box_jitter_preserve_spacing_candidates() -> None:
    source = korean_evidence("데 이 터 시 맨 틱 모 델 을 구 성 한 다")
    expected_characters = "데이터시맨틱모델을구성한다"

    for seed in range(100):
        generator = random.Random(seed)
        atoms = tuple(
            atom.model_copy(
                update={
                    "source_object": generator.randrange(0, 12),
                    "bbox": atom.bbox.model_copy(
                        update={
                            "left": atom.bbox.left + generator.uniform(-0.002, 0.002),
                            "right": atom.bbox.right + generator.uniform(-0.002, 0.002),
                        }
                    ),
                }
            )
            for atom in source.atoms
        )
        evidence = source.model_copy(update={"atoms": atoms})
        layout = normalize_layout(evidence, StructureDocument(blocks=()))

        candidate_set = build_spacing_candidate_set(
            region=layout.regions[0],
            evidence=evidence,
            layout=layout,
            scorer=ReviewedSpacingScorer(),
        )

        assert "데이터 시맨틱 모델을 구성한다" in {
            candidate.rendered_text for candidate in candidate_set.candidates
        }
        assert all(
            "".join(candidate.rendered_text.split()) == expected_characters
            for candidate in candidate_set.candidates
        )
