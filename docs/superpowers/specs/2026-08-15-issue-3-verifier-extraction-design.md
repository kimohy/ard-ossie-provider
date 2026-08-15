# Issue #3 Verifier Extraction Design

## Goal

Verify Issue #3 semantic PDF artifacts based on fidelity and deterministic reuse, whether the source PDF is read from a usable embedded text layer or through OCR.

## Contract

- Accept only PDF extraction modes: `pdf_embedded` and `ocr`.
- Reject non-PDF modes such as `docx_xml`.
- Keep the existing five-page, zero-loss, zero-duplication, non-failing fidelity, safe Markdown, table, correction-evidence, and provider-free reuse checks.
- Do not force OCR when embedded text is available and valid.
- Do not weaken the existing OCR correction audit checks; they remain conditional on recorded correction patches.

## Implementation

Enforce the accepted PDF modes in `verify_issue_3` and rename the script description from OCR-specific to semantic-PDF-specific wording. Load the candidate decision report, require its source hash and provider/model identity to be internally consistent, and replay candidate mode with that identity while retaining provider methods that fail if called. Add a behavioral contract test covering both accepted PDF modes, trusted candidate replay, and rejection of `docx_xml`.

## Validation

- The new contract test must fail against the current OCR-only guard and pass after the change.
- The full repository test suite and Ruff must pass.
- The regenerated Issue #3 product verifier must pass against `products/500138301`.
- CommonMark parsing of the generated Markdown must retain 12 headings at levels `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`, 10 tables, no raw HTML, no literal rendered escape characters, and the expected corrected Korean terms.
