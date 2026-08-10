from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ard_ossie.application.contracts import (
    ExitCode,
    MutationRecord,
    WorkflowContext,
    WorkflowResult,
    WorkflowStatus,
)


def test_workflow_result_preserves_json_outputs_and_redacts_sensitive_fields() -> None:
    """A scalar-only result or an exposed sensitive field would break workflow handoffs."""
    result = WorkflowResult(
        command="workflow.release-detect",
        status=WorkflowStatus.SUCCESS,
        outputs={
            "products": ["finance-order", "sales-order"],
            "artifact_hashes": {"bundle.zip": "a" * 64},
            "api_key": "sentinel-key",
        },
        mutations=[
            MutationRecord(
                resource="status",
                target="a" * 40 + ":ard/quality-gate",
                action="set",
            )
        ],
    )

    payload = result.model_dump(mode="json")
    assert result.schema_version == 1
    assert result.retryable is False
    assert payload["outputs"] == {
        "products": ["finance-order", "sales-order"],
        "artifact_hashes": {"bundle.zip": "a" * 64},
        "api_key": "***",
    }
    assert "sentinel-key" not in result.model_dump_json()


def test_workflow_context_resolves_repository_and_repository_event(tmp_path: Path) -> None:
    event = tmp_path / ".ard" / "event.json"
    event.parent.mkdir()
    event.write_text("{}", encoding="utf-8")

    context = WorkflowContext(
        repository=tmp_path,
        event_path=event,
        event_name="pull_request",
        run_id="7",
    )

    assert context.repository == tmp_path.resolve()
    assert context.event_path == event.resolve()
    assert ExitCode.SECURITY == 50


def test_workflow_context_allows_event_only_below_runner_temp(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    runner_temp = tmp_path / "runner-temp"
    repository.mkdir()
    runner_temp.mkdir()
    event = runner_temp / "event.json"
    event.write_text("{}", encoding="utf-8")

    context = WorkflowContext(
        repository=repository,
        runner_temp=runner_temp,
        event_path=event,
    )

    assert context.event_path == event.resolve()


def test_workflow_context_rejects_event_outside_trusted_roots(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    runner_temp = tmp_path / "runner-temp"
    repository.mkdir()
    runner_temp.mkdir()
    event = tmp_path / "untrusted-event.json"
    event.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="EVENT_PATH_OUTSIDE_TRUSTED_ROOTS"):
        WorkflowContext(
            repository=repository,
            runner_temp=runner_temp,
            event_path=event,
        )
