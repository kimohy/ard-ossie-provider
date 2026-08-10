# Trusted Processing Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure direct-branch and Issue processing execute only default-branch workflow and Python code before protected Secrets or write credentials are used, while restricting intake to immutable GitHub attachments and making merge readiness live-head based.

**Architecture:** A read-only push workflow emits a stable signal. A default-branch `workflow_run` coordinator validates the exact candidate with trusted CLI code, creates or reuses the Draft PR, and calls a reusable processor that has separate credential-free validation and protected processing jobs. Candidate files remain in a separate checkout passed through `--repository`; attachment validation distinguishes immutable initial URLs from signed storage redirects.

**Tech Stack:** GitHub Actions YAML, Python 3.12, Typer, httpx, pytest, Ruff, actionlint, uv.

## Global Constraints

- Preserve automatic same-repository Draft PR creation and exact-head writeback.
- Preserve Issue-based processing and existing result-envelope/reconciliation behavior.
- `ARD_LLM_API_KEY` is available only in `environment: ard-llm`, whose deployment branch policy is exactly `main`.
- All third-party Actions remain pinned to 40-character commit SHAs.
- All lifecycle shell steps invoke the locked trusted `ard` CLI; no candidate Python, shell hook, or local workflow is executed in a protected job.
- Initial attachments are exactly `https://github.com/user-attachments/assets/<UUID>` with no query or fragment.
- No force push, status forgery, or branch-protection bypass is permitted.

---

### Task 1: Separate untrusted branch signals from trusted processing

**Files:**
- Create: `.github/workflows/ard-direct-signal.yml`
- Modify: `.github/workflows/ard-direct-change.yml`
- Modify: `.github/workflows/ard-process.yml`
- Modify: `src/ard_ossie/application/github_bootstrap.py`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `tests/unit/test_github_bootstrap_service.py`
- Modify: `tests/unit/test_github_cli_adapter.py`

**Interfaces:**
- Consumes: `ard workflow detect-product`, `source-check`, `ensure-product-pr`, `process`, and `process-reconcile`, each with the existing `--repository` option.
- Produces: workflow `ARD direct branch signal`; trusted `workflow_run` coordinator outputs `product_key`, `expected_head`, and `pr_number`; reusable processor `validate` job gates the protected `process` job.

- [ ] **Step 1: Replace the workflow contracts with failing trust-boundary assertions**

Add assertions equivalent to:

```python
signal = load_workflow("ard-direct-signal.yml")
assert signal["permissions"] == {"contents": "read"}
assert signal["on"]["push"]["paths"] == ["products/*/sources/**"]
assert all("run" not in step for step in signal["jobs"]["signal"]["steps"])

direct = load_workflow("ard-direct-change.yml")
assert set(direct["on"]) == {"workflow_run"}
assert direct["on"]["workflow_run"]["workflows"] == ["ARD direct branch signal"]
assert "github.event.workflow_run.head_repository.full_name == github.repository" in direct["jobs"]["validate"]["if"]
assert direct["jobs"]["validate"]["permissions"] == {"contents": "read"}

processor = load_workflow("ard-process.yml")
assert processor["jobs"]["validate"]["permissions"] == {"contents": "read"}
assert "environment" not in processor["jobs"]["validate"]
assert processor["jobs"]["process"]["needs"] == "validate"
assert processor["jobs"]["process"]["environment"] == "ard-llm"
for job_name in ("validate", "process"):
    run_steps = [step for step in processor["jobs"][job_name]["steps"] if step.get("run")]
    assert all(step["working-directory"] == "trusted" for step in run_steps)
    assert all('--repository "$CANDIDATE_REPOSITORY"' in step["run"] for step in run_steps)
```

Change bootstrap expectations to `EnvironmentState(..., branch_patterns=("main",))` and assert the CLI adapter never creates an `ard/*` deployment policy.

- [ ] **Step 2: Run the focused contracts and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest -q tests/integration/test_workflow_contracts.py tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py
```

Expected: failures for missing `ard-direct-signal.yml`, the old push/pull_request direct workflow, the single-checkout processor, and `ard/*` Environment policy.

- [ ] **Step 3: Implement the read-only signal and default-branch coordinator**

Create `ard-direct-signal.yml` with `on.push.branches-ignore: [main]`, source paths, top-level `permissions: {contents: read}`, and a single pinned `actions/checkout` step using `ref: ${{ github.sha }}` and `persist-credentials: false`.

Replace `ard-direct-change.yml` with a `workflow_run` workflow for `ARD direct branch signal`. Its `validate` job must guard success, push origin, same repository, non-default branch, and non-empty head SHA; checkout `main` to `trusted` and the exact signaled SHA to `candidate`; then run trusted detect/source-check commands against `candidate`. Its `pull_request` job uses trusted code to call `ensure-product-pr`. Its `process` job calls `./.github/workflows/ard-process.yml` and passes only validated outputs.

- [ ] **Step 4: Implement validate-before-secret dual checkout in the reusable processor**

Add a `validate` job with `contents: read`, no Environment, no `GH_TOKEN`, trusted/candidate checkouts, and trusted `source-check`. Change `process` to `needs: validate`, checkout trusted `main` at `trusted/` and exact candidate at `candidate/`, and run processing/reconciliation from `trusted/` with absolute candidate `--repository` and result paths. Set `PYTHONSAFEPATH=1`. Point artifact paths to the candidate checkout. Make `finalize` explicitly checkout trusted `main`.

- [ ] **Step 5: Restrict the protected Environment to `main`**

Change:

```python
def _llm_environment(owner: EnvironmentReviewer) -> EnvironmentState:
    return EnvironmentState(
        name="ard-llm",
        reviewers=(owner,),
        prevent_self_review=False,
        wait_timer=0,
        branch_patterns=("main",),
    )
```

- [ ] **Step 6: Run focused tests and official actionlint for GREEN**

Run the focused pytest command from Step 2, then:

```bash
actionlint .github/workflows/*.yml
```

Expected: all focused tests pass and actionlint exits 0.

- [ ] **Step 7: Commit the workflow trust boundary**

```bash
git add .github/workflows/ard-direct-signal.yml .github/workflows/ard-direct-change.yml .github/workflows/ard-process.yml src/ard_ossie/application/github_bootstrap.py tests/integration/test_workflow_contracts.py tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py
git commit -m "fix: isolate trusted ARD processing"
```

### Task 2: Restrict Issue intake to immutable attachments

**Files:**
- Modify: `src/ard_ossie/github_event.py`
- Modify: `tests/unit/test_github_event.py`

**Interfaces:**
- Consumes: `validate_attachment_url(url: str) -> str` for initial Issue links.
- Produces: `_validate_attachment_redirect_url(url: str) -> str` for each redirect; both raise `AttachmentSecurityError` with typed attachment codes.

- [ ] **Step 1: Add failing immutable-URL tests**

Add parameterized rejection cases:

```python
@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/acme/repo/main/product.html",
    "https://github.com/acme/repo/raw/main/dictionary.xlsx",
    "https://avatars.githubusercontent.com/u/1",
    "https://objects.githubusercontent.com/download/1?signature=value",
    "https://github.com/user-attachments/assets/not-a-uuid",
])
def test_initial_attachment_requires_canonical_immutable_github_upload(url: str) -> None:
    with pytest.raises(AttachmentSecurityError):
        validate_attachment_url(url)
```

Add a redirect test that rejects `raw.githubusercontent.com`, retain the signed `objects.githubusercontent.com` success test, and add a signed `github-production-user-asset-6210df.s3.amazonaws.com` success case. Use canonical UUID initial URLs in all download tests.

- [ ] **Step 2: Run the URL tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest -q tests/unit/test_github_event.py
```

Expected: mutable GitHub URLs and malformed user-attachment paths are currently accepted, so the new cases fail.

- [ ] **Step 3: Split initial and redirect validation**

Use `uuid.UUID` plus an exact path regex for the initial URL. Keep shared transport checks for HTTPS, credentials, fragment, and port. The redirect validator accepts only another canonical user attachment or exact storage hosts `objects.githubusercontent.com` and `github-production-user-asset-<lowercase suffix>.s3.amazonaws.com`; only storage hosts may carry signed queries. Replace redirect-loop calls to `validate_attachment_url(..., allow_query=True)` with `_validate_attachment_redirect_url(...)`.

- [ ] **Step 4: Run URL tests for GREEN**

Run the command from Step 2.

Expected: all GitHub event tests pass, including redirect and file-header validation.

- [ ] **Step 5: Commit immutable attachment validation**

```bash
git add src/ard_ossie/github_event.py tests/unit/test_github_event.py
git commit -m "fix: require immutable issue attachments"
```

### Task 3: Make merge readiness live-head based and complete verification

**Files:**
- Modify: `docs/next-steps.md`
- Modify: `docs/github-actions-setup.md`
- Modify: PR #1 description after push

**Interfaces:**
- Consumes: live PR `head_sha` and workflow runs associated with that exact commit.
- Produces: a merge checklist that cannot become stale merely because documentation is committed.

- [ ] **Step 1: Confirm the stale documentation evidence**

Run:

```bash
rg -n "검증 대상 head|bootstrap 검증|run #2|ard/\\*" docs/next-steps.md docs/github-actions-setup.md
```

Expected before editing: the roadmap contains a fixed historical head/run and the setup guide allows `ard/*` into the protected Environment.

- [ ] **Step 2: Replace fixed checkpoints with live checks**

Update both documents to require reading the current PR head, selecting the latest bootstrap run with matching `head_sha`, requiring all four jobs to succeed, and restarting review if the head moves. Document the read-only signal/default-branch coordinator and `ard-llm` main-only policy. Remove statements that same-repository candidate branches enter the protected Environment directly.

- [ ] **Step 3: Verify the documentation invariant**

Run:

```bash
rg -n "head_sha|같은 SHA|처음부터 다시" docs/next-steps.md docs/github-actions-setup.md
git diff --check
```

Expected: live same-head/restart instructions are present and no whitespace errors are reported. Human-facing prose is not protected by a source-text unit test.

- [ ] **Step 4: Commit documentation**

```bash
git add docs/next-steps.md docs/github-actions-setup.md docs/superpowers/plans/2026-08-10-trusted-processing-boundary.md
git commit -m "docs: make bootstrap review head-aware"
```

- [ ] **Step 5: Run complete local verification**

Run in a disposable clone pinned to the final commit tree:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check src tests
actionlint .github/workflows/*.yml
UV_CACHE_DIR=/tmp/ard-uv-cache uv build --sdist --wheel
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ard workflow repository-check --base-ref c23333610cb1d27ff136910de010011b6c870f3a --head-ref "$(git rev-parse HEAD)" --head-sha "$(git rev-parse HEAD)" --repository . --verification-group static
```

Expected: zero failures; the test count is recorded from fresh output rather than copied from an older run.

- [ ] **Step 6: Push without force and verify GitHub Actions**

Confirm PR #1 still has the expected old head, create equivalent Git data objects for the local commits, compare each remote tree to the local tree, and move the PR branch with `force=false`. Wait for the new bootstrap run and require `static`, `pytest`, `wheel`, and aggregate to succeed.

- [ ] **Step 7: Update PR #1 description and perform a fresh review**

Replace historical checkpoint language with the new live head/run and summarize the trust-boundary and attachment changes. Re-fetch PR metadata, reviews, threads, diff, and same-head workflow conclusion. Review the final diff against the design invariants. If no Critical/High/Medium issue remains, report merge readiness; do not merge unless the user separately asks after this review.
