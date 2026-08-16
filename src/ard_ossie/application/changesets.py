from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowConflict,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.impact import ChangeSetRecord, ProductReadiness, build_changeset
from ard_ossie.models import ProductRecord, StrictModel
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, PullRequestState
from ard_ossie.registry import Registry


class ChangesetRequest(StrictModel):
    repository: Path
    mode: Literal["create", "ready"]
    changeset_id: str = Field(
        pattern=r"^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    table_ids: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    product_id: str | None = None
    version: int | None = Field(default=None, ge=1, le=999)
    pr_number: int | None = Field(default=None, gt=0)
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    initiating_pr: int | None = Field(default=None, gt=0)
    base_branch: str
    target_url: str = ""

    @model_validator(mode="after")
    def validate_mode_inputs(self) -> ChangesetRequest:
        if self.mode == "create":
            if not self.table_ids or not self.product_ids:
                raise ValueError("CHANGESET_CREATE_INPUTS_INCOMPLETE")
            if any(
                value is not None
                for value in (self.product_id, self.version, self.pr_number, self.head_sha)
            ):
                raise ValueError("CHANGESET_CREATE_INPUTS_CONFLICT")
        elif any(
            value is None
            for value in (self.product_id, self.version, self.pr_number, self.head_sha)
        ):
            raise ValueError("CHANGESET_READY_INPUTS_INCOMPLETE")
        return self


class ChangesetService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github

    def run(self, request: ChangesetRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "CHANGESET_REPOSITORY_MISMATCH",
                "changeset repository does not match filesystem port",
            )
        if request.mode == "create":
            return self._create(request)
        return self._ready(request)

    def _create(self, request: ChangesetRequest) -> WorkflowResult:
        branch = _coordination_branch(request.changeset_id)
        self._switch_existing_scoped_branch(
            branch,
            request.base_branch,
            {Path("registry") / "changesets" / f"{request.changeset_id}.json"},
            "CHANGESET_COORDINATION_PATH_MISMATCH",
        )
        registry = Registry.load(self.paths.resolve_read("registry"))
        table_ids = sorted(set(request.table_ids))
        product_ids = sorted(set(request.product_ids))
        _require_registry_entities(registry, table_ids, product_ids)
        existing = registry.get_changeset(request.changeset_id)
        state_changed = existing is None
        if existing is None:
            record = build_changeset(
                table_ids,
                product_ids,
                changeset_id=request.changeset_id,
            )
            registry.write_changeset(record)
        else:
            record = existing
            if record.table_ids != table_ids or record.required_product_ids != product_ids:
                raise WorkflowConflict(
                    "CHANGESET_DEFINITION_CONFLICT",
                    "existing changeset has different tables or products",
                )

        mutations: list[MutationRecord] = []
        commit = self.git.commit_changeset_paths(
            request.changeset_id,
            None,
            f"data(changeset): update {request.changeset_id}",
        )
        mutations.append(_commit_mutation(commit.sha, commit.created))
        if commit.created:
            self.git.push(branch)
        coordination_pr, created = self._ensure_pr(
            branch,
            request.base_branch,
            f"data(changeset): {request.changeset_id}",
            f"Central readiness state for `{request.changeset_id}`.",
        )
        if created:
            mutations.append(_pr_mutation(coordination_pr))
        mutations.extend(self._central_statuses(coordination_pr.head_sha, request.target_url))

        tracking_prs: list[PullRequestState] = []
        for product_id in record.required_product_ids:
            product = registry.get_product(product_id)
            assert product is not None
            tracking_branch = f"ard/{request.changeset_id}-{product.product_key}"
            marker = (
                self.paths.root
                / "products"
                / product.product_key
                / "changesets"
                / f"{request.changeset_id}.json"
            )
            self._switch_existing_scoped_branch(
                tracking_branch,
                request.base_branch,
                {marker.relative_to(self.paths.root)},
                "CHANGESET_TRACKING_PATH_MISMATCH",
            )
            if self.git.remote_branch_sha(tracking_branch) is not None:
                _require_tracking_marker(
                    self.git,
                    self.git.current_sha(),
                    marker.relative_to(self.paths.root),
                    request.changeset_id,
                    product_id,
                )
            _atomic_json(
                marker,
                {
                    "changeset_id": request.changeset_id,
                    "product_id": product_id,
                    "status": "required",
                },
            )
            tracking_commit = self.git.commit_changeset_paths(
                request.changeset_id,
                product.product_key,
                f"data({product.product_key}): track {request.changeset_id}",
            )
            mutations.append(_commit_mutation(tracking_commit.sha, tracking_commit.created))
            if tracking_commit.created:
                self.git.push(tracking_branch)
            tracking_pr, tracking_created = self._ensure_pr(
                tracking_branch,
                request.base_branch,
                f"data({product.product_key}): apply {request.changeset_id}",
                f"Required product update for `{request.changeset_id}`.",
            )
            tracking_prs.append(tracking_pr)
            if tracking_created:
                mutations.append(_pr_mutation(tracking_pr))
            mutations.append(
                self.github.set_status(
                    tracking_pr.head_sha,
                    "ard/changeset",
                    "pending",
                    f"Waiting for {request.changeset_id}",
                    request.target_url,
                )
            )

        if request.initiating_pr is not None:
            mutations.append(
                self.github.upsert_pr_comment(
                    request.initiating_pr,
                    "ard:changeset-impact",
                    _impact_comment(record, coordination_pr.number),
                )
            )
        return _result(
            request,
            record,
            coordination_pr,
            mutations,
            WorkflowStatus.SUCCESS if state_changed or created else WorkflowStatus.NOOP,
        )

    def _ready(self, request: ChangesetRequest) -> WorkflowResult:
        branch = _coordination_branch(request.changeset_id)
        self._switch_existing_scoped_branch(
            branch,
            request.base_branch,
            {Path("registry") / "changesets" / f"{request.changeset_id}.json"},
            "CHANGESET_COORDINATION_PATH_MISMATCH",
        )
        registry = Registry.load(self.paths.resolve_read("registry"))
        record = registry.get_changeset(request.changeset_id)
        if record is None:
            raise WorkflowValidationError("CHANGESET_NOT_FOUND", "changeset does not exist")
        product_id = request.product_id or ""
        product = registry.get_product(product_id)
        if product is None:
            raise WorkflowValidationError(
                "CHANGESET_PRODUCT_NOT_FOUND",
                "ready product does not exist",
            )
        pull_request = self.github.get_pr(request.pr_number or 0)
        expected_branch = f"ard/{request.changeset_id}-{product.product_key}"
        if (
            pull_request.head_branch != expected_branch
            or pull_request.head_sha != request.head_sha
        ):
            raise WorkflowSecurityError(
                "CHANGESET_READY_HEAD_MISMATCH",
                "ready PR does not match the product tracking branch",
            )
        tracking_product = _tracking_product_at_head(
            self.git,
            pull_request,
            product,
            request.changeset_id,
        )
        if tracking_product.version != request.version:
            raise WorkflowConflict(
                "CHANGESET_VERSION_NOT_TRACKING_HEAD",
                "ready version does not match the exact tracking PR head",
            )
        desired = ProductReadiness(
            version=request.version or 0,
            pr_number=request.pr_number or 0,
            head_sha=request.head_sha or "",
        )
        current = record.ready_products.get(product_id)
        if current is not None and current != desired:
            raise WorkflowConflict(
                "CHANGESET_READINESS_CONFLICT",
                "product readiness is already recorded for another head",
            )
        state_changed = current is None
        if state_changed:
            record.mark_ready(
                product_id,
                version=desired.version,
                pr_number=desired.pr_number,
                head_sha=desired.head_sha,
            )
            registry.write_changeset(record)

        mutations: list[MutationRecord] = []
        if state_changed:
            commit = self.git.commit_changeset_paths(
                request.changeset_id,
                None,
                f"data(changeset): update {request.changeset_id}",
            )
            mutations.append(_commit_mutation(commit.sha, commit.created))
            if commit.created:
                self.git.push(branch)
        coordination_pr, created = self._ensure_pr(
            branch,
            request.base_branch,
            f"data(changeset): {request.changeset_id}",
            f"Central readiness state for `{request.changeset_id}`.",
        )
        if created:
            mutations.append(_pr_mutation(coordination_pr))
        mutations.extend(self._central_statuses(coordination_pr.head_sha, request.target_url))
        mutations.extend(self._reconcile_tracking_statuses(record, registry, request))
        return _result(
            request,
            record,
            coordination_pr,
            mutations,
            WorkflowStatus.SUCCESS if state_changed else WorkflowStatus.NOOP,
        )

    def _ensure_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> tuple[PullRequestState, bool]:
        existing = self.github.find_open_pr(branch)
        if existing is not None:
            remote_head = self.git.remote_branch_sha(branch)
            if (
                existing.base_branch != base
                or not existing.draft
                or existing.head_sha != remote_head
                or self.git.current_sha() != remote_head
            ):
                raise WorkflowSecurityError(
                    "CHANGESET_PULL_REQUEST_MISMATCH",
                    "managed changeset PR has unexpected properties",
                )
            return existing, False
        created = self.github.create_draft_pr(branch, base, title, body)
        remote_head = self.git.remote_branch_sha(branch)
        if (
            created.base_branch != base
            or not created.draft
            or created.head_branch != branch
            or created.head_sha != remote_head
            or self.git.current_sha() != remote_head
        ):
            raise WorkflowSecurityError(
                "CHANGESET_PULL_REQUEST_MISMATCH",
                "created changeset PR has unexpected properties",
            )
        return created, True

    def _switch_existing_scoped_branch(
        self,
        branch: str,
        base_branch: str,
        expected_paths: set[Path],
        code: str,
    ) -> None:
        existing_head = self.git.remote_branch_sha(branch)
        self.git.switch_or_create(branch, base_branch)
        if existing_head is None:
            return
        if self.git.current_sha() != existing_head:
            raise WorkflowSecurityError(code, "existing branch checkout is not its remote head")
        changed = self.git.changed_paths(base_branch, existing_head)
        if set(changed.paths) == expected_paths:
            return
        if not changed.paths and self.git.is_ancestor(existing_head, base_branch):
            return
        raise WorkflowSecurityError(
            code,
            "existing managed branch contains unexpected committed paths",
        )

    def _central_statuses(self, sha: str, target_url: str) -> list[MutationRecord]:
        return [
            self.github.set_status(
                sha,
                context,
                "success",
                "Validated central changeset state",
                target_url,
            )
            for context in ("ard/quality-gate", "ard/changeset")
        ]

    def _reconcile_tracking_statuses(
        self,
        record: ChangeSetRecord,
        registry: Registry,
        request: ChangesetRequest,
    ) -> list[MutationRecord]:
        ready = _verified_ready_count(record, registry, self.git, self.github)
        complete = ready == len(record.required_product_ids)
        state = "success" if complete else "pending"
        description = (
            f"Changeset {record.changeset_id} is ready"
            if complete
            else (
                f"Changeset {record.changeset_id}: "
                f"{ready}/{len(record.required_product_ids)} products ready"
            )
        )
        mutations: list[MutationRecord] = []
        for product_id in record.required_product_ids:
            product = registry.get_product(product_id)
            if product is None:
                continue
            branch = f"ard/{record.changeset_id}-{product.product_key}"
            pull_request = self.github.find_open_pr(branch)
            if pull_request is not None:
                mutations.append(
                    self.github.set_status(
                        pull_request.head_sha,
                        "ard/changeset",
                        state,
                        description,
                        request.target_url,
                    )
                )
        return mutations


def _require_registry_entities(
    registry: Registry,
    table_ids: list[str],
    product_ids: list[str],
) -> None:
    missing_table = next((item for item in table_ids if registry.get_table(item) is None), None)
    if missing_table:
        raise WorkflowValidationError("CHANGESET_TABLE_NOT_FOUND", missing_table)
    missing_product = next(
        (item for item in product_ids if registry.get_product(item) is None),
        None,
    )
    if missing_product:
        raise WorkflowValidationError("CHANGESET_PRODUCT_NOT_FOUND", missing_product)


def _verified_ready_count(
    record: ChangeSetRecord,
    registry: Registry,
    git: GitPort,
    github: GitHubPort,
) -> int:
    count = 0
    for product_id, readiness in record.ready_products.items():
        product = registry.get_product(product_id)
        if product is None:
            continue
        pull_request = github.get_pr(readiness.pr_number)
        if pull_request.head_sha != readiness.head_sha:
            continue
        try:
            tracking_product = _tracking_product_at_head(
                git,
                pull_request,
                product,
                record.changeset_id,
            )
        except (WorkflowConflict, WorkflowSecurityError, WorkflowValidationError):
            continue
        if tracking_product.version == readiness.version:
            count += 1
    return count


def _tracking_product_at_head(
    git: GitPort,
    pull_request: PullRequestState,
    current_product: ProductRecord,
    changeset_id: str,
) -> ProductRecord:
    product_key = current_product.product_key
    try:
        product = ProductRecord.model_validate_json(
            git.read_text_at(
                pull_request.head_sha,
                Path("registry") / "products" / f"{current_product.product_id}.json",
            )
        )
        config = yaml.safe_load(
            git.read_text_at(
                pull_request.head_sha,
                Path("products") / product_key / "product.yaml",
            )
        )
        marker = json.loads(
            git.read_text_at(
                pull_request.head_sha,
                Path("products")
                / product_key
                / "changesets"
                / f"{changeset_id}.json",
            )
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise WorkflowSecurityError(
            "CHANGESET_TRACKING_HEAD_INVALID",
            "tracking PR head does not contain valid product evidence",
        ) from error
    if (
        product.product_id != current_product.product_id
        or product.product_key != product_key
        or not isinstance(config, dict)
        or config.get("product_id") != current_product.product_id
        or config.get("product_key") != product_key
        or config.get("version") != product.version
        or config.get("changeset_id") != changeset_id
        or not isinstance(marker, dict)
        or marker.get("changeset_id") != changeset_id
        or marker.get("product_id") != current_product.product_id
        or marker.get("status") != "required"
    ):
        raise WorkflowSecurityError(
            "CHANGESET_TRACKING_HEAD_MISMATCH",
            "tracking PR head evidence does not match the changeset product",
        )
    return product


def _require_tracking_marker(
    git: GitPort,
    revision: str,
    marker_path: Path,
    changeset_id: str,
    product_id: str,
) -> None:
    try:
        marker = json.loads(git.read_text_at(revision, marker_path))
    except (TypeError, ValueError) as error:
        raise WorkflowSecurityError(
            "CHANGESET_TRACKING_MARKER_INVALID",
            "existing tracking marker is malformed",
        ) from error
    if not isinstance(marker, dict) or marker != {
        "changeset_id": changeset_id,
        "product_id": product_id,
        "status": "required",
    }:
        raise WorkflowSecurityError(
            "CHANGESET_TRACKING_MARKER_MISMATCH",
            "existing tracking marker does not match the request",
        )


def _coordination_branch(changeset_id: str) -> str:
    return f"ard/changeset-{changeset_id}"


def _commit_mutation(sha: str, created: bool) -> MutationRecord:
    return MutationRecord(
        resource="commit",
        target=sha,
        action="create" if created else "noop",
    )


def _pr_mutation(pr: PullRequestState) -> MutationRecord:
    return MutationRecord(
        resource="pull_request",
        target=f"pr:{pr.number}",
        action="create",
        result_id=str(pr.number),
    )


def _result(
    request: ChangesetRequest,
    record: ChangeSetRecord,
    coordination_pr: PullRequestState,
    mutations: list[MutationRecord],
    status: WorkflowStatus,
) -> WorkflowResult:
    ready_count = len(record.ready_products)
    return WorkflowResult(
        command="workflow.changeset",
        status=status,
        outputs={
            "changeset_id": request.changeset_id,
            "coordination_pr": coordination_pr.number,
            "ready_count": ready_count,
            "required_count": len(record.required_product_ids),
            "state": record.status.value,
        },
        artifacts=[f"registry/changesets/{request.changeset_id}.json"],
        mutations=mutations,
    )


def _impact_comment(record: ChangeSetRecord, coordination_pr: int) -> str:
    tables = "\n".join(f"- `{item}`" for item in record.table_ids)
    products = "\n".join(f"- `{item}`" for item in record.required_product_ids)
    return (
        f"Changeset {record.changeset_id} created in PR #{coordination_pr}.\n\n"
        f"Tables:\n{tables}\n\nRequired products:\n{products}\n"
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
