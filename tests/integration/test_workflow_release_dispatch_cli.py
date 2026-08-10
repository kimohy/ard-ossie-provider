from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubReleaseDispatchService:
    def __init__(self) -> None:
        self.request = None

    def run(self, request):
        self.request = request
        return WorkflowResult(
            command="workflow.release-dispatch",
            status=WorkflowStatus.SUCCESS,
            outputs={"dispatched": True},
        )


def test_workflow_release_dispatch_maps_result_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubReleaseDispatchService()
    result_path = tmp_path / ".ard" / "run" / "workflow.release-product-result.json"
    monkeypatch.setattr(
        workflow_cli,
        "_release_dispatch_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "release-dispatch",
            "--result-path",
            str(result_path),
            "--current",
            "a" * 40,
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.result_path == result_path
    assert service.request.current == "a" * 40
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.release-dispatch-result.json").read_text()
    )
    assert envelope["outputs"]["dispatched"] is True
