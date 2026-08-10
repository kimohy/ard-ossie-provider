from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.github as github_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.application.github_bootstrap import (
    BootstrapConfig,
    BootstrapItem,
    BootstrapPlan,
)
from ard_ossie.cli import app

REPOSITORY = "kimohy/ard-ossie-provider"


class StubBootstrapService:
    def __init__(self, *, secret_present: bool = False) -> None:
        self.secret_present = secret_present
        self.applied_api_key: str | None = None

    def plan(self, config: BootstrapConfig) -> BootstrapPlan:
        return BootstrapPlan(
            repository=REPOSITORY,
            owner_login="kimohy",
            owner_id=11,
            config=config,
            secret_present=self.secret_present,
            items=[BootstrapItem(target="branch:main", action="update")],
        )

    def apply(
        self,
        plan,
        *,
        api_key=None,
        api_key_provider=None,
        replace_secret=False,
    ):
        self.applied_api_key = (
            api_key if api_key is not None else api_key_provider()
            if api_key_provider is not None
            else None
        )
        return WorkflowResult(
            command="github.bootstrap",
            status=WorkflowStatus.SUCCESS,
            outputs={"repository": REPOSITORY},
        )

    def enable_review_protection(self):
        return WorkflowResult(
            command="github.enable-review-protection",
            status=WorkflowStatus.SUCCESS,
        )


def test_github_bootstrap_dry_run_never_prompts_for_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubBootstrapService()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github_cli, "_bootstrap_service", lambda repo: service)
    monkeypatch.setattr(
        github_cli,
        "_getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("secret prompt")),
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "github",
            "bootstrap",
            "--repo",
            REPOSITORY,
            "--base-url",
            "https://api.openai.com/v1",
            "--model",
            "gpt-example",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.applied_api_key is None
    assert "ARD_LLM_API_KEY" not in result.output


def test_github_bootstrap_collects_secret_only_after_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubBootstrapService()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github_cli, "_bootstrap_service", lambda repo: service)
    monkeypatch.setattr(github_cli, "_confirm", lambda *args, **kwargs: True, raising=False)
    monkeypatch.setattr(github_cli, "_getpass", lambda prompt: "sentinel-key", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "github",
            "bootstrap",
            "--repo",
            REPOSITORY,
            "--base-url",
            "https://api.openai.com/v1",
            "--model",
            "gpt-example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.applied_api_key == "sentinel-key"
    assert "sentinel-key" not in result.output
    assert all(
        "sentinel-key" not in path.read_text(errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
