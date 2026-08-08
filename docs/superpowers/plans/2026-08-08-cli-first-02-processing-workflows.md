# CLI-First Processing Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Issue intake, direct-change detection, product processing, shared-table changesets, and finalization out of Actions shell blocks into tested `ard workflow` lifecycle commands.

**Architecture:** Each trust-separated GitHub job invokes one lifecycle command. Application services coordinate the foundation ports and adapters, write a common result envelope, and perform exact-head/idempotent mutations. YAML retains permissions, Environment boundaries, checkout/setup, job outputs, and artifact upload only.

**Tech Stack:** Foundation plan interfaces, Python 3.12, Typer, Pydantic, Docling, openpyxl, OpenAI-compatible API, Git/Git LFS, GitHub CLI, GitHub Actions

## Global Constraints

- Complete and verify plan 01 before starting this plan.
- Preserve the `ard-llm` Environment and same-repository/path-scope gate.
- Fork PRs and Issue authorization jobs never receive LLM secrets or write credentials.
- LLM output remains a strict-schema suggestion and cannot assign IDs, approve duplicates, or decide versions.
- Registry, `generated/`, and `quality/` promote atomically only after all hard checks pass.
- GitHub mutations verify exact branch/head SHA and are safe to rerun.
- Every processing/finalizer `run:` block begins with `uv run --frozen ard` and contains no direct shell business logic.
- Existing status contexts remain `ard/quality-gate` and `ard/changeset`.
- Actions install uv with `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b` (`v8.1.0`) and `version: '0.11.33'`.

---

### Task 1: Issue authorization and intake application services

**Files:**
- Create: `src/ard_ossie/application/intake.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `src/ard_ossie/github_event.py`
- Test: `tests/unit/test_intake_service.py`
- Test: `tests/integration/test_workflow_issue_cli.py`

**Interfaces:**
- Consumes: `WorkflowContext`, `WorkflowResult`, `GitPort`, `GitHubPort`, `RepositoryPaths`, existing `prepare_issue_event()`
- Produces: `IssueAuthorizationService.run()`, `IssueIntakeService.run()`, `ard workflow issue-authorize`, `ard workflow issue-intake`

- [ ] **Step 1: Write failing authorization tests**

```python
def test_issue_authorize_requires_approved_label_and_writer(context, github) -> None:
    github.permissions["kimohy"] = "admin"
    result = IssueAuthorizationService(github).run(context, label="ard:approved", actor="kimohy")
    assert result.status == "success"
    assert result.outputs["allowed"] is True


def test_issue_authorize_rejects_reader(context, github) -> None:
    github.permissions["reader"] = "read"
    with pytest.raises(WorkflowSecurityError, match="ISSUE_APPROVER_PERMISSION_DENIED"):
        IssueAuthorizationService(github).run(context, label="ard:approved", actor="reader")
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_intake_service.py -q`

Expected: FAIL because `IssueAuthorizationService` is undefined.

- [ ] **Step 3: Implement authorization and intake planning**

Parse the Issue event into typed fields, call the existing attachment validators, derive `ard/issue-<number>-<product-key>`, compute canonical source hashes, and search for an equivalent open PR before any write. The plan contains label changes, download targets, branch/commit, Draft PR, and outputs `branch`, `product_key`, `pr_number`, `expected_head`.

- [ ] **Step 4: Add failing idempotent intake test**

```python
def test_issue_intake_reuses_equivalent_branch_and_pr(fixture_event, services) -> None:
    first = services.intake.run(fixture_event)
    second = services.intake.run(fixture_event)
    assert first.outputs["pr_number"] == second.outputs["pr_number"]
    assert second.status == "noop"
    assert services.github.created_pr_count == 1
```

- [ ] **Step 5: Implement apply and CLI commands**

`issue-authorize` is read-only and writes `allowed`. `issue-intake` adds `ard:processing`, downloads only validated GitHub Issue attachments, writes canonical sources/product config, commits via `GitPort`, pushes LFS before Git, creates or reuses the Draft PR, and adds `ard:pr-created`. Map validation/security/transient/partial failures to the approved exit codes and always write a result envelope.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_intake_service.py tests/integration/test_workflow_issue_cli.py tests/unit/test_github_event.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/intake.py src/ard_ossie/cli/workflow.py src/ard_ossie/github_event.py tests/unit/test_intake_service.py tests/integration/test_workflow_issue_cli.py
git commit -m "feat: add CLI issue intake workflow"
```

### Task 2: Direct-change detection and secret-free source check

**Files:**
- Create: `src/ard_ossie/application/source_check.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_source_check_service.py`
- Test: `tests/integration/test_workflow_direct_cli.py`

**Interfaces:**
- Consumes: `GitPort.changed_paths()`, `RepositoryPaths`, `scan_sources()`, isolated granular validation, `GitHubPort`
- Produces: `DetectProductService.run()`, `SourceCheckService.run()`, `EnsureProductPrService.run()`, `ard workflow detect-product`, `ard workflow source-check`, `ard workflow ensure-product-pr`

- [ ] **Step 1: Write failing product-detection tests**

```python
@pytest.mark.parametrize("paths,expected", [
    (("products/sales-order/sources/product.html",), "sales-order"),
    (("README.md",), None),
])
def test_detect_product(paths, expected, git) -> None:
    git.paths = paths
    result = DetectProductService(git).run("origin/main", "HEAD")
    assert result.outputs.get("product_key") == expected


def test_detect_product_rejects_multiple_products(git) -> None:
    git.paths = ("products/a/sources/a.html", "products/b/sources/b.html")
    with pytest.raises(WorkflowValidationError, match="MULTIPLE_PRODUCTS_NOT_ALLOWED"):
        DetectProductService(git).run("origin/main", "HEAD")
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_source_check_service.py -q`

Expected: FAIL because the services do not exist.

- [ ] **Step 3: Implement detection and source-only validation**

Parse changed paths in Python, enforce exactly one `products/<key>/sources/**` product for ARD changes, reject mixed code/data paths, and verify all source signatures and limits. Copy the candidate product and a read-only Registry snapshot into `.ard/staging/source-check`, run deterministic parsing/model validation there with no provider, then delete staging. `source-check` must assert `ARD_LLM_API_KEY` is absent and must not construct Git/GitHub write adapters.

- [ ] **Step 4: Add CLI output and path-escape tests**

Invoke both commands with a fixture repository and fake event. Assert `$GITHUB_OUTPUT` receives `product_key` and `expected_head`, `.ard/run` is written, sources and Registry are unchanged, and traversal/symlink cases exit `50`.

- [ ] **Step 5: Add the validated direct-branch PR command**

`EnsureProductPrService.run(branch, product_key, expected_head)` refetches the branch head, requires it to equal `expected_head`, finds an existing open PR by exact head branch, or creates one Draft PR with title `data(<product-key>): update ARD sources`. It returns `pr_number` and `expected_head`; a second call is a no-op. This command runs in the existing write-permission PR job after `source-check` and before the protected processing job.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_source_check_service.py tests/integration/test_workflow_direct_cli.py tests/unit/test_ingestion.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/source_check.py src/ard_ossie/cli/workflow.py tests/unit/test_source_check_service.py tests/integration/test_workflow_direct_cli.py
git commit -m "feat: add CLI direct-change preflight"
```

### Task 3: Product-processing lifecycle and exact-head writeback

**Files:**
- Create: `src/ard_ossie/application/processing.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `src/ard_ossie/pipeline.py`
- Test: `tests/unit/test_processing_service.py`
- Test: `tests/integration/test_workflow_process_cli.py`
- Modify: `tests/integration/test_atomic_promotion.py`

**Interfaces:**
- Consumes: existing `process_product()`, `GitPort`, `GitHubPort`, `WorkflowContext`, `ResultWriter`
- Produces: `ProcessingRequest`, `ProcessingService.run(request)`, `ard workflow process`

- [ ] **Step 1: Write a failing success-transaction test**

```python
def test_processing_promotes_commits_and_sets_exact_head_status(services, request) -> None:
    result = services.processing.run(request)
    assert result.status == "success"
    assert result.outputs["product_id"].startswith("prd_")
    assert [m.resource for m in result.mutations][-3:] == ["commit", "status", "status"]
    assert services.github.statuses[-1].sha == services.git.current_sha()
```

- [ ] **Step 2: Run the test and verify missing service failure**

Run: `uv run pytest tests/unit/test_processing_service.py -q`

Expected: FAIL because `ProcessingService` is unavailable.

- [ ] **Step 3: Implement request validation and processing orchestration**

```python
class ProcessingRequest(StrictModel):
    repository: Path
    product_key: str
    branch: str
    pr_number: int
    expected_head: str
    allow_writeback: bool
```

Before loading secrets or parsing, compare current checkout SHA, PR head SHA, and `expected_head`; require all three to match. Call `process_product` with the environment-derived OpenAI provider, then revalidate writeback paths, commit and push, read the new exact head, and publish both statuses to that head. Dispatch changeset readiness only after successful commit/status publication.

- [ ] **Step 4: Add failure and atomicity tests**

Cover validation hard error, provider timeout, stale head before processing, head change before push, writeback path violation, LFS failure, status failure after commit, and warnings-as-errors. Assert previous Registry/generated artifacts survive hard errors, detailed quality reports survive, and exit codes are respectively `10`, `30`, `40`, `50`, or `70`.

- [ ] **Step 5: Implement CLI command and result artifacts**

The command writes `.ard/run/workflow.process-result.json`, outputs product ID/version/current head/changeset ID, emits quality artifact paths, and returns only after verifying remote branch head. It never prints provider configuration values.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_processing_service.py tests/integration/test_workflow_process_cli.py tests/integration/test_atomic_promotion.py tests/integration/test_openai_compatible.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/processing.py src/ard_ossie/cli/workflow.py src/ard_ossie/pipeline.py tests/unit/test_processing_service.py tests/integration/test_workflow_process_cli.py tests/integration/test_atomic_promotion.py
git commit -m "feat: add CLI product processing workflow"
```

### Task 4: Shared-table changeset lifecycle

**Files:**
- Create: `src/ard_ossie/application/changesets.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_changeset_service.py`
- Test: `tests/integration/test_workflow_changeset_cli.py`

**Interfaces:**
- Consumes: `Registry`, `GitPort`, `GitHubPort`, existing changeset domain models
- Produces: `ChangesetRequest`, `ChangesetService.run()`, `ard workflow changeset`

- [ ] **Step 1: Write failing create/ready/idempotency tests**

```python
def test_changeset_create_builds_coordination_and_tracking_prs(service, create_request) -> None:
    result = service.run(create_request)
    assert result.outputs["required_count"] == 2
    assert len([m for m in result.mutations if m.resource == "pull_request"]) == 3


def test_changeset_ready_is_idempotent_for_same_head(service, ready_request) -> None:
    first = service.run(ready_request)
    second = service.run(ready_request)
    assert first.outputs == second.outputs
    assert second.status == "noop"
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_changeset_service.py -q`

Expected: FAIL because `ChangesetService` is unavailable.

- [ ] **Step 3: Implement serialized coordination**

For create mode, validate `cst_<uuidv7>`, exact table/product IDs, and Registry existence; create/reuse the central branch/PR and one tracking branch/PR per required product. For ready mode, require version `1..999`, PR number, exact 40-character head SHA, and an existing central changeset. Commit only the central JSON/approved tracking marker paths.

- [ ] **Step 4: Implement status reconciliation and impact comment**

Compute ready/required counts in Python. Set `ard/changeset` success only when every required product has current Registry version and exact recorded PR head; otherwise pending. Upsert one marker-owned impact comment on the initiating PR. Reject conflicting readiness under the same product ID with exit `40`.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_changeset_service.py tests/integration/test_workflow_changeset_cli.py tests/unit/test_impact.py tests/unit/test_release.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/changesets.py src/ard_ossie/cli/workflow.py tests/unit/test_changeset_service.py tests/integration/test_workflow_changeset_cli.py
git commit -m "feat: add CLI changeset coordination workflow"
```

### Task 5: Common finalizer lifecycle

**Files:**
- Create: `src/ard_ossie/application/finalize.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_finalize_service.py`
- Test: `tests/integration/test_workflow_finalize_cli.py`

**Interfaces:**
- Consumes: prior `WorkflowResult` files, GitHub job-result strings, `GitHubPort`
- Produces: `FinalizeRequest`, `FinalizeService.run()`, `ard workflow finalize`

- [ ] **Step 1: Write failing finalizer tests**

```python
def test_issue_success_removes_processing_without_failed(finalizer, request) -> None:
    result = finalizer.run(request.model_copy(update={"upstream_result": "success"}))
    assert finalizer.github.issue_labels == {"ard:approved", "ard:pr-created"}
    assert result.status == "success"


def test_partial_processing_posts_failure_status_and_comment_once(finalizer, request) -> None:
    finalizer.run(request.model_copy(update={"upstream_result": "failure"}))
    finalizer.run(request.model_copy(update={"upstream_result": "failure"}))
    assert finalizer.github.managed_comment_count("ard:finalize") == 1
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_finalize_service.py -q`

Expected: FAIL because `FinalizeService` is unavailable.

- [ ] **Step 3: Implement result consumption and idempotent reconciliation**

Parse only version `1` result envelopes. Reject malformed/untrusted result paths. Reconcile `ard:processing`/`ard:failed`, upsert the appropriate PR summary marker, and set missing failure statuses on the exact known head. A finalizer failure must not overwrite the original result; write its own envelope.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_finalize_service.py tests/integration/test_workflow_finalize_cli.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/finalize.py src/ard_ossie/cli/workflow.py tests/unit/test_finalize_service.py tests/integration/test_workflow_finalize_cli.py
git commit -m "feat: add CLI workflow finalizer"
```

### Task 6: Thin Issue, direct, process, and changeset Actions

**Files:**
- Modify: `.github/workflows/ard-issue-intake.yml`
- Modify: `.github/workflows/ard-direct-change.yml`
- Modify: `.github/workflows/ard-process.yml`
- Modify: `.github/workflows/ard-changeset.yml`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `tests/e2e/test_approved_issue_to_release.py`

**Interfaces:**
- Consumes: Tasks 1–5 lifecycle commands and their GitHub outputs
- Produces: Thin Actions YAML with unchanged triggers, permissions, Environment boundaries, statuses, and artifacts

- [ ] **Step 1: Replace permissive workflow tests with a failing Thin Actions policy**

```python
FORBIDDEN_RUN_TOKENS = ("git ", "gh ", "jq ", "awk ", "sed ", "python ", "pytest", "ruff", "actionlint")


def test_processing_run_steps_only_invoke_ard_cli() -> None:
    for path in PROCESSING_WORKFLOWS:
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "run" not in step:
                    continue
                command = step["run"].strip()
                assert command.startswith("uv run --frozen ard ")
                assert not any(token in command for token in FORBIDDEN_RUN_TOKENS)
```

- [ ] **Step 2: Run workflow tests and confirm current YAML fails**

Run: `uv run pytest tests/integration/test_workflow_contracts.py -q`

Expected: FAIL on current inline Git/GitHub/JQ/Python logic.

- [ ] **Step 3: Convert each job to its lifecycle command**

Use pinned checkout/setup-python/upload-artifact Actions plus `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b` with `version: '0.11.33'`. Give every CLI step an `id` and map values such as `${{ steps.intake.outputs.branch }}` into job outputs. The direct workflow's write-permission PR job invokes only `ard workflow ensure-product-pr`. Preserve secret-free authorize/source-check jobs, `ard-llm` only on processing, `persist-credentials: false` on untrusted checkouts, concurrency groups, LFS checkout, and `if: always()` finalizers.

- [ ] **Step 4: Verify no behavior was lost in E2E fixtures**

Extend the Issue-to-release E2E fixture to invoke the same lifecycle application services with fake Git/GitHub adapters. Assert Draft PR, stable IDs, numeric version, four generated artifacts, five quality artifacts, exact statuses, and Issue labels.

- [ ] **Step 5: Run workflow and full verification**

Run:

```bash
uv run pytest tests/integration/test_workflow_contracts.py tests/e2e/test_approved_issue_to_release.py -q
actionlint .github/workflows/*.yml
uv run pytest -q
uv run ruff check src tests
```

Expected: all commands PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ard-issue-intake.yml .github/workflows/ard-direct-change.yml .github/workflows/ard-process.yml .github/workflows/ard-changeset.yml tests/integration/test_workflow_contracts.py tests/e2e/test_approved_issue_to_release.py
git commit -m "ci: run ARD processing through CLI workflows"
```

## Phase completion gate

- [ ] The four migrated workflows contain no direct Git/GitHub/JQ/AWK/sed/inline-Python processing.
- [ ] Fork and source-check jobs have no write token and no `ard-llm` Environment.
- [ ] The LLM job uses exact expected head and protected Environment.
- [ ] Issue, direct-change, processing, changeset, and finalizer retry tests pass.
- [ ] `uv run pytest -q`, Ruff, and actionlint pass.
- [ ] `git status --short` is clean.
- [ ] Proceed to `2026-08-08-cli-first-03-release-governance.md` only after review.
