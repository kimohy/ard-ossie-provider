# Source-check semantic preflight design

## Problem

Workflow run `31771169206` failed in `process / validate` before the protected
`ard-llm` job ran. The trusted `source-check` command intentionally has no LLM
credentials, but it calls `ModelingService.validate()`, which invokes the full
publication pipeline with `provider=None`.

For PDF semantic sources, parsing without a correction provider records
`SEMANTIC_OCR_CORRECTION_UNAVAILABLE` and no per-page correction audits. The
publication gate then converts that expected preflight state into
`SEMANTIC_VISUAL_CORRECTION_FAILED`. As a result, every PDF that requires the
protected LLM stage is rejected before that stage can start.

## Selected behavior

`source-check` defers only the provider-dependent visual-correction completion
gate. It continues to reject all deterministic semantic integrity failures,
including source text loss, duplicated spans, unmatched spans, and degraded
structure.

The protected processing job and every normal `process_product()` caller keep
the current fail-closed publication behavior. Missing, rejected, or incomplete
PDF visual correction remains a hard error before generated files are
promoted.

## Design

Add an explicit `require_semantic_visual_correction` policy parameter at the
pipeline validation boundary. Its default is `True`, so existing production and
test callers remain fail-closed.

`ModelingService.validate()` accepts the same policy and preserves the default.
`SourceCheckService` is the only caller that passes `False`, because it runs in
the credential-free preflight job. The policy suppresses only the
`SEMANTIC_VISUAL_CORRECTION_FAILED` finding. It does not suppress
`SEMANTIC_FIDELITY_FAILED`, `SEMANTIC_STRUCTURE_DEGRADED`, source validation, or
model validation findings.

The policy is explicit rather than inferred from `provider is None`. This avoids
accidentally weakening offline publication calls or tests that intentionally
verify fail-closed behavior without a provider.

## Data flow

1. `workflow source-check` scans and hashes the candidate sources without LLM
   credentials.
2. It runs deterministic model validation with visual correction marked as
   pending.
3. Any deterministic source, fidelity, or structure error stops the workflow.
4. A valid candidate enters the protected `ard-llm` processing job.
5. The normal publication pipeline requires complete page-level visual
   correction and rejects incomplete or degraded output before promotion.

## Error handling

No existing error code changes. The preflight job no longer emits
`SEMANTIC_VISUAL_CORRECTION_FAILED` solely because its provider is intentionally
unavailable. The protected processing job continues to emit that detailed code
for missing pages, rejected patches, warning codes, or incomplete correction
audits.

## Focused verification

- A regression proves the source-check policy accepts a fidelity report whose
  only pending condition is visual correction.
- A regression proves the same policy still rejects degraded semantic
  structure.
- The existing publication regression continues to prove that default pipeline
  processing rejects unavailable visual correction.
- Run only the directly affected source-check and semantic pipeline test files,
  plus Ruff on changed files. Do not run the broad suite.

## Alternatives rejected

- Deferring every semantic hard gate would allow deterministic corruption to
  consume LLM resources and move farther through the workflow.
- Treating `provider=None` as an implicit relaxation would weaken unrelated
  offline callers and make the trust boundary difficult to audit.
- Fabricating a successful correction audit during preflight would misrepresent
  work that has not occurred.
