from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from ard_ossie.application.contracts import (
    WorkflowConflict,
    WorkflowSecurityError,
    WorkflowValidationError,
)
from ard_ossie.pipeline import QualityReport, QualityStatus
from ard_ossie.semantic.adjudication import DecisionRecord, DecisionReport
from ard_ossie.semantic.canonical import (
    SemanticPipelineStatus,
    SemanticValidationReport,
)

BASE_SHA = "1" * 40
SOURCE_HASH = "a" * 64
OTHER_SOURCE_HASH = "b" * 64
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
CANONICAL_BYTES = "Data Semantics 정의서이며\n".encode()


class FakeGit:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[tuple[str, str]] = []

    def read_bytes_at(self, revision: str, path: str | Path) -> bytes:
        relative = Path(path).as_posix()
        self.reads.append((revision, relative))
        if revision != BASE_SHA or relative not in self.files:
            raise WorkflowConflict("REVISION_FILE_NOT_FOUND", relative)
        return self.files[relative]

    @property
    def product_read_order(self) -> list[str]:
        return [
            Path(path).parts[1]
            for _revision, path in self.reads
            if path.endswith("generated/source-manifest.json")
        ]


def _loader():
    return importlib.import_module("ard_ossie.application.semantic_replay")


def _decision_report(source_hash: str = SOURCE_HASH) -> DecisionReport:
    return DecisionReport(
        source_hash=source_hash,
        decisions=(
            DecisionRecord(
                decision_id="decision_0000000000000001",
                request_hash="d" * 64,
                source_hash=source_hash,
                evidence_hash="e" * 64,
                candidate_set_id="candidate_set_0000000000000001",
                region_id="region_0000000000000001",
                decision_type="spacing",
                selected_candidate_id="candidate_0000000000000001",
                outcome="selected",
                source="deterministic",
                confidence=1.0,
                provider="deterministic",
                model="deterministic",
            ),
        ),
    )


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _validation(source_hash: str = SOURCE_HASH) -> SemanticValidationReport:
    return SemanticValidationReport(
        status="verified",
        publishable=True,
        source_hash=source_hash,
        canonical_hash="c" * 64,
        findings=[],
        character_coverage=1.0,
        missing_atom_count=0,
        duplicate_atom_count=0,
        degraded_block_count=0,
        model_call_count=0,
    )


def _replace_hashed_payload(
    files: dict[str, bytes],
    *,
    product_key: str,
    name: str,
    payload: bytes,
    generated: bool,
) -> None:
    root = f"products/{product_key}"
    directory = "generated" if generated else "quality"
    files[f"{root}/{directory}/{name}"] = payload
    quality_path = f"{root}/quality/quality-report.json"
    quality = json.loads(files[quality_path])
    field = "artifact_hashes" if generated else "quality_artifact_hashes"
    quality[field][name] = hashlib.sha256(payload).hexdigest()
    files[quality_path] = _json_bytes(quality)


def _verified_tree(
    *keys: str,
    source_hash: str = SOURCE_HASH,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "registry/indexes/product-keys.json": _json_bytes({key: PRODUCT_ID for key in keys})
    }
    for key in keys:
        manifest = _json_bytes(
            {
                "files": [
                    {
                        "role": "semantic_document",
                        "relative_path": "semantic/semantic.pdf",
                        "sha256": source_hash,
                        "size_bytes": 7,
                    }
                ]
            }
        )
        decisions = _json_bytes(_decision_report(source_hash))
        validation = _json_bytes(_validation(source_hash))
        quality = QualityReport(
            status=QualityStatus.PASS,
            product_id=PRODUCT_ID,
            product_version=1,
            completeness=1.0,
            hard_errors=[],
            warnings=[],
            artifact_hashes={
                "source-manifest.json": hashlib.sha256(manifest).hexdigest(),
                "data-semantic.md": hashlib.sha256(CANONICAL_BYTES).hexdigest(),
            },
            quality_artifact_hashes={
                "decision-report.json": hashlib.sha256(decisions).hexdigest(),
                "validation-report.json": hashlib.sha256(validation).hexdigest(),
            },
        )
        root = f"products/{key}"
        files.update(
            {
                f"{root}/generated/source-manifest.json": manifest,
                f"{root}/generated/data-semantic.md": CANONICAL_BYTES,
                f"{root}/quality/quality-report.json": _json_bytes(quality),
                f"{root}/quality/decision-report.json": decisions,
                f"{root}/quality/validation-report.json": validation,
            }
        )
    return files


def _load(git: FakeGit, *, product_key: str = "current"):
    return _loader().load_semantic_replay_catalog(
        git,
        base_sha=BASE_SHA,
        product_key=product_key,
        semantic_source_hash=SOURCE_HASH,
    )


def test_loader_reads_current_product_then_lexical_keys_from_exact_base() -> None:
    git = FakeGit(_verified_tree("zeta", "current", "alpha"))

    catalog = _load(git)

    assert catalog.baselines[0].product_key == "current"
    assert catalog.canonical_markdown_for(_decision_report()) == CANONICAL_BYTES
    assert git.reads[0] == (BASE_SHA, "registry/indexes/product-keys.json")
    assert git.product_read_order == ["current", "alpha", "zeta"]
    assert all(revision == BASE_SHA for revision, _path in git.reads)


def test_loader_returns_empty_catalog_when_base_has_no_product_index() -> None:
    git = FakeGit({})

    catalog = _load(git, product_key="first-product")

    assert catalog.baselines == ()
    assert git.reads == [(BASE_SHA, "registry/indexes/product-keys.json")]


def test_loader_skips_complete_history_for_different_semantic_source() -> None:
    git = FakeGit(_verified_tree("alpha", source_hash=OTHER_SOURCE_HASH))

    catalog = _load(git, product_key="new-product")

    assert catalog.baselines == ()
    assert git.product_read_order == ["alpha"]


def test_loader_ignores_complete_but_unverified_history() -> None:
    files = _verified_tree("current")
    validation = _validation().model_copy(
        update={
            "status": SemanticPipelineStatus.REVIEW_REQUIRED,
            "publishable": False,
        }
    )
    _replace_hashed_payload(
        files,
        product_key="current",
        name="validation-report.json",
        payload=_json_bytes(validation),
        generated=False,
    )

    assert _load(FakeGit(files)).baselines == ()


def test_loader_ignores_matching_manifest_when_quality_tree_is_wholly_absent() -> None:
    files = _verified_tree("current")
    files = {
        path: payload
        for path, payload in files.items()
        if "/quality/" not in path and not path.endswith("generated/data-semantic.md")
    }

    assert _load(FakeGit(files)).baselines == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_decisions",
        "tampered_markdown",
        "tampered_validation",
        "invalid_manifest_utf8",
        "decision_source_mismatch",
        "decision_report_source_mismatch",
        "validation_source_mismatch",
        "verified_not_publishable",
    ],
)
def test_loader_rejects_untrusted_matching_history(mutation: str) -> None:
    files = _verified_tree("current")
    root = "products/current"
    if mutation == "missing_decisions":
        del files[f"{root}/quality/decision-report.json"]
    elif mutation == "tampered_markdown":
        files[f"{root}/generated/data-semantic.md"] += b" "
    elif mutation == "tampered_validation":
        files[f"{root}/quality/validation-report.json"] += b" "
    elif mutation == "invalid_manifest_utf8":
        files[f"{root}/generated/source-manifest.json"] = b"{\xff}"
    elif mutation == "decision_source_mismatch":
        report = _decision_report()
        report = report.model_copy(
            update={
                "decisions": (
                    report.decisions[0].model_copy(update={"source_hash": OTHER_SOURCE_HASH}),
                )
            }
        )
        _replace_hashed_payload(
            files,
            product_key="current",
            name="decision-report.json",
            payload=_json_bytes(report),
            generated=False,
        )
    elif mutation == "decision_report_source_mismatch":
        _replace_hashed_payload(
            files,
            product_key="current",
            name="decision-report.json",
            payload=_json_bytes(
                _decision_report().model_copy(update={"source_hash": OTHER_SOURCE_HASH})
            ),
            generated=False,
        )
    elif mutation == "validation_source_mismatch":
        _replace_hashed_payload(
            files,
            product_key="current",
            name="validation-report.json",
            payload=_json_bytes(
                _validation().model_copy(update={"source_hash": OTHER_SOURCE_HASH})
            ),
            generated=False,
        )
    elif mutation == "verified_not_publishable":
        _replace_hashed_payload(
            files,
            product_key="current",
            name="validation-report.json",
            payload=_json_bytes(_validation().model_copy(update={"publishable": False})),
            generated=False,
        )

    with pytest.raises(WorkflowSecurityError) as captured:
        _load(FakeGit(files))

    assert captured.value.code == "SEMANTIC_REPLAY_TRUST_MISMATCH"
    assert "정의서이며" not in str(captured.value)


def test_loader_rejects_conflicting_verified_baselines_without_source_text() -> None:
    files = _verified_tree("alpha", "beta")
    _replace_hashed_payload(
        files,
        product_key="beta",
        name="data-semantic.md",
        payload="Data Semantics 정의 서이며\n".encode(),
        generated=True,
    )

    with pytest.raises(WorkflowValidationError) as captured:
        _load(FakeGit(files), product_key="new-product")

    assert captured.value.code == "SEMANTIC_REPLAY_BASELINE_CONFLICT"
    assert "정의" not in str(captured.value)


def test_loader_rejects_malformed_product_index() -> None:
    git = FakeGit({"registry/indexes/product-keys.json": b"[]\n"})

    with pytest.raises(WorkflowSecurityError) as captured:
        _load(git)

    assert captured.value.code == "SEMANTIC_REPLAY_TRUST_MISMATCH"
