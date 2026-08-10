from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowConflict,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.impact import ChangeSetStatus
from ard_ossie.models import ProductKey, StrictModel, TableId
from ard_ossie.ports.filesystem import FileSystemPort, PathPolicyError
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, ReleaseState
from ard_ossie.registry import Registry
from ard_ossie.release import (
    ReleaseBlocked,
    ReleasePlan,
    build_release_bundle,
    release_source_paths,
    resolve_release_plan,
)


class ReleasePublicationRequest(StrictModel):
    repository: Path
    product_key: ProductKey
    current: str = Field(pattern=r"^[0-9a-f]{40}$")
    table_ids: list[TableId] = Field(default_factory=list)
    output: Path


class ReleasePublicationService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
        *,
        plan_resolver: Callable[..., ReleasePlan] = resolve_release_plan,
        bundle_builder: Callable[[Path, Path], Path] = build_release_bundle,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github
        self.plan_resolver = plan_resolver
        self.bundle_builder = bundle_builder

    def run(self, request: ReleasePublicationRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "RELEASE_REPOSITORY_MISMATCH",
                "release repository does not match filesystem port",
            )
        self._require_current_head(request.current)
        product_root = self._verify_release_source_paths(request.product_key)
        plan = self._resolve_plan(request)
        self._verify_changeset_readiness(plan, request.current)

        output = self.paths.resolve_write(request.output)
        bundle_path = self.paths.resolve_write(
            output / f"{plan.product_id}-v{plan.product_version}.zip"
        )
        self._verify_release_source_paths(plan.product_key)
        try:
            bundle = self.bundle_builder(product_root, bundle_path)
        except (OSError, TypeError, ValueError, ReleaseBlocked) as error:
            raise _release_conflict(error, "RELEASE_BUNDLE_BUILD_FAILED") from error
        bundle = self.paths.resolve_read(bundle)
        _verify_bundle_sources(bundle, plan.artifact_hashes)
        artifact_sha256 = _sha256_file(bundle)
        self._require_current_head(request.current)

        tags = [plan.product_tag, *plan.table_tags]
        existing_targets = {tag: self.git.tag_target(tag) for tag in tags}
        for tag, target in existing_targets.items():
            if target is not None and target != request.current:
                raise WorkflowConflict(
                    "TAG_TARGET_CONFLICT",
                    f"immutable tag {tag} targets {target}, not {request.current}",
                )
        existing_release = self.github.get_release(plan.product_tag)
        _verify_existing_release(existing_release, plan, bundle.name, artifact_sha256)

        artifact = bundle.relative_to(self.paths.root).as_posix()
        outputs: dict[str, object] = {
            "product_id": plan.product_id,
            "product_key": plan.product_key,
            "version": plan.product_version,
            "product_tag": plan.product_tag,
            "table_tags": plan.table_tags,
            "commit": request.current,
            "artifact_sha256": artifact_sha256,
            "artifact_hashes": plan.artifact_hashes,
        }
        if plan.changeset_id:
            outputs["changeset_id"] = plan.changeset_id

        mutations: list[MutationRecord] = []
        remote_attempted = False
        try:
            new_tags: list[str] = []
            for tag in tags:
                if existing_targets[tag] is not None:
                    continue
                self.git.create_annotated_tag(tag, request.current, tag)
                new_tags.append(tag)
                mutations.append(
                    MutationRecord(resource="tag", target=tag, action="create")
                )
            if new_tags:
                remote_attempted = True
                self.git.push_tags(new_tags)
            remote_attempted = True
            mutations.append(
                self.github.upsert_release(
                    plan.product_tag,
                    f"{plan.product_key} v{plan.product_version}",
                    bundle,
                    artifact_sha256,
                )
            )
        except WorkflowPartialError:
            raise
        except WorkflowError as error:
            if not remote_attempted:
                raise
            raise WorkflowPartialError(
                "RELEASE_PUBLICATION_PARTIAL",
                "release publication began but did not converge",
                retryable=error.retryable,
                outputs=outputs,
                artifacts=[artifact],
                mutations=mutations,
            ) from error

        return WorkflowResult(
            command="workflow.release-product",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
            artifacts=[artifact],
            mutations=mutations,
        )

    def _require_current_head(self, expected: str) -> None:
        if self.git.current_sha() != expected:
            raise WorkflowSecurityError(
                "RELEASE_HEAD_MISMATCH",
                "release publication must remain at the exact merged commit",
            )

    def _resolve_plan(self, request: ReleasePublicationRequest) -> ReleasePlan:
        try:
            return self.plan_resolver(
                request.product_key,
                registry_root=self.paths.resolve_write("registry"),
                repository_root=self.paths.root,
                table_ids=set(request.table_ids),
            )
        except (OSError, TypeError, ValueError, ReleaseBlocked) as error:
            raise _release_conflict(error, "RELEASE_PLAN_INVALID") from error

    def _verify_release_source_paths(self, product_key: str) -> Path:
        product_root = self.paths.resolve_read(Path("products") / product_key)
        for source in release_source_paths(product_root):
            try:
                self.paths.resolve_read(source)
            except PathPolicyError as error:
                if error.code != "READ_PATH_NOT_FOUND":
                    raise
        return product_root

    def _verify_changeset_readiness(self, plan: ReleasePlan, current: str) -> None:
        if plan.changeset_id is None:
            return
        try:
            registry = Registry.load(self.paths.resolve_write("registry"))
        except (OSError, TypeError, ValueError) as error:
            raise WorkflowConflict(
                "RELEASE_REGISTRY_INVALID",
                "release registry is malformed",
            ) from error
        changeset = registry.get_changeset(plan.changeset_id)
        if changeset is None or changeset.status is not ChangeSetStatus.READY:
            raise WorkflowConflict(
                "CHANGESET_INCOMPLETE",
                f"release changeset is not complete: {plan.changeset_id}",
            )
        for product_id in changeset.required_product_ids:
            readiness = changeset.ready_products[product_id]
            pull_request = self.github.get_pr(readiness.pr_number)
            if pull_request.head_sha != readiness.head_sha:
                raise WorkflowConflict(
                    "CHANGESET_HEAD_SHA_MISMATCH",
                    f"recorded readiness head changed for PR {readiness.pr_number}",
                )
            if pull_request.merged_at is None or pull_request.merge_sha is None:
                raise WorkflowConflict(
                    "CHANGESET_PR_NOT_MERGED",
                    f"readiness PR is not merged: {readiness.pr_number}",
                )
            if not self.git.is_ancestor(pull_request.merge_sha, current):
                raise WorkflowConflict(
                    "CHANGESET_MERGE_NOT_REACHABLE",
                    f"readiness PR is not reachable: {readiness.pr_number}",
                )


def _verify_existing_release(
    release: ReleaseState | None,
    plan: ReleasePlan,
    asset_name: str,
    artifact_sha256: str,
) -> None:
    if release is None:
        return
    if release.tag != plan.product_tag:
        raise WorkflowConflict(
            "RELEASE_TAG_CONFLICT",
            "existing release is attached to another tag",
        )
    matching = [asset for asset in release.assets if asset.name == asset_name]
    if len(matching) > 1:
        raise WorkflowConflict(
            "MULTIPLE_RELEASE_ASSETS",
            f"release has duplicate assets: {asset_name}",
        )
    if matching and matching[0].digest != artifact_sha256:
        raise WorkflowConflict(
            "RELEASE_ASSET_CONFLICT",
            f"release asset digest differs: {asset_name}",
        )


def _verify_bundle_sources(bundle: Path, expected: dict[str, str]) -> None:
    try:
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise WorkflowConflict(
                    "RELEASE_BUNDLE_CONTENT_MISMATCH",
                    "release bundle entries do not match the verified plan",
                )
            for name, digest in expected.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise WorkflowConflict(
                        "RELEASE_BUNDLE_HASH_MISMATCH",
                        f"release bundle source changed after planning: {name}",
                    )
    except WorkflowConflict:
        raise
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
        raise WorkflowConflict(
            "RELEASE_BUNDLE_INVALID",
            "release bundle is malformed",
        ) from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_conflict(error: Exception, fallback: str) -> WorkflowConflict:
    code = str(error).partition(":")[0] or fallback
    return WorkflowConflict(code, "release verification failed")
