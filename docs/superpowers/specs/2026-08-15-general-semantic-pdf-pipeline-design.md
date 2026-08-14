# General Semantic PDF Pipeline Design

**Status:** Approved design; written specification for review

**Date:** 2026-08-15

**Scope:** Replace the brittle span-to-free-form-repair path with an evidence-preserving,
candidate-based semantic PDF pipeline that works across embedded-text, scanned, and mixed PDFs.

## 1. Context and evidence

Issue #3 exposed an architectural failure rather than an isolated prompt defect. The source PDF
is valid, contains five pages, and has a readable Korean/English embedded text layer. The current
extractor nevertheless turns PDF text-object boundaries and whitespace into 717 source spans.
Deterministic structure reconciliation leaves 677 spans unresolved, so the model must return a
large, exact allocation plan while also reconstructing headings, paragraphs, and tables.

The action history shows three distinct failure classes:

- embedded-text runs preserved source coverage but produced 403 degraded blocks, raw layout
  fragments, no tables, and unusable Markdown;
- OCR-first runs used unsuitable recognition behavior and introduced corrupt Korean text such as
  Chinese characters while still reporting superficially successful coverage; and
- later repair runs failed strict validation with missing spans, empty responses, invalid tables,
  exclusions, or schemas. The most recent evidence-batched run rejected every page and applied no
  blocks after two logical attempts.

The underlying problems are:

1. extraction artifacts are treated as semantic boundaries;
2. a generative model is asked to author a large exact structure rather than make a bounded
   decision;
3. whitespace evidence, layout gaps, and output spacing are conflated;
4. source coverage can be perfect even when the chosen source text is wrong;
5. strict publication validation is correct, but successful intermediate page work is discarded;
   and
6. failed validation jobs do not retain enough page-level evidence to diagnose the returned
   candidate or model decision.

## 2. Goals

- Produce usable semantic Markdown from common embedded-text, scanned, and mixed PDFs.
- Preserve every authoritative non-whitespace character and its provenance.
- Reconstruct Korean spacing without allowing an LLM to rewrite, translate, summarize, or invent
  text.
- Build headings, paragraphs, lists, reading order, and complete table grids from deterministic
  candidates.
- Use an LLM or visual model only to select among bounded, prevalidated candidates when
  deterministic evidence is ambiguous.
- Avoid LLM calls for unambiguous embedded-text pages.
- Cache successful page and region decisions while retaining atomic document publication.
- Make every failure attributable to a stage, page, region, invariant, and retry decision.
- Produce deterministic final artifacts for fixed evidence, configuration, and cached decisions.

## 3. Non-goals

- General document rewriting, copy editing, summarization, or translation.
- Allowing a model to return replacement source prose, arbitrary span IDs, or a new table grid.
- Treating the PDF's text-object boundaries as semantic truth.
- Publishing a partially verified document.
- Guaranteeing automatic publication for unreadable scans or genuinely ambiguous layouts.
- Changing the semantic behavior of DOCX, HTML, dictionary, Registry, or Ossie sources.

## 4. Decision

Adopt a deterministic core with bounded model adjudication:

1. Extract an immutable evidence document from embedded text and page geometry, using OCR evidence
   only where embedded text is absent or demonstrably incomplete.
2. Normalize layout into lines and regions without replacing or deleting source evidence.
3. Generate complete, valid text, block, reading-order, and table candidates deterministically.
4. Auto-select a candidate when its evidence score and margin meet a configured threshold.
5. Send only ambiguous regions, compact candidate summaries, and an optional bounded image crop to
   a model. The model may return only an allowlisted candidate ID.
6. Assemble the selected candidates into a canonical semantic intermediate representation.
7. Validate evidence coverage, character preservation, ordering, table topology, and rendering
   before publication.

The former free-form semantic repair path is retained only behind a rollback flag during rollout
and is removed after the new path is stable.

## 5. Architecture and responsibility boundaries

The pipeline has six explicit stages:

```text
source snapshot
  -> evidence extraction
  -> layout normalization
  -> candidate generation
  -> bounded adjudication
  -> canonical assembly and validation
  -> deterministic rendering and publication
```

### 5.1 Evidence extraction

`EvidenceDocument` is immutable and content-addressed. It contains:

- the source hash, page count, dimensions, and extraction mode;
- non-whitespace character atoms in source order, with page, bounding box, source object, font,
  and confidence metadata where available;
- explicit source whitespace and line-break atoms as evidence, not output instructions;
- page images or image hashes when visual verification is permitted; and
- extraction engine, model, language, and configuration versions.

Embedded PDF text is the character authority whenever it is readable and sufficiently complete.
The extractor must preserve its non-whitespace code points exactly. A Unicode-normalized view may
be used for scoring, but it cannot replace the authoritative code points in output.

For a scanned region, the page image is the authority. OCR engines may contribute multiple
recognition hypotheses with boxes and confidence. A visual model may choose among precomputed
hypotheses but may not freely transcribe replacement prose. If no hypothesis reaches the required
confidence, the region requires review.

Mixed PDFs are resolved per region rather than by forcing one extraction mode over the whole
document. Duplicate embedded and OCR evidence for the same visible region must be detected before
assembly.

### 5.2 Layout normalization

Normalization creates derived objects that reference evidence atoms:

- `LayoutLine` groups characters by baseline, writing direction, and geometric continuity;
- `LayoutRegion` groups lines into headings, paragraphs, lists, captions, figures, or table areas;
- whitespace ownership records whether a source whitespace atom was retained, normalized, or
  excluded as layout-only evidence;
- repeated edge regions identify page numbers, headers, and footers using cross-page frequency;
  and
- a reading-order graph represents legal ordering constraints across columns and regions.

Text-object boundaries are ignored unless geometry or typography independently supports a
semantic boundary. Page breaks do not automatically create spaces or paragraph breaks. Every
derived object retains links back to its complete evidence set.

### 5.3 Candidate generation

The deterministic candidate builder produces only internally valid, complete alternatives:

- text candidates assign each inter-character boundary `none`, `space`, or `hard_break`;
- block candidates assign a bounded semantic type and heading/list level;
- reading-order candidates are topological orderings of the layout graph;
- table candidates define rectangular row/column grids, cell ownership, spans, and header roles;
  and
- continuation candidates join blocks or tables across adjacent pages when edge geometry and
  repeated headers support continuation.

Docling output is a structural hint, not an authority. Geometry, font transitions, alignment,
separator lines, repetition, lexical features, and source order contribute independently scored
evidence. An invalid or incomplete table grid is never offered as a selectable candidate.

### 5.4 Bounded adjudication

The adjudicator first applies deterministic thresholds. A model call occurs only when two or more
valid candidates remain close enough to be materially ambiguous.

Each request is scoped to one paragraph, heading, list, table region, or cross-page continuation.
It contains:

- the immutable evidence hash and region coordinates;
- a bounded page crop when visual evidence is required;
- allowlisted candidate IDs and compact structural summaries;
- the exact decision type; and
- a closed response schema.

The response may contain only `candidate_id` and advisory confidence. Unknown IDs, replacement
text, new source references, or schema additions are rejected. Provider confidence alone cannot
override invariants or minimum deterministic evidence.

An accepted decision is cached by source, evidence, region, candidate-set, prompt, schema,
provider, and model hashes. The cache makes retries and repeated builds stable even if the remote
model is nondeterministic.

### 5.5 Canonical assembly

Selected candidates form a versioned canonical semantic IR containing:

- ordered headings, paragraphs, lists, tables, figures, and exclusions;
- exact evidence allocations and output text;
- spacing and structure decision provenance;
- confidence and review state per region; and
- source, extractor, candidate-builder, adjudicator, schema, and policy hashes.

Renderers consume only validated canonical IR. They cannot reinterpret source geometry or call a
model.

## 6. Korean and multilingual whitespace restoration

Source whitespace, geometric separation, and publishable spacing are separate concepts. For each
semantic block, the system builds a whitespace lattice across the authoritative non-whitespace
character sequence.

Candidate scoring uses:

- horizontal gaps normalized by local font size and character width;
- PDF text-object and line-wrap boundaries as low-confidence hints only;
- Korean morphology, particles, common compounds, and dictionary likelihood;
- transitions among Hangul, Latin letters, numbers, symbols, and units;
- punctuation and paired-delimiter rules;
- neighboring lines in the same paragraph or table column; and
- block-specific policies for headings, prose, lists, and table cells.

The top bounded candidates are complete strings made from the same authoritative non-whitespace
characters in the same order. A model may select a candidate ID when deterministic scores are
ambiguous. It cannot return corrected text.

For embedded text, the following invariant is absolute:

```text
remove_whitespace(output_text) == concatenate(authoritative_non_whitespace_code_points)
```

Original whitespace remains auditable even when output spacing changes. The decision report lists
each changed boundary, its source evidence, selected state, scoring features, decision source,
and confidence.

For OCR regions there is no embedded character authority. Recognition is therefore completed
before spacing restoration by selecting one bounded OCR hypothesis against the page image. Once
selected, its non-whitespace characters become immutable for the remaining pipeline. Low-
confidence recognition cannot be hidden by a high-confidence spacing decision.

## 7. Validation invariants

A document is publishable only when all applicable invariants pass:

1. Every authoritative non-whitespace atom is allocated exactly once or belongs to an explicitly
   validated exclusion.
2. Every source whitespace atom is accounted for as retained, normalized, or layout-only.
3. Embedded-text non-whitespace characters are neither added, deleted, substituted, nor reordered.
4. Block order satisfies the reading-order graph and page constraints.
5. Every table is a complete rectangular grid with legal row/column spans, no overlapping cell
   ownership, and no unallocated table evidence.
6. Cross-page continuation preserves source order and does not merge unrelated regions.
7. No selected candidate or model response references evidence outside its allowlist.
8. Markdown is non-empty, contains no renderer-generated raw HTML, and preserves the canonical IR.
9. All required regions meet the publication confidence policy.

If no valid candidate exists, the pipeline retains a lossless diagnostic representation but does
not publish it as successful semantic output.

## 8. State and publication policy

Canonical validation produces one of three states:

- `VERIFIED`: all invariants and automatic-publication confidence thresholds pass;
- `REVIEW_REQUIRED`: evidence preservation passes, but one or more recognition, spacing, or
  structure decisions remain below the automatic threshold; or
- `FAILED`: an evidence, character, order, topology, security, or rendering invariant fails.

Only `VERIFIED` is automatically published by default. Policy may route `REVIEW_REQUIRED` to a
human approval workflow, but it cannot silently convert that state to success. `FAILED` never
publishes semantic artifacts.

Page and region computation is incremental; publication is document-atomic. Validated page and
region results are cached, so one failed region does not force successful siblings to be sent to
the model again. The final document is promoted only after a fresh global validation of the
assembled cached and newly computed results.

## 9. Failure handling and retry policy

Failures use stable stage-prefixed codes such as:

- `EXTRACTION_*`
- `LAYOUT_*`
- `READING_ORDER_*`
- `TABLE_GRID_*`
- `SPACING_*`
- `OCR_*`
- `LLM_*`
- `INVARIANT_*`
- `RENDER_*`

Each failure records source and configuration hashes, stage, page, region, candidate set, invariant,
retryability, and safe provider metadata.

Retry behavior is failure-specific:

- transport errors, timeouts, rate limits, and empty responses use bounded exponential backoff;
- a schema-invalid model response receives at most one corrective request;
- an uncertain but valid decision becomes `REVIEW_REQUIRED` rather than triggering blind retries;
- invariant failures are deterministic and are not retried; and
- only failed or expired regions are recomputed.

No path applies a partial model-authored plan, repeats the same invalid whole-document request, or
discards already verified sibling-region decisions.

## 10. Observability and Actions artifacts

Validation jobs upload diagnostics under an unconditional `always()` step, subject to the
repository's retention policy:

- `manifest.json`: source, parser, model, prompt, schema, policy, and canonical IR hashes;
- `evidence-summary.json`: page, character, line, region, and table evidence counts;
- `candidate-report.json`: candidate IDs, features, scores, validity, and margins;
- `decision-report.json`: deterministic, NLP, model, cache, and human decisions;
- `validation-report.json`: invariant results, status, confidence, and publication decision;
- `failure-report.json`: stage, location, code, retryability, and attempt history; and
- optional page crops and coordinate overlays for failed or review-required regions.

Default diagnostics omit full source text and image bytes. They use hashes, counts, coordinates,
and short masked previews. Raw-text or image diagnostics require an explicit protected debug mode,
must never contain credentials or complete provider payloads, and use a shorter retention policy.

Operational metrics include processing time by stage, candidate counts, LLM calls, tokens, cache
hits, review-required regions, invariant failures, and final status. Metrics are partitioned by
extraction mode and document complexity so a quality gain cannot conceal a cost regression.

## 11. Test strategy

The regression corpus covers:

- clean embedded-text PDFs;
- scanned Korean/English PDFs;
- mixed embedded and image regions;
- Korean, English, numbers, symbols, and units in the same sentence;
- excessive PDF text-object fragmentation and synthetic whitespace insertion;
- multi-column reading order, headers, footers, page numbers, captions, and footnotes;
- tables with blank cells, merged cells, multiple headers, no visible borders, and page
  continuations;
- rotated pages and irregular coordinates; and
- the real Issue #3 Marketing Insight PDF.

Testing combines:

- golden canonical IR and Markdown for manually reviewed fixtures;
- unit tests for extraction adapters, whitespace lattices, table grids, ordering, and validators;
- property tests that mutate object boundaries, whitespace, coordinates, and candidate order while
  asserting character conservation and determinism;
- replay tests for accepted model decisions without network access;
- contract tests that reject unknown candidate IDs, free text, invalid schemas, and cross-region
  evidence;
- workflow tests proving diagnostics upload after validation failure; and
- performance regression tests grouped by page count, evidence density, and table complexity.

The current two-spans-per-page synthetic repair fixture is insufficient and cannot be the main PDF
regression. At least one table-heavy fixture must exercise hundreds of character atoms and multiple
ambiguous regions without requiring an enormous model response.

## 12. Acceptance criteria

For every publishable embedded-text fixture:

- non-whitespace character preservation is 100%;
- missing and duplicate evidence counts are zero;
- invalid or degraded blocks are zero;
- every table passes topology validation;
- unambiguous pages make zero model calls; and
- repeated runs with the same cached decisions produce the same canonical and Markdown hashes.

For Issue #3 specifically:

- the five-page embedded text layer remains authoritative;
- reviewed Korean words and sentences contain neither OCR corruption nor extraction-inserted
  spacing errors;
- manually reviewed headings, paragraphs, and table grids match the golden canonical IR;
- coverage is 100%, with zero missing, duplicated, or degraded evidence;
- output contains no raw HTML; and
- the workflow publishes only with `VERIFIED` status and retains diagnostics on failure.

For scanned fixtures, character quality is measured against reviewed ground truth by script and
reported separately from spacing and structure accuracy. A low-confidence or disagreeing OCR
hypothesis must produce `REVIEW_REQUIRED`, not a false `VERIFIED` result.

## 13. Performance and cost controls

- Deterministic extraction, normalization, candidate generation, and validation are the default
  path.
- Model calls are proportional to ambiguous regions, not raw span count or total page count.
- Candidate count, page-crop dimensions, request bytes, output schema, token budget, retries, and
  concurrent calls have explicit upper bounds.
- Cross-page tables may join adjacent regions but cannot create an unbounded whole-document model
  request.
- Exact cache keys prevent repeat calls for verified decisions.
- CI compares stage latency, peak memory, model-call count, and tokens against versioned baselines.
  Thresholds are calibrated from the representative corpus before the new path becomes default.

## 14. Rollout and compatibility

Rollout proceeds in reversible stages:

1. Introduce the canonical evidence and semantic IR schemas behind a feature flag.
2. Add invariant validators and always-uploaded diagnostics without changing publication output.
3. Run normalization and candidate generation in shadow mode beside the current path.
4. Establish golden fixtures and compare old/new quality, cost, and latency.
5. Enable deterministic spacing and table decisions for high-confidence regions.
6. Enable bounded model adjudication and content-addressed decision reuse.
7. Switch the new pipeline to the default only after the representative corpus meets acceptance.
8. Keep the old path behind a rollback flag through a defined stabilization window.
9. Remove the free-form repair path and obsolete diagnostics after stabilization.

DOCX and other non-PDF parsers retain their current authoritative source structures. Public product,
dictionary, Registry, and Ossie schemas do not change unless a later implementation plan identifies
a required versioned migration. Generated artifacts continue to originate from immutable source
attachments and are never manually corrected.

## 15. Risks and mitigations

- **Candidate explosion:** use staged pruning and require each candidate generator to enforce
  bounded top-k output before adjudication.
- **Confident but wrong deterministic selection:** require score margins, golden regressions, and
  `REVIEW_REQUIRED` for low-separation cases.
- **Model nondeterminism:** restrict output to candidate IDs and cache accepted decisions by all
  relevant hashes.
- **OCR disagreement:** retain page-image authority, record all hypotheses, and require review when
  no hypothesis is sufficiently supported.
- **Privacy leakage through diagnostics:** default to metadata-only artifacts and require protected
  opt-in for raw crops or text.
- **A new pipeline appearing successful only on Issue #3:** gate rollout on a varied corpus and
  report quality by extraction mode and document feature.
- **Partial-cache inconsistency:** version every intermediate schema and always run document-global
  invariants before promotion.

## 16. Superseded assumptions

This design supersedes the assumptions that every PDF should be OCR-first, that every embedded span
must be returned by a generative repair plan, and that page-sized batching alone solves the
structure problem. Earlier documents remain useful as incident history, but this specification is
the target architecture for general semantic PDF processing.
