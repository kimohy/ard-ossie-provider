# Issue #3 Verifier Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Issue #3 artifact verifier accept either validated PDF embedded-text extraction or OCR without weakening fidelity checks.

**Architecture:** Keep `verify_issue_3` as the end-to-end artifact gate. It accepts the two PDF modes, rejects non-PDF modes, validates candidate decision provenance, and replays candidate mode with the recorded provider/model identity while keeping actual provider calls prohibited.

**Tech Stack:** Python 3.12, Pydantic models, pytest, Ruff, markdown-it-py

## Global Constraints

- Accept only `ExtractionMode.PDF_EMBEDDED` and `ExtractionMode.OCR`.
- Preserve all existing fidelity, correction-audit, Markdown safety, and trusted-reuse checks.
- Do not modify generated Issue #3 artifacts manually.

---

### Task 1: Correct the Issue #3 PDF extraction contract

**Files:**
- Modify: `scripts/verify_issue_3_semantic.py`
- Create: `tests/unit/test_issue_3_verifier.py`

**Interfaces:**
- Consumes: `ExtractionMode`, `SemanticFidelityReport`, and `DecisionReport`
- Produces: a provider-free `verify_issue_3(product_root: Path) -> dict[str, object]` result

- [ ] **Step 1: Write the failing contract test**

```python
@pytest.mark.parametrize("mode", (ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR))
def test_issue_3_verifier_accepts_pdf_extraction_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ExtractionMode,
) -> None:
    root = write_candidate_artifact(tmp_path, mode)
    install_provider_free_candidate_parser(monkeypatch)

    result = verify_issue_3(root)

    assert result["page_count"] == 5


def test_issue_3_verifier_rejects_non_pdf_extraction_mode(tmp_path: Path) -> None:
    root = write_candidate_artifact(tmp_path, ExtractionMode.DOCX_XML)
    with pytest.raises(Issue3VerificationError, match="ISSUE_3_NOT_PDF"):
        verify_issue_3(root)
```

- [ ] **Step 2: Run the test and verify the embedded-text case fails**

Run: `uv run pytest -q tests/unit/test_issue_3_verifier.py`

Expected: FAIL because `pdf_embedded` is rejected as `ISSUE_3_NOT_OCR` and candidate replay is not configured with the recorded provider identity.

- [ ] **Step 3: Implement the minimal PDF-mode guard**

```python
_require(
    fidelity.extraction_mode in {ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR},
    "ISSUE_3_NOT_PDF",
)
decision_report = DecisionReport.model_validate_json(decision_path.read_bytes())
identities = {(item.provider, item.model) for item in decision_report.decisions}
_require(len(identities) == 1, "ISSUE_3_DECISION_PROVIDER_INVALID")
provider_name, model = next(iter(identities))
provider = _ProviderMustNotRun(
    fidelity,
    provider=provider_name,
    model=model,
)
reused = DoclingParser(
    trusted_fidelity_report=fidelity,
    semantic_pipeline_mode="candidate",
    candidate_provider=provider,
    trusted_candidate_decisions=decision_report.decisions,
).parse(source)
```

Update the script description to say “semantic PDF artifact.” Keep `generate_structured` and `generate_multimodal_structured` fail-closed so a cache miss fails verification.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
uv run pytest -q tests/unit/test_issue_3_verifier.py
uv run ruff check .
uv run pytest -q
```

Expected: all commands pass.

- [ ] **Step 5: Commit the implementation**

```bash
git add scripts/verify_issue_3_semantic.py tests/unit/test_issue_3_verifier.py
git commit -m "fix: accept embedded PDF issue verifier input"
```

### Task 2: Validate the regenerated Issue #3 artifact

**Files:**
- Read: `products/500138301/generated/data-semantic.md`
- Read: `products/500138301/quality/semantic-fidelity.json`

**Interfaces:**
- Consumes: the regenerated PR #5 product tree
- Produces: a passing Issue #3 verifier result and CommonMark rendering evidence

- [ ] **Step 1: Run the artifact verifier**

Run: `uv run python scripts/verify_issue_3_semantic.py --product-root products/500138301`

Expected: JSON result with a non-failing status, `page_count` 5, and a stable Markdown SHA-256.

- [ ] **Step 2: Parse the generated Markdown as CommonMark/GFM**

Run a read-only `MarkdownIt("commonmark").enable("table")` parse and assert heading levels `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`, 10 tables, zero raw HTML tokens, zero literal backslashes in rendered HTML, and the corrected Korean terms.

- [ ] **Step 3: Confirm GitHub state**

Confirm PR #5 quality checks pass and Issue #3 remains open until PR #5 is merged.
