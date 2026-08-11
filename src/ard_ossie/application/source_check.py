from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.application.modeling import ModelingService
from ard_ossie.ingestion import SourceValidationError, scan_sources
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, PullRequestState

_PRODUCT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CHANGESET_MARKER = re.compile(
    r"^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


class DetectProductService:
    def __init__(self, git: GitPort) -> None:
        self.git = git

    def run(self, base_ref: str, head_ref: str = "HEAD") -> WorkflowResult:
        changed = self.git.changed_paths(base_ref, head_ref)
        data_paths: list[Path] = []
        code_paths: list[Path] = []
        for path in changed.paths:
            if path.is_absolute() or ".." in path.parts:
                raise WorkflowSecurityError(
                    "UNSAFE_CHANGED_PATH",
                    "changed path is outside the repository",
                )
            if path.parts and path.parts[0] in {"products", "registry"}:
                data_paths.append(path)
            else:
                code_paths.append(path)
        if data_paths and code_paths:
            raise WorkflowValidationError(
                "MIXED_CODE_AND_ARD_CHANGES",
                "direct changes cannot mix ARD data with repository code",
            )

        products: set[str] = set()
        marker_products: set[str] = set()
        config_products: set[str] = set()
        for path in data_paths:
            parts = path.parts
            is_source = len(parts) >= 4 and parts[0] == "products" and parts[2] == "sources"
            is_marker = (
                len(parts) == 4
                and parts[0] == "products"
                and parts[2] == "changesets"
                and _CHANGESET_MARKER.fullmatch(parts[3]) is not None
            )
            is_config = (
                len(parts) == 3
                and parts[0] == "products"
                and parts[2] == "product.yaml"
            )
            if not (is_source or is_marker or is_config):
                raise WorkflowValidationError(
                    "DIRECT_CHANGE_PATH_NOT_ALLOWED",
                    "direct ARD changes are limited to one product source tree and marker",
                )
            if not _PRODUCT_KEY.fullmatch(parts[1]):
                raise WorkflowValidationError(
                    "INVALID_PRODUCT_KEY",
                    "changed product key is not canonical",
                )
            if is_source:
                products.add(parts[1])
            elif is_marker:
                marker_products.add(parts[1])
            else:
                config_products.add(parts[1])
        if len(products) > 1:
            raise WorkflowValidationError(
                "MULTIPLE_PRODUCTS_NOT_ALLOWED",
                "direct changes must target exactly one product",
            )
        if marker_products and marker_products != products:
            raise WorkflowValidationError(
                "CHANGESET_MARKER_PRODUCT_MISMATCH",
                "changeset marker must accompany sources for the same product",
            )
        if config_products and (
            config_products != products or config_products != marker_products
        ):
            raise WorkflowValidationError(
                "CHANGESET_CONFIG_PRODUCT_MISMATCH",
                "product config may change only with its canonical changeset marker",
            )

        outputs: dict[str, object] = {
            "expected_head": self.git.current_sha(),
            "merge_base": changed.merge_base,
        }
        if products:
            outputs["product_key"] = next(iter(products))
        return WorkflowResult(
            command="workflow.detect-product",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
        )


class SourceCheckService:
    def __init__(self, paths: FileSystemPort) -> None:
        self.paths = paths

    def run(self, product_key: str, expected_head: str) -> WorkflowResult:
        _validate_product_key(product_key)
        _validate_sha(expected_head)
        if os.environ.get("ARD_LLM_API_KEY") is not None:
            raise WorkflowSecurityError(
                "SOURCE_CHECK_LLM_SECRET_PRESENT",
                "source-check must run without LLM credentials",
            )
        product = self.paths.resolve_read(Path("products") / product_key)
        changeset_id = validate_changeset_binding(self.paths, product, product_key)
        try:
            manifest = scan_sources(product / "sources")
        except SourceValidationError as error:
            raise WorkflowValidationError(_error_code(error), "source validation failed") from error
        validation = ModelingService(self.paths).validate(product, "registry")
        if not validation.passed:
            code = validation.findings[0].code if validation.findings else "MODEL_VALIDATION_FAILED"
            raise WorkflowValidationError(code, "staged product validation failed")
        outputs: dict[str, object] = {
            "expected_head": expected_head,
            "product_key": product_key,
            "source_count": len(manifest.files),
            "source_hashes": {
                item.role.value: item.sha256 for item in manifest.files
            },
        }
        if changeset_id:
            outputs["changeset_id"] = changeset_id
        return WorkflowResult(
            command="workflow.source-check",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
        )


class EnsureProductPrService:
    def __init__(self, git: GitPort, github: GitHubPort) -> None:
        self.git = git
        self.github = github

    def run(
        self,
        branch: str,
        product_key: str,
        expected_head: str,
        *,
        base_branch: str,
    ) -> WorkflowResult:
        _validate_product_key(product_key)
        _validate_sha(expected_head)
        remote_head = self.git.remote_branch_sha(branch)
        if remote_head != expected_head:
            raise WorkflowSecurityError(
                "DIRECT_BRANCH_HEAD_MISMATCH",
                "remote branch no longer matches the validated head",
            )
        existing = self.github.find_open_pr(branch)
        mutations: list[MutationRecord] = []
        if existing is not None:
            _require_pr(existing, base_branch, expected_head)
            pull_request = existing
            status = WorkflowStatus.NOOP
        else:
            pull_request = self.github.create_draft_pr(
                branch,
                base_branch,
                f"data({product_key}): update ARD sources",
                "Automated ARD validation and Ossie generation.",
            )
            _require_pr(pull_request, base_branch, expected_head)
            mutations.append(
                MutationRecord(
                    resource="pull_request",
                    target=f"pr:{pull_request.number}",
                    action="create",
                    result_id=str(pull_request.number),
                )
            )
            status = WorkflowStatus.SUCCESS
        return WorkflowResult(
            command="workflow.ensure-product-pr",
            status=status,
            outputs={
                "branch": branch,
                "expected_head": expected_head,
                "pr_number": pull_request.number,
                "product_key": product_key,
            },
            mutations=mutations,
        )


def _require_pr(pr: PullRequestState, base_branch: str, expected_head: str) -> None:
    if pr.base_branch != base_branch or pr.head_sha != expected_head or not pr.draft:
        raise WorkflowSecurityError(
            "DIRECT_PULL_REQUEST_MISMATCH",
            "managed pull request does not match the validated branch head",
        )


def validate_changeset_binding(
    paths: FileSystemPort,
    product: Path,
    product_key: str,
) -> str | None:
    try:
        config = yaml.safe_load(
            paths.resolve_read(product / "product.yaml").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise WorkflowValidationError(
            "INVALID_PRODUCT_CONFIG",
            "product config is malformed",
        ) from error
    if not isinstance(config, dict):
        raise WorkflowValidationError(
            "INVALID_PRODUCT_CONFIG",
            "product config is not a mapping",
        )
    changeset_id = config.get("changeset_id")
    marker_root = product / "changesets"
    markers = sorted(marker_root.glob("cst_*.json")) if marker_root.is_dir() else []
    if not markers and not changeset_id:
        return None
    if not isinstance(changeset_id, str) or not changeset_id:
        raise WorkflowSecurityError(
            "CHANGESET_BINDING_REQUIRED",
            "tracking marker requires product config changeset binding",
        )
    marker_path = marker_root / f"{changeset_id}.json"
    if marker_path not in markers:
        raise WorkflowSecurityError(
            "CHANGESET_MARKER_REQUIRED",
            "product changeset requires its canonical tracking marker",
        )
    try:
        marker = json.loads(paths.resolve_read(marker_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WorkflowSecurityError(
            "CHANGESET_MARKER_INVALID",
            "tracking marker is malformed",
        ) from error
    if not isinstance(marker, dict) or marker != {
        "changeset_id": changeset_id,
        "product_id": config.get("product_id"),
        "status": "required",
    }:
        raise WorkflowSecurityError(
            "CHANGESET_MARKER_MISMATCH",
            "tracking marker does not match product config",
        )
    if config.get("product_key") not in (None, product_key):
        raise WorkflowSecurityError(
            "CHANGESET_PRODUCT_KEY_MISMATCH",
            "tracking product key does not match its directory",
        )
    return changeset_id


def _validate_product_key(value: str) -> None:
    if Path(value).name != value or any(token in value for token in ("/", "\\", "\x00", "..")):
        raise WorkflowSecurityError(
            "PRODUCT_KEY_PATH_ESCAPE",
            "product key must not contain path syntax",
        )
    if not _PRODUCT_KEY.fullmatch(value):
        raise WorkflowValidationError("INVALID_PRODUCT_KEY", "product key is not canonical")


def _validate_sha(value: str) -> None:
    if not _SHA.fullmatch(value):
        raise WorkflowValidationError("INVALID_EXPECTED_HEAD", "expected head is not a SHA")


def _error_code(error: Exception) -> str:
    return str(error).partition(":")[0] or type(error).__name__
