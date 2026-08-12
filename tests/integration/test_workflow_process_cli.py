from __future__ import annotations

import json
import traceback
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowConfigurationError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowStatus,
)
from ard_ossie.cli import app


class StubProcessingService:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "current_head": "b" * 40,
                "expected_head": "a" * 40,
                "product_id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
                "product_key": "sales-order",
                "version": 1,
            },
        )


def test_workflow_process_cli_writes_exact_result_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubProcessingService()
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process",
            "--product-key",
            "sales-order",
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--expected-head",
            "a" * 40,
            "--invocation-id",
            "31543231017-1",
            "--allow-writeback",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.requests[0].allow_writeback is True
    assert service.requests[0].repository == tmp_path
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.process-result.json").read_text()
    )
    assert envelope["outputs"]["current_head"] == "b" * 40
    assert envelope["outputs"]["invocation_id"] == "31543231017-1"
    assert "version=1\n" in github_output.read_text(encoding="utf-8")


def test_workflow_process_cli_preserves_partial_head_and_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class PartialService:
        def run(self, request):
            raise WorkflowPartialError(
                "PROCESSING_POST_COMMIT_FAILED",
                "publication failed",
                retryable=True,
                outputs={
                    "current_head": "b" * 40,
                    "expected_head": "a" * 40,
                },
                mutations=[
                    MutationRecord(
                        resource="commit",
                        target="b" * 40,
                        action="create",
                    )
                ],
            )

    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: PartialService(),
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process",
            "--product-key",
            "sales-order",
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--expected-head",
            "a" * 40,
            "--invocation-id",
            "31543231017-1",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 70
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.process-result.json").read_text()
    )
    assert envelope["outputs"]["current_head"] == "b" * 40
    assert envelope["outputs"]["failure_exit_code"] == 70
    assert envelope["outputs"]["invocation_id"] == "31543231017-1"
    assert envelope["mutations"][0]["resource"] == "commit"


def test_workflow_process_cli_records_safe_non_partial_failure_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailedService:
        def run(self, request):
            raise WorkflowConfigurationError(
                "LLM_PROVIDER_AUTHENTICATION_FAILED",
                "provider configuration or request was rejected",
            )

    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: FailedService(),
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process",
            "--product-key",
            "sales-order",
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--expected-head",
            "a" * 40,
            "--invocation-id",
            "31543231017-1",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 20
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.process-result.json").read_text()
    )
    assert envelope["outputs"] == {
        "failure_exit_code": 20,
        "invocation_id": "31543231017-1",
    }
    assert envelope["findings"] == [
        {
            "code": "LLM_PROVIDER_AUTHENTICATION_FAILED",
            "message": "LLM_PROVIDER_AUTHENTICATION_FAILED",
        }
    ]


def test_workflow_process_cli_records_wrapped_validation_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class InvalidService:
        def run(self, request):
            raise ValueError("SAFE_VALIDATION_CODE: sentinel-sensitive-detail")

    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: InvalidService(),
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process",
            "--product-key",
            "sales-order",
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--expected-head",
            "a" * 40,
            "--invocation-id",
            "31543231017-1",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 10
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.process-result.json").read_text()
    )
    assert envelope["outputs"] == {
        "failure_exit_code": 10,
        "invocation_id": "31543231017-1",
    }
    assert envelope["findings"] == [
        {"code": "SAFE_VALIDATION_CODE", "message": "SAFE_VALIDATION_CODE"}
    ]
    assert "sentinel-sensitive-detail" not in result.output
    assert "sentinel-sensitive-detail" not in json.dumps(envelope)
    assert "sentinel-sensitive-detail" not in "".join(
        traceback.format_exception(result.exception)
    )


def test_workflow_process_reconcile_cli_maps_same_job_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class ReconcileService:
        def __init__(self) -> None:
            self.request = None

        def run(self, request):
            self.request = request
            return WorkflowResult(
                command="workflow.process-reconcile",
                status=WorkflowStatus.SUCCESS,
                outputs={"current_head": "b" * 40},
            )

    service = ReconcileService()
    monkeypatch.setattr(
        workflow_cli,
        "_processing_reconcile_service",
        lambda repository_name, paths: service,
    )
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process-reconcile",
            "--result-path",
            str(result_path),
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--invocation-id",
            "31543231017-1",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.result_path == result_path
    assert service.request.branch == "ard/example"
    assert service.request.invocation_id == "31543231017-1"


def test_workflow_process_cli_invalidates_stale_envelope_before_unhandled_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class CrashingService:
        def run(self, request):
            raise RuntimeError("simulated abrupt processor failure")

    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text('{"forged":"stale"}\n', encoding="utf-8")
    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: CrashingService(),
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "process",
            "--product-key",
            "sales-order",
            "--branch",
            "ard/example",
            "--pr-number",
            "7",
            "--expected-head",
            "a" * 40,
            "--invocation-id",
            "31543231017-1",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert not result_path.exists()


def test_workflow_process_cli_rejects_invalid_invocation_before_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnexpectedService:
        def run(self, request):
            raise AssertionError("processor must not run")

    monkeypatch.setattr(
        workflow_cli,
        "_processing_service",
        lambda repository_name, paths: UnexpectedService(),
    )

    for invalid_invocation_id in ("invalid invocation", "x" * 129):
        result = CliRunner().invoke(
            app,
            [
                "workflow",
                "process",
                "--product-key",
                "sales-order",
                "--branch",
                "ard/example",
                "--pr-number",
                "7",
                "--expected-head",
                "a" * 40,
                "--invocation-id",
                invalid_invocation_id,
                "--repository-name",
                "owner/repository",
                "--repository",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 2
        assert "INVALID_PROCESSING_INVOCATION_ID" in result.output
    assert not (tmp_path / ".ard" / "run" / "workflow.process-result.json").exists()
