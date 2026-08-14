# OCR semantic structure repair design

## Problem and confirmed root cause

Issue 3 reprocessing run `31791884978` reached the protected LLM validation
job and completed OCR visual correction, but failed with
`SEMANTIC_STRUCTURE_DEGRADED` and exit code 10.

The failure is deterministic. `parse_semantic_document()` creates a repair
planner for the protected provider, but `_repair_and_degrade()` invokes it only
when `native.extraction_mode is not ExtractionMode.OCR`. OCR documents therefore
skip LLM structure repair entirely. Every unresolved span is preserved as a
`LosslessBlock(reason="structure_unresolved")`, which increments
`degraded_block_count` and is then rejected by the publication gate.

Provider injection, environment credentials, and LLM output unwrapping are not
the cause of this failure. The repair provider never receives the OCR structure
request.

## Goals

- Run semantic structure repair for unresolved OCR spans after visual text
  correction.
- Preserve every source span exactly once unless an exclusion has explicit,
  validated evidence.
- Allow the LLM to assign structure only; it must not author, delete,
  paraphrase, translate, merge, or split source text.
- Retry a semantically invalid repair plan at most once with bounded diagnostic
  feedback.
- Emit actionable, safe diagnostics when structure repair cannot complete.
- Keep the publication gate fail-closed for lost, duplicated, unresolved, or
  uncorrected source content.
- Reprocess Issue 3 once after the fix and accept it only when the generated
  semantic artifacts pass the defined fidelity criteria.

## Non-goals

- Do not weaken or bypass `SEMANTIC_STRUCTURE_DEGRADED`.
- Do not replace lossless span accounting with free-form Markdown generation.
- Do not add unbounded LLM retries.
- Do not log API keys, full prompts, or the full source document.
- Do not refactor unrelated ingestion, dictionary, registry, or release code.

## Selected architecture

The existing immutable-span pipeline remains authoritative:

1. Full-page OCR produces spans with stable IDs, page numbers, bounding boxes,
   ordinals, text hashes, and the source hash.
2. The multimodal correction planner may correct recognition and spacing while
   preserving the document's span identity and evidence contract.
3. Deterministic reconciliation constructs all structures it can resolve.
4. If spans remain unresolved and a repair planner exists, the planner runs for
   every extraction mode, including OCR.
5. The LLM receives the immutable span catalog and returns only block kinds,
   ordering, supplied span IDs, table geometry, exclusions, and confidence.
6. The repair validator checks the returned plan before any block is applied.
7. Only a fully valid plan can replace unresolved lossless blocks.
8. Coverage and publication gates run over the corrected native document and
   the completed structure.

The minimal functional correction is to remove the OCR exclusion at the parser
boundary. This is paired with bounded semantic-plan repair and better
diagnostics so the next failure, if any, identifies the exact invalid invariant
rather than collapsing back to one generic degraded code.

## Components and responsibilities

### Parser orchestration

`semantic/parser.py` owns when structure repair runs. It will invoke
`SemanticStructureRepairPlanner.repair()` whenever unresolved spans exist and a
planner is available, regardless of extraction mode. If no planner exists,
lossless degradation remains the safe fallback.

The parser continues to enforce native-table integrity after a plan is
accepted. Applied blocks and exclusions are added only through the existing
native validation functions.

### Structure repair planner

`semantic/repair.py` remains the sole owner of repair-plan schema and semantic
validation. Validation includes:

- all returned span IDs are known;
- every requested span is allocated exactly once;
- block order is unique and monotonic;
- non-table blocks are non-empty;
- table dimensions and cell coordinates are valid;
- table cell coverage is internally consistent;
- exclusions are supported by native evidence;
- every block meets the configured confidence threshold.

The first invalid plan is retained only as structured audit data. The planner
builds one follow-up request containing the validation code and a compact list
of affected span IDs or block orders. It reuses the same schema and immutable
source context. The second response goes through the identical validator.

There is no third plan attempt. Provider-level transient retry remains the
responsibility of `LLMService`; semantic-plan retry does not duplicate network
retry policy.

### Validation diagnostics

The structure repair record will preserve per-attempt validation results. The
public error contract will include safe aggregate details:

- extraction mode;
- unresolved span count and affected page numbers;
- first and second semantic validation codes;
- provider and model identity;
- applied and rejected block counts;
- total semantic-plan attempts.

The final workflow finding will use the most specific repair validation code
when available, while retaining `SEMANTIC_STRUCTURE_DEGRADED` as the publication
category. Logs must not contain credentials, full prompts, source text, or
provider response bodies.

## Error handling

- Provider configuration failures remain exit 20 and keep their provider code.
- Provider output/schema failures remain exit 10 and report the exact schema
  code.
- Provider transient failures remain exit 30 and are not converted into
  structure degradation during trusted source-check.
- First semantic-plan validation failure triggers exactly one constrained retry.
- Second semantic-plan validation failure returns exit 10 with attempt-specific
  validation codes and unresolved evidence counts.
- Source coverage loss or duplication is always a hard failure, independent of
  repair outcome.
- Missing OCR correction pages, rejected correction patches, or correction
  warnings remain hard failures before publication.

## Focused verification strategy

Implementation follows red-green TDD with only affected tests:

1. A parser regression proves an OCR document with unresolved spans invokes the
   repair planner and applies valid semantic blocks.
2. A planner regression returns a missing-span plan first and a valid plan
   second, proving one semantic retry and a final applied outcome.
3. A planner regression returns two invalid plans, proving bounded attempts and
   preserved validation diagnostics.
4. A source-check integration regression uses the real `LLMResult.structured`
   boundary and proves strict OCR structure validation can pass.
5. Existing non-OCR repair, provider propagation, coverage, native-table, and
   fail-closed regressions are rerun only where directly affected.
6. Ruff runs only on changed Python and test files, followed by
   `git diff --check`.

The broad local test suite is explicitly excluded. The repository's required PR
checks remain the authoritative integrated code gate.

## Issue 3 acceptance criteria

After required PR checks pass and the fix is merged, Issue 3 is reprocessed by
removing and re-adding only `ard:approved` once. The same run is monitored to a
terminal state without redundant reruns.

The generated PR is accepted only when all of these checks hold:

- `data-semantic.md` contains no raw HTML tags, `&#32;`, or `&#9;`;
- Korean content includes `Semantics 문서`, `개인정보`, `유효성`, and
  `캠페인 기간`;
- known corrupt strings `是州`, `号h`, and `左叫` are absent;
- semantic fidelity reports exactly five audited pages;
- source text coverage is `1.0`;
- rejected OCR corrections are zero;
- degraded blocks are zero;
- warning codes are empty;
- semantic fidelity status is `PASS`;
- the quality report contains no hard errors and is `PASS` or has only an
  unrelated, explicitly explained warning.

The managed product PR remains draft unless every criterion passes.

## Temporary environment approval policy

The `ard-llm` environment currently has required reviewer `kimohy`. That single
required-reviewer rule may be removed only for the controlled Issue 3
remediation window. Secrets and custom branch policy remain unchanged.

The reviewer must be restored immediately after Issue 3 succeeds and its
artifacts pass inspection. It must also be restored if work stops, the run
reaches a terminal failure, or two hours pass without successful completion.
Because the available GitHub integration can read but cannot update environment
reviewer rules, the user performs the removal and restoration through repository
environment settings using exact instructions supplied at action time.

## Rollout sequence

1. Implement and validate the parser, bounded retry, and diagnostics on an
   isolated branch.
2. Publish a focused PR and wait for the two required repository statuses.
3. Merge only after both statuses are successful.
4. Ask the user to remove only the `ard-llm` required-reviewer rule.
5. Trigger Issue 3 exactly once and monitor the single run.
6. Inspect generated Markdown, semantic fidelity, repair audit, and quality
   report at the exact new product PR head.
7. Restore required reviewer `kimohy` immediately on success, terminal failure,
   work stoppage, or timeout.
8. Report exact PR, merge SHA, workflow run, product PR head, artifact metrics,
   and any remaining risk.
