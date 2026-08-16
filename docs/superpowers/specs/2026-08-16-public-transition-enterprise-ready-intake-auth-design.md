# Public Transition with Enterprise-Ready Intake Authentication Design

## Status and supersession

`kimohy/ard-ossie-provider` is temporarily public on GitHub.com so the current GitHub Free account
can use branch protection, Environment secrets, deployment branch policies, and the existing
Environment-based workflows. A future migration will move the repository to a private GitHub
Enterprise target.

This design supersedes the current-runtime visibility and private-content clauses in
`2026-08-16-private-repository-issue-intake-auth-design.md`. It retains that design's attachment
credential isolation, redirect protection, error redaction, and workflow scoping as an
Enterprise-readiness boundary. The public repository contract remains authoritative until the
Enterprise migration is completed and verified.

## Current public content contract

Repository contents, Issues, attachment links, branches, pull requests, Actions metadata,
artifacts, tags, and Releases can be visible to anyone. Submitters may upload only synthetic,
non-confidential content that they are authorized to publish publicly. Secrets, tokens, personal
data, customer data, internal documents, and content prohibited by organizational policy remain
forbidden.

The Issue Form, README, labels, and active operating documentation must describe public submission
and publication accurately. They must not imply that GitHub access controls protect submitted
content during this transition.

## Retained attachment authentication boundary

The `intake` and `base_sync` jobs continue to enter `ard-private-intake`, and only their trusted CLI
steps receive `ARD_ATTACHMENT_TOKEN`. The Python downloader continues to require that token for the
initial exact `github.com/user-attachments` request, removes client-default authorization, and sends
no credential to validated GitHub asset-storage redirects.

Public GitHub.com attachments do not require this credential today. The token path remains enabled
to exercise the intended credential boundary before Enterprise migration and to avoid a second
workflow redesign at migration time. The token must still belong to a dedicated bot, have an
expiration, use the classic `repo` scope required by the planned private GitHub.com target, and be
stored only as the `ARD_ATTACHMENT_TOKEN` Environment Secret. A maintainer's general-purpose token
is not acceptable.

This is not proof that the same endpoint contract works on every Enterprise product. GitHub
Enterprise Cloud on `github.com` and GHES can differ in attachment host, path, token, API, and
Environment behavior. Migration requires product-specific fixtures and a separate security review;
the exact `github.com` allowlist must not be broadened speculatively.

## Public bootstrap convergence

`ard github bootstrap` requires the exact public, unarchived `main` repository and admin permission.
It converges:

- the public `ard:submission` and `ard:approved` label descriptions;
- Actions default permissions;
- `ard-llm` and `production-linkage` Environments and Variables;
- the current provider Secret contract; and
- `main` branch protection with the existing pull-request, current-base, conversation-resolution,
  force-push/deletion, and `ard/quality-gate` plus `ard/changeset` requirements.

The separate `ard github enable-review-protection` transition remains available after a non-owner
writer is present. The temporary GitHub Free private exclusion for `branch:main` is not part of this
public runtime.

Bootstrap does not own `ard-private-intake`, its `main` deployment branch policy, or
`ARD_ATTACHMENT_TOKEN`. Those remain rollout-managed because the dedicated bot token is provisioned
and rotated independently. Bootstrap must not inspect or mutate that Environment.

## GitHub CLI compatibility

The installed GitHub CLI is `gh 2.45.0`, whose `gh api` command supports `--paginate` but not
`--slurp`. The adapter must not emit the unsupported flag. It decodes one JSON document directly and
combines multiple sequential JSON documents into pages so pagination remains correct without
raising `INVALID_GITHUB_JSON`.

## Rollout and acceptance

Before merge:

1. provision `ARD_ATTACHMENT_TOKEN` through hidden input and read back only Secret metadata;
2. run bootstrap dry-run and apply to restore public labels, Environment state, and `main`
   protection;
3. read back public visibility, exact `main` protection, Environment policies, and Secret names;
4. land the code through a reviewed exact-head pull request with all checks successful.

After merge, remove and reapply `ard:approved` on Issue #46 and require successful authorization,
routing, attachment intake, processing, finalization, exact source hashes, and the managed Draft PR.
Only synthetic public-safe fixtures may be used.

## Enterprise migration boundary

The migration plan must revalidate, rather than assume:

- private repository and Environment feature availability on the selected Enterprise product;
- attachment initial and redirect hosts, paths, and credential type;
- Environment Secret, reviewer, deployment-branch, and reusable-workflow behavior;
- branch protection or ruleset semantics and required status names;
- Git LFS, Actions, runner, API, and release behavior.

Only after those checks pass may active documentation return to a private-content contract. The
temporary public repository must not receive confidential migration samples.

## Verification

- Unit tests prove public bootstrap acceptance, private visibility rejection, public label desired
  state, `branch:main` planning, branch-protection convergence, and `ard-private-intake`
  non-ownership.
- Adapter tests prove `--paginate` without `--slurp` and correct decoding of sequential pages.
- Workflow tests retain exactly two `ARD_ATTACHMENT_TOKEN` references and unchanged processor
  secret inheritance.
- A live bootstrap dry-run succeeds on the public repository and includes `branch:main` without
  mutating GitHub.
- Full pytest, Ruff, actionlint, schema, wheel, checksum, secret scan, diff check, code review, and
  same-head CI pass before merge.
