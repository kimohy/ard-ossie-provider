# Semantic document structure fidelity design

**Status:** Approved design; written specification for review

**Date:** 2026-08-13

**Scope:** Preserve source text and logical document structure in `data-semantic.md`

## 1. Context

The current `main` implementation fixed source-text corruption by extracting complete PDF text
with PDFium before Docling. That preserves embedded characters such as `개인정보` and `유효성`,
but it joins pages as plain text. Headings, paragraphs, lists, reading order, and tables are lost.

The required behavior is a source-faithful GFM Markdown document:

- retain the original body text without spelling, spacing, translation, or summary changes;
- preserve headings, paragraphs, nested lists, and tables as logical Markdown structures;
- expand merged cells into a rectangular Markdown table;
- remove repeated headers, footers, and page numbers;
- use a bounded LLM recovery step when deterministic structure reconciliation is insufficient;
- never allow the LLM to author text that appears in the semantic artifact.

## 2. Goals

- Make native PDF or DOCX text the only authority for final strings.
- Use Docling for logical structure and reading-order evidence rather than published text.
- Reconcile structure and authoritative text deterministically whenever possible.
- Recover unresolved structure with an LLM plan that references immutable source span IDs.
- Preserve 100% of non-excluded source spans even when structure recovery fails.
- Render deterministic GFM headings, paragraphs, nested lists, and pipe tables.
- Keep LLM-generated metrics and relationships out of `data-semantic.md`.
- Preserve existing IDs, versions, source hashes, atomic promotion, and Ossie behavior.
- Produce auditable fidelity and optional LLM repair records.

## 3. Non-goals

- Do not reproduce fonts, colors, page dimensions, multi-column geometry, or page breaks.
- Do not include source images in `data-semantic.md`; retain visible captions and alternative text.
- Do not correct OCR, spelling, grammar, spacing, or terminology with an LLM.
- Do not infer new semantic facts, metrics, relationships, or descriptions in this path.
- Do not change `data-product.md` or `data-dictionary.json` behavior.
- Do not mix embedded PDF text and OCR text within one document.

## 4. Selected architecture

Semantic parsing becomes a dual-source reconciliation pipeline:

1. A source-native adapter extracts immutable text spans.
2. Docling extracts a structural skeleton and reading-order evidence.
3. A deterministic reconciler maps source spans to structural blocks.
4. A constrained LLM repair planner handles only unresolved blocks.
5. A deterministic validator accepts or rejects the repair plan.
6. A Markdown renderer obtains every final string from the immutable span store.
7. The existing pipeline receives the resulting `ParsedDocument.markdown`.

The public parser boundary remains `DoclingParser.parse(source) -> ParsedDocument`. Product HTML
and dictionary parsing are unchanged. Semantic-specific extraction, reconciliation, repair, and
rendering live in focused modules rather than accumulating in `docling_parser.py`.

`DoclingParser.__init__` gains an optional `SemanticStructureRepairPlanner`. When production
`process_product()` creates the parser, it wraps the existing configured `LLMProvider` in that
planner and injects it. A caller that supplies a custom parser may inject a fake or real planner
explicitly; absence of a planner selects lossless degradation after deterministic reconciliation.
The existing `parse(source)` method signature and non-semantic behavior remain unchanged.

### 4.1 Source-native adapters

The PDF adapter uses PDFium to extract page text together with character or text-span locations.
The DOCX adapter reads OOXML document order, paragraphs, runs, text boxes, tables, cells, and
explicit merge metadata without executing macros, external links, or embedded objects.

Each adapter emits immutable spans with at least:

- `span_id` derived deterministically from the source hash and ordinal;
- page or document ordinal;
- normalized coordinate data when available;
- exact source text;
- a text hash.

CRLF and bare CR line endings become LF. Matching may use a normalized comparison projection, but
the stored output text is not normalized, repaired, or regenerated.

### 4.2 Structural skeleton

Docling supplies block kind, heading level, list kind and depth, table grid, merged regions,
provenance, and reading order. Its converted text is only a matching hint. It can never be copied
directly into the final Markdown when authoritative native text exists.

For DOCX tables, explicit OOXML rows, cells, and merge metadata validate the Docling grid. If the
two conflict, the explicit OOXML structure wins. Docling remains responsible for heading and list
classification and provides a cross-check on document order.

### 4.3 Internal blocks

The reconciled document uses a small discriminated model:

- `Heading(level, span_ids)`;
- `Paragraph(span_ids)`;
- `ListItem(kind, depth, span_ids)`;
- `Table(rows, columns, cells)`;
- `LosslessBlock(span_ids, reason)` for graceful degradation.

Every block retains source provenance. A consumer can understand and test each block without
depending on Docling internals or a provider-specific LLM response.

## 5. Deterministic reconciliation

### 5.1 PDF blocks

For each Docling block, candidates are limited to authoritative spans from the same page and
compatible bounding region. Candidate scoring combines:

- geometric containment and overlap;
- similarity of comparison-only normalized strings;
- monotonicity relative to the previously assigned block;
- compatibility with the detected block or table-cell region.

The highest valid candidate above a fixed threshold is selected. Source text itself is never
changed by the scoring process. Overlap between assigned spans is forbidden except for
system-created repetition when rendering a merged table cell.

### 5.2 DOCX blocks

Paragraphs and tables follow explicit OOXML document order. Native paragraphs and cells provide
the strings, while Docling contributes heading and list classifications. Explicit OOXML table
merges are authoritative when they disagree with inferred structure.

### 5.3 Repeated page elements

A block is removable as a repeated header or footer only when all conditions hold:

- it lies in a configured top or bottom page-edge band;
- its comparison signature occurs on at least two pages and at least 60% of all pages;
- it does not overlap a body block or table cell.

Simple numeric and `n/N` patterns in a page-edge band are page-number candidates. Single-page
documents and repeated text inside the body are never removed by frequency alone. Removed spans
remain represented by hashes and provenance in the fidelity audit.

## 6. Markdown rendering

- Headings use the reconciled `#` level.
- Ordered and unordered lists use deterministic indentation from list depth.
- Paragraphs preserve internal source characters, spaces, and line order.
- Structural Markdown characters are added by the renderer and are not source text.
- Markdown control characters are escaped without rewriting the underlying string.
- Blocks are separated by exactly one blank line and the file ends with one newline.

### 6.1 Tables

Tables are rendered as rectangular GFM pipe tables.

- A merged cell value is repeated into every row and column position that it occupies.
- Multiline cell content is joined with `<br>` while retaining source order.
- The first explicitly detected header row becomes the GFM header.
- Additional header rows remain as ordinary rows rather than being deleted or synthesized into
  combined labels.
- If there is no detected header, the renderer creates an empty structural header and keeps every
  source row as data.
- Pipe, backslash, and other structural characters are escaped deterministically.

## 7. Bounded LLM structure recovery

The LLM is called only for blocks that deterministic reconciliation cannot validate. It receives
the unresolved immutable span catalog, coordinate and order metadata, and the incomplete
structural skeleton. The same configured ARD LLM provider and secret boundary are reused.

The structured response may contain only:

- source `span_id` references;
- block type, heading level, list kind and depth;
- reading-order positions;
- table row and column coordinates;
- header, footer, or page-number classifications;
- confidence and evidence references.

The response schema has no free-text field. The LLM therefore cannot submit a replacement phrase,
corrected spelling, summary, metric, or additional cell value.

### 7.1 Repair validation

Deterministic code rejects a plan unless:

- every referenced span is in the request-local allowlist;
- every non-excluded unresolved span is covered exactly once;
- reading order is compatible with page and coordinate constraints;
- a proposed table is rectangular and has consistent coordinates;
- no table span is duplicated by the model;
- confidence meets the configured threshold;
- the plan is bound to the current source, span catalog, prompt, and schema hashes.

Merged-cell repetition occurs only in the renderer after validation, never in the LLM response.

### 7.2 Graceful degradation

If the provider is unavailable, its output is invalid, or the repair plan fails validation, the
pipeline still preserves the source:

- unresolved ordinary content becomes an exact-text paragraph;
- unresolved table content becomes a line- and space-preserving `<pre>` lossless block;
- the product receives `SEMANTIC_STRUCTURE_DEGRADED` and a `WARN` quality status;
- source span coverage remains 100%.

Only an unreadable source, actual source-span loss, or a security violation remains a hard error.

### 7.3 Repair reuse

A validated repair plan is stored in `quality/semantic-structure-repair.json`. It is reusable only
when the source hash, ordered span hashes, parser version, prompt version, and repair schema hash
all match. Otherwise it is ignored and a new plan is requested. Reuse prevents repeat processing
of an unchanged source from producing a different structure.

The processor never treats a repair record from the mutable PR head as trusted cache input. It may
reuse only a record resolved from the trusted base or previously promoted product state, with its
artifact hash bound to the trusted quality record. The complete plan is revalidated against the
current immutable span allowlist before use.

## 8. OCR behavior

A PDF with a complete embedded text layer uses that layer for the whole document. If any page has
no usable embedded text, the whole document uses the existing Docling OCR path; embedded and OCR
text are never mixed. OCR output becomes the authoritative text for that execution and receives
`SEMANTIC_OCR_FALLBACK`.

OCR cannot guarantee character identity with the visual source. The normal pipeline may publish a
validated OCR document with a warning, while `warnings-as-errors` blocks it. The LLM may recover
structure over immutable OCR spans but may not correct OCR text.

## 9. Fidelity and audit records

`quality/semantic-fidelity.json` records:

- source hash, extraction mode, page count, and parser versions;
- counts of headings, paragraphs, list items, tables, rows, and cells;
- source, preserved, excluded, unmatched, and duplicated span counts;
- source-text coverage;
- removed repeated-element hashes and provenance;
- degraded block provenance and reasons;
- table reconciliation results;
- deterministic thresholds and final fidelity status.

The file stores hashes and locations for excluded source elements rather than duplicating the
entire source text. Existing quality hashes and atomic promotion include the new artifacts.

`quality/semantic-structure-repair.json` exists only when a repair is requested or a matching
validated plan is reused. It records provider and model identifiers, prompt and schema versions,
request span hashes, the proposed mapping, validation findings, applied and rejected blocks, and
the final repair-plan hash. Secrets and credentials are never recorded.

## 10. Error and quality policy

- `PASS`: deterministic reconciliation succeeds, or a bounded LLM repair passes every invariant.
- `WARN`: OCR is used, or unresolved structure is published as a lossless degraded block.
- `FAIL`: the source cannot be read, non-excluded source coverage is below 100%, an unexpected
  duplicate is present, or a security validation fails.

A provider failure during structure repair is not itself a pipeline failure because a lossless
fallback exists. This does not change the existing error policy for the later semantic-suggestion
provider call. Existing `warnings-as-errors` policy may still turn repair warnings into a
publication failure for strict runs.

## 11. Determinism and security

- No LLM-authored text enters `data-semantic.md`.
- Stable span IDs, ordering rules, thresholds, and rendering rules produce stable bytes.
- A previously validated repair plan is content-addressed and reused only for an exact match.
- Existing path, MIME, size, hash, source-signature, and atomic-promotion protections remain.
- OOXML external relationships, macros, and embedded objects are not executed.
- PDF and DOCX parsing remains local; only the existing approved LLM boundary receives repair
  inputs.

## 12. Test strategy

### 12.1 Shared invariants

Every fixture proves:

1. all non-excluded source spans appear in the rendered representation;
2. each span is used exactly once before system-owned merged-cell expansion;
3. Markdown syntax and escaping are the only non-source characters in content positions;
4. LLM free text cannot enter the output schema;
5. identical input and a validated repair record produce identical output hashes.

### 12.2 Parser and renderer coverage

- Korean headings, paragraphs, ordered lists, and nested unordered lists;
- English, numbers, special characters, consecutive spaces, and line endings;
- simple tables, multiline cells, missing headers, merged cells, and multirow headers;
- repeated page headers, footers, and page numbers;
- identical body text that must not be removed;
- DOCX text boxes, table captions, image captions, and alternative text;
- Markdown control-character escaping.

### 12.3 LLM recovery coverage

- no provider call when deterministic reconciliation succeeds;
- only unresolved spans included in a repair request;
- valid heading, list, reading-order, and table repair plans;
- rejection of unknown, missing, duplicated, reversed, low-confidence, or nonrectangular mappings;
- provider failure and invalid structured output producing a lossless degraded block;
- repair-plan reuse for an unchanged source;
- cache invalidation after source, prompt, parser, or schema changes.

### 12.4 Integration and regression coverage

- source-faithful `data-semantic.md` golden output;
- unchanged `data-product.md` and `data-dictionary.json` output;
- metrics and relationships retained only in Ossie and audit outputs;
- unchanged source manifest, stable IDs, versioning, canonical-hash, and atomic-promotion behavior;
- installed wheel assets and parsers;
- complete pytest, Ruff, schema, workflow, checksum, and secret-scan gates.

## 13. Acceptance criteria

- `data-semantic.md` retains all non-excluded body text from a supported PDF or DOCX.
- Headings, paragraphs, lists, and successfully recognized tables use valid GFM structure.
- Merged table cells are expanded by repeating their source value across occupied coordinates.
- Repeated page headers, footers, and page numbers are removed under the approved edge and
  frequency rules.
- The current real semantic source contains intact `개인정보` and `유효성`, no split variants, and
  its source tables render as GFM tables when deterministic or validated LLM recovery succeeds.
- LLM output can change structure only through allowlisted source span references.
- A failed repair produces a 100%-coverage lossless artifact with `WARN`, not a provider error.
- Unreadable sources, source text loss, and security failures are the only hard failures in this
  feature path.
- No generated artifact or Registry record is manually edited.
