# Marker-only Changeset Tracking Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse and safely populate a coordinator-created marker-only changeset tracking PR through the approved Issue workflow.

**Architecture:** Keep routing and workflow permissions unchanged. `IssueBaseSyncService` recognizes only the exact canonical marker-only changed-path set, validates the marker, merges the exact trusted base, prepares the approved Issue intake into the candidate, canonically revalidates it, and publishes the same branch with LFS using the existing exact-head replay checks. Populated Draft PR base synchronization remains unchanged.

**Tech Stack:** Python 3.12, Pydantic, pytest, Typer, Git/Git LFS, GitHub Actions, Ruff.

## Global Constraints

- Preserve production Issues #57/#58, failed runs `32005658784`/`32005661511`, tracking PRs #55/#56, and their marker-only heads.
- Never force-push, replace a managed PR or branch, hand-edit generated product/Registry output, or bypass an Environment approval.
- Marker-only classification requires exactly `products/<product-key>/changesets/<changeset-id>.json`; any additional changed path must not enter the new transition.
- Marker content must equal `{"changeset_id": <id>, "product_id": <id>, "status": "required"}` with no missing or extra key.
- Reuse the existing canonical attachment downloader and its redacted security error mapping.
- Recheck the live Draft PR, remote candidate head, and remote default-branch head before local synchronization and again before publication.
- Publish the marker-only transition with `git lfs push` followed by a normal fast-forward branch push.
- Do not retry the production Issues until the code-fix PR is merged at an unchanged green head.
- Keep the unrelated local `.gitignore` modification out of every commit.

---

### Task 1: Reproduce and implement the marker-only transition

**Files:**
- Modify: `tests/unit/test_issue_base_sync_service.py`
- Modify: `src/ard_ossie/application/base_sync.py`

**Interfaces:**
- Consumes: `IssueRequest`, `WorkflowContext.event_path`, `FileSystemPort.root`, `GitPort.changed_paths`, `GitPort.merge_revision`, `GitPort.commit_intake_paths`, and the existing `prepare_existing_intake` canonical validator.
- Produces: marker-only `IssueBaseSyncService.run(context, base_sha=base_sha) -> WorkflowResult` with the unchanged outputs `branch`, `product_key`, `pr_number`, `expected_head`, and `product_id`.

- [ ] **Step 1: Add production-shaped test fixtures**

In `tests/unit/test_issue_base_sync_service.py`, add the exact changeset constants and an Issue body representing the failed production state:

```python
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
TRACKING_BRANCH = f"ard/{CHANGESET_ID}-500138301"
MARKER_PATH = Path(
    f"products/500138301/changesets/{CHANGESET_ID}.json"
)
INTAKE_SHA = "9" * 40


def changeset_issue_body() -> str:
    return f"""### Operation
update

### Product key
500138301

### Existing product ID
{PRODUCT_ID}

### Requested version
2

### Display name
Marketing Insight

### Changeset ID
{CHANGESET_ID}

### Product HTML
[product.html](https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111)

### Semantic document
[semantic.pdf](https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222)

### Data dictionary
[dictionary.xlsx](https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333)

### Change reason
Coordinate the shared table update
"""
```

Change `context(tmp_path)` to `context(tmp_path, *, body: str | None = None)` and write `body or issue_body()` into the event. Add `populate_pristine_tracking_candidate(root)` that reuses the existing v1 candidate fixture and adds only the exact marker payload:

```python
def populate_pristine_tracking_candidate(root: Path) -> None:
    populate_candidate(root)
    marker = root / MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "changeset_id": CHANGESET_ID,
                "product_id": PRODUCT_ID,
                "status": "required",
            }
        )
        + "\n",
        encoding="utf-8",
    )
```

Add `prepare_changeset_candidate(event_path, workspace)` by copying the source-writing loop from
`populate_candidate` and changing only the resulting config and manifest to these exact values:

```python
product = workspace / "products" / "500138301"
manifest = IntakeManifest(
    issue_number=3,
    product_key="500138301",
    product_id=PRODUCT_ID,
    version=2,
    files=files,
)
(product / "product.yaml").write_text(
    "operation: update\n"
    f"product_id: {PRODUCT_ID}\n"
    "product_key: '500138301'\n"
    "base_version: 1\n"
    "version: 2\n"
    "display_name: Marketing Insight\n"
    "description: null\n"
    f"changeset_id: {CHANGESET_ID}\n"
    "tables: []\n",
    encoding="utf-8",
)
(product / "intake-manifest.json").write_text(
    manifest.model_dump_json(),
    encoding="utf-8",
)
return manifest
```

The `files` list must use the same three `DownloadedAttachment` records, contents, relative paths,
filenames, and URLs already constructed by `populate_candidate`; extract that existing loop into a
test-only `write_sources(product) -> list[DownloadedAttachment]` helper and call it from both
preparation helpers.

Extend `BaseSyncGit` with an optional `intake_sha`. When set, `commit_intake_paths` updates `self.current` to that SHA and returns `CommitResult(sha=intake_sha, created=True)`. Make `is_ancestor` accept the configured final SHA without changing the existing default `RESET_SHA` behavior.

- [ ] **Step 2: Write the failing production regression test**

Add this test using the production-shaped helpers:

```python
def test_base_sync_populates_pristine_changeset_tracking_pr(tmp_path: Path) -> None:
    populate_pristine_tracking_candidate(tmp_path)
    git = BaseSyncGit((MARKER_PATH,))
    git.remotes[TRACKING_BRANCH] = CANDIDATE_SHA
    git.intake_sha = INTAKE_SHA
    pull_request = managed_pr(head_branch=TRACKING_BRANCH)

    result = IssueBaseSyncService(
        RepositoryPaths(tmp_path),
        git,
        RouteGitHub(pull_request),
        prepare=prepare_changeset_candidate,
    ).run(
        context(tmp_path, body=changeset_issue_body()),
        base_sha=BASE_SHA,
    )

    assert result.outputs == {
        "branch": TRACKING_BRANCH,
        "product_key": "500138301",
        "pr_number": 5,
        "expected_head": INTAKE_SHA,
        "product_id": PRODUCT_ID,
    }
    assert ("merge_revision", BASE_SHA, "chore(500138301): merge main before reprocessing") in git.operations
    assert (
        "commit_intake_paths",
        "500138301",
        "data(500138301): ingest approved changeset intake",
    ) in git.operations
    assert git.operations[-4:] == [
        ("remote_branch_sha", TRACKING_BRANCH),
        ("remote_branch_sha", "main"),
        ("push", TRACKING_BRANCH, True),
        ("remote_branch_sha", TRACKING_BRANCH),
    ]
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
uv run --frozen pytest \
  tests/unit/test_issue_base_sync_service.py::test_base_sync_populates_pristine_changeset_tracking_pr \
  -q
```

Expected: FAIL with `ISSUE_EXISTING_CONFIG_MISMATCH`. This proves the test reaches the same incorrect `prepare_existing_intake` call as production.

- [ ] **Step 4: Implement the minimal marker-only branch**

In `src/ard_ossie/application/base_sync.py`, import `json`, `AttachmentSecurityError`, `WorkflowValidationError`, and `_error_code` from their existing modules. Immediately after `changed = self.git.changed_paths(...)`, derive:

```python
product_key = str(request.intake.product_key)
marker_path = (
    Path("products")
    / product_key
    / "changesets"
    / f"{request.intake.changeset_id}.json"
    if request.intake.changeset_id is not None
    else None
)
is_pristine_tracking = marker_path is not None and set(changed.paths) == {marker_path}
```

When `is_pristine_tracking` is true, delegate to a private method with this exact shape:

```python
def _populate_pristine_tracking(
    self,
    context: WorkflowContext,
    request: IssueRequest,
    pull_request: PullRequestState,
    *,
    base_sha: str,
    marker_path: Path,
) -> WorkflowResult:
```

The method must:

1. repeat `_require_same_managed_pr` and both remote-head comparisons;
2. call `merge_revision(base_sha, f"chore({product_key}): merge main before reprocessing")`;
3. require `context.event_path` and call `self.prepare(context.event_path, self.paths.root)`;
4. map `AttachmentSecurityError` to `WorkflowSecurityError(_error_code(error), "unsafe issue attachment")` and canonical parse/preparation failures to `WorkflowValidationError(_error_code(error), "invalid issue intake")`;
5. require manifest issue number, product key, version, and product ID to equal the trusted request;
6. call `commit_intake_paths(product_key, f"data({product_key}): ingest approved changeset intake")`;
7. call `prepare_existing_intake` with `event_path=context.event_path` and `runner_temp=context.runner_temp` to byte-revalidate the committed config, manifest, sources, filenames, URLs, sizes, and hashes;
8. require trusted-base ancestry and a clean worktree;
9. repeat the same managed-PR and remote-head checks;
10. call `push(request.branch, lfs=True)` and verify the published remote head equals the intake commit SHA; and
11. return mutations for the created merge/intake commits and the unchanged base-sync output contract.

Task 1 deliberately classifies by exact marker path only. Task 2 adds the required marker-content
validation in a separate RED/GREEN cycle before the branch is eligible for production.

Do not change the existing populated-Draft path except to move the already-derived `product_key` above the branch.

- [ ] **Step 5: Run the new test and verify GREEN**

Run:

```bash
uv run --frozen pytest \
  tests/unit/test_issue_base_sync_service.py::test_base_sync_populates_pristine_changeset_tracking_pr \
  -q
```

Expected: `1 passed`.

- [ ] **Step 6: Run the existing base-sync suite**

Run:

```bash
uv run --frozen pytest tests/unit/test_issue_base_sync_service.py -q
```

Expected: all tests pass, including the existing attachment-token-before-mutation and populated-Draft non-LFS assertions.

- [ ] **Step 7: Commit the happy-path implementation**

```bash
git add src/ard_ossie/application/base_sync.py tests/unit/test_issue_base_sync_service.py
git commit -m "fix: populate marker-only tracking intake"
```

---

### Task 2: Enforce exact marker content and reject ambiguous state

**Files:**
- Modify: `tests/unit/test_issue_base_sync_service.py`
- Modify: `src/ard_ossie/application/base_sync.py`

**Interfaces:**
- Consumes: `_populate_pristine_tracking` and the canonical `marker_path` from Task 1.
- Produces: `_require_tracking_marker(request: IssueRequest, marker_path: Path) -> None` with typed `ISSUE_BASE_SYNC_TRACKING_MARKER_INVALID` and `ISSUE_BASE_SYNC_TRACKING_MARKER_MISMATCH` failures.

- [ ] **Step 1: Write failing malformed and mismatched marker tests**

Add a parameterized test that starts from `populate_pristine_tracking_candidate`, replaces the marker with each payload below, and invokes the marker-only transition:

```python
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("{", "ISSUE_BASE_SYNC_TRACKING_MARKER_INVALID"),
        (
            json.dumps(
                {
                    "changeset_id": CHANGESET_ID,
                    "product_id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632",
                    "status": "required",
                }
            ),
            "ISSUE_BASE_SYNC_TRACKING_MARKER_MISMATCH",
        ),
        (
            json.dumps(
                {
                    "changeset_id": CHANGESET_ID,
                    "product_id": PRODUCT_ID,
                    "status": "required",
                    "extra": True,
                }
            ),
            "ISSUE_BASE_SYNC_TRACKING_MARKER_MISMATCH",
        ),
    ],
)
def test_base_sync_rejects_invalid_pristine_tracking_marker(
    tmp_path: Path,
    payload: str,
    code: str,
) -> None:
    populate_pristine_tracking_candidate(tmp_path)
    (tmp_path / MARKER_PATH).write_text(payload, encoding="utf-8")
    git = BaseSyncGit((MARKER_PATH,))
    git.remotes[TRACKING_BRANCH] = CANDIDATE_SHA
    git.intake_sha = INTAKE_SHA

    with pytest.raises(WorkflowSecurityError, match=code) as captured:
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr(head_branch=TRACKING_BRANCH)),
            prepare=prepare_changeset_candidate,
        ).run(
            context(tmp_path, body=changeset_issue_body()),
            base_sha=BASE_SHA,
        )

    assert captured.value.code == code
    assert not any(
        isinstance(item, tuple)
        and item[0] in {"merge_revision", "commit_intake_paths", "push"}
        for item in git.operations
    )
```

Assert the captured exception code equals `code` and no `merge_revision`, `commit_intake_paths`, or `push` operation occurred.

- [ ] **Step 2: Run the marker tests and verify RED**

Run:

```bash
uv run --frozen pytest \
  tests/unit/test_issue_base_sync_service.py::test_base_sync_rejects_invalid_pristine_tracking_marker \
  -q
```

Expected: at least one case fails because Task 1 has not yet required exact marker content.

- [ ] **Step 3: Implement exact marker validation**

Add this method to `IssueBaseSyncService` and call it before any mutation in `_populate_pristine_tracking`:

```python
def _require_tracking_marker(
    self,
    request: IssueRequest,
    marker_path: Path,
) -> None:
    try:
        marker = json.loads(
            self.paths.resolve_read(marker_path).read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, PathPolicyError) as error:
        raise WorkflowSecurityError(
            "ISSUE_BASE_SYNC_TRACKING_MARKER_INVALID",
            "changeset tracking marker is malformed",
        ) from error
    expected_product_id = request.intake.product_id
    if expected_product_id is None or marker != {
        "changeset_id": request.intake.changeset_id,
        "product_id": str(expected_product_id),
        "status": "required",
    }:
        raise WorkflowSecurityError(
            "ISSUE_BASE_SYNC_TRACKING_MARKER_MISMATCH",
            "changeset tracking marker does not match approved issue input",
        )
```

- [ ] **Step 4: Run the marker tests and verify GREEN**

Run:

```bash
uv run --frozen pytest \
  tests/unit/test_issue_base_sync_service.py::test_base_sync_rejects_invalid_pristine_tracking_marker \
  -q
```

Expected: all parameter cases pass.

- [ ] **Step 5: Add the marker-plus-extra-path regression**

Add a test using `BaseSyncGit((MARKER_PATH, Path("products/500138301/quality/quality-report.json")))` with the old product config. Assert the call raises `ISSUE_EXISTING_CONFIG_MISMATCH` and that no merge, intake commit, or push occurs. This proves an extra path cannot enter the marker-only transition.

- [ ] **Step 6: Run all affected unit and integration tests**

Run:

```bash
uv run --frozen pytest \
  tests/unit/test_issue_base_sync_service.py \
  tests/unit/test_intake_service.py \
  tests/integration/test_workflow_issue_cli.py \
  tests/integration/test_workflow_contracts.py \
  tests/integration/test_base_sync_git.py \
  tests/unit/test_filesystem_adapter.py \
  tests/unit/test_git_cli_adapter.py \
  -q
```

Expected: all tests pass with no workflow-contract or populated-Draft regression.

- [ ] **Step 7: Commit security coverage**

```bash
git add src/ard_ossie/application/base_sync.py tests/unit/test_issue_base_sync_service.py
git commit -m "test: harden marker-only intake transition"
```

---

### Task 3: Verify and publish the code-fix PR

**Files:**
- Verify: `src/ard_ossie/application/base_sync.py`
- Verify: `tests/unit/test_issue_base_sync_service.py`
- Verify: `docs/superpowers/specs/2026-08-17-marker-only-tracking-intake-design.md`
- Verify: `docs/superpowers/plans/2026-08-17-marker-only-tracking-intake.md`

**Interfaces:**
- Consumes: completed Task 1 and Task 2 commits.
- Produces: one unchanged green code-fix PR ready for explicit merge approval.

- [ ] **Step 1: Run formatting and focused static checks**

```bash
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
git diff --check origin/main...HEAD
```

Expected: every command exits `0`.

- [ ] **Step 2: Run the full test suite**

```bash
uv run --frozen pytest -q
```

Expected: all tests pass; record the exact count and duration.

- [ ] **Step 3: Run the integrated repository verifier**

```bash
uv run --frozen ard workflow repository-check \
  --base-ref "$(git rev-parse origin/main)" \
  --head-ref "$(git rev-parse HEAD)" \
  --head-sha "$(git rev-parse HEAD)" \
  --repository . \
  --verification-group static
```

Expected: Ruff, pinned actionlint, schema/catalog, Ossie checksum, and secret-scan groups all succeed.

- [ ] **Step 4: Verify exact PR scope**

```bash
git status --short --branch
git diff --name-only origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected tracked paths are exactly the base-sync implementation, its unit test, the design, and this plan. `.gitignore`, `products/**`, and `registry/**` must not appear.

- [ ] **Step 5: Push and create a Draft PR**

```bash
git push -u origin fix/marker-only-tracking-intake
gh pr create --draft \
  --repo kimohy/ard-ossie-provider \
  --base main \
  --head fix/marker-only-tracking-intake \
  --title "fix: populate marker-only changeset tracking PRs" \
  --body "Fixes the production routing gap where coordinator-created marker-only tracking PRs were sent through existing-intake validation before their approved Issue data had been populated. Preserves exact-head, marker, LFS, and non-force-push boundaries."
```

Capture the PR number, URL, and exact `headRefOid`.

- [ ] **Step 6: Verify immutable CI evidence**

```bash
FIX_PR=$(gh pr view --repo kimohy/ard-ossie-provider --json number --jq .number)
FIX_HEAD=$(gh pr view "$FIX_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)
gh pr diff "$FIX_PR" --repo kimohy/ard-ossie-provider --name-only
gh pr checks "$FIX_PR" --repo kimohy/ard-ossie-provider --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$FIX_HEAD/status"
```

Require `ard/quality-gate` and `ard/changeset` success on the unchanged exact head, no unresolved conversation, and the exact four-file scope from Step 4.

- [ ] **Step 7: Pause for explicit merge approval**

Present the PR URL, exact head, local test count, static-verifier result, required statuses, and the planned retry action. Do not merge until the owner explicitly approves that exact head.

After approval, merge without changing the head, verify the code-only numeric release is a no-op, then remove and reapply `ard:approved` on Issues #57 and #58 and resume Task 4 of `docs/superpowers/plans/2026-08-16-shared-table-changeset-e2e.md`.
