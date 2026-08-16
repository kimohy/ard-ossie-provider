# Direct Branch `v2` Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advance the synthetic Marketing Insight product from `v1` to `v2` through the trusted direct-branch workflow, prove that all four unchanged tables remain at `v1`, and preserve success and rejection evidence.

**Architecture:** Keep design, plan, and evidence documents on the planning branch while a clean branch from `origin/main` carries only one product's metadata and HTML source change. Let the default-branch `workflow_run` coordinator create the Draft PR and let the trusted processor write generated, quality, and Registry output. Verify the merged release, idempotent retry, stale/gap rejections, and fork identity guard before publishing the acceptance record.

**Tech Stack:** Git worktrees, Git/Git LFS pointer format, Python 3.12, uv, pytest, Ruff, ARD CLI, jq, GitHub Actions, GitHub CLI

## Global Constraints

- The successful candidate branch is exactly `acceptance/marketing-insight-v2` and starts from the current `origin/main`.
- The authored candidate diff contains only `products/500138301/product.yaml` and `products/500138301/sources/product-info/product.html`.
- Product identity stays `prd_019ff10c-8be8-79d0-af07-21450abedf9e`; `base_version` is `1`; proposed `version` is `2`.
- Semantic PDF and dictionary XLSX Git LFS pointers are byte-identical to `origin/main`.
- No generated, quality, mapping, index, or Registry record is edited by hand.
- All four existing table IDs and versions remain unchanged at `v1`; no table `v2` tag is created.
- All 11 existing metric IDs remain stable and relationships remain empty.
- Candidate or workflow failure never authorizes force push, tag movement, generated-file repair, or branch deletion.
- Secret values, raw provider responses, private-key material, and unrestricted source payloads never enter logs, commits, PR comments, or acceptance docs.
- `production-linkage` currently has a `main` branch policy and no required-reviewer rule; record that read-back without changing Environment protection in P1.
- The planning worktree stays separate from successful and negative candidate worktrees so code/docs and ARD data are never mixed in one PR.

---

## File Responsibility Map

- `products/500138301/product.yaml`: declares the existing product update, exact base/current version, stable identity, and approved `v2` description.
- `products/500138301/sources/product-info/product.html`: supplies the source-path trigger and the same approved description as HTML metadata.
- `products/500138301/generated/**`: trusted processor output only; inspected but never manually edited.
- `products/500138301/quality/**`: trusted processor evidence only; inspected for hard errors and verified publishability.
- `registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`: trusted product `v2` state and stable metric IDs.
- `registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`: trusted table links, each retaining `table_version: 1`.
- `registry/tables/*.json`: authoritative table records that must remain byte-identical.
- `docs/acceptance/direct-branch-v2-verification.md`: durable run, commit, version, hash, release, dispatch, and rejection evidence.
- `docs/next-steps.md`: marks P1 direct-branch `v2` complete only after all success and rejection evidence exists.

### Task 1: Prepare a Clean Candidate Worktree and Capture the `v1` Baseline

**Files:**
- Read: `products/500138301/product.yaml`
- Read: `products/500138301/sources/semantic/semantic.pdf`
- Read: `products/500138301/sources/dictionary/dictionary.xlsx`
- Read: `registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`
- Read: `registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`
- Read: `registry/tables/*.json`

**Interfaces:**
- Consumes: `origin/main` as the authoritative `v1` base.
- Produces: clean worktree `.worktrees/marketing-insight-v2`, branch `acceptance/marketing-insight-v2`, base SHA, LFS pointer OIDs, table blob hashes, metric IDs, and mapping versions used by later tasks.

- [ ] **Step 1: Refresh the remote base and prove the planning branch is clean**

Run from the planning worktree:

```bash
git fetch origin main --tags --prune
git status --short
git log -1 --oneline origin/main
```

Expected: only committed planning artifacts exist, `git status --short` is empty, and the current remote main commit is known. If `origin/main` moved after this plan was written, inspect the intervening commits before continuing.

- [ ] **Step 2: Prove the candidate branch does not already exist**

Run:

```bash
if git show-ref --verify --quiet refs/heads/acceptance/marketing-insight-v2; then exit 1; fi
if git ls-remote --exit-code --heads origin acceptance/marketing-insight-v2 >/dev/null 2>&1; then exit 1; fi
```

Expected: both commands report that the ref is absent. If either ref exists, stop and inspect it instead of overwriting or force-updating it.

- [ ] **Step 3: Create the isolated candidate worktree from the exact remote base**

Run from the repository root:

```bash
git worktree add .worktrees/marketing-insight-v2 -b acceptance/marketing-insight-v2 origin/main
```

Expected: the new worktree is on `acceptance/marketing-insight-v2`, tracks the reviewed `origin/main`, and contains no planning-branch commits.

- [ ] **Step 4: Capture the immutable binary pointer identities**

Run from the candidate worktree:

```bash
git show HEAD:products/500138301/sources/semantic/semantic.pdf
git show HEAD:products/500138301/sources/dictionary/dictionary.xlsx
git check-attr filter -- products/500138301/sources/semantic/semantic.pdf products/500138301/sources/dictionary/dictionary.xlsx
```

Expected:

```text
semantic.pdf oid sha256:ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6 size 114912
dictionary.xlsx oid sha256:10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a size 14813
```

Both paths must report `filter: lfs`.

- [ ] **Step 5: Capture the current product, metric, mapping, and table identities**

Run:

```bash
jq '{product_id,product_key,version,metric_ids:[.metrics[].metric_id],relationships}' registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json
jq '[.[] | {table_id,table_version}]' registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json
sha256sum registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json registry/tables/tbl_01a00585-94b9-70f1-b339-c7b2e9d77704.json registry/tables/tbl_01a00585-94b9-72c1-8f98-d818ed98b0a8.json registry/tables/tbl_01a00585-94b9-7cea-a110-ad22ea63a258.json
```

Expected: product version `1`, exactly 11 metric IDs, no relationships, exactly four mappings at table version `1`, and four recorded SHA-256 values.

### Task 2: Author and Locally Verify the Minimal Product-Only `v2` Candidate

**Files:**
- Modify: `products/500138301/product.yaml`
- Modify: `products/500138301/sources/product-info/product.html`
- Test: `tests/`

**Interfaces:**
- Consumes: clean candidate worktree and identities from Task 1.
- Produces: one reviewed candidate commit whose SHA becomes the direct-signal exact head.

- [ ] **Step 1: Change product metadata to the exact update contract**

Use `apply_patch` so `product.yaml` contains these values and retains `display_name: Marketing Insight`, `changeset_id: null`, and `tables: []`:

```yaml
operation: update
product_id: prd_019ff10c-8be8-79d0-af07-21450abedf9e
product_key: '500138301'
base_version: 1
version: 2
display_name: Marketing Insight
description: 이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습을 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
changeset_id: null
tables: []
```

- [ ] **Step 2: Add the matching source HTML description without reformatting the snapshot**

Use `apply_patch` to insert this one line immediately after the existing `<title>AX 데이터 거버넌스 포털</title>` line:

```html
    <meta name="description" content="이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습을 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.">
```

Expected: the existing 655 KB HTML snapshot is not reformatted; the only HTML diff is the added metadata line.

- [ ] **Step 3: Verify the authored diff boundary and exact metadata**

Run:

```bash
git diff --check
git diff --name-only
git diff -- products/500138301/product.yaml products/500138301/sources/product-info/product.html
rg -n '^operation: update$|^base_version: 1$|^version: 2$|캠페인 성과 탐색과 AI 분석 실습' products/500138301/product.yaml products/500138301/sources/product-info/product.html
```

Expected: exactly the two allowed files are modified; update/base/version each appear once in YAML; the approved description appears in YAML and the new HTML metadata.

- [ ] **Step 4: Prove binary and table state did not change locally**

Run:

```bash
git diff --exit-code HEAD -- products/500138301/sources/semantic/semantic.pdf products/500138301/sources/dictionary/dictionary.xlsx registry/tables registry/mappings registry/products
```

Expected: exit `0` and no output.

- [ ] **Step 5: Synchronize dependencies and run the complete local verification**

Run:

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check src tests
```

Expected: every test passes and Ruff reports no errors. Do not run local product processing against LFS pointer files; the trusted GitHub checkout downloads the actual LFS objects.

- [ ] **Step 6: Scan the candidate diff for forbidden material**

Run:

```bash
if git diff --unified=0 | rg -n 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|api[_-]?key|secret|token|password'; then exit 1; fi
```

Expected: no matches. The synthetic Korean word `비밀정보` is not introduced by this diff.

- [ ] **Step 7: Commit the exact candidate**

Run:

```bash
git add products/500138301/product.yaml products/500138301/sources/product-info/product.html
git commit -m "data(500138301): propose direct v2 update"
git status --short
```

Expected: one commit, clean worktree, and no generated/quality/Registry file in the commit.

### Task 3: Push the Exact Candidate and Observe Trusted Direct Processing

**Files:**
- Remote mutation: branch `acceptance/marketing-insight-v2`
- Remote mutation: processor-managed Draft PR
- Read: `.github/workflows/ard-direct-signal.yml`
- Read: `.github/workflows/ard-direct-change.yml`

**Interfaces:**
- Consumes: candidate commit from Task 2 and trusted default-branch workflows.
- Produces: direct-signal run URL, coordinator run URL, Draft PR number, candidate SHA, processor SHA, and two exact-head status targets.

- [ ] **Step 1: Push without force and record the exact candidate SHA**

Run:

```bash
git push -u origin acceptance/marketing-insight-v2
git rev-parse HEAD
```

Expected: a normal new-branch push succeeds. Never add `--force` or `--force-with-lease`.

- [ ] **Step 2: Find and watch the source-only signal run**

Run:

```bash
signal_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-signal.yml --branch acceptance/marketing-insight-v2 --limit 1 --json databaseId,headSha,status,conclusion,createdAt,url --jq '.[0].databaseId')
gh run view "$signal_run_id" --repo kimohy/ard-ossie-provider --json databaseId,headSha,status,conclusion,createdAt,url
gh run watch "$signal_run_id" --repo kimohy/ard-ossie-provider --exit-status
```

Expected: the viewed `headSha` equals the candidate SHA and the run succeeds with only the credential-free checkout signal job.

- [ ] **Step 3: Find and watch the corresponding default-branch coordinator**

Run:

```bash
coordinator_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-change.yml --event workflow_run --limit 1 --json databaseId,status,conclusion,createdAt,url --jq '.[0].databaseId')
gh run view "$coordinator_run_id" --repo kimohy/ard-ossie-provider --json databaseId,event,status,conclusion,createdAt,jobs,url
gh run watch "$coordinator_run_id" --repo kimohy/ard-ossie-provider --exit-status
```

Expected: the coordinator was created after the signal run; validate, pull request, reusable processing, and finalization all succeed. Confirm in the validate job metadata that the candidate branch was checked. If any job fails, preserve the run and invoke `superpowers:systematic-debugging`; do not edit generated output.

- [ ] **Step 4: Resolve the automatically created Draft PR**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state open --json number --jq '.[0].number')
gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json number,title,isDraft,headRefOid,url
```

Expected: exactly one Draft PR, with the head first equal to the authored candidate and later advanced only by the trusted processor commit.

- [ ] **Step 5: Fetch the processor commit without changing local files**

Run:

```bash
git fetch origin acceptance/marketing-insight-v2
git log --oneline --decorate HEAD..origin/acceptance/marketing-insight-v2
```

Expected: exactly one trusted `data(500138301): generate validated Ossie artifacts` commit after the authored candidate unless the processor proves a documented no-op. Fast-forward the local candidate branch only after reviewing Task 4 invariants:

```bash
git merge --ff-only origin/acceptance/marketing-insight-v2
```

### Task 4: Verify Processor Output and Merge the Product PR

**Files:**
- Read: `products/500138301/generated/**`
- Read: `products/500138301/quality/**`
- Read: `registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`
- Read: `registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json`
- Read: `registry/tables/*.json`
- Remote mutation: mark the processor-managed PR ready and merge it

**Interfaces:**
- Consumes: final processor head and baseline identities from Tasks 1 and 3.
- Produces: verified final PR head, merge commit, and exact pre-merge evidence for release detection.

- [ ] **Step 1: Verify product, quality, and validation state at the processor head**

Run from the fast-forwarded candidate worktree:

```bash
jq '{product_id,product_key,version,metric_ids:[.metrics[].metric_id],relationships}' registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json
jq '{status,hard_error_count:(.hard_errors|length),warning_count:(.warnings|length)}' products/500138301/quality/quality-report.json
jq '{status,publishable,review_debt,review_required,review_pending}' products/500138301/quality/validation-report.json
rg -n 'Version: `v2`|캠페인 성과 탐색과 AI 분석 실습' products/500138301/generated/data-product.md
```

Expected: stable product identity, version `2`, the same 11 metric IDs, empty relationships, zero hard errors, validation `verified`, `publishable: true`, no review debt, and generated product documentation containing the approved description.

- [ ] **Step 2: Verify table mappings and table records stayed at `v1`**

Run:

```bash
jq '[.[] | {table_id,table_version}]' registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json
git diff --exit-code origin/main -- registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json registry/tables/tbl_01a00585-94b9-70f1-b339-c7b2e9d77704.json registry/tables/tbl_01a00585-94b9-72c1-8f98-d818ed98b0a8.json registry/tables/tbl_01a00585-94b9-7cea-a110-ad22ea63a258.json
git diff --exit-code origin/main -- products/500138301/sources/semantic/semantic.pdf products/500138301/sources/dictionary/dictionary.xlsx
```

Expected: four mappings at version `1`, byte-identical table records, and byte-identical LFS pointers.

- [ ] **Step 3: Verify stable metric IDs against the base rather than by count alone**

Run:

```bash
git show origin/main:registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json | jq '[.metrics[].metric_id] | sort'
jq '[.metrics[].metric_id] | sort' registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json
```

Expected: the two sorted arrays are identical.

- [ ] **Step 4: Verify the two required statuses target the final processor head**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state open --json number --jq '.[0].number')
gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json headRefOid,isDraft,statusCheckRollup,url
git rev-parse HEAD
```

Expected: PR `headRefOid` equals local `HEAD`; `ard/quality-gate` and `ard/changeset` are both `SUCCESS` and their target URLs point to the coordinator run.

- [ ] **Step 5: Confirm the base did not advance during processing**

Run:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
```

Expected: exit `0`. If the base moved and is not an ancestor, stop; do not merge a stale PR or force-update it. Re-plan a normal base merge plus a new source metadata revision that triggers exact-head reprocessing.

- [ ] **Step 6: Mark the PR ready and merge through branch protection**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state open --json number --jq '.[0].number')
gh pr ready "$product_pr_number" --repo kimohy/ard-ossie-provider
gh pr merge "$product_pr_number" --repo kimohy/ard-ossie-provider --merge
gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json state,mergedAt,mergeCommit,url
```

Expected: GitHub accepts the protected merge, reports `MERGED`, and returns one merge commit. Do not delete the candidate branch during acceptance evidence collection.

### Task 5: Verify the Immutable `v2` Release, Environment State, and Idempotent Retry

**Files:**
- Read: `.github/workflows/ard-release.yml`
- Remote mutation: Git tag, GitHub Release, repository dispatch, and one explicit workflow rerun

**Interfaces:**
- Consumes: merge commit from Task 4.
- Produces: release run URL, tag target, Release asset SHA-256, result-envelope SHA-256, Environment read-back, dispatch result, and no-op retry evidence.

- [ ] **Step 1: Find and watch the release run for the merge commit**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state all --json number --jq '.[0].number')
merge_sha=$(gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json mergeCommit --jq '.mergeCommit.oid')
release_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-release.yml --event push --limit 10 --json databaseId,headSha --jq ".[] | select(.headSha == \"$merge_sha\") | .databaseId" | head -n 1)
gh run view "$release_run_id" --repo kimohy/ard-ossie-provider --json databaseId,headSha,status,conclusion,createdAt,url
gh run watch "$release_run_id" --repo kimohy/ard-ossie-provider --exit-status
```

Select the run whose `headSha` equals the merge commit. Expected: detect, release, and linkage jobs succeed.

- [ ] **Step 2: Read back the actual linkage protection before interpreting dispatch timing**

Run:

```bash
gh api repos/kimohy/ard-ossie-provider/environments/production-linkage --jq '{name,can_admins_bypass,protection_rules,deployment_branch_policy}'
```

Expected: a custom branch policy restricted to `main` and no required-reviewer rule. Record this as actual P1 state; do not add or remove Environment rules.

- [ ] **Step 3: Verify the product tag and absence of table `v2` tags**

Run:

```bash
git fetch origin main --tags
git rev-list -n 1 product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v2
git rev-parse origin/main
git tag --list 'table/*/v2'
```

Expected: the product `v2` tag resolves to the merge commit and there are no table `v2` tags.

- [ ] **Step 4: Download the workflow artifact and GitHub Release asset and compare their hashes**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state all --json number --jq '.[0].number')
merge_sha=$(gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json mergeCommit --jq '.mergeCommit.oid')
release_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-release.yml --event push --limit 10 --json databaseId,headSha --jq ".[] | select(.headSha == \"$merge_sha\") | .databaseId" | head -n 1)
acceptance_download_dir=$(mktemp -d)
gh run download "$release_run_id" --repo kimohy/ard-ossie-provider --name "ard-release-500138301-$release_run_id" --dir "$acceptance_download_dir/run"
gh release download product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v2 --repo kimohy/ard-ossie-provider --dir "$acceptance_download_dir/release"
find "$acceptance_download_dir" -maxdepth 5 -type f -print
jq '{status,outputs,mutations}' "$acceptance_download_dir/run/.ard/run/workflow.release-product-result.json"
run_zip_hash=$(sha256sum "$acceptance_download_dir/run/dist/prd_019ff10c-8be8-79d0-af07-21450abedf9e-v2.zip" | cut -d' ' -f1)
release_zip_hash=$(sha256sum "$acceptance_download_dir/release/prd_019ff10c-8be8-79d0-af07-21450abedf9e-v2.zip" | cut -d' ' -f1)
recorded_zip_hash=$(jq -r '.outputs.artifact_sha256' "$acceptance_download_dir/run/.ard/run/workflow.release-product-result.json")
test "$run_zip_hash" = "$release_zip_hash"
test "$run_zip_hash" = "$recorded_zip_hash"
unzip -t "$acceptance_download_dir/release/prd_019ff10c-8be8-79d0-af07-21450abedf9e-v2.zip"
```

Expected: the run artifact contains `.ard/run/workflow.release-product-result.json`, the release asset is `prd_019ff10c-8be8-79d0-af07-21450abedf9e-v2.zip`, result status is success, both SHA-256 values are identical to the recorded artifact hash, and the ZIP integrity check passes.

- [ ] **Step 5: Rerun the exact release input and verify convergence**

Run:

```bash
product_pr_number=$(gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2 --state all --json number --jq '.[0].number')
merge_sha=$(gh pr view "$product_pr_number" --repo kimohy/ard-ossie-provider --json mergeCommit --jq '.mergeCommit.oid')
release_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-release.yml --event push --limit 10 --json databaseId,headSha --jq ".[] | select(.headSha == \"$merge_sha\") | .databaseId" | head -n 1)
gh run rerun "$release_run_id" --repo kimohy/ard-ossie-provider
gh run watch "$release_run_id" --repo kimohy/ard-ossie-provider --exit-status
gh api "repos/kimohy/ard-ossie-provider/actions/runs/$release_run_id" --jq '{run_attempt,status,conclusion,html_url}'
gh run view "$release_run_id" --repo kimohy/ard-ossie-provider --json conclusion,jobs,url
rerun_download_dir=$(mktemp -d)
gh run download "$release_run_id" --repo kimohy/ard-ossie-provider --name "ard-release-500138301-$release_run_id" --dir "$rerun_download_dir/run"
jq '{status,outputs,mutations}' "$rerun_download_dir/run/.ard/run/workflow.release-product-result.json"
gh api "repos/kimohy/ard-ossie-provider/commits/$merge_sha/status" --jq '.statuses[] | select(.context == "ard/dispatched:prd_019ff10c-8be8-79d0-af07-21450abedf9e:v2") | {context,state,target_url}'
gh run view "$release_run_id" --repo kimohy/ard-ossie-provider --log | rg 'NOOP|noop|dispatched'
uv run --frozen pytest tests/unit/test_release_dispatch_service.py::test_dispatch_is_noop_after_exact_success_status -v
```

Expected: the rerun release result shows immutable tag and existing asset reuse, the commit already has the exact successful dispatch status, the linkage log reports no-op rather than a new dispatch, and the contract test proves that a pre-existing exact success status suppresses `repository_dispatch`.

### Task 6: Run Live Stale-Base and Version-Gap Rejection Probes

**Files:**
- Modify on stale branch: `products/500138301/sources/product-info/product.html`
- Modify on gap branch: `products/500138301/product.yaml`
- Modify on gap branch: `products/500138301/sources/product-info/product.html`
- Test: `tests/integration/test_workflow_contracts.py`

**Interfaces:**
- Consumes: merged `v2` on current `origin/main`.
- Produces: two failed coordinator run URLs and diagnostics, proof of no processor writeback, and automated fork identity-guard evidence.

- [ ] **Step 1: Refresh `origin/main` and create the stale probe branch without overwriting refs**

Run from the repository root:

```bash
git fetch origin main --prune
if git show-ref --verify --quiet refs/heads/acceptance/marketing-insight-v2-stale; then exit 1; fi
if git ls-remote --exit-code --heads origin acceptance/marketing-insight-v2-stale >/dev/null 2>&1; then exit 1; fi
git worktree add .worktrees/marketing-insight-v2-stale -b acceptance/marketing-insight-v2-stale origin/main
```

Expected: both existence checks report absent before worktree creation. If a ref exists, inspect it and stop.

- [ ] **Step 2: Make a visible stale-probe description while leaving `base_version: 1` and `version: 2`**

In the stale worktree, use `apply_patch` to replace only the `v2` meta description line with:

```html
    <meta name="description" content="Stale-base acceptance probe for synthetic Marketing Insight; this candidate must be rejected and never published.">
```

Run:

```bash
rg -n '^operation: update$|^base_version: 1$|^version: 2$|Stale-base acceptance probe' products/500138301/product.yaml products/500138301/sources/product-info/product.html
git diff --name-only
git diff --check
```

Expected: only HTML changed; the merged metadata still proposes base `1` and version `2` against current Registry `v2`.

- [ ] **Step 3: Commit, push, and capture the stale rejection**

Run:

```bash
git add products/500138301/sources/product-info/product.html
git commit -m "test(500138301): probe stale direct update"
git push -u origin acceptance/marketing-insight-v2-stale
stale_candidate_sha=$(git rev-parse HEAD)
stale_signal_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-signal.yml --branch acceptance/marketing-insight-v2-stale --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$stale_signal_run_id" --repo kimohy/ard-ossie-provider --json headSha,status,conclusion,url
gh run watch "$stale_signal_run_id" --repo kimohy/ard-ossie-provider --exit-status
stale_coordinator_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-change.yml --event workflow_run --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$stale_coordinator_run_id" --repo kimohy/ard-ossie-provider --json status,conclusion,createdAt,jobs,url
gh run watch "$stale_coordinator_run_id" --repo kimohy/ard-ossie-provider --exit-status
gh run view "$stale_coordinator_run_id" --repo kimohy/ard-ossie-provider --log-failed | rg 'VERSION_STALE'
git ls-remote origin refs/heads/acceptance/marketing-insight-v2-stale
gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2-stale --state all --json number,state,isDraft,headRefOid,url
```

Expected: signal success; its head equals `stale_candidate_sha`; coordinator watch exits nonzero with `VERSION_STALE`; remote branch remains at the authored SHA; no processor commit, Registry/generated publication, successful required status, or Draft PR exists. Preserve the branch and failed run. If a PR exists, stop and investigate the unexpected mutation rather than closing or deleting it.

- [ ] **Step 4: Create the gap probe branch from the same merged base**

Run from the repository root:

```bash
if git show-ref --verify --quiet refs/heads/acceptance/marketing-insight-v2-gap; then exit 1; fi
if git ls-remote --exit-code --heads origin acceptance/marketing-insight-v2-gap >/dev/null 2>&1; then exit 1; fi
git worktree add .worktrees/marketing-insight-v2-gap -b acceptance/marketing-insight-v2-gap origin/main
```

Expected: both refs are absent before creation.

- [ ] **Step 5: Author the skipped-version candidate**

In the gap worktree, use `apply_patch` to change `base_version: 1` to `base_version: 2`, `version: 2` to `version: 4`, and replace the HTML meta description with:

```html
    <meta name="description" content="Skipped-version acceptance probe for synthetic Marketing Insight; this candidate must be rejected and never published.">
```

Run:

```bash
rg -n '^operation: update$|^base_version: 2$|^version: 4$|Skipped-version acceptance probe' products/500138301/product.yaml products/500138301/sources/product-info/product.html
git diff --name-only
git diff --check
```

Expected: exactly product YAML and HTML changed.

- [ ] **Step 6: Commit, push, and capture the gap rejection**

Run:

```bash
git add products/500138301/product.yaml products/500138301/sources/product-info/product.html
git commit -m "test(500138301): probe skipped direct version"
git push -u origin acceptance/marketing-insight-v2-gap
gap_candidate_sha=$(git rev-parse HEAD)
gap_signal_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-signal.yml --branch acceptance/marketing-insight-v2-gap --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$gap_signal_run_id" --repo kimohy/ard-ossie-provider --json headSha,status,conclusion,url
gh run watch "$gap_signal_run_id" --repo kimohy/ard-ossie-provider --exit-status
gap_coordinator_run_id=$(gh run list --repo kimohy/ard-ossie-provider --workflow ard-direct-change.yml --event workflow_run --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$gap_coordinator_run_id" --repo kimohy/ard-ossie-provider --json status,conclusion,createdAt,jobs,url
gh run watch "$gap_coordinator_run_id" --repo kimohy/ard-ossie-provider --exit-status
gh run view "$gap_coordinator_run_id" --repo kimohy/ard-ossie-provider --log-failed | rg 'VERSION_GAP'
git ls-remote origin refs/heads/acceptance/marketing-insight-v2-gap
gh pr list --repo kimohy/ard-ossie-provider --head acceptance/marketing-insight-v2-gap --state all --json number,state,isDraft,headRefOid,url
```

Expected: signal success; its head equals `gap_candidate_sha`; coordinator watch exits nonzero with `VERSION_GAP`; remote branch remains at the authored SHA; no processor commit, Registry/generated publication, successful required status, or Draft PR exists. Preserve the branch and failed run. If a PR exists, stop and investigate the unexpected mutation rather than closing or deleting it.

- [ ] **Step 7: Verify the fork writeback identity guard through its exact contract test**

Run from the planning worktree after merging the current `origin/main`:

```bash
uv run --frozen pytest tests/integration/test_workflow_contracts.py::test_direct_change_uses_read_only_signal_and_default_branch_coordinator -v
```

Expected: pass, including the exact guard `github.event.workflow_run.head_repository.full_name == github.repository`, credential-free candidate checkout, read-only validation permissions, and no `GH_TOKEN` in validation.

### Task 7: Reconcile CLI History and Publish the Acceptance Record

**Files:**
- Create: `docs/acceptance/direct-branch-v2-verification.md`
- Modify: `docs/next-steps.md`
- Read: `docs/superpowers/specs/2026-08-16-direct-branch-v2-acceptance-design.md`
- Read: `docs/superpowers/plans/2026-08-16-direct-branch-v2-acceptance.md`

**Interfaces:**
- Consumes: all successful and rejected run evidence from Tasks 1-6.
- Produces: durable acceptance report, checked P1 roadmap, and a reviewable documentation branch.

- [ ] **Step 1: Bring the planning branch forward with the merged product history**

Run in the planning worktree:

```bash
git fetch origin main --tags
git merge --no-edit origin/main
git status --short
```

Expected: a normal merge preserves the design and plan commits while adding the product `v2` merge. Resolve no conflict by discarding either side; stop if a conflict requires judgment.

- [ ] **Step 2: Run the supported history and diff commands**

Run:

```bash
uv sync --frozen
uv run --frozen ard history 500138301
uv run --frozen ard diff '500138301@v1..v2'
```

Expected: history shows `v1` and `v2`; diff reports the approved product description/version change and no table version advance.

- [ ] **Step 3: Create the acceptance report with exact observed values**

Use `apply_patch` to create `docs/acceptance/direct-branch-v2-verification.md` with these sections populated from prior command output rather than inferred claims:

```markdown
# Direct Branch `v2` Production Verification

## Scope and baseline
## Successful candidate and trusted workflow
## Product, metric, relationship, and table invariants
## Merge, tag, Release asset, and SHA-256
## `production-linkage` policy and dispatch convergence
## `ard history` and `ard diff`
## Stale-base rejection
## Skipped-version rejection
## Fork identity-guard evidence
## Security and residual follow-ups
```

For every GitHub item, include its URL and exact commit/head. Include the two LFS OIDs, four table hashes, release ZIP hash, required-status target URLs, diagnostics `VERSION_STALE` and `VERSION_GAP`, and the Environment read-back showing no required reviewer. Do not include provider prompt/response text, Secret values, unrestricted source excerpts, or temporary directory paths.

- [ ] **Step 4: Mark only the completed P1 item in the roadmap**

Use `apply_patch` in `docs/next-steps.md` to:

- move direct-branch `v2` from pending to completed;
- link the acceptance report, product PR, successful workflow/release, and two rejection runs;
- check the five P1 bullets only when their corresponding evidence is present;
- leave shared-table, review protection, failure training, and representative PDF backlog unchanged;
- note that adding a `production-linkage` required reviewer remains an operations-protection follow-up.

- [ ] **Step 5: Run documentation and repository verification**

Run:

```bash
git diff --check
if rg -n 'TBD|TODO|FIXME|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|api[_-]?key\s*[:=]|password\s*[:=]' docs/acceptance/direct-branch-v2-verification.md docs/next-steps.md; then exit 1; fi
uv run --frozen pytest
uv run --frozen ruff check src tests
```

Expected: no placeholder or secret-pattern matches, all tests pass, and Ruff passes.

- [ ] **Step 6: Commit the acceptance record**

Run:

```bash
git add docs/acceptance/direct-branch-v2-verification.md docs/next-steps.md
git commit -m "docs: record direct branch v2 acceptance"
git status --short
```

Expected: clean planning worktree with design, plan, and final evidence commits separate from the product data PR.

### Task 8: Final Review and Branch Handoff

**Files:**
- Review: all planning-branch changes against `origin/main`
- Review: successful product PR and negative-run links

**Interfaces:**
- Consumes: completed and verified artifacts from Tasks 1-7.
- Produces: final review result and user-approved integration path for planning/evidence docs.

- [ ] **Step 1: Review the complete planning diff and commit history**

Run:

```bash
git log --oneline --decorate origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
git status --short
```

Expected: only design, plan, acceptance report, and roadmap changes remain relative to the post-product `origin/main`; worktree is clean.

- [ ] **Step 2: Re-read all externally asserted evidence**

Use `gh pr view`, `gh run view`, `gh release view`, `git rev-list`, and `sha256sum` to re-check every URL, SHA, conclusion, tag target, and artifact hash in the acceptance report. Expected: every claim is directly supported and no failed run is presented as success.

- [ ] **Step 3: Invoke branch completion workflow**

Announce and use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. Present the tested integration options for the planning/evidence branch; do not push or create a documentation PR until the user chooses that finishing action.
