# Draft PR Base Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route an approved Issue with an existing managed Draft PR through a trusted exact-head
base synchronization, discard only same-product derived output, and re-run the reusable processor
against the synchronized head.

**Architecture:** A read-only `IssueRouteService` chooses the unchanged intake path or a new
`IssueBaseSyncService`. The base-sync workflow runs trusted default-branch code from `trusted/`
against a separately checked-out exact candidate in `candidate/`; application code revalidates the
Issue inputs and a narrow reset allowlist, while `GitCli` performs an exact merge, staged restore,
ordinary commit, and non-force push.

**Tech Stack:** Python 3.12, Pydantic 2, Typer, Git CLI, GitHub CLI, GitHub Actions, pytest, uv, Ruff

## Global Constraints

- Keep `RepositoryPaths.is_intake_write_allowed` and `IssueIntakeService`'s existing path decision
  unchanged.
- Execute every base-sync CLI command from a trusted default-branch checkout with
  `PYTHONSAFEPATH=1`; never execute candidate Python, actions, hooks, or workflows.
- Pin route to one exact default-branch SHA and candidate to one exact managed Draft PR head.
- Preserve source/config/manifest paths only after canonical Issue and attachment revalidation.
- Reset only `generated/**`, `quality/**`, two Registry indexes, the validated product/mapping
  records, and Registry table records proved by a strict same-product Registry ownership chain.
- Merge with `--no-ff`, abort conflicts, never force-push, and fail if base or candidate remote head
  moves.
- Invoke the existing `.github/workflows/ard-process.yml` with the synchronized exact head.
- Keep global Draft-PR fan-out, direct-change PRs, code-only PRs, and PR #5 merge out of scope.

---

### Task 1: Trusted Issue Request and Read-only Route

**Files:**
- Create: `tests/unit/test_issue_base_sync_service.py`
- Modify: `src/ard_ossie/application/intake.py`
- Create: `src/ard_ossie/application/base_sync.py`

**Interfaces:**
- Produces: `IssueRequest(event: IssueEvent, intake: IssueIntake, branch: str)`.
- Produces: `load_issue_request(context: WorkflowContext) -> IssueRequest`.
- Produces: `require_managed_pr(pr, branch, base_branch, expected_head=None) -> None`.
- Produces: `IssueRouteService(git: GitPort, github: GitHubPort).run(context) -> WorkflowResult`.
- Produces route outputs `mode`, `base_sha`, `branch`, `product_key`, and, for base sync,
  `pr_number` plus `expected_head`.

- [ ] **Step 1: Write route tests that catch wrong path selection and stale trust state**

Create literal Issue events and real `IssueIntake` parsing. Use fakes that record every Git/GitHub
call, then add these tests:

```python
def test_route_selects_unchanged_intake_when_managed_pr_is_absent(tmp_path: Path) -> None:
    service, git, github, context = route_fixture(tmp_path, pull_request=None)

    result = service.run(context)

    assert result.outputs == {
        "mode": "intake",
        "base_sha": BASE_SHA,
        "branch": "ard/issue-3-500138301",
        "product_key": "500138301",
    }
    assert git.calls == ["current_sha", ("remote_branch_sha", "main")]
    assert github.mutations == []


def test_route_selects_exact_existing_managed_draft(tmp_path: Path) -> None:
    service, _, _, context = route_fixture(tmp_path, pull_request=managed_pr())

    result = service.run(context)

    assert result.outputs["mode"] == "base_sync"
    assert result.outputs["pr_number"] == 5
    assert result.outputs["expected_head"] == CANDIDATE_SHA
```

Add separate cases for remote `main != BASE_SHA`, wrong PR head branch, wrong base branch,
non-Draft PR, and already merged PR. Each expectation must name the stable code
`ISSUE_ROUTE_BASE_MOVED` or `ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH`. Removing the corresponding
validation branch must make one test fail.

- [ ] **Step 2: Run the route tests and verify RED**

Run:

```bash
python -m pytest tests/unit/test_issue_base_sync_service.py -q
```

Expected: collection fails because `ard_ossie.application.base_sync` and the shared Issue request
interfaces do not exist.

- [ ] **Step 3: Extract the shared trusted Issue request without changing intake behavior**

In `intake.py`, expose the existing event parsing and equivalence checks through these concrete
interfaces:

```python
@dataclass(frozen=True)
class IssueRequest:
    event: IssueEvent
    intake: IssueIntake
    branch: str


def load_issue_request(context: WorkflowContext) -> IssueRequest:
    event = load_issue_event(context)
    require_matching_context(context, event)
    intake = parse_issue_intake(event)
    branch = (
        f"ard/{intake.changeset_id}-{intake.product_key}"
        if intake.changeset_id
        else f"ard/issue-{event.number}-{intake.product_key}"
    )
    return IssueRequest(event=event, intake=intake, branch=branch)
```

`parse_issue_intake` must preserve the current `AttachmentSecurityError -> WorkflowSecurityError`
and malformed input `-> WorkflowValidationError` mappings. Update `IssueIntakeService` to consume
`IssueRequest` and `require_managed_pr` while keeping its existing changed-path branch byte-for-byte
equivalent in behavior.

- [ ] **Step 4: Implement the minimal read-only route and verify GREEN**

In `base_sync.py`, implement route in this order:

```python
request = load_issue_request(context)
base_sha = self.git.current_sha()
if self.git.remote_branch_sha(request.event.default_branch) != base_sha:
    raise WorkflowSecurityError("ISSUE_ROUTE_BASE_MOVED", "trusted base moved")
pull_request = self.github.find_open_pr(request.branch)
```

Return `mode="intake"` when absent. When present, call `require_managed_pr` with the derived branch
and base, require `merged_at is None`, and return the exact PR number/head. Run the new route tests,
all `tests/unit/test_intake_service.py`, and Ruff.

- [ ] **Step 5: Commit the read-only route**

```bash
git add src/ard_ossie/application/intake.py src/ard_ossie/application/base_sync.py \
  tests/unit/test_issue_base_sync_service.py
git commit -m "feat: route existing Issue Draft PRs"
```

### Task 2: Narrow Reset Policy and Base-sync Application Service

**Files:**
- Modify: `src/ard_ossie/ports/filesystem.py`
- Modify: `src/ard_ossie/adapters/filesystem.py`
- Modify: `src/ard_ossie/ports/git.py`
- Modify: `src/ard_ossie/application/intake.py`
- Modify: `src/ard_ossie/application/base_sync.py`
- Modify: `tests/unit/test_filesystem_adapter.py`
- Modify: `tests/unit/test_issue_base_sync_service.py`

**Interfaces:**
- Produces:
  `FileSystemPort.is_base_sync_reset_allowed(path, product_key, product_id, table_ids) -> bool`.
- Produces: `GitPort.merge_revision(revision, message) -> CommitResult`.
- Produces: `GitPort.restore_paths(revision, paths: Sequence[Path]) -> None`.
- Produces:
  `prepare_existing_intake(paths, request, prepare, runner_temp) -> IntakeManifest`.
- Produces:
  `IssueBaseSyncService(paths, git, github, prepare=prepare_issue_event).run(context, base_sha) -> WorkflowResult`.

- [ ] **Step 1: Write reset-policy tests and verify RED**

Use literal identifiers and paths, not production helpers, to assert the closed policy:

```python
@pytest.mark.parametrize(
    "path",
    [
        "products/500138301/generated/ossie-model.json",
        "products/500138301/quality/quality-report.json",
        "registry/indexes/product-keys.json",
        "registry/indexes/table-locators.json",
        f"registry/products/{PRODUCT_ID}.json",
        f"registry/mappings/{PRODUCT_ID}.json",
        f"registry/tables/{TABLE_ID}.json",
    ],
)
def test_base_sync_reset_policy_accepts_only_same_product_output(
    repository_paths: RepositoryPaths,
    path: str,
) -> None:
    assert repository_paths.is_base_sync_reset_allowed(
        path, "500138301", PRODUCT_ID, {TABLE_ID}
    )
```

Reject `README.md`, `.github/workflows/ard-process.yml`, another product tree, another product or
mapping ID, an unknown table ID, a third Registry index, `registry/changesets/**`, traversal, and a
symlinked generated directory. Run only these tests and observe the missing-method failure.

- [ ] **Step 2: Implement the exact filesystem policy and verify GREEN**

Normalize each path with `resolve_write`, then compare complete path tuples. Product output
requires at least four components and `parts[:3]` equal to the selected product plus
`generated|quality`. Registry files require exactly three components and one of the literal
identity/index rules. No prefix-only Registry acceptance is permitted.

- [ ] **Step 3: Write base-sync service RED tests**

Build a candidate workspace containing real `product.yaml`, `intake-manifest.json`, approved source
files, `generated/ossie-model.json`, quality files, and Registry files. Inject a deterministic
`prepare` callable that writes canonical source bytes into staging. Add one happy-path test whose
expected call order is literal:

```python
assert git.operations == [
    "is_worktree_clean",
    "current_sha",
    ("remote_branch_sha", "ard/issue-3-500138301"),
    ("remote_branch_sha", "main"),
    ("changed_paths", BASE_SHA, CANDIDATE_SHA),
    ("merge_revision", BASE_SHA, "chore(500138301): merge main before reprocessing"),
    ("restore_paths", BASE_SHA, EXPECTED_RESET_PATHS),
    ("commit_allowed_paths", "500138301", "data(500138301): reset generated outputs after base sync"),
    ("is_ancestor", BASE_SHA, RESET_SHA),
    "is_worktree_clean",
    ("remote_branch_sha", "ard/issue-3-500138301"),
    ("push", "ard/issue-3-500138301", False),
    ("remote_branch_sha", "ard/issue-3-500138301"),
]
```

Assert preserved source/config/manifest paths are never passed to `restore_paths`, and outputs are
the branch, product key, PR number, `expected_head=RESET_SHA`, and product ID.

Add independent failures for dirty worktree, candidate head mismatch, moved PR branch, moved base,
canonical attachment mismatch, code/workflow path, another product, unrelated Registry record,
missing/malformed Registry ownership, candidate product key/ID mismatch, and unlisted Registry
table.
Every case must assert no merge, restore, commit, or push operation occurred.

- [ ] **Step 4: Run the service tests and verify RED**

Run:

```bash
python -m pytest tests/unit/test_issue_base_sync_service.py -q
```

Expected: route tests pass; base-sync tests fail because `IssueBaseSyncService` and the new port
methods are absent.

- [ ] **Step 5: Implement canonical validation reuse and strict output classification**

Move the existing temporary canonicalization block into `prepare_existing_intake` and call it from
both the unchanged intake idempotency path and base-sync service. In base sync:

1. load the trusted Issue request and live managed PR;
2. verify clean/current/remote/base exact heads;
3. compute changed paths with Git's three-dot range from `base_sha` to `live_pr.head_sha`;
4. canonicalize and validate the preserved intake tree;
5. reject non-intake paths outside `is_writeback_allowed`;
6. strictly parse the target Registry product, mapping, and referenced table records;
7. require the Registry product key and ID to match the Issue manifest, every mapping to belong to
   that product, and every table ID and version to match its exact table record;
8. restrict every derived path with `is_base_sync_reset_allowed`;
9. reread live managed PR plus both remote heads, merge, restore every validated intake path from
   the exact candidate head, commit only intake paths, and rerun canonical validation;
10. restore sorted derived paths from the base, commit only writeback paths, and verify
    ancestry/clean state;
11. reread live managed PR plus both remote heads again, push without LFS or force, and verify the
    published remote SHA.

Map malformed Registry ownership parsing to `ISSUE_BASE_SYNC_OUTPUT_REGISTRY_INVALID` and identity
mismatch to `ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH`. A disallowed path uses
`ISSUE_BASE_SYNC_PATH_NOT_ALLOWED` without including file content.

- [ ] **Step 6: Verify GREEN and commit the application boundary**

Run the filesystem, base-sync, intake, model, and Registry unit files plus Ruff. Confirm deleting
the strict table-ID membership check makes the unknown-table test fail, restore it, then commit:

```bash
git add src/ard_ossie/ports/filesystem.py src/ard_ossie/adapters/filesystem.py \
  src/ard_ossie/ports/git.py src/ard_ossie/application/intake.py \
  src/ard_ossie/application/base_sync.py tests/unit/test_filesystem_adapter.py \
  tests/unit/test_issue_base_sync_service.py
git commit -m "feat: validate Draft PR base sync"
```

### Task 3: Exact Git Merge and Restore Transaction

**Files:**
- Modify: `src/ard_ossie/adapters/git_cli.py`
- Modify: `tests/unit/test_git_cli_adapter.py`
- Create: `tests/integration/test_base_sync_git.py`

**Interfaces:**
- Implements: `GitCli.merge_revision(revision: str, message: str) -> CommitResult`.
- Implements: `GitCli.restore_paths(revision: str, paths: Sequence[Path]) -> None`.
- Preserves: `GitCli.push` ordinary fast-forward behavior and `NON_FAST_FORWARD` conflict.

- [ ] **Step 1: Write adapter command-contract tests and verify RED**

For a successful merge, hand-build runner results and assert exact commands:

```python
assert runner.argv == [
    ("git", "rev-parse", "--verify", "HEAD"),
    ("git", "config", "user.name", "github-actions[bot]"),
    ("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"),
    ("git", "merge", "--no-ff", "--no-edit", "--message", "sync", BASE_SHA),
    ("git", "rev-parse", "--verify", "HEAD"),
]
```

Add merge no-op, merge conflict with successful `git merge --abort`, abort failure classified as
`BASE_SYNC_ABORT_FAILED`, worktree restore with sorted explicit paths, empty restore no-op, unsafe
path rejection, and restore failure `BASE_SYNC_RESTORE_FAILED`. Run the two named tests and
observe missing-method failures.

- [ ] **Step 2: Implement minimal merge and restore methods**

Validate the revision and every repository-relative path. Configure local bot identity, run the
exact merge command, abort on nonzero merge, and compare pre/post SHA for `CommitResult.created`.
For restore, call:

```text
git restore --source <base_sha> --worktree -- <sorted explicit paths>
```

The existing `commit_allowed_paths` then validates the complete status and stages exactly those
restored paths. Never invoke shell pipelines, wildcard pathspecs, `reset`, `checkout --`, or force
options. A real Git RED proved that restoring `--staged` first removes base-absent paths from the
index and makes the following explicit `git add -- <deleted path>` fail; worktree-first preserves
the validated commit boundary and produces the same final tree.

- [ ] **Step 3: Verify adapter GREEN**

Run all `tests/unit/test_git_cli_adapter.py` and Ruff. Confirm the existing non-fast-forward push
test remains unchanged and passing.

- [ ] **Step 4: Add a real Git-backed transaction test and verify RED/GREEN**

Create a temporary bare origin and working repositories using a test helper that calls
`subprocess.run(argv, cwd=repository, check=True, text=True, capture_output=True)`.
Commit intake source and derived output on a product branch, advance `main` with a trusted code
file and a cleanly mergeable intake-config change, clone the exact product head, then execute real
`GitCli` merge, restore the approved candidate intake, reset derived output, commit, and push.
Assert with literal Git queries:

```python
assert git(candidate, "merge-base", "--is-ancestor", base_sha, final_sha).returncode == 0
assert read_at(final_sha, "products/500138301/product.yaml") == approved_product_config
assert read_at(final_sha, "products/500138301/sources/product.html") == "approved source\n"
assert file_exists_at(final_sha, "products/500138301/generated/ossie-model.json") is False
assert file_exists_at(final_sha, f"registry/products/{PRODUCT_ID}.json") is False
assert git(candidate, "status", "--porcelain").stdout == ""
assert remote_head(origin, "ard/issue-3-500138301") == final_sha
```

The production changes whose removal must fail this test are the candidate-head intake restore and
the base-head derived restore. Without them, either the merged base silently replaces approved
input or prior processor output remains at the final head.

- [ ] **Step 5: Commit the Git transaction**

```bash
git add src/ard_ossie/adapters/git_cli.py tests/unit/test_git_cli_adapter.py \
  tests/integration/test_base_sync_git.py
git commit -m "feat: merge trusted base before reprocessing"
```

### Task 4: CLI Commands and Trusted Workflow Routing

**Files:**
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `tests/integration/test_workflow_issue_cli.py`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `.github/workflows/ard-issue-intake.yml`

**Interfaces:**
- Produces CLI commands `ard workflow issue-route` and `ard workflow issue-base-sync`.
- Produces result envelopes `.ard/run/workflow.issue-route-result.json` and
  `.ard/run/workflow.issue-base-sync-result.json`.
- Produces workflow jobs `route`, unchanged conditional `intake`, and new conditional `base_sync`.
- Supplies one successful preparation job's exact outputs to reusable `process`.

- [ ] **Step 1: Write CLI RED tests**

Stub the real service boundary, invoke both commands, and assert real CLI effects rather than stub
call counts:

```python
assert github_output.read_text(encoding="utf-8") == (
    f"mode=base_sync\nbase_sha={BASE_SHA}\nbranch=ard/issue-3-500138301\n"
    f"product_key=500138301\npr_number=5\nexpected_head={CANDIDATE_SHA}\n"
)
assert envelope["command"] == "workflow.issue-route"
assert envelope["outputs"]["expected_head"] == CANDIDATE_SHA
```

For base sync, assert `--base-sha` reaches the service and its envelope/GitHub output contains the
new synchronized head. Run the CLI file and observe unknown-command failures.

- [ ] **Step 2: Add the two CLI adapters and verify GREEN**

Add Typer functions that build `WorkflowContext` through `_context`, use
`_issue_route_service(repository_name, paths)` or
`_issue_base_sync_service(repository_name, paths)`, and publish through `_publish`. The base-sync
factory injects `RepositoryPaths`, `GitCli`, `GitHubCli`, and `prepare_issue_event`; no provider or
LLM environment is read.

- [ ] **Step 3: Write parsed-workflow RED tests**

Parse YAML with the repository's `load_workflow` helper and assert:

- `route.needs == "authorize"`, read-only permissions, exact `github.sha` trusted checkout, and
  outputs;
- `intake.needs == "route"` and `if == "needs.route.outputs.mode == 'intake'"`;
- `base_sync.needs == "route"`, contents write plus Issue/PR read only;
- base-sync checkouts use `trusted` at `needs.route.outputs.base_sha` without credentials and
  `candidate` at `needs.route.outputs.expected_head` with `fetch-depth: 0` and LFS;
- every base-sync run step uses `working-directory: trusted`, `PYTHONSAFEPATH=1`, and
  `--repository "$CANDIDATE_REPOSITORY"`;
- `process.needs == ["route", "intake", "base_sync"]`, uses `always()` with exactly one successful
  preparation branch, and selects outputs with `||`;
- finalizer needs route plus both preparation jobs and process;
- all actions remain SHA-pinned and neither route nor base sync references `ARD_LLM_API_KEY`.

Run the single workflow contract and observe failure because `route` and `base_sync` do not exist.

- [ ] **Step 4: Implement the workflow and verify GREEN**

Keep authorization unchanged. Add the read-only route job, condition the current intake job, and
add base sync with separate trusted/candidate checkouts. The reusable process job must use:

```yaml
needs: [route, intake, base_sync]
if: >-
  always() &&
  needs.route.result == 'success' &&
  (needs.intake.result == 'success' || needs.base_sync.result == 'success')
```

Select each processor input from `needs.intake.outputs.<name> ||
needs.base_sync.outputs.<name>`. Expand finalizer needs but keep `UPSTREAM_RESULT` sourced only from
`needs.process.result`. Run CLI, workflow contract, secret contract, all workflow YAML parsing, and
Ruff.

- [ ] **Step 5: Commit the workflow routing**

```bash
git add src/ard_ossie/cli/workflow.py tests/integration/test_workflow_issue_cli.py \
  tests/integration/test_workflow_contracts.py .github/workflows/ard-issue-intake.yml
git commit -m "feat: reprocess synchronized Issue Draft PRs"
```

### Task 5: Full Verification, Review, and Feature PR

**Files:**
- Verify: `docs/superpowers/specs/2026-08-12-draft-pr-base-sync-design.md`
- Verify: `docs/superpowers/plans/2026-08-12-draft-pr-base-sync.md`
- Verify: all changed source, test, and workflow files

**Interfaces:**
- Produces a separate reviewable feature PR based on
  `main@a922a67717e919d8c3051401081433bd4241abc0`.

- [ ] **Step 1: Run focused regression and mutation checks**

Run base-sync service, filesystem, Git adapter, CLI, workflow contract, secret contract, and real
Git transaction tests. Temporarily remove the strict `table_id` membership condition and prove the
unlisted-table test fails; restore it. Temporarily omit the worktree restore call and prove the
real Git transaction test fails; restore it. Rerun the focused set to GREEN.

- [ ] **Step 2: Run the complete repository gate**

Using the explicitly selected Python 3.12 environment and worktree `src` on `PYTHONPATH`, run:

```bash
python -m pytest -q
ruff check .
python -m ard_ossie.application.model_schema_verification --repository .
```

Parse every `.github/workflows/*.yml` with `yaml.safe_load`; run the repository static verifier's
schema catalog, Ossie SHA-256, and secret scan groups; build sdist and wheel into an isolated `/tmp`
directory with the locked environment; inspect wheel metadata and assets. Require a clean worktree
and `git diff --check`.

- [ ] **Step 3: Re-read the approved design and inspect the complete diff**

Check every acceptance criterion. Confirm intake path acceptance is unchanged, no candidate
working-directory runs trusted commands, base sync has no LLM secret, every mutation precedes exact
head checks, output classification is narrower than ordinary writeback, and global fan-out is
absent.

- [ ] **Step 4: Request independent code review and remediate with TDD**

Give the reviewer base `a922a67717e919d8c3051401081433bd4241abc0`, current HEAD, approved design,
plan, complete diff, and verification evidence. Fix every Critical or Important finding with a
fresh failing test, rerun focused verification, rerun the full gate, and request a final Ready
review.

- [ ] **Step 5: Publish a separate Draft PR and verify CI**

Confirm exact changed files, push `agent/draft-pr-base-sync` without force, and open a Draft PR
targeting `main`. Include the `ISSUE_EXISTING_PATH_NOT_ALLOWED` root cause, trust boundaries, path
policy, Git transaction, workflow routing, and exact verification evidence. Require every GitHub
Actions job and required status to succeed at the exact published head, with zero unresolved review
threads.

### Task 6: Apply and Prove PR #5 Reprocessing

**Files:**
- Modify remotely only through reviewed GitHub PR merge and Issue #3 label operations.
- Do not directly edit PR #5 files or force-update its branch.

**Interfaces:**
- Produces the feature merge commit on `main`.
- Produces a new Issue #3 approval run using the merged trusted route.
- Produces a synchronized and reprocessed exact head for Draft PR #5.

- [ ] **Step 1: Merge the feature only under exact-head protection**

After feature CI, required statuses, final review, mergeability, and unresolved-thread checks all
pass, convert the feature PR from Draft, reread its exact head, and squash-merge with expected-head
protection. Verify the resulting `main` SHA contains the feature tree.

- [ ] **Step 2: Trigger one fresh approved-Issue run**

Read Issue #3 and PR #5 live state. Remove only `ard:approved`, then add only `ard:approved` to emit
one fresh event. Record the new workflow run ID and require route output `mode=base_sync`, base SHA
equal to the feature merge, and candidate head equal to PR #5's pre-run live head.

- [ ] **Step 3: Verify synchronization before protected processing**

Require base-sync success, ordinary fast-forward branch update, feature `main` ancestry, unchanged
source/config/manifest blobs, and absence of prior generated/quality/same-product Registry diff at
the synchronized head. If `ard-llm` requires environment approval, stop at the exact job URL and
request only that user approval.

- [ ] **Step 4: Verify the regenerated PR #5 evidence**

After processing completes, require all processor/finalizer jobs and required statuses to succeed
at the final PR head. Inspect generated Ossie/Markdown, quality reports, raw suggestions, and
Registry to prove:

```text
Campaign Count -> COUNT(DISTINCT marketing_campaign.campaign_id)
Modeled Efficiency -> absent from published output and Registry
quality finding -> METRIC_MULTI_DATASET_UNSUPPORTED
quality status -> WARN or stricter, never PASS
```

Confirm PR #5 remains Draft and report its final head, run URL, status conclusions, evidence paths,
and any residual non-blocking warnings.
