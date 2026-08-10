from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubService:
    def __init__(self, result: WorkflowResult) -> None:
        self.result = result
        self.contexts = []

    def run(self, context, **kwargs):
        self.contexts.append(context)
        return self.result


def event_file(tmp_path: Path) -> Path:
    event = tmp_path / "event.json"
    event.write_text("{}\n", encoding="utf-8")
    return event


def test_issue_authorize_cli_writes_allowed_output_and_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.issue-authorize",
            status=WorkflowStatus.SUCCESS,
            outputs={"allowed": True},
        )
    )
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_issue_authorization_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "issue-authorize",
            "--event",
            str(event_file(tmp_path)),
            "--repository",
            str(tmp_path),
            "--repository-name",
            "owner/repository",
            "--actor",
            "kimohy",
            "--label",
            "ard:approved",
        ],
    )

    assert result.exit_code == 0, result.output
    assert github_output.read_text(encoding="utf-8") == "allowed=true\n"
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.issue-authorize-result.json").read_text()
    )
    assert envelope["status"] == "success"


def test_issue_intake_cli_exposes_branch_pr_and_exact_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.issue-intake",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "branch": "ard/issue-42-sales-order",
                "expected_head": "a" * 40,
                "pr_number": 7,
                "product_key": "sales-order",
            },
        )
    )
    monkeypatch.setattr(
        workflow_cli,
        "_issue_intake_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "issue-intake",
            "--event",
            str(event_file(tmp_path)),
            "--repository",
            str(tmp_path),
            "--repository-name",
            "owner/repository",
            "--actor",
            "kimohy",
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.issue-intake-result.json").read_text()
    )
    assert envelope["outputs"]["pr_number"] == 7
    assert envelope["outputs"]["expected_head"] == "a" * 40
