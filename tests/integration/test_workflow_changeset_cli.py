from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app

CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
PRODUCT_A = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
PRODUCT_B = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


class StubService:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return WorkflowResult(
            command="workflow.changeset",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "changeset_id": request.changeset_id,
                "coordination_pr": 3,
                "ready_count": 0,
                "required_count": 2,
                "state": "blocked",
            },
        )


def test_workflow_changeset_cli_parses_create_lists_and_writes_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService()
    monkeypatch.setattr(
        workflow_cli,
        "_changeset_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "changeset",
            "--mode",
            "create",
            "--changeset-id",
            CHANGESET_ID,
            "--table-ids",
            TABLE_ID,
            "--product-ids",
            f"{PRODUCT_A},{PRODUCT_B}",
            "--initiating-pr",
            "99",
            "--base-branch",
            "main",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.requests[0].product_ids == [PRODUCT_A, PRODUCT_B]
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.changeset-result.json").read_text()
    )
    assert envelope["outputs"]["required_count"] == 2
