# Candidate Schema Verification Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Allow an existing Pydantic model and its checked-in JSON Schema to change atomically in one pull request without executing candidate code in the static trusted job.

**Architecture:** Move the immutable model-schema catalog into a trusted helper module that contains only schema paths and import strings. The static verifier will parse Draft 2020-12 schemas and enforce the trusted path catalog without importing candidate models; a new credential-free `model-schemas` verifier will launch the trusted helper by absolute path inside the candidate project's frozen `uv` environment and compare the candidate classes with the candidate schemas.

**Tech Stack:** Python 3.12, Pydantic v2, jsonschema Draft 2020-12, Typer, uv, pytest, Ruff, GitHub Actions.

## Global Constraints

- Static verification must not execute or import candidate Python, build hooks, tests, actions, or shell scripts.
- Candidate execution must use `contents: read`, no Environment, no write token, and no repository or provider secret.
- The model-schema catalog remains trusted-code-owned and contains exactly the existing eight non-Ossie schema entries.
- Exact-head and clean-tree checks must continue to bracket every executable verifier.
- Required status names remain `ard/changeset` and `ard/quality-gate`.
- This bootstrap changes no Pydantic model and no checked-in model schema.
- Candidate helper failures expose only stable error codes and catalog schema paths.
- A successful child process is accepted only with a fresh nonce-bound receipt covering all eight catalog paths.
- The trusted helper digest must remain unchanged across candidate execution.
- Intentionally malicious candidate runtime semantics remain a code-review responsibility; automated checks fail closed on accidental early exit, unexpected exceptions, noisy output, missing receipts, and helper mutation.

---

### Task 1: Split Static Schema Catalog Validation from Model Synchronization

**Files:**
- Create: `src/ard_ossie/application/model_schema_verification.py`
- Modify: `src/ard_ossie/application/repository_checks.py:19-75,243-278`
- Modify: `tests/unit/test_repository_check_service.py:327-340`

**Interfaces:**
- Produces: `ModelSchemaReference(schema_path: Path, module_name: str, class_name: str)`.
- Produces: `MODEL_SCHEMA_CATALOG: tuple[ModelSchemaReference, ...]` with the existing eight trusted entries.
- Consumes: `MODEL_SCHEMA_CATALOG` in `RepositoryVerificationTools._run_schemas()` to derive the exact non-Ossie path set.
- Preserves: malformed JSON/Draft schemas fail `SCHEMA_SYNCHRONIZATION_FAILED`; missing or unexpected catalog paths fail `SCHEMA_CATALOG_MISMATCH`.

- [x] **Step 1: Write the failing static-boundary test**

Add this behavior test to `tests/unit/test_repository_check_service.py`. The production change that makes it pass is removing live Pydantic model comparison from the static verifier while retaining the trusted path catalog.

```python
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
```

- [x] **Step 2: Add preserved fail-closed catalog and syntax tests**

Add literal fixtures proving the refactor does not weaken static validation:

```python
def test_static_schema_verifier_rejects_untrusted_catalog_entry(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "schemas"
    shutil.copytree(source, tmp_path / "schemas")
    (tmp_path / "schemas" / "unexpected.schema.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}),
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
```

- [x] **Step 3: Run the focused tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_accepts_valid_candidate_model_schema_change \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_rejects_untrusted_catalog_entry \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_rejects_missing_catalog_entry \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_rejects_malformed_schema -q
```

Expected: the candidate-change test fails with `SCHEMA_SYNCHRONIZATION_FAILED` because trusted live classes are still compared; the three preserved fail-closed tests pass.

- [x] **Step 4: Add the immutable trusted catalog and simplify static verification**

Create the catalog module without top-level imports of candidate model modules:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelSchemaReference:
    schema_path: Path
    module_name: str
    class_name: str


MODEL_SCHEMA_CATALOG = (
    ModelSchemaReference(Path("candidate-change.schema.json"), "ard_ossie.models", "CandidateChange"),
    ModelSchemaReference(Path("changeset.schema.json"), "ard_ossie.impact", "ChangeSetRecord"),
    ModelSchemaReference(Path("ir/product-ir.schema.json"), "ard_ossie.ir", "ProductIR"),
    ModelSchemaReference(Path("reports/duplicate-report.schema.json"), "ard_ossie.identity", "DuplicateReport"),
    ModelSchemaReference(Path("reports/impact-report.schema.json"), "ard_ossie.impact", "ImpactReport"),
    ModelSchemaReference(Path("reports/quality-report.schema.json"), "ard_ossie.pipeline", "QualityReport"),
    ModelSchemaReference(Path("reports/version-report.schema.json"), "ard_ossie.versioning", "VersionDecision"),
    ModelSchemaReference(Path("source-manifest.schema.json"), "ard_ossie.ingestion", "SourceManifest"),
)
```

In `repository_checks.py`, remove the eight live model imports and `_MODEL_SCHEMAS`. Import `MODEL_SCHEMA_CATALOG`, derive `expected_paths = {entry.schema_path for entry in MODEL_SCHEMA_CATALOG}`, and remove the `model_json_schema()` comparison loop.

- [x] **Step 5: Run the focused tests and verify GREEN**

Run the command from Step 3. Expected: `4 passed`.

- [x] **Step 6: Commit Task 1**

```bash
git add src/ard_ossie/application/model_schema_verification.py \
  src/ard_ossie/application/repository_checks.py \
  tests/unit/test_repository_check_service.py
git commit -m "refactor: separate static schema catalog validation"
```

---

### Task 2: Implement the Trusted Candidate Model-Schema Helper

**Files:**
- Modify: `src/ard_ossie/application/model_schema_verification.py`
- Create: `tests/unit/test_model_schema_verification.py`

**Interfaces:**
- Produces: `ModelSchemaVerificationError(code: str, schema_path: Path)` with bounded `CODE:path` text.
- Produces: `verify_model_schemas(repository: Path) -> None` using the trusted catalog and dynamically importing candidate modules.
- Produces: `main(argv: Sequence[str] | None = None) -> int` for the trusted absolute-path subprocess.
- Produces: optional `--result` plus `--nonce` completion receipt containing status, nonce, and every trusted catalog path.
- Requires: supplied repository resolves to the current working directory and contains `schemas/`.

- [x] **Step 1: Write helper behavior tests**

Create `tests/unit/test_model_schema_verification.py` with real current models and copied schemas:

```python
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ard_ossie.application.model_schema_verification import (
    MODEL_SCHEMA_CATALOG,
    ModelSchemaReference,
    ModelSchemaVerificationError,
    main,
    verify_model_schemas,
)


def test_model_schema_helper_accepts_current_candidate_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parents[2]
    monkeypatch.chdir(repository)
    verify_model_schemas(repository)


def test_model_schema_helper_rejects_stale_candidate_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    stale = tmp_path / "schemas" / "candidate-change.schema.json"
    schema = json.loads(stale.read_text(encoding="utf-8"))
    schema["required"].remove("operation")
    stale.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"SCHEMA_SYNCHRONIZATION_FAILED:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_rejects_repository_other_than_working_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ModelSchemaVerificationError,
        match="MODEL_SCHEMA_REPOSITORY_INVALID",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_maps_import_failure_to_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ard_ossie.application.model_schema_verification.MODEL_SCHEMA_CATALOG",
        (
            ModelSchemaReference(
                MODEL_SCHEMA_CATALOG[0].schema_path,
                "candidate_module_that_does_not_exist",
                "CandidateChange",
            ),
        ),
    )

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"MODEL_SCHEMA_IMPORT_FAILED:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_rejects_non_strict_model_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ard_ossie.application.model_schema_verification.MODEL_SCHEMA_CATALOG",
        (
            ModelSchemaReference(
                MODEL_SCHEMA_CATALOG[0].schema_path,
                "json",
                "JSONDecoder",
            ),
        ),
    )

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"MODEL_SCHEMA_TYPE_INVALID:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_cli_emits_only_bounded_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--repository", str(tmp_path)]) == 10
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MODEL_SCHEMA_REPOSITORY_INVALID:.\n"
```

- [x] **Step 2: Run helper tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/unit/test_model_schema_verification.py -q
```

Expected: collection fails because `ModelSchemaVerificationError` and `verify_model_schemas` do not exist.

- [x] **Step 3: Implement dynamic candidate verification and bounded CLI errors**

Extend `model_schema_verification.py` so `verify_model_schemas()`:

1. resolves the repository with `strict=True` and requires equality with `Path.cwd().resolve()`;
2. parses each catalog schema from `<repository>/schemas/<schema_path>`;
3. imports `module_name` with `importlib.import_module()` and resolves `class_name`;
4. imports `StrictModel` from the candidate `ard_ossie.models` environment and requires a class/subclass;
5. compares parsed JSON exactly with `model_json_schema()`;
6. catches root/JSON/import/type/schema-generation/mismatch failures and maps them to `MODEL_SCHEMA_REPOSITORY_INVALID`, `MODEL_SCHEMA_SCHEMA_INVALID`, `MODEL_SCHEMA_IMPORT_FAILED`, `MODEL_SCHEMA_TYPE_INVALID`, or `SCHEMA_SYNCHRONIZATION_FAILED`, including only the catalog path.

The CLI entrypoint must use `argparse` with required `--repository`, print only the bounded error to stderr, and return `10` on validation failure or `0` on success:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 4: Run helper and static tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/unit/test_model_schema_verification.py \
  tests/unit/test_repository_check_service.py -q
```

Expected: all selected tests pass with no warnings.

- [x] **Step 5: Commit Task 2**

```bash
git add src/ard_ossie/application/model_schema_verification.py \
  tests/unit/test_model_schema_verification.py
git commit -m "feat: verify candidate model schema synchronization"
```

---

### Task 3: Add the Credential-Free `model-schemas` Verification Group

**Files:**
- Modify: `src/ard_ossie/application/repository_checks.py:38-51,78-83,197-216,280-326`
- Modify: `tests/unit/test_repository_check_service.py:150-165,281-325`

**Interfaces:**
- Extends: `RepositoryCheckRequest.verification_group` with literal `"model-schemas"`.
- Extends: `_VERIFIERS` and `_VERIFIER_GROUPS` so `"model-schemas"` runs exactly one verifier.
- Produces: `RepositoryVerificationTools._run_model_schemas() -> None`.
- Invokes: `uv run --frozen python -I <absolute-trusted-helper> --repository <absolute-candidate-root> --result <fresh-path> --nonce <random-64-hex>` through `_command(..., credential_free=True, include_failure_evidence=False)`.
- Requires: unchanged helper SHA-256 and an exact nonce/full-catalog receipt after child exit 0.

- [x] **Step 1: Extend group and credential-isolation tests first**

Change the executable group parametrization to:

```python
@pytest.mark.parametrize("verification_group", ["model-schemas", "pytest", "wheel"])
```

Change the credential-free verifier parametrization to the same three values, and add this absolute-helper contract test:

```python
def test_model_schema_verifier_invokes_absolute_trusted_helper(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self) -> None:
            self.requests = []

        def run(self, request):
            self.requests.append(request)
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
```

- [x] **Step 2: Run focused group tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/unit/test_repository_check_service.py::test_repository_executable_group_runs_one_isolated_verifier \
  tests/unit/test_repository_check_service.py::test_candidate_executable_verifiers_receive_credential_free_environment \
  tests/unit/test_repository_check_service.py::test_model_schema_verifier_invokes_absolute_trusted_helper -q
```

Expected: `model-schemas` is rejected as an unknown request/verifier and the helper invocation test fails.

- [x] **Step 3: Implement the new verifier group minimally**

Add `"model-schemas"` to the request literal, verifier registry, and one-verifier group. Implement `_run_model_schemas()` using the absolute trusted helper path and `_command(..., timeout=600, credential_free=True, include_failure_evidence=False)` with the exact argv from the interface above.

Add `ard_ossie/application/model_schema_verification.py` to the wheel-required source set in `_run_wheel()` so a merged trusted checkout cannot omit the helper from the distribution.

Update the fake wheel archive in `test_candidate_executable_verifiers_receive_credential_free_environment()` to include `ard_ossie/application/model_schema_verification.py`; otherwise the wheel verifier fixture would no longer mirror the complete required distribution structure.

Add review regressions proving noisy `RuntimeError` and `SystemExit(0)` imports produce only stable helper errors, a full-catalog receipt is written only after success, a zero exit without a receipt is rejected, trusted helper mutation raises `TRUSTED_MODEL_SCHEMA_HELPER_CHANGED`, and timeout evidence is replaced while transient retryability is preserved.

- [x] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected parametrized cases pass.

- [x] **Step 5: Commit Task 3**

```bash
git add src/ard_ossie/application/repository_checks.py \
  tests/unit/test_repository_check_service.py
git commit -m "feat: add isolated model schema verifier group"
```

---

### Task 4: Add `model-schemas` to the GitHub Actions Executable Matrix

**Files:**
- Modify: `.github/workflows/ard-repository-change.yml:59-107`
- Modify: `tests/integration/test_workflow_contracts.py:294-367`
- Modify: `tests/integration/test_workflow_repository_check_cli.py:26-64`

**Interfaces:**
- Workflow matrix is exactly `[model-schemas, pytest, wheel]`.
- The executable job retains `permissions: {contents: read}`, contains no `GH_TOKEN`, checks out trusted and exact-head candidate trees separately, and invokes trusted `ard workflow repository-check` from `working-directory: trusted`.
- Finalizer still requires successful `check` and the complete `executable` matrix before publishing existing required statuses.

- [x] **Step 1: Write the failing workflow contract**

Change the matrix assertion in `test_code_only_pull_requests_publish_the_same_required_statuses()` to:

```python
assert executable["strategy"]["matrix"]["verification_group"] == [
    "model-schemas",
    "pytest",
    "wheel",
]
```

Extend `test_workflow_repository_check_maps_exact_head()` to invoke `--verification-group model-schemas` and assert the service request preserves that exact value.

- [x] **Step 2: Run workflow contracts and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/integration/test_workflow_contracts.py::test_code_only_pull_requests_publish_the_same_required_statuses \
  tests/integration/test_workflow_repository_check_cli.py -q
```

Expected: the workflow matrix assertion fails because it still contains only `pytest` and `wheel`; the CLI test passes after Task 3's request literal change.

- [x] **Step 3: Extend the executable matrix only**

Change `.github/workflows/ard-repository-change.yml` line 65 to:

```yaml
verification_group: [model-schemas, pytest, wheel]
```

Do not change permissions, tokens, checkout refs, working directories, finalizer conditions, or status names.

- [x] **Step 4: Run workflow contracts and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [x] **Step 5: Commit Task 4**

```bash
git add .github/workflows/ard-repository-change.yml \
  tests/integration/test_workflow_contracts.py \
  tests/integration/test_workflow_repository_check_cli.py
git commit -m "ci: verify candidate model schemas in isolation"
```

---

### Task 5: Validate, Review, and Prepare the Bootstrap Pull Request

**Files:**
- Modify: `docs/superpowers/plans/2026-08-12-candidate-schema-verification-bootstrap.md`
- Verify: all files changed since `origin/main`

**Interfaces:**
- Produces: a clean, reviewable bootstrap branch containing no Pydantic model or checked-in schema changes.
- Produces: exact verification evidence for pytest, Ruff, seven workflow YAML files, repository schema synchronization, and sdist/wheel builds.

- [x] **Step 1: Run the focused security and synchronization suite**

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest \
  tests/unit/test_model_schema_verification.py \
  tests/unit/test_repository_check_service.py \
  tests/integration/test_workflow_repository_check_cli.py \
  tests/integration/test_workflow_contracts.py -q
```

Expected: all focused tests pass.

- [x] **Step 2: Run the complete Python test suite**

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run pytest -q
```

Expected: at least 405 tests pass and zero fail.

- [x] **Step 3: Run static and workflow parsing gates independently**

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run ruff check src tests
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run python - <<'PY'
from pathlib import Path
import yaml

paths = sorted(Path(".github/workflows").glob("*.yml"))
for path in paths:
    yaml.safe_load(path.read_text(encoding="utf-8"))
print(f"parsed {len(paths)} workflows")
PY
```

Expected: Ruff reports `All checks passed!` and Python reports `parsed 7 workflows`.

- [x] **Step 4: Run the candidate helper and distribution builds**

```bash
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv run python -I \
  "$(pwd)/src/ard_ossie/application/model_schema_verification.py" \
  --repository "$(pwd)"
UV_CACHE_DIR=/tmp/ard-ossie-bootstrap-uv-cache uv build --sdist --wheel
```

Expected: helper exit 0 and both sdist and wheel artifacts are produced.

- [x] **Step 5: Review scope and security invariants**

```bash
git diff --check origin/main...HEAD
git diff --name-status origin/main...HEAD
git status --short
git diff origin/main...HEAD -- schemas src/ard_ossie/ir.py src/ard_ossie/models.py
```

Expected: no whitespace errors; only design/plan, verifier/helper, tests, and one workflow change; the final command has no output; worktree is clean after commits.

- [x] **Step 6: Record final verification and commit the plan status**

Mark every checkbox complete and append exact test/build results under `## Execution Record`, then commit only the plan update:

```bash
git add docs/superpowers/plans/2026-08-12-candidate-schema-verification-bootstrap.md
git commit -m "docs: record bootstrap verification"
```

- [x] **Step 7: Request an independent code review**

Review `origin/main...HEAD` against the approved design. Resolve every Critical or Important finding with a new RED/GREEN cycle, then rerun Steps 1-5.

- [ ] **Step 8: Publish and integrate**

Push the named branch, open a Draft PR against the current default branch, confirm the remote head/tree and changed-file set, wait for `ard/changeset` and `ard/quality-gate` on the exact head, mark Ready, and merge only after all required checks and review are green.

- [ ] **Step 9: Resume PR #11 rollout**

Update PR #11 onto the merged bootstrap `main`, require a fresh exact-head run with the new `model-schemas` matrix, re-review the final diff, merge when green, then reprocess Issue #3 and verify its regenerated `data-product.md` and `generated/ossie-model.json`.

## Execution Record

- Baseline at `origin/main@aa45977`: `405 passed`.
- TDD RED/GREEN cycles observed for the static/live-model split, trusted helper API, isolated verification group, workflow matrix, noisy candidate exceptions, `SystemExit(0)`, completion receipt, helper mutation, and timeout evidence suppression.
- Code verification head: `9db4fcd424025c4eade3508a8223f8e8ce77228c`.
- Full suite: `423 passed in 19.40s`.
- Ruff: `All checks passed!` for `src` and `tests`.
- Workflow syntax: `parsed 7 workflows`.
- Actual trusted lifecycle: `ard workflow repository-check --verification-group model-schemas` returned success for exact head `9db4fcd` with one successful verifier.
- Network-free static verifiers: `schemas`, `ossie-checksum`, and `secret-scan` all returned success.
- Distinct-checkout verification: an archived candidate tree created its own frozen `uv` environment and resolved the worktree-external trusted helper with exit 0.
- Distribution: sdist and wheel built successfully; the wheel verifier requires `model_schema_verification.py`.
- Scope: nine intended files; no change under `schemas/`, `src/ard_ossie/ir.py`, or `src/ard_ossie/models.py`; `git diff --check` clean.
- Independent review: initial runtime-boundary findings were reproduced and hardened with stable errors, fixed parent evidence, nonce/full-catalog receipt, and helper digest checks; final re-review reported no Critical, Important, or Minor issues and `Ready to merge: Yes`.
- Local official actionlint download could not run because this environment blocks direct GitHub release access; GitHub CI remains the authoritative actionlint gate before merge.
