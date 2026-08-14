# LLM-backed Source Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make trusted source validation run the configured LLM OCR correction and semantic structure repair without exposing credentials to candidate code.

**Architecture:** Resolve the existing repository LLM profile in the trusted workflow CLI, inject the provider through `SourceCheckService` and `ModelingService.validate()`, and keep all normal semantic publication gates strict. Attach both production source-check jobs to the protected `ard-llm` environment while retaining read-only, credential-free candidate checkouts.

**Tech Stack:** Python 3.12, Typer, Pydantic, pytest, GitHub Actions YAML, existing ARD multi-provider LLM abstraction.

## Global Constraints

- Candidate branch code must never execute with LLM or GitHub write credentials.
- Source-check must use trusted default-branch code and treat the candidate checkout as data only.
- `SEMANTIC_VISUAL_CORRECTION_FAILED`, `SEMANTIC_STRUCTURE_DEGRADED`, and `SEMANTIC_FIDELITY_FAILED` remain hard gates.
- Use the existing `provider_from_environment()` and profile variables; do not add a second provider factory.
- Do not run the full test suite. Run only the named regressions, Ruff on changed Python files, and `git diff --check`.
- The existing processing job remains the only content writer.

## File Structure

- `src/ard_ossie/application/source_check.py`: owns source-check orchestration and provider injection into model validation.
- `src/ard_ossie/application/modeling.py`: transports the injected provider into `process_product()`.
- `src/ard_ossie/cli/workflow.py`: resolves the provider in trusted CLI code and enforces `--require-llm` for protected workflows.
- `.github/workflows/ard-process.yml`: protects Issue-driven source validation with `ard-llm`.
- `.github/workflows/ard-direct-change.yml`: applies the same protected validation contract to direct source changes.
- `tests/unit/test_source_check_service.py`: proves provider transport and strict semantic gating.
- `tests/integration/test_workflow_direct_cli.py`: proves required-LLM configuration fails with a structured configuration result.
- `tests/integration/test_workflow_contracts.py`: proves secrets stay in trusted jobs and candidate checkouts remain credential-free.
- `tests/unit/test_workflow_secret_contract.py`: limits secret references to the two protected jobs in the reusable processor.

---

### Task 1: Inject the provider through trusted source validation

**Files:**
- Modify: `tests/unit/test_source_check_service.py`
- Modify: `src/ard_ossie/application/source_check.py`
- Modify: `src/ard_ossie/application/modeling.py`

**Interfaces:**
- Consumes: `LLMProvider | None` from `ard_ossie.llm.contracts`.
- Produces: `SourceCheckService(paths, provider=...)` and `ModelingService.validate(..., provider=...)`.

- [ ] **Step 1: Replace the obsolete deferral regression with a failing strict-provider regression**

```python
def test_source_check_injects_provider_and_keeps_semantic_gates_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    provider = object()
    captured: dict[str, object] = {}
    fidelity = pass_fidelity_report().model_copy(
        update={
            "extraction_mode": ExtractionMode.OCR,
            "status": "WARN",
            "warning_codes": ["SEMANTIC_OCR_CORRECTION_UNAVAILABLE"],
        }
    )

    def parser_factory(**kwargs):
        captured.update(kwargs)
        return FidelityParser(fidelity)

    monkeypatch.setattr(pipeline_module, "_processing_parser", parser_factory)

    with pytest.raises(
        WorkflowValidationError,
        match="SEMANTIC_VISUAL_CORRECTION_FAILED",
    ):
        SourceCheckService(
            RepositoryPaths(tmp_path),
            provider=provider,
        ).run("sales-order", SHA)

    assert captured["provider"] is provider
```

Delete the old assertion that source-check succeeds when visual correction is unavailable. Remove the old environment-secret rejection portion from `test_source_check_is_read_only_and_secret_free`; the application service will receive a provider explicitly and will not inspect `os.environ`.

- [ ] **Step 2: Run the regression to verify RED**

Run:

```bash
.venv/bin/pytest -q tests/unit/test_source_check_service.py::test_source_check_injects_provider_and_keeps_semantic_gates_strict
```

Expected: FAIL because `SourceCheckService.__init__()` does not accept `provider`.

- [ ] **Step 3: Add the minimal provider transport**

In `src/ard_ossie/application/source_check.py`:

```python
from ard_ossie.llm.contracts import LLMProvider


class SourceCheckService:
    def __init__(
        self,
        paths: FileSystemPort,
        *,
        provider: LLMProvider | None = None,
    ) -> None:
        self.paths = paths
        self.provider = provider

    # ...
    validation = ModelingService(self.paths).validate(
        product,
        "registry",
        provider=self.provider,
    )
```

Do not pass `require_semantic_visual_correction=False`.

In `src/ard_ossie/application/modeling.py`:

```python
from ard_ossie.llm.contracts import LLMProvider


def validate(
    self,
    product_path: str | Path,
    registry_path: str | Path,
    *,
    provider: LLMProvider | None = None,
    require_semantic_visual_correction: bool = True,
) -> ValidationResult:
    # ...
    processed = process_product(
        staged_product,
        registry_root=staged_registry,
        provider=provider,
        require_semantic_visual_correction=require_semantic_visual_correction,
    )
```

- [ ] **Step 4: Run the focused source-check regressions**

Run:

```bash
.venv/bin/pytest -q \
  tests/unit/test_source_check_service.py::test_source_check_is_read_only_and_secret_free \
  tests/unit/test_source_check_service.py::test_source_check_injects_provider_and_keeps_semantic_gates_strict
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the provider boundary**

```bash
git add src/ard_ossie/application/source_check.py \
  src/ard_ossie/application/modeling.py \
  tests/unit/test_source_check_service.py
git commit -m "fix: run strict source validation with LLM provider"
```

---

### Task 2: Protect the workflow and require its LLM profile

**Files:**
- Modify: `tests/integration/test_workflow_direct_cli.py`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `tests/unit/test_workflow_secret_contract.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `.github/workflows/ard-process.yml`
- Modify: `.github/workflows/ard-direct-change.yml`

**Interfaces:**
- Consumes: `provider_from_environment()` and `WorkflowConfigurationError`.
- Produces: `workflow source-check --require-llm` and protected `validate` jobs.

- [ ] **Step 1: Add a failing required-provider CLI regression**

```python
def test_source_check_cli_requires_configured_llm_when_requested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ARD_LLM_PROFILE", raising=False)

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
            str(tmp_path),
            "--require-llm",
        ],
    )

    assert result.exit_code == 20, result.output
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.source-check-result.json").read_text()
    )
    assert envelope["findings"][0]["code"] == "LLM_PROFILE_REQUIRED"
```

- [ ] **Step 2: Tighten the existing workflow contract assertions**

For both `ard-process.yml` and `ard-direct-change.yml`, assert:

```python
assert validation["environment"] == "ard-llm"
assert validation["permissions"] == {"contents": "read"}
assert validation["env"]["ARD_LLM_PROFILE"] == "${{ vars.ARD_LLM_PROFILE }}"
assert validation["env"]["ARD_LLM_API_KEY"] == "${{ secrets.ARD_LLM_API_KEY }}"
assert validation["env"]["ARD_LLM_BASE_URL"] == (
    "${{ secrets.ARD_LLM_BASE_URL || vars.ARD_LLM_BASE_URL || 'https://api.openai.com/v1' }}"
)
assert "--require-llm" in validation_run["run"]
assert validation_checkouts[1]["with"]["persist-credentials"] == "false"
```

Also assert the Azure and Vertex environment names match the existing process-job contract. Remove assertions that the validation job has no environment or contains no LLM secret expression. Keep assertions that routing, base sync, signal, and candidate checkout steps have no LLM secrets.

Update `TestWorkflowSecretContract.test_only_protected_processor_job_references_secrets` to loop over `("validate", "process")`, verify both jobs use `ard-llm` with the three secret-bearing provider variables, rename it to `test_only_protected_validation_and_processor_jobs_reference_secrets`, and assert that no other reusable processor job contains a secret reference.

- [ ] **Step 3: Run both contract targets to verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/integration/test_workflow_direct_cli.py::test_source_check_cli_requires_configured_llm_when_requested \
  tests/integration/test_workflow_contracts.py::test_reusable_processor_has_writeback_quality_and_secret_contracts \
  tests/integration/test_workflow_contracts.py::test_direct_change_uses_read_only_signal_and_default_branch_coordinator \
  tests/unit/test_workflow_secret_contract.py::TestWorkflowSecretContract::test_only_protected_processor_job_references_secrets
```

Expected: FAIL because the option and protected validation environment do not exist, and the old secret contract permits only `process`.

- [ ] **Step 4: Add explicit trusted CLI provider resolution**

In `src/ard_ossie/cli/workflow.py`, add `require_llm` to the command and resolve the provider inside the `_publish()` callback:

```python
@app.command("source-check")
def source_check(
    product_key: Annotated[str, typer.Option("--product-key")],
    expected_head: Annotated[str, typer.Option("--expected-head")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    require_llm: Annotated[bool, typer.Option("--require-llm")] = False,
) -> None:
    command = "workflow.source-check"
    paths = _repository_paths(repository)
    _publish(
        paths.root,
        command,
        lambda: _source_check_service(paths, require_llm=require_llm).run(
            product_key,
            expected_head,
        ),
    )
```

Use a helper that converts provider configuration failures into a workflow configuration result:

```python
def _source_check_service(paths, *, require_llm: bool = False):
    try:
        provider = provider_from_environment()
    except ProviderExecutionError as error:
        raise WorkflowConfigurationError(
            error.code,
            "source-check LLM provider configuration failed",
        ) from error
    if require_llm and provider is None:
        raise WorkflowConfigurationError(
            "LLM_PROFILE_REQUIRED",
            "source-check requires ARD_LLM_PROFILE",
        )
    return SourceCheckService(paths, provider=provider)
```

Import `WorkflowConfigurationError` and `ProviderExecutionError` from their existing modules. Do not read or log individual secret values.

- [ ] **Step 5: Protect both validation jobs**

In both workflow files:

- Set `environment: ard-llm` on `jobs.validate`.
- Copy only the existing supported provider variables and secrets from the reusable `process` job into `jobs.validate.env`.
- Keep validation permissions at `contents: read`.
- Keep both candidate checkouts at `persist-credentials: false`.
- Rename the step to `Revalidate sources with trusted code and protected LLM`.
- Append `--require-llm` to the trusted CLI command.

- [ ] **Step 6: Run the three focused contract tests**

Run the exact command from Step 3.

Use the renamed secret-contract test target in place of its old name. Expected: `4 passed`.

- [ ] **Step 7: Commit the protected workflow contract**

```bash
git add src/ard_ossie/cli/workflow.py \
  .github/workflows/ard-process.yml \
  .github/workflows/ard-direct-change.yml \
  tests/integration/test_workflow_direct_cli.py \
  tests/integration/test_workflow_contracts.py \
  tests/unit/test_workflow_secret_contract.py
git commit -m "fix: protect LLM-backed source checks"
```

---

### Task 3: Verify, publish, and rerun Issue #3

**Files:**
- Verify only: all files changed in Tasks 1 and 2.
- GitHub: create a new fix PR; do not reuse merged PR `#25`.

**Interfaces:**
- Consumes: focused green commits from Tasks 1 and 2.
- Produces: reviewed main-branch fix and a new Issue `#3` workflow run.

- [ ] **Step 1: Run the combined focused regression set once**

```bash
.venv/bin/pytest -q \
  tests/unit/test_source_check_service.py::test_source_check_is_read_only_and_secret_free \
  tests/unit/test_source_check_service.py::test_source_check_injects_provider_and_keeps_semantic_gates_strict \
  tests/integration/test_workflow_direct_cli.py::test_source_check_cli_requires_configured_llm_when_requested \
  tests/integration/test_workflow_contracts.py::test_reusable_processor_has_writeback_quality_and_secret_contracts \
  tests/integration/test_workflow_contracts.py::test_direct_change_uses_read_only_signal_and_default_branch_coordinator \
  tests/unit/test_workflow_secret_contract.py::TestWorkflowSecretContract::test_only_protected_validation_and_processor_jobs_reference_secrets
```

Expected: `6 passed`.

- [ ] **Step 2: Run narrow static verification**

```bash
.venv/bin/ruff check \
  src/ard_ossie/application/source_check.py \
  src/ard_ossie/application/modeling.py \
  src/ard_ossie/cli/workflow.py \
  tests/unit/test_source_check_service.py \
  tests/integration/test_workflow_direct_cli.py \
  tests/integration/test_workflow_contracts.py \
  tests/unit/test_workflow_secret_contract.py
git diff --check HEAD~2..HEAD
git status --short
```

Expected: Ruff succeeds, diff check is empty, and the worktree is clean.

- [ ] **Step 3: Review the exact branch diff**

Confirm that:

- only trusted code constructs the provider;
- candidate checkout steps still use `persist-credentials: false`;
- validation permissions remain read-only;
- no command or log prints a secret;
- source-check no longer passes `require_semantic_visual_correction=False`.

- [ ] **Step 4: Publish and open the fix PR**

Push the branch through the available authenticated GitHub path and create a draft PR against `main`. Wait for the repository's exact pytest and static checks, then perform a focused code review before merging.

- [ ] **Step 5: Rerun Issue #3 once**

After merge, remove and re-add only `ard:approved` on Issue `#3`. Inspect the new `process / validate` log. It must show the protected `ard-llm` environment path and must not end with `SEMANTIC_STRUCTURE_DEGRADED` caused by `provider_unavailable`.

- [ ] **Step 6: Validate generated semantic evidence**

If processing succeeds, inspect PR `#5` and confirm:

- `data-semantic.md` contains no raw HTML tags or HTML character entities;
- the expected Korean semantic terms are preserved;
- `semantic-fidelity.json` has complete page correction audits and no degraded blocks;
- `quality-report.json` has no semantic hard errors.

If a new content or provider error occurs, stop after recording its exact code and job log; do not trigger another run or broaden the fix without a new root-cause analysis.
