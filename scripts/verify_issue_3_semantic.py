#!/usr/bin/env python3
"""Verify the real Issue #3 OCR artifact without exposing source content."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from ard_ossie.docling_parser import DoclingParser
from ard_ossie.ingestion import SourceRole, scan_sources
from ard_ossie.semantic.correction import OcrCorrectionPlanner
from ard_ossie.semantic.models import ExtractionMode, SemanticFidelityReport

RAW_HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
GFM_SEPARATOR_ROW = re.compile(r"(?m)^\|(?:\s*:?-{3,}:?\s*\|)+$")


class Issue3VerificationError(RuntimeError):
    """Raised when a generated Issue #3 artifact violates its contract."""


class _ProviderMustNotRun:
    def __init__(self, fidelity: SemanticFidelityReport) -> None:
        reusable = next(
            (
                page
                for page in fidelity.ocr_corrections
                if page.outcome in {"applied", "reused"}
            ),
            None,
        )
        self.provider = reusable.provider if reusable is not None else "acceptance-stub"
        self.model = reusable.model if reusable is not None else "acceptance-stub"

    def capabilities(self) -> dict[str, object]:
        return {"provider": self.provider, "model": self.model, "vision": True}

    def generate_structured(self, **_kwargs: object) -> object:
        raise Issue3VerificationError("ISSUE_3_PROVIDER_CALLED")

    def generate_multimodal_structured(self, **_kwargs: object) -> object:
        raise Issue3VerificationError("ISSUE_3_PROVIDER_CALLED")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise Issue3VerificationError(code)


def verify_issue_3(product_root: Path) -> dict[str, object]:
    root = product_root.expanduser().resolve(strict=True)
    markdown_path = root / "generated" / "data-semantic.md"
    fidelity_path = root / "quality" / "semantic-fidelity.json"
    markdown_bytes = markdown_path.read_bytes()
    markdown = markdown_bytes.decode("utf-8", errors="strict")
    fidelity = SemanticFidelityReport.model_validate_json(fidelity_path.read_bytes())

    _require(fidelity.extraction_mode is ExtractionMode.OCR, "ISSUE_3_NOT_OCR")
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
    provider = _ProviderMustNotRun(fidelity)
    reused = DoclingParser(
        ocr_correction_planner=OcrCorrectionPlanner(provider),
        trusted_fidelity_report=fidelity,
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
    parser.add_argument("--product-root", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(verify_issue_3(arguments.product_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
