# OCR-first semantic Markdown design

**Status:** Approved design; written specification for review

**Date:** 2026-08-13

**Scope:** Replace fragmented PDF text-object parsing with full-page OCR, bounded image-grounded correction, and HTML-free GFM rendering

## 1. Context and evidence

The first real acceptance run on Issue #3 completed its workflow and preserved all source spans,
but did not produce usable semantic Markdown:

- PDFium emitted 717 embedded-text spans, of which 388 contained one character;
- deterministic reconciliation left 677 spans unresolved;
- the single repair request consumed 139,537 input tokens and was rejected with
  `SEMANTIC_REPAIR_MISSING_SPAN`;
- 403 lossless blocks rendered with raw `<pre>` and `<br>` HTML;
- no table was represented as a GFM table;
- visually intact terms such as `개인정보` and `유효성` were split across spans and tags.

The failure is architectural. PDF text objects are unsuitable as the publication authority for
this document, the all-spans-at-once repair contract is too brittle, and the fallback renderer
violates the HTML-free Markdown requirement.

## 2. Decisions

- Every semantic PDF uses whole-document, full-page OCR, even when it contains embedded text.
- DOCX keeps OOXML text and explicit table structure as its authority; it is not rasterized.
- The LLM may correct only image-grounded OCR recognition and spacing errors.
- The LLM may not summarize, paraphrase, translate, add facts, delete content, or reorganize
  content through free text.
- `data-semantic.md` contains no raw HTML elements. The renderer never emits `<pre>`, `<br>`, or
  any other tag.
- A multiline table cell is joined with one ordinary space before Markdown escaping.
- Test growth is deliberately bounded: a small focused regression set, one real Issue #3 PDF
  acceptance, and one full-suite run at completion.

## 3. Selected architecture

The semantic pipeline has five explicit stages:

1. **Authoritative extraction** produces immutable OCR or OOXML spans and structural evidence.
2. **Image-grounded correction** proposes sparse text patches for PDF OCR spans.
3. **Deterministic correction validation** accepts only bounded, evidenced changes.
4. **Structure assembly** builds headings, paragraphs, lists, and rectangular tables from the
   corrected span catalog and OCR/OOXML geometry.
5. **HTML-free rendering** produces deterministic GFM and rejects raw HTML output.

The existing `DoclingParser.parse(source) -> ParsedDocument` boundary remains unchanged. Product
HTML and dictionary processing remain out of scope.

### 3.1 PDF authority

PDF processing always invokes Docling's full-page OCR configuration for the complete document.
Embedded and OCR text are never mixed. Each publishable OCR item—paragraph line, heading line,
list item, or table cell—becomes a stable text span. Lower-level OCR words and boxes remain
matching evidence and never become independently rendered one-character fragments. Span IDs
remain bound to the source hash and a deterministic page/reading-order ordinal.

The page raster used as correction evidence is rendered deterministically from the snapshotted PDF
at the same bounded orientation and scale used by OCR. Page images are bounded by configured pixel
dimensions and encoded size before entering the approved LLM provider boundary. They are never
persisted in quality JSON or logs.

### 3.2 DOCX authority

DOCX continues to read OOXML paragraphs, list properties, tables, cells, and merge metadata.
Docling may contribute heading classification, but no OCR or LLM text correction is performed for
DOCX in this change.

## 4. Image-grounded OCR correction

Correction runs page by page so a provider never receives the whole document or hundreds of
mandatory allocations in one response. Each request contains:

- one bounded page image;
- the page's ordered OCR spans with `span_id`, OCR text, text hash, and normalized bounding box;
- a closed structured-output schema;
- instructions that permit recognition and spacing correction only.

The response is a **sparse patch list**, not a restatement of every span. Each patch contains:

- `span_id`;
- `original_text_hash`;
- `corrected_text`;
- `correction_kind`: `character_recognition` or `spacing`;
- evidence bounding box copied from the request;
- confidence.

Unchanged spans are omitted. A missing span is therefore not an error. This removes the previous
all-or-nothing `SEMANTIC_REPAIR_MISSING_SPAN` failure mode.

### 4.1 Provider contract

The provider abstraction gains one bounded multimodal structured-generation method. Provider
adapters translate the same internal text/image message blocks to OpenAI-compatible, Azure
OpenAI, Vertex Gemini, or Vertex Claude payloads. A profile declares a `vision` capability.

If the selected provider or model does not declare vision support, correction is skipped and the
OCR text continues unchanged with `SEMANTIC_OCR_CORRECTION_UNAVAILABLE`. It is never silently sent
as a text-only correction request.

### 4.2 Deterministic validation

A patch is accepted only when all conditions hold:

- its span exists in the current page-local allowlist;
- its original hash and evidence box exactly match the request snapshot;
- the same span appears at most once;
- corrected text is non-empty and within configured length-growth limits;
- the patch does not add or remove line blocks;
- correction kind is allowlisted;
- confidence meets the threshold;
- control characters and raw HTML syntax are absent;
- the request is bound to source, page-image, OCR catalog, prompt, schema, provider, and model
  hashes.

Accepted patches create a new immutable corrected span catalog. The original OCR text is retained
by hash in the audit record. Rejected patches do not partially mutate source state.

Deterministic code cannot prove semantic equivalence in the general case. Consequently the
image-grounded schema, sparse patch shape, correction categories, size bounds, and audit trail are
the safety boundary. Any provider failure leaves OCR text unchanged.

## 5. Structure assembly

The OCR document's own block and table-cell geometry is the primary structural source. Structure
assembly is deterministic:

- OCR headings become Markdown headings with bounded levels;
- OCR paragraph lines are joined according to block geometry;
- detected list items retain ordered/unordered kind and depth;
- detected table cells form rectangular grids using OCR cell coordinates;
- merged regions repeat their corrected source value only during grid expansion;
- repeated edge content and page numbers follow the existing frequency and edge-band rules.

The LLM correction call does not decide block type, order, or table coordinates. If OCR structure
is unresolved, text is emitted as ordinary Markdown paragraphs in reading order and receives a
structure warning. There is no HTML fallback.

## 6. HTML-free Markdown rendering

The renderer supports only these output constructs:

- ATX headings;
- ordinary paragraphs;
- ordered and unordered lists;
- GFM pipe tables;
- blank-line block separators.

All source text passes through a Markdown escape function. Angle brackets are backslash-escaped so
source text cannot become a raw HTML element. The renderer does not use raw HTML for line breaks,
lossless blocks, tables, or spacing.

For table cells:

- CRLF and CR become LF before rendering;
- internal line breaks and surrounding line whitespace are deterministically joined with one
  space;
- pipe and backslash characters are escaped;
- an explicit header row is used when detected;
- otherwise an empty structural header is generated while all source rows remain data rows.

The completed Markdown must satisfy a final raw-HTML guard. Detection of an emitted raw HTML tag
is a renderer defect and a hard failure, not a warning.

## 7. Audit and quality policy

`semantic-fidelity.json` records extraction mode, page count, OCR engine versions, structural
counts, table coverage, correction counts, unmatched/duplicated spans, and final status.

An `ocr_corrections` section in `semantic-fidelity.json` records, without page-image bytes:

- source and page-image hashes;
- provider, model, prompt, and schema identifiers;
- original text hash, accepted corrected text, and corrected text hash;
- page and evidence box;
- correction kind, confidence, and applied/rejected outcome;
- provider error codes and retry metadata.

Accepted corrected text is stored because exact-source reruns must reuse the reviewed patch without
calling the provider or producing different artifact bytes. Rejected provider text is never
persisted; only its hash and deterministic rejection code are recorded.

Quality status is:

- `PASS`: HTML-free Markdown, complete span allocation, and valid structure/correction invariants;
- `WARN`: OCR text is published without available correction, a provider fails, or unresolved OCR
  structure is emitted as ordinary paragraphs;
- `FAIL`: unreadable OCR, span loss/duplication, invalid table allocation, raw HTML output, or a
  security-boundary violation.

`warnings-as-errors` retains its existing behavior.

## 8. Failure handling

- OCR failure: hard error; no semantic artifact is promoted.
- Vision provider unavailable or transiently failing: publish unchanged OCR text with `WARN`.
- Invalid correction patch: reject that page's patch set, retain unchanged OCR spans, and record
  `WARN`.
- Structure failure: publish escaped ordinary paragraphs in reading order with `WARN`.
- HTML guard failure, source loss, or duplicate allocation: hard error and atomic rollback.

No generated product or Registry artifact is manually edited. Issue #3 is regenerated through the
trusted `main` workflow after the implementation merges.

## 9. Security and determinism

- Page images cross only the existing protected LLM environment boundary.
- Image bytes, OCR source text, and secrets are not logged.
- Page count, dimensions, encoded bytes, spans per page, corrected length, retries, and total
  provider calls are bounded.
- Sparse responses can only patch allowlisted span IDs from one page.
- Correction entries are content-addressed and reusable only for exact source, image, OCR, prompt,
  schema, provider, and model matches.
- Rendering remains deterministic for a fixed corrected catalog and structure.

## 10. Bounded test strategy

The implementation must not create a broad combinatorial test matrix.

Add only the focused cases needed to prove the changed contracts:

1. one OCR-source test proving full-page OCR is used even with embedded PDF text;
2. one correction validator test covering a valid image-grounded patch and essential rejection
   cases through parameterization;
3. one renderer regression proving no raw HTML and correct multiline table-cell joining;
4. one provider-boundary test for a multimodal structured request;
5. one Issue #3 acceptance script or test using the genuine PDF.

Use existing tests for unchanged invariants. Run focused tests during development, then Ruff,
schema/workflow gates, the real acceptance, and the complete suite exactly once at final
verification unless a failure requires a targeted rerun.

## 11. Acceptance criteria

For the genuine Issue #3 semantic PDF:

- extraction mode is full-page OCR for all five pages;
- `data-semantic.md` contains no raw HTML tags, including `<pre>` and `<br>`;
- `개인정보` and `유효성` appear intact;
- source tables render as actual GFM pipe tables, not footer text containing `|`;
- table-cell multiline content is joined with one space;
- unmatched and duplicated corrected spans are zero;
- every applied text change has page-image evidence and an audit record;
- the quality report has no hard errors;
- repeated processing with an exact matching correction record produces identical bytes.

For DOCX, OOXML-authoritative behavior and existing table fidelity remain unchanged.

## 12. Non-goals

- General proofreading, style improvement, translation, summarization, or semantic rewriting.
- OCR correction for DOCX.
- Reconstructing original typography, pagination, colors, or images in Markdown.
- Supporting arbitrary HTML embedded in Markdown.
- Expanding tests beyond the bounded cases in Section 10.
