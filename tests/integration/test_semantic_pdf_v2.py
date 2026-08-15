from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import ard_ossie.semantic.parser as semantic_parser
from ard_ossie.docling_parser import DoclingParser, ParsedDocument
from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.pipeline import _semantic_hard_findings
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
)
from ard_ossie.semantic.models import (
    ExtractionMode,
    NativeDocument,
    SourceBox,
    SourceSpan,
    make_span_id,
)
from ard_ossie.semantic.pipeline_v2 import (
    SemanticPipelineMode,
    canonical_fidelity_report,
    parse_semantic_pdf_v2,
)
from ard_ossie.semantic.structure import StructureDocument

SOURCE_HASH = hashlib.sha256(b"fixture").hexdigest()


class StableSpacingScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        return (text,)


class AmbiguousSpacingScorer:
    def propose(self, text: str, line_chunks: tuple[str, ...]) -> tuple[str, ...]:
        return ()


def _source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "semantic.pdf"
    path.write_bytes(b"fixture")
    return SourceFile(
        path=path,
        relative_path="sources/semantic.pdf",
        role=SourceRole.SEMANTIC_DOCUMENT,
        sha256=SOURCE_HASH,
        size_bytes=7,
        snapshot=b"fixture",
    )


def _extracted() -> ExtractedEvidence:
    text = "이 문장은 충분히 긴 일반 본문 문장으로 구성되어 있으며 안정적으로 처리된다."
    atoms = tuple(
        EvidenceAtom(
            atom_id=f"atom_{index + 1:016x}",
            ordinal=index,
            page=1,
            bbox=SourceBox(
                left=0.05 + index * 0.01,
                bottom=0.7,
                right=0.06 + index * 0.01,
                top=0.75,
            ),
            text=character,
            kind="whitespace" if character.isspace() else "character",
            authority="embedded",
            source_object=0,
            source_index=index,
        )
        for index, character in enumerate(text)
    )
    box = SourceBox(left=0.05, bottom=0.7, right=0.95, top=0.75)
    return ExtractedEvidence(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=atoms,
        regions=(
            EvidenceRegion(
                region_id="region_0000000000000001",
                page=1,
                bbox=box,
                atom_ids=tuple(atom.atom_id for atom in atoms),
                authority="embedded",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("mode", "expected_markdown"),
    [
        (SemanticPipelineMode.LEGACY, "legacy output\n"),
        (SemanticPipelineMode.SHADOW, "legacy output\n"),
        (
            SemanticPipelineMode.CANDIDATE,
            "이 문장은 충분히 긴 일반 본문 문장으로 구성되어 있으며 안정적으로 처리된다\\.\n",
        ),
    ],
)
def test_pdf_pipeline_mode_controls_publication(
    tmp_path: Path,
    mode: SemanticPipelineMode,
    expected_markdown: str,
) -> None:
    result = parse_semantic_pdf_v2(
        _source(tmp_path),
        hints=StructureDocument(blocks=()),
        mode=mode,
        legacy_markdown="legacy output\n",
        extracted_evidence=_extracted(),
        spacing_scorer=StableSpacingScorer(),
    )

    assert result.markdown == expected_markdown
    assert result.mode is mode
    assert result.validation.status == "verified"


def test_shadow_difference_never_changes_published_markdown(tmp_path: Path) -> None:
    result = parse_semantic_pdf_v2(
        _source(tmp_path),
        hints=StructureDocument(blocks=()),
        mode="shadow",
        legacy_markdown="published legacy\n",
        extracted_evidence=_extracted(),
        spacing_scorer=StableSpacingScorer(),
    )

    assert result.markdown == "published legacy\n"
    assert result.canonical_markdown != result.markdown
    assert result.semantic_diff.changed is True


def test_canonical_fidelity_maps_verified_atom_coverage() -> None:
    result = parse_semantic_pdf_v2(
        SourceFile(
            path=Path("semantic.pdf"),
            relative_path="sources/semantic.pdf",
            role=SourceRole.SEMANTIC_DOCUMENT,
            sha256=SOURCE_HASH,
            size_bytes=7,
            snapshot=b"fixture",
        ),
        hints=StructureDocument(blocks=()),
        mode="candidate",
        legacy_markdown="legacy\n",
        extracted_evidence=_extracted(),
        spacing_scorer=StableSpacingScorer(),
    )

    fidelity = canonical_fidelity_report(result.evidence, result.canonical, result.validation)

    assert fidelity.status == "PASS"
    assert fidelity.source_text_coverage == 1.0
    assert fidelity.degraded_block_count == 0
    assert fidelity.paragraph_count == 1


class ExplodingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def repair(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("free-form repair must not run")

    def correct(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("free-form correction must not run")


def test_candidate_mode_never_invokes_legacy_free_form_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    pipeline_result = parse_semantic_pdf_v2(
        source,
        hints=StructureDocument(blocks=()),
        mode="candidate",
        extracted_evidence=_extracted(),
        spacing_scorer=StableSpacingScorer(),
    )
    native = NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={},
        spans=(),
        groups=(),
        tables=(),
    )
    monkeypatch.setattr(
        semantic_parser,
        "_native_and_structure",
        lambda *_args, **_kwargs: (native, StructureDocument(blocks=())),
    )
    monkeypatch.setattr(
        semantic_parser,
        "parse_semantic_pdf_v2",
        lambda *_args, **_kwargs: pipeline_result,
    )
    repair = ExplodingPlanner()
    correction = ExplodingPlanner()

    parsed = DoclingParser(
        structure_repair_planner=repair,  # type: ignore[arg-type]
        ocr_correction_planner=correction,  # type: ignore[arg-type]
        semantic_pipeline_mode="candidate",
    ).parse(source)

    assert parsed.markdown == pipeline_result.canonical_markdown
    assert parsed.semantic_validation is not None
    assert parsed.semantic_validation.status == "verified"
    assert repair.calls == 0
    assert correction.calls == 0


def test_docx_ignores_candidate_pdf_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.docx"
    path.write_bytes(b"fixture")
    source = SourceFile(
        path=path,
        relative_path="sources/semantic.docx",
        role=SourceRole.SEMANTIC_DOCUMENT,
        sha256=SOURCE_HASH,
        size_bytes=7,
        snapshot=b"fixture",
    )
    span = SourceSpan(
        span_id=make_span_id(SOURCE_HASH, 0),
        ordinal=0,
        text="DOCX 원문",
        text_hash=hashlib.sha256("DOCX 원문".encode()).hexdigest(),
    )
    native = NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.DOCX_XML,
        page_count=0,
        parser_versions={},
        spans=(span,),
        groups=(),
        tables=(),
    )
    monkeypatch.setattr(
        semantic_parser,
        "_native_and_structure",
        lambda *_args, **_kwargs: (native, StructureDocument(blocks=())),
    )
    monkeypatch.setattr(
        semantic_parser,
        "parse_semantic_pdf_v2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PDF pipeline must not run for DOCX")
        ),
    )

    parsed = DoclingParser(semantic_pipeline_mode="candidate").parse(source)

    assert "DOCX 원문" in parsed.markdown
    assert parsed.semantic_pipeline_result is None
    assert parsed.semantic_validation is None


def test_review_required_candidate_report_is_rejected_by_hard_quality_gate(
    tmp_path: Path,
) -> None:
    result = parse_semantic_pdf_v2(
        _source(tmp_path),
        hints=StructureDocument(blocks=()),
        mode="candidate",
        extracted_evidence=_extracted(),
        spacing_scorer=AmbiguousSpacingScorer(),
    )
    fidelity = canonical_fidelity_report(result.evidence, result.canonical, result.validation)
    parsed = ParsedDocument(
        role=SourceRole.SEMANTIC_DOCUMENT,
        source_hash=SOURCE_HASH,
        markdown=result.markdown,
        semantic_fidelity=fidelity,
        semantic_validation=result.validation,
    )

    findings = _semantic_hard_findings(parsed)

    assert result.validation.status == "review_required"
    assert [item.code for item in findings] == ["SEMANTIC_CANDIDATE_REVIEW_REQUIRED"]
