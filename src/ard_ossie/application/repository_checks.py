from __future__ import annotations

import hashlib
import json
import platform
import re
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx
from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowTransientError,
    WorkflowValidationError,
)
from ard_ossie.models import StrictModel
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort
from ard_ossie.ports.process import CommandRequest, CommandRunner

_VERIFIERS = (
    "pytest",
    "ruff",
    "actionlint",
    "schemas",
    "wheel",
    "ossie-checksum",
    "secret-scan",
)
_ACTIONLINT_VERSION = "1.7.7"
_ACTIONLINT_ARCHIVE = f"actionlint_{_ACTIONLINT_VERSION}_linux_x86_64.tar.gz"
_ACTIONLINT_CHECKSUMS = f"actionlint_{_ACTIONLINT_VERSION}_checksums.txt"
_ACTIONLINT_RELEASE_ROOT = (
    f"https://github.com/rhysd/actionlint/releases/download/v{_ACTIONLINT_VERSION}"
)
_SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,255}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class RepositoryCheckRequest(StrictModel):
    repository: Path
    base_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_url: str = ""


class RepositoryToolsPort(Protocol):
    def run(self, name: str) -> dict[str, object]: ...


class RepositoryCheckService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
        tools: RepositoryToolsPort,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github
        self.tools = tools

    def run(self, request: RepositoryCheckRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "REPOSITORY_CHECK_ROOT_MISMATCH",
                "repository check root does not match filesystem port",
            )
        if self.git.current_sha() != request.head_sha or request.head_ref != request.head_sha:
            raise WorkflowSecurityError(
                "REPOSITORY_CHECK_HEAD_MISMATCH",
                "repository check must run at the exact pull request head",
            )
        changed = self.git.changed_paths(request.base_ref, request.head_ref)
        data_change = False
        repository_change = False
        for path in changed.paths:
            relative = self._safe_relative(path)
            if relative.parts and relative.parts[0] in {"products", "registry"}:
                data_change = True
            else:
                repository_change = True
        outputs: dict[str, object] = {
            "code_only": repository_change and not data_change,
            "head_sha": request.head_sha,
            "merge_base": changed.merge_base,
            "verifiers": [],
        }
        if data_change and repository_change:
            error = WorkflowValidationError(
                "MIXED_CODE_AND_ARD_DATA_NOT_ALLOWED",
                "repository code and ARD data cannot change together",
            )
            mutations = self._publish_statuses(
                request,
                "failure",
                "Mixed repository and ARD data changes are not allowed",
                outputs,
            )
            _attach_failure_context(error, outputs, mutations)
            raise error
        if not repository_change:
            return WorkflowResult(
                command="workflow.repository-check",
                status=WorkflowStatus.NOOP,
                outputs=outputs,
            )

        summaries: list[dict[str, object]] = []
        outputs["verifiers"] = summaries
        for name in _VERIFIERS:
            try:
                summaries.append(self.tools.run(name))
            except WorkflowError as error:
                summaries.append(
                    {"name": name, "status": "failure", "code": error.code}
                )
                mutations = self._publish_statuses(
                    request,
                    "failure",
                    f"Repository verifier failed: {name}",
                    outputs,
                )
                _attach_failure_context(error, outputs, mutations)
                raise

        if self.git.current_sha() != request.head_sha:
            error = WorkflowSecurityError(
                "REPOSITORY_CHECK_HEAD_CHANGED",
                "repository head changed while verifiers were running",
            )
            mutations = self._publish_statuses(
                request,
                "failure",
                "Repository head changed during validation",
                outputs,
            )
            _attach_failure_context(error, outputs, mutations)
            raise error
        mutations = self._publish_statuses(
            request,
            "success",
            "Repository validation passed",
            outputs,
        )
        return WorkflowResult(
            command="workflow.repository-check",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
            mutations=mutations,
        )

    def _safe_relative(self, path: Path) -> Path:
        resolved = self.paths.resolve_write(path)
        relative = resolved.relative_to(self.paths.root)
        if path.is_absolute() or ".." in path.parts or relative != path:
            raise WorkflowSecurityError(
                "REPOSITORY_CHANGED_PATH_UNSAFE",
                "changed path is outside the repository",
            )
        return relative

    def _publish_statuses(
        self,
        request: RepositoryCheckRequest,
        state: str,
        description: str,
        outputs: dict[str, object],
    ) -> list[MutationRecord]:
        mutations: list[MutationRecord] = []
        try:
            for context in ("ard/quality-gate", "ard/changeset"):
                mutations.append(
                    self.github.set_status(
                        request.head_sha,
                        context,
                        state,
                        description,
                        request.target_url,
                    )
                )
        except WorkflowPartialError:
            raise
        except WorkflowError as error:
            if not mutations:
                raise
            raise WorkflowPartialError(
                "REPOSITORY_STATUS_PARTIAL",
                "repository status publication did not converge",
                retryable=error.retryable,
                outputs=outputs,
                mutations=mutations,
            ) from error
        return mutations


class RepositoryVerificationTools:
    def __init__(
        self,
        paths: FileSystemPort,
        runner: CommandRunner,
        *,
        downloader: Callable[[str], bytes] | None = None,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.downloader = downloader or _download

    def run(self, name: str) -> dict[str, object]:
        if name not in _VERIFIERS:
            raise WorkflowValidationError(
                "REPOSITORY_VERIFIER_UNKNOWN",
                f"unknown repository verifier: {name}",
            )
        getattr(self, f"_run_{name.replace('-', '_')}")()
        return {"name": name, "status": "success"}

    def _run_pytest(self) -> None:
        self._command("pytest", "uv", "run", "--frozen", "pytest", "-q", timeout=3600)

    def _run_ruff(self) -> None:
        self._command(
            "ruff",
            "uv",
            "run",
            "--frozen",
            "ruff",
            "check",
            "src",
            "tests",
            timeout=600,
        )

    def _run_actionlint(self) -> None:
        binary = self._verified_actionlint()
        workflows = sorted((self.paths.root / ".github" / "workflows").glob("*.yml"))
        if not workflows:
            raise WorkflowValidationError(
                "ACTIONLINT_WORKFLOWS_MISSING",
                "repository contains no workflow files",
            )
        self._command(
            "actionlint",
            str(binary),
            *(str(path.relative_to(self.paths.root)) for path in workflows),
            timeout=600,
        )

    def _run_schemas(self) -> None:
        schemas = sorted((self.paths.root / "schemas").rglob("*.json"))
        if not schemas:
            raise WorkflowValidationError("SCHEMAS_MISSING", "checked-in schemas are missing")
        try:
            for path in schemas:
                Draft202012Validator.check_schema(
                    json.loads(self.paths.resolve_read(path).read_text(encoding="utf-8"))
                )
        except (OSError, TypeError, ValueError, SchemaError) as error:
            raise WorkflowValidationError(
                "SCHEMA_SYNCHRONIZATION_FAILED",
                "checked-in schema is invalid",
            ) from error

    def _run_wheel(self) -> None:
        staging = self.paths.resolve_write(".ard/staging")
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="repository-wheel-", dir=staging) as value:
            output = Path(value)
            self._command(
                "wheel",
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(output),
                timeout=1200,
            )
            wheels = list(output.glob("*.whl"))
            if len(wheels) != 1:
                raise WorkflowValidationError(
                    "WHEEL_BUILD_OUTPUT_INVALID",
                    "wheel build did not produce exactly one artifact",
                )
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            expected = {
                *(
                    "ard_ossie/assets/templates/"
                    f"{path.relative_to(self.paths.root / 'templates').as_posix()}"
                    for path in (self.paths.root / "templates").rglob("*")
                    if path.is_file()
                ),
                *(
                    "ard_ossie/assets/schemas/"
                    f"{path.relative_to(self.paths.root / 'schemas').as_posix()}"
                    for path in (self.paths.root / "schemas").rglob("*")
                    if path.is_file()
                ),
                "ard_ossie/application/release_detection.py",
                "ard_ossie/application/release_publication.py",
                "ard_ossie/application/release_dispatch.py",
                "ard_ossie/application/repository_checks.py",
            }
            missing = sorted(expected - names)
            if missing:
                raise WorkflowValidationError(
                    "WHEEL_ASSET_MISSING",
                    f"wheel is missing required asset: {missing[0]}",
                )

    def _run_ossie_checksum(self) -> None:
        root = self.paths.root / "schemas" / "ossie" / "0.1.1"
        try:
            line = self.paths.resolve_read(root / "SHA256SUMS").read_text(
                encoding="utf-8"
            ).strip()
            digest, filename = line.split()
            schema = self.paths.resolve_read(root / filename)
        except (OSError, TypeError, ValueError) as error:
            raise WorkflowValidationError(
                "OSSIE_CHECKSUM_MANIFEST_INVALID",
                "Ossie checksum manifest is malformed",
            ) from error
        actual = hashlib.sha256(schema.read_bytes()).hexdigest()
        if filename != "osi-schema.json" or actual != digest:
            raise WorkflowValidationError(
                "OSSIE_CHECKSUM_MISMATCH",
                "vendored Ossie 0.1.1 schema checksum differs",
            )

    def _run_secret_scan(self) -> None:
        excluded = {".git", ".ard", ".venv", "__pycache__"}
        for path in sorted(self.paths.root.rglob("*")):
            if not path.is_file() or any(part in excluded for part in path.parts):
                continue
            if path.stat().st_size > 5 * 1024 * 1024:
                continue
            content = path.read_bytes()
            if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
                raise WorkflowValidationError(
                    "REPOSITORY_SECRET_PATTERN_FOUND",
                    f"high-confidence secret pattern found: {path.relative_to(self.paths.root)}",
                )

    def _verified_actionlint(self) -> Path:
        if platform.system() != "Linux" or platform.machine().casefold() not in {
            "x86_64",
            "amd64",
        }:
            raise WorkflowValidationError(
                "ACTIONLINT_PLATFORM_UNSUPPORTED",
                "pinned actionlint binary requires Linux x86_64",
            )
        tool_root = self.paths.resolve_write(f".ard/tools/actionlint-{_ACTIONLINT_VERSION}")
        tool_root.mkdir(parents=True, exist_ok=True)
        manifest = self.downloader(f"{_ACTIONLINT_RELEASE_ROOT}/{_ACTIONLINT_CHECKSUMS}")
        manifest_path = self.paths.resolve_write(tool_root / _ACTIONLINT_CHECKSUMS)
        manifest_path.write_bytes(manifest)
        expected = _manifest_digest(manifest, _ACTIONLINT_ARCHIVE)
        archive_path = self.paths.resolve_write(tool_root / _ACTIONLINT_ARCHIVE)
        if not archive_path.is_file() or _sha256(archive_path.read_bytes()) != expected:
            archive_path.write_bytes(
                self.downloader(f"{_ACTIONLINT_RELEASE_ROOT}/{_ACTIONLINT_ARCHIVE}")
            )
        archive = archive_path.read_bytes()
        if _sha256(archive) != expected:
            raise WorkflowValidationError(
                "ACTIONLINT_ARCHIVE_CHECKSUM_MISMATCH",
                "actionlint archive differs from the official checksum manifest",
            )
        payload = _actionlint_binary(archive_path)
        binary = self.paths.resolve_write(tool_root / "actionlint")
        if not binary.is_file() or binary.read_bytes() != payload:
            binary.write_bytes(payload)
            binary.chmod(0o755)
        return binary

    def _command(self, name: str, *argv: str, timeout: int) -> None:
        result = self.runner.run(
            CommandRequest(
                argv=tuple(argv),
                cwd=self.paths.root,
                timeout_seconds=timeout,
            )
        )
        if result.returncode != 0:
            evidence = (result.stderr or result.stdout).strip()[:400]
            raise WorkflowValidationError(
                f"REPOSITORY_{name.upper().replace('-', '_')}_FAILED",
                evidence or f"{name} verifier failed",
            )


def _download(url: str) -> bytes:
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=60) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    raise WorkflowValidationError(
                        "ACTIONLINT_DOWNLOAD_INVALID",
                        "downloaded actionlint asset exceeds the size limit",
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
    except httpx.HTTPError as error:
        raise WorkflowTransientError(
            "ACTIONLINT_DOWNLOAD_FAILED",
            "could not download the pinned actionlint release",
        ) from error
    if not content:
        raise WorkflowValidationError(
            "ACTIONLINT_DOWNLOAD_INVALID",
            "downloaded actionlint asset has an invalid size",
        )
    return content


def _manifest_digest(manifest: bytes, filename: str) -> str:
    matches: list[str] = []
    try:
        for line in manifest.decode("utf-8").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].removeprefix("*") == filename:
                matches.append(fields[0].casefold())
    except UnicodeDecodeError as error:
        raise WorkflowValidationError(
            "ACTIONLINT_CHECKSUM_MANIFEST_INVALID",
            "actionlint checksum manifest is not UTF-8",
        ) from error
    if len(matches) != 1 or re.fullmatch(r"[0-9a-f]{64}", matches[0]) is None:
        raise WorkflowValidationError(
            "ACTIONLINT_CHECKSUM_MANIFEST_INVALID",
            "actionlint archive checksum is missing or ambiguous",
        )
    return matches[0]


def _actionlint_binary(archive: Path) -> bytes:
    try:
        with tarfile.open(archive, mode="r:gz") as package:
            members = [
                item
                for item in package.getmembers()
                if item.name == "actionlint" and item.isfile() and not item.issym()
            ]
            if len(members) != 1 or members[0].size > 64 * 1024 * 1024:
                raise WorkflowValidationError(
                    "ACTIONLINT_ARCHIVE_INVALID",
                    "actionlint archive does not contain one safe binary",
                )
            stream = package.extractfile(members[0])
            if stream is None:
                raise WorkflowValidationError(
                    "ACTIONLINT_ARCHIVE_INVALID",
                    "actionlint binary could not be read",
                )
            return stream.read()
    except WorkflowValidationError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise WorkflowValidationError(
            "ACTIONLINT_ARCHIVE_INVALID",
            "actionlint archive is malformed",
        ) from error


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _attach_failure_context(
    error: WorkflowError,
    outputs: dict[str, object],
    mutations: list[MutationRecord],
) -> None:
    error.outputs = outputs
    error.mutations = mutations
