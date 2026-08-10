from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, ValidationError

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.models import StrictModel
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort

_PRODUCT_ID = re.compile(
    r"^prd_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseDispatchRequest(StrictModel):
    repository: Path
    result_path: Path
    current: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_url: str = ""


class ReleaseDispatchService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github

    def run(self, request: ReleaseDispatchRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "RELEASE_DISPATCH_REPOSITORY_MISMATCH",
                "release dispatch repository does not match filesystem port",
            )
        result = self._load_release_result(request.result_path)
        product_id = _required_string(result, "product_id")
        tag = _required_string(result, "product_tag")
        commit = _required_string(result, "commit")
        version = result.outputs.get("version")
        hashes = result.outputs.get("artifact_hashes")
        if _PRODUCT_ID.fullmatch(product_id) is None:
            raise WorkflowSecurityError(
                "RELEASE_PRODUCT_ID_INVALID",
                "release result has an invalid product ID",
            )
        if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 999:
            raise WorkflowSecurityError(
                "RELEASE_VERSION_INVALID",
                "release result has an invalid numeric version",
            )
        if tag != f"product/{product_id}/v{version}":
            raise WorkflowSecurityError(
                "RELEASE_TAG_MISMATCH",
                "release tag does not match its product and version",
            )
        if (
            commit != request.current
            or self.git.current_sha() != request.current
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        ):
            raise WorkflowSecurityError(
                "RELEASE_COMMIT_MISMATCH",
                "release result and checkout must match the approved commit",
            )
        artifact_hashes = _validated_artifact_hashes(hashes)
        context = f"ard/dispatched:{product_id}:v{version}"
        deduplication_key: list[object] = [product_id, version, tag, commit]
        outputs: dict[str, object] = {
            "product_id": product_id,
            "version": version,
            "tag": tag,
            "commit": commit,
            "artifact_hashes": artifact_hashes,
            "deduplication_key": deduplication_key,
            "dispatched": False,
        }
        if self.github.get_status(commit, context) == "success":
            return WorkflowResult(
                command="workflow.release-dispatch",
                status=WorkflowStatus.NOOP,
                outputs=outputs,
            )

        payload: dict[str, object] = {
            "product_id": product_id,
            "version": version,
            "tag": tag,
            "commit": commit,
            "artifact_hashes": artifact_hashes,
        }
        mutations: list[MutationRecord] = []
        try:
            mutations.append(
                self.github.repository_dispatch("ard_product_released", payload)
            )
            outputs["dispatched"] = True
            mutations.append(
                self.github.set_status(
                    commit,
                    context,
                    "success",
                    "Approved downstream linkage dispatched",
                    request.target_url,
                )
            )
        except WorkflowPartialError:
            raise
        except WorkflowError as error:
            if not mutations:
                raise
            raise WorkflowPartialError(
                "RELEASE_DISPATCH_PARTIAL",
                "downstream dispatch succeeded but status publication failed",
                retryable=error.retryable,
                outputs=outputs,
                mutations=mutations,
            ) from error
        return WorkflowResult(
            command="workflow.release-dispatch",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
            mutations=mutations,
        )

    def _load_release_result(self, value: Path) -> WorkflowResult:
        supplied = value if value.is_absolute() else self.paths.root / value
        expected = self.paths.root / ".ard" / "run" / "workflow.release-product-result.json"
        if supplied.absolute() != expected:
            raise WorkflowSecurityError(
                "RELEASE_RESULT_PATH_NOT_TRUSTED",
                "dispatch requires the release-product result envelope",
            )
        try:
            result = WorkflowResult.model_validate_json(
                self.paths.resolve_read(value).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ValidationError) as error:
            raise WorkflowValidationError(
                "RELEASE_RESULT_INVALID",
                "release result envelope is malformed",
            ) from error
        if (
            result.schema_version != 1
            or result.command != "workflow.release-product"
            or result.status is not WorkflowStatus.SUCCESS
        ):
            raise WorkflowSecurityError(
                "RELEASE_RESULT_NOT_APPROVED",
                "dispatch requires a successful release-product result",
            )
        return result


def _required_string(result: WorkflowResult, key: str) -> str:
    value = result.outputs.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowSecurityError(
            "RELEASE_RESULT_INVALID",
            f"release result is missing {key}",
        )
    return value


def _validated_artifact_hashes(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise WorkflowSecurityError(
            "ARTIFACT_HASHES_INVALID",
            "release artifact hashes are missing",
        )
    validated: dict[str, str] = {}
    for name, digest in value.items():
        path = Path(name) if isinstance(name, str) else Path("..")
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise WorkflowSecurityError(
                "ARTIFACT_HASHES_INVALID",
                "release artifact hashes contain an unsafe entry",
            )
        validated[name] = digest
    return dict(sorted(validated.items()))
