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

    def run(self, *args, **kwargs) -> WorkflowResult:
        return self.result


def test_detect_product_cli_writes_product_and_exact_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.detect-product",
            status=WorkflowStatus.SUCCESS,
            outputs={"product_key": "sales-order", "expected_head": "a" * 40},
        )
    )
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_detect_product_service",
        lambda paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "detect-product",
            "--base-ref",
            "origin/main",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    output = github_output.read_text(encoding="utf-8")
    assert "product_key=sales-order\n" in output
    assert f"expected_head={'a' * 40}\n" in output


def test_source_check_cli_rejects_symlink_product_with_security_exit(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    repository = tmp_path / "repository"
    outside.mkdir()
    (repository / "products").mkdir(parents=True)
    (repository / "registry").mkdir()
    (repository / "products" / "sales-order").symlink_to(outside, target_is_directory=True)

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "source-check",
            "--product-key",
            "sales-order",
            "--expected-head",
            "a" * 40,
            "--repository",
            str(repository),
        ],
    )

    assert result.exit_code == 50, result.output
    envelope = json.loads(
        (repository / ".ard" / "run" / "workflow.source-check-result.json").read_text()
    )
    assert envelope["findings"][0]["code"] == "SYMLINK_NOT_ALLOWED"


def test_source_check_cli_rejects_product_key_traversal_with_security_exit(
    tmp_path: Path,
) -> None:
    (tmp_path / "registry").mkdir()

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "source-check",
            "--product-key",
            "../outside",
            "--expected-head",
            "a" * 40,
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 50, result.output


def test_ensure_product_pr_cli_exposes_pr_number(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubService(
        WorkflowResult(
            command="workflow.ensure-product-pr",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "branch": "feature/sales",
                "expected_head": "a" * 40,
                "pr_number": 9,
                "product_key": "sales-order",
            },
        )
    )
    monkeypatch.setattr(
        workflow_cli,
        "_ensure_product_pr_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "ensure-product-pr",
            "--branch",
            "feature/sales",
            "--product-key",
            "sales-order",
            "--expected-head",
            "a" * 40,
            "--base-branch",
            "main",
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.ensure-product-pr-result.json").read_text()
    )
    assert envelope["outputs"]["pr_number"] == 9
