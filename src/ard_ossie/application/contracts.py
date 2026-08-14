from __future__ import annotations

import os
import re
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from ard_ossie.models import StrictModel

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|credential|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)


class ExitCode(IntEnum):
    SUCCESS = 0
    VALIDATION = 10
    CONFIGURATION = 20
    TRANSIENT = 30
    CONFLICT = 40
    SECURITY = 50
    PARTIAL = 70


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NOOP = "noop"


class WorkflowError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: ExitCode,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable


class WorkflowValidationError(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.VALIDATION)


class WorkflowConfigurationError(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.CONFIGURATION)


class WorkflowTransientError(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.TRANSIENT, retryable=True)


class WorkflowConflict(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.CONFLICT)


class WorkflowSecurityError(WorkflowError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, ExitCode.SECURITY)


class WorkflowPartialError(WorkflowError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        outputs: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
        mutations: list[Any] | None = None,
    ) -> None:
        super().__init__(code, message, ExitCode.PARTIAL, retryable=retryable)
        self.outputs = outputs or {}
        self.artifacts = artifacts or []
        self.mutations = mutations or []


class WorkflowContext(StrictModel):
    repository: Path
    event_path: Path | None = None
    event_name: str | None = None
    run_id: str | None = None
    repository_name: str | None = None
    server_url: str | None = None
    actor: str | None = None
    runner_temp: Path | None = Field(default=None, exclude=True)

    @field_validator("repository", mode="before")
    @classmethod
    def resolve_repository(cls, value: object) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("REPOSITORY_NOT_FOUND")
        return path

    @model_validator(mode="after")
    def resolve_trusted_paths(self) -> WorkflowContext:
        runner_temp = self.runner_temp
        if runner_temp is None and os.environ.get("RUNNER_TEMP"):
            runner_temp = Path(os.environ["RUNNER_TEMP"])
        if runner_temp is not None:
            runner_temp = runner_temp.expanduser().resolve()
            object.__setattr__(self, "runner_temp", runner_temp)

        if self.event_path is None:
            return self
        event_path = self.event_path.expanduser().resolve()
        if not event_path.is_file():
            raise ValueError("EVENT_PATH_NOT_FOUND")
        trusted_roots = [self.repository]
        if runner_temp is not None:
            trusted_roots.append(runner_temp)
        if not any(event_path.is_relative_to(root) for root in trusted_roots):
            raise ValueError("EVENT_PATH_OUTSIDE_TRUSTED_ROOTS")
        object.__setattr__(self, "event_path", event_path)
        return self


class MutationRecord(StrictModel):
    resource: str
    target: str
    action: str
    result_id: str | None = None


class WorkflowResult(StrictModel):
    schema_version: Literal[1] = 1
    command: str
    status: WorkflowStatus
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    findings: list[dict[str, JsonValue]] = Field(default_factory=list)
    mutations: list[MutationRecord] = Field(default_factory=list)
    retryable: bool = False

    @model_validator(mode="before")
    @classmethod
    def redact_sensitive_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        sanitized = dict(value)
        for field in ("outputs", "findings"):
            if field in sanitized:
                sanitized[field] = _redact_mapping_values(sanitized[field])
        return sanitized


def _redact_mapping_values(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_mapping_values(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_mapping_values(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_mapping_values(item) for item in value]
    return value
