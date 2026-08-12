# Draft PR Base Synchronization Design

**Status:** Approved

**Date:** 2026-08-12

**Scope:** Trusted, issue-triggered synchronization and reprocessing of one existing managed Draft
ARD product PR

## 1. Context

PR #14 merged metric dataset safety into `main` at `a922a67717e919d8c3051401081433bd4241abc0`.
Reapplying `ard:approved` to Issue #3 then started workflow run `31577162874`, whose `authorize`
job succeeded but whose `intake` job failed with `ISSUE_EXISTING_PATH_NOT_ALLOWED`. PR #5 was still
a managed Draft PR at head `2ada713c4ddabccf754ba0a7ee65cc9065a915a6`.

The failure is correct under the current intake contract. `IssueIntakeService` compares the
existing branch with `main` and accepts only the original product configuration, intake manifest,
and source attachments. A previously processed branch also contains `generated/**`, `quality/**`,
and Registry writeback, so widening the intake allowlist would mix two trust domains and permit
untrusted historical output to be treated as approved Issue input.

The selected behavior is option 1: verify the exact existing Draft PR head, merge the latest
trusted `main` into that branch, remove prior processor-derived output by restoring it to the
trusted base state, push the new head without force, and invoke the existing reusable processor
against that exact new head.

## 2. Goals

- Reprocess one existing managed Draft PR when its source Issue receives a new `ard:approved`
  label event.
- Keep the existing Issue intake path policy unchanged.
- Run synchronization logic only from the default branch and never execute candidate code.
- Revalidate the approved Issue body, attachment bytes, product configuration, and intake manifest
  before preserving source input.
- Permit only processor-derived output for the same product to be discarded.
- Merge one exact trusted base SHA and fail closed if either `main` or the PR branch moves.
- Push only a fast-forward update to the existing PR branch.
- Pass the resulting exact head to `.github/workflows/ard-process.yml`, which regenerates required
  statuses and output using the current trusted processor.
- Prove the behavior on PR #5 after the code change is reviewed, merged, and its CI succeeds.

## 3. Non-goals

- Do not broaden `RepositoryPaths.is_intake_write_allowed` or the existing
  `IssueIntakeService` changed-path acceptance rule.
- Do not preserve prior generated, quality, or Registry output across a base synchronization.
- Do not infer that arbitrary `registry/**` changes belong to the selected product.
- Do not resolve merge conflicts automatically or force-push a branch.
- Do not reprocess every open Draft PR after every `main` update.
- Do not synchronize direct-change PRs or code-only PRs through this Issue-only route.
- Do not mark PR #5 Ready or merge it as part of the synchronization feature PR.

## 4. Security invariants

1. The label actor still passes `IssueAuthorizationService`; only `ard:approved` applied by a
   collaborator with `write`, `maintain`, or `admin` permission can enter routing.
2. A read-only route job checks out the Issue event's exact default-branch SHA (`github.sha`) and
   derives the Issue, product key, managed branch, base SHA, PR number, and PR head from trusted
   event and GitHub state. The remote default branch must still equal that SHA, so the later local
   reusable workflow and executed trusted code cannot come from different revisions.
3. Existing intake and base synchronization are mutually exclusive jobs. The original intake job
   runs only when no managed PR exists; the synchronization job runs only when one exists.
4. The synchronization job has separate `trusted/` and `candidate/` checkouts. Every `uv run ard`
   command executes with `working-directory: trusted`, `PYTHONSAFEPATH=1`, and an explicit
   `--repository "$CANDIDATE_REPOSITORY"`. Candidate Python, actions, hooks, and workflows are never
   executed.
5. The candidate checkout is pinned to the route job's exact PR head. The service independently
   rereads the PR and remote branch before mutation.
6. The trusted base checkout is pinned to the route job's exact default-branch SHA. Before merge,
   the candidate repository independently verifies that the remote default branch still points to
   that SHA. A moved base causes a typed failure and requires a fresh label event.
7. Approved intake paths are preserved only after byte-for-byte canonical revalidation against the
   Issue. Every other branch-authored path must be a narrowly classified resettable output for the
   same product.
8. Synchronization uses an ordinary fast-forward push from the exact old head. A concurrent branch
   update becomes `NON_FAST_FORWARD`; no force option is available.
9. The reusable processor receives the synchronization result's exact final SHA and repeats its
   source, Draft PR, branch, and head checks before obtaining the `ard-llm` secret.

## 5. Workflow architecture

### 5.1 Read-only route

Add `ard workflow issue-route`. `IssueRouteService` loads the trusted Issue event, verifies context,
parses the canonical Issue intake fields, derives the managed branch name, and reads the default
branch SHA from the trusted checkout.

It compares that SHA with the current remote default-branch head and queries for one open PR whose
head branch is the derived managed branch. Its output is:

```text
mode: intake | base_sync
base_sha: <40 lowercase hex>
branch: <managed branch>
product_key: <canonical product key>
pr_number: <positive integer, base_sync only>
expected_head: <40 lowercase hex, base_sync only>
```

For `base_sync`, the PR must be open, Draft, unmerged, target the repository default branch, use the
derived head branch, and have a valid exact head SHA. `merged_at` is the merged-state authority;
GitHub's non-null synthetic `merge_commit_sha` for an open PR is not treated as merge evidence.
Route performs no repository or GitHub write.

### 5.2 Existing intake

The existing `intake` job depends on `route` and runs only for `mode == 'intake'`. Its service,
path policy, attachment preparation, commit, push, and PR creation behavior remain unchanged.

### 5.3 Base synchronization

Add `ard workflow issue-base-sync`. `IssueBaseSyncService` receives the trusted Issue event and
`base_sha`, but recomputes the product key, managed branch, PR number, and expected head rather than
trusting workflow string inputs.

The base-sync job checks out:

- `trusted/` at the route job's `base_sha`, without persisted credentials;
- `candidate/` at the route job's `expected_head`, with full history, LFS, and the scoped contents
  credential needed for the eventual fast-forward push.

The service verifies a clean candidate worktree, current candidate SHA, remote PR branch SHA,
remote default branch SHA, and live managed Draft PR state before examining content. Because
canonical attachment revalidation can take time, it rereads the live managed PR and both remote
heads immediately before the local merge. It rereads all three again before the remote push.

### 5.4 Processor selection

The existing `process` reusable-workflow job depends on `route`, `intake`, and `base_sync`. It runs
when route succeeded and exactly one preparation job succeeded. Its `branch`, `product_key`,
`pr_number`, and `expected_head` inputs select the non-empty outputs of `intake` or `base_sync`.
No processor input is read from Issue prose or a candidate workflow.

The finalizer depends on both preparation alternatives and the processor, uses `always()`, and
continues to reconcile Issue labels from the processor result. A route or synchronization failure
therefore produces `ard:failed` rather than silently retaining `ard:processing`.

## 6. Input revalidation

Before any Git commit, base synchronization reruns the same canonical validation used for an
idempotent existing intake:

- Issue number, repository, actor context, operation, product key, product ID, version, and optional
  changeset ID;
- `product.yaml` values;
- `intake-manifest.json` identity and exact attachment role set;
- every source relative path, source URL, original filename, size, SHA-256 digest, and byte content;
- the absence of extra files or symlinks in the product source tree.

The canonical comparison redownloads the approved immutable GitHub attachments into a temporary
directory below `RUNNER_TEMP`. An update operation receives the current product configuration in
the staging tree exactly as the current intake validator requires.

## 7. Changed-path classification

Changed paths are computed from the merge base of `base_sha...expected_head`. They are divided into
preserved intake paths and resettable derived paths.

Preserved paths are only those already accepted by
`RepositoryPaths.is_intake_write_allowed(path, product_key)`:

```text
products/<product_key>/product.yaml
products/<product_key>/intake-manifest.json
products/<product_key>/sources/**
```

Every non-intake path first must be an existing processor writeback path, then must pass the new
narrow reset policy. The policy accepts only:

```text
products/<product_key>/generated/**
products/<product_key>/quality/**
registry/indexes/product-keys.json
registry/indexes/table-locators.json
registry/products/<product_id>.json
registry/mappings/<product_id>.json
registry/tables/<table_id>.json
```

`product_id` must equal the validated intake manifest and a strictly parsed
`registry/products/<product_id>.json` record whose product key also matches. `table_id` must occur
in the strictly parsed `registry/mappings/<product_id>.json`; every mapping must belong to that
product, and its exact `registry/tables/<table_id>.json` record must parse with the same ID and
version. Duplicate mapping link IDs are rejected.

No other product tree, repository code, workflow, documentation, changeset, Registry product,
Registry mapping, or unlisted Registry table is resettable. A malformed, missing, or inconsistent
Registry ownership chain fails closed. The public `generated/ossie-model.json` is processor output,
not ownership metadata, and is never parsed as an internal `CandidateChange`. Candidate Registry
records are used only to restrict which files may be discarded; none of their semantic content is
published or trusted after synchronization.

## 8. Git transaction

After validation, trusted code performs this sequence in `candidate/`:

1. Recheck candidate `HEAD == expected_head`, remote PR branch `== expected_head`, remote default
   branch `== base_sha`, and a clean worktree.
2. Configure only the local Git bot identity.
3. Merge exact `base_sha` with `--no-ff`, `--no-edit`, and an explicit synchronization message.
4. If the merge conflicts, abort it and raise `BASE_SYNC_MERGE_CONFLICT`; do not push.
5. Restore the validated product config, intake manifest, and every manifest-listed source from
   exact `expected_head`; commit only intake-allowed paths, then rerun canonical Issue validation
   against the post-merge tree. This prevents a clean base merge from replacing approved input.
6. Restore every classified derived path in the worktree from exact `base_sha`. Files absent from
   the trusted base are deleted; base-owned files are restored byte-for-byte.
7. Let the existing explicit-path commit boundary validate the resulting status, stage only those
   paths, and commit them with
   `data(<product_key>): reset generated outputs after base sync`.
8. Require `base_sha` to be an ancestor of the final local SHA and the worktree to be clean.
9. Recheck the live managed PR, remote PR branch, and remote default branch.
10. Push `HEAD:refs/heads/<managed branch>` without force, then require the remote branch SHA to
   equal the final local SHA.

If `base_sha` is already an ancestor, Git may make no merge commit; prior derived output is still
reset and committed before reprocessing. Any failure before the push leaves the remote unchanged.
A failed or concurrent push is safe to retry because a new run checks out the live remote head.

## 9. Stable failure codes

The route and base-sync boundary uses stable codes and redacted messages:

- `ISSUE_ROUTE_BASE_MOVED`
- `ISSUE_BASE_SYNC_PR_REQUIRED`
- `ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH`
- `ISSUE_BASE_SYNC_WORKTREE_DIRTY`
- `ISSUE_BASE_SYNC_HEAD_MISMATCH`
- `ISSUE_BASE_SYNC_BASE_MOVED`
- `ISSUE_BASE_SYNC_PATH_NOT_ALLOWED`
- `ISSUE_BASE_SYNC_OUTPUT_REGISTRY_INVALID`
- `ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH`
- `ISSUE_BASE_SYNC_ANCESTRY_MISMATCH`
- `BASE_SYNC_MERGE_CONFLICT`
- `BASE_SYNC_ABORT_FAILED`
- `BASE_SYNC_RESTORE_FAILED`
- existing `NON_FAST_FORWARD` for a concurrent branch push

Error envelopes expose codes, exact-head outputs needed for diagnosis, and retryability, but do not
copy attachment content, generated model content, GitHub tokens, or provider SQL into logs.

## 10. Test strategy

- Route unit tests prove new intake selection, existing Draft selection, wrong base/head/branch,
  non-Draft/merged PR rejection, and read-only behavior.
- Base-sync unit tests prove canonical Issue revalidation precedes mutation and reject code,
  workflow, another product, unrelated Registry, missing model, forged product identity, and
  unlisted table paths.
- Path policy tests cover every allowed family plus traversal, symlink, wrong product/product ID,
  wrong index, and unknown table rejection.
- Git adapter tests verify exact merge arguments, conflict abort, source-based worktree restore,
  explicit reset path staging, clean/no-op handling, exact remote-head checks, and
  non-fast-forward classification.
- CLI tests prove route and base-sync outputs are written to both the result envelope and
  `GITHUB_OUTPUT`.
- Workflow contract tests prove the read-only route, mutually exclusive preparation jobs,
  separate trusted/candidate base-sync checkouts, trusted working directory, exact refs,
  least-privilege permissions, dynamic processor input selection, finalizer coverage, pinned
  actions, and no candidate code execution.
- A Git-backed integration test creates a base branch and processed product branch, advances base,
  performs synchronization, and proves two-parent ancestry, source preservation, derived-output
  reset, clean final state, and fast-forward publication behavior.
- Full pytest, Ruff, model-schema verification, workflow YAML parsing, schema catalog, Ossie
  checksum, secret scan, and isolated sdist/wheel builds run before publication.

## 11. Acceptance criteria

- Reapplying `ard:approved` to an unprocessed Issue still follows the unchanged intake path.
- Reapplying it to an existing managed Draft PR selects only the dedicated base-sync path.
- No candidate code or workflow runs with a write token or the `ARD_LLM_API_KEY`.
- PR source/config/manifest bytes remain identical after synchronization.
- Every prior generated, quality, and same-product Registry change is absent at the synchronized
  head relative to the trusted base.
- The synchronized head contains the exact trusted `main` SHA in its ancestry and is pushed without
  force from the previously verified PR head.
- The existing processor runs against that exact synchronized head and recreates required statuses.
- Reprocessing PR #5 qualifies `Campaign Count`, excludes `Modeled Efficiency` with
  `METRIC_MULTI_DATASET_UNSUPPORTED`, emits non-`PASS` quality, and keeps Registry output
  consistent with generated output.
- The feature is delivered in a separate code PR; global Draft-PR fan-out remains out of scope.
