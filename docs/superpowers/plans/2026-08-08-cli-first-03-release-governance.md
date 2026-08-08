# CLI-First Release and Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the CLI-first conversion for release detection/publication, downstream dispatch, repository validation, and GitHub repository bootstrap, then enforce Thin Actions across the repository.

**Architecture:** Release and governance use cases consume the same result, Git, GitHub, filesystem, and command-runner contracts built in plan 01. Merged data is revalidated before immutable publication, protected downstream dispatch remains a separate Environment job, and repository/bootstrap operations are desired-state reconcilers. The final workflows are platform declarations whose processing steps call only `ard`.

**Tech Stack:** Plans 01–02 interfaces, Python 3.12, Typer, Pydantic, Git/Git LFS, GitHub CLI/API, pytest, Ruff, actionlint, uv, GitHub Actions

## Global Constraints

- Complete and verify plans 01 and 02 before starting this plan.
- Release only merged, exact-head, current-version products with passing quality and complete changesets.
- Tags `product/<product-id>/vN` and `table/<table-id>/vN` are immutable.
- GitHub Release bundles contain generated artifacts, quality reports, and verified hashes.
- `production-linkage` approval remains separate from release creation.
- Repository default workflow permissions remain read-only; workflows request explicit permissions.
- Bootstrap accepts the LLM API key only through hidden input and sends it to `gh secret set` via stdin.
- Initial `main` protection requires PR/current base/conversation resolution/two statuses with zero approving reviews; one review is enabled only after a non-owner writer exists.
- Actions install uv with `astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b` (`v8.1.0`) and `version: '0.11.33'`.

---

### Task 1: Release-target detection lifecycle

**Files:**
- Create: `src/ard_ossie/application/release_detection.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_release_detection_service.py`
- Test: `tests/integration/test_workflow_release_detect_cli.py`

**Interfaces:**
- Consumes: `GitPort.changed_paths()`, `Registry`, changeset records
- Produces: `ReleaseDetectionRequest`, `ReleaseDetectionService.run()`, `ard workflow release-detect`, JSON-array outputs `products` and `tables`

- [ ] **Step 1: Write failing target-expansion tests**

```python
def test_detect_expands_completed_changeset_products(service, request) -> None:
    result = service.run(request)
    assert result.outputs["products"] == ["finance-order", "sales-order"]


def test_detect_rejects_stale_readiness_version(service, request) -> None:
    service.registry.products[PRODUCT_ID].version += 1
    with pytest.raises(WorkflowConflict, match="CHANGESET_VERSION_NOT_CURRENT"):
        service.run(request)
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_release_detection_service.py -q`

Expected: FAIL because the detection service does not exist.

- [ ] **Step 3: Implement Python path classification and changeset expansion**

Compare the merged range `before..current`, collect changed `products/*/generated/**`, changed changeset records, and table records, resolve product keys/IDs through Registry indexes, and expand only complete changesets whose readiness versions equal current Registry versions. Sort and deduplicate every output.

- [ ] **Step 4: Implement CLI result/GitHub matrix output**

Write JSON arrays to the result envelope and collision-safe `$GITHUB_OUTPUT`; an empty release set is successful no-op. Reject missing Registry references, malformed changesets, or deleted current artifacts with exit `40`.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_release_detection_service.py tests/integration/test_workflow_release_detect_cli.py tests/unit/test_release.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/release_detection.py src/ard_ossie/cli/workflow.py tests/unit/test_release_detection_service.py tests/integration/test_workflow_release_detect_cli.py
git commit -m "feat: add CLI release target detection"
```

### Task 2: Verified tag, bundle, and GitHub Release lifecycle

**Files:**
- Create: `src/ard_ossie/application/release_publication.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `src/ard_ossie/release.py`
- Test: `tests/unit/test_release_publication_service.py`
- Test: `tests/integration/test_workflow_release_product_cli.py`

**Interfaces:**
- Consumes: `resolve_release_plan()`, `build_release_bundle()`, `GitPort`, `GitHubPort`
- Produces: `ReleasePublicationRequest`, `ReleasePublicationService.run()`, `ard workflow release-product`

- [ ] **Step 1: Write failing exact-readiness and immutable-tag tests**

```python
def test_publish_verifies_every_recorded_pr_head_and_ancestry(service, request) -> None:
    result = service.run(request)
    assert result.outputs["product_tag"] == f"product/{PRODUCT_ID}/v12"
    assert result.outputs["artifact_sha256"] == sha256_file(Path(result.artifacts[0]))


def test_publish_rejects_existing_tag_at_other_commit(service, request) -> None:
    service.git.tags[f"product/{PRODUCT_ID}/v12"] = "b" * 40
    with pytest.raises(WorkflowConflict, match="TAG_TARGET_CONFLICT"):
        service.run(request)
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_release_publication_service.py -q`

Expected: FAIL because the publication service is unavailable.

- [ ] **Step 3: Implement release verification**

Re-resolve current Registry state and quality report, verify zero hard errors, complete/current changeset readiness, each recorded PR's merged time and exact head, merge-commit ancestry to the current main SHA, and every bundle source hash. Perform all checks before creating the first tag.

- [ ] **Step 4: Implement convergent publication**

Create annotated product/table tags at the exact main SHA only when absent; reuse exact matches and reject mismatches. Build the ZIP deterministically, calculate SHA-256, create or update the GitHub Release asset only when content matches the plan, and return tag, product/version, commit, bundle, and artifact hashes. A failure after any remote write returns exit `70` and a mutation journal.

- [ ] **Step 5: Add retry and artifact-tampering cases**

Test rerun after tags but before Release, existing matching Release, mismatched Release asset, missing quality report, wrong recorded head, unmerged PR, non-ancestor merge commit, and changed bundle file after plan. Assert no mutable tag overwrite.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_release_publication_service.py tests/integration/test_workflow_release_product_cli.py tests/unit/test_release.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/release_publication.py src/ard_ossie/cli/workflow.py src/ard_ossie/release.py tests/unit/test_release_publication_service.py tests/integration/test_workflow_release_product_cli.py
git commit -m "feat: publish verified ARD releases through CLI"
```

### Task 3: Protected downstream dispatch lifecycle

**Files:**
- Create: `src/ard_ossie/application/release_dispatch.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_release_dispatch_service.py`
- Test: `tests/integration/test_workflow_release_dispatch_cli.py`

**Interfaces:**
- Consumes: release result envelopes, `GitHubPort.repository_dispatch()`, and exact-context status lookup/publication
- Produces: `ReleaseDispatchRequest`, `ReleaseDispatchService.run()`, `ard workflow release-dispatch`

- [ ] **Step 1: Write failing payload allowlist tests**

```python
def test_dispatch_contains_only_approved_release_fields(service, release_result) -> None:
    service.run(release_result)
    assert service.github.last_dispatch == {
        "event_type": "ard_product_released",
        "client_payload": {
            "product_id": PRODUCT_ID,
            "version": 12,
            "tag": f"product/{PRODUCT_ID}/v12",
            "commit": "a" * 40,
            "artifact_hashes": release_result.outputs["artifact_hashes"],
        },
    }
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_release_dispatch_service.py -q`

Expected: FAIL because the dispatch service does not exist.

- [ ] **Step 3: Implement approved-envelope validation and dispatch**

Accept only a successful version-1 `workflow.release-product` result. Verify artifact hash syntax, exact tag/product/version relationship, current main commit, and absence of extra payload fields. Before sending, check the best-effort status context `ard/dispatched:<product-id>:v<version>` on the merged commit; success is a no-op. After sending, publish that success status. If sending succeeds but status publication fails, return exit `70`; a retry may redeliver, so document the payload tuple `(product_id, version, tag, commit)` as the required downstream deduplication key.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/test_release_dispatch_service.py tests/integration/test_workflow_release_dispatch_cli.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/release_dispatch.py src/ard_ossie/cli/workflow.py tests/unit/test_release_dispatch_service.py tests/integration/test_workflow_release_dispatch_cli.py
git commit -m "feat: dispatch approved ARD releases through CLI"
```

### Task 4: Repository change classifier and verification CLI

**Files:**
- Create: `src/ard_ossie/application/repository_checks.py`
- Modify: `src/ard_ossie/cli/workflow.py`
- Test: `tests/unit/test_repository_check_service.py`
- Test: `tests/integration/test_workflow_repository_check_cli.py`

**Interfaces:**
- Consumes: `GitPort.changed_paths()`, `CommandRunner`, `GitHubPort.set_status()`
- Produces: `RepositoryCheckRequest`, `RepositoryCheckService.run()`, `ard workflow repository-check`

- [ ] **Step 1: Write failing classification and tool-order tests**

```python
def test_repository_check_rejects_mixed_code_and_data(service, request) -> None:
    service.git.paths = ("src/ard_ossie/cli/root.py", "products/a/sources/a.html")
    with pytest.raises(WorkflowValidationError, match="MIXED_CODE_AND_ARD_DATA_NOT_ALLOWED"):
        service.run(request)


def test_repository_check_runs_pinned_verifiers_in_order(service, request) -> None:
    service.run(request)
    assert service.tools.names == ["pytest", "ruff", "actionlint", "schemas", "wheel", "ossie-checksum", "secret-scan"]
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_repository_check_service.py -q`

Expected: FAIL because `RepositoryCheckService` is unavailable.

- [ ] **Step 3: Implement classification and verification adapters**

Classify `products/**` and `registry/**` as ARD data and every other changed path as repository code/config/docs. Reject mixed changes. For code-only changes, run locked pytest and Ruff, actionlint `v1.7.7`, checked-in schema synchronization, wheel asset inspection, Ossie checksum, and secret pattern scan. The actionlint adapter downloads the official `v1.7.7` release archive and its published checksum manifest into `.ard/tools`, verifies the matching archive digest before extraction, and reuses the verified binary by digest. Stop on first failure but record completed tools.

- [ ] **Step 4: Implement status publication and CLI**

Publish both ARD status contexts to the exact PR head: success only when all verifiers pass, failure otherwise. The finalizer path must still publish failure if a verifier process crashes. Output `code_only`, `head_sha`, and verifier summaries.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_repository_check_service.py tests/integration/test_workflow_repository_check_cli.py -q`

Expected: PASS.

```bash
git add src/ard_ossie/application/repository_checks.py src/ard_ossie/cli/workflow.py tests/unit/test_repository_check_service.py tests/integration/test_workflow_repository_check_cli.py
git commit -m "feat: run repository verification through ARD CLI"
```

### Task 5: GitHub repository bootstrap desired-state CLI

**Files:**
- Create: `src/ard_ossie/application/github_bootstrap.py`
- Modify: `src/ard_ossie/cli/github.py`
- Test: `tests/unit/test_github_bootstrap_service.py`
- Test: `tests/integration/test_github_bootstrap_cli.py`
- Modify: `docs/github-actions-setup.md`

**Interfaces:**
- Consumes: approved bootstrap spec, `GitHubPort`, `ResultWriter`, hidden prompt function
- Produces: `BootstrapConfig`, `GitHubBootstrapService.plan()/apply()`, `ard github bootstrap`, `ard github enable-review-protection`

- [ ] **Step 1: Write failing desired-state and no-op tests**

```python
def test_bootstrap_plan_contains_exact_project_resources(service) -> None:
    plan = service.plan(provider_config())
    assert [item.target for item in plan.items] == [
        "label:ard:submission", "label:ard:approved", "label:ard:processing",
        "label:ard:failed", "label:ard:pr-created", "actions:workflow-permissions",
        "environment:ard-llm", "environment:production-linkage", "branch:main",
    ]


def test_second_bootstrap_is_noop(service) -> None:
    service.apply(service.plan(provider_config()), api_key="sentinel")
    assert all(item.action == "noop" for item in service.plan(provider_config()).items)
```

- [ ] **Step 2: Run tests and verify missing service failure**

Run: `uv run pytest tests/unit/test_github_bootstrap_service.py -q`

Expected: FAIL because the bootstrap service is unavailable.

- [ ] **Step 3: Implement exact desired state**

Encode the five approved labels/colors/descriptions, read-only default workflow permission plus PR creation setting, `ard-llm` reviewer/variables/secret-name/`main`+`ard/*` policies, `production-linkage` reviewer/`main` policy, and initial `main` protection with strict two statuses/PR/conversation/admin enforcement/no force/no delete/zero reviews. Compute `create`, `update`, `noop`, and `blocked` without mutation.

- [ ] **Step 4: Implement interactive secret-safe apply**

Display the exact repository and redacted plan, confirm, apply sequentially, and request the API key with `getpass` only immediately before the secret operation. Pass it through stdin, discard it after the call, and sanitize any child output. Existing secret replacement defaults to no. `--dry-run` never prompts for or changes a secret.

- [ ] **Step 5: Implement review-protection transition**

List collaborators, require a non-owner with write/maintain/admin permission, refetch current protection, and change only `required_approving_review_count` from `0` to `1`. Without an eligible collaborator, exit `20`/`ELIGIBLE_REVIEWER_NOT_FOUND` and make no mutation.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_github_bootstrap_service.py tests/integration/test_github_bootstrap_cli.py tests/unit/test_github_cli_adapter.py -q`

Expected: PASS and the sentinel secret appears only in fake `gh` stdin.

```bash
git add src/ard_ossie/application/github_bootstrap.py src/ard_ossie/cli/github.py tests/unit/test_github_bootstrap_service.py tests/integration/test_github_bootstrap_cli.py docs/github-actions-setup.md
git commit -m "feat: automate GitHub ARD repository bootstrap"
```

### Task 6: Thin release/repository Actions and final repository gate

**Files:**
- Modify: `.github/workflows/ard-release.yml`
- Modify: `.github/workflows/ard-repository-change.yml`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `tests/e2e/test_approved_issue_to_release.py`
- Modify: `README.md`
- Modify: `docs/github-actions-setup.md`

**Interfaces:**
- Consumes: Tasks 1–5 lifecycle commands; plan 02 Thin Actions contract
- Produces: fully CLI-first repository, complete documentation, passing local and GitHub CI

- [ ] **Step 1: Extend the failing Thin Actions policy to every workflow**

Add `ard-release.yml` and `ard-repository-change.yml` to `PROCESSING_WORKFLOWS`. Parse every `run:` scalar and assert it begins with `uv run --frozen ard`, contains no shell control syntax, and invokes one of the approved lifecycle commands.

- [ ] **Step 2: Run workflow tests and confirm remaining YAML fails**

Run: `uv run pytest tests/integration/test_workflow_contracts.py -q`

Expected: FAIL on inline release detection/JQ/Git/GitHub and repository verification shell.

- [ ] **Step 3: Convert release and repository jobs**

Release detect invokes `release-detect`; the matrix invokes `release-product`; the protected `production-linkage` job invokes `release-dispatch`. Repository classification/verification invokes `repository-check`, and its `if: always()` finalizer publishes statuses through `finalize`. Preserve exact permissions, concurrency, checkout depth, LFS, artifact upload, matrices, and Environment names.

- [ ] **Step 4: Update operator and contributor documentation**

Document local granular commands, local workflow simulation with `--event`, common result envelopes, exit codes, retry guidance, bootstrap usage, required `ard/*` branches, and the rule that Actions YAML contains platform declarations only. Remove obsolete manual shell sequences.

- [ ] **Step 5: Run complete verification**

Run:

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
actionlint .github/workflows/*.yml
uv build
uv run ard --help
uv run ard workflow --help
uv run ard github --help
```

Expected: all commands PASS; the test count is greater than the pre-migration 105 tests.

- [ ] **Step 6: Run security and artifact checks**

Verify the Apache Ossie 0.1.1 checksum, checked-in schemas, wheel contents for templates/schemas/new packages, and repository secret-pattern scan. Run the fake-key tests and confirm `sentinel-key` is absent from `.ard/`, test reports, Git diff, and built artifacts.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ard-release.yml .github/workflows/ard-repository-change.yml tests/integration/test_workflow_contracts.py tests/e2e/test_approved_issue_to_release.py README.md docs/github-actions-setup.md
git commit -m "ci: complete CLI-first ARD Actions pipeline"
```

## Final completion gate

- [ ] All six workflows satisfy the Thin Actions static contract.
- [ ] All lifecycle and granular commands appear in `ard --help` or their command-group help.
- [ ] Issue, direct, process, changeset, release, repository-check, finalizer, and bootstrap E2E paths pass.
- [ ] Stable IDs, duplicate handling, numeric versions, changeset readiness, Ossie 0.1.1, atomic promotion, immutable tags, and hash manifests remain verified.
- [ ] No token or API key appears in arguments, logs, results, artifacts, commits, comments, or releases.
- [ ] `uv run pytest -q`, Ruff, actionlint, build, schema, checksum, wheel, and secret scan pass.
- [ ] Draft PR #1 head has successful `ard/quality-gate` and `ard/changeset` statuses.
- [ ] `git status --short` is clean.
