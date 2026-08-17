"""Load semantic replay authority from a revision-pinned trusted base."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, RootModel

from ard_ossie.application.contracts import (
    WorkflowConflict,
    WorkflowSecurityError,
    WorkflowValidationError,
)
from ard_ossie.ingestion import SourceRole
from ard_ossie.models import ProductId, ProductKey, Sha256
from ard_ossie.pipeline import QualityReport
from ard_ossie.ports.git import GitPort
from ard_ossie.semantic.adjudication import DecisionReport
from ard_ossie.semantic.canonical import (
    SemanticPipelineStatus,
    SemanticValidationReport,
)
from ard_ossie.semantic.diagnostics import DIAGNOSTIC_REPORT_NAMES
from ard_ossie.semantic.models import ImmutableStrictModel
from ard_ossie.semantic.replay import (
    SemanticReplayBaseline,
    SemanticReplayBaselineConflict,
    SemanticReplayCatalog,
    semantic_replay_identity,
)


class StoredSourceFile(ImmutableStrictModel):
    role: SourceRole
    relative_path: str = Field(min_length=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class StoredSourceManifest(ImmutableStrictModel):
    files: tuple[StoredSourceFile, ...]

    def semantic_hash(self) -> Sha256:
        matches = tuple(
            item.sha256 for item in self.files if item.role is SourceRole.SEMANTIC_DOCUMENT
        )
        if len(matches) != 1:
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        return matches[0]


class ProductKeyIndex(RootModel[dict[ProductKey, ProductId]]):
    pass


class StoredSemanticDiagnosticsManifest(ImmutableStrictModel):
    schema_version: Literal["semantic-diagnostics-v1"]
    source_hash: Sha256
    configuration_hash: Sha256
    mode: Literal["legacy", "shadow", "candidate"]
    publication_status: str = Field(min_length=1)
    reports: dict[str, Sha256]


_DIAGNOSTIC_PAYLOAD_NAMES = tuple(
    name for name in DIAGNOSTIC_REPORT_NAMES if name != "manifest.json"
)
_DIAGNOSTIC_REPORT_SET = frozenset(DIAGNOSTIC_REPORT_NAMES)
_DIAGNOSTIC_PAYLOAD_SET = frozenset(_DIAGNOSTIC_PAYLOAD_NAMES)


def load_semantic_replay_catalog(
    git: GitPort,
    *,
    base_sha: str,
    product_key: str,
    semantic_source_hash: Sha256,
) -> SemanticReplayCatalog:
    try:
        if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        index_bytes = _read_optional(
            git,
            base_sha,
            Path("registry/indexes/product-keys.json"),
        )
        if index_bytes is None:
            return SemanticReplayCatalog()
        index = ProductKeyIndex.model_validate_json(index_bytes)
        ordered_keys = (
            *((product_key,) if product_key in index.root else ()),
            *(key for key in sorted(index.root) if key != product_key),
        )
        entries = tuple(
            baseline
            for key in ordered_keys
            if (
                baseline := _load_matching_baseline(
                    git,
                    base_sha=base_sha,
                    product_key=key,
                    semantic_source_hash=semantic_source_hash,
                )
            )
            is not None
        )
        return SemanticReplayCatalog.build(entries)
    except WorkflowConflict:
        raise _trust_mismatch() from None
    except SemanticReplayBaselineConflict:
        raise WorkflowValidationError(
            "SEMANTIC_REPLAY_BASELINE_CONFLICT",
            "trusted semantic replay baselines conflict",
        ) from None
    except ValueError:
        raise _trust_mismatch() from None


def _load_matching_baseline(
    git: GitPort,
    *,
    base_sha: str,
    product_key: str,
    semantic_source_hash: Sha256,
) -> SemanticReplayBaseline | None:
    root = Path("products") / product_key
    manifest_bytes = _read_optional(
        git,
        base_sha,
        root / "generated" / "source-manifest.json",
    )
    if manifest_bytes is None:
        return None
    manifest = StoredSourceManifest.model_validate_json(manifest_bytes)
    if manifest.semantic_hash() != semantic_source_hash:
        return None

    markdown_bytes = _read_optional(
        git,
        base_sha,
        root / "generated" / "data-semantic.md",
    )
    quality_bytes = _read_optional(
        git,
        base_sha,
        root / "quality" / "quality-report.json",
    )
    decision_bytes = _read_optional(
        git,
        base_sha,
        root / "quality" / "decision-report.json",
    )
    validation_bytes = _read_optional(
        git,
        base_sha,
        root / "quality" / "validation-report.json",
    )
    diagnostics_manifest_bytes = _read_optional(
        git,
        base_sha,
        root / "quality" / "manifest.json",
    )
    if quality_bytes is None:
        if any(
            payload is not None
            for payload in (
                decision_bytes,
                validation_bytes,
                diagnostics_manifest_bytes,
            )
        ):
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        return None
    quality = QualityReport.model_validate_json(quality_bytes)
    declared_diagnostics = set(quality.quality_artifact_hashes) & _DIAGNOSTIC_REPORT_SET
    manifest_declared = "manifest.json" in declared_diagnostics
    if diagnostics_manifest_bytes is None:
        if declared_diagnostics or decision_bytes is not None or validation_bytes is not None:
            raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
        return None
    if not manifest_declared:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    _verify_hash(
        quality.quality_artifact_hashes,
        "manifest.json",
        diagnostics_manifest_bytes,
    )
    diagnostics_manifest = StoredSemanticDiagnosticsManifest.model_validate_json(
        diagnostics_manifest_bytes
    )
    if diagnostics_manifest.source_hash != semantic_source_hash:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    if diagnostics_manifest.mode != "candidate":
        return None
    if set(diagnostics_manifest.reports) != _DIAGNOSTIC_PAYLOAD_SET:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    diagnostic_payloads: dict[str, bytes | None] = {
        "decision-report.json": decision_bytes,
        "validation-report.json": validation_bytes,
    }
    for name in _DIAGNOSTIC_PAYLOAD_NAMES:
        if name not in diagnostic_payloads:
            diagnostic_payloads[name] = _read_optional(
                git,
                base_sha,
                root / "quality" / name,
            )
    if any(
        payload is None
        for payload in (
            markdown_bytes,
            quality_bytes,
            *diagnostic_payloads.values(),
        )
    ):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    assert markdown_bytes is not None
    assert quality_bytes is not None
    assert decision_bytes is not None
    assert validation_bytes is not None
    markdown_bytes.decode("utf-8", errors="strict")

    decisions = DecisionReport.model_validate_json(decision_bytes)
    validation = SemanticValidationReport.model_validate_json(validation_bytes)
    _verify_hash(quality.artifact_hashes, "source-manifest.json", manifest_bytes)
    _verify_hash(quality.artifact_hashes, "data-semantic.md", markdown_bytes)
    for name, payload in diagnostic_payloads.items():
        assert payload is not None
        _verify_hash(quality.quality_artifact_hashes, name, payload)
        _verify_hash(diagnostics_manifest.reports, name, payload)
    if decisions.source_hash != semantic_source_hash:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    identity = semantic_replay_identity(decisions)
    if validation.source_hash != semantic_source_hash:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    if diagnostics_manifest.publication_status != validation.status.value:
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    if quality.hard_errors or validation.status is not SemanticPipelineStatus.VERIFIED:
        return None
    if (
        not validation.publishable
        or validation.findings
        or validation.character_coverage != 1.0
        or validation.missing_atom_count != 0
        or validation.duplicate_atom_count != 0
        or validation.degraded_block_count != 0
    ):
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")
    return SemanticReplayBaseline(
        product_key=product_key,
        identity=identity,
        canonical_markdown=markdown_bytes,
        decisions=decisions,
    )


def _verify_hash(
    hashes: Mapping[str, Sha256],
    name: str,
    payload: bytes,
) -> None:
    if hashes.get(name) != hashlib.sha256(payload).hexdigest():
        raise ValueError("SEMANTIC_REPLAY_TRUST_MISMATCH")


def _read_optional(
    git: GitPort,
    revision: str,
    path: Path,
) -> bytes | None:
    try:
        return git.read_bytes_at(revision, path)
    except WorkflowConflict as error:
        if error.code == "REVISION_FILE_NOT_FOUND":
            return None
        raise


def _trust_mismatch() -> WorkflowSecurityError:
    return WorkflowSecurityError(
        "SEMANTIC_REPLAY_TRUST_MISMATCH",
        "trusted semantic replay artifacts failed verification",
    )
