from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubRepositoryCheckService:
    def __init__(self) -> None:
        self.request = None

    def run(self, request):
        self.request = request
        return WorkflowResult(
            command="workflow.repository-check",
            status=WorkflowStatus.SUCCESS,
            outputs={"code_only": True, "head_sha": "b" * 40},
        )


def test_workflow_repository_check_maps_exact_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubRepositoryCheckService()
    monkeypatch.setattr(
        workflow_cli,
        "_repository_check_service",
        lambda paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "repository-check",
            "--base-ref",
            "a" * 40,
            "--head-ref",
            "b" * 40,
            "--head-sha",
            "b" * 40,
            "--verification-group",
            "model-schemas",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.base_ref == "a" * 40
    assert service.request.head_sha == "b" * 40
    assert service.request.verification_group == "model-schemas"
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.repository-check-result.json").read_text()
    )
    assert envelope["outputs"]["code_only"] is True
