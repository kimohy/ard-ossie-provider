from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ard_ossie.semantic.adjudication import DecisionReport
from ard_ossie.semantic.canonical import SemanticPipelineStatus
from ard_ossie.semantic.models import ExtractionMode, SemanticFidelityReport
from scripts import verify_issue_3_semantic as verifier

MARKDOWN = "# Semantics\n\n개인정보 유효성\n\n| 항목 | 값 |\n| --- | --- |\n| A | B |\n"


def _write_artifact(
    root: Path,
    mode: ExtractionMode,
    *,
    decision_model: str = "gpt-5.6-terra",
    decision_outcome: str = "selected",
    decision_source: str = "deterministic",
) -> SemanticFidelityReport:
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
    fidelity_bytes = fidelity.model_dump_json().encode()
    (root / "quality" / "semantic-fidelity.json").write_bytes(fidelity_bytes)
    validation_bytes = json.dumps(
        {
            "status": "verified",
            "publishable": True,
            "source_hash": "a" * 64,
            "canonical_hash": "d" * 64,
            "findings": [],
            "character_coverage": 1.0,
            "missing_atom_count": 0,
            "duplicate_atom_count": 0,
            "degraded_block_count": 0,
            "model_call_count": 0,
        }
    ).encode()
    (root / "quality" / "validation-report.json").write_bytes(validation_bytes)
    decision_bytes = json.dumps(
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
                    "outcome": decision_outcome,
                    "source": (
                        decision_source if decision_outcome == "selected" else "fallback"
                    ),
                    "confidence": 1.0 if decision_outcome == "selected" else 0.7,
                    "provider": "openai_compatible",
                    "model": decision_model,
                    "recovery_status": (
                        "not_needed" if decision_outcome == "selected" else "deferred_review"
                    ),
                }
            ],
        }
    ).encode()
    (root / "quality" / "decision-report.json").write_bytes(decision_bytes)
    (root / "quality" / "quality-report.json").write_text(
        json.dumps(
            {
                "quality_artifact_hashes": {
                    "decision-report.json": hashlib.sha256(decision_bytes).hexdigest(),
                    "semantic-fidelity.json": hashlib.sha256(fidelity_bytes).hexdigest(),
                    "validation-report.json": hashlib.sha256(validation_bytes).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    return fidelity


def _convert_to_legacy_correction_artifact(
    root: Path,
    fidelity: SemanticFidelityReport,
    *,
    correction_model: str = "gpt-5.6-terra",
) -> None:
    corrected_text = "교정"
    payload = fidelity.model_dump(mode="json")
    payload.update(
        {
            "status": "WARN",
            "warning_codes": ["LLM_OCR_CORRECTION_APPLIED"],
            "ocr_correction_applied_count": 1,
            "ocr_corrections": [
                {
                    "source_hash": "a" * 64,
                    "page": 1,
                    "page_image_hash": "1" * 64,
                    "ocr_catalog_hash": "2" * 64,
                    "request_hash": "3" * 64,
                    "prompt_version": "v1",
                    "prompt_hash": "4" * 64,
                    "schema_hash": "5" * 64,
                    "provider": "openai_compatible",
                    "model": correction_model,
                    "outcome": "applied",
                    "patches": [
                        {
                            "span_id": "span_0000000000000001",
                            "original_text_hash": "6" * 64,
                            "corrected_text": corrected_text,
                            "corrected_text_hash": hashlib.sha256(
                                corrected_text.encode()
                            ).hexdigest(),
                            "bbox": {
                                "left": 0.1,
                                "bottom": 0.1,
                                "right": 0.2,
                                "top": 0.2,
                            },
                            "correction_kind": "character_recognition",
                            "confidence": 0.99,
                            "outcome": "applied",
                        }
                    ],
                }
            ],
        }
    )
    corrected = SemanticFidelityReport.model_validate(payload)
    fidelity_bytes = corrected.model_dump_json().encode()
    (root / "quality" / "semantic-fidelity.json").write_bytes(fidelity_bytes)
    quality_path = root / "quality" / "quality-report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["quality_artifact_hashes"].pop("decision-report.json")
    quality["quality_artifact_hashes"].pop("validation-report.json")
    quality["quality_artifact_hashes"]["semantic-fidelity.json"] = hashlib.sha256(
        fidelity_bytes
    ).hexdigest()
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    (root / "quality" / "validation-report.json").unlink()


class ReusingParser:
    replay_mode: ExtractionMode | None = None
    reuse_model_decisions = True

    def __init__(
        self,
        *,
        trusted_fidelity_report: SemanticFidelityReport,
        semantic_pipeline_mode: str = "shadow",
        candidate_provider: object | None = None,
        trusted_candidate_decisions: tuple[object, ...] = (),
        ocr_correction_planner: object | None = None,
        **_kwargs: object,
    ) -> None:
        legacy_replay = semantic_pipeline_mode == "legacy" and ocr_correction_planner is not None
        if legacy_replay:
            payload = trusted_fidelity_report.model_dump(mode="json")
            for page in payload["ocr_corrections"]:
                if page["outcome"] == "applied":
                    page["outcome"] = "reused"
                    for patch in page["patches"]:
                        if patch["outcome"] == "applied":
                            patch["outcome"] = "reused"
            self.fidelity = SemanticFidelityReport.model_validate(payload)
        else:
            self.fidelity = trusted_fidelity_report
        self.decisions = tuple(
            decision.model_copy(update={"source": "cache"})
            if self.reuse_model_decisions and decision.source != "deterministic"
            else decision
            for decision in trusted_candidate_decisions
        )
        identity = (
            candidate_provider.capabilities()  # type: ignore[attr-defined]
            if candidate_provider is not None
            else {}
        )
        self.reusable = legacy_replay or (
            semantic_pipeline_mode == "candidate"
            and identity.get("provider") == "openai_compatible"
            and identity.get("model") == "gpt-5.6-terra"
            and len(trusted_candidate_decisions) == 1
        )

    def parse(self, _source: object) -> SimpleNamespace:
        markdown = MARKDOWN if self.reusable else "provider identity mismatch\n"
        fidelity = self.fidelity.model_copy(
            update={"extraction_mode": self.replay_mode or self.fidelity.extraction_mode}
        )
        pipeline_result = SimpleNamespace(
            validation=SimpleNamespace(
                status=SemanticPipelineStatus.VERIFIED,
                publishable=True,
                source_hash=self.fidelity.source_hash,
                canonical_hash="d" * 64,
                character_coverage=1.0,
                missing_atom_count=0,
                duplicate_atom_count=0,
                degraded_block_count=0,
            ),
            decisions=DecisionReport(
                source_hash=self.fidelity.source_hash,
                decisions=self.decisions,
            ),
        )
        return SimpleNamespace(
            markdown=markdown,
            semantic_fidelity=fidelity,
            semantic_pipeline_result=pipeline_result,
        )


def _install_replay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_name: str = "semantic.pdf",
    source_hash: str = "a" * 64,
    replay_mode: ExtractionMode | None = None,
    reuse_model_decisions: bool = True,
) -> None:
    source = SimpleNamespace(path=Path(source_name), sha256=source_hash)
    monkeypatch.setattr(
        verifier,
        "scan_sources",
        lambda _path: SimpleNamespace(by_role=lambda _role: source),
    )
    monkeypatch.setattr(ReusingParser, "replay_mode", replay_mode)
    monkeypatch.setattr(ReusingParser, "reuse_model_decisions", reuse_model_decisions)
    monkeypatch.setattr(verifier, "DoclingParser", ReusingParser)


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
    _install_replay(monkeypatch)

    result = verifier.verify_issue_3(root)

    assert result["status"] == ("WARN" if mode is ExtractionMode.OCR else "PASS")
    assert result["page_count"] == 5


def test_issue_3_verifier_rejects_non_pdf_extraction_mode(tmp_path: Path) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.DOCX_XML)

    with pytest.raises(verifier.Issue3VerificationError, match="ISSUE_3_NOT_PDF"):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_rejects_non_pdf_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.PDF_EMBEDDED)
    _install_replay(monkeypatch, source_name="semantic.docx")

    with pytest.raises(verifier.Issue3VerificationError, match="ISSUE_3_NOT_PDF"):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_binds_reports_to_source_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.PDF_EMBEDDED)
    _install_replay(monkeypatch, source_hash="d" * 64)

    with pytest.raises(verifier.Issue3VerificationError, match="ISSUE_3_SOURCE_HASH_MISMATCH"):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_rejects_untrusted_provider_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(
        root,
        ExtractionMode.PDF_EMBEDDED,
        decision_model="untrusted-model",
    )
    _install_replay(monkeypatch)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_DECISION_PROVIDER_INVALID",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_rejects_deferred_candidate_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(
        root,
        ExtractionMode.PDF_EMBEDDED,
        decision_outcome="deferred_review",
    )
    _install_replay(monkeypatch)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_DECISIONS_UNRESOLVED",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_rejects_decision_report_hash_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.PDF_EMBEDDED)
    quality_path = root / "quality" / "quality-report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["quality_artifact_hashes"]["decision-report.json"] = "0" * 64
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    _install_replay(monkeypatch)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_DECISION_HASH_MISMATCH",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_binds_saved_validation_to_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.PDF_EMBEDDED)
    validation_path = root / "quality" / "validation-report.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["canonical_hash"] = "e" * 64
    validation_bytes = json.dumps(validation).encode()
    validation_path.write_bytes(validation_bytes)
    quality_path = root / "quality" / "quality-report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["quality_artifact_hashes"]["validation-report.json"] = hashlib.sha256(
        validation_bytes
    ).hexdigest()
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    _install_replay(monkeypatch)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_REPLAY_VALIDATION_MISMATCH",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_rejects_replay_extraction_mode_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(root, ExtractionMode.PDF_EMBEDDED)
    _install_replay(monkeypatch, replay_mode=ExtractionMode.DOCX_XML)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_REPLAY_MODE_MISMATCH",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_requires_model_decision_cache_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(
        root,
        ExtractionMode.PDF_EMBEDDED,
        decision_source="model",
    )
    _install_replay(monkeypatch, reuse_model_decisions=False)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_DECISION_REUSE_MISMATCH",
    ):
        verifier.verify_issue_3(root)


def test_issue_3_verifier_accepts_model_decision_cache_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    _write_artifact(
        root,
        ExtractionMode.PDF_EMBEDDED,
        decision_source="model",
    )
    _install_replay(monkeypatch)

    assert verifier.verify_issue_3(root)["status"] == "PASS"


def test_issue_3_verifier_preserves_legacy_ocr_correction_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    fidelity = _write_artifact(root, ExtractionMode.OCR)
    _convert_to_legacy_correction_artifact(root, fidelity)
    assert (root / "quality" / "decision-report.json").exists()
    _install_replay(monkeypatch)

    result = verifier.verify_issue_3(root)

    assert result["correction_count"] == 1
    assert result["reused_page_count"] == 1


def test_issue_3_verifier_rejects_untrusted_legacy_correction_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "product"
    fidelity = _write_artifact(root, ExtractionMode.OCR)
    _convert_to_legacy_correction_artifact(
        root,
        fidelity,
        correction_model="untrusted-model",
    )
    _install_replay(monkeypatch)

    with pytest.raises(
        verifier.Issue3VerificationError,
        match="ISSUE_3_CORRECTION_PROVIDER_INVALID",
    ):
        verifier.verify_issue_3(root)
