# Shared-table changeset production E2E design

## Objective

Complete the next unfinished roadmap item by proving the shared-table changeset lifecycle in the production GitHub repository. The acceptance reuses the two durable synthetic products that now exist, coordinates one shared-table change across both products, publishes the products and table atomically after the final product merge, and clears the active changeset binding in later independent product updates without deleting the audit history.

The test uses only synthetic Marketing Insight material. It must not introduce customer, account, platform, organization, or operational data.

## Current baseline

- Repository default branch: `main`.
- Existing product key: `500138301`.
- Existing product ID: `prd_019ff10c-8be8-79d0-af07-21450abedf9e`.
- Existing product version: `2`.
- Second product key: `500138302`.
- Second product ID: `prd_01a00ccd-9a0d-7683-95c1-1ed6bdb43c0d`.
- Second product version: `1`.
- Existing physical tables: four version-1 tables under `synthetic_workspace.marketing_insight`.
- Shared-table target: `marketing_campaign`.
- Target table ID: `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`.
- Both products already reuse the same four Registry table IDs.
- The repository retains merged branches (`deleteBranchOnMerge: false`).
- `ard-changeset.yml` creates a central coordination PR and one Draft tracking PR per required product.
- Product and table versions are current-only Registry values. Historical states live in Git commits, conflict-protected numeric tags, Releases, and changeset records.
- The retained-coordination-branch and future-readiness lifecycle fixes are already on `main` in commits `0f00f91305c0b0ec51e5b5e5371c9e8eb3553096`, `5b481e3a8bc8910ce125f0a1e2d708407a2f9ff4`, and `7d1354cf9af8176c1b6019dd4442a236f74c9515`.
- The read-only preflight on 2026-08-17 found no open PR, open Issue, changeset workflow run, or retained `cst_` coordination/tracking branch that could collide with this E2E.

The current PDF and XLSX are Git LFS objects shared by both products. Their pointers have PDF OID `ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6` and XLSX OID `10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a`. Authoring and local validation require independently materialized binaries with those hashes and the expected PDF/XLSX signatures, not pointer files.

## Scope

### Included

1. Re-verify the already-landed lifecycle regression contract before production mutation.
2. Create one changeset for the existing `marketing_campaign` table and both existing products.
3. Process both tracking PRs with an identical semantic-only table change.
4. Accumulate exact-head readiness, merge the readiness PR before the product PRs, and publish the entire changeset only after the final product merge.
5. Perform one later independent update per product that clears `changeset_id` while leaving the historical changeset and markers intact.
6. Record local and GitHub evidence in a durable acceptance report and update only the completed shared-table roadmap item.

### Excluded

- Review-protection or `production-linkage` Environment changes.
- Failure-drill backlog items unrelated to the changeset lifecycle.
- New physical schemas or new table identities.
- Creating or releasing a third product.
- Repeating the already-completed `500138302` create/v1 acceptance.
- New lifecycle implementation unless preflight exposes a regression.
- Destructive cleanup of branches, runs, PRs, Releases, tags, changeset records, or tracking markers.
- Force pushes, tag movement, branch overwrites, generated-artifact hand editing, and Release asset replacement.
- Retire/tombstone behavior and representative-PDF stabilization work.

## Chosen approach

Reuse product keys `500138301` and `500138302`. The second product is already a permanent acceptance fixture with a distinct product identity and the same four physical table locators, so the shared-table contract can be exercised without another create/intake/release cycle.

Directly authoring the two product branches was rejected because it would bypass the public Issue intake contract that must populate the canonical tracking PRs. Creating a third product was rejected because it adds product creation, LLM, identity, and release variability without improving the shared-table contract under test.

## Already-landed lifecycle prerequisites

Production execution is gated on re-verifying two regression contracts that are already implemented on `main`. This E2E does not open another lifecycle code PR unless those checks reveal a new defect.

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

### Shared-table changeset

Generate one new UUIDv7 changeset ID and use it for the central record, both tracking markers, both product configs, and every readiness dispatch.

The changeset definition contains:

- table IDs: only `tbl_01a00585-94b8-7e49-ac43-97e00a165e26`;
- required products: the existing Marketing Insight and Campaign Governance Monitor products;
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

### Phase A: publish the changeset definition

1. Dispatch `ARD shared-table changeset coordinator` in `create` mode.
2. Verify one central definition PR and two Draft product tracking PRs.
3. Verify the central branch changes only `registry/changesets/<id>.json` and each tracking branch initially changes only its owned marker.
4. Confirm both tracking heads receive `ard/changeset: pending`.
5. Merge the initial central definition PR after its two required statuses succeed.
6. Verify the release workflow sees a blocked changeset and produces no product or table Release.

### Phase B: prepare both exact tracking heads

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

### Phase C: readiness and coordinated release

1. Dispatch `ready` for the first exact tracking head and verify `ready_count=1`, `required_count=2`, state `blocked`, and both tracking statuses `pending`.
2. Dispatch `ready` for the second exact tracking head and verify `ready_count=2`, `required_count=2`, state `ready`, and both tracking statuses `success`.
3. Verify the central readiness PR contains only the updated changeset JSON with both exact PR numbers, versions, and head SHAs.
4. Merge the readiness PR before either product PR. Release detection must succeed as a no-op because both readiness versions are future.
5. Merge the first product PR. Release detection must again succeed as a no-op because one required product is still future.
6. Merge the second product PR. Release detection must expand both products and the target table.
7. Release publication must verify that both recorded tracking PR heads are merged and their merge commits are ancestors of the final `main` commit.
8. Verify product v3, product v2, and table v2 tags all target the final product merge commit. Verify both product Release assets and downstream dispatches.
9. Re-run the final release workflow and verify tag, asset, and dispatch operations converge to no-op without changing digests.

The initial definition merge may proceed after its exact head is green because it publishes only a blocked coordination record and must produce an empty release target. Pause for explicit user approval immediately before each of these release-sensitive merges:

1. the completed readiness PR;
2. the first product tracking PR;
3. the final product tracking PR.

Also pause before each later independent product-update merge because each publishes a numeric product Release. If a `production-linkage` deployment requests approval, wait for the repository owner rather than attempting to bypass the Environment gate.

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
- workflow runs, conflict-protected numeric tags, Releases, assets, and linkage statuses.

## Failure handling

Stop before merge if any of the following occurs:

- either product maps a different target table ID;
- a managed coordination or tracking branch contains an unexpected path;
- the first readiness result is not blocked at `1/2`;
- the completed readiness record does not contain the exact final tracking heads;
- the two tracking branches produce different table-v2 Registry blobs or XLSX LFS object IDs;
- a Release or numeric tag is created before the final required product merge;
- the final merge omits either product or the target table from release detection;
- clearing the active changeset advances any table version or re-expands the old changeset.

Before merge, preserve the Draft PR and failed run for diagnosis. After an incorrect main merge, use a new revert PR. Never force-update a protected branch or move an existing numeric tag. If a bad version has already been published, correct it with a new numeric version rather than replacing history.

## Verification strategy

### Local regression coverage

Re-run the focused tests that cover:

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

- the two fixed product IDs and generated changeset ID;
- source, table, mapping, Registry, and Release hashes;
- every relevant branch, commit, PR, run, exact head, status, tag, Release, and asset digest;
- the tracking-update and independent-clear Issue numbers, labels, approved payload identities, and attachment hashes;
- the `1/2` and `2/2` readiness results;
- definition, readiness, first-product, final-product, rerun, and independent-update release-detection results;
- table schema/canonical hash comparison and unchanged-table hashes;
- active `changeset_id` clearing and historical-record preservation;
- any failed intermediate run and the final converged state, without secrets or provider payloads.

Update `docs/next-steps.md` only after all five Shared-table changeset E2E bullets have evidence. Leave operations protection, failure drills, representative PDF stabilization, and P3 backlog unchanged.

## Acceptance criteria

The work is complete only when all of the following are true:

1. The preflight confirms both fixed products and their four shared version-1 tables at the documented baseline.
2. One real changeset coordinates the two products and only the `marketing_campaign` table.
3. Readiness progresses from blocked `1/2` to ready `2/2` using exact immutable tracking heads.
4. The definition and readiness PRs merge before product PRs without producing premature Releases or failed expected-state release runs.
5. The first product merge does not partially release the changeset.
6. The final product merge releases both products and table v2 from one commit, and rerun converges to no-op.
7. Later independent product updates clear both active changeset bindings, preserve table versions, and do not expand the historical changeset.
8. Local tests, the complete suite, Ruff, and actionlint pass.
9. The acceptance report contains reproducible evidence and no secrets.
10. The Shared-table roadmap item is checked without altering unrelated backlog.
