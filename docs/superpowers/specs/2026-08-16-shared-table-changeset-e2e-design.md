# Shared-table changeset production E2E design

## Objective

Complete the next unfinished roadmap item by proving the shared-table changeset lifecycle in the production GitHub repository. The acceptance must create a second durable synthetic product, coordinate one shared-table change across both products, publish the products and table atomically after the final product merge, and clear the active changeset binding in later independent product updates without deleting the audit history.

The test uses only synthetic Marketing Insight material. It must not introduce customer, account, platform, organization, or operational data.

## Current baseline

- Repository default branch: `main`.
- Existing product key: `500138301`.
- Existing product ID: `prd_019ff10c-8be8-79d0-af07-21450abedf9e`.
- Existing product version: `2`.
- Existing physical tables: four version-1 tables under `synthetic_workspace.marketing_insight`.
- Shared-table target: `marketing_campaign`.
- Target table ID: `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`.
- The repository retains merged branches (`deleteBranchOnMerge: false`).
- `ard-changeset.yml` creates a central coordination PR and one Draft tracking PR per required product.
- Product and table versions are current-only Registry values. Historical states live in Git commits, immutable numeric tags, Releases, and changeset records.

The current Marketing Insight source PDF and XLSX are Git LFS objects. Authoring and local validation therefore require materialized binaries, not pointer files. Before modifying either source, the working copy must contain the expected PDF and XLSX signatures and the baseline LFS object IDs recorded by the v2 acceptance report.

## Scope

### Included

1. Add lifecycle regression coverage and the minimum implementation needed for a merged coordination branch and future readiness versions.
2. Create and release a second durable synthetic product.
3. Reuse the existing four physical table IDs and table versions in the second product.
4. Create one changeset for the existing `marketing_campaign` table and both products.
5. Process both tracking PRs with an identical semantic-only table change.
6. Accumulate exact-head readiness, merge the readiness PR before the product PRs, and publish the entire changeset only after the final product merge.
7. Perform one later independent update per product that clears `changeset_id` while leaving the historical changeset and markers intact.
8. Record local and GitHub evidence in a durable acceptance report and update only the completed shared-table roadmap item.

### Excluded

- Review-protection or `production-linkage` Environment changes.
- Failure-drill backlog items unrelated to the changeset lifecycle.
- New physical schemas or new table identities.
- Destructive cleanup of branches, runs, PRs, Releases, tags, changeset records, or tracking markers.
- Force pushes, tag movement, branch overwrites, generated-artifact hand editing, and Release asset replacement.
- Retire/tombstone behavior and representative-PDF stabilization work.

## Chosen approach

Create product key `500138302` with display name `Campaign Governance Monitor`. It uses a distinct product description and product HTML metadata but reuses the existing Marketing Insight semantic PDF, dictionary structure, and all four physical table locators. This gives the product a distinct canonical product identity while exercising locator-based table reuse.

The second product is a permanent acceptance fixture. It is not deleted or retired after the E2E.

Alternatives that created a new partial schema or two entirely new products were rejected because they add PDF/XLSX authoring and LLM variability without improving the shared-table contract under test.

## Required lifecycle fixes

Production execution is gated on two local regression contracts.

### Reusing a merged coordination branch

The initial changeset definition PR is merged before tracking products are processed. Because the repository retains merged branches, the remote coordination head becomes an ancestor of `main` and its diff against `main` is empty. A later `ready` dispatch must be allowed to commit the updated central JSON on that managed branch without deleting, force-pushing, or resetting it.

The changeset service may accept an existing managed branch when either:

1. its changed paths against the base are exactly the one allowed central changeset path; or
2. its head is an ancestor of the base and the changed-path set is empty because the prior central PR was merged.

All divergent branches and all branches containing unexpected paths remain fail-closed with the existing path-mismatch error.

### Future readiness before product merge

The readiness PR is merged before either product PR. At that point the changeset records the versions visible at the exact tracking heads, while the current Registry still contains the previous product versions.

Release detection must classify readiness versions as follows:

- readiness version greater than the current Registry version: a valid future transition; do not expand or partially release the changeset yet;
- readiness version equal to the current Registry version: current and eligible;
- readiness version lower than the current Registry version: stale audit state; fail with `CHANGESET_VERSION_NOT_CURRENT`.

Expansion is all-or-nothing. If any required product is still future, the changeset contributes no products and no tables to the release target. When every required version is current, release detection expands both products and the changed table in one result.

This behavior preserves stale-state rejection while preventing expected pre-merge states from producing red release runs.

## Data and version contract

### Seed product

Generate one new UUIDv7 product ID and keep it unchanged for the entire lifecycle.

The initial `500138302` release must satisfy:

- operation `create` and product version `1`;
- distinct display name, description, product key, and product HTML metadata;
- the same semantic PDF and dictionary table locators as `500138301`;
- exactly four mappings to the existing table IDs, all at `table_version: 1`;
- no new table record, no table version increment, and no table v2 tag;
- hard errors `0`, verified semantic validation, and `publishable: true`;
- one immutable product v1 tag and Release.

The second product may have generated prose or metric wording appropriate to its product context, but duplicate checks must not block it as a content copy. Metric SQL remains inside the existing safety boundary.

### Shared-table changeset

Generate one new UUIDv7 changeset ID and use it for the central record, both tracking markers, both product configs, and every readiness dispatch.

The changeset definition contains:

- table IDs: only `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`;
- required products: the existing Marketing Insight product and the new Campaign Governance Monitor product;
- empty readiness at initial definition time.

The table change updates only the dictionary description for `marketing_campaign.campaign_status` to state that the synthetic status is one of `Draft`, `Active`, `Paused`, or `Closed`. The two product branches must receive byte-identical modified XLSX content and therefore the same new LFS object ID.

The change must preserve column name, ordinal, type, nullability, primary-key state, locator, and all other table contents. Expected version effects are:

| Entity | Before | Changeset release |
|---|---:|---:|
| `500138301` product | 2 | 3 |
| `500138302` product | 1 | 2 |
| `marketing_campaign` table | 1 | 2 |
| Other three tables | 1 | 1 |

The target table schema hash remains unchanged and its canonical hash changes. The other table Registry blobs remain byte-identical.

## GitHub execution sequence

### Phase A: seed the second product

1. Materialize and verify the baseline LFS sources, then upload the three synthetic source files to a new public ARD Issue for product key `500138302`.
2. Submit operation `create`, version `1`, the distinct product metadata, an empty Existing product ID, and an empty Changeset ID.
3. Apply `ard:approved` only after the Issue body and immutable GitHub attachments match the approved payload.
4. Let the trusted Issue intake and processor author the new product branch, source tree, config, and generated artifacts.
5. Verify the exact processor head, allowed changed paths, quality reports, mappings, Registry identities, and both required statuses.
6. Merge the product PR only after the exact head is green.
7. Verify the v1 product tag, Release bundle, asset digest, and linkage result.

### Phase B: publish the changeset definition

1. Dispatch `ARD shared-table changeset coordinator` in `create` mode.
2. Verify one central definition PR and two Draft product tracking PRs.
3. Verify the central branch changes only `registry/changesets/<id>.json` and each tracking branch initially changes only its owned marker.
4. Confirm both tracking heads receive `ard/changeset: pending`.
5. Merge the initial central definition PR after its two required statuses succeed.
6. Verify the release workflow sees a blocked changeset and produces no product or table Release.

### Phase C: prepare both exact tracking heads

1. Prepare one public update Issue per product. Each Issue supplies the complete synthetic source set, the exact existing product ID, the current base version, the next product version, and the same Changeset ID.
2. Upload byte-identical modified XLSX files to both Issues and preserve each product's approved HTML metadata and semantic PDF.
3. Approve the Issues only after their attachment hashes and requested identities are recorded. Issue intake must reuse and populate the existing canonical tracking branches and PRs rather than creating replacement PRs.
4. Bring the merged definition record into both tracking branches without overwriting their managed markers.
5. Let intake set each product config to `operation: update`, the exact current `base_version`, the next product version, and the same active `changeset_id`.
6. Verify that version planning assigns `marketing_campaign` base version 1 and proposed version 2 while leaving the other table versions unchanged; no Issue field or manual generated edit supplies these decisions.
7. Run each trusted processor before merging either product PR.
8. Verify that the final tracking heads contain their product update, the required marker, the same table v2 Registry blob, and no unrelated files.
9. Verify both exact heads have successful quality gates while `ard/changeset` remains pending until readiness is complete.

Do not merge `main` into the second processed product branch after the first product PR merges. Both product heads are validated against the same table-v1 baseline and carry the same resulting table-v2 blob; changing a recorded head after readiness invalidates the changeset.

### Phase D: readiness and coordinated release

1. Dispatch `ready` for the first exact tracking head and verify `ready_count=1`, `required_count=2`, state `blocked`, and both tracking statuses `pending`.
2. Dispatch `ready` for the second exact tracking head and verify `ready_count=2`, `required_count=2`, state `ready`, and both tracking statuses `success`.
3. Verify the central readiness PR contains only the updated changeset JSON with both exact PR numbers, versions, and head SHAs.
4. Merge the readiness PR before either product PR. Release detection must succeed as a no-op because both readiness versions are future.
5. Merge the first product PR. Release detection must again succeed as a no-op because one required product is still future.
6. Merge the second product PR. Release detection must expand both products and the target table.
7. Release publication must verify that both recorded tracking PR heads are merged and their merge commits are ancestors of the final `main` commit.
8. Verify product v3, product v2, and table v2 tags all target the final product merge commit. Verify both product Release assets and downstream dispatches.
9. Re-run the final release workflow and verify tag, asset, and dispatch operations converge to no-op without changing digests.

## Clearing the active changeset

After the coordinated release, submit one genuine non-table product-description update Issue per product. Each Issue leaves Changeset ID empty so intake writes `changeset_id: null`, and each supplies matching updated product HTML metadata.

Expected versions after clearing are:

| Entity | Changeset release | Independent release |
|---|---:|---:|
| `500138301` product | 3 | 4 |
| `500138302` product | 2 | 3 |
| `marketing_campaign` table | 2 | 2 |
| Other three tables | 1 | 1 |

The follow-up releases must target only the independently changed product. They must not expand the historical changeset or create any new table tag.

The following remain permanently tracked:

- `registry/changesets/<changeset-id>.json` with its completed readiness evidence;
- both product tracking markers;
- coordination, readiness, tracking, and independent-update PRs;
- workflow runs, immutable tags, Releases, assets, and linkage statuses.

## Failure handling

Stop before merge if any of the following occurs:

- the seed product creates a fifth table ID or advances an existing table version;
- either product maps a different target table ID;
- a managed coordination or tracking branch contains an unexpected path;
- the first readiness result is not blocked at `1/2`;
- the completed readiness record does not contain the exact final tracking heads;
- the two tracking branches produce different table-v2 Registry blobs or XLSX LFS object IDs;
- a Release or numeric tag is created before the final required product merge;
- the final merge omits either product or the target table from release detection;
- clearing the active changeset advances any table version or re-expands the old changeset.

Before merge, preserve the Draft PR and failed run for diagnosis. After an incorrect main merge, use a new revert PR. Never force-update a protected branch or move an immutable tag. If a bad version has already been published, correct it with a new numeric version rather than replacing history.

## Verification strategy

### Local regression coverage

Add focused tests for:

1. a retained coordination branch whose head is an ancestor of the base and has an empty current diff;
2. rejection of a retained branch that diverges or contains unexpected paths;
3. a ready changeset whose required versions are all future returning no release targets;
4. a mixed current/future changeset returning no partial targets;
5. a fully current changeset expanding all products and tables;
6. a readiness version lower than current remaining fail-closed;
7. the workflow YAML and CLI contracts required by the production sequence.

Run at minimum:

```text
uv run --frozen pytest tests/unit/test_changeset_service.py tests/unit/test_release_detection_service.py tests/integration/test_workflow_changeset_cli.py tests/integration/test_workflow_release_detect_cli.py tests/integration/test_workflow_contracts.py -q
uv run --frozen pytest -q
uv run --frozen ruff check src tests
actionlint .github/workflows/*.yml
```

### Production evidence

Record evidence in `docs/acceptance/shared-table-changeset-e2e-verification.md`:

- generated product and changeset IDs;
- source, table, mapping, Registry, and Release hashes;
- every relevant branch, commit, PR, run, exact head, status, tag, Release, and asset digest;
- the seed, tracking-update, and independent-clear Issue numbers, labels, approved payload identities, and attachment hashes;
- the `1/2` and `2/2` readiness results;
- definition, readiness, first-product, final-product, rerun, and independent-update release-detection results;
- table schema/canonical hash comparison and unchanged-table hashes;
- active `changeset_id` clearing and historical-record preservation;
- any failed intermediate run and the final converged state, without secrets or provider payloads.

Update `docs/next-steps.md` only after all five Shared-table changeset E2E bullets have evidence. Leave operations protection, failure drills, representative PDF stabilization, and P3 backlog unchanged.

## Acceptance criteria

The work is complete only when all of the following are true:

1. Product `500138302` exists as a permanent, verified v1 Release and reuses the four existing v1 tables.
2. One real changeset coordinates the two products and only the `marketing_campaign` table.
3. Readiness progresses from blocked `1/2` to ready `2/2` using exact immutable tracking heads.
4. The definition and readiness PRs merge before product PRs without producing premature Releases or failed expected-state release runs.
5. The first product merge does not partially release the changeset.
6. The final product merge releases both products and table v2 from one commit, and rerun converges to no-op.
7. Later independent product updates clear both active changeset bindings, preserve table versions, and do not expand the historical changeset.
8. Local tests, the complete suite, Ruff, and actionlint pass.
9. The acceptance report contains reproducible evidence and no secrets.
10. The Shared-table roadmap item is checked without altering unrelated backlog.
