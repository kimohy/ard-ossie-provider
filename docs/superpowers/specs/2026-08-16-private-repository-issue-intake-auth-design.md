# Private Repository Issue Intake Authentication Design

## Status and supersession

`kimohy/ard-ossie-provider` is intentionally private. This decision supersedes earlier clauses
that require public repository visibility or describe Issue submissions and generated artifacts
as public. Historical plans remain as implementation records, but active documentation, forms,
workflows, tests, and repository metadata must use the private contract defined here.

The Issue intake UX remains unchanged: authorized collaborators upload one HTML, one DOCX/PDF,
and one XLSX directly to a GitHub Issue, and a maintainer applies `ard:approved` after reviewing
the exact body and attachment hashes.

## Problem

New `github.com/user-attachments` files associated with a private repository require an
authenticated GitHub user. The Actions-provided `github.token` is a GitHub App installation
token. It can mutate the repository with the declared workflow permissions, but it receives
HTTP 404 when it requests these private attachment URLs. GitHub does not document a REST API or
fine-grained personal-access-token permission for downloading private Issue attachments.

A user OAuth token succeeds, and GitHub community reproductions distinguish working classic
personal access tokens from failing installation and fine-grained tokens. The intake workflow
therefore needs a narrowly isolated classic personal access token without weakening redirect,
size, type, or content validation.

## Goals

- Keep the repository private and preserve approved Issue attachment intake.
- Authenticate only the initial `github.com/user-attachments` request and any validated redirect
  back to the same host.
- Prevent the attachment credential from reaching GitHub asset-storage hosts, reusable processing
  workflows, unapproved Issue jobs, logs, result envelopes, artifacts, or generated branches.
- Fail deterministically before network I/O when the required credential is absent.
- Replace active public-content language with an authorized-private-content contract.
- Preserve all existing immutable URL, redirect allowlist, byte limit, MIME, magic, extension,
  hash, approval, trusted-code, and exact-head controls.

## Non-goals

- Supporting fine-grained PATs or GitHub App installation tokens for private `user-attachments`.
- Automating creation of a GitHub user, classic PAT, SSO authorization, or token rotation.
- Accepting external storage, mutable branch URLs, raw repository URLs, arbitrary GitHub hosts,
  browser cookies, or headless-browser sessions.
- Exposing the attachment token to source processing, LLM validation, release, or dispatch jobs.
- Changing direct-branch intake, product semantics, Registry behavior, or release behavior.

## Credential ownership and storage

Use a dedicated bot account with `read` access to this repository and no access to unrelated
private repositories. Create a classic PAT for that bot with:

- the `repo` scope, which classic PATs require for private repository access;
- an explicit expiration date and an operational owner responsible for rotation;
- any required organization SSO authorization before use, if the repository later moves into an
  organization that enforces SSO.

Classic PAT scopes cannot restrict a token to one repository. Repository isolation is therefore
enforced by limiting the bot account itself to this repository. A maintainer's general-purpose
personal token must not be used.

Store the token as the `ARD_ATTACHMENT_TOKEN` secret in a new GitHub Environment named
`ard-private-intake`. The Environment has no required reviewer because `ard:approved` is already
the human authorization gate, and its deployment branch policy permits only `main`.

The existing `secrets: inherit` calls into `ard-process.yml` must remain unchanged. They are a
verified compatibility requirement for the protected `ard-llm` Environment. An Environment
secret is selected because it is available only to jobs that enter `ard-private-intake`; unlike a
Repository Secret, it is not part of the caller's inherited repository-secret set.

The PAT value is provisioned through GitHub's encrypted Secret input or a local stdin command such
as `gh secret set ARD_ATTACHMENT_TOKEN --env ard-private-intake`. It is never accepted through a
CLI argument, Issue field, committed file, workflow input, shell trace, or chat message.

## Workflow boundary

Only these mutually exclusive jobs in `.github/workflows/ard-issue-intake.yml` enter
`environment: ard-private-intake`:

1. `intake`, when no managed Draft PR exists yet;
2. `base_sync`, when an existing managed Draft PR must be revalidated and synchronized.

Within each job, only the step that invokes `ard workflow issue-intake` or
`ard workflow issue-base-sync` maps `${{ secrets.ARD_ATTACHMENT_TOKEN }}` to the process
environment. Checkout, Python setup, uv setup, authorization, routing, reusable processing, and
finalization steps do not reference the secret.

The `authorize` and `route` jobs continue to run before either credential-bearing job. Candidate
code is never executed in a credential-bearing job. The workflow continues to use `GH_TOKEN` only
for GitHub API and Git mutations; attachment download code never falls back to `GH_TOKEN`.

## Download contract

`download_attachment()` accepts an optional injected attachment token for tests and embedded use.
When it is not explicitly supplied, the runtime resolves `ARD_ATTACHMENT_TOKEN`. After validating
the canonical initial URL but before sending the first request, it rejects a missing or
whitespace-only token with `AttachmentSecurityError("ATTACHMENT_TOKEN_REQUIRED")`. The existing
application boundary maps that security error to exit code `50` and a redacted failure result.

For every request:

1. Build a fresh request for the already validated URL.
2. Remove any client-default `Authorization` header and disable client-default auth.
3. Add `Authorization: Bearer <ARD_ATTACHMENT_TOKEN>` only when the exact host is `github.com`.
4. Disable automatic redirects.
5. Validate each redirect target against the existing exact allowlist.
6. Rebuild the next request. Signed `objects.githubusercontent.com` and
   `github-production-user-asset-*.s3.amazonaws.com` requests contain no authorization header.

HTTP errors may report status and sanitized URL but never request headers or token values. Token
format prefixes are not validated or logged. All current atomic temporary-file, size, content-type,
magic-byte, extension, SHA-256, and cleanup behavior remains unchanged.

## Private content contract

Active user-facing and operational documentation changes from "public submission" to
"authorized private repository submission":

- The repository remains private and its contents are visible only to users granted repository
  access, subject to GitHub's own retention and access controls.
- Submitters must be authorized to place the source data in this repository.
- Secrets, access tokens, personal data, and content prohibited by organizational policy remain
  forbidden even though the repository is private.
- Generated branches, PRs, artifacts, tags, and Releases remain private unless a separate reviewed
  publication process explicitly exports them.

Update the README, Issue Form name/description/acknowledgement, GitHub Actions setup guide, active
architecture contract, bootstrap/foundation visibility clauses, and live label descriptions. Older
dated design and implementation records that materially assert a public runtime contract receive a
short supersession note rather than having their historical steps silently rewritten.

## Bootstrap ownership and convergence

`ard github bootstrap` remains the repeatable desired-state command for the repository's existing
Actions settings, `ard-llm` and `production-linkage` Environments, branch protection, Variables,
and labels. Its repository precondition requires the exact private `kimohy/ard-ossie-provider`
repository with `main` as the unarchived default branch and admin permission. It must reject a
public repository rather than treating public and private visibility as interchangeable.

Bootstrap converges the two intake labels to the private contract:

- `ard:submission`: `Private AI Ready Data submission`;
- `ard:approved`: `Maintainer approved private ingestion`.

The `ard-private-intake` Environment remains outside bootstrap ownership. It is a rollout-managed
security boundary because bootstrap cannot create or rotate the dedicated bot's classic PAT, and
silently managing only part of that Environment would obscure who owns its credential and branch
policy. The private-auth rollout creates the Environment, restricts it to the single `main` branch
policy, provisions `ARD_ATTACHMENT_TOKEN` through hidden input, and verifies metadata separately.
Bootstrap must neither delete nor mutate this Environment. The Actions setup and Enterprise
migration guides state this ownership boundary explicitly.

## Failure and rotation behavior

- Missing or empty Environment Secret: fail before the attachment request with
  `ATTACHMENT_TOKEN_REQUIRED`; finalization keeps the Issue open and applies `ard:failed`.
- Expired, revoked, unauthorized, or SSO-unapproved PAT: the authenticated GitHub request fails;
  no partial source file or managed-branch mutation is retained.
- Untrusted redirect or credential-bearing storage request: fail as a security violation before
  content is written.
- Rotation: update the Environment Secret, remove and reapply `ard:approved`, and reuse the same
  Issue, managed branch, and Draft PR. No code change is required.

The workflow must not print a probe request containing the token. Operational validation downloads
the approved attachment through the trusted CLI and compares its size and SHA-256 with the
independently recorded source.

## Verification

### Unit tests

- A missing or blank `ARD_ATTACHMENT_TOKEN` fails before the HTTP transport is called.
- An explicitly injected token takes precedence over the environment for deterministic tests.
- A private GitHub request receives the attachment token, not `GH_TOKEN` or client-default auth.
- Every allowed storage redirect receives no `Authorization` header or client auth.
- Existing URL, redirect, byte, MIME, magic, extension, and atomic-write tests remain green.

### Workflow contract tests

- Only `intake` and `base_sync` enter `ard-private-intake`.
- Only their CLI invocation steps reference `secrets.ARD_ATTACHMENT_TOKEN`.
- Authorization, routing, checkout/setup, processing, finalization, direct-change, release, and
  reusable workflows contain no attachment-token reference.
- Both existing `secrets: inherit` processor calls remain present.
- The credential-free candidate validation and protected `ard-llm` boundaries remain unchanged.

### Bootstrap contract tests

- The exact private, unarchived `main` repository with admin permission is accepted.
- A public repository, archived repository, wrong repository name, wrong default branch, or
  insufficient permission is rejected before mutation.
- Bootstrap desired labels use the two private descriptions and do not restore public wording.
- Bootstrap does not inspect, create, update, or delete `ard-private-intake` or its Secret.

### Documentation and live acceptance

- Search active contracts and user-facing copy for stale claims that the repository or submission
  is public.
- Run focused tests, the complete local suite, static checks, schema verification, and wheel build.
- Land the change through a reviewed PR with required checks at the exact head.
- Create `ard-private-intake`, restrict it to `main`, provision the classic PAT without exposing its
  value, and read back only secret metadata and Environment policy.
- Retry Issue #46 and require exact attachment hashes, Draft PR creation, processing success, and
  removal of `ard:failed` before resuming the shared-table acceptance plan.

## Alternatives considered

1. **Repository Secret:** simpler, but the required `secrets: inherit` processor call would make a
   new repository-level credential available to the reusable workflow secret context.
2. **Fine-grained PAT or installation token:** least privilege in principle, but the private
   `user-attachments` web endpoint returns 404 and exposes no documented fine-grained permission.
3. **Exact commit plus Git LFS paths:** avoids a user credential and remains the preferred fallback
   if classic PAT policy changes, but replaces the approved Issue attachment UX.
4. **Browser session automation:** rejected because cookies are broader, harder to rotate and audit,
   and incompatible with the existing non-browser trusted processing boundary.
