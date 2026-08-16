# Direct Branch `v2` Acceptance Design

**Date:** 2026-08-16
**Status:** Approved

## 1. Goal

Complete the next P1 acceptance item by updating the existing synthetic Marketing Insight
product through the same-repository direct-branch workflow. The acceptance must prove that a
product-only source change advances the product from `v1` to `v2` while all four unchanged
tables remain at `v1`.

The resulting GitHub history must connect the exact candidate commit, trusted coordinator run,
processor writeback, required statuses, merge commit, immutable tags, Release bundle, and
downstream dispatch.

## 2. Scope

The human-authored candidate changes are limited to:

- `products/500138301/product.yaml`
- `products/500138301/sources/product-info/product.html`

The candidate metadata changes from create to update while preserving identity:

```yaml
operation: update
product_id: prd_019ff10c-8be8-79d0-af07-21450abedf9e
product_key: '500138301'
base_version: 1
version: 2
```

`display_name`, `changeset_id`, and `tables` remain unchanged. The description in both the
HTML source and `product.yaml` becomes:

> 이 데이터는 가상 캠페인, 소재, 집행 및 성과 신호를 설명하고 캠페인 성과 탐색과 AI 분석 실습을 지원하는 합성 데이터입니다. 실제 고객, 계정, 광고 플랫폼, 조직 또는 운영 수치를 포함하지 않으며, 상세 구조는 첨부된 Data Dictionary에서 확인할 수 있습니다.

The semantic PDF and dictionary XLSX are not modified. Generated files, quality reports, and
Registry files are never edited by hand; only the trusted processor may update them.

This acceptance does not change table schema, metric SQL, semantic content, workflow code,
release code, or repository protection settings.

## 3. Branch and Trusted Processing Flow

The candidate branch is `acceptance/marketing-insight-v2`, based on the current remote `main`.
It does not carry this design document or the implementation plan; those stay on the planning
branch so the product PR remains a single-product data change. The source HTML change ensures
that `ARD direct branch signal` runs on push. Its completed run causes the default-branch `ARD
trusted direct branch coordinator` to perform the following flow:

1. Load trusted workflow and CLI code from the default branch.
2. Check out the exact candidate SHA as credential-free data.
3. Detect exactly one changed product.
4. Revalidate candidate sources using the protected `ard-llm` environment.
5. Create or reuse one Draft product PR for the branch.
6. Call the reusable processor with the exact validated candidate head.
7. Commit generated, quality, product metadata, and Registry results back to the candidate
   branch.
8. Publish `ard/quality-gate` and `ard/changeset` against the processor's final PR head.

The candidate checkout is input data only. Trusted CLI execution always comes from the default
branch. No direct candidate code execution, force update, or manual generated-file repair is
allowed.

## 4. Expected Version and Identity Result

The processed result must satisfy all of these invariants:

- Product ID remains `prd_019ff10c-8be8-79d0-af07-21450abedf9e`.
- Product key remains `500138301`.
- Product version becomes exactly `2`.
- All 11 existing metric IDs remain stable, and the relationship list remains empty.
- The four existing table IDs remain unchanged.
- Every Registry mapping keeps `table_version: 1`.
- Every table Registry record remains byte-identical to its `v1` state.
- Product documentation describes only the approved description change.
- The quality report contains zero hard errors.
- PDF validation remains `verified` and `publishable=true`; review debt must remain absent.

A changed table version, a new table identity, an unrequested metric change, or any manual
generated/Registry edit blocks merge and requires investigation.

## 5. Merge and Release Gate

The Draft PR may be marked ready and merged only after:

- the coordinator and processor identify the expected exact candidate and final heads;
- generated, quality, and Registry changes belong to the trusted processor commit;
- the product/table version invariants above are confirmed;
- `ard/quality-gate` and `ard/changeset` both succeed on the final PR head; and
- no secret, credential, private-key material, or unexpected source payload appears in the diff
  or logs.

After merge, `ARD numeric release` must create immutable product tag
`product/prd_019ff10c-8be8-79d0-af07-21450abedf9e/v2`. No `v2` table tag is expected because
the table contents do not change. The Release manifest and downloaded assets must agree by
SHA-256. The `production-linkage` environment is currently restricted to `main` by branch
policy but has no required-reviewer rule in the GitHub API read-back. This acceptance records
that actual state, observes the resulting dispatch without claiming a manual approval gate, and
requires the same release input to converge to a no-op when retried. Adding an environment
reviewer is outside P1 and remains an operations-protection follow-up.

## 6. Failure Handling and Negative Acceptance

Failed coordinator or processor runs are retained as evidence. Generated files and Registry
records are not repaired manually. A corrected source commit creates a new exact head and lets
the trusted workflow converge normally. If the processor reports partial mutation, its result
envelope and mutation journal are preserved and the existing reconcile path is used; immutable
tags and managed branches are never force-moved.

After the successful `v2` merge, two same-repository source-change probes provide version
rejection evidence:

- **Stale base:** current product is `v2`, but the candidate uses `base_version: 1` and
  `version: 2`. Processing must fail closed with the stale-version diagnostic and must not
  publish a product update.
- **Skipped version:** current product is `v2`, but the candidate uses `base_version: 2` and
  `version: 4`. Processing must fail closed with the version-gap diagnostic and must not publish
  a product update.

Each probe changes only the single product source required to trigger the direct signal. Its
failed run URL, candidate SHA, diagnostic, and absence of writeback are recorded.

The current operator account cannot create a fork of its own repository. Fork writeback is
therefore verified through the workflow repository-identity guard and the existing automated
workflow contract tests. The acceptance record explicitly distinguishes this automated guard
evidence from the two live same-repository rejection runs.

## 7. Verification and Evidence

Before the remote push:

- run the full pytest suite and Ruff on the current remote baseline and candidate;
- confirm the authored diff is limited to `product.yaml` and product HTML;
- record the semantic PDF, dictionary XLSX LFS pointer, and four table Registry SHA-256 values;
- verify no secret or sensitive source material was introduced.

For the successful remote flow, record:

- direct signal and trusted coordinator run URLs;
- candidate and processor commit SHAs;
- Draft PR URL and final required-status targets;
- generated and Registry product version;
- stable product, table, metric, and relationship IDs;
- unchanged table versions and Registry hashes;
- merge commit, product tag target, Release URL, asset names, manifest hash, and asset SHA-256;
- `production-linkage` policy read-back, dispatch result, and idempotent retry result.

After fetching the merged history and tags, run:

```bash
uv run --frozen ard history 500138301
uv run --frozen ard diff '500138301@v1..v2'
```

The CLI history and diff, Git diff, Registry state, tag, and Release must describe the same
product-only change.

Durable evidence is written to
`docs/acceptance/direct-branch-v2-verification.md`. When every success and rejection condition is
met, `docs/next-steps.md` is updated to mark direct-branch `v2` acceptance complete. Run URLs,
commit/tag links, artifact hashes, status conclusions, and redacted diagnostics are recorded;
secret values and raw private payloads are not.

## 8. Completion Criteria

This work is complete when:

1. The direct branch produces one trusted processor-managed PR for Marketing Insight `v2`.
2. The product advances to `v2` while all four table identities and versions remain at `v1`.
3. Required statuses succeed on the exact final PR head and the PR is merged.
4. The immutable product `v2` tag, Release bundle, manifest hashes, and main-only environment
   dispatch agree.
5. `ard history` and `ard diff` match Git and Registry history.
6. Live stale-base and skipped-version probes fail closed without writeback.
7. Fork secret/writeback isolation remains covered by repository-identity guards and automated
   contract tests.
8. The acceptance evidence and next-step roadmap are updated without exposing secrets.
