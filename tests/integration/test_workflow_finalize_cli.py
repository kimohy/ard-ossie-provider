from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubFinalizer:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return WorkflowResult(
            command="workflow.finalize",
            status=WorkflowStatus.SUCCESS,
            outputs={"finalized_success": False, "upstream_result": "failure"},
        )


def test_workflow_finalize_cli_writes_independent_result_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    finalizer = StubFinalizer()
    monkeypatch.setattr(
        workflow_cli,
        "_finalize_service",
        lambda repository_name, paths: finalizer,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "finalize",
            "--upstream-result",
            "failure",
            "--issue-number",
            "42",
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

    assert result.exit_code == 0, result.output
    assert finalizer.requests[0].result_path is None
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.finalize-result.json").read_text()
    )
    assert envelope["outputs"]["finalized_success"] is False
