from __future__ import annotations

import hashlib
import json
import subprocess
import traceback
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.adapters.git_cli import GitCli
from ard_ossie.adapters.subprocess import SubprocessRunner
from ard_ossie.application.contracts import (
    ExitCode,
    MutationRecord,
    WorkflowConfigurationError,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowTransientError,
    WorkflowValidationError,
)
from ard_ossie.application.processing import (
    ProcessingReconcileRequest,
    ProcessingReconcileService,
    ProcessingRequest,
    ProcessingService,
    _trusted_semantic_repair,
)
from ard_ossie.ingestion import SourceRole
from ard_ossie.pipeline import (
    ProcessResult,
    ProviderExecutionError,
    ProviderFailureKind,
    QualityFinding,
    QualityReport,
    QualityStatus,
)
from ard_ossie.ports.git import ChangedPaths, CommitResult, GitConflict, GitTransientError
from ard_ossie.ports.github import GitHubTransientError, PullRequestState, RepositoryState
from ard_ossie.semantic.replay import SemanticReplayCatalog
from tests.integration.test_cli_process import create_product_fixture

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
INVOCATION_ID = "31543231017-1"
SEMANTIC_SOURCE_BYTES = b"%PDF-1.7 same semantic source"
SEMANTIC_SOURCE_HASH = hashlib.sha256(SEMANTIC_SOURCE_BYTES).hexdigest()


class FakeGit:
    def __init__(
        self,
        *,
        base_sha: str = OLD_SHA,
        revision_files: dict[str, str | bytes] | None = None,
    ) -> None:
        self.sha = OLD_SHA
        self.remote_sha = OLD_SHA
        self.base_sha = base_sha
        self.revision_files = {
            "registry/indexes/product-keys.json": json.dumps({"sales-order": PRODUCT_ID}),
            **(revision_files or {}),
        }
        self.revision_reads: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, bool]] = []

    @classmethod
    def with_revision_files(
        cls,
        *,
        base_sha: str,
        files: dict[str, str | bytes],
    ) -> FakeGit:
        return cls(base_sha=base_sha, revision_files=files)

    def current_sha(self) -> str:
        return self.sha

    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult:
        if self.sha == NEW_SHA:
            return CommitResult(sha=NEW_SHA, created=False)
        self.sha = NEW_SHA
        return CommitResult(sha=NEW_SHA, created=True)

    def push(self, branch: str, *, lfs: bool = False) -> None:
        self.pushes.append((branch, lfs))
        self.remote_sha = self.sha

    def remote_branch_sha(self, branch: str) -> str | None:
        if branch == "main":
            return self.base_sha
        return self.remote_sha

    def read_text_at(self, revision: str, path: str | Path) -> str:
        relative = Path(path).as_posix()
        self.revision_reads.append((revision, relative))
        if revision != self.base_sha or relative not in self.revision_files:
            raise GitConflict("REVISION_FILE_NOT_FOUND", relative)
        value = self.revision_files[relative]
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def read_bytes_at(self, revision: str, path: str | Path) -> bytes:
        relative = Path(path).as_posix()
        self.revision_reads.append((revision, relative))
        if revision != self.base_sha or relative not in self.revision_files:
            raise GitConflict("REVISION_FILE_NOT_FOUND", relative)
        value = self.revision_files[relative]
        return value if isinstance(value, bytes) else value.encode("utf-8")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor == OLD_SHA and descendant == NEW_SHA

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        assert base_ref == OLD_SHA
        assert head_ref == NEW_SHA
        return ChangedPaths(
            merge_base=OLD_SHA,
            paths=(Path("products/sales-order/generated/ossie-model.json"),),
        )


class FailingPushGit(FakeGit):
    def push(self, branch: str, *, lfs: bool = False) -> None:
        raise GitTransientError("GIT_PUSH_FAILED", "network")


class FakeGitHub:
    def __init__(self, git: FakeGit) -> None:
        self.git = git
        self.head_branch = "ard/example"
        self.base_branch = "main"
        self.statuses: list[tuple[str, str, str]] = []
        self.dispatched = 0
        self.fail_status = False

    def get_pr(self, number: int) -> PullRequestState:
        return PullRequestState(
            number=number,
            head_branch=self.head_branch,
            head_sha=self.git.remote_sha,
            base_branch=self.base_branch,
            draft=True,
            merged_at=None,
            merge_sha=None,
            url="https://example.invalid/pull/7",
        )

    def repository(self) -> RepositoryState:
        return RepositoryState(
            full_name="owner/repository",
            public=True,
            archived=False,
            default_branch="main",
            permission="admin",
        )

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ):
        if self.fail_status:
            raise GitHubTransientError("STATUS_FAILED", "network")
        self.statuses.append((sha, context, state))
        from ard_ossie.application.contracts import MutationRecord

        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")

    def get_status(self, sha: str, context: str) -> str | None:
        return next(
            (
                state
                for status_sha, status_context, state in reversed(self.statuses)
                if status_sha == sha and status_context == context
            ),
            None,
        )

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]):
        self.dispatched += 1
        from ard_ossie.application.contracts import MutationRecord

        return MutationRecord(resource="workflow", target=f"{workflow}:{ref}", action="dispatch")


class FlakyReconcileGitHub(FakeGitHub):
    def __init__(self, git: FakeGit) -> None:
        super().__init__(git)
        self.get_status_failures = 2
        self.set_status_failures = 2
        self.dispatch_failures = 2
        self.dispatch_attempts = 0

    def get_status(self, sha: str, context: str) -> str | None:
        if self.get_status_failures:
            self.get_status_failures -= 1
            raise GitHubTransientError("STATUS_READ_FAILED", "network")
        return super().get_status(sha, context)

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ):
        if self.set_status_failures:
            self.set_status_failures -= 1
            raise GitHubTransientError("STATUS_FAILED", "network")
        return super().set_status(sha, context, state, description, target_url)

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]):
        self.dispatch_attempts += 1
        if self.dispatch_failures:
            self.dispatch_failures -= 1
            raise GitHubTransientError("DISPATCH_FAILED", "network")
        return super().dispatch_workflow(workflow, ref, inputs)


def request(tmp_path: Path) -> ProcessingRequest:
    return ProcessingRequest(
        repository=tmp_path,
        product_key="sales-order",
        branch="ard/example",
        pr_number=7,
        expected_head=OLD_SHA,
        allow_writeback=True,
    )


def successful_processor(product_path: Path, **kwargs) -> ProcessResult:
    return ProcessResult(
        product_id=PRODUCT_ID,
        product_version=1,
        generated_dir=product_path / "generated",
        quality_report=QualityReport(
            status=QualityStatus.PASS,
            product_id=PRODUCT_ID,
            product_version=1,
            completeness=1,
            hard_errors=[],
            warnings=[],
            artifact_hashes={},
        ),
    )


def review_pending_processor(product_path: Path, **kwargs) -> ProcessResult:
    del kwargs
    quality = product_path / "quality"
    quality.mkdir(exist_ok=True)
    (quality / "semantic-review.json").write_text(
        json.dumps(
            {
                "schema_version": "semantic-review-v1",
                "entries": [{"validation_codes": ["LLM_SPACING_REPAIR_DEFERRED"]}],
            }
        ),
        encoding="utf-8",
    )
    return ProcessResult(
        product_id=PRODUCT_ID,
        product_version=1,
        generated_dir=product_path / "generated",
        quality_report=QualityReport(
            status=QualityStatus.WARN,
            product_id=PRODUCT_ID,
            product_version=1,
            completeness=1,
            hard_errors=[],
            warnings=[
                QualityFinding(
                    code="LLM_SPACING_REPAIR_DEFERRED",
                    message="Draft output requires later human review",
                    path="quality.semantic-review.json",
                )
            ],
            artifact_hashes={},
        ),
    )


def repository(tmp_path: Path) -> None:
    product = tmp_path / "products" / "sales-order"
    product.mkdir(parents=True)
    (product / "product.yaml").write_text(
        f"product_id: {PRODUCT_ID}\nversion: 1\nchangeset_id:\n",
        encoding="utf-8",
    )
    for directory, name, payload in (
        ("product-info", "product.html", b"<h1>Product</h1>"),
        ("semantic", "semantic.pdf", SEMANTIC_SOURCE_BYTES),
        ("dictionary", "dictionary.xlsx", b"PK\x03\x04dictionary"),
    ):
        target = product / "sources" / directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (tmp_path / "registry").mkdir()


def replay_revision_files(product_key: str = "base-product") -> dict[str, str | bytes]:
    manifest = (
        json.dumps(
            {
                "files": [
                    {
                        "role": "semantic_document",
                        "relative_path": "semantic/semantic.pdf",
                        "sha256": SEMANTIC_SOURCE_HASH,
                        "size_bytes": len(SEMANTIC_SOURCE_BYTES),
                    }
                ]
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    markdown = "Data Semantics 정의서이며\n".encode()
    decisions = (
        json.dumps(
            {"source_hash": SEMANTIC_SOURCE_HASH, "decisions": []},
            sort_keys=True,
        )
        + "\n"
    ).encode()
    validation = (
        json.dumps(
            {
                "status": "verified",
                "publishable": True,
                "source_hash": SEMANTIC_SOURCE_HASH,
                "canonical_hash": "c" * 64,
                "findings": [],
                "character_coverage": 1.0,
                "missing_atom_count": 0,
                "duplicate_atom_count": 0,
                "degraded_block_count": 0,
                "model_call_count": 0,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    diagnostics_manifest = (
        json.dumps(
            {
                "schema_version": "semantic-diagnostics-v1",
                "source_hash": SEMANTIC_SOURCE_HASH,
                "configuration_hash": "f" * 64,
                "mode": "candidate",
                "publication_status": "verified",
                "reports": {
                    "decision-report.json": hashlib.sha256(decisions).hexdigest(),
                    "validation-report.json": hashlib.sha256(validation).hexdigest(),
                },
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    quality = (
        json.dumps(
            {
                "status": "PASS",
                "product_id": PRODUCT_ID,
                "product_version": 1,
                "completeness": 1.0,
                "hard_errors": [],
                "warnings": [],
                "artifact_hashes": {
                    "source-manifest.json": hashlib.sha256(manifest).hexdigest(),
                    "data-semantic.md": hashlib.sha256(markdown).hexdigest(),
                },
                "quality_artifact_hashes": {
                    "decision-report.json": hashlib.sha256(decisions).hexdigest(),
                    "manifest.json": hashlib.sha256(diagnostics_manifest).hexdigest(),
                    "validation-report.json": hashlib.sha256(validation).hexdigest(),
                },
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    root = f"products/{product_key}"
    return {
        "registry/indexes/product-keys.json": json.dumps({product_key: PRODUCT_ID}),
        f"{root}/generated/source-manifest.json": manifest,
        f"{root}/generated/data-semantic.md": markdown,
        f"{root}/quality/quality-report.json": quality,
        f"{root}/quality/decision-report.json": decisions,
        f"{root}/quality/manifest.json": diagnostics_manifest,
        f"{root}/quality/validation-report.json": validation,
    }


def valid_repair_record() -> dict[str, object]:
    return {
        "source_hash": "a" * 64,
        "ordered_span_hashes": [],
        "parser_version": "semantic-structure-v1",
        "prompt_version": "semantic-structure-repair-v1",
        "schema_hash": "b" * 64,
        "provider": "test-provider",
        "model": "test-model",
        "outcome": "degraded",
        "plan": None,
        "provider_error_code": "LLM_PROVIDER_TRANSIENT_FAILED",
        "validation_codes": [],
        "applied_orders": [],
        "rejected_orders": [],
        "plan_hash": None,
    }


def valid_fidelity_report() -> dict[str, object]:
    return {
        "source_hash": "a" * 64,
        "extraction_mode": "docx_xml",
        "page_count": 1,
        "parser_versions": {"semantic": "test"},
        "status": "PASS",
        "heading_count": 0,
        "paragraph_count": 1,
        "list_item_count": 0,
        "table_count": 0,
        "row_count": 0,
        "cell_count": 0,
        "source_span_count": 1,
        "preserved_span_count": 1,
        "excluded_span_count": 0,
        "unmatched_span_count": 0,
        "duplicated_span_count": 0,
        "degraded_block_count": 0,
        "source_text_coverage": 1.0,
        "removed_elements": [],
        "degraded_blocks": [],
        "table_results": [],
        "ocr_corrections": [],
        "ocr_correction_applied_count": 0,
        "ocr_correction_rejected_count": 0,
        "warning_codes": [],
        "thresholds": {
            "overlap_weight": 0.55,
            "text_similarity_weight": 0.35,
            "order_weight": 0.1,
            "acceptance_score": 0.72,
            "page_edge_band": 0.1,
            "repeat_ratio": 0.6,
        },
    }


def capturing_processing_service(
    tmp_path: Path,
    *,
    git: FakeGit,
) -> tuple[ProcessingService, dict[str, object]]:
    repository(tmp_path)
    captured: dict[str, object] = {}

    def processor(product_path: Path, **kwargs) -> ProcessResult:
        captured.update(kwargs)
        return successful_processor(product_path, **kwargs)

    return (
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=processor,
            provider_factory=lambda: None,
        ),
        captured,
    )


def test_processing_passes_matching_base_replay_catalog_to_processor(
    tmp_path: Path,
) -> None:
    git = FakeGit.with_revision_files(
        base_sha=NEW_SHA,
        files=replay_revision_files(),
    )
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    catalog = captured["trusted_semantic_replay_catalog"]
    assert isinstance(catalog, SemanticReplayCatalog)
    assert len(catalog.baselines) == 1
    assert catalog.baselines[0].product_key == "base-product"
    assert catalog.baselines[0].identity.source_hash == SEMANTIC_SOURCE_HASH
    assert captured["source_manifest"].by_role(SourceRole.SEMANTIC_DOCUMENT).sha256 == (
        SEMANTIC_SOURCE_HASH
    )
    assert all(revision == NEW_SHA for revision, _path in git.revision_reads)


def test_processing_ignores_matching_non_candidate_history(tmp_path: Path) -> None:
    files = replay_revision_files()
    root = "products/base-product"
    del files[f"{root}/quality/manifest.json"]
    del files[f"{root}/quality/decision-report.json"]
    del files[f"{root}/quality/validation-report.json"]
    quality_path = f"{root}/quality/quality-report.json"
    quality = json.loads(files[quality_path])
    quality["quality_artifact_hashes"] = {}
    files[quality_path] = json.dumps(quality, sort_keys=True) + "\n"
    git = FakeGit.with_revision_files(base_sha=NEW_SHA, files=files)
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    catalog = captured["trusted_semantic_replay_catalog"]
    assert isinstance(catalog, SemanticReplayCatalog)
    assert catalog.baselines == ()


def test_processing_replay_trust_failure_stops_before_side_effects(
    tmp_path: Path,
) -> None:
    files = replay_revision_files()
    markdown_path = "products/base-product/generated/data-semantic.md"
    files[markdown_path] = bytes(files[markdown_path]) + b"tampered"
    git = FakeGit.with_revision_files(base_sha=NEW_SHA, files=files)
    repository(tmp_path)
    github = FakeGitHub(git)
    provider_loaded = False
    processor_called = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    def processor(*_args, **_kwargs):
        nonlocal processor_called
        processor_called = True
        raise AssertionError("untrusted replay must stop before processing")

    with pytest.raises(WorkflowSecurityError) as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=processor,
            provider_factory=provider_factory,
        ).run(request(tmp_path))

    assert captured.value.code == "SEMANTIC_REPLAY_TRUST_MISMATCH"
    assert provider_loaded is False
    assert processor_called is False
    assert github.statuses == []
    assert git.pushes == []


def test_processing_passes_none_when_base_quality_has_no_repair_hash_or_file(
    tmp_path: Path,
) -> None:
    git = FakeGit.with_revision_files(
        base_sha=OLD_SHA,
        files={
            "products/sales-order/quality/quality-report.json": json.dumps(
                {"quality_artifact_hashes": {}}
            )
        },
    )
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    assert captured["trusted_semantic_repair"] is None


def test_processing_passes_only_hash_verified_base_semantic_artifacts_to_processor(
    tmp_path: Path,
) -> None:
    repair_text = json.dumps(valid_repair_record()) + "\n"
    fidelity_text = json.dumps(valid_fidelity_report()) + "\n"
    quality_text = json.dumps(
        {
            "quality_artifact_hashes": {
                "semantic-structure-repair.json": hashlib.sha256(repair_text.encode()).hexdigest(),
                "semantic-fidelity.json": hashlib.sha256(fidelity_text.encode()).hexdigest(),
            }
        }
    )
    git = FakeGit.with_revision_files(
        base_sha=NEW_SHA,
        files={
            "products/sales-order/quality/quality-report.json": quality_text,
            "products/sales-order/quality/semantic-structure-repair.json": repair_text,
            "products/sales-order/quality/semantic-fidelity.json": fidelity_text,
        },
    )
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    assert captured["trusted_semantic_repair"] == valid_repair_record()
    assert captured["trusted_semantic_fidelity"] == valid_fidelity_report()
    assert git.revision_reads == [
        (
            NEW_SHA,
            "products/sales-order/quality/quality-report.json",
        ),
        (
            NEW_SHA,
            "products/sales-order/quality/semantic-structure-repair.json",
        ),
        (
            NEW_SHA,
            "products/sales-order/quality/semantic-fidelity.json",
        ),
        (NEW_SHA, "registry/indexes/product-keys.json"),
        (NEW_SHA, "products/sales-order/generated/source-manifest.json"),
    ]


def test_processing_passes_only_hash_verified_candidate_decisions(tmp_path: Path) -> None:
    decision_text = json.dumps({"source_hash": "a" * 64, "decisions": []}) + "\n"
    quality_text = json.dumps(
        {
            "quality_artifact_hashes": {
                "decision-report.json": hashlib.sha256(decision_text.encode()).hexdigest(),
            }
        }
    )
    git = FakeGit.with_revision_files(
        base_sha=NEW_SHA,
        files={
            "products/sales-order/quality/quality-report.json": quality_text,
            "products/sales-order/quality/decision-report.json": decision_text,
        },
    )
    service, captured = capturing_processing_service(tmp_path, git=git)

    service.run(request(tmp_path))

    assert captured["trusted_semantic_decisions"] == {
        "source_hash": "a" * 64,
        "decisions": [],
    }


def test_processing_rejects_tampered_candidate_decisions(tmp_path: Path) -> None:
    expected = b'{"source_hash":"' + b"a" * 64 + b'","decisions":[]}\n'
    tampered = expected + b" "
    quality_text = json.dumps(
        {
            "quality_artifact_hashes": {
                "decision-report.json": hashlib.sha256(expected).hexdigest(),
            }
        }
    )
    git = FakeGit.with_revision_files(
        base_sha=NEW_SHA,
        files={
            "products/sales-order/quality/quality-report.json": quality_text,
            "products/sales-order/quality/decision-report.json": tampered,
        },
    )
    service, _captured = capturing_processing_service(tmp_path, git=git)

    with pytest.raises(WorkflowSecurityError, match="SEMANTIC_REPAIR_TRUST_MISMATCH"):
        service.run(request(tmp_path))


def test_processing_hashes_raw_repair_bytes_and_rejects_invalid_utf8(tmp_path: Path) -> None:
    repair_bytes = b'{"invalid":"\xff"}\n'
    quality_bytes = json.dumps(
        {
            "quality_artifact_hashes": {
                "semantic-structure-repair.json": hashlib.sha256(repair_bytes).hexdigest()
            }
        }
    ).encode("utf-8")
    git = FakeGit.with_revision_files(
        base_sha=NEW_SHA,
        files={
            "products/sales-order/quality/quality-report.json": quality_bytes,
            "products/sales-order/quality/semantic-structure-repair.json": repair_bytes,
        },
    )
    service, _captured = capturing_processing_service(tmp_path, git=git)

    with pytest.raises(WorkflowSecurityError, match="SEMANTIC_REPAIR_TRUST_MISMATCH"):
        service.run(request(tmp_path))


def test_real_git_adapter_loads_hash_verified_repair_blob_over_one_mib(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    quality = tmp_path / "products" / "sales-order" / "quality"
    quality.mkdir(parents=True)
    repair = {"padding": "x" * (1024 * 1024 + 17)}
    repair_bytes = (json.dumps(repair) + "\n").encode("utf-8")
    (quality / "semantic-structure-repair.json").write_bytes(repair_bytes)
    (quality / "quality-report.json").write_text(
        json.dumps(
            {
                "quality_artifact_hashes": {
                    "semantic-structure-repair.json": hashlib.sha256(repair_bytes).hexdigest()
                }
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    loaded = _trusted_semantic_repair(
        GitCli(tmp_path, SubprocessRunner()),
        base_sha=revision,
        product_key="sales-order",
    )

    assert loaded == repair


def test_processing_never_reads_repair_from_mutable_checkout(tmp_path: Path) -> None:
    git = FakeGit()
    service, captured = capturing_processing_service(tmp_path, git=git)
    mutable_quality = tmp_path / "products" / "sales-order" / "quality"
    mutable_quality.mkdir()
    (mutable_quality / "semantic-structure-repair.json").write_text(
        json.dumps(valid_repair_record()),
        encoding="utf-8",
    )

    service.run(request(tmp_path))

    assert captured["trusted_semantic_repair"] is None
    assert captured["trusted_semantic_fidelity"] is None
    assert git.revision_reads == [
        (OLD_SHA, "products/sales-order/quality/quality-report.json"),
        (OLD_SHA, "products/sales-order/quality/semantic-structure-repair.json"),
        (OLD_SHA, "products/sales-order/quality/semantic-fidelity.json"),
        (OLD_SHA, "registry/indexes/product-keys.json"),
        (OLD_SHA, "products/sales-order/generated/source-manifest.json"),
    ]


@pytest.mark.parametrize(
    ("quality_text", "repair_text"),
    [
        pytest.param(
            json.dumps({"quality_artifact_hashes": {"semantic-structure-repair.json": "0" * 64}}),
            json.dumps(valid_repair_record()),
            id="digest-mismatch",
        ),
        pytest.param("{", json.dumps(valid_repair_record()), id="malformed-quality"),
        pytest.param(
            json.dumps(
                {
                    "quality_artifact_hashes": {
                        "semantic-structure-repair.json": hashlib.sha256(b"{").hexdigest()
                    }
                }
            ),
            "{",
            id="malformed-repair",
        ),
        pytest.param(None, json.dumps(valid_repair_record()), id="missing-quality"),
        pytest.param(
            json.dumps({"quality_artifact_hashes": {"semantic-structure-repair.json": None}}),
            None,
            id="null-digest-without-repair",
        ),
        pytest.param(
            json.dumps({"quality_artifact_hashes": {"semantic-structure-repair.json": 7}}),
            json.dumps(valid_repair_record()),
            id="non-string-digest-with-repair",
        ),
        pytest.param(
            json.dumps({"quality_artifact_hashes": {"semantic-structure-repair.json": "0" * 64}}),
            None,
            id="missing-repair",
        ),
    ],
)
def test_processing_rejects_unverified_base_repair_state(
    tmp_path: Path,
    quality_text: str | None,
    repair_text: str | None,
) -> None:
    files = {}
    if quality_text is not None:
        files["products/sales-order/quality/quality-report.json"] = quality_text
    if repair_text is not None:
        files["products/sales-order/quality/semantic-structure-repair.json"] = repair_text
    git = FakeGit.with_revision_files(base_sha=NEW_SHA, files=files)
    service, _ = capturing_processing_service(tmp_path, git=git)

    with pytest.raises(WorkflowSecurityError) as captured:
        service.run(request(tmp_path))

    assert captured.value.code == "SEMANTIC_REPAIR_TRUST_MISMATCH"
    assert str(captured.value) == (
        "SEMANTIC_REPAIR_TRUST_MISMATCH: "
        "trusted semantic repair record failed hash or JSON verification"
    )


def test_processing_rejects_noncanonical_base_revision(tmp_path: Path) -> None:
    git = FakeGit(base_sha="B" * 40)
    service, _ = capturing_processing_service(tmp_path, git=git)

    with pytest.raises(WorkflowSecurityError, match="SEMANTIC_REPAIR_TRUST_MISMATCH"):
        service.run(request(tmp_path))


def bind_changeset(tmp_path: Path) -> None:
    product = tmp_path / "products" / "sales-order"
    (product / "product.yaml").write_text(
        "\n".join(
            (
                f"product_id: {PRODUCT_ID}",
                "version: 1",
                f"changeset_id: {CHANGESET_ID}",
                "",
            )
        ),
        encoding="utf-8",
    )
    marker = product / "changesets" / f"{CHANGESET_ID}.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "changeset_id": CHANGESET_ID,
                "product_id": PRODUCT_ID,
                "status": "required",
            }
        ),
        encoding="utf-8",
    )


def test_processing_promotes_commits_and_sets_exact_head_status(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ProcessingService(
        RepositoryPaths(tmp_path),
        git,
        github,
        processor=successful_processor,
        provider_factory=lambda: None,
    )

    result = service.run(request(tmp_path))

    assert result.status == "success"
    assert result.outputs["product_id"] == PRODUCT_ID
    assert result.outputs["current_head"] == NEW_SHA
    assert [mutation.resource for mutation in result.mutations][:3] == [
        "commit",
        "status",
        "status",
    ]
    assert all(status[0] == NEW_SHA for status in github.statuses)
    assert git.pushes == [("ard/example", True)]


def test_review_pending_warning_still_writes_and_promotes_draft_artifacts(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ProcessingService(
        RepositoryPaths(tmp_path),
        git,
        github,
        processor=review_pending_processor,
        provider_factory=lambda: None,
    )

    result = service.run(request(tmp_path))

    assert result.status is WorkflowStatus.SUCCESS
    assert (tmp_path / "products/sales-order/quality/semantic-review.json").is_file()
    assert github.get_pr(7).draft is True
    assert all(state == "success" for _sha, _context, state in github.statuses)
    assert git.pushes == [("ard/example", True)]


def test_processing_creates_first_registry_through_atomic_promotion(
    tmp_path: Path,
) -> None:
    """Trusted processing owns creation of the first authoritative registry."""
    create_product_fixture(tmp_path)
    git = FakeGit()

    result = ProcessingService(
        RepositoryPaths(tmp_path),
        git,
        FakeGitHub(git),
        provider_factory=lambda: None,
    ).run(request(tmp_path))

    assert result.status is WorkflowStatus.SUCCESS
    assert (tmp_path / "registry" / "products" / f"{PRODUCT_ID}.json").is_file()


def test_processing_rejects_registry_symlink_inserted_before_processor(
    tmp_path: Path,
) -> None:
    """Provider setup must not reopen the vetted registry path outside the repository."""
    create_product_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    git = FakeGit()

    def swap_registry_for_symlink():
        (tmp_path / "registry").symlink_to(outside, target_is_directory=True)
        return None

    with pytest.raises(WorkflowSecurityError, match="SYMLINK_NOT_ALLOWED"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            provider_factory=swap_registry_for_symlink,
        ).run(request(tmp_path))

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]
    assert git.sha == OLD_SHA
    assert git.pushes == []


def test_processing_rejects_stale_head_before_loading_provider(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = "c" * 40
    provider_loaded = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_HEAD_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=provider_factory,
        ).run(request(tmp_path))

    assert provider_loaded is False


def test_processing_rejects_pr_retargeted_away_from_default_branch(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    github.base_branch = "attacker-base"
    provider_loaded = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_BASE_BRANCH_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=successful_processor,
            provider_factory=provider_factory,
        ).run(request(tmp_path))

    assert git.revision_reads == []
    assert provider_loaded is False
    assert github.statuses == []


def test_processing_rejects_pr_retargeted_while_processor_runs(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)

    def processor(product_path: Path, **kwargs) -> ProcessResult:
        result = successful_processor(product_path, **kwargs)
        github.base_branch = "attacker-base"
        return result

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_BASE_BRANCH_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))

    assert git.pushes == []
    assert github.statuses == []


def test_processing_accepts_historical_changeset_marker_without_active_binding(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    marker = (
        tmp_path
        / "products"
        / "sales-order"
        / "changesets"
        / ("cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json")
    )
    marker.parent.mkdir()
    marker.write_text("{}", encoding="utf-8")
    provider_loaded = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    git = FakeGit()
    result = ProcessingService(
        RepositoryPaths(tmp_path),
        git,
        FakeGitHub(git),
        processor=successful_processor,
        provider_factory=provider_factory,
    ).run(request(tmp_path))

    assert provider_loaded is True
    assert result.status is WorkflowStatus.SUCCESS


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
            ProviderFailureKind.TRANSIENT,
            WorkflowTransientError,
            30,
            id="transient",
        ),
        pytest.param(
            ProviderFailureKind.OUTPUT,
            WorkflowValidationError,
            10,
            id="output",
        ),
    ],
)
def test_processing_maps_provider_failure_kind_to_workflow_exit_contract(
    tmp_path: Path,
    kind: ProviderFailureKind,
    expected_error: type[Exception],
    expected_exit_code: int,
) -> None:
    repository(tmp_path)
    git = FakeGit()

    def fail_provider(*args, **kwargs):
        try:
            raise RuntimeError("sentinel-provider-response")
        except RuntimeError as error:
            raise ProviderExecutionError(
                "LLM_PROVIDER_SAFE_CODE",
                kind=kind,
            ) from error

    with pytest.raises(expected_error, match="LLM_PROVIDER_SAFE_CODE") as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=fail_provider,
            provider_factory=lambda: object(),
        ).run(request(tmp_path))

    assert captured.value.exit_code == expected_exit_code
    assert "sentinel-provider-response" not in "".join(traceback.format_exception(captured.value))


def test_processing_status_failure_after_push_is_partial(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    github.fail_status = True

    with pytest.raises(
        WorkflowPartialError,
        match="PROCESSING_POST_COMMIT_FAILED",
    ) as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))

    assert git.remote_sha == NEW_SHA
    assert captured.value.outputs["current_head"] == NEW_SHA
    assert captured.value.outputs["expected_head"] == OLD_SHA
    assert captured.value.mutations[0].resource == "commit"


def test_processing_push_failure_after_commit_is_partial(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FailingPushGit()

    with pytest.raises(
        WorkflowPartialError,
        match="PROCESSING_POST_COMMIT_FAILED",
    ) as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))

    assert captured.value.outputs["current_head"] == NEW_SHA
    assert captured.value.mutations == [
        MutationRecord(resource="commit", target=NEW_SHA, action="create")
    ]


def test_processing_validates_changeset_binding_before_commit(tmp_path: Path) -> None:
    repository(tmp_path)
    bind_changeset(tmp_path)
    git = FakeGit()

    def corrupt_binding(product_path: Path, **kwargs) -> ProcessResult:
        (product_path / "product.yaml").write_text(
            f"product_id: {PRODUCT_ID}\nversion: 1\nchangeset_id:\n",
            encoding="utf-8",
        )
        return successful_processor(product_path, **kwargs)

    changeset_request = request(tmp_path).model_copy(
        update={"branch": f"ard/{CHANGESET_ID}-sales-order"}
    )
    github = FakeGitHub(git)
    github.head_branch = changeset_request.branch
    with pytest.raises(WorkflowSecurityError):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=corrupt_binding,
            provider_factory=lambda: None,
        ).run(changeset_request)

    assert git.sha == OLD_SHA
    assert git.pushes == []


def test_processing_rejects_descendant_writeback_head_as_untrusted(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_HEAD_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))


def test_processing_reconcile_uses_same_job_partial_envelope(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "invocation_id": INVOCATION_ID,
                "failure_exit_code": 70,
                "current_head": NEW_SHA,
                "expected_head": OLD_SHA,
                "product_id": PRODUCT_ID,
                "product_key": "sales-order",
                "version": 1,
            },
            findings=[
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                }
            ],
            mutations=[MutationRecord(resource="commit", target=NEW_SHA, action="create")],
        ).model_dump_json(),
        encoding="utf-8",
    )
    github = FakeGitHub(git)

    result = ProcessingReconcileService(
        RepositoryPaths(tmp_path),
        git,
        github,
    ).run(
        ProcessingReconcileRequest(
            repository=tmp_path,
            result_path=result_path,
            branch="ard/example",
            pr_number=7,
            invocation_id=INVOCATION_ID,
        )
    )

    assert result.outputs["current_head"] == NEW_SHA
    assert github.statuses == [
        (NEW_SHA, "ard/changeset", "success"),
        (NEW_SHA, "ard/quality-gate", "success"),
    ]


def test_processing_reconcile_replays_safe_non_partial_failure(tmp_path: Path) -> None:
    repository(tmp_path)
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "invocation_id": INVOCATION_ID,
                "failure_exit_code": 20,
            },
            findings=[
                {
                    "code": "LLM_PROVIDER_AUTHENTICATION_FAILED",
                    "message": "LLM_PROVIDER_AUTHENTICATION_FAILED",
                }
            ],
            retryable=False,
        ).model_dump_json(),
        encoding="utf-8",
    )
    git = FakeGit()

    with pytest.raises(
        WorkflowError,
        match="LLM_PROVIDER_AUTHENTICATION_FAILED",
    ) as captured:
        ProcessingReconcileService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
        ).run(
            ProcessingReconcileRequest(
                repository=tmp_path,
                result_path=result_path,
                branch="ard/example",
                pr_number=7,
                invocation_id=INVOCATION_ID,
            )
        )

    assert captured.value.code == "LLM_PROVIDER_AUTHENTICATION_FAILED"
    assert captured.value.exit_code is ExitCode.CONFIGURATION
    assert captured.value.retryable is False


def test_processing_reconcile_rejects_result_from_another_invocation(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "invocation_id": "31543231017-0",
                "failure_exit_code": 20,
            },
            findings=[
                {
                    "code": "LLM_PROVIDER_AUTHENTICATION_FAILED",
                    "message": "LLM_PROVIDER_AUTHENTICATION_FAILED",
                }
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(
        WorkflowSecurityError,
        match="PROCESSING_RECONCILE_INVOCATION_MISMATCH",
    ):
        ProcessingReconcileService(
            RepositoryPaths(tmp_path),
            FakeGit(),
            FakeGitHub(FakeGit()),
        ).run(
            ProcessingReconcileRequest(
                repository=tmp_path,
                result_path=result_path,
                branch="ard/example",
                pr_number=7,
                invocation_id=INVOCATION_ID,
            )
        )


@pytest.mark.parametrize(
    ("failure_exit_code", "findings"),
    [
        pytest.param(
            30,
            [
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                }
            ],
            id="wrong-partial-exit-code",
        ),
        pytest.param(
            70,
            [
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                },
                {"code": "EXTRA_FINDING", "message": "EXTRA_FINDING"},
            ],
            id="extra-partial-finding",
        ),
    ],
)
def test_processing_reconcile_rejects_malformed_partial_envelope(
    tmp_path: Path,
    failure_exit_code: int,
    findings: list[dict[str, str]],
) -> None:
    repository(tmp_path)
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "invocation_id": INVOCATION_ID,
                "failure_exit_code": failure_exit_code,
                "current_head": NEW_SHA,
                "expected_head": OLD_SHA,
                "product_id": PRODUCT_ID,
                "product_key": "sales-order",
                "version": 1,
            },
            findings=findings,
            mutations=[MutationRecord(resource="commit", target=NEW_SHA, action="create")],
        ).model_dump_json(),
        encoding="utf-8",
    )
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA

    with pytest.raises(
        WorkflowSecurityError,
        match="PROCESSING_RECONCILE_RESULT_INVALID",
    ):
        ProcessingReconcileService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
        ).run(
            ProcessingReconcileRequest(
                repository=tmp_path,
                result_path=result_path,
                branch="ard/example",
                pr_number=7,
                invocation_id=INVOCATION_ID,
            )
        )


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.FAILURE,
                outputs={"invocation_id": INVOCATION_ID},
                findings=[{"code": "LLM_PROVIDER_FAILURE", "message": "safe"}],
            ),
            id="missing-exit-code",
        ),
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.FAILURE,
                outputs={
                    "invocation_id": INVOCATION_ID,
                    "failure_exit_code": 20,
                },
                findings=[{"code": "unsafe code", "message": "safe"}],
            ),
            id="invalid-finding-code",
        ),
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.FAILURE,
                outputs={
                    "invocation_id": INVOCATION_ID,
                    "failure_exit_code": 99,
                },
                findings=[{"code": "LLM_PROVIDER_FAILURE", "message": "safe"}],
            ),
            id="unknown-exit-code",
        ),
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.FAILURE,
                outputs={
                    "invocation_id": INVOCATION_ID,
                    "failure_exit_code": 70,
                },
                findings=[{"code": "LLM_PROVIDER_FAILURE", "message": "safe"}],
            ),
            id="partial-exit-for-non-partial-failure",
        ),
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.FAILURE,
                outputs={
                    "invocation_id": INVOCATION_ID,
                    "failure_exit_code": 30,
                },
                findings=[{"code": "LLM_PROVIDER_FAILURE", "message": "safe"}],
                retryable=False,
            ),
            id="retryability-mismatch",
        ),
    ],
)
def test_processing_reconcile_rejects_invalid_recorded_failure(
    tmp_path: Path,
    result: WorkflowResult,
) -> None:
    repository(tmp_path)
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    git = FakeGit()

    with pytest.raises(
        WorkflowSecurityError,
        match="PROCESSING_RECONCILE_RESULT_INVALID",
    ):
        ProcessingReconcileService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
        ).run(
            ProcessingReconcileRequest(
                repository=tmp_path,
                result_path=result_path,
                branch="ard/example",
                pr_number=7,
                invocation_id=INVOCATION_ID,
            )
        )


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            WorkflowResult(
                command="workflow.process-reconcile",
                status=WorkflowStatus.FAILURE,
                outputs={"invocation_id": INVOCATION_ID},
            ),
            id="wrong-command",
        ),
        pytest.param(
            WorkflowResult(
                command="workflow.process",
                status=WorkflowStatus.SUCCESS,
                outputs={"invocation_id": INVOCATION_ID},
            ),
            id="successful-process-result",
        ),
    ],
)
def test_processing_reconcile_rejects_non_failure_process_envelope(
    tmp_path: Path,
    result: WorkflowResult,
) -> None:
    repository(tmp_path)
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(result.model_dump_json(), encoding="utf-8")

    with pytest.raises(
        WorkflowSecurityError,
        match="PROCESSING_RECONCILE_RESULT_NOT_PARTIAL",
    ):
        git = FakeGit()
        ProcessingReconcileService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
        ).run(
            ProcessingReconcileRequest(
                repository=tmp_path,
                result_path=result_path,
                branch="ard/example",
                pr_number=7,
                invocation_id=INVOCATION_ID,
            )
        )


def test_processing_reconcile_retries_consecutive_transient_failures(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "invocation_id": INVOCATION_ID,
                "failure_exit_code": 70,
                "current_head": NEW_SHA,
                "expected_head": OLD_SHA,
                "product_id": PRODUCT_ID,
                "product_key": "sales-order",
                "version": 1,
                "changeset_id": CHANGESET_ID,
            },
            findings=[
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                }
            ],
            mutations=[MutationRecord(resource="commit", target=NEW_SHA, action="create")],
        ).model_dump_json(),
        encoding="utf-8",
    )
    github = FlakyReconcileGitHub(git)

    result = ProcessingReconcileService(
        RepositoryPaths(tmp_path),
        git,
        github,
        retry_attempts=3,
        retry_delay_seconds=0,
        sleeper=lambda _: None,
    ).run(
        ProcessingReconcileRequest(
            repository=tmp_path,
            result_path=result_path,
            branch="ard/example",
            pr_number=7,
            invocation_id=INVOCATION_ID,
        )
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert github.get_status_failures == 0
    assert github.set_status_failures == 0
    assert github.dispatch_attempts == 3
    assert github.dispatched == 1
