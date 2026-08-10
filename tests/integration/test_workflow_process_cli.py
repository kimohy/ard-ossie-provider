from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import (
    MutationRecord,
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
    assert envelope["mutations"][0]["resource"] == "commit"


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
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.result_path == result_path
    assert service.request.branch == "ard/example"
