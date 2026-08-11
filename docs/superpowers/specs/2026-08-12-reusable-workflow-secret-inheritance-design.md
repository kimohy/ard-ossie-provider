# Reusable Workflow Secret Inheritance Design

## Problem

The trusted Issue and direct-branch coordinators call `ard-process.yml` as a reusable
workflow without forwarding secrets. The protected `process` job enters the `ard-llm`
environment and receives environment variables, but `secrets.ARD_LLM_API_KEY` evaluates
to an empty string in the observed runner behavior. Processing consequently stops with
`LLM_PROVIDER_CONFIG_INCOMPLETE` before publishing generated artifacts.

## Accepted design

Add `secrets: inherit` only to the `process` reusable-workflow call in these trusted callers:

- `.github/workflows/ard-issue-intake.yml`
- `.github/workflows/ard-direct-change.yml`

Do not change the fork or pull-request validation workflows. Keep `environment: ard-llm`
and every explicit `${{ secrets.* }}` reference inside the reusable processor's protected
`process` job. The validation and finalize jobs remain free of secret references.

## Alternatives considered

1. Explicit caller secret mapping would narrow the forwarded names, but the observed
   reusable-workflow regression is specifically avoided by the verified inheritance path.
2. Inlining the processor into each caller would avoid reusable-workflow semantics but
   duplicate security-sensitive workflow code and drift over time.
3. Repeated environment approval or secret re-entry does not change the evaluation boundary
   and has already failed twice.

## Security boundary

The callers are trusted, repository-owned workflows triggered only after their existing
authorization and source-validation gates. Inherited secrets are not referenced by caller
jobs or by the reusable workflow's validation/finalize jobs. GitHub environment approval
continues to gate the only job that reads `ARD_LLM_API_KEY`.

## Verification

A YAML contract test parses the workflows and verifies both trusted calls inherit secrets,
both calls still target the expected reusable workflow, the processor remains protected by
`ard-llm`, and no reusable-workflow job other than `process` references secret context.
The focused test must fail before the YAML change and pass afterward. The PR must also pass
the repository's static, pytest, and wheel gates. After merge, Issue #3 is retried and the
processor must publish generated results to PR #5.
