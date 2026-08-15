#!/usr/bin/env python3
"""Verify the real Issue #3 semantic PDF artifact without exposing source content."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ard_ossie.canonical import canonical_hash
from ard_ossie.docling_parser import DoclingParser
from ard_ossie.ingestion import (
    SourceFile,
    SourceRole,
    scan_sources,
    snapshot_source_file,
)
from ard_ossie.llm.contracts import LLMMetadata, LLMResult
from ard_ossie.semantic.adjudication import DecisionRecord, DecisionReport
from ard_ossie.semantic.evidence import ExtractedEvidence
from ard_ossie.semantic.evidence_sources import extract_pdf_evidence
from ard_ossie.semantic.models import ExtractionMode, SemanticFidelityReport
from ard_ossie.semantic.pipeline_v2 import SemanticPipelineResult, parse_semantic_pdf_v2
from ard_ossie.semantic.structure import (
    StructureBlock,
    StructureCell,
    StructureDocument,
    StructureTable,
)

RAW_HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
GFM_SEPARATOR_ROW = re.compile(r"(?m)^\|(?:\s*:?-{3,}:?\s*\|)+$")


class Issue3VerificationError(RuntimeError):
    """Raised when a generated Issue #3 artifact violates its contract."""


class _ProviderMustNotRun:
    def __init__(
        self,
        fidelity: SemanticFidelityReport,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        reusable = next(
            (
                page
                for page in fidelity.ocr_corrections
                if page.outcome in {"applied", "reused"}
            ),
            None,
        )
        self.provider = (
            provider
            or (reusable.provider if reusable is not None else "acceptance-stub")
        )
        self.model = model or (reusable.model if reusable is not None else "acceptance-stub")

    def capabilities(self) -> dict[str, object]:
        return {"provider": self.provider, "model": self.model, "vision": True}

    def generate_structured(self, **_kwargs: object) -> object:
        raise Issue3VerificationError("ISSUE_3_PROVIDER_CALLED")

    def generate_multimodal_structured(self, **_kwargs: object) -> object:
        raise Issue3VerificationError("ISSUE_3_PROVIDER_CALLED")


class ReplayCandidateProvider:
    LOW_CONFIDENCE_PRIMARY = {
        "candidate_set_78620dc093a748fe": 0.70,
        "candidate_set_6d08c750276170e3": 0.74,
    }
    APPROVED_CANDIDATES = {
        "candidate_set_78620dc093a748fe": "candidate_e6954a2fd1fdfe6f",
        "candidate_set_6d08c750276170e3": "candidate_2eefa1840c448a35",
    }

    def __init__(self) -> None:
        self.calls = 0
        self.recovery_calls = 0
        self.generation_calls = 0
        self.verification_calls = 0
        self.table_spacing_generation_calls = 0
        self.table_spacing_verification_calls = 0
        self.table_spacing_candidate_sets: set[str] = set()
        self.max_candidate_count = 0

    def capabilities(self) -> dict[str, object]:
        return {
            "provider": "openai_compatible",
            "model": "semantic-replay",
            "structured_output": "json_schema",
        }

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        del schema
        request = json.loads(messages[-1]["content"])
        candidates = request["candidates"]
        candidate_set_id = request["candidate_set_id"]
        if not isinstance(candidates, list) or not candidates:
            raise Issue3VerificationError("EVIDENCE_REPLAY_CANDIDATES_EMPTY")
        self.calls += 1
        task = request.get("task")
        if task == "generate_whitespace_repair":
            self.generation_calls += 1
            table_composite = candidate_set_id in self.table_spacing_candidate_sets
            if table_composite:
                self.table_spacing_generation_calls += 1
            anchor_id = request["anchor_candidate_id"]
            anchor = next(
                candidate
                for candidate in candidates
                if candidate["candidate_id"] == anchor_id
            )
            structured = {
                "rendered_text": anchor["rendered_text"],
                "confidence": 0.99,
                "repair_reasons": (
                    ["korean_morphology", "table_cell_boundary"]
                    if table_composite
                    else ["korean_morphology"]
                ),
            }
            return self._result(structured)
        if task == "verify_whitespace_repair":
            self.verification_calls += 1
            if candidate_set_id in self.table_spacing_candidate_sets:
                self.table_spacing_verification_calls += 1
            structured = {
                "candidate_id": request["generated_candidate_id"],
                "confidence": 0.99,
                "validation_codes": [],
            }
            return self._result(structured)

        phase = request["phase"]
        table_composite = any(
            "table_cell_composite" in candidate.get("features", {})
            for candidate in candidates
        )
        if table_composite:
            self.table_spacing_candidate_sets.add(candidate_set_id)
        if phase in {"recovery", "tiebreak"}:
            self.recovery_calls += 1
        self.max_candidate_count = max(self.max_candidate_count, len(candidates))
        approved = self.APPROVED_CANDIDATES.get(candidate_set_id)
        if approved is not None:
            if approved not in {candidate["candidate_id"] for candidate in candidates}:
                raise Issue3VerificationError("EVIDENCE_REPLAY_APPROVED_CANDIDATE_MISSING")
            selected = approved
        else:
            selected = max(
                candidates,
                key=lambda candidate: (
                    candidate.get("score", 0.0),
                    len(candidate.get("features", {})),
                    float(
                        re.match(
                            r"^\d+(?:\.\d+)*[.)]\s",
                            str(candidate.get("rendering", "")),
                        )
                        is not None
                    ),
                    float("source_spacing" in candidate.get("features", {})),
                    candidate["candidate_id"],
                ),
            )["candidate_id"]
        confidence = (
            0.70
            if phase == "primary" and table_composite
            else self.LOW_CONFIDENCE_PRIMARY.get(candidate_set_id, 0.99)
            if phase == "primary"
            else 0.99
        )
        structured = {"candidate_id": selected, "confidence": confidence}
        return self._result(structured)

    @staticmethod
    def _result(structured: dict[str, object]) -> LLMResult:
        return LLMResult(
            text=json.dumps(structured, sort_keys=True),
            structured=structured,
            metadata=LLMMetadata(
                profile="semantic-replay",
                provider="openai_compatible",
                model="semantic-replay",
                elapsed_ms=0,
            ),
        )

    def generate_multimodal_structured(self, **_kwargs: object) -> object:
        raise Issue3VerificationError("ISSUE_3_PROVIDER_CALLED")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise Issue3VerificationError(code)


def capture_evidence(
    source: SourceFile | Path,
    destination: Path,
    *,
    hints: StructureDocument | None = None,
    pdfium: Any | None = None,
) -> Path:
    active_source = (
        source
        if isinstance(source, SourceFile)
        else snapshot_source_file(
            source,
            role=SourceRole.SEMANTIC_DOCUMENT,
            relative_path=source.name,
        )
    )
    evidence = extract_pdf_evidence(active_source, pdfium=pdfium)
    if evidence.source_hash != active_source.sha256:
        raise ValueError("EVIDENCE_REPLAY_SOURCE_HASH_MISMATCH")
    if hints is None:
        from ard_ossie.semantic.parser import _ordinary_structure

        hints = _ordinary_structure(active_source, converter=None)
    payload = {
        "capture_schema": "semantic-evidence-replay-v1",
        **evidence.model_dump(mode="json"),
        "structure_hints": [_structure_block_payload(block) for block in hints.blocks],
    }
    payload["capture_sha256"] = canonical_hash(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def load_evidence_replay(path: Path) -> ExtractedEvidence:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("EVIDENCE_REPLAY_INVALID") from error
    if not isinstance(payload, dict) or payload.get("capture_schema") != (
        "semantic-evidence-replay-v1"
    ):
        raise ValueError("EVIDENCE_REPLAY_SCHEMA_INVALID")
    _reject_sensitive_capture_keys(payload)
    capture_hash = payload.get("capture_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "capture_sha256"}
    if capture_hash != canonical_hash(unsigned):
        raise ValueError("EVIDENCE_REPLAY_HASH_MISMATCH")
    evidence_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"capture_schema", "capture_sha256", "structure_hints"}
    }
    return ExtractedEvidence.model_validate(evidence_payload)


def load_structure_replay(path: Path) -> StructureDocument:
    payload = json.loads(path.read_text(encoding="utf-8"))
    load_evidence_replay(path)
    return StructureDocument(
        blocks=tuple(_structure_block_from_payload(item) for item in payload["structure_hints"])
    )


def run_evidence_replay(
    path: Path,
    *,
    trusted_decisions: tuple[DecisionRecord, ...] = (),
) -> tuple[SemanticPipelineResult, ReplayCandidateProvider]:
    evidence = load_evidence_replay(path)
    provider = ReplayCandidateProvider()
    source = SourceFile.model_construct(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=Path("issue-3.pdf"),
        relative_path="sources/semantic/issue-3.pdf",
        sha256=evidence.source_hash,
        size_bytes=0,
        snapshot=b"",
    )
    result = parse_semantic_pdf_v2(
        source,
        hints=load_structure_replay(path),
        mode="candidate",
        provider=provider,
        trusted_decisions=trusted_decisions,
        extracted_evidence=evidence,
    )
    return result, provider


def verify_evidence_replay(evidence_path: Path, golden_path: Path) -> dict[str, object]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    result, provider = run_evidence_replay(evidence_path)
    repeated, repeated_provider = run_evidence_replay(
        evidence_path,
        trusted_decisions=result.decisions.decisions,
    )
    headings = [block.text for block in result.canonical.blocks if block.kind == "heading"]
    heading_levels = [
        block.heading_level
        for block in result.canonical.blocks
        if block.kind == "heading"
    ]
    tables = [
        [block.row_count, block.column_count]
        for block in result.canonical.blocks
        if block.kind == "table"
    ]
    plain_text = "\n".join(
        [
            *(block.text for block in result.canonical.blocks),
            *(
                cell.text
                for block in result.canonical.blocks
                for cell in block.cells
            ),
        ]
    )
    cell_texts = [
        cell.text
        for block in result.canonical.blocks
        for cell in block.cells
    ]
    _require(result.validation.status == "verified", "EVIDENCE_REPLAY_NOT_VERIFIED")
    _require(result.validation.character_coverage == 1.0, "EVIDENCE_REPLAY_COVERAGE")
    _require(result.validation.missing_atom_count == 0, "EVIDENCE_REPLAY_MISSING")
    _require(result.validation.duplicate_atom_count == 0, "EVIDENCE_REPLAY_DUPLICATE")
    _require(result.validation.degraded_block_count == 0, "EVIDENCE_REPLAY_DEGRADED")
    _require(result.evidence.source_hash == golden["source_hash"], "EVIDENCE_REPLAY_SOURCE")
    _require(headings == golden["headings"], "EVIDENCE_REPLAY_HEADINGS")
    _require(
        heading_levels == golden["heading_levels"],
        "EVIDENCE_REPLAY_HEADING_LEVELS",
    )
    _require(tables == golden["table_dimensions"], "EVIDENCE_REPLAY_TABLES")
    _require(
        all(value in plain_text for value in golden["required_phrases"]),
        "EVIDENCE_REPLAY_REQUIRED_PHRASE",
    )
    _require(
        all(value in cell_texts for value in golden["required_repaired_table_cells"]),
        "EVIDENCE_REPLAY_REPAIRED_TABLE_CELL",
    )
    _require(
        all(value in cell_texts for value in golden["required_unchanged_table_cells"]),
        "EVIDENCE_REPLAY_UNCHANGED_TABLE_CELL",
    )
    _require(
        all(
            fragment not in plain_text
            for fragment in golden["forbidden_table_cell_fragments"]
        ),
        "EVIDENCE_REPLAY_TABLE_CELL_FRAGMENT",
    )
    _require(
        all(value not in result.markdown for value in golden["forbidden_strings"]),
        "EVIDENCE_REPLAY_FORBIDDEN_STRING",
    )
    _require("<pre" not in result.markdown, "EVIDENCE_REPLAY_RAW_HTML")
    for region_id, expected_rows in golden["exact_tables"].items():
        _require(
            _canonical_table_rows(result, region_id) == expected_rows,
            "EVIDENCE_REPLAY_EXACT_TABLE",
        )
        block = next(item for item in result.canonical.blocks if item.region_id == region_id)
        _require(
            block.text == "\n".join("\t".join(row) for row in expected_rows),
            "EVIDENCE_REPLAY_EXACT_TABLE_TEXT",
        )
    _require(
        result.validation.canonical_hash == repeated.validation.canonical_hash,
        "EVIDENCE_REPLAY_NONDETERMINISTIC",
    )
    table_decisions = [
        decision for decision in result.decisions.decisions if decision.decision_type == "table"
    ]
    _require(
        bool(table_decisions)
        and all(decision.source == "deterministic" for decision in table_decisions),
        "EVIDENCE_REPLAY_TABLE_NOT_DETERMINISTIC",
    )
    _require(
        provider.table_spacing_generation_calls > 0,
        "EVIDENCE_REPLAY_TABLE_SPACING_GENERATION_MISSING",
    )
    _require(
        provider.table_spacing_verification_calls
        == provider.table_spacing_generation_calls,
        "EVIDENCE_REPLAY_TABLE_SPACING_VERIFICATION_MISMATCH",
    )
    _require(repeated_provider.calls == 0, "EVIDENCE_REPLAY_CACHE_MISS")
    return {
        "status": result.validation.status,
        "canonical_hash": result.validation.canonical_hash,
        "heading_count": len(headings),
        "heading_levels": heading_levels,
        "table_count": len(tables),
        "model_calls": provider.calls,
        "recovered_decision_count": sum(
            decision.recovery_status == "recovered" for decision in result.decisions.decisions
        ),
        "recovery_model_calls": provider.recovery_calls,
        "generation_model_calls": provider.generation_calls,
        "verification_model_calls": provider.verification_calls,
        "affected_table_spacing_count": len(provider.table_spacing_candidate_sets),
        "table_spacing_generation_calls": provider.table_spacing_generation_calls,
        "table_spacing_verification_calls": provider.table_spacing_verification_calls,
        "cache_model_calls": repeated_provider.calls,
        "exact_table_count": len(golden["exact_tables"]),
        "max_candidate_count": provider.max_candidate_count,
    }


def _canonical_table_rows(
    result: SemanticPipelineResult,
    region_id: str,
) -> list[list[str]]:
    block = next(item for item in result.canonical.blocks if item.region_id == region_id)
    rows = [["" for _ in range(block.column_count or 0)] for _ in range(block.row_count or 0)]
    for cell in block.cells:
        rows[cell.start_row][cell.start_column] = cell.text
    return rows


def _structure_block_payload(block: StructureBlock) -> dict[str, object]:
    return {
        "kind": block.kind,
        "order": block.order,
        "page": block.page,
        "bbox": block.bbox.model_dump(mode="json") if block.bbox is not None else None,
        "text_hint": block.text_hint,
        "heading_level": block.heading_level,
        "list_kind": block.list_kind,
        "list_depth": block.list_depth,
        "table": (
            {
                "row_count": block.table.row_count,
                "column_count": block.table.column_count,
                "cells": [
                    {
                        "start_row": cell.start_row,
                        "end_row": cell.end_row,
                        "start_column": cell.start_column,
                        "end_column": cell.end_column,
                        "text_hint": cell.text_hint,
                        "column_header": cell.column_header,
                        "bbox": (
                            cell.bbox.model_dump(mode="json")
                            if cell.bbox is not None
                            else None
                        ),
                    }
                    for cell in block.table.cells
                ],
            }
            if block.table is not None
            else None
        ),
    }


def _structure_block_from_payload(payload: dict[str, object]) -> StructureBlock:
    from ard_ossie.semantic.models import SourceBox

    table_payload = payload.get("table")
    table = None
    if isinstance(table_payload, dict):
        table = StructureTable(
            row_count=int(table_payload["row_count"]),
            column_count=int(table_payload["column_count"]),
            cells=tuple(
                StructureCell(
                    start_row=int(cell["start_row"]),
                    end_row=int(cell["end_row"]),
                    start_column=int(cell["start_column"]),
                    end_column=int(cell["end_column"]),
                    text_hint=str(cell["text_hint"]),
                    column_header=bool(cell["column_header"]),
                    bbox=(
                        SourceBox.model_validate(cell["bbox"])
                        if cell.get("bbox") is not None
                        else None
                    ),
                )
                for cell in table_payload["cells"]
            ),
        )
    return StructureBlock(
        kind=payload["kind"],
        order=int(payload["order"]),
        page=int(payload["page"]) if payload.get("page") is not None else None,
        bbox=(
            SourceBox.model_validate(payload["bbox"])
            if payload.get("bbox") is not None
            else None
        ),
        text_hint=str(payload["text_hint"]),
        heading_level=(
            int(payload["heading_level"])
            if payload.get("heading_level") is not None
            else None
        ),
        list_kind=payload.get("list_kind"),
        list_depth=(
            int(payload["list_depth"])
            if payload.get("list_depth") is not None
            else None
        ),
        table=table,
    )


def _reject_sensitive_capture_keys(value: object) -> None:
    forbidden = {"page_images", "image_bytes", "credentials", "api_key"}
    if isinstance(value, dict):
        if forbidden & {str(key).casefold() for key in value}:
            raise ValueError("EVIDENCE_REPLAY_SENSITIVE_CONTENT")
        for child in value.values():
            _reject_sensitive_capture_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_capture_keys(child)


def verify_issue_3(product_root: Path) -> dict[str, object]:
    root = product_root.expanduser().resolve(strict=True)
    markdown_path = root / "generated" / "data-semantic.md"
    fidelity_path = root / "quality" / "semantic-fidelity.json"
    markdown_bytes = markdown_path.read_bytes()
    markdown = markdown_bytes.decode("utf-8", errors="strict")
    fidelity = SemanticFidelityReport.model_validate_json(fidelity_path.read_bytes())

    _require(
        fidelity.extraction_mode in {ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR},
        "ISSUE_3_NOT_PDF",
    )
    _require(fidelity.page_count == 5, "ISSUE_3_PAGE_COUNT_INVALID")
    _require(fidelity.unmatched_span_count == 0, "ISSUE_3_UNMATCHED_SPANS")
    _require(fidelity.duplicated_span_count == 0, "ISSUE_3_DUPLICATED_SPANS")
    _require(fidelity.status != "FAIL", "ISSUE_3_FIDELITY_FAILED")
    _require(RAW_HTML_TAG.search(markdown) is None, "ISSUE_3_RAW_HTML")
    _require("<pre>" not in markdown and "<br>" not in markdown, "ISSUE_3_HTML_FALLBACK")
    _require("개인정보" in markdown and "유효성" in markdown, "ISSUE_3_OCR_TEXT_INVALID")
    _require(GFM_SEPARATOR_ROW.search(markdown) is not None, "ISSUE_3_TABLE_MISSING")
    _require(
        all(
            patch.bbox is not None and bool(patch.original_text_hash)
            for page in fidelity.ocr_corrections
            for patch in page.patches
            if patch.outcome in {"applied", "reused"}
        ),
        "ISSUE_3_CORRECTION_EVIDENCE_INVALID",
    )

    source = scan_sources(root / "sources").by_role(SourceRole.SEMANTIC_DOCUMENT)
    decision_report = DecisionReport.model_validate_json(
        (root / "quality" / "decision-report.json").read_bytes()
    )
    _require(
        decision_report.source_hash == fidelity.source_hash,
        "ISSUE_3_DECISION_SOURCE_MISMATCH",
    )
    _require(
        all(
            decision.source_hash == fidelity.source_hash
            for decision in decision_report.decisions
        ),
        "ISSUE_3_DECISION_SOURCE_MISMATCH",
    )
    provider_identities = {
        (decision.provider, decision.model) for decision in decision_report.decisions
    }
    _require(len(provider_identities) == 1, "ISSUE_3_DECISION_PROVIDER_INVALID")
    provider_name, model = next(iter(provider_identities))
    provider = _ProviderMustNotRun(fidelity, provider=provider_name, model=model)
    reused = DoclingParser(
        trusted_fidelity_report=fidelity,
        semantic_pipeline_mode="candidate",
        candidate_provider=provider,
        trusted_candidate_decisions=decision_report.decisions,
    ).parse(source)
    reused_fidelity = reused.semantic_fidelity
    _require(reused_fidelity is not None, "ISSUE_3_REUSED_FIDELITY_MISSING")
    _require(reused.markdown.encode("utf-8") == markdown_bytes, "ISSUE_3_REUSE_CHANGED_MARKDOWN")

    applied_pages = {
        page.page for page in fidelity.ocr_corrections if page.outcome == "applied"
    }
    reused_pages = {
        page.page for page in reused_fidelity.ocr_corrections if page.outcome == "reused"
    }
    _require(applied_pages <= reused_pages, "ISSUE_3_APPLIED_PAGE_NOT_REUSED")
    applied_patches = {
        (page.page, patch.span_id)
        for page in fidelity.ocr_corrections
        for patch in page.patches
        if patch.outcome == "applied"
    }
    reused_patches = {
        (page.page, patch.span_id)
        for page in reused_fidelity.ocr_corrections
        for patch in page.patches
        if patch.outcome == "reused"
    }
    _require(applied_patches <= reused_patches, "ISSUE_3_APPLIED_PATCH_NOT_REUSED")

    return {
        "status": fidelity.status,
        "page_count": fidelity.page_count,
        "source_span_count": fidelity.source_span_count,
        "correction_count": fidelity.ocr_correction_applied_count,
        "reused_page_count": len(reused_pages),
        "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--product-root", type=Path)
    modes.add_argument(
        "--capture-evidence",
        nargs=2,
        metavar=("OUTPUT", "PDF"),
        type=Path,
    )
    modes.add_argument("--evidence", type=Path)
    parser.add_argument("--golden", type=Path)
    arguments = parser.parse_args()
    if arguments.capture_evidence is not None:
        output, source = arguments.capture_evidence
        capture_evidence(source, output)
        print(json.dumps({"evidence": str(output)}, sort_keys=True))
        return 0
    if arguments.evidence is not None:
        if arguments.golden is None:
            parser.error("--evidence requires --golden")
        print(
            json.dumps(
                verify_evidence_replay(arguments.evidence, arguments.golden),
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps(verify_issue_3(arguments.product_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
