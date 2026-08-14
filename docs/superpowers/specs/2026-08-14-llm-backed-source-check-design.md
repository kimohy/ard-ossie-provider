# LLM-backed source-check design

## Status and supersession

This design supersedes
`2026-08-14-source-check-semantic-preflight-design.md` for semantic PDF
validation. Workflow run `31784561730` proved that deferring only visual OCR
correction is insufficient: credential-free validation still produces
`SEMANTIC_STRUCTURE_DEGRADED` because unresolved embedded-PDF spans require the
configured LLM structure-repair provider.

## Objective

Run the complete OCR correction and semantic structure-repair pipeline during
trusted source validation. A candidate may advance only when the same hard
semantic gates used for publication pass with the repository-configured LLM.

The candidate branch remains untrusted data. It must never execute code or
receive repository credentials or LLM secrets.

## Selected architecture

Both Issue-driven and direct-change `source-check` jobs use the protected
`ard-llm` GitHub environment. The jobs continue to execute the exact trusted
default-branch checkout. The candidate checkout is read as source data only,
with persisted Git credentials disabled.

The workflow supplies the existing multi-provider environment contract:

- `ARD_LLM_PROFILE`
- `ARD_LLM_API_KEY` and `ARD_LLM_BASE_URL` for OpenAI-compatible profiles
- the existing Azure OpenAI and Vertex variables or secrets used by the
  selected repository profile

The CLI resolves the provider through the existing
`provider_from_environment()` factory. It injects that provider into
`SourceCheckService`, which passes it through `ModelingService.validate()` to
`process_product()`. Application services do not read secrets directly.

Source validation uses the normal strict publication defaults. It does not set
`require_semantic_visual_correction=False`; therefore OCR audit completeness,
semantic fidelity, and degraded-structure checks all remain hard gates.

## Data and trust flow

1. GitHub checks out trusted `main` workflow code without persisted
   credentials.
2. GitHub checks out the exact candidate SHA with LFS materialized and without
   persisted credentials.
3. The protected job exposes LLM configuration to the trusted command only.
4. Trusted source scanning verifies candidate paths and hashes before parsing.
5. The trusted parser performs OCR, page-level LLM correction, semantic
   structure repair, and deterministic fidelity validation in staged storage.
6. A failed provider call, rejected correction, unresolved structure, text
   loss, duplication, or unmatched span stops validation.
7. Successful validation allows the existing processing job to regenerate and
   publish the candidate artifacts. That job remains the only writer.

The validation and processing jobs intentionally perform separate LLM calls.
This costs an additional validation call but preserves the existing
least-privilege split: preflight has no content write permission, while the
processing job cannot rely on unauthenticated candidate-produced artifacts.

## Error behavior

Missing profile configuration or credentials fails before source validation
with the existing provider configuration error. Provider execution failures
retain their detailed provider code and context. Semantic failures continue to
use the existing specific findings, including
`SEMANTIC_VISUAL_CORRECTION_FAILED`, `SEMANTIC_STRUCTURE_DEGRADED`, and
`SEMANTIC_FIDELITY_FAILED`.

The workflow step name changes from “no LLM credentials” to describe protected
LLM validation accurately. Logs must not print secret values.

## Compatibility

The provider argument added to `SourceCheckService` and
`ModelingService.validate()` is explicit and injectable. Unit tests and local
callers may provide a fake provider. Normal publication callers retain strict
defaults. The earlier visual-correction policy parameter can remain available
for explicit non-publication callers, but `source-check` no longer relaxes it.

## Focused verification

Use the smallest tests that cover the changed boundaries:

1. A source-check service regression proves that its injected provider reaches
   model validation and that strict semantic correction remains enabled.
2. A workflow contract regression proves that both source-check jobs use the
   `ard-llm` environment and pass the supported provider settings only to the
   trusted execution job.
3. The existing Issue `#3` workflow is rerun as the end-to-end check. Its
   validation stage must no longer fail with `SEMANTIC_STRUCTURE_DEGRADED`.

Run Ruff only for changed Python files and `git diff --check`. Do not run the
full test suite.

## Rejected alternatives

- Deferring `provider_unavailable` degradation would let preflight pass without
  proving that the configured provider can repair the actual source.
- Allowing every degraded structure would weaken deterministic fidelity gates.
- Supplying secrets to candidate code would violate the repository trust
  boundary.
- Combining validation and writeback into one job would enlarge the
  credentialed mutation surface and make failures harder to isolate.

## Success criteria

- Trusted source-check performs LLM OCR correction and structure repair.
- Candidate code never executes with LLM or GitHub write credentials.
- Strict semantic hard gates remain enabled.
- Issue `#3` reaches the protected processing job and produces reviewable
  semantic artifacts, or fails with a new specific content/provider error.
