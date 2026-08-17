# Marker-only Changeset Tracking Intake Design

## Goal

Allow an approved Issue to populate an existing canonical changeset tracking PR whose exact head
contains only the coordinator-created product marker. The workflow must reuse that PR, synchronize
it with the exact trusted default-branch head, preserve the marker, ingest the approved attachments,
and continue through normal protected processing without weakening the existing Draft PR replay
boundary.

## Production failure

The changeset coordinator creates the canonical tracking branch and Draft PR before Issue intake.
After the central definition PR is merged, the tracking head is both behind `main` and populated only
with `products/<product-key>/changesets/<changeset-id>.json`.

`IssueRouteService` currently selects `base_sync` for every existing managed Draft PR. The existing
base-sync path assumes the branch already contains intake metadata for the triggering Issue and calls
`prepare_existing_intake`. A pristine tracking PR instead contains the previous product config and
manifest, so both production runs stopped with `ISSUE_EXISTING_CONFIG_MISMATCH`. The older
`IssueIntakeService` already recognizes marker-only PRs, but that code is unreachable after routing
selects `base_sync`, and it does not synchronize the stale tracking head with the newly merged
changeset definition.

## Selected approach

Extend `IssueBaseSyncService` with an explicit marker-only transition. This is the narrowest safe
boundary because the job already has:

- the exact route-time default-branch SHA in a credential-free trusted checkout;
- the exact managed Draft head in a full-history, LFS-enabled candidate checkout;
- the scoped credential required for a non-force branch push; and
- repeated live PR, remote head, and default-branch checks before mutation and publication.

Changing route to inspect PR files would add a GitHub API contract and still require a second path to
merge the exact base. Replacing or manually editing the tracking PR is excluded because it violates
the canonical-PR and provenance contract.

## State classification

After the existing clean-worktree, exact-head, remote-head, default-branch, and managed-PR checks,
base sync computes the changed paths between the exact trusted base and tracking head.

The new transition applies only when all of the following are true:

1. the Issue contains a non-null changeset ID;
2. the changed-path set is exactly the canonical marker path for that product and changeset; and
3. the marker content is structurally valid for the same changeset and product.

Any extra path, wrong product path, wrong changeset path, malformed marker, or populated intake
continues through the existing validation path or fails under its current typed security error.

## Marker-only data flow

1. Recheck the same managed Draft PR, its exact remote head, and the exact remote default-branch SHA.
2. Merge the exact trusted base SHA into the candidate with a normal merge commit.
3. Prepare the Issue attachments directly into the candidate workspace using the existing canonical
   attachment parser and downloader.
4. Require the resulting manifest to match the trusted Issue number, product key, requested version,
   and existing product ID.
5. Commit only paths allowed by `is_intake_write_allowed`; the coordinator marker is preserved by
   the merge and is not included in the intake commit.
6. Re-run canonical existing-intake validation against the just-written config, manifest, source
   URLs, filenames, sizes, hashes, and bytes.
7. Recheck the managed PR and both remote heads immediately before publication.
8. Push Git LFS objects and then perform a normal fast-forward branch push.
9. Verify the published remote head and return the existing base-sync output contract so protected
   processing continues unchanged.

The ordinary populated-Draft base-sync path remains unchanged: it validates the existing approved
intake first, merges the trusted base, preserves intake paths, resets only same-product derived
output, and pushes without an LFS object upload.

## Security and failure behavior

- No force push, branch replacement, PR replacement, direct generated-output edit, or Environment
  approval bypass is introduced.
- Attachment credentials remain confined to the existing `ard-private-intake` job and canonical
  downloader boundary.
- The candidate cannot choose its base SHA, PR identity, branch, product key, changeset ID, or
  publication head.
- Marker-only classification is exact; it is not a general allowance for product paths.
- Attachment or manifest validation fails before publication with the existing redacted typed
  errors.
- A moved base, PR, or candidate head fails before push. A concurrent push remains a non-fast-forward
  failure.
- Failed production runs and their untouched marker-only heads remain preserved as E2E evidence.

## Tests

Add a failing service test that reproduces the production state: a managed Draft head differs from
the exact base only by its canonical marker, while its old product config does not match the new
Issue. The expected result is a base merge, canonical attachment preparation, intake-only commit,
canonical revalidation, LFS-enabled push, and reuse of the same PR number.

Add negative coverage for malformed marker content and for a marker plus any extra changed path.
Retain the existing populated-Draft tests to prove ordinary base synchronization and non-LFS push
behavior are unchanged. Run focused base-sync, intake, CLI, workflow-contract, filesystem, and Git
adapter tests before the full repository suite and static verifier.

## Rollout and E2E resumption

Ship this change in a code/documentation-only PR with both required statuses successful. After the
owner approves and merges that exact head, remove and reapply only `ard:approved` on Issues #57 and
#58. Require both new runs to reuse PRs #55 and #56, advance their marker-only heads, pass protected
processing, and satisfy the remaining shared-table E2E checks. Do not merge either tracking PR until
the later explicit approval checkpoints in the active E2E plan.
