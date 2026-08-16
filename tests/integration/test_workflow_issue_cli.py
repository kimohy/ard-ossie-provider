from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import (
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.cli import app


class StubService:
    def __init__(self, result: WorkflowResult) -> None:
        self.result = result
        self.contexts = []
        self.kwargs = []

    def run(self, context, **kwargs):
        self.contexts.append(context)
        self.kwargs.append(kwargs)
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


def test_issue_route_cli_writes_mode_base_and_exact_pr_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.issue-route",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "mode": "base_sync",
                "base_sha": "a" * 40,
                "branch": "ard/issue-3-500138301",
                "product_key": "500138301",
                "pr_number": 5,
                "expected_head": "b" * 40,
            },
        )
    )
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_issue_route_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "issue-route",
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
    assert github_output.read_text(encoding="utf-8") == (
        f"base_sha={'a' * 40}\n"
        "branch=ard/issue-3-500138301\n"
        f"expected_head={'b' * 40}\n"
        "mode=base_sync\n"
        "pr_number=5\n"
        "product_key=500138301\n"
    )
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.issue-route-result.json").read_text()
    )
    assert envelope["outputs"]["expected_head"] == "b" * 40


def test_issue_base_sync_cli_writes_the_synchronized_exact_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.issue-base-sync",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "branch": "ard/issue-3-500138301",
                "product_key": "500138301",
                "pr_number": 5,
                "expected_head": "c" * 40,
                "product_id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
            },
        )
    )
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_issue_base_sync_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "issue-base-sync",
            "--event",
            str(event_file(tmp_path)),
            "--base-sha",
            "a" * 40,
            "--repository",
            str(tmp_path),
            "--repository-name",
            "owner/repository",
            "--actor",
            "kimohy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.kwargs == [{"base_sha": "a" * 40}]
    assert github_output.read_text(encoding="utf-8") == (
        "branch=ard/issue-3-500138301\n"
        f"expected_head={'c' * 40}\n"
        "pr_number=5\n"
        "product_id=prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631\n"
        "product_key=500138301\n"
    )
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.issue-base-sync-result.json").read_text()
    )
    assert envelope["outputs"]["expected_head"] == "c" * 40


def test_issue_base_sync_cli_redacts_attachment_failure_with_security_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    signed_secret = "must-not-appear-in-cli-output"

    class FailedService:
        def run(self, context, **kwargs):
            raise WorkflowSecurityError(
                "ATTACHMENT_DOWNLOAD_HTTP_403",
                f"unsafe signed attachment URL: {signed_secret}",
            )

    monkeypatch.setattr(
        workflow_cli,
        "_issue_base_sync_service",
        lambda repository_name, paths: FailedService(),
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "issue-base-sync",
            "--event",
            str(event_file(tmp_path)),
            "--base-sha",
            "a" * 40,
            "--repository",
            str(tmp_path),
            "--repository-name",
            "owner/repository",
            "--actor",
            "kimohy",
        ],
    )

    assert result.exit_code == 50, result.output
    assert signed_secret not in result.output
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.issue-base-sync-result.json").read_text()
    )
    assert envelope["outputs"]["failure_exit_code"] == 50
    assert envelope["findings"] == [
        {
            "code": "ATTACHMENT_DOWNLOAD_HTTP_403",
            "message": "ATTACHMENT_DOWNLOAD_HTTP_403",
        }
    ]
    assert signed_secret not in json.dumps(envelope)
