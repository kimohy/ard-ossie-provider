# Trusted Processing Boundary Design

## Goal

Close the three merge blockers found in PR #1 without removing automatic direct-branch intake, exact-head writeback, or Issue-based processing.

## Security invariants

1. A commit outside `main` may signal that work exists, but it must not select or execute code in a job that receives `ARD_LLM_API_KEY` or a write-capable token.
2. Every privileged processing command must load the workflow and Python package from the default branch. The candidate checkout is data and Git state only.
3. Candidate validation must complete in a separate job with `contents: read`, no Environment, no write token, and no LLM secret before the protected processing job starts.
4. The `ard-llm` Environment must accept deployments from `main` only. `production-linkage` remains `main` only.
5. Direct changes remain limited to one canonical product source tree and an optional matching changeset marker/configuration. Exact branch, PR, and head checks remain authoritative immediately before writeback.
6. Initial Issue attachment URLs must be immutable GitHub user attachments. Redirects may reach only GitHub's documented asset-storage hosts, and every redirect is revalidated.
7. Merge readiness must be evaluated from the live PR head and the successful workflow run for that same head. Documentation must not embed a head SHA that becomes stale when the documentation changes.

## Workflow architecture

### Read-only branch signal

`.github/workflows/ard-direct-signal.yml` listens to source-tree pushes outside `main`. It has only `contents: read`, no Environment, and no command that executes repository Python. Its stable workflow name is the event boundary consumed by the coordinator. A candidate may disable or alter this signal only by making processing fail closed; it cannot gain the protected secret through this workflow.

### Default-branch coordinator

`.github/workflows/ard-direct-change.yml` listens to `workflow_run` completion for the read-only signal. GitHub loads a `workflow_run` workflow from the default branch, so its definition is trusted. The coordinator rejects non-push runs, failed signal runs, fork repositories, `main`, and missing head metadata.

The coordinator checks out:

- `main` at `trusted/`, without persisted credentials;
- the exact signaled head at `candidate/`, with full history and LFS, without persisted credentials during validation.

All CLI commands run with `working-directory: trusted` and pass `--repository "$CANDIDATE_REPOSITORY"`. The trusted CLI detects the changed product, validates sources, creates or reuses one Draft PR, and then invokes the local reusable processor from the trusted default-branch workflow.

### Reusable processor

`.github/workflows/ard-process.yml` adds a credential-free `validate` job before `process`. Both jobs check out trusted code and candidate data separately. `validate` reruns source validation with no protected Environment. `process` depends on `validate`, uses `environment: ard-llm`, and executes only the trusted CLI against the exact candidate repository.

The candidate checkout in `process` may persist the scoped GitHub credential because trusted code needs exact-head commit and LFS writeback. No candidate script, action, hook, or Python module is executed. `PYTHONSAFEPATH=1` is set for CLI steps as an additional import-path guard. Reconciliation and artifact upload use the candidate repository's `.ard/run` and quality paths explicitly.

Issue intake continues to call the same local reusable processor from a default-branch `issues` event. It therefore receives the same validate-before-secret and trusted-code guarantees.

## Attachment contract

`validate_attachment_url()` accepts only URLs with all of these properties:

- scheme `https`, no credentials, fragment, query, or non-443 port;
- host exactly `github.com`;
- path exactly `/user-attachments/assets/<UUID>` with a canonical UUID, or
  `/user-attachments/files/<positive decimal ID>/<safe filename>` with one canonical path segment.

Redirect validation is a separate internal function. It accepts another canonical user-attachment URL in either form or a signed asset URL on `objects.githubusercontent.com` or `github-production-user-asset-<suffix>.s3.amazonaws.com`. Queries are allowed only on storage-host redirects. Broad `github.com/*`, `raw.githubusercontent.com`, avatars, and arbitrary `*.githubusercontent.com` URLs are rejected.

## Documentation contract

`docs/next-steps.md` describes a live merge gate:

1. read PR #1's current head;
2. select the latest bootstrap run whose `head_sha` equals that value;
3. require `static`, `pytest`, `wheel`, and aggregate to be successful;
4. restart review if the head changes before merge.

The PR description reports the current verification run after each push but does not call a historical SHA a permanent merge criterion.

## Error handling and fail-closed behavior

- A missing, cancelled, failed, forked, or malformed signal does not create a PR and does not start processing.
- A head change between signal, PR creation, validation, provider execution, or writeback fails the existing exact-head checks.
- Validation failure prevents the `ard-llm` job from starting.
- An untrusted initial attachment or redirect raises `AttachmentSecurityError` before response content is written.
- Partial writeback continues to use the existing journal and trusted reconciliation path.

## Verification

Automated contracts must prove:

- the signal has read-only permissions and no local code execution;
- the coordinator is `workflow_run`-based, same-repository-only, and runs trusted CLI against a separate candidate checkout;
- the processor has a credential-free validation job and never runs `uv` from the candidate directory;
- `ard-llm` is `main`-only;
- mutable GitHub content URLs are rejected while canonical attachments and signed redirects are accepted;
- roadmap merge checks are live rather than fixed-SHA based.

Focused RED/GREEN tests are followed by the complete pytest suite, Ruff, official actionlint, the integrated static verifier, and sdist/wheel builds. The pushed PR head must then receive a successful bootstrap matrix and aggregate before re-review.
