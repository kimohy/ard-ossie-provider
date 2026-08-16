# CLI-First Foundation and Adapters Implementation Plan

> **Superseded policy (2026-08-16):** Public visibility requirements in this historical plan are
> replaced by the private repository and Issue intake contract in
> `docs/superpowers/specs/2026-08-16-private-repository-issue-intake-auth-design.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the versioned CLI execution contract, split the Typer entry point into focused command modules, and introduce testable process, Git, GitHub, and filesystem adapters without changing GitHub Actions behavior.

**Architecture:** Pure domain code remains independent of GitHub and subprocesses. CLI commands assemble application services through typed ports; concrete adapters use argument-array subprocess calls and emit a redacted `WorkflowResult`. This phase preserves every current command while establishing interfaces consumed by the workflow migrations in plans 02 and 03.

**Tech Stack:** Python 3.12, Typer 0.26.8, Pydantic 2.12, uv 0.11.33, pytest 8, Ruff, Git, Git LFS, GitHub CLI

## Global Constraints

- Target repository: `kimohy/ard-ossie-provider`; public visibility and `main` remain unchanged.
- Keep OpenAI-compatible provider settings in environment variables; never accept API keys or GitHub tokens as CLI arguments.
- Stable entity IDs and independent numeric versions `v1`–`v999` remain authoritative.
- Use `subprocess.run(..., shell=False)` with argument arrays, bounded output, and timeouts.
- Resolve every filesystem input/output below an explicit repository root and reject traversal and unexpected symlinks.
- `.ard/` is runtime-only, Git-ignored, and excluded from writeback.
- Existing commands remain compatible for one project release.
- Actions install uv with `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b` (`v8.1.0`) and request uv `0.11.33` explicitly.
- Every task follows red-green-refactor, runs focused tests, then commits only its own files.

---

## File structure produced by this plan

```text
src/ard_ossie/
├── application/
│   ├── __init__.py
│   ├── contracts.py        # WorkflowContext, WorkflowResult, mutation records
│   └── output.py           # atomic result/GitHub output/summary writing
├── ports/
│   ├── __init__.py
│   ├── process.py          # external command runner protocol
│   ├── git.py              # Git/LFS protocol
│   ├── github.py           # GitHub resource protocol
│   └── filesystem.py       # repository-root path policy
├── adapters/
│   ├── __init__.py
│   ├── subprocess.py       # safe command runner
│   ├── git_cli.py          # Git/LFS implementation
│   ├── github_cli.py       # gh implementation
│   └── filesystem.py       # concrete safe-path implementation
└── cli/
    ├── __init__.py         # preserves ard_ossie.cli:app
    ├── root.py             # Typer root and group registration only
    ├── process.py
    ├── registry.py
    ├── changeset.py
    ├── release.py
    ├── history.py
    ├── parse.py
    ├── model.py
    ├── validate.py
    ├── github.py
    └── workflow.py
```

### Task 1: Versioned workflow result and exit-code contract

**Files:**
- Create: `src/ard_ossie/application/__init__.py`
- Create: `src/ard_ossie/application/contracts.py`
- Create: `src/ard_ossie/application/output.py`
- Modify: `.gitignore`
- Test: `tests/unit/test_application_contracts.py`
- Test: `tests/unit/test_application_output.py`

**Interfaces:**
- Consumes: `ard_ossie.models.StrictModel`
- Produces: `ExitCode`, `WorkflowError` subclasses, `WorkflowStatus`, `WorkflowContext`, `MutationRecord`, `WorkflowResult`, `ResultWriter.write(result)`

- [ ] **Step 1: Write failing model and exit-code tests**

```python
from pathlib import Path

from ard_ossie.application.contracts import (
    ExitCode,
    MutationRecord,
    WorkflowContext,
    WorkflowResult,
    WorkflowStatus,
)


def test_workflow_result_is_versioned_and_redacted() -> None:
    result = WorkflowResult(
        command="workflow.process",
        status=WorkflowStatus.SUCCESS,
        outputs={"product_id": "prd_example"},
        mutations=[MutationRecord(resource="status", target="sha:ard/quality-gate", action="set")],
    )
    assert result.schema_version == 1
    assert result.retryable is False
    assert "secret" not in result.model_dump_json().lower()


def test_workflow_context_resolves_repository(tmp_path: Path) -> None:
    context = WorkflowContext(repository=tmp_path, event_name="pull_request", run_id="7")
    assert context.repository == tmp_path.resolve()
    assert ExitCode.SECURITY == 50
```

- [ ] **Step 2: Run the tests and verify the missing-module failure**

Run: `uv run pytest tests/unit/test_application_contracts.py -q`

Expected: FAIL with `ModuleNotFoundError: ard_ossie.application`.

- [ ] **Step 3: Implement the typed execution contract**

```python
class ExitCode(IntEnum):
    SUCCESS = 0
    VALIDATION = 10
    CONFIGURATION = 20
    TRANSIENT = 30
    CONFLICT = 40
    SECURITY = 50
    PARTIAL = 70


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NOOP = "noop"


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: ExitCode, *, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.exit_code = exit_code
        self.retryable = retryable


class WorkflowValidationError(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, ExitCode.VALIDATION)


class WorkflowConfigurationError(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, ExitCode.CONFIGURATION)


class WorkflowTransientError(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, ExitCode.TRANSIENT, retryable=True)


class WorkflowConflict(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, ExitCode.CONFLICT)


class WorkflowSecurityError(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, ExitCode.SECURITY)


class WorkflowPartialError(WorkflowError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(code, message, ExitCode.PARTIAL, retryable=retryable)


class MutationRecord(StrictModel):
    resource: str
    target: str
    action: str
    result_id: str | None = None


class WorkflowResult(StrictModel):
    schema_version: Literal[1] = 1
    command: str
    status: WorkflowStatus
    outputs: dict[str, str | int | bool | None] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    mutations: list[MutationRecord] = Field(default_factory=list)
    retryable: bool = False
```

Implement `WorkflowContext` with resolved `repository: Path`, optional resolved `event_path`, `event_name`, `run_id`, `repository_name`, `server_url`, and `actor`. Reject an event path outside the repository or runner temp directory.

- [ ] **Step 4: Write failing atomic output tests**

```python
def test_result_writer_writes_json_outputs_and_summary(tmp_path: Path) -> None:
    writer = ResultWriter(
        result_path=tmp_path / ".ard/run/result.json",
        github_output=tmp_path / "github-output",
        github_summary=tmp_path / "summary.md",
    )
    writer.write(WorkflowResult(command="workflow.detect", status="success", outputs={"key": "sales-order"}))
    assert json.loads((tmp_path / ".ard/run/result.json").read_text())["schema_version"] == 1
    assert "key=sales-order" in (tmp_path / "github-output").read_text()
    assert "workflow.detect" in (tmp_path / "summary.md").read_text()
```

- [ ] **Step 5: Implement `ResultWriter` and ignore runtime output**

Use same-directory `NamedTemporaryFile(delete=False)` plus `Path.replace()` for atomic JSON. Append GitHub scalar outputs with UTF-8 and render a fixed Markdown table containing command, status, artifacts, findings count, and redacted mutations. Add `/.ard/` to `.gitignore`.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/unit/test_application_contracts.py tests/unit/test_application_output.py -q`

Expected: PASS.

```bash
git add .gitignore src/ard_ossie/application tests/unit/test_application_contracts.py tests/unit/test_application_output.py
git commit -m "feat: add CLI workflow result contract"
```

### Task 2: Safe subprocess and repository filesystem adapters

**Files:**
- Create: `src/ard_ossie/ports/__init__.py`
- Create: `src/ard_ossie/ports/process.py`
- Create: `src/ard_ossie/ports/filesystem.py`
- Create: `src/ard_ossie/adapters/__init__.py`
- Create: `src/ard_ossie/adapters/subprocess.py`
- Create: `src/ard_ossie/adapters/filesystem.py`
- Test: `tests/unit/test_subprocess_adapter.py`
- Test: `tests/unit/test_filesystem_adapter.py`

**Interfaces:**
- Consumes: no Task 1 implementation beyond stable error codes
- Produces: `CommandRequest`, `CommandResult`, `CommandRunner.run(request)`, `RepositoryPaths.resolve_read()`, `resolve_write()`

- [ ] **Step 1: Write failing subprocess contract tests**

```python
def test_runner_uses_argument_array_and_captures_output() -> None:
    result = SubprocessRunner().run(
        CommandRequest(argv=(sys.executable, "-c", "print('ok')"), timeout_seconds=5)
    )
    assert result.returncode == 0
    assert result.stdout == "ok\n"


def test_runner_redacts_registered_secret() -> None:
    request = CommandRequest(
        argv=(sys.executable, "-c", "import sys; print(sys.stdin.read())"),
        stdin="sentinel-key",
        secrets=("sentinel-key",),
    )
    assert "sentinel-key" not in SubprocessRunner().run(request).stdout
```

- [ ] **Step 2: Run the subprocess tests and verify they fail**

Run: `uv run pytest tests/unit/test_subprocess_adapter.py -q`

Expected: FAIL because `ports.process` and `adapters.subprocess` do not exist.

- [ ] **Step 3: Implement command types and runner**

```python
@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: Path | None = None
    stdin: str | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: int = 60
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...
```

`SubprocessRunner` must set `shell=False`, `capture_output=True`, `text=True`, a bounded timeout, and a maximum captured-output size of 1 MiB per stream. Replace each non-empty registered secret with `***` in stdout, stderr, and raised timeout text.

- [ ] **Step 4: Write failing repository-path policy tests**

```python
def test_repository_paths_rejects_parent_escape(tmp_path: Path) -> None:
    paths = RepositoryPaths(tmp_path / "repo")
    with pytest.raises(PathPolicyError, match="PATH_OUTSIDE_REPOSITORY"):
        paths.resolve_write(Path("../secret"))


def test_repository_paths_rejects_symlink_write(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(PathPolicyError, match="SYMLINK_NOT_ALLOWED"):
        RepositoryPaths(root).resolve_write(Path("link/file"))
```

- [ ] **Step 5: Implement `RepositoryPaths`**

Resolve the root once. `resolve_read` requires an existing regular file/directory, checks every existing path component for symlinks, and returns a resolved path below root. `resolve_write` permits missing leaf components but rejects existing symlink ancestors and `.git/` writes. Add `is_writeback_allowed(path, product_key)` for exact `registry/**`, `products/<key>/generated/**`, `products/<key>/quality/**`, and `products/<key>/product.yaml` scopes.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/unit/test_subprocess_adapter.py tests/unit/test_filesystem_adapter.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/ports src/ard_ossie/adapters tests/unit/test_subprocess_adapter.py tests/unit/test_filesystem_adapter.py
git commit -m "feat: add safe CLI process and filesystem adapters"
```

### Task 3: Typed Git and Git LFS adapter

**Files:**
- Create: `src/ard_ossie/ports/git.py`
- Create: `src/ard_ossie/adapters/git_cli.py`
- Test: `tests/unit/test_git_cli_adapter.py`

**Interfaces:**
- Consumes: `CommandRunner`, `CommandRequest`, `CommandResult`, `RepositoryPaths`
- Produces: `GitPort`, `GitCli`, `ChangedPaths`, `CommitResult`

- [ ] **Step 1: Write failing fake-runner tests**

```python
def test_changed_paths_uses_merge_base_and_name_only(tmp_path: Path) -> None:
    runner = RecordingRunner([
        ok("abc123\n"),
        ok("products/sales-order/sources/product.html\n"),
    ])
    changed = GitCli(tmp_path, runner).changed_paths("origin/main", "HEAD")
    assert changed.merge_base == "abc123"
    assert changed.paths == (Path("products/sales-order/sources/product.html"),)
    assert runner.argv[0] == ("git", "merge-base", "HEAD", "origin/main")


def test_commit_allowed_paths_rejects_unexpected_status(tmp_path: Path) -> None:
    runner = RecordingRunner([ok(" M README.md\n")])
    with pytest.raises(GitConflict, match="WRITEBACK_PATH_NOT_ALLOWED"):
        GitCli(tmp_path, runner).commit_allowed_paths("sales-order", "message")
```

- [ ] **Step 2: Run the test and verify missing adapter failure**

Run: `uv run pytest tests/unit/test_git_cli_adapter.py -q`

Expected: FAIL because `GitCli` is unavailable.

- [ ] **Step 3: Define the Git port**

```python
class GitConflict(WorkflowConflict):
    pass


class GitTransientError(WorkflowTransientError):
    pass


@dataclass(frozen=True)
class ChangedPaths:
    merge_base: str
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class CommitResult:
    sha: str
    created: bool


class GitPort(Protocol):
    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths: ...
    def current_sha(self) -> str: ...
    def branch_exists(self, branch: str) -> bool: ...
    def switch_or_create(self, branch: str, base_ref: str) -> None: ...
    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult: ...
    def push(self, branch: str, *, lfs: bool = False) -> None: ...
    def tag_target(self, tag: str) -> str | None: ...
    def create_annotated_tag(self, tag: str, target: str, message: str) -> None: ...
    def push_tags(self, tags: Sequence[str]) -> None: ...
    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...
```

- [ ] **Step 4: Implement `GitCli` with exact command arrays**

Use `git status --porcelain=v1 -z`, parse NUL records without shell tools, validate every path through `RepositoryPaths`, configure the bot identity only before a commit, stage with explicit pathspecs, call `git lfs push origin HEAD` before `git push origin HEAD:<branch>`, and verify `git rev-parse HEAD` after each commit/push. Never invoke `git add -A` without an explicit validated path list.

- [ ] **Step 5: Cover stale refs, no-op commits, LFS ordering, and tag conflicts**

Add tests where the fake runner returns an existing equivalent commit, non-fast-forward push failure, LFS failure, and an immutable tag pointing at another SHA. Assert typed `GitTransientError` versus `GitConflict` classification.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_git_cli_adapter.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/ports/git.py src/ard_ossie/adapters/git_cli.py tests/unit/test_git_cli_adapter.py
git commit -m "feat: add typed Git and LFS adapter"
```

### Task 4: Typed GitHub CLI adapter and redacted mutations

**Files:**
- Create: `src/ard_ossie/ports/github.py`
- Create: `src/ard_ossie/adapters/github_cli.py`
- Test: `tests/unit/test_github_cli_adapter.py`

**Interfaces:**
- Consumes: `CommandRunner`, `MutationRecord`
- Produces: `GitHubPort`, `GitHubCli`, typed repository/PR/release/environment/protection snapshots, and the exact methods listed in Step 3

- [ ] **Step 1: Write failing status and managed-comment tests**

```python
def test_set_status_targets_exact_repository_and_sha() -> None:
    runner = RecordingRunner([ok("{}")])
    client = GitHubCli("kimohy/ard-ossie-provider", runner, token_env="GH_TOKEN")
    mutation = client.set_status("a" * 40, "ard/quality-gate", "success", "passed", "https://run")
    assert mutation.target == f"{'a' * 40}:ard/quality-gate"
    assert "kimohy/ard-ossie-provider" in " ".join(runner.argv[0])


def test_upsert_comment_updates_existing_marker() -> None:
    runner = RecordingRunner([ok('[{"id":7,"body":"<!-- ard:process --> old"}]'), ok("{}")])
    client = GitHubCli("kimohy/ard-ossie-provider", runner)
    client.upsert_pr_comment(3, "ard:process", "new")
    assert "issues/comments/7" in " ".join(runner.argv[1])
```

- [ ] **Step 2: Run tests and verify missing adapter failure**

Run: `uv run pytest tests/unit/test_github_cli_adapter.py -q`

Expected: FAIL because the GitHub port and adapter do not exist.

- [ ] **Step 3: Define only workflow-required GitHub operations**

```python
class GitHubConflict(WorkflowConflict):
    pass


class GitHubPort(Protocol):
    def repository(self) -> RepositoryState: ...
    def collaborator_permission(self, login: str) -> str: ...
    def list_collaborators(self) -> tuple[CollaboratorState, ...]: ...
    def find_open_pr(self, branch: str) -> PullRequestState | None: ...
    def get_pr(self, number: int) -> PullRequestState: ...
    def create_draft_pr(self, branch: str, base: str, title: str, body: str) -> PullRequestState: ...
    def set_issue_labels(self, number: int, *, add: set[str], remove: set[str]) -> list[MutationRecord]: ...
    def upsert_pr_comment(self, number: int, marker: str, body: str) -> MutationRecord: ...
    def set_status(self, sha: str, context: str, state: str, description: str, target_url: str) -> MutationRecord: ...
    def get_status(self, sha: str, context: str) -> str | None: ...
    def dispatch_workflow(self, workflow: str, ref: str, inputs: Mapping[str, str]) -> MutationRecord: ...
    def get_release(self, tag: str) -> ReleaseState | None: ...
    def upsert_release(self, tag: str, title: str, asset: Path, sha256: str) -> MutationRecord: ...
    def repository_dispatch(self, event_type: str, payload: Mapping[str, object]) -> MutationRecord: ...
    def list_labels(self) -> Mapping[str, LabelState]: ...
    def upsert_label(self, name: str, color: str, description: str) -> MutationRecord: ...
    def get_actions_permissions(self) -> ActionsPermissionState: ...
    def set_actions_permissions(self, state: ActionsPermissionState) -> MutationRecord: ...
    def get_environment(self, name: str) -> EnvironmentState | None: ...
    def upsert_environment(self, state: EnvironmentState) -> MutationRecord: ...
    def list_environment_secret_names(self, environment: str) -> frozenset[str]: ...
    def set_environment_secret(self, environment: str, name: str, value: str) -> MutationRecord: ...
    def set_variable(self, name: str, value: str, environment: str | None = None) -> MutationRecord: ...
    def get_branch_protection(self, branch: str) -> BranchProtectionState | None: ...
    def set_branch_protection(self, branch: str, state: BranchProtectionState) -> MutationRecord: ...
```

Define the snapshot types as frozen dataclasses with only fields consumed by the three plans. `PullRequestState` includes number, head branch/SHA, base branch, draft, merged time, and merge SHA. `RepositoryState` includes full name, public/archived flags, default branch, and authenticated permission. `EnvironmentState` includes reviewers, self-review flag, wait timer, and exact branch patterns. Return these types rather than raw JSON dictionaries.

- [ ] **Step 4: Implement `GitHubCli` over `gh api` and high-level `gh` commands**

All calls include the exact repository or REST path. Send JSON bodies through stdin using `gh api --input -`; never interpolate JSON into a shell. Read `GH_TOKEN` only through the child environment. Bound and redact child output with `CommandRunner`. Treat a matching existing resource as no-op and immutable mismatches as `GitHubConflict`.

- [ ] **Step 5: Add secret-channel tests**

Use sentinel `ARD_LLM_API_KEY=sentinel-key`. Assert it occurs only in the `stdin` field of the `gh secret set ARD_LLM_API_KEY --env ard-llm` request and is absent from argv, stdout, stderr, `MutationRecord`, and serialized exceptions.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_github_cli_adapter.py tests/unit/test_subprocess_adapter.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/ports/github.py src/ard_ossie/adapters/github_cli.py tests/unit/test_github_cli_adapter.py
git commit -m "feat: add typed GitHub CLI adapter"
```

### Task 5: Split the Typer entry point without breaking current commands

**Files:**
- Delete: `src/ard_ossie/cli.py`
- Create: `src/ard_ossie/cli/__init__.py`
- Create: `src/ard_ossie/cli/root.py`
- Create: `src/ard_ossie/cli/process.py`
- Create: `src/ard_ossie/cli/registry.py`
- Create: `src/ard_ossie/cli/changeset.py`
- Create: `src/ard_ossie/cli/release.py`
- Create: `src/ard_ossie/cli/history.py`
- Create: `src/ard_ossie/cli/parse.py`
- Create: `src/ard_ossie/cli/model.py`
- Create: `src/ard_ossie/cli/validate.py`
- Create: `src/ard_ossie/cli/github.py`
- Create: `src/ard_ossie/cli/workflow.py`
- Modify: `tests/unit/test_cli.py`
- Test: `tests/unit/test_cli_structure.py`

**Interfaces:**
- Consumes: existing CLI callables plus Tasks 1–4 interfaces
- Produces: unchanged `ard_ossie.cli:app`, registered command groups, empty-but-real `github` and `workflow` groups for later plans

- [ ] **Step 1: Add failing help and import tests**

```python
def test_console_entrypoint_exports_root_app() -> None:
    from ard_ossie.cli import app
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("registry", "impact", "changeset", "release", "parse", "model", "validate", "github", "workflow"):
        assert group in result.stdout


def test_existing_process_command_remains_compatible() -> None:
    result = CliRunner().invoke(app, ["process", "--help"])
    assert result.exit_code == 0
    assert "--warnings-as-errors" in result.stdout
```

- [ ] **Step 2: Run CLI tests and observe missing groups**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_cli_structure.py -q`

Expected: FAIL because the current single file does not register the new groups.

- [ ] **Step 3: Replace the module with a package and register focused groups**

```python
# src/ard_ossie/cli/__init__.py
from ard_ossie.cli.root import app

__all__ = ["app"]


# src/ard_ossie/cli/root.py
app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(registry.app, name="registry")
app.add_typer(impact.app, name="impact")
app.add_typer(changeset.app, name="changeset")
app.add_typer(release.app, name="release")
app.add_typer(parse.app, name="parse")
app.add_typer(model.app, name="model")
app.add_typer(validate.app, name="validate")
app.add_typer(github.app, name="github")
app.add_typer(workflow.app, name="workflow")
```

Move existing implementations without behavior changes. Keep `process`, `history`, `show`, and `diff` registered at the root. Do not import adapters at module import time; dependency construction occurs inside commands.

- [ ] **Step 4: Run all existing CLI and wheel tests**

Run: `uv run pytest tests/unit/test_cli.py tests/integration/test_cli_process.py tests/integration/test_wheel_assets.py -q`

Expected: PASS and the wheel still exposes the `ard` entry point.

- [ ] **Step 5: Run Ruff and commit**

Run: `uv run ruff check src/ard_ossie/cli tests/unit/test_cli_structure.py`

Expected: PASS.

```bash
git add src/ard_ossie/cli.py src/ard_ossie/cli tests/unit/test_cli.py tests/unit/test_cli_structure.py
git commit -m "refactor: split ARD CLI command groups"
```

### Task 6: Granular parse, model, and validate commands

**Files:**
- Create: `src/ard_ossie/application/parsing.py`
- Create: `src/ard_ossie/application/modeling.py`
- Modify: `src/ard_ossie/cli/parse.py`
- Modify: `src/ard_ossie/cli/model.py`
- Modify: `src/ard_ossie/cli/validate.py`
- Test: `tests/integration/test_granular_cli.py`
- Test: `tests/unit/test_parsing_service.py`

**Interfaces:**
- Consumes: `DoclingParser`, `parse_dictionary`, `process_product`, `RepositoryPaths`, `ResultWriter`
- Produces: `ParsingService.parse_product_html()`, `parse_semantic_document()`, `parse_dictionary_workbook()`, and CLI commands that write deterministic JSON/Markdown outputs

- [ ] **Step 1: Add failing granular CLI tests**

```python
def test_parse_dictionary_command_writes_structured_json(dictionary_xlsx: Path, tmp_path: Path) -> None:
    output = tmp_path / "dictionary.json"
    result = runner.invoke(app, ["parse", "dictionary", str(dictionary_xlsx), "--output", str(output)])
    assert result.exit_code == 0
    assert json.loads(output.read_text())["tables"][0]["name"] == "orders"


def test_validate_product_is_read_only(product_fixture: Path, tmp_path: Path) -> None:
    before = snapshot_tree(product_fixture)
    result = runner.invoke(app, ["validate", "product", str(product_fixture), "--registry", str(tmp_path / "registry")])
    assert result.exit_code in (0, 10)
    assert snapshot_tree(product_fixture) == before
```

- [ ] **Step 2: Run tests and verify commands are missing**

Run: `uv run pytest tests/integration/test_granular_cli.py -q`

Expected: FAIL with unknown `parse dictionary` and `validate product` commands.

- [ ] **Step 3: Implement parsing application service**

Return Pydantic result models containing source hash, parser kind, evidence, and parsed content. The product and semantic commands render Docling Markdown plus evidence JSON; dictionary renders the existing `ParsedDictionary` JSON. Every output uses a temporary file and atomic replace.

- [ ] **Step 4: Implement CLI commands and read-only model validation**

`ard model build <product-root> --registry <path> --no-llm` copies the product and Registry into a `TemporaryDirectory` below `.ard/staging`, calls the existing deterministic pipeline with `provider=None` against those copies, and copies only generated outputs to the explicit `--staging-output`. `ard validate product` uses the same isolated copy for source, schema, identity, version, and reference checks, then deletes staging. Neither command touches the source product or real Registry. Validation returns `0` on pass and `10` on findings.

- [ ] **Step 5: Add reproducibility and path-policy cases**

Run each command twice and assert byte-identical outputs. Add traversal and symlink inputs and assert exit code `50` with a result envelope that contains `PATH_OUTSIDE_REPOSITORY` or `SYMLINK_NOT_ALLOWED` but no absolute secret path.

- [ ] **Step 6: Run phase verification and commit**

Run:

```bash
uv run pytest tests/unit/test_application_contracts.py tests/unit/test_application_output.py tests/unit/test_subprocess_adapter.py tests/unit/test_filesystem_adapter.py tests/unit/test_git_cli_adapter.py tests/unit/test_github_cli_adapter.py tests/unit/test_cli.py tests/unit/test_cli_structure.py tests/unit/test_parsing_service.py tests/integration/test_granular_cli.py tests/integration/test_cli_process.py -q
uv run ruff check src tests
```

Expected: all selected tests and Ruff PASS.

```bash
git add src/ard_ossie/application src/ard_ossie/cli tests/integration/test_granular_cli.py tests/unit/test_parsing_service.py
git commit -m "feat: expose granular ARD CLI modules"
```

## Phase completion gate

- [ ] `uv run pytest -q` passes.
- [ ] `uv run ruff check src tests` passes.
- [ ] `uv build` includes `ard_ossie/cli`, `application`, `ports`, and `adapters`.
- [ ] Existing GitHub workflow files are byte-unchanged in this phase.
- [ ] `git status --short` is clean.
- [ ] Proceed to `2026-08-08-cli-first-02-processing-workflows.md` only after review.
