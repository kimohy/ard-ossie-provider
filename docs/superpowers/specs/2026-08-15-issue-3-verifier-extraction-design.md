# Issue #3 Verifier Extraction Design

## Goal

Verify Issue #3 semantic PDF artifacts based on fidelity and deterministic reuse, whether the source PDF is read from a usable embedded text layer or through OCR.

## Contract

- Accept only PDF extraction modes: `pdf_embedded` and `ocr`.
- Reject non-PDF modes such as `docx_xml`.
- Keep the existing five-page, zero-loss, zero-duplication, non-failing fidelity, safe Markdown, table, correction-evidence, and provider-free reuse checks.
- Do not force OCR when embedded text is available and valid.
- Do not weaken the existing OCR correction audit checks; they remain conditional on recorded correction patches.
- Bind the fidelity, validation, and decision reports to the actual scanned PDF source hash and to their hashes recorded in `quality-report.json`.
- Trust only the packaged `openai-compatible-default` provider/model identity. A mutable decision report cannot choose its own trusted identity.
- Require every candidate decision to be resolved, and prove that non-deterministic decisions replay from cache without a provider call.

## Implementation

Enforce both the reported extraction mode and the actual `.pdf` source in `verify_issue_3`. Validate quality-artifact hashes and the verified/publishable validation report before replay. Candidate artifacts use the independently configured packaged provider identity and must replay all model decisions as cache hits while provider methods fail closed. Legacy artifacts with applied OCR correction audits use a separate correction-planner replay path; candidate decisions and legacy correction audits are not mixed.

The quality report is an integrity-consistency anchor within the product, not an external signature. Authenticity still comes from the protected workflow and repository review boundary.

## Validation

- Focused tests cover actual source type/hash binding, quality-artifact hashes, independent provider identity, resolved decisions, replay extraction mode, and mandatory cache reuse.
- The full repository test suite and Ruff must pass.
- The regenerated Issue #3 product verifier must pass against `products/500138301`.
- CommonMark parsing of the generated Markdown must retain 12 headings at levels `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`, 10 tables, no raw HTML, no literal rendered escape characters, and the expected corrected Korean terms.
