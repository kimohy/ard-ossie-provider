# Shared-table Changeset Production E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the production shared-table changeset lifecycle across the two existing synthetic products, including exact-head readiness, atomic release after the final product merge, and later independent clearing of both active changeset bindings.

**Architecture:** Reuse products `500138301` v2 and `500138302` v1, which already map to the same four version-1 Registry tables. Create one central changeset and two canonical tracking PRs, populate them through the public Issue intake with one byte-identical dictionary change, then prove future-version deferral and all-or-nothing release. Finish with two independent product-only updates and a documentation-only evidence PR.

**Tech Stack:** Python 3.12, pytest, Typer, Pydantic, GitHub Actions, `gh`, Git/GitHub commit statuses, Git LFS assets, `openpyxl`, `jq`, `curl`, Ruff, actionlint, gstack browser automation.

## Global Constraints

- Repository is exactly `kimohy/ard-ossie-provider`, default branch is `main`, and production visibility is `PUBLIC` during this acceptance.
- Work from the linked worktree `/home/moohyunkim/workspace/ard-ossie-provider/.worktrees/shared-table-e2e-refresh`; do not modify the stale root checkout.
- The revised design and this plan must land through a documentation-only PR before production mutation begins.
- Never combine code/workflow/documentation paths with `products/**` or `registry/**` in one PR.
- Do not create another product. Reuse product `prd_019ff10c-8be8-79d0-af07-21450abedf9e` (`500138301`, v2) and product `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d` (`500138302`, v1).
- Coordinate only table `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`, locator `unspecified|synthetic_workspace|marketing_insight|marketing_campaign`.
- Create and update product data only through the public `AI Ready Data submission` Issue workflow. Create and update changeset data only through `ARD shared-table changeset coordinator`.
- Check public publication authorization only for the approved synthetic, non-confidential fixtures. Do not attach customer, account, platform, organization, operational, personal, or secret data.
- Apply `ard:approved` only after the exact Issue body and all three uploaded attachment hashes are independently verified.
- Never force-push, delete a managed branch, move an existing numeric tag, replace a Release asset, hand-edit generated product/Registry output, or bypass an Environment approval.
- Merge only an unchanged exact PR head with successful `ard/quality-gate` and `ard/changeset` statuses.
- The definition PR may be merged after exact-head verification. Pause for explicit user approval before merging the readiness PR, either product tracking PR, and either later independent-clear PR.
- Preserve failed runs and Draft PRs for diagnosis. If an incorrect change reaches `main`, use a new revert PR rather than rewriting history.
- Record only real runtime facts in ignored `.ard/run/shared-table-e2e-state.json`; never write placeholder values, tokens, signed URLs, provider request bodies, or Environment secret values.
- `production-linkage` and `ard-llm` approval requests are owner-controlled gates. Wait for owner approval rather than attempting another path.

Fixed source identities:

```text
PDF URL: https://github.com/user-attachments/files/31115996/Marketing.Insight.Data.Semantics.pdf
PDF SHA-256 / LFS OID: ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6
PDF size: 114912
XLSX URL: https://github.com/user-attachments/files/31116002/Marketing.Insight.Data.Dictionary.xlsx
XLSX SHA-256 / LFS OID: 10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a
XLSX size: 14813
500138301 current HTML: products/500138301/sources/product-info/product.html
500138301 HTML SHA-256: ce2f8db06d307f10010c0a2509f8f65b2e89b375eaeb0555bc8565d553407e2d
500138301 HTML size: 656039
500138302 current HTML: products/500138302/sources/product-info/product.html
500138302 HTML SHA-256: b39248654c0cd9b6f3f28111a6c44036d86a2440a1d7dc2c9bfd7bd40281d7f9
500138302 HTML size: 656045
```

Expected coordinated versions:

| Entity | Before | Coordinated release | Independent clear |
|---|---:|---:|---:|
| `500138301` | 2 | 3 | 4 |
| `500138302` | 1 | 2 | 3 |
| `marketing_campaign` | 1 | 2 | 2 |
| Other three tables | 1 | 1 | 1 |

---

### Task 1: Land the revised design and plan

**Files:**
- Modify: `docs/superpowers/specs/2026-08-16-shared-table-changeset-e2e-design.md`
- Modify: `docs/superpowers/plans/2026-08-16-shared-table-changeset-e2e.md`

**Interfaces:**
- Consumes: approved design decision to reuse both existing products and pause at release-sensitive merges.
- Produces: a merged, repository-gated operating contract on `main`.

- [ ] **Step 1: Verify the documentation branch scope**

Run:

```bash
git fetch origin main --tags --prune
git status --short --branch
git diff --name-only origin/main...HEAD
git diff --check origin/main...HEAD
```

Expected changed paths are exactly the design and plan above. There must be no `products/**`, `registry/**`, code, workflow, or generated path.

- [ ] **Step 2: Verify the clean repository baseline**

Run:

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ard workflow repository-check \
  --base-ref origin/main \
  --head-ref HEAD \
  --head-sha "$(git rev-parse HEAD)" \
  --repository . \
  --verification-group static
```

Expected: pytest reports `1224 passed`; Ruff and the integrated static verifier exit `0`. The static verifier performs checksum-verified actionlint, workflow YAML, schema/catalog, Ossie checksum, and secret scans. If the test count changes because `origin/main` advanced, record the exact new count and investigate any failure before continuing.

- [ ] **Step 3: Push and open the documentation PR**

Run:

```bash
git push -u origin docs/shared-table-e2e-refresh
gh pr create --draft \
  --repo kimohy/ard-ossie-provider \
  --base main \
  --head docs/shared-table-e2e-refresh \
  --title "docs: rebase shared-table changeset e2e" \
  --body "Rebases the approved production E2E on the two products that now exist, removes completed seed/code work, and records explicit release-sensitive approval checkpoints."
```

Capture the returned PR number and URL.

- [ ] **Step 4: Verify and merge the exact documentation head**

Run:

```bash
DOC_PR=$(gh pr view --repo kimohy/ard-ossie-provider --json number --jq .number)
DOC_HEAD=$(gh pr view "$DOC_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)
gh pr diff "$DOC_PR" --repo kimohy/ard-ossie-provider --name-only
gh pr checks "$DOC_PR" --repo kimohy/ard-ossie-provider --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$DOC_HEAD/status"
```

Require both required statuses to be successful on `DOC_HEAD` and the path set to contain exactly two documentation files. Then run:

```bash
gh pr ready "$DOC_PR" --repo kimohy/ard-ossie-provider
gh pr merge "$DOC_PR" --repo kimohy/ard-ossie-provider --merge
git fetch origin main --tags
gh pr view "$DOC_PR" --repo kimohy/ard-ossie-provider \
  --json state,mergedAt,mergeCommit,headRefOid,url
```

Expected: merged without starting `ARD numeric release`, because no `products/**` or `registry/**` path changed.

---

### Task 2: Freeze the production baseline and author one canonical XLSX

**Files:**
- Create locally: `.ard/run/shared-table-e2e-state.json` (ignored, actual values only)
- Create locally: `.ard/run/shared-table-e2e-sources/` (ignored synthetic upload sources)
- No tracked repository file changes.

**Interfaces:**
- Consumes: fixed product/table/source identities from the global constraints.
- Produces: one verified modified XLSX used byte-for-byte by both tracking Issues, plus exact baseline evidence.

- [ ] **Step 1: Re-read repository identity and collision state**

Run:

```bash
gh repo view kimohy/ard-ossie-provider \
  --json nameWithOwner,visibility,defaultBranchRef,deleteBranchOnMerge,url
gh pr list --repo kimohy/ard-ossie-provider --state open --limit 100 \
  --json number,title,isDraft,headRefName,headRefOid,baseRefName,url
gh issue list --repo kimohy/ard-ossie-provider --state open --limit 100 \
  --json number,title,labels,url
gh api repos/kimohy/ard-ossie-provider/branches --paginate \
  --jq '.[] | select(.name | startswith("ard/cst_") or startswith("ard/changeset-cst_")) | [.name,.commit.sha] | @tsv'
```

Require repository `kimohy/ard-ossie-provider`, visibility `PUBLIC`, default branch `main`, `deleteBranchOnMerge: false`, and no pre-existing `cst_` coordination/tracking branch. Stop on any collision.

- [ ] **Step 2: Re-read the fixed Registry baseline from `origin/main`**

Run:

```bash
git fetch origin main --tags --prune
git show origin/main:registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json | jq -e '.product_key == "500138301" and .version == 2'
git show origin/main:registry/products/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d.json | jq -e '.product_key == "500138302" and .version == 1'
git show origin/main:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json | jq -e '.version == 1 and .locator.table_name == "marketing_campaign"'
```

Read both mapping files and require the same four sorted table IDs, each at `table_version: 1`:

```bash
git show origin/main:registry/mappings/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json | jq -S 'map({table_id,table_version}) | sort_by(.table_id)'
git show origin/main:registry/mappings/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d.json | jq -S 'map({table_id,table_version}) | sort_by(.table_id)'
```

- [ ] **Step 3: Materialize the approved sources**

Run:

```bash
mkdir -p .ard/run/shared-table-e2e-sources
curl --fail --location --proto '=https' \
  --output .ard/run/shared-table-e2e-sources/semantic.pdf \
  'https://github.com/user-attachments/files/31115996/Marketing.Insight.Data.Semantics.pdf'
curl --fail --location --proto '=https' \
  --output .ard/run/shared-table-e2e-sources/dictionary-baseline.xlsx \
  'https://github.com/user-attachments/files/31116002/Marketing.Insight.Data.Dictionary.xlsx'
cp products/500138301/sources/product-info/product.html \
  .ard/run/shared-table-e2e-sources/500138301.html
cp products/500138302/sources/product-info/product.html \
  .ard/run/shared-table-e2e-sources/500138302.html
sha256sum .ard/run/shared-table-e2e-sources/semantic.pdf \
  .ard/run/shared-table-e2e-sources/dictionary-baseline.xlsx \
  .ard/run/shared-table-e2e-sources/500138301.html \
  .ard/run/shared-table-e2e-sources/500138302.html
stat -c '%n %s' .ard/run/shared-table-e2e-sources/*
file .ard/run/shared-table-e2e-sources/*
```

Require the exact hashes/sizes in Global Constraints, `%PDF` identification for the PDF, and Microsoft Excel/ZIP identification for the XLSX.

- [ ] **Step 4: Create the canonical modified workbook**

Run this exact value-based transformation. It locates the `marketing_campaign` sheet, header row, `campaign_status` row, and description column by content rather than relying on a fixed cell coordinate:

```bash
uv run --frozen python - <<'PY'
from pathlib import Path
from openpyxl import load_workbook

source = Path('.ard/run/shared-table-e2e-sources/dictionary-baseline.xlsx')
target = Path('.ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx')
desired = '합성 캠페인 상태이며 Draft, Active, Paused, Closed 중 하나입니다.'

baseline = load_workbook(source, data_only=False)
workbook = load_workbook(source, data_only=False)
worksheet = workbook['marketing_campaign']

header_row = None
name_column = None
description_column = None
for row in worksheet.iter_rows():
    values = [cell.value for cell in row]
    if '컬럼명' in values and any(
        isinstance(value, str) and value.startswith('컬럼 설명') for value in values
    ):
        header_row = row[0].row
        name_column = values.index('컬럼명') + 1
        description_column = next(
            index + 1
            for index, value in enumerate(values)
            if isinstance(value, str) and value.startswith('컬럼 설명')
        )
        break
assert header_row is not None
assert name_column is not None
assert description_column is not None

matches = []
for row_number in range(header_row + 1, worksheet.max_row + 1):
    if worksheet.cell(row_number, name_column).value == 'campaign_status':
        matches.append(row_number)
assert len(matches) == 1
row_number = matches[0]
assert worksheet.cell(row_number, description_column).value == 'Draft, Active, Paused, Closed 상태'
worksheet.cell(row_number, description_column).value = desired
workbook.save(target)

reopened = load_workbook(target, data_only=False)
changes = []
for before_sheet, after_sheet in zip(baseline.worksheets, reopened.worksheets, strict=True):
    assert before_sheet.title == after_sheet.title
    assert before_sheet.max_row == after_sheet.max_row
    assert before_sheet.max_column == after_sheet.max_column
    assert sorted(str(item) for item in before_sheet.merged_cells.ranges) == sorted(
        str(item) for item in after_sheet.merged_cells.ranges
    )
    for before_row, after_row in zip(
        before_sheet.iter_rows(), after_sheet.iter_rows(), strict=True
    ):
        for before_cell, after_cell in zip(before_row, after_row, strict=True):
            if before_cell.value != after_cell.value:
                changes.append(
                    (before_sheet.title, before_cell.coordinate, before_cell.value, after_cell.value)
                )
assert changes == [
    ('marketing_campaign', worksheet.cell(row_number, description_column).coordinate,
     'Draft, Active, Paused, Closed 상태', desired)
]
print(changes)
PY
```

Expected: exactly one logical cell change with the exact desired Korean description.

- [ ] **Step 5: Verify and record the new binary identity**

Run:

```bash
sha256sum .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
stat -c '%n %s' .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
file .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
```

Use `apply_patch` to create `.ard/run/shared-table-e2e-state.json` with literal values returned by the commands: baseline `origin/main` SHA, both product IDs/versions, all four table IDs/versions, source hashes/sizes, modified XLSX hash/size, and the four pre-change table Registry blob hashes. Do not add a field until its exact value is known.

---

### Task 3: Publish and merge the blocked changeset definition

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json` (ignored actual facts)
- No tracked repository file changes.

**Interfaces:**
- Consumes: fixed product IDs, target table ID, and clean collision state.
- Produces: one merged blocked changeset record and two open canonical Draft tracking PRs.

- [ ] **Step 1: Generate and persist one changeset ID**

Run:

```bash
CHANGESET_ID=$(uv run --frozen python -c 'from ard_ossie.ids import new_id; print(new_id("cst"))')
printf '%s\n' "$CHANGESET_ID" | rg '^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
```

Use `apply_patch` to add the literal generated ID to the ignored state JSON before dispatching.

- [ ] **Step 2: Dispatch create mode from the trusted default branch**

Run:

```bash
gh workflow run ard-changeset.yml \
  --repo kimohy/ard-ossie-provider \
  --ref main \
  -f mode=create \
  -f changeset_id="$CHANGESET_ID" \
  -f table_ids=tbl_01a00585-94b8-7e49-ac43-97e00a165e26 \
  -f product_ids='prd_019ff10c-8be8-79d0-af07-21450abedf9e,prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d'
gh run list --repo kimohy/ard-ossie-provider \
  --workflow ard-changeset.yml --event workflow_dispatch --limit 5 \
  --json databaseId,status,conclusion,headBranch,headSha,createdAt,url
```

Select the run created after this dispatch with `headBranch: main`, record its ID, then wait:

```bash
gh run watch "$CHANGESET_CREATE_RUN" --repo kimohy/ard-ossie-provider --exit-status
```

- [ ] **Step 3: Verify all three managed PRs and exact path ownership**

Derive branches exactly:

```bash
CENTRAL_BRANCH="ard/changeset-$CHANGESET_ID"
FIRST_TRACKING_BRANCH="ard/$CHANGESET_ID-500138301"
SECOND_TRACKING_BRANCH="ard/$CHANGESET_ID-500138302"
gh pr list --repo kimohy/ard-ossie-provider --state open --limit 100 \
  --json number,title,isDraft,headRefName,headRefOid,baseRefName,url
```

Require one PR for each branch. The central PR changes only `registry/changesets/$CHANGESET_ID.json`; each Draft tracking PR changes only its owned marker under `products/<key>/changesets/$CHANGESET_ID.json`.

At each exact head, verify:

```bash
gh pr diff "$DEFINITION_PR" --repo kimohy/ard-ossie-provider --name-only
gh pr diff "$FIRST_TRACKING_PR" --repo kimohy/ard-ossie-provider --name-only
gh pr diff "$SECOND_TRACKING_PR" --repo kimohy/ard-ossie-provider --name-only
git fetch origin "$CENTRAL_BRANCH" "$FIRST_TRACKING_BRANCH" "$SECOND_TRACKING_BRANCH"
git show "origin/$CENTRAL_BRANCH:registry/changesets/$CHANGESET_ID.json" | jq .
git show "origin/$FIRST_TRACKING_BRANCH:products/500138301/changesets/$CHANGESET_ID.json" | jq .
git show "origin/$SECOND_TRACKING_BRANCH:products/500138302/changesets/$CHANGESET_ID.json" | jq .
```

Require exactly one target table ID, both required product IDs, empty readiness, blocked state, `status: required` in both markers, and `ard/changeset: pending` on both tracking heads. Central head must have both required statuses successful.

- [ ] **Step 4: Merge the exact initial definition head**

Run:

```bash
DEFINITION_HEAD=$(gh pr view "$DEFINITION_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)
gh pr checks "$DEFINITION_PR" --repo kimohy/ard-ossie-provider --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$DEFINITION_HEAD/status"
gh pr ready "$DEFINITION_PR" --repo kimohy/ard-ossie-provider
gh pr merge "$DEFINITION_PR" --repo kimohy/ard-ossie-provider --merge
gh pr view "$DEFINITION_PR" --repo kimohy/ard-ossie-provider \
  --json state,mergedAt,mergeCommit,headRefOid,url
```

Stop if the head changed between verification and merge.

- [ ] **Step 5: Prove the definition merge has no release target**

Find the `ARD numeric release` run whose `headSha` is the definition merge commit:

```bash
gh run list --repo kimohy/ard-ossie-provider --workflow ard-release.yml --limit 10 \
  --json databaseId,status,conclusion,headSha,createdAt,url
gh run watch "$DEFINITION_RELEASE_RUN" --repo kimohy/ard-ossie-provider --exit-status
gh run view "$DEFINITION_RELEASE_RUN" --repo kimohy/ard-ossie-provider --log
```

Require detect success/no-op with `products=[]`, `tables=[]`, release and linkage jobs skipped, and no new product/table numeric tag or Release. Add literal PR/run/head/merge facts to the state JSON with `apply_patch`.

---

### Task 4: Populate both canonical tracking PRs through public Issue intake

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository file changes.

**Interfaces:**
- Consumes: canonical tracking PR numbers/branches and the one modified XLSX from Task 2.
- Produces: two verified exact tracking heads containing the same table-v2 Registry blob and XLSX LFS OID.

- [ ] **Step 1: Invoke the browser skill and open the public Issue form**

Invoke `gstack:browse` before browser automation. Open:

```text
https://github.com/kimohy/ard-ossie-provider/issues/new?template=ard-content.yml
```

Do not use a generic Issue body because the trusted parser requires the form headings and one GitHub attachment link in each upload field.

- [ ] **Step 2: Submit the `500138301` v3 update Issue**

Use exactly:

```text
Title: [ARD] Marketing Insight shared-table v3
Public publication authorization: checked
Operation: update
Product key: 500138301
Existing product ID: prd_019ff10c-8be8-79d0-af07-21450abedf9e
Requested version: 3
Display name: Marketing Insight
Description: 이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습을 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
Changeset ID: the exact generated cst_ ID
Product HTML: .ard/run/shared-table-e2e-sources/500138301.html
Semantic document: .ard/run/shared-table-e2e-sources/semantic.pdf
Data dictionary: .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
Change reason: campaign_status 합성 상태값의 허용 범위를 Data Dictionary에 명시합니다.
```

Capture the created Issue number and URL.

- [ ] **Step 3: Submit the `500138302` v2 update Issue**

Use exactly:

```text
Title: [ARD] Campaign Governance Monitor shared-table v2
Public publication authorization: checked
Operation: update
Product key: 500138302
Existing product ID: prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d
Requested version: 2
Display name: Campaign Governance Monitor
Description: 이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석을 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
Changeset ID: the exact generated cst_ ID
Product HTML: .ard/run/shared-table-e2e-sources/500138302.html
Semantic document: .ard/run/shared-table-e2e-sources/semantic.pdf
Data dictionary: the same .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
Change reason: campaign_status 합성 상태값의 허용 범위를 Data Dictionary에 명시합니다.
```

Capture the created Issue number and URL.

- [ ] **Step 4: Verify both Issue bodies and uploaded attachment identities before approval**

Run:

```bash
gh issue view "$FIRST_UPDATE_ISSUE" --repo kimohy/ard-ossie-provider --json number,title,body,labels,url
gh issue view "$SECOND_UPDATE_ISSUE" --repo kimohy/ard-ossie-provider --json number,title,body,labels,url
```

Extract the three `github.com/user-attachments/files/...` URLs from each body. Download each attachment to a fresh temporary directory with `curl --fail --location --proto '=https'`, then require:

- each product-specific HTML equals its Task 2 hash;
- both semantic PDFs equal `ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6`;
- both uploaded XLSX files equal the single Task 2 modified-XLSX hash;
- operation, product ID, requested version, display name, description, changeset ID, and change reason exactly match Steps 2 and 3.

Only after every assertion passes, run:

```bash
gh issue edit "$FIRST_UPDATE_ISSUE" --repo kimohy/ard-ossie-provider --add-label ard:approved
gh issue edit "$SECOND_UPDATE_ISSUE" --repo kimohy/ard-ossie-provider --add-label ard:approved
```

- [ ] **Step 5: Wait for trusted intake and owner-controlled LLM approvals**

Watch the two `ARD Issue intake` runs associated with the exact Issue numbers. If GitHub pauses at `ard-llm`, ask the owner to approve that deployment. Do not approve or reroute it programmatically.

Require both workflows to conclude successfully and to reuse the two Task 3 PR numbers and canonical branch names. Stop if either workflow creates a replacement PR.

- [ ] **Step 6: Verify each final exact tracking head**

For each PR, record the new `headRefOid` and verify it differs from the marker-only head. At the immutable head require:

- marker remains at `products/<key>/changesets/$CHANGESET_ID.json`;
- `product.yaml` is `operation: update`, has the exact product ID, current `base_version`, next version, and active `$CHANGESET_ID`;
- `quality/validation-report.json` has `status: verified`, `publishable: true`, and no findings;
- `quality/quality-report.json` has zero hard errors;
- target table is v2, other tables remain v1;
- `ard/quality-gate` is successful and `ard/changeset` is pending only because readiness is incomplete;
- changed paths contain only the owned product subtree and product/locator-matched Registry records.

Use immutable reads:

```bash
git fetch origin "$FIRST_TRACKING_BRANCH" "$SECOND_TRACKING_BRANCH"
git show "$FIRST_TRACKING_HEAD:products/500138301/product.yaml"
git show "$SECOND_TRACKING_HEAD:products/500138302/product.yaml"
git show "$FIRST_TRACKING_HEAD:products/500138301/quality/validation-report.json" | jq .
git show "$SECOND_TRACKING_HEAD:products/500138302/quality/validation-report.json" | jq .
gh api "repos/kimohy/ard-ossie-provider/commits/$FIRST_TRACKING_HEAD/status"
gh api "repos/kimohy/ard-ossie-provider/commits/$SECOND_TRACKING_HEAD/status"
```

- [ ] **Step 7: Cross-compare both table results**

Run:

```bash
git show "$FIRST_TRACKING_HEAD:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json" | sha256sum
git show "$SECOND_TRACKING_HEAD:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json" | sha256sum
git show "$FIRST_TRACKING_HEAD:products/500138301/sources/dictionary/dictionary.xlsx"
git show "$SECOND_TRACKING_HEAD:products/500138302/sources/dictionary/dictionary.xlsx"
```

The two table JSON blobs must be byte-identical. Both LFS pointer texts must contain the same modified XLSX SHA-256 OID. The target `schema_hash` must equal the baseline and `canonical_hash` must differ. Compare the other three Registry table blobs against `origin/main` and require byte identity.

Add both Issue identities, attachment hashes, run IDs, PR numbers, exact heads, status URLs, LFS OID, target table blob hash, schema/canonical hashes, and unchanged-table hashes to the ignored state JSON with `apply_patch`.

Do not merge either product PR and do not merge `main` into either recorded head after this point.

---

### Task 5: Accumulate exact-head readiness

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository file changes.

**Interfaces:**
- Consumes: two final exact tracking PR heads from Task 4.
- Produces: one central readiness PR with exact `1/2` then `2/2` evidence and successful tracking statuses.

- [ ] **Step 1: Mark `500138301` v3 ready**

Run:

```bash
gh workflow run ard-changeset.yml \
  --repo kimohy/ard-ossie-provider \
  --ref main \
  -f mode=ready \
  -f changeset_id="$CHANGESET_ID" \
  -f product_id=prd_019ff10c-8be8-79d0-af07-21450abedf9e \
  -f version=3 \
  -f pr_number="$FIRST_TRACKING_PR" \
  -f head_sha="$FIRST_TRACKING_HEAD"
```

Capture and watch the new coordinator run. Require `ready_count=1`, `required_count=2`, changeset state blocked, and both exact tracking statuses pending.

- [ ] **Step 2: Mark `500138302` v2 ready**

Run:

```bash
gh workflow run ard-changeset.yml \
  --repo kimohy/ard-ossie-provider \
  --ref main \
  -f mode=ready \
  -f changeset_id="$CHANGESET_ID" \
  -f product_id=prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d \
  -f version=2 \
  -f pr_number="$SECOND_TRACKING_PR" \
  -f head_sha="$SECOND_TRACKING_HEAD"
```

Capture and watch the run. Require `ready_count=2`, `required_count=2`, changeset state ready, and `ard/changeset: success` on both recorded exact tracking heads.

- [ ] **Step 3: Verify the readiness PR payload and exact head**

The coordinator reopens a PR on `ard/changeset-$CHANGESET_ID`. Require its diff to contain only `registry/changesets/$CHANGESET_ID.json`. At its exact head, the JSON must contain both exact triples:

```text
prd_019ff10c-8be8-79d0-af07-21450abedf9e: version 3, first tracking PR number, first exact head SHA
prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d: version 2, second tracking PR number, second exact head SHA
```

Run:

```bash
READINESS_HEAD=$(gh pr view "$READINESS_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)
gh pr diff "$READINESS_PR" --repo kimohy/ard-ossie-provider --name-only
git fetch origin "$CENTRAL_BRANCH"
git show "$READINESS_HEAD:registry/changesets/$CHANGESET_ID.json" | jq .
gh pr checks "$READINESS_PR" --repo kimohy/ard-ossie-provider --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$READINESS_HEAD/status"
```

- [ ] **Step 4: Pause for readiness-merge approval**

Present the readiness PR URL, exact head, two readiness triples, status result, and the expected no-op release behavior to the user. Do not merge until the user explicitly approves.

---

### Task 6: Merge readiness and prove the all-future no-op

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository file changes.

**Interfaces:**
- Consumes: explicit user approval and unchanged `READINESS_HEAD`.
- Produces: merged ready record with no numeric release target.

- [ ] **Step 1: Re-check and merge the approved exact head**

Run:

```bash
test "$(gh pr view "$READINESS_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)" = "$READINESS_HEAD"
gh pr ready "$READINESS_PR" --repo kimohy/ard-ossie-provider
gh pr merge "$READINESS_PR" --repo kimohy/ard-ossie-provider --merge
gh pr view "$READINESS_PR" --repo kimohy/ard-ossie-provider \
  --json state,mergedAt,mergeCommit,headRefOid,url
```

- [ ] **Step 2: Verify the all-future release no-op**

Find and watch the release run for the readiness merge commit. Require:

```text
detect conclusion: success
products: []
tables: []
release job: skipped
linkage job: skipped
```

Require no new numeric product/table tag or GitHub Release. Record the exact merge/run evidence.

- [ ] **Step 3: Pause for first-product merge approval**

Re-read the first tracking PR and require `headRefOid == FIRST_TRACKING_HEAD`, both required statuses successful, and no unresolved conversation. Present the PR URL/head and expected mixed current/future no-op to the user. Do not merge until explicitly approved.

---

### Task 7: Merge the first product and prove the mixed-version no-op

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository file changes.

**Interfaces:**
- Consumes: explicit approval and unchanged `FIRST_TRACKING_HEAD`.
- Produces: product `500138301` v3 on `main` while the entire changeset remains unreleased.

- [ ] **Step 1: Merge only the approved exact first-product head**

Run:

```bash
test "$(gh pr view "$FIRST_TRACKING_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)" = "$FIRST_TRACKING_HEAD"
gh pr ready "$FIRST_TRACKING_PR" --repo kimohy/ard-ossie-provider
gh pr merge "$FIRST_TRACKING_PR" --repo kimohy/ard-ossie-provider --merge
gh pr view "$FIRST_TRACKING_PR" --repo kimohy/ard-ossie-provider \
  --json state,mergedAt,mergeCommit,headRefOid,url
```

- [ ] **Step 2: Verify the mixed current/future release no-op**

Watch the release run for the first-product merge commit. Require detect success with `products=[]`, `tables=[]`, release/linkage skipped, and no v3/v2 numeric tags or Releases created. This proves the directly changed table path is removed while one required product remains future.

- [ ] **Step 3: Pause for final-product merge approval**

Re-read the second tracking PR and require `headRefOid == SECOND_TRACKING_HEAD`, both statuses successful, and no unresolved conversation. Present:

- the exact PR/head;
- the readiness JSON containing that head;
- the equal table-v2 blob and XLSX OID;
- expected release targets `500138301`, `500138302`, and the one target table;
- the fact that this merge creates the coordinated numeric Releases.

Do not merge until explicitly approved.

---

### Task 8: Merge the final product and verify the atomic release

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- No tracked repository file changes.

**Interfaces:**
- Consumes: explicit approval and unchanged `SECOND_TRACKING_HEAD`.
- Produces: both coordinated product Releases and table v2 from one final merge commit.

- [ ] **Step 1: Merge only the approved exact final head**

Run:

```bash
test "$(gh pr view "$SECOND_TRACKING_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)" = "$SECOND_TRACKING_HEAD"
gh pr ready "$SECOND_TRACKING_PR" --repo kimohy/ard-ossie-provider
gh pr merge "$SECOND_TRACKING_PR" --repo kimohy/ard-ossie-provider --merge
gh pr view "$SECOND_TRACKING_PR" --repo kimohy/ard-ossie-provider \
  --json state,mergedAt,mergeCommit,headRefOid,url
```

The returned merge commit is `FINAL_RELEASE_COMMIT`.

- [ ] **Step 2: Verify exact release detection**

Watch the release run whose `headSha` is `FINAL_RELEASE_COMMIT`. Require exact sorted outputs:

```json
{"products":["500138301","500138302"],"tables":["tbl_01a00585-94b8-7e49-ac43-97e00a165e26"]}
```

No other product or table may be present.

- [ ] **Step 3: Wait for production-linkage approval and completion**

When both matrix linkage jobs pause at `production-linkage`, ask the owner to approve them. After approval, require detect, both release jobs, and both linkage jobs to conclude successfully.

- [ ] **Step 4: Verify Registry and numeric tag identities**

Fetch `main` and tags, then verify:

```bash
git fetch origin main --tags
git show origin/main:registry/products/prd_019ff10c-8be8-79d0-af07-21450abedf9e.json | jq -e '.version == 3'
git show origin/main:registry/products/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d.json | jq -e '.version == 2'
git show origin/main:registry/tables/tbl_01a00585-94b8-7e49-ac43-97e00a165e26.json | jq -e '.version == 2'
git rev-parse 'product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v3^{}'
git rev-parse 'product/prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d/v2^{}'
git rev-parse 'table/tbl_01a00585-94b8-7e49-ac43-97e00a165e26/v2^{}'
```

All three peeled tag targets must equal `FINAL_RELEASE_COMMIT`. The other three tables must remain v1 and no table v3 tag may exist.

- [ ] **Step 5: Verify Release assets and downstream status**

Use `gh release view` for both product tags. Download assets into separate temporary directories, record SHA-256 digests, and compare them with each release-result artifact. Require exact downstream dispatch statuses on `FINAL_RELEASE_COMMIT` to be successful.

- [ ] **Step 6: Re-run the exact final workflow and prove convergence**

Run:

```bash
gh run rerun "$FINAL_RELEASE_RUN" --repo kimohy/ard-ossie-provider
gh run watch "$FINAL_RELEASE_RUN" --repo kimohy/ard-ossie-provider --exit-status
```

Approve linkage again only if GitHub creates a new owner-controlled deployment request. Require all tag, Release asset, and dispatch operations to converge without changing target commits or asset digests. Record original attempt and rerun attempt identities from the same run.

---

### Task 9: Clear `500138301` independently

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- Create locally: `.ard/run/shared-table-e2e-sources/500138301-clear-v4.html`
- No tracked repository file changes.

**Interfaces:**
- Consumes: coordinated v3 product sources and table-v2 dictionary.
- Produces: a product-only v4 release with `changeset_id: null` and no table release.

- [ ] **Step 1: Prepare exact v4 product metadata**

Reuse the Task 2 HTML, whose bytes are preserved by the coordinated v3 update:

```bash
cp .ard/run/shared-table-e2e-sources/500138301.html \
  .ard/run/shared-table-e2e-sources/500138301-clear-v4.html
rg -F -o '이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습을 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.' \
  .ard/run/shared-table-e2e-sources/500138301-clear-v4.html | wc -l
```

Require count `1`. Use `apply_patch` to replace that one exact old sentence with:

```text
이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습, 상태별 비교를 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
```

Retain the current semantic PDF and modified XLSX byte-for-byte. Record all three upload hashes.

- [ ] **Step 2: Submit and verify the public v4 Issue**

Invoke `gstack:browse` and submit operation `update`, key `500138301`, exact existing product ID, requested version `4`, display name `Marketing Insight`, the exact description above, empty Changeset ID, matching modified HTML, unchanged PDF/XLSX, and change reason:

```text
독립 제품 설명을 보강하고 완료된 changeset의 활성 연결을 해제합니다.
```

Download and hash all three uploaded attachments and verify the complete Issue body before applying `ard:approved`.

- [ ] **Step 3: Verify the exact product-only PR and pause**

After trusted processing succeeds, require:

- `product.yaml` version 4 with `base_version: 3` and `changeset_id: null`;
- no `registry/tables/**` or historical changeset/marker deletion;
- validation verified/publishable, zero hard errors;
- exact-head required statuses successful.

Present the Issue, PR, exact head, changed paths, and expected product-only release to the user. Do not merge until explicitly approved.

- [ ] **Step 4: Merge and verify the v4 product-only release**

After approval, re-check the exact head, merge, approve `production-linkage` when requested, and require release detection to contain only `500138301` with `tables=[]`. Verify the v4 tag/Release targets that merge commit, target table remains v2, old changeset JSON remains, and both marker files remain.

---

### Task 10: Clear `500138302` independently

**Files:**
- Modify locally: `.ard/run/shared-table-e2e-state.json`
- Create locally: `.ard/run/shared-table-e2e-sources/500138302-clear-v3.html`
- No tracked repository file changes.

**Interfaces:**
- Consumes: coordinated v2 product sources and table-v2 dictionary.
- Produces: a product-only v3 release with `changeset_id: null` and final durable lifecycle state.

- [ ] **Step 1: Prepare exact v3 product metadata**

Reuse the Task 2 HTML, whose bytes are preserved by the coordinated v2 update:

```bash
cp .ard/run/shared-table-e2e-sources/500138302.html \
  .ard/run/shared-table-e2e-sources/500138302-clear-v3.html
rg -F -o '이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석을 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.' \
  .ard/run/shared-table-e2e-sources/500138302-clear-v3.html | wc -l
```

Require count `1`. Use `apply_patch` to replace that one exact old sentence with:

```text
이 데이터는 가상 캠페인과 소재의 상태, 집행 및 성과 신호를 함께 살펴보고 합성 데이터 거버넌스 분석과 상태별 비교를 연습하도록 구성되었습니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.
```

Retain the current semantic PDF and modified XLSX byte-for-byte. Record all three upload hashes.

- [ ] **Step 2: Submit and verify the public v3 Issue**

Invoke `gstack:browse` and submit operation `update`, key `500138302`, exact existing product ID, requested version `3`, display name `Campaign Governance Monitor`, the exact description above, empty Changeset ID, matching modified HTML, unchanged PDF/XLSX, and the same independent-clear change reason from Task 9.

Download and hash all three uploaded attachments and verify the complete Issue body before applying `ard:approved`.

- [ ] **Step 3: Verify the exact product-only PR and pause**

After trusted processing succeeds, require product version 3 with `base_version: 2`, `changeset_id: null`, no table or historical-audit mutation, verified/publishable validation, zero hard errors, and successful exact-head statuses. Present all evidence and wait for explicit merge approval.

- [ ] **Step 4: Merge and verify final durable state**

After approval, re-check the head, merge, approve `production-linkage` when requested, and require only `500138302` with `tables=[]` in release detection.

At final `origin/main`, require:

```text
500138301 current version: 4
500138302 current version: 3
both product.yaml changeset_id: null
marketing_campaign current version: 2
other three tables: version 1
table v3 tag: absent
completed central changeset JSON: present
both product marker files: present
```

Record final product/table versions, exact PR/head/merge/run/release/linkage identities, and retained audit paths.

---

### Task 11: Publish acceptance evidence and close the roadmap item

**Files:**
- Create: `docs/acceptance/shared-table-changeset-e2e-verification.md`
- Modify: `docs/next-steps.md`

**Interfaces:**
- Consumes: immutable evidence and literal values accumulated in the local state JSON.
- Produces: reproducible acceptance record and checked Shared-table roadmap bullets on `main`.

- [ ] **Step 1: Start a clean documentation branch from final `main`**

Run:

```bash
git fetch origin main --tags --prune
git switch -c docs/shared-table-changeset-e2e-verification origin/main
git status --short --branch
```

Ignored upload sources and state JSON must not appear in the tracked diff.

- [ ] **Step 2: Write the evidence report with literal facts**

Use `apply_patch` to create `docs/acceptance/shared-table-changeset-e2e-verification.md`. Include direct GitHub links and exact values for:

- revised design/plan PR head, merge commit, required checks, and local test counts;
- fixed product IDs, baseline versions, shared table mappings, source hashes, and table Registry hashes;
- changeset ID, create run, definition PR/head/merge, and initial no-op release run;
- both update Issues, attachment hashes, intake/processor runs, canonical PRs, and exact heads;
- equal modified XLSX LFS OIDs and target table-v2 Registry blobs;
- unchanged schema hash, changed canonical hash, and byte-identical other-table records;
- `1/2 blocked` and `2/2 ready` coordinator evidence;
- readiness all-future no-op, first-product mixed no-op, final full expansion, and rerun convergence;
- all three coordinated tags targeting the final merge commit, both Release asset digests, and downstream statuses;
- both independent-clear Issues/PRs/releases and product-only detection;
- final null active bindings, final versions, completed changeset JSON, and retained markers;
- every failed intermediate run, its diagnosis, and the final converged state.

Do not include secrets, signed URLs, provider request bodies, placeholders, or claims that GitHub Release is natively immutable. Describe the tags/assets as repository-enforced conflict-protected publication.

- [ ] **Step 3: Update only the five Shared-table roadmap bullets**

Use `apply_patch` in `docs/next-steps.md` to check the five Shared-table changeset E2E bullets and add a link to the new acceptance report. Leave every operations-protection, failure-drill, stabilization, and P3 entry unchanged.

- [ ] **Step 4: Run final verification**

Run:

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
uv run --frozen ard workflow repository-check \
  --base-ref origin/main \
  --head-ref HEAD \
  --head-sha "$(git rev-parse HEAD)" \
  --repository . \
  --verification-group static
git diff --check
rg -n 'TBD|TODO|PLACEHOLDER|<changeset|<product|<run|<sha' \
  docs/acceptance/shared-table-changeset-e2e-verification.md
```

All validation commands must exit `0`; the final `rg` must return no matches. Cross-check every report identifier against GitHub.

- [ ] **Step 5: Commit and publish the evidence PR**

Run:

```bash
git add docs/acceptance/shared-table-changeset-e2e-verification.md docs/next-steps.md
git diff --cached --check
git commit -m "docs: verify shared-table changeset e2e"
git push -u origin docs/shared-table-changeset-e2e-verification
gh pr create --draft \
  --repo kimohy/ard-ossie-provider \
  --base main \
  --head docs/shared-table-changeset-e2e-verification \
  --title "docs: verify shared-table changeset e2e" \
  --body "Records reproducible evidence for the completed two-product shared-table lifecycle and updates only its roadmap checklist."
```

Require exactly the two documentation paths, successful required statuses on the exact head, and no release workflow trigger. Mark ready and merge through protected `main`.

- [ ] **Step 6: Invoke completion verification**

Invoke `superpowers:verification-before-completion`. Completion may be claimed only when:

1. all fresh local checks pass;
2. the evidence PR is merged to `origin/main`;
3. the report contains no placeholders, secrets, or unsupported immutability claim;
4. all five Shared-table roadmap bullets are checked and no unrelated bullet changed;
5. every recorded GitHub tag, Release, PR, run, status, commit, and asset digest still matches the published evidence.
