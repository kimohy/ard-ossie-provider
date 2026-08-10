from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowValidationError,
)
from ard_ossie.application.repository_checks import (
    RepositoryCheckRequest,
    RepositoryCheckService,
    RepositoryVerificationTools,
)
from ard_ossie.ports.git import ChangedPaths
from ard_ossie.ports.process import CommandResult

BASE = "a" * 40
HEAD = "b" * 40


class FakeGit:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths
        self.sha = HEAD

    def current_sha(self) -> str:
        return self.sha

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        assert base_ref == BASE
        assert head_ref == HEAD
        return ChangedPaths(merge_base=BASE, paths=self.paths)


class FakeTools:
    def __init__(self, *, fail: str | None = None) -> None:
        self.names: list[str] = []
        self.fail = fail

    def run(self, name: str) -> dict[str, object]:
        self.names.append(name)
        if name == self.fail:
            raise WorkflowValidationError("REPOSITORY_VERIFIER_FAILED", name)
        return {"name": name, "status": "success"}


class FakeGitHub:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, str]] = []

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ) -> MutationRecord:
        self.statuses.append((sha, context, state))
        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")


def request(tmp_path: Path) -> RepositoryCheckRequest:
    return RepositoryCheckRequest(
        repository=tmp_path,
        base_ref=BASE,
        head_ref=HEAD,
        head_sha=HEAD,
    )


def service(tmp_path: Path, git: FakeGit, tools: FakeTools, github: FakeGitHub):
    return RepositoryCheckService(
        RepositoryPaths(tmp_path),
        git,
        github,
        tools,
    )


def test_repository_check_rejects_mixed_code_and_data(tmp_path: Path) -> None:
    git = FakeGit(
        (
            Path("src/ard_ossie/cli/root.py"),
            Path("products/a/sources/a.html"),
        )
    )
    tools = FakeTools()
    github = FakeGitHub()

    with pytest.raises(
        WorkflowValidationError,
        match="MIXED_CODE_AND_ARD_DATA_NOT_ALLOWED",
    ):
        service(tmp_path, git, tools, github).run(request(tmp_path))

    assert tools.names == []
    assert github.statuses == [
        (HEAD, "ard/quality-gate", "failure"),
        (HEAD, "ard/changeset", "failure"),
    ]


def test_repository_check_runs_pinned_verifiers_in_order(tmp_path: Path) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools()
    github = FakeGitHub()

    result = service(tmp_path, git, tools, github).run(request(tmp_path))

    assert tools.names == [
        "pytest",
        "ruff",
        "actionlint",
        "schemas",
        "wheel",
        "ossie-checksum",
        "secret-scan",
    ]
    assert result.outputs["code_only"] is True
    assert github.statuses == [
        (HEAD, "ard/quality-gate", "success"),
        (HEAD, "ard/changeset", "success"),
    ]


def test_repository_check_stops_on_first_verifier_failure(tmp_path: Path) -> None:
    git = FakeGit((Path("pyproject.toml"),))
    tools = FakeTools(fail="actionlint")
    github = FakeGitHub()

    with pytest.raises(WorkflowValidationError, match="REPOSITORY_VERIFIER_FAILED"):
        service(tmp_path, git, tools, github).run(request(tmp_path))

    assert tools.names == ["pytest", "ruff", "actionlint"]
    assert github.statuses[-2:] == [
        (HEAD, "ard/quality-gate", "failure"),
        (HEAD, "ard/changeset", "failure"),
    ]


def test_repository_check_data_only_is_noop(tmp_path: Path) -> None:
    git = FakeGit((Path("registry/products/product.json"),))
    tools = FakeTools()
    github = FakeGitHub()

    result = service(tmp_path, git, tools, github).run(request(tmp_path))

    assert result.outputs["code_only"] is False
    assert tools.names == []
    assert github.statuses == []


def test_actionlint_archive_is_checksum_verified_and_cached_binary_is_repaired(
    tmp_path: Path,
) -> None:
    workflow = tmp_path / ".github" / "workflows" / "check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: check\n", encoding="utf-8")
    binary_payload = b"verified-actionlint"
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("actionlint")
        info.size = len(binary_payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(binary_payload))
    archive_payload = archive_buffer.getvalue()
    archive_name = "actionlint_1.7.7_linux_x86_64.tar.gz"
    manifest = (
        f"{hashlib.sha256(archive_payload).hexdigest()}  {archive_name}\n".encode()
    )
    downloads: list[str] = []

    def download(url: str) -> bytes:
        downloads.append(url)
        return manifest if url.endswith("checksums.txt") else archive_payload

    class Runner:
        def __init__(self) -> None:
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return CommandResult(returncode=0, stdout="", stderr="")

    runner = Runner()
    tools = RepositoryVerificationTools(
        RepositoryPaths(tmp_path),
        runner,
        downloader=download,
    )

    tools.run("actionlint")
    cached = tmp_path / ".ard" / "tools" / "actionlint-1.7.7" / "actionlint"
    cached.write_bytes(b"tampered")
    tools.run("actionlint")

    assert cached.read_bytes() == binary_payload
    assert sum(url.endswith(archive_name) for url in downloads) == 1
    assert len(runner.requests) == 2
