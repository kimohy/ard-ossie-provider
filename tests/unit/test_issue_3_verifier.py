from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ard_ossie.semantic.models import ExtractionMode, SemanticFidelityReport
from scripts import verify_issue_3_semantic as verifier

MARKDOWN = "# Semantics\n\n개인정보 유효성\n\n| 항목 | 값 |\n| --- | --- |\n| A | B |\n"


def _write_artifact(root: Path, mode: ExtractionMode) -> SemanticFidelityReport:
    fidelity = SemanticFidelityReport.model_validate(
        {
            "source_hash": "a" * 64,
            "extraction_mode": mode,
            "page_count": 5,
            "parser_versions": {},
            "status": "WARN" if mode is ExtractionMode.OCR else "PASS",
            "heading_count": 1,
            "paragraph_count": 1,
            "list_item_count": 0,
            "table_count": 1,
            "row_count": 2,
            "cell_count": 4,
            "source_span_count": 1,
            "preserved_span_count": 1,
            "excluded_span_count": 0,
            "unmatched_span_count": 0,
            "duplicated_span_count": 0,
            "degraded_block_count": 0,
            "source_text_coverage": 1.0,
        }
    )
    (root / "generated").mkdir(parents=True)
    (root / "quality").mkdir()
    (root / "generated" / "data-semantic.md").write_text(MARKDOWN, encoding="utf-8")
    (root / "quality" / "semantic-fidelity.json").write_text(
        fidelity.model_dump_json(),
        encoding="utf-8",
    )
    (root / "quality" / "decision-report.json").write_text(
        json.dumps(
            {
                "source_hash": "a" * 64,
                "decisions": [
                    {
                        "decision_id": "decision_0000000000000001",
                        "request_hash": "b" * 64,
                        "source_hash": "a" * 64,
                        "evidence_hash": "c" * 64,
                        "candidate_set_id": "candidate_set_0000000000000001",
                        "region_id": "region_0000000000000001",
                        "decision_type": "spacing",
                        "selected_candidate_id": "candidate_0000000000000001",
                        "outcome": "selected",
                        "source": "deterministic",
                        "confidence": 1.0,
                        "provider": "openai_compatible",
                        "model": "gpt-5.6-terra",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return fidelity


class ReusingParser:
    def __init__(
        self,
        *,
        trusted_fidelity_report: SemanticFidelityReport,
        semantic_pipeline_mode: str = "shadow",
        candidate_provider: object | None = None,
        trusted_candidate_decisions: tuple[object, ...] = (),
        **_kwargs: object,
    ) -> None:
        self.fidelity = trusted_fidelity_report
        identity = (
            candidate_provider.capabilities()  # type: ignore[attr-defined]
            if candidate_provider is not None
            else {}
        )
        self.reusable = (
            semantic_pipeline_mode == "candidate"
            and identity.get("provider") == "openai_compatible"
            and identity.get("model") == "gpt-5.6-terra"
            and len(trusted_candidate_decisions) == 1
        )

    def parse(self, _source: object) -> SimpleNamespace:
        markdown = MARKDOWN if self.reusable else "provider identity mismatch\n"
        return SimpleNamespace(markdown=markdown, semantic_fidelity=self.fidelity)


@pytest.mark.parametrize(
    "mode",
    (ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR),
)
def test_issue_3_verifier_accepts_pdf_extraction_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ExtractionMode,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, mode)
    monkeypatch.setattr(
        verifier,
        "scan_sources",
        lambda _path: SimpleNamespace(by_role=lambda _role: object()),
    )
    monkeypatch.setattr(verifier, "DoclingParser", ReusingParser)

    result = verifier.verify_issue_3(root)

    assert result["status"] == ("WARN" if mode is ExtractionMode.OCR else "PASS")
    assert result["page_count"] == 5


def test_issue_3_verifier_rejects_non_pdf_extraction_mode(tmp_path: Path) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.DOCX_XML)

    with pytest.raises(verifier.Issue3VerificationError, match="ISSUE_3_NOT_PDF"):
        verifier.verify_issue_3(root)
