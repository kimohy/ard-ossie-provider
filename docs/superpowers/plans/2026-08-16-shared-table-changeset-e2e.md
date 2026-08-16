# Shared-table Changeset Production E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task-by-task in the current session, or `superpowers:executing-plans` if the user chooses inline execution. Use `superpowers:test-driven-development` for Tasks 1 and 2, `superpowers:systematic-debugging` for any unexpected failure, and `superpowers:verification-before-completion` before every completion claim.

**Goal:** Prove the production shared-table changeset lifecycle across two durable synthetic products, including exact-head readiness, atomic release after the final product merge, and later independent clearing of each active changeset binding.

**Architecture:** First repair the two lifecycle edges that the production sequence depends on: reusing a retained coordination branch after its first PR has merged, and treating future readiness versions as an all-or-nothing release no-op. Land those fixes through the repository code gate before touching production data. Then use only the private Issue intake and changeset coordinator workflows to create the second product, coordinate one semantic-only table change, publish the final atomic release, clear the active bindings, and record immutable evidence.

**Tech Stack:** Python 3.12, pytest, Typer, Pydantic, GitHub Actions, `gh`, Git/GitHub commit statuses, Git LFS assets, `openpyxl`, `jq`, `curl`, Ruff, actionlint.

**Approved design:** `docs/superpowers/specs/2026-08-16-shared-table-changeset-e2e-design.md`

---

## Global constraints and fixed identities

- Work only in the existing linked worktree; do not return to the stale root checkout.
- Never combine code/workflow/documentation paths with `products/**` or `registry/**` in one PR.
- Never force-push, move an existing tag, overwrite a managed branch, replace a Release asset, or hand-edit generated product/Registry output.
- Create and update product data only through the public `AI Ready Data submission` Issue workflow.
- Create and update changeset data only through `ARD shared-table changeset coordinator`.
- Apply `ard:approved` only after the exact Issue body and all three attachment hashes are independently verified.
- Merge only an exact PR head with successful `ard/quality-gate` and `ard/changeset` statuses.
- If a pre-merge check fails, keep the Draft PR and run for diagnosis. If an incorrect change reaches `main`, prepare a new revert PR. Never rewrite history.
- Do not record tokens, secrets, provider request bodies, or environment values in the acceptance report.
- Use `apply_patch` for local text-file changes. It is acceptable to use `curl --output` for downloaded binary source assets and `openpyxl` for the one approved XLSX transformation.
- Maintain exact runtime facts in the ignored local file `.ard/run/shared-table-e2e-state.json`. Create or replace that JSON with `apply_patch` only after values are known; never put placeholder strings in it. Rehydrate facts from GitHub or `origin/main` before relying on the file.

Fixed existing identities:

```text
repository: kimohy/ard-ossie-provider
default branch: main
existing product key: 500138301
existing product ID: prd_019ff10c-8be8-79d0-af07-21450abedf9e
existing product starting version: 2
new product key: 500138302
new product display name: Campaign Governance Monitor
target table ID: tbl_01a00585-94b8-7e49-ac43-97e00a165e26
target locator: synthetic_workspace.marketing_insight.marketing_campaign
target table starting version: 1
```

Immutable baseline source assets:

```text
HTML URL: https://github.com/user-attachments/files/30932940/AX.html
HTML baseline SHA-256: ca83c0c02e15d218ecf680043f4a0875ec8146e0190a94b083c498e93dbfbda5
HTML baseline size: 655626
PDF URL: https://github.com/user-attachments/files/30932950/Marketing.Insight.Data.Semantics.pdf
PDF SHA-256 / LFS OID: ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6
PDF size: 114912
XLSX URL: https://github.com/user-attachments/files/30932953/Marketing.Insight.Data.Dictionary.xlsx
XLSX SHA-256 / LFS OID: 10310e99767bc6ae48c1d214fac279565409203900643d553a8033cdddc00274
XLSX size: 14813
```

Before each GitHub mutation, refresh and assert the repository identity:

```bash
gh repo view --json nameWithOwner,defaultBranchRef,deleteBranchOnMerge
git fetch origin main --tags --prune
```

Expected repository response is `kimohy/ard-ossie-provider`, default branch `main`, and `deleteBranchOnMerge: false`. Stop if any value differs.

---

## Task 1: Reuse a merged coordination branch safely

**Files:**

- Modify: `tests/unit/test_changeset_service.py`
- Modify: `src/ard_ossie/application/changesets.py`

### Step 1: Rename the local implementation branch and confirm its scope

The current branch contains only the approved design. Rename it before code changes so the eventual PR clearly carries the lifecycle fix and its design/plan.

```bash
git branch -m fix/shared-table-changeset-lifecycle
git status --short --branch
git diff --name-only origin/main...HEAD
```

Expected current diff: only the approved design and this plan.

### Step 2: Write the failing public-service regression test

Extend `FakeGit` with a default-false ancestor override:

```python
class FakeGit:
    def __init__(self) -> None:
        # Keep the existing branch, SHA, remote, tracking-version, and
        # changed-path fields unchanged.
        self.ancestor_override = False

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ancestor_override
```

Add a test named `test_changeset_ready_reuses_merged_coordination_branch_with_empty_diff`. It must:

1. seed the Registry;
2. run `create_request(tmp_path)` once;
3. capture the `sales-order` tracking PR;
4. remove the central PR entry from `github.prs` to model that the definition PR is no longer open;
5. set `git.changed_override = ()` and `git.ancestor_override = True` to model the merged retained coordination head;
6. call `ready` for the exact tracking head;
7. assert `ready_count == 1`, success, and creation of a replacement central readiness PR.

Use the existing constants and request construction pattern. Do not call the private helper directly.

Also add `test_changeset_rejects_empty_divergent_coordination_branch`. Preclaim the central remote branch, set `changed_override = ()`, leave `ancestor_override = False`, run the public create request, and assert `CHANGESET_COORDINATION_PATH_MISMATCH`. An empty diff alone is not sufficient authority to reuse a branch.

### Step 3: Prove the test fails for the intended reason

```bash
uv run --frozen pytest tests/unit/test_changeset_service.py::test_changeset_ready_reuses_merged_coordination_branch_with_empty_diff -q
```

Expected: failure with `CHANGESET_COORDINATION_PATH_MISMATCH`. If it fails earlier, fix the test fixture rather than changing production code.

### Step 4: Implement the minimal branch acceptance rule

In `_switch_existing_scoped_branch`, preserve the exact-head check, then accept either the exact expected path set or the merged-empty case:

```python
changed = self.git.changed_paths(base_branch, existing_head)
if set(changed.paths) == expected_paths:
    return
if not changed.paths and self.git.is_ancestor(existing_head, base_branch):
    return
raise WorkflowSecurityError(
    code,
    "existing managed branch contains unexpected committed paths",
)
```

Do not switch, reset, delete, or force-update the branch. The retained head must be both empty against the base and an ancestor of the base.

### Step 5: Run focused positive and negative tests

```bash
uv run --frozen pytest \
  tests/unit/test_changeset_service.py::test_changeset_ready_reuses_merged_coordination_branch_with_empty_diff \
  tests/unit/test_changeset_service.py::test_changeset_rejects_empty_divergent_coordination_branch \
  tests/unit/test_changeset_service.py::test_changeset_rejects_preclaimed_coordination_branch_with_code \
  tests/unit/test_changeset_service.py -q
```

Expected: all pass. The existing unexpected-path test must remain fail-closed.

### Step 6: Commit the branch-reuse fix

```bash
git add src/ard_ossie/application/changesets.py tests/unit/test_changeset_service.py
git diff --cached --check
git commit -m "fix: reuse merged changeset coordination branch"
```

---

## Task 2: Defer future readiness atomically

**Files:**

- Modify: `tests/unit/test_release_detection_service.py`
- Modify: `src/ard_ossie/application/release_detection.py`

### Step 1: Generalize the release-detection fixture

Replace the `stale: bool` helper argument with explicit version tuples:

```python
def build_repository(
    tmp_path: Path,
    *,
    current_versions: tuple[int, int] = (1, 1),
    readiness_versions: tuple[int, int] = (1, 1),
) -> None:
```

Use `current_versions` in both `ProductRecord` values and both `product.yaml` files. Use `readiness_versions` in the two `mark_ready` calls. Update the stale test to call:

```python
build_repository(
    tmp_path,
    current_versions=(2, 1),
    readiness_versions=(1, 1),
)
```

### Step 2: Write all-future and mixed-current/future failing tests

Add these two tests:

```python
def test_detect_defers_changeset_when_all_readiness_versions_are_future(
    tmp_path: Path,
) -> None:
    build_repository(
        tmp_path,
        current_versions=(1, 1),
        readiness_versions=(2, 2),
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((Path(f"registry/changesets/{CHANGESET_ID}.json"),)),
    )

    result = service.run(request(tmp_path))

    assert result.status is WorkflowStatus.NOOP
    assert result.outputs["products"] == []
    assert result.outputs["tables"] == []


def test_detect_defers_whole_changeset_when_one_product_is_future(
    tmp_path: Path,
) -> None:
    build_repository(
        tmp_path,
        current_versions=(2, 1),
        readiness_versions=(2, 2),
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit(
            (
                Path("products/sales-order/generated/ossie-model.json"),
                Path(f"registry/tables/{TABLE_ID}.json"),
            )
        ),
    )

    result = service.run(request(tmp_path))

    assert result.status is WorkflowStatus.NOOP
    assert result.outputs["products"] == []
    assert result.outputs["tables"] == []
```

The mixed test deliberately includes the changed table path. It guards against a partial table result after the first product merge.

### Step 3: Prove both tests fail for the version mismatch

```bash
uv run --frozen pytest \
  tests/unit/test_release_detection_service.py::test_detect_defers_changeset_when_all_readiness_versions_are_future \
  tests/unit/test_release_detection_service.py::test_detect_defers_whole_changeset_when_one_product_is_future -q
```

Expected: both currently raise `CHANGESET_VERSION_NOT_CURRENT`.

### Step 4: Implement a preflighted, all-or-nothing expansion

Change `_expand_changeset` to return `bool`. First resolve and validate every required product without mutating either output set:

```python
@staticmethod
def _expand_changeset(
    registry: Registry,
    changeset: ChangeSetRecord,
    products: set[str],
    tables: set[str],
) -> bool:
    resolved_products: list[ProductRecord] = []
    has_future_version = False
    for product_id in changeset.required_product_ids:
        readiness = changeset.ready_products.get(product_id)
        product = registry.get_product(product_id)
        if product is None or readiness is None:
            raise WorkflowConflict(
                "CHANGESET_REGISTRY_REFERENCE_MISSING",
                f"changeset references a missing product: {product_id}",
            )
        if readiness.version < product.version:
            raise WorkflowConflict(
                "CHANGESET_VERSION_NOT_CURRENT",
                f"{product_id}:v{readiness.version} < v{product.version}",
            )
        if readiness.version > product.version:
            has_future_version = True
        resolved_products.append(product)

    if has_future_version:
        return False

    for product in resolved_products:
        products.add(product.product_key)
    for table_id in changeset.table_ids:
        ReleaseDetectionService._require_table(registry, table_id)
        tables.add(table_id)
    return True
```

At the call site, collect table IDs held by a future changeset and subtract them after all changesets are considered:

```python
deferred_table_ids: set[str] = set()
# In the existing sorted changeset loop, keep the missing/blocked checks.
expanded = self._expand_changeset(
    registry,
    changeset,
    expanded_products,
    table_ids,
)
if not expanded:
    deferred_table_ids.update(changeset.table_ids)

table_ids.difference_update(deferred_table_ids)
```

Keep this ordering before the output payload is constructed. This makes direct table-path detection subordinate to an incomplete active changeset and prevents partial release output.

### Step 5: Run the complete release-detection contract

```bash
uv run --frozen pytest tests/unit/test_release_detection_service.py -q
```

Expected:

- all-future: successful no-op, no products or tables;
- mixed current/future: successful no-op, no products or tables;
- fully current: both products plus the table;
- readiness lower than current: `CHANGESET_VERSION_NOT_CURRENT`;
- direct non-changeset product behavior unchanged.

### Step 6: Commit the release-defer fix

```bash
git add src/ard_ossie/application/release_detection.py tests/unit/test_release_detection_service.py
git diff --cached --check
git commit -m "fix: defer future changeset releases"
```

---

## Task 3: Lock the CLI/documentation contract and land the lifecycle fix

**Files:**

- Modify: `tests/integration/test_workflow_release_detect_cli.py`
- Modify: `docs/github-actions-setup.md`

### Step 1: Add the empty-matrix CLI regression

Make the stub accept configurable status and outputs, or add a dedicated no-op stub. Add `test_workflow_release_detect_writes_empty_json_matrix_outputs`, returning:

```python
WorkflowResult(
    command="workflow.release-detect",
    status=WorkflowStatus.NOOP,
    outputs={"products": [], "tables": []},
)
```

Assert exit code `0`, result-envelope status `noop`, and exact GitHub outputs:

```text
products=[]
tables=[]
```

### Step 2: Clarify the operator contract

In `docs/github-actions-setup.md` section 7, replace the final release paragraph with wording that says:

- a readiness version greater than current is an expected future transition and release detection returns no targets;
- if any required product is future, the whole changeset is deferred;
- equality for all products expands the complete product/table set;
- a readiness version lower than current remains a hard stale-state failure;
- exact-head ancestry checks still happen at publication time.

Do not change any protection or Environment guidance.

### Step 3: Run focused and complete local verification

```bash
uv run --frozen pytest \
  tests/unit/test_changeset_service.py \
  tests/unit/test_release_detection_service.py \
  tests/integration/test_workflow_changeset_cli.py \
  tests/integration/test_workflow_release_detect_cli.py \
  tests/integration/test_workflow_contracts.py -q
uv run --frozen pytest -q
uv run --frozen ruff check src tests
actionlint .github/workflows/*.yml
```

All commands must exit `0`. Record exact test counts and versions in the implementation report.

### Step 4: Commit the contract update

```bash
git add docs/github-actions-setup.md tests/integration/test_workflow_release_detect_cli.py
git diff --cached --check
git commit -m "docs: define future changeset release behavior"
```

### Step 5: Review and publish the code PR

Inspect the entire PR diff before pushing:

```bash
git status --short --branch
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
git log --oneline origin/main..HEAD
git push -u origin fix/shared-table-changeset-lifecycle
gh pr create --draft \
  --base main \
  --head fix/shared-table-changeset-lifecycle \
  --title "fix: support pre-merge changeset readiness" \
  --body "Implements the approved shared-table changeset lifecycle design. Adds fail-closed retained-branch reuse and atomic deferral for future readiness versions, with local regression coverage."
```

Capture the PR number and exact head. Confirm changed paths contain no `products/**` or `registry/**` files.

### Step 6: Wait for the exact-head repository gate and merge

```bash
CODE_PR=$(gh pr view --json number --jq .number)
CODE_HEAD=$(gh pr view "$CODE_PR" --json headRefOid --jq .headRefOid)
gh pr checks "$CODE_PR" --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$CODE_HEAD/status"
```

Verify `ard/quality-gate` and `ard/changeset` both point to `CODE_HEAD` and are successful. Then mark ready and merge through the protected branch:

```bash
gh pr ready "$CODE_PR"
gh pr merge "$CODE_PR" --merge
git fetch origin main --tags
gh pr view "$CODE_PR" --json state,mergedAt,mergeCommit,headRefOid,url
```

Stop if GitHub reports a different head, a merge queue, or an unsuccessful required status. Production E2E is gated on this PR being merged.

---

## Task 4: Create and release the durable second product

**Files:**

- Create locally during the phase: `.ard/run/shared-table-e2e-state.json` (ignored runtime facts, actual values only)
- No tracked repository files are edited directly

### Step 1: Materialize and verify the three baseline sources

Create a narrowly scoped temporary directory and download the immutable Issue assets:

```bash
E2E_SOURCE_DIR=$(mktemp -d)
curl --fail --location --proto '=https' \
  --output "$E2E_SOURCE_DIR/campaign-governance-monitor.html" \
  'https://github.com/user-attachments/files/30932940/AX.html'
curl --fail --location --proto '=https' \
  --output "$E2E_SOURCE_DIR/Marketing.Insight.Data.Semantics.pdf" \
  'https://github.com/user-attachments/files/30932950/Marketing.Insight.Data.Semantics.pdf'
curl --fail --location --proto '=https' \
  --output "$E2E_SOURCE_DIR/Marketing.Insight.Data.Dictionary.xlsx" \
  'https://github.com/user-attachments/files/30932953/Marketing.Insight.Data.Dictionary.xlsx'
sha256sum "$E2E_SOURCE_DIR"/*
stat -c '%n %s' "$E2E_SOURCE_DIR"/*
file "$E2E_SOURCE_DIR"/*
```

Require the exact baseline hashes/sizes listed in the global constraints, `%PDF` for the PDF, and ZIP/XLSX identification for the workbook.

### Step 2: Make the seed HTML product-specific

Use `apply_patch` to change only public product metadata in the downloaded HTML. The visible or meta description must be exactly:

```text
이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석을 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
```

Set the title/display metadata to `Campaign Governance Monitor`. Do not alter embedded data, schema locators, or metric definitions. Record the resulting HTML SHA-256 and size.

### Step 3: Create the public seed Issue with browser upload

Use the `gstack:browse` skill for the GitHub Issue form because `gh issue create` cannot attach local files. Submit exactly:

```text
Issue title: [ARD] Campaign Governance Monitor
Public content acknowledgement: checked
Operation: create
Product key: 500138302
Existing product ID: empty
Requested version: 1
Display name: Campaign Governance Monitor
Description: the exact Korean description from Step 2
Changeset ID: empty
Product HTML: campaign-governance-monitor.html upload
Semantic document: Marketing.Insight.Data.Semantics.pdf upload
Data dictionary: Marketing.Insight.Data.Dictionary.xlsx upload
Change reason: 공유 테이블 changeset 운영 검증을 위한 두 번째 영구 합성 제품을 생성합니다.
```

After submission, capture the Issue number and inspect the body without changing it:

```bash
SEED_ISSUE=$(gh issue list --state open --search 'in:title [ARD] Campaign Governance Monitor' --json number,createdAt --jq 'sort_by(.createdAt) | last | .number')
gh issue view "$SEED_ISSUE" --json number,url,title,body,labels,state
```

Download all three attachment URLs from the rendered Issue body, hash them, and require exact equality with the local upload files. Only then approve:

```bash
gh issue edit "$SEED_ISSUE" --add-label ard:approved
```

### Step 4: Verify trusted intake and processing before merge

Watch the Issue intake run and identify the one Draft PR that closes the seed Issue. Capture its exact head. Verify:

- allowed diff is limited to `products/500138302/**` and the product/locator-matched Registry records;
- generated product ID is UUIDv7 and remains stable;
- `product.yaml` is create/v1 with null changeset;
- validation status is `verified`, `publishable` is true, and hard errors are `0`;
- four product mappings point to the four pre-existing table IDs at version 1;
- no fifth table record exists and all four existing table blobs are byte-identical to `origin/main`;
- `ard/quality-gate` and `ard/changeset` both succeed on the exact processor head.

Use immutable commit reads for evidence, for example:

```bash
SEED_PR=$(gh pr list --state open --json number,body --jq ".[] | select(.body | contains(\"Closes #$SEED_ISSUE\")) | .number" | head -n 1)
SEED_HEAD=$(gh pr view "$SEED_PR" --json headRefOid --jq .headRefOid)
gh pr diff "$SEED_PR" --name-only
gh api "repos/kimohy/ard-ossie-provider/commits/$SEED_HEAD/status"
```

Read the new product ID from `registry/products/*.json` at `SEED_HEAD`; do not invent it locally.

### Step 5: Merge and verify the v1 immutable release

Merge only the exact verified head, wait for `ARD numeric release`, and approve the `production-linkage` deployment when prompted:

```bash
gh pr ready "$SEED_PR"
gh pr merge "$SEED_PR" --merge
gh pr view "$SEED_PR" --json mergedAt,mergeCommit,headRefOid,url
```

Verify product tag `product/<new-product-id>/v1`, the corresponding GitHub Release, bundle assets/digests, and successful downstream linkage. Assert no table v2 tag exists and all four table Registry records remain version 1.

### Step 6: Record phase facts

Create `.ard/run/shared-table-e2e-state.json` with `apply_patch`, containing only actual values for:

- seed Issue number and three attachment hashes;
- seed PR number/head/merge commit;
- new product ID;
- seed release run, product v1 tag, Release URL, asset digests, and linkage status;
- the four table IDs and their pre-seed blob hashes.

Do not add fields whose values are not known yet.

---

## Task 5: Publish the initial changeset definition

**Files:**

- Modify locally: `.ard/run/shared-table-e2e-state.json` (actual runtime facts only)
- No tracked repository files are edited directly

### Step 1: Rehydrate current identities and generate one changeset ID

Read the new product ID back from `origin/main` and verify it equals the state file. Generate the changeset UUIDv7 using the project helper:

```bash
NEW_PRODUCT_ID=$(jq -er .new_product_id .ard/run/shared-table-e2e-state.json)
CHANGESET_ID=$(uv run --frozen python -c 'from ard_ossie.ids import new_id; print(new_id("cst"))')
printf '%s\n' "$CHANGESET_ID"
```

Validate it against `^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`. Add the actual ID to the state JSON with `apply_patch` before dispatching.

### Step 2: Dispatch create mode

```bash
gh workflow run ard-changeset.yml \
  --ref main \
  -f mode=create \
  -f changeset_id="$CHANGESET_ID" \
  -f table_ids=tbl_01a00585-94b8-7e49-ac43-97e00a165e26 \
  -f product_ids="prd_019ff10c-8be8-79d0-af07-21450abedf9e,$NEW_PRODUCT_ID"
```

Capture the run selected by workflow name, `event=workflow_dispatch`, `headBranch=main`, and creation time after the dispatch. Wait for conclusion success.

### Step 3: Verify the three managed PRs

Derive branches exactly:

```bash
CENTRAL_BRANCH="ard/changeset-$CHANGESET_ID"
EXISTING_TRACKING_BRANCH="ard/$CHANGESET_ID-500138301"
NEW_TRACKING_BRANCH="ard/$CHANGESET_ID-500138302"
```

Verify:

- central PR changes only `registry/changesets/$CHANGESET_ID.json`;
- central JSON has exactly the target table ID, both product IDs, empty readiness, and blocked state;
- each Draft tracking PR changes only its own marker at `products/500138301/changesets/$CHANGESET_ID.json` or `products/500138302/changesets/$CHANGESET_ID.json`;
- each marker has the exact changeset/product pair and `status: required`;
- both tracking heads have `ard/changeset: pending`;
- central head has both required statuses successful.

Capture PR numbers and exact heads in the state file.

### Step 4: Merge only the initial central definition

```bash
gh pr ready "$DEFINITION_PR"
gh pr merge "$DEFINITION_PR" --merge
```

Wait for the push-triggered numeric release workflow. Its detect job must succeed with `products=[]` and `tables=[]`, with release/linkage jobs skipped. Confirm no numeric tag or Release was created by this merge. Record definition merge commit and release run/result.

---

## Task 6: Populate both tracking PRs with one identical table change

**Files:**

- Modify locally during authoring: one temporary XLSX binary
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository files are edited directly

### Step 1: Prepare one canonical modified XLSX

Download the baseline XLSX into a fresh temporary directory and verify baseline SHA/size. Use `openpyxl` to locate the row by semantic headers and values, not by a hard-coded cell coordinate:

- table name: `marketing_campaign`;
- column name: `campaign_status`;
- description field: the dictionary's column-description header.

Replace only that description with exactly:

```text
합성 캠페인 상태이며 Draft, Active, Paused, Closed 중 하나입니다.
```

Save once, then use that same file for both Issues. Re-open the saved workbook and assert:

- exactly one logical cell differs from baseline;
- all worksheet names, dimensions, merged ranges, locators, column names, types, key/nullability values, and other descriptions are unchanged;
- ZIP/XLSX signature remains valid.

Record modified XLSX SHA-256, size, and the anticipated LFS OID (the same SHA-256).

### Step 2: Prepare product-specific complete sources

For `500138301`, use the exact current v2 product HTML from its accepted Issue attachment or materialized source at the current product commit, plus the unchanged PDF and the modified XLSX.

For `500138302`, use its exact current v1 product HTML from the seed Issue attachment, plus the unchanged PDF and the same modified XLSX.

Verify both PDF hashes equal the fixed PDF baseline and both XLSX hashes equal each other. Do not reuse the original v1 HTML for `500138301` because its current v2 metadata must be preserved.

### Step 3: Create two private update Issues

Use the `gstack:browse` skill to upload files through the Issue form.

Existing product Issue:

```text
Operation: update
Product key: 500138301
Existing product ID: prd_019ff10c-8be8-79d0-af07-21450abedf9e
Requested version: 3
Display name/description: exact current accepted values
Changeset ID: actual generated changeset ID
Product HTML: exact current v2 HTML upload
Semantic document: unchanged verified PDF upload
Data dictionary: canonical modified XLSX upload
Change reason: campaign_status 합성 상태값의 허용 범위를 Data Dictionary에 명시합니다.
```

New product Issue:

```text
Operation: update
Product key: 500138302
Existing product ID: actual generated new product ID
Requested version: 2
Display name/description: exact current accepted values
Changeset ID: actual generated changeset ID
Product HTML: exact current v1 HTML upload
Semantic document: unchanged verified PDF upload
Data dictionary: the same canonical modified XLSX upload
Change reason: campaign_status 합성 상태값의 허용 범위를 Data Dictionary에 명시합니다.
```

Capture both Issue numbers, inspect their bodies, download their attachments, and verify identities plus all hashes. Approve only after both are exact:

```bash
gh issue edit "$EXISTING_TRACKING_ISSUE" --add-label ard:approved
gh issue edit "$NEW_TRACKING_ISSUE" --add-label ard:approved
```

### Step 4: Verify intake reuses the two canonical tracking PRs

The approved Issue workflows must populate the two already-open Draft PRs from Task 5. Stop if either creates a replacement PR or changes the canonical branch name.

For each final processor head, verify:

- PR number is unchanged from Task 5 and head SHA advanced;
- marker is preserved;
- `product.yaml` is update with exact current `base_version`, next version, and active changeset ID;
- target table base/proposed version is 1/2;
- the other three table base/proposed versions remain 1/1;
- validation is verified/publishable with zero hard errors;
- exact-head quality status succeeds;
- `ard/changeset` remains pending until both readiness records are present.

Cross-compare the two heads:

```bash
git fetch origin "$EXISTING_TRACKING_BRANCH" "$NEW_TRACKING_BRANCH"
git show "$EXISTING_TRACKING_HEAD:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json" | sha256sum
git show "$NEW_TRACKING_HEAD:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json" | sha256sum
```

Require identical target table v2 blobs, identical modified XLSX LFS OIDs, unchanged schema hash, changed canonical hash, and byte-identical Registry blobs for the other three tables.

Do not merge either product PR and do not merge `main` into either head after readiness begins. Record final exact heads and evidence in the state file.

---

## Task 7: Accumulate readiness and prove the atomic coordinated release

**Files:**

- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository files are edited directly

### Step 1: Mark the existing product head ready

```bash
gh workflow run ard-changeset.yml \
  --ref main \
  -f mode=ready \
  -f changeset_id="$CHANGESET_ID" \
  -f product_id=prd_019ff10c-8be8-79d0-af07-21450abedf9e \
  -f version=3 \
  -f pr_number="$EXISTING_TRACKING_PR" \
  -f head_sha="$EXISTING_TRACKING_HEAD"
```

Wait for success. Inspect the result artifact/log and central JSON at the new coordination head. Require `ready_count=1`, `required_count=2`, state blocked, and both tracking statuses pending.

### Step 2: Mark the new product head ready

```bash
gh workflow run ard-changeset.yml \
  --ref main \
  -f mode=ready \
  -f changeset_id="$CHANGESET_ID" \
  -f product_id="$NEW_PRODUCT_ID" \
  -f version=2 \
  -f pr_number="$NEW_TRACKING_PR" \
  -f head_sha="$NEW_TRACKING_HEAD"
```

Require `ready_count=2`, `required_count=2`, state ready, and successful `ard/changeset` on both exact tracking heads. Verify the new readiness PR changes only the central changeset JSON and records the two exact PR/version/head triples.

### Step 3: Merge readiness before products and verify future no-op

Merge the readiness PR after its exact head statuses succeed. Wait for numeric release detection and require:

```text
status: noop
products: []
tables: []
release job: skipped
linkage job: skipped
```

No new numeric tag or Release may exist. Record the readiness merge commit and run result.

### Step 4: Merge the first product and verify mixed future no-op

Re-read the existing product tracking PR and ensure `headRefOid` still equals the recorded ready head. Merge it. Wait for numeric release detection and again require empty product/table outputs, skipped release/linkage jobs, and no new numeric tags or Releases.

This is the production proof that a directly changed table path is removed from the output while one required product remains future.

### Step 5: Merge the second product and verify the full expansion

Re-read the new product tracking PR and ensure its head is unchanged. Merge it. Its merge commit is the required coordinated release commit.

The release-detect output must be exactly:

```text
products: ["500138301", "500138302"]
tables: ["tbl_01a00585-94b8-7e49-ac43-97e00a165e26"]
```

Approve both `production-linkage` deployments. Verify:

- existing product becomes v3;
- new product becomes v2;
- target table becomes v2;
- other three tables remain v1;
- `product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v3` targets the final merge commit;
- `product/<new-product-id>/v2` targets the final merge commit;
- `table/tbl_01a00585-94b8-7e49-ac43-97e00a165e26/v2` targets the final merge commit;
- both Releases contain the expected assets and recorded SHA-256 digests;
- publication verifies both exact PR heads, merged states, and merge-commit ancestry;
- downstream linkage succeeds for both products.

### Step 6: Re-run the final release workflow and prove convergence

Read the final release run ID from the state file and rerun that exact workflow:

```bash
FINAL_RELEASE_RUN=$(jq -er .final_release_run_id .ard/run/shared-table-e2e-state.json)
gh run rerun "$FINAL_RELEASE_RUN"
```

Wait for completion and approve linkage if GitHub requests it again. Require every tag operation, Release asset operation, and downstream dispatch to converge without changing target commits or asset digests. Record original and rerun IDs/results.

---

## Task 8: Clear both active changeset bindings independently

**Files:**

- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository files are edited directly

### Step 1: Prepare exact non-table descriptions and matching HTML

Existing product v4 description:

```text
이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습, 상태별 비교를 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
```

New product v3 description:

```text
이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석과 상태별 비교를 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
```

For each product, materialize its current accepted HTML, change only its matching public description metadata with `apply_patch`, and retain the current semantic PDF and modified XLSX unchanged. Record every source hash.

### Step 2: Submit and approve the existing product v4 update

Create a private update Issue with the exact existing ID, version 4, the first description, matching HTML, unchanged current PDF/XLSX, and an empty Changeset ID. Use change reason:

```text
독립 제품 설명을 보강하고 완료된 changeset의 활성 연결을 해제합니다.
```

Verify body/attachments, then apply `ard:approved`. Before merge require:

- `changeset_id: null` in `product.yaml`;
- product-only generated/quality/Registry changes;
- no target or other table Registry changes;
- exact-head required statuses successful.

Merge and verify release detection contains only `500138301`, no tables, and the new immutable v4 product tag/Release. Confirm the old changeset JSON and both markers still exist.

### Step 3: Submit and approve the new product v3 update

Repeat with the exact new product ID, version 3, the second description, matching HTML, unchanged current PDF/XLSX, empty Changeset ID, and the same change reason.

Merge only the exact green head. Verify release detection contains only `500138302`, no tables, and the new immutable v3 product tag/Release.

### Step 4: Verify the final durable state

At the final `main` commit require:

- existing product current version 4 and `changeset_id: null`;
- new product current version 3 and `changeset_id: null`;
- target table remains v2;
- other three tables remain v1;
- no table v3 tag exists;
- central changeset JSON still contains completed readiness evidence;
- both marker files still exist;
- neither independent release expanded the historical changeset.

Record both clear Issue/PR/head/merge/run/release/linkage facts in the state file.

---

## Task 9: Publish acceptance evidence and close only the completed roadmap item

**Files:**

- Create: `docs/acceptance/shared-table-changeset-e2e-verification.md`
- Modify: `docs/next-steps.md`

### Step 1: Start a documentation-only branch from current main

After all production mutations have converged:

```bash
git fetch origin main --tags --prune
git switch -c docs/shared-table-changeset-e2e-verification origin/main
git status --short --branch
```

Do not carry ignored temporary sources into the commit.

### Step 2: Write the acceptance report from immutable evidence

Create `docs/acceptance/shared-table-changeset-e2e-verification.md` with `apply_patch`. Include actual values and direct GitHub links for:

- local lifecycle-fix test commands and exact pass counts;
- lifecycle code PR/head/merge commit and repository-gate runs;
- seed Issue/attachments/hashes, PR/head/merge, generated product ID, Registry/mapping hashes, tag/Release/assets/linkage;
- changeset ID, definition run/PR/head/merge and two initial tracking PRs/heads;
- both update Issues and attachment hashes, final tracking heads, equal XLSX LFS OIDs, equal target table blobs;
- target table schema hash before/after and canonical hash before/after;
- byte-identical other-table Registry hashes;
- first `1/2` blocked and second `2/2` ready dispatch results;
- readiness no-op, first-product no-op, final full expansion, and rerun convergence;
- three final tags all targeting the final product merge commit;
- both independent clear Issues/PRs/releases and product-only detection;
- final product/table versions, null active bindings, and preserved historical JSON/markers;
- any failed intermediate run, its diagnosis, and the final converged state.

Every SHA, ID, run, PR, tag, Release, asset digest, and URL must be real and reproducible. Do not write `TBD`, `TODO`, ellipses, secrets, or provider payloads.

### Step 3: Update only the shared-table roadmap bullets

In `docs/next-steps.md`, check the five Shared-table changeset E2E bullets only after the report contains direct evidence for each one. Add a link to the new report beside that subsection. Leave operations protection, failure drills, representative-PDF stabilization, and P3 entries unchanged.

### Step 4: Re-run final local verification on the final documentation branch

```bash
uv sync --frozen
uv run --frozen pytest \
  tests/unit/test_changeset_service.py \
  tests/unit/test_release_detection_service.py \
  tests/integration/test_workflow_changeset_cli.py \
  tests/integration/test_workflow_release_detect_cli.py \
  tests/integration/test_workflow_contracts.py -q
uv run --frozen pytest -q
uv run --frozen ruff check src tests
actionlint .github/workflows/*.yml
git diff --check
```

All commands must exit `0`. Cross-check every report identifier against GitHub one final time.

### Step 5: Commit, review, and land the evidence PR

```bash
git add docs/acceptance/shared-table-changeset-e2e-verification.md docs/next-steps.md
git diff --cached --check
git commit -m "docs: verify shared-table changeset e2e"
git push -u origin docs/shared-table-changeset-e2e-verification
gh pr create --draft \
  --base main \
  --head docs/shared-table-changeset-e2e-verification \
  --title "docs: verify shared-table changeset e2e" \
  --body "Records immutable evidence for the completed two-product shared-table changeset lifecycle and updates only its roadmap checklist."
```

Confirm the PR changes exactly the two documentation files, wait for both exact-head required statuses, mark ready, and merge through the protected branch. Finally run:

```bash
git fetch origin main --tags
git status --short --branch
gh pr view --json state,mergedAt,mergeCommit,headRefOid,url
```

### Step 6: Completion gate

Invoke `superpowers:verification-before-completion`. Completion may be claimed only when:

1. all local commands above are freshly green;
2. both documentation files are on `origin/main`;
3. the acceptance report contains no placeholders or secrets;
4. the five roadmap bullets are checked and no unrelated bullet changed;
5. all GitHub tags/Releases/PRs/runs described in the report still match their immutable commits and digests.
