from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.models import StrictModel
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.github import GitHubPort

_SHA = re.compile(r"^[0-9a-f]{40}$")


class FinalizeRequest(StrictModel):
    repository: Path
    upstream_result: Literal["success", "failure", "cancelled", "skipped"]
    result_path: Path | None = None
    issue_number: int | None = Field(default=None, gt=0)
    pr_number: int | None = Field(default=None, gt=0)
    expected_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    target_url: str = ""


class FinalizeService:
    def __init__(self, paths: FileSystemPort, github: GitHubPort) -> None:
        self.paths = paths
        self.github = github

    def run(self, request: FinalizeRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "FINALIZE_REPOSITORY_MISMATCH",
                "finalizer repository does not match filesystem port",
            )
        prior = self._load_prior(request.result_path) if request.result_path else None
        succeeded = request.upstream_result == "success" and (
            prior is None or prior.status in {WorkflowStatus.SUCCESS, WorkflowStatus.NOOP}
        )
        expected_head = _known_head(request, prior)
        mutations: list[MutationRecord] = []

        if request.issue_number is not None:
            mutations.extend(
                self.github.set_issue_labels(
                    request.issue_number,
                    add=set() if succeeded else {"ard:failed"},
                    remove={"ard:processing", "ard:failed"} if succeeded else {"ard:processing"},
                )
            )
        if request.pr_number is not None:
            mutations.append(
                self.github.upsert_pr_comment(
                    request.pr_number,
                    "ard:finalize",
                    _summary(request, prior, succeeded),
                )
            )
        if not succeeded and expected_head is not None:
            for context in ("ard/changeset", "ard/quality-gate"):
                existing = self.github.get_status(expected_head, context)
                if existing in {"success", "failure"}:
                    continue
                mutations.append(
                    self.github.set_status(
                        expected_head,
                        context,
                        "failure",
                        "ARD workflow did not complete successfully",
                        request.target_url,
                    )
                )

        changed = any(item.action != "noop" for item in mutations)
        outputs: dict[str, object] = {
            "finalized_success": succeeded,
            "upstream_result": request.upstream_result,
        }
        if expected_head is not None:
            outputs["expected_head"] = expected_head
        return WorkflowResult(
            command="workflow.finalize",
            status=WorkflowStatus.SUCCESS if changed else WorkflowStatus.NOOP,
            outputs=outputs,
            mutations=mutations,
        )

    def _load_prior(self, value: Path) -> WorkflowResult:
        supplied = value if value.is_absolute() else self.paths.root / value
        runtime_root = self.paths.root / ".ard" / "run"
        lexical = supplied.absolute()
        if lexical.parent != runtime_root or lexical.suffix != ".json":
            raise WorkflowSecurityError(
                "FINALIZE_RESULT_PATH_NOT_TRUSTED",
                "result envelope must be a direct .ard/run JSON file",
            )
        resolved = self.paths.resolve_read(value)
        try:
            return WorkflowResult.model_validate_json(resolved.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as error:
            raise WorkflowValidationError(
                "INVALID_FINALIZE_RESULT",
                "prior result envelope is malformed or unsupported",
            ) from error


def _known_head(
    request: FinalizeRequest,
    prior: WorkflowResult | None,
) -> str | None:
    prior_head: str | None = None
    if prior is not None:
        for key in ("current_head", "expected_head"):
            value = prior.outputs.get(key)
            if isinstance(value, str):
                prior_head = value
                break
    known = [value for value in (request.expected_head, prior_head) if value is not None]
    if any(not _SHA.fullmatch(value) for value in known):
        raise WorkflowValidationError("INVALID_FINALIZE_HEAD", "result contains an invalid SHA")
    if len(set(known)) > 1:
        raise WorkflowSecurityError(
            "FINALIZE_HEAD_MISMATCH",
            "request and prior result disagree on the exact head",
        )
    return known[0] if known else None


def _summary(
    request: FinalizeRequest,
    prior: WorkflowResult | None,
    succeeded: bool,
) -> str:
    command = prior.command if prior is not None else "unavailable"
    status = "success" if succeeded else "failure"
    return (
        f"ARD workflow finalization: **{status}**.\n\n"
        f"Upstream job: `{request.upstream_result}`. Result command: `{command}`."
    )
