from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    WorkflowSecurityError,
    WorkflowTransientError,
    WorkflowValidationError,
)
from ard_ossie.application.model_schema_verification import MODEL_SCHEMA_CATALOG
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
        self.clean = True

    def current_sha(self) -> str:
        return self.sha

    def is_worktree_clean(self) -> bool:
        return self.clean

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        assert base_ref == BASE
        assert head_ref == HEAD
        return ChangedPaths(merge_base=BASE, paths=self.paths)


class FakeTools:
    def __init__(
        self,
        *,
        fail: str | None = None,
        mutate_after: str | None = None,
        git: FakeGit | None = None,
    ) -> None:
        self.names: list[str] = []
        self.fail = fail
        self.mutate_after = mutate_after
        self.git = git

    def run(self, name: str) -> dict[str, object]:
        self.names.append(name)
        if name == self.fail:
            raise WorkflowValidationError("REPOSITORY_VERIFIER_FAILED", name)
        if name == self.mutate_after:
            assert self.git is not None
            self.git.clean = False
        return {"name": name, "status": "success"}


def write_model_schema_receipt(request) -> None:
    result = Path(request.argv[request.argv.index("--result") + 1])
    nonce = request.argv[request.argv.index("--nonce") + 1]
    result.write_text(
        json.dumps(
            {
                "nonce": nonce,
                "schemas": [
                    reference.schema_path.as_posix()
                    for reference in MODEL_SCHEMA_CATALOG
                ],
                "status": "success",
            }
        ),
        encoding="utf-8",
    )


def request(tmp_path: Path) -> RepositoryCheckRequest:
    return RepositoryCheckRequest(
        repository=tmp_path,
        base_ref=BASE,
        head_ref=HEAD,
        head_sha=HEAD,
        verification_group="static",
    )


def test_repository_check_rejects_same_checkout_aggregate_group(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="verification_group"):
        RepositoryCheckRequest(
            repository=tmp_path,
            base_ref=BASE,
            head_ref=HEAD,
            head_sha=HEAD,
            verification_group="all",
        )


def service(tmp_path: Path, git: FakeGit, tools: FakeTools):
    return RepositoryCheckService(
        RepositoryPaths(tmp_path),
        git,
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
    with pytest.raises(
        WorkflowValidationError,
        match="MIXED_CODE_AND_ARD_DATA_NOT_ALLOWED",
    ):
        service(tmp_path, git, tools).run(request(tmp_path))

    assert tools.names == []


def test_repository_check_runs_static_verifiers_in_order(tmp_path: Path) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools()
    result = service(tmp_path, git, tools).run(request(tmp_path))

    assert tools.names == [
        "ruff",
        "actionlint",
        "schemas",
        "ossie-checksum",
        "secret-scan",
    ]
    assert result.outputs["code_only"] is True
    assert result.mutations == []


def test_repository_static_group_never_executes_candidate_tests_or_build(
    tmp_path: Path,
) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools()
    result = service(tmp_path, git, tools).run(
        request(tmp_path).model_copy(update={"verification_group": "static"})
    )

    assert result.status.value == "success"
    assert tools.names == [
        "ruff",
        "actionlint",
        "schemas",
        "ossie-checksum",
        "secret-scan",
    ]


@pytest.mark.parametrize("verification_group", ["model-schemas", "pytest", "wheel"])
def test_repository_executable_group_runs_one_isolated_verifier(
    tmp_path: Path,
    verification_group: str,
) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools()

    service(tmp_path, git, tools).run(
        request(tmp_path).model_copy(
            update={"verification_group": verification_group}
        )
    )

    assert tools.names == [verification_group]


def test_repository_check_never_publishes_statuses(tmp_path: Path) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools()

    result = service(tmp_path, git, tools).run(request(tmp_path))

    assert result.status.value == "success"
    assert tools.names == [
        "ruff",
        "actionlint",
        "schemas",
        "ossie-checksum",
        "secret-scan",
    ]
    assert result.mutations == []


def test_repository_check_stops_on_first_verifier_failure(tmp_path: Path) -> None:
    git = FakeGit((Path("pyproject.toml"),))
    tools = FakeTools(fail="actionlint")
    with pytest.raises(WorkflowValidationError, match="REPOSITORY_VERIFIER_FAILED"):
        service(tmp_path, git, tools).run(request(tmp_path))

    assert tools.names == ["ruff", "actionlint"]


def test_repository_check_rejects_candidate_tree_mutation_between_verifiers(
    tmp_path: Path,
) -> None:
    git = FakeGit((Path("src/ard_ossie/cli/root.py"),))
    tools = FakeTools(mutate_after="pytest", git=git)
    with pytest.raises(WorkflowSecurityError, match="REPOSITORY_CHECK_TREE_CHANGED"):
        service(tmp_path, git, tools).run(
            request(tmp_path).model_copy(update={"verification_group": "pytest"})
        )

    assert tools.names == ["pytest"]


def test_repository_check_data_only_is_noop(tmp_path: Path) -> None:
    git = FakeGit((Path("registry/products/product.json"),))
    tools = FakeTools()
    result = service(tmp_path, git, tools).run(request(tmp_path))

    assert result.outputs["code_only"] is False
    assert tools.names == []


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
    archive_name = "actionlint_1.7.7_linux_amd64.tar.gz"
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


def test_ruff_uses_trusted_environment_without_syncing_candidate_project(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self) -> None:
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return CommandResult(returncode=0, stdout="", stderr="")

    runner = Runner()
    RepositoryVerificationTools(RepositoryPaths(tmp_path), runner).run("ruff")

    assert runner.requests[0].argv == ("ruff", "check", "src", "tests")


@pytest.mark.parametrize("verifier", ["model-schemas", "pytest", "wheel"])
def test_candidate_executable_verifiers_receive_credential_free_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier: str,
) -> None:
    class Runner:
        def __init__(self) -> None:
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            if verifier == "model-schemas":
                write_model_schema_receipt(request)
            if verifier == "wheel":
                output = Path(request.argv[request.argv.index("--out-dir") + 1])
                with zipfile.ZipFile(output / "candidate.whl", "w") as archive:
                    for name in (
                        "ard_ossie/application/model_schema_verification.py",
                        "ard_ossie/application/release_detection.py",
                        "ard_ossie/application/release_publication.py",
                        "ard_ossie/application/release_dispatch.py",
                        "ard_ossie/application/repository_checks.py",
                    ):
                        archive.writestr(name, "")
            return CommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("GH_TOKEN", "github-token")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "cloud-key")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "credential-config"))
    runner = Runner()
    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), runner)

    tools.run(verifier)

    request_env = runner.requests[0].env
    assert request_env is not None
    assert "GH_TOKEN" not in request_env
    assert "GITHUB_TOKEN" not in request_env
    assert "GH_CONFIG_DIR" not in request_env
    assert "AWS_ACCESS_KEY_ID" not in request_env
    assert "SSH_AUTH_SOCK" not in request_env
    assert request_env["HOME"] != str(Path.home())
    assert request_env["XDG_CONFIG_HOME"] != str(tmp_path / "credential-config")


def test_model_schema_verifier_invokes_absolute_trusted_helper(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self) -> None:
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            write_model_schema_receipt(request)
            return CommandResult(returncode=0, stdout="", stderr="")

    runner = Runner()
    RepositoryVerificationTools(RepositoryPaths(tmp_path), runner).run("model-schemas")

    command = runner.requests[0]
    helper = Path(command.argv[5])
    assert command.argv[:5] == ("uv", "run", "--frozen", "python", "-I")
    assert helper.is_absolute()
    assert not helper.is_relative_to(tmp_path)
    assert command.argv[6:8] == ("--repository", str(tmp_path.resolve()))
    assert command.argv[8] == "--result"
    assert Path(command.argv[9]).name == "receipt.json"
    assert command.argv[10] == "--nonce"
    assert len(command.argv[11]) == 64
    assert command.cwd == tmp_path.resolve()


def test_model_schema_verifier_rejects_success_without_completion_receipt(
    tmp_path: Path,
) -> None:
    class Runner:
        def run(self, request):
            return CommandResult(returncode=0, stdout="", stderr="")

    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), Runner())

    with pytest.raises(
        WorkflowValidationError,
        match="REPOSITORY_MODEL_SCHEMAS_RECEIPT_INVALID",
    ):
        tools.run("model-schemas")


def test_model_schema_verifier_rejects_trusted_helper_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "trusted-model-schema-helper.py"
    helper.write_text("trusted helper", encoding="utf-8")
    monkeypatch.setattr(
        "ard_ossie.application.repository_checks._MODEL_SCHEMA_HELPER",
        helper,
        raising=False,
    )

    class Runner:
        def run(self, request):
            helper.write_text("mutated helper", encoding="utf-8")
            write_model_schema_receipt(request)
            return CommandResult(returncode=0, stdout="", stderr="")

    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), Runner())

    with pytest.raises(
        WorkflowSecurityError,
        match="TRUSTED_MODEL_SCHEMA_HELPER_CHANGED",
    ):
        tools.run("model-schemas")


def test_model_schema_verifier_discards_timeout_evidence(tmp_path: Path) -> None:
    class Runner:
        def run(self, request):
            raise WorkflowTransientError(
                "COMMAND_TIMEOUT",
                "candidate-controlled timeout evidence",
            )

    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), Runner())

    with pytest.raises(WorkflowTransientError, match="COMMAND_TIMEOUT") as raised:
        tools.run("model-schemas")

    assert raised.value.retryable is True
    assert "candidate-controlled" not in str(raised.value)
    assert "model-schemas verifier failed" in str(raised.value)


def test_static_schema_verifier_accepts_valid_candidate_model_schema_change(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    candidate = tmp_path / "schemas" / "ir" / "product-ir.schema.json"
    schema = json.loads(candidate.read_text(encoding="utf-8"))
    schema["title"] = "CandidateProductIR"
    candidate.write_text(json.dumps(schema), encoding="utf-8")

    RepositoryVerificationTools(
        RepositoryPaths(tmp_path), runner=None  # type: ignore[arg-type]
    ).run("schemas")


def test_static_schema_verifier_rejects_untrusted_catalog_entry(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    (tmp_path / "schemas" / "unexpected.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
        ),
        encoding="utf-8",
    )

    tools = RepositoryVerificationTools(
        RepositoryPaths(tmp_path), runner=None  # type: ignore[arg-type]
    )
    with pytest.raises(WorkflowValidationError, match="SCHEMA_CATALOG_MISMATCH"):
        tools.run("schemas")


def test_static_schema_verifier_rejects_missing_catalog_entry(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    (tmp_path / "schemas" / "source-manifest.schema.json").unlink()

    tools = RepositoryVerificationTools(
        RepositoryPaths(tmp_path), runner=None  # type: ignore[arg-type]
    )
    with pytest.raises(WorkflowValidationError, match="SCHEMA_CATALOG_MISMATCH"):
        tools.run("schemas")


def test_static_schema_verifier_rejects_malformed_schema(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    (tmp_path / "schemas" / "source-manifest.schema.json").write_text(
        "{not-json", encoding="utf-8"
    )

    tools = RepositoryVerificationTools(
        RepositoryPaths(tmp_path), runner=None  # type: ignore[arg-type]
    )
    with pytest.raises(WorkflowValidationError, match="SCHEMA_SYNCHRONIZATION_FAILED"):
        tools.run("schemas")


def test_checked_in_schemas_are_synchronized_with_models(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), runner=None)  # type: ignore[arg-type]

    tools.run("schemas")


def test_secret_scan_streams_files_larger_than_previous_limit(tmp_path: Path) -> None:
    payload = b"x" * (6 * 1024 * 1024) + b"sk-" + b"s" * 24
    (tmp_path / "large-fixture.bin").write_bytes(payload)
    tools = RepositoryVerificationTools(RepositoryPaths(tmp_path), runner=None)  # type: ignore[arg-type]

    with pytest.raises(WorkflowValidationError, match="REPOSITORY_SECRET_PATTERN_FOUND"):
        tools.run("secret-scan")


def test_secret_scan_does_not_exclude_a_root_nested_below_dot_ard(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".ard" / "staging" / "candidate"
    root.mkdir(parents=True)
    (root / "secret.txt").write_text("sk-" + "s" * 24, encoding="utf-8")
    tools = RepositoryVerificationTools(RepositoryPaths(root), runner=None)  # type: ignore[arg-type]

    with pytest.raises(WorkflowValidationError, match="REPOSITORY_SECRET_PATTERN_FOUND"):
        tools.run("secret-scan")
