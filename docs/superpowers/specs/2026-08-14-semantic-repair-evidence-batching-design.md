# Semantic Structure Repair Evidence Batching Design

Date: 2026-08-14

## Status

Approved direction. This document defines the implementation contract before code changes.

## Problem

Issue #3 supplies a five-page semantic PDF whose unresolved structure contains 677 immutable
source spans. The configured OpenAI-compatible profile already uses the model maximum output
length. Nevertheless, both whole-document repair attempts omitted at least one supplied span and
failed with `SEMANTIC_REPAIR_MISSING_SPAN`.

Increasing an output-token setting cannot resolve this failure because no explicit application
limit remains. The repair request must reduce the response surface while preserving the existing
lossless and fail-closed guarantees.

## Goals

- Partition large repair work using source evidence, not an arbitrary span or token limit.
- Preserve every unresolved source span exactly once without changing its text.
- Keep native tables within one repair batch, including tables that cross page boundaries.
- Produce one deterministic document-level `RepairPlan` and audit record.
- Retry invalid model output at most once at the logical document-pass level.
- Never publish a partially repaired document.
- Retain compatibility for documents that naturally form one evidence batch.

## Non-goals

- Correcting, paraphrasing, translating, adding, or deleting source text.
- Relaxing missing, unknown, duplicate, order, table, exclusion, or confidence validation.
- Introducing a configurable chunk-size or token-size limit.
- Changing the public semantic-repair report schema.
- Making provider calls concurrently.

## Chosen Approach

### Evidence batches

The planner first builds the existing full repair context and validates all requested span IDs.
It then partitions the ordered unresolved spans into deterministic evidence batches:

1. Spans on the same source page belong to the same initial component.
2. All unresolved spans owned by one native table belong to the same component.
3. Components are expanded to contiguous ordinal intervals. If a table connects two pages, every
   unresolved span between those table spans joins the same batch. This prevents a merged block
   from jumping over spans assigned to another batch.
4. Page-less spans are grouped by their contiguous ordinal region. If the document has no page
   evidence, it remains one batch and retains the current behavior.
5. Overlapping components are merged until every final batch is a disjoint, contiguous slice of
   the full ordered span sequence.

This approach has no fixed batch size. A five-page PDF normally yields five calls, while a
cross-page table may reduce that count by joining adjacent page batches.

### Batch requests

Each batch receives only its immutable span IDs, text, ordinals, page numbers, and bounding boxes.
Structural hints are limited to the batch's pages, with page-less hints retained only where the
batch also lacks page evidence. The response schema is unchanged.

The prompt version advances because one request now represents an evidence region rather than
necessarily the whole document. The prompt explicitly requires every supplied batch span exactly
once and forbids references outside the batch.

### Validation and retry

A logical repair pass consists of one serial provider request for every batch needing work.

- Each first-pass response is validated against its batch context using the existing strict
  validator.
- Valid batches are retained in memory.
- Invalid batches record their validation code and are the only batches requested again during
  the second logical pass. Retry feedback remains bounded and requests a complete replacement for
  that batch.
- A provider error in any batch fails the whole repair. No validated sibling batch is applied.
- If any second-pass batch remains invalid, the whole repair is rejected. No partial blocks are
  returned.

Validation codes remain ordered by logical pass and batch position. Diagnostic `attempts` means
logical document passes and is therefore `1` or `2`, independent of the number of serial batch
requests. Detailed validation codes still expose every failed batch response.

### Deterministic merge

After all batches validate:

1. Collect the validated batch blocks.
2. Sort them by the first allocated span's position in the full repair context.
3. Reassign block orders to one consecutive document-level sequence.
4. Serialize the combined plan and run the existing validator again against the full context.

The final global validation is authoritative. It must prove that all original unresolved spans
are allocated exactly once, allocations follow global source order, tables are valid, exclusions
are independently justified, and confidence meets the configured threshold.

Only this combined plan is converted to semantic blocks, hashed, persisted, or reused. Existing
source hash, ordered span hashes, schema hash, parser version, prompt version, plan hash, and
applied/rejected order checks continue to protect trusted reuse.

## Failure Semantics

- Invalid input context: reject before calling the provider.
- Provider failure in any batch: degrade or propagate according to the existing fail-fast option.
- Invalid first pass: retry only failed batches once.
- Invalid second pass: reject the entire document.
- Invalid deterministic merge or global validation: reject the entire document.
- Rejected or degraded repair: preserve all unresolved source spans through the existing lossless
  blocks and fail the semantic quality gate.

No branch can apply only a subset of batch results.

## Compatibility

- One-batch documents follow the existing one-request plus one-retry path.
- The strict response schema and public audit schema do not change.
- Advancing the prompt version intentionally prevents reuse of plans generated under the old
  whole-document request contract.
- Existing native-table post-validation remains authoritative after repair.

## Verification Strategy

Use narrowly scoped regression tests rather than the complete repository suite during iteration:

1. Two page batches produce one globally ordered, fully covered plan.
2. A native table joining pages produces one contiguous evidence batch.
3. Only an invalid batch is retried; a valid sibling is not called again.
4. A second invalid response rejects the entire repair with no partial blocks.
5. A provider error in one batch discards all sibling results.
6. A single evidence batch preserves the existing two-call maximum and retry feedback.
7. Trusted-plan hashing and reuse operate only on the combined plan.
8. Diagnostic attempts remain bounded at two while validation codes identify batch failures.

The final local gate is limited to the semantic repair unit tests, the directly affected pipeline
diagnostic tests, and Ruff on changed files. GitHub CI remains the broader integration gate.

## Issue #3 Acceptance

After the fix reaches `main`, retrigger Issue #3 exactly once and keep PR #5 in draft until:

- `data-semantic.md` contains the expected Korean semantic content and contains no raw HTML or
  known OCR garbage strings;
- semantic fidelity reports five pages, full source coverage, no duplicated or unmatched spans,
  no rejected OCR corrections, no degraded blocks, and no warnings;
- semantic structure repair is `applied` or safely `reused`, with no provider error and no rejected
  orders; and
- the quality report has no hard errors.
