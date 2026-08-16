from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import ard_ossie.application.source_check as source_check_module
import ard_ossie.pipeline as pipeline_module
from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    WorkflowConfigurationError,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowTransientError,
    WorkflowValidationError,
)
from ard_ossie.application.modeling import ValidationResult
from ard_ossie.application.source_check import (
    DetectProductService,
    EnsureProductPrService,
    SourceCheckService,
)
from ard_ossie.docling_parser import DoclingParser, ParsedDocument
from ard_ossie.ingestion import SourceRole
from ard_ossie.llm import (
    LLMMetadata,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
)
from ard_ossie.pipeline import QualityFinding, process_product
from ard_ossie.ports.git import ChangedPaths
from ard_ossie.ports.github import PullRequestState
from ard_ossie.semantic import parser as semantic_parser
from ard_ossie.semantic.correction import OcrCorrectionApplication
from ard_ossie.semantic.models import (
    ExtractionMode,
    NativeDocument,
    OcrCorrectionPageAudit,
    SourceBox,
    SourceSpan,
    make_span_id,
)
from ard_ossie.semantic.repair import SemanticStructureRepairPlanner
from ard_ossie.semantic.structure import StructureDocument
from tests.integration.test_cli_process import (
    FakeSemanticProvider,
    FidelityParser,
    create_product_fixture,
    pass_fidelity_report,
)

SHA = "a" * 40


class FakeGit:
    def __init__(self, paths: tuple[str, ...] = ()) -> None:
        self.paths = paths
        self.remote_sha = SHA

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        return ChangedPaths(merge_base="b" * 40, paths=tuple(Path(item) for item in self.paths))

    def current_sha(self) -> str:
        return SHA

    def remote_branch_sha(self, branch: str) -> str | None:
        return self.remote_sha


class FakeGitHub:
    def __init__(self) -> None:
        self.pull_request: PullRequestState | None = None
        self.created = 0
        self.remote_sha = SHA

    def branch_sha(self, branch: str) -> str | None:
        return self.remote_sha

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        return self.pull_request

    def create_draft_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestState:
        self.created += 1
        self.pull_request = PullRequestState(
            number=9,
            head_branch=branch,
            head_sha=SHA,
            base_branch=base,
            draft=True,
            merged_at=None,
            merge_sha=None,
            url="https://example.invalid/pull/9",
        )
        return self.pull_request


class OcrRepairResultProvider:
    def health_check(self) -> bool:
        return True

    def capabilities(self) -> dict[str, str | bool]:
        return {
            "structured_output": "json_schema",
            "provider": "ocr-repair-test",
            "model": "ocr-repair-v1",
            "vision": True,
        }

    def generate_structured(self, *, schema, messages):
        if "blocks" not in schema.get("properties", {}):
            return {"suggestions": [], "metrics": [], "product_facts": []}
        request = json.loads(messages[1]["content"])
        span_ids = [item["span_id"] for item in request["unresolved_spans"]]
        return LLMResult(
            text="",
            structured={
                "blocks": [
                    {
                        "kind": "paragraph",
                        "order": 0,
                        "span_ids": span_ids,
                        "heading_level": None,
                        "list_kind": None,
                        "list_depth": None,
                        "row_count": None,
                        "column_count": None,
                        "cells": [],
                        "exclusion_kind": None,
                        "confidence": 0.99,
                    }
                ]
            },
            metadata=LLMMetadata(
                profile="ocr-source-check",
                provider="openai_compatible",
                model="ocr-repair-v1",
                elapsed_ms=1,
            ),
        )


class AcceptedOcrCorrectionPlanner:
    def correct(self, _source, native, **_kwargs):
        return OcrCorrectionApplication(
            document=native,
            audits=(
                OcrCorrectionPageAudit(
                    source_hash=native.source_hash,
                    page=1,
                    page_image_hash="1" * 64,
                    ocr_catalog_hash="2" * 64,
                    request_hash="3" * 64,
                    prompt_version="ocr-correction-test-v1",
                    prompt_hash="4" * 64,
                    schema_hash="5" * 64,
                    provider="ocr-repair-test",
                    model="ocr-repair-v1",
                    outcome="applied",
                    patches=[],
                ),
            ),
            warning_codes=(),
        )


@pytest.mark.parametrize(
    "paths,expected",
    [
        (("products/sales-order/sources/product.html",), "sales-order"),
        (("README.md",), None),
    ],
)
def test_detect_product(
    tmp_path: Path,
    paths: tuple[str, ...],
    expected: str | None,
) -> None:
    result = DetectProductService(RepositoryPaths(tmp_path), FakeGit(paths)).run(
        "origin/main", "HEAD"
    )

    assert result.outputs.get("product_key") == expected
    assert result.outputs["expected_head"] == SHA


def test_detect_product_rejects_multiple_products(tmp_path: Path) -> None:
    git = FakeGit(
        (
            "products/a/sources/a.html",
            "products/b/sources/b.html",
        )
    )

    with pytest.raises(WorkflowValidationError, match="MULTIPLE_PRODUCTS_NOT_ALLOWED"):
        DetectProductService(RepositoryPaths(tmp_path), git).run("origin/main", "HEAD")


def test_detect_product_allows_canonical_changeset_marker_with_sources(
    tmp_path: Path,
) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    product_id = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
    product = tmp_path / "products" / "sales-order"
    marker = product / "changesets" / f"{changeset_id}.json"
    marker.parent.mkdir(parents=True)
    (product / "product.yaml").write_text(
        f"product_id: {product_id}\nproduct_key: sales-order\nchangeset_id: {changeset_id}\n",
        encoding="utf-8",
    )
    marker.write_text(
        json.dumps(
            {
                "changeset_id": changeset_id,
                "product_id": product_id,
                "status": "required",
            }
        ),
        encoding="utf-8",
    )
    result = DetectProductService(
        RepositoryPaths(tmp_path),
        FakeGit(
            (
                f"products/sales-order/changesets/{changeset_id}.json",
                "products/sales-order/product.yaml",
                "products/sales-order/sources/product/product.html",
            )
        )
    ).run("origin/main", "HEAD")

    assert result.outputs["product_key"] == "sales-order"


def test_detect_product_rejects_changeset_marker_for_another_product(
    tmp_path: Path,
) -> None:
    git = FakeGit(
        (
            "products/finance-order/changesets/"
            "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json",
            "products/sales-order/product.yaml",
            "products/sales-order/sources/product/product.html",
        )
    )

    with pytest.raises(
        WorkflowValidationError,
        match="CHANGESET_MARKER_PRODUCT_MISMATCH",
    ):
        DetectProductService(RepositoryPaths(tmp_path), git).run("origin/main", "HEAD")


def test_detect_product_allows_direct_update_config_with_same_product_sources(
    tmp_path: Path,
) -> None:
    result = DetectProductService(
        RepositoryPaths(tmp_path),
        FakeGit(
            (
                "products/sales-order/product.yaml",
                "products/sales-order/sources/product/product.html",
            )
        )
    ).run("origin/main", "HEAD")

    assert result.outputs["product_key"] == "sales-order"


@pytest.mark.parametrize(
    "paths",
    [
        ("products/sales-order/product.yaml",),
        (
            "products/finance-order/product.yaml",
            "products/sales-order/sources/product/product.html",
        ),
    ],
)
def test_detect_product_rejects_config_without_same_product_sources(
    tmp_path: Path,
    paths: tuple[str, ...],
) -> None:
    with pytest.raises(WorkflowValidationError, match="CHANGESET_CONFIG_PRODUCT_MISMATCH"):
        DetectProductService(RepositoryPaths(tmp_path), FakeGit(paths)).run(
            "origin/main", "HEAD"
        )


def test_detect_product_rejects_mixed_code_and_data(tmp_path: Path) -> None:
    git = FakeGit(("README.md", "products/sales/sources/product.html"))

    with pytest.raises(WorkflowValidationError, match="MIXED_CODE_AND_ARD_CHANGES"):
        DetectProductService(RepositoryPaths(tmp_path), git).run("origin/main", "HEAD")


def test_source_check_is_read_only_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("ARD_LLM_API_KEY", "injected-by-trusted-cli")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert result.status is WorkflowStatus.SUCCESS
    assert result.outputs["expected_head"] == SHA
    assert result.outputs["source_count"] == 3
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_relative_to(tmp_path / ".ard")
    } == before


def test_direct_update_config_and_sources_validate_against_existing_registry(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 2
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    product_html = product / "sources" / "product-info" / "product.html"
    product_html.write_text(
        product_html.read_text(encoding="utf-8").replace(
            "Order analytics",
            "Order insights",
        ),
        encoding="utf-8",
    )

    detected = DetectProductService(
        RepositoryPaths(tmp_path),
        FakeGit(
            (
                "products/sales-order/product.yaml",
                "products/sales-order/sources/product-info/product.html",
            )
        ),
    ).run("origin/main", "HEAD")
    checked = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert detected.outputs["product_key"] == "sales-order"
    assert checked.status is WorkflowStatus.SUCCESS


def test_source_check_treats_absent_registry_as_empty_without_creating_it(
    tmp_path: Path,
) -> None:
    """The first product validates against temporary empty state only."""
    create_product_fixture(tmp_path)

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert result.status is WorkflowStatus.SUCCESS
    assert not (tmp_path / "registry").exists()


def test_source_check_injects_provider_and_keeps_semantic_gates_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    provider = FakeSemanticProvider()
    captured: dict[str, object] = {}
    fidelity = pass_fidelity_report().model_copy(
        update={
            "extraction_mode": ExtractionMode.OCR,
            "status": "WARN",
            "warning_codes": ["SEMANTIC_OCR_CORRECTION_UNAVAILABLE"],
        }
    )

    def parser_factory(**kwargs):
        captured.update(kwargs)
        return FidelityParser(fidelity)

    monkeypatch.setattr(pipeline_module, "_processing_parser", parser_factory)

    with pytest.raises(
        WorkflowValidationError,
        match="SEMANTIC_VISUAL_CORRECTION_FAILED",
    ):
        SourceCheckService(
            RepositoryPaths(tmp_path),
            provider=provider,
        ).run("sales-order", SHA)

    assert captured["provider"] is provider
    assert captured["propagate_provider_errors"] is True


def test_source_check_accepts_repaired_ocr_from_llm_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    provider = OcrRepairResultProvider()

    def native_and_structure(source, **_kwargs):
        texts = ("Semantics 문서", "개인정보")
        spans = tuple(
            SourceSpan(
                span_id=make_span_id(source.sha256, index),
                ordinal=index,
                page=1,
                bbox=SourceBox(
                    left=0.05,
                    bottom=0.80 - index * 0.10,
                    right=0.95,
                    top=0.88 - index * 0.10,
                ),
                text=text,
                text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            for index, text in enumerate(texts)
        )
        return (
            NativeDocument(
                source_hash=source.sha256,
                extraction_mode=ExtractionMode.OCR,
                page_count=1,
                parser_versions={"ocr": "fixture-v1"},
                spans=spans,
                groups=(),
                tables=(),
            ),
            StructureDocument(blocks=()),
        )

    class SourceCheckParser:
        def __init__(self) -> None:
            self.semantic = DoclingParser(
                structure_repair_planner=SemanticStructureRepairPlanner(
                    provider,
                    propagate_provider_errors=True,
                ),
                ocr_correction_planner=AcceptedOcrCorrectionPlanner(),
            )

        def parse(self, source):
            if source.role is SourceRole.PRODUCT_HTML:
                return ParsedDocument(
                    role=source.role,
                    source_hash=source.sha256,
                    markdown="# Sales Order\n\nOrder analytics.",
                )
            return self.semantic.parse(source)

    monkeypatch.setattr(semantic_parser, "_native_and_structure", native_and_structure)
    monkeypatch.setattr(
        pipeline_module,
        "_processing_parser",
        lambda **_kwargs: SourceCheckParser(),
    )

    result = SourceCheckService(
        RepositoryPaths(tmp_path),
        provider=provider,
    ).run("sales-order", SHA)

    assert result.status is WorkflowStatus.SUCCESS


def test_source_check_preserves_quality_finding_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)

    class FailedModelingService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def validate(self, *_args: object, **_kwargs: object) -> ValidationResult:
            return ValidationResult(
                passed=False,
                findings=[
                    QualityFinding(
                        code="SEMANTIC_REPAIR_ORDER_INVALID",
                        message=(
                            "Semantic structure repair validation failed; "
                            "category=SEMANTIC_STRUCTURE_DEGRADED; "
                            "extraction_mode=ocr; unresolved_spans=4; pages=1; "
                            "validation_codes=SEMANTIC_REPAIR_MISSING_SPAN,"
                            "SEMANTIC_REPAIR_ORDER_INVALID; "
                            "provider=openai_compatible; model=gpt-5.6-terra; "
                            "applied_blocks=0; rejected_blocks=2; attempts=2"
                        ),
                        path="quality.semantic-structure-repair.json",
                    )
                ],
            )

    monkeypatch.setattr(
        source_check_module,
        "ModelingService",
        FailedModelingService,
    )

    with pytest.raises(WorkflowValidationError) as caught:
        SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert caught.value.code == "SEMANTIC_REPAIR_ORDER_INVALID"
    assert "validation_codes=SEMANTIC_REPAIR_MISSING_SPAN" in caught.value.message
    assert "category=SEMANTIC_STRUCTURE_DEGRADED" in caught.value.message
    assert "path=quality.semantic-structure-repair.json" in caught.value.message


def test_source_check_forwards_candidate_pipeline_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    captured: dict[str, object] = {}

    class RecordingModelingService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def validate(self, *_args: object, **kwargs: object) -> ValidationResult:
            captured.update(kwargs)
            return ValidationResult(passed=True)

    monkeypatch.setattr(source_check_module, "ModelingService", RecordingModelingService)

    SourceCheckService(RepositoryPaths(tmp_path)).run(
        "sales-order",
        SHA,
        semantic_pipeline_mode="candidate",
    )

    assert captured["semantic_pipeline_mode"] == "candidate"


@pytest.mark.parametrize(
    ("kind", "expected_error", "expected_exit_code"),
    [
        pytest.param(
            ProviderFailureKind.CONFIGURATION,
            WorkflowConfigurationError,
            20,
            id="configuration",
        ),
        pytest.param(
            ProviderFailureKind.OUTPUT,
            WorkflowValidationError,
            10,
            id="output",
        ),
        pytest.param(
            ProviderFailureKind.TRANSIENT,
            WorkflowTransientError,
            30,
            id="transient",
        ),
    ],
)
def test_source_check_maps_provider_failure_kind_to_workflow_exit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: ProviderFailureKind,
    expected_error: type[Exception],
    expected_exit_code: int,
) -> None:
    create_product_fixture(tmp_path)

    class FailingModelingService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def validate(self, *_args: object, **_kwargs: object):
            raise ProviderExecutionError("LLM_PROVIDER_SAFE_CODE", kind=kind)

    monkeypatch.setattr(source_check_module, "ModelingService", FailingModelingService)

    with pytest.raises(expected_error, match="LLM_PROVIDER_SAFE_CODE") as captured:
        SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert captured.value.exit_code == expected_exit_code


def test_detect_product_requires_changed_marker_config_binding(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    marker = product / "changesets" / (
        "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json"
    )
    marker.parent.mkdir()
    marker.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkflowSecurityError, match="CHANGESET_BINDING_REQUIRED"):
        DetectProductService(
            RepositoryPaths(tmp_path),
            FakeGit(
                (
                    marker.relative_to(tmp_path).as_posix(),
                    "products/sales-order/product.yaml",
                    "products/sales-order/sources/product-info/product.html",
                )
            ),
        ).run("origin/main", "HEAD")


def test_source_check_accepts_historical_marker_without_active_binding(tmp_path: Path) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    product = create_product_fixture(tmp_path)
    (tmp_path / "registry").mkdir()
    config = yaml.safe_load((product / "product.yaml").read_text(encoding="utf-8"))
    marker = product / "changesets" / f"{changeset_id}.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "changeset_id": changeset_id,
                "product_id": config["product_id"],
                "status": "required",
            }
        ),
        encoding="utf-8",
    )

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert "changeset_id" not in result.outputs


def test_source_check_accepts_exact_changeset_binding(tmp_path: Path) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    product = create_product_fixture(tmp_path)
    (tmp_path / "registry").mkdir()
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["changeset_id"] = changeset_id
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    marker = product / "changesets" / f"{changeset_id}.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "changeset_id": changeset_id,
                "product_id": config["product_id"],
                "status": "required",
            }
        ),
        encoding="utf-8",
    )

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert result.outputs["changeset_id"] == changeset_id


def test_ensure_product_pr_is_idempotent_for_exact_remote_head() -> None:
    git = FakeGit()
    github = FakeGitHub()
    service = EnsureProductPrService(git, github)

    first = service.run("feature/sales", "sales-order", SHA, base_branch="main")
    second = service.run("feature/sales", "sales-order", SHA, base_branch="main")

    assert first.outputs == second.outputs
    assert second.status is WorkflowStatus.NOOP
    assert github.created == 1


def test_ensure_product_pr_rejects_stale_remote_head() -> None:
    github = FakeGitHub()
    github.remote_sha = "c" * 40

    with pytest.raises(WorkflowSecurityError, match="DIRECT_BRANCH_HEAD_MISMATCH"):
        EnsureProductPrService(FakeGit(), github).run(
            "feature/sales",
            "sales-order",
            SHA,
            base_branch="main",
        )
