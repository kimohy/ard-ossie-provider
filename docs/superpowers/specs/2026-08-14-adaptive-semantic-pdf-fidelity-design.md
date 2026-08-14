# Adaptive Semantic PDF Fidelity Design

## Problem

The OCR-first PDF path can publish semantically unusable Markdown while reporting successful
source coverage. Issue #3 reproduced the failure:

- the five-page PDF has a complete, readable embedded Korean/English text layer;
- forced OCR selected RapidOCR's default Chinese recognition model;
- the OCR catalog contained 185 spans, including `Semantics 是州` and `号h`;
- the vision LLM proposed 97 corrections, but all 97 were rejected and none were applied;
- 169 blocks remained structurally degraded; and
- fidelity still reported 100% coverage because it measured preservation of the incorrect OCR
  catalog rather than agreement with the visible PDF.

The prior embedded-text path preserved the correct words but rendered PDF text-object boundaries
as HTML-heavy fragments. Embedded extraction is therefore the best textual evidence, but it is
not presentation-ready and still requires image-grounded text and structure correction.

## Decision

Use an adaptive, fidelity-first PDF pipeline:

1. Prefer the embedded PDF text catalog when every page has readable text. Do not treat its
   text-object boundaries or character mapping as presentation-ready.
2. Apply bounded, page-image-grounded LLM text correction to both embedded and OCR catalogs. It
   may fix only visible character-recognition and whitespace errors. It may not summarize,
   rewrite, translate, add, delete, or reorder semantic content.
3. Use Docling structure extraction and a separate bounded LLM structure plan to arrange the
   corrected spans into headings, paragraphs, lists, and tables. The plan must map every span
   exactly once and cannot author text.
4. Use full-page OCR only when the embedded catalog is absent or incomplete.
5. Configure Docling EasyOCR explicitly with `lang=["ko", "en"]`, using Docling's pinned
   `easyocr` extra, rather than an auto-selected engine with an uncontrolled default language.
6. Keep text correction sparse and source-bound. Corrections may change a limited number of
   visibly incorrect characters or whitespace within a span. Cross-span joining and layout
   changes belong exclusively to the structure plan.
7. Block automated publication when required visual correction is unavailable/rejected or any
   semantic structure remains degraded. Diagnostic evidence may be retained, but the result must
   not be reported as publishable success.
8. Render pure Markdown without raw HTML tags or HTML entities. Layout-only PDF whitespace may
   be normalized while all non-whitespace source content remains source-backed.

## Alternatives Considered

### Force Korean/English OCR for every PDF

This needlessly re-recognizes digitally generated text and can be less accurate than the PDF's
glyph-to-text mapping. It is slower and requires OCR models for documents that do not need them.
Rejected.

### Transcribe every page directly with a vision LLM

This can repair severe corruption but creates a large hallucination surface, costs more, and
makes exact no-add/no-delete validation difficult. Rejected as an authoritative source.

### Keep OCR-first and relax correction validation

Issue #3 required wholesale replacement of badly recognized spans. Relaxing the validation
limits would let the LLM rewrite source content and defeat the fidelity boundary. Rejected.

## Processing Flow

For a PDF semantic source:

1. Snapshot and hash the source once.
2. Attempt whole-document embedded-text extraction.
3. If every page is readable:
   - mark the mode `pdf_embedded`;
   - visually validate and sparsely correct characters/whitespace against each page image;
   - extract structure with the ordinary Docling PDF path;
   - reconcile structure against corrected embedded spans; and
   - invoke bounded structure repair for unresolved allocations.
4. Otherwise:
   - run EasyOCR full-page recognition with explicit Korean/English languages;
   - bind each span to page and bounding-box evidence;
   - run the same bounded visual correction contract; and
   - fail the publish gate if any page/correction is unavailable or rejected.
5. Render source-backed blocks as pure Markdown.
6. Validate coverage, duplication, HTML absence, correction outcome, and degraded structure
   before promotion or release.

## Correction Boundaries

Text correction and structure correction are separate contracts:

- Text correction consumes page images plus an immutable span catalog. It returns sparse patches
  bound to span ID, original hash, and bounding box. Only character-recognition and whitespace
  changes are allowed within bounded ratios.
- Structure correction consumes the corrected catalog and structural hints. It returns only
  block types and ordered span allocations. It cannot return replacement text.
- A patch or structure plan is applied atomically only after full validation. Rejected output
  leaves the source catalog unchanged and blocks publication.

## Failure Semantics

The following are hard failures for automated publication:

- unmatched or duplicated source spans;
- source text coverage below 100%;
- any required visual-correction warning, rejected patch, missing page audit, or unavailable
  vision provider on a PDF document;
- any unresolved/degraded semantic block;
- raw HTML tags or HTML entities in generated semantic Markdown; or
- empty semantic content.

Errors identify the failing boundary, for example `SEMANTIC_VISUAL_CORRECTION_GATE_FAILED`,
`SEMANTIC_STRUCTURE_GATE_FAILED`, or `SEMANTIC_MARKDOWN_HTML_FORBIDDEN`. Evidence retains hashes,
counts, pages, and safe provider metadata without prompts, response bodies, credentials, or raw
exceptions.

## Compatibility

- DOCX processing is unchanged.
- Existing PDF correction and structure-repair audit models remain in use; prompt versions and
  hashes change because embedded spans become eligible for image-grounded correction.
- `pdf_embedded` remains an existing extraction mode.
- Registry IDs, product versions, dictionary output, and Ossie generation are unchanged.
- Generated branches are regenerated from immutable source attachments; generated semantic files
  are never manually edited.

## Focused Verification

Testing is limited to affected boundaries:

1. A readable embedded PDF must not call full-page OCR, must invoke visual LLM correction, and
   must preserve/correct Korean words exactly.
2. A PDF with an unreadable page must use Korean/English EasyOCR fallback.
3. Rejected visual corrections must produce a hard quality/release failure.
4. Degraded semantic structure must block publication.
5. Semantic Markdown must contain neither raw HTML nor HTML entities.
6. The Issue #3 PDF is used as a focused deterministic probe to confirm expected Korean phrases
   and absence of the observed Chinese corruption.

No broad local test suite is required. Repository CI is the final integration gate.
