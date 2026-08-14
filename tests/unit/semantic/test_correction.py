from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import pytest

from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.llm import (
    LLMMetadata,
    LLMResult,
    LLMTextPart,
    ProviderExecutionError,
    ProviderFailureKind,
)
from ard_ossie.semantic.models import (
    ExtractionMode,
    NativeDocument,
    SourceBox,
    SourceSpan,
    make_span_id,
)

SOURCE_HASH = "a" * 64


def _span() -> SourceSpan:
    text = "개 인정보"
    return SourceSpan(
        span_id=make_span_id(SOURCE_HASH, 0),
        ordinal=0,
        page=1,
        bbox=SourceBox(left=0.1, bottom=0.2, right=0.8, top=0.3),
        text=text,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _identity(value: dict[str, object]) -> dict[str, object]:
    return value


@pytest.mark.parametrize(
    ("case", "expected_code", "mutate"),
    [
        pytest.param("valid", None, _identity, id="valid"),
        pytest.param(
            "unknown",
            "SEMANTIC_OCR_CORRECTION_UNKNOWN_SPAN",
            lambda patch: patch | {"span_id": "span_0000000000000000"},
            id="unknown-span",
        ),
        pytest.param(
            "hash",
            "SEMANTIC_OCR_CORRECTION_HASH_MISMATCH",
            lambda patch: patch | {"original_text_hash": "0" * 64},
            id="hash",
        ),
        pytest.param(
            "box",
            "SEMANTIC_OCR_CORRECTION_BOX_MISMATCH",
            lambda patch: patch | {"bbox": {"left": 0.0, "bottom": 0.0, "right": 0.1, "top": 0.1}},
            id="box",
        ),
        pytest.param(
            "duplicate",
            "SEMANTIC_OCR_CORRECTION_DUPLICATE_SPAN",
            _identity,
            id="duplicate",
        ),
        pytest.param(
            "empty",
            "SEMANTIC_OCR_CORRECTION_TEXT_EMPTY",
            lambda patch: patch | {"corrected_text": ""},
            id="empty",
        ),
        pytest.param(
            "growth",
            "SEMANTIC_OCR_CORRECTION_TEXT_GROWTH_EXCEEDED",
            lambda patch: patch | {"corrected_text": "개인정보" * 4},
            id="growth",
        ),
        pytest.param(
            "deletion",
            "SEMANTIC_OCR_CORRECTION_SPACING_INVALID",
            lambda patch: patch | {"corrected_text": "삭제됨"},
            id="short-deletion",
        ),
        pytest.param(
            "paraphrase",
            "SEMANTIC_OCR_CORRECTION_RECOGNITION_INVALID",
            lambda patch: (
                patch | {"corrected_text": "마 케팅", "correction_kind": "character_recognition"}
            ),
            id="paraphrase",
        ),
        pytest.param(
            "false-spacing",
            "SEMANTIC_OCR_CORRECTION_SPACING_INVALID",
            lambda patch: patch | {"corrected_text": "개인 자료"},
            id="false-spacing",
        ),
        pytest.param(
            "line",
            "SEMANTIC_OCR_CORRECTION_LINE_BLOCK_CHANGE",
            lambda patch: patch | {"corrected_text": "개인\n정보"},
            id="line-block",
        ),
        pytest.param(
            "kind",
            "SEMANTIC_OCR_CORRECTION_KIND_INVALID",
            lambda patch: patch | {"correction_kind": "rewrite"},
            id="kind",
        ),
        pytest.param(
            "confidence",
            "SEMANTIC_OCR_CORRECTION_CONFIDENCE_LOW",
            lambda patch: patch | {"confidence": 0.79},
            id="confidence",
        ),
        pytest.param(
            "html",
            "SEMANTIC_OCR_CORRECTION_TEXT_UNSAFE",
            lambda patch: patch | {"corrected_text": "개인<br>정보"},
            id="raw-html",
        ),
        pytest.param(
            "control",
            "SEMANTIC_OCR_CORRECTION_TEXT_UNSAFE",
            lambda patch: patch | {"corrected_text": "개인\x00정보"},
            id="control",
        ),
        pytest.param(
            "request",
            "SEMANTIC_OCR_CORRECTION_REQUEST_MISMATCH",
            _identity,
            id="request-binding",
        ),
    ],
)
def test_image_grounded_patch_validation_is_all_or_nothing(
    case: str,
    expected_code: str | None,
    mutate: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    from ard_ossie.semantic.correction import make_page_snapshot, validate_page_corrections

    span = _span()
    snapshot = make_page_snapshot(
        source_hash=SOURCE_HASH,
        page=1,
        page_image=b"\x89PNG\r\n\x1a\nfixture",
        spans=(span,),
        provider="openai_compatible",
        model="gpt-5.6-terra",
    )
    patch = mutate(
        {
            "span_id": span.span_id,
            "original_text_hash": span.text_hash,
            "corrected_text": "개인정보",
            "correction_kind": "spacing",
            "bbox": span.bbox.model_dump(mode="json"),
            "confidence": 0.99,
        }
    )
    patches = [patch, dict(patch)] if case == "duplicate" else [patch]
    response = {
        "request_hash": "0" * 64 if case == "request" else snapshot.request_hash,
        "patches": patches,
    }

    application = validate_page_corrections(snapshot, response)

    if expected_code is None:
        assert application.warning_codes == ()
        assert application.spans[0].text == "개인정보"
        assert application.audit.outcome == "applied"
        assert application.audit.patches[0].corrected_text == "개인정보"
        assert application.audit.patches[0].rejected_text_hash is None
    else:
        assert application.warning_codes == (expected_code,)
        assert application.spans == snapshot.spans
        assert application.audit.outcome == "rejected"
        assert all(item.corrected_text is None for item in application.audit.patches)
        if application.audit.patches:
            assert (
                application.audit.patches[0].rejected_text_hash
                == hashlib.sha256(str(patch["corrected_text"]).encode("utf-8")).hexdigest()
            )


def test_incomplete_document_evidence_rejects_all_correction_before_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ard_ossie.semantic import correction

    payload = b"%PDF-1.7 fixture"
    source_hash = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "semantic.pdf"
    path.write_bytes(payload)
    source = SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=source_hash,
        size_bytes=len(payload),
        snapshot=payload,
    )

    def span(
        ordinal: int,
        text: str,
        bbox: SourceBox | None,
        *,
        page: int | None = 1,
    ) -> SourceSpan:
        return SourceSpan(
            span_id=make_span_id(source_hash, ordinal),
            ordinal=ordinal,
            page=page,
            bbox=bbox,
            text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest(),
        )

    native = NativeDocument(
        source_hash=source_hash,
        extraction_mode=ExtractionMode.OCR,
        page_count=1,
        parser_versions={"ocr": "fixture"},
        spans=(
            span(0, "개 인정보", SourceBox(left=0.1, bottom=0.2, right=0.8, top=0.3)),
            span(
                1,
                "페이지 근거 없음",
                SourceBox(left=0.1, bottom=0.4, right=0.8, top=0.5),
                page=None,
            ),
        ),
        groups=(),
        tables=(),
    )

    class NeverProvider:
        def capabilities(self) -> dict[str, object]:
            return {"provider": "openai_compatible", "model": "test", "vision": True}

        def generate_multimodal_structured(self, **_kwargs: object) -> object:
            raise AssertionError("provider must not be called for partial evidence")

    monkeypatch.setattr(correction, "render_pdf_page_images", lambda *_args, **_kwargs: (b"png",))

    application = correction.OcrCorrectionPlanner(NeverProvider()).correct(source, native)

    assert application.document == native
    assert application.warning_codes == ("SEMANTIC_OCR_CORRECTION_EVIDENCE_UNAVAILABLE",)
    assert application.audits == ()


@pytest.mark.parametrize("extraction_mode", [ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR])
def test_pdf_extraction_modes_receive_the_same_image_grounded_correction(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    extraction_mode: ExtractionMode,
) -> None:
    from ard_ossie.semantic import correction

    payload = b"%PDF-1.7 fixture"
    source_hash = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "semantic.pdf"
    path.write_bytes(payload)
    source = SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=source_hash,
        size_bytes=len(payload),
        snapshot=payload,
    )
    text = "개 인정보"
    span = SourceSpan(
        span_id=make_span_id(source_hash, 0),
        ordinal=0,
        page=1,
        bbox=SourceBox(left=0.1, bottom=0.2, right=0.8, top=0.3),
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    native = NativeDocument(
        source_hash=source_hash,
        extraction_mode=extraction_mode,
        page_count=1,
        parser_versions={"fixture": "1"},
        spans=(span,),
        groups=(),
        tables=(),
    )

    class CorrectingProvider:
        system_prompt = ""

        def capabilities(self) -> dict[str, object]:
            return {
                "provider": "openai_compatible",
                "model": "test-model",
                "vision": True,
            }

        def generate_multimodal_structured(self, **kwargs: object) -> LLMResult:
            messages = kwargs["messages"]
            assert isinstance(messages, list)
            system_part = messages[0].content[0]
            user_part = messages[1].content[0]
            assert isinstance(system_part, LLMTextPart)
            assert isinstance(user_part, LLMTextPart)
            self.system_prompt = system_part.text
            request = json.loads(user_part.text)
            return LLMResult(
                text="",
                structured={
                    "request_hash": request["request_hash"],
                    "patches": [
                        {
                            "span_id": span.span_id,
                            "original_text_hash": span.text_hash,
                            "corrected_text": "개인정보",
                            "correction_kind": "spacing",
                            "bbox": span.bbox.model_dump(mode="json"),
                            "confidence": 0.99,
                        }
                    ],
                },
                metadata=LLMMetadata(
                    profile="test-profile",
                    provider="openai_compatible",
                    model="test-model",
                    elapsed_ms=1,
                ),
            )

    provider = CorrectingProvider()
    monkeypatch.setattr(correction, "render_pdf_page_images", lambda *_args, **_kwargs: (b"png",))

    application = correction.OcrCorrectionPlanner(provider).correct(source, native)

    assert application.document.spans[0].text == "개인정보"
    assert application.warning_codes == ()
    assert application.audits[0].outcome == "applied"
    assert "extracted PDF span catalog" in provider.system_prompt
    assert "OCR span catalog" not in provider.system_prompt


def test_docx_extraction_skips_visual_correction() -> None:
    from ard_ossie.semantic import correction

    native = NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.DOCX_XML,
        page_count=0,
        parser_versions={},
        spans=(_span(),),
        groups=(),
        tables=(),
    )

    class NeverProvider:
        def capabilities(self) -> dict[str, object]:
            raise AssertionError("DOCX must not request visual correction")

    application = correction.OcrCorrectionPlanner(NeverProvider()).correct(
        object(),  # type: ignore[arg-type]
        native,
    )

    assert application.document == native
    assert application.audits == ()
    assert application.warning_codes == ()


def test_provider_error_propagates_when_fail_fast_requested(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ard_ossie.semantic import correction

    payload = b"%PDF-1.7 fixture"
    source_hash = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "semantic.pdf"
    path.write_bytes(payload)
    source = SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=source_hash,
        size_bytes=len(payload),
        snapshot=payload,
    )
    text = "개 인정보"
    span = SourceSpan(
        span_id=make_span_id(source_hash, 0),
        ordinal=0,
        page=1,
        bbox=SourceBox(left=0.1, bottom=0.2, right=0.8, top=0.3),
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    native = NativeDocument(
        source_hash=source_hash,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        spans=(span,),
        groups=(),
        tables=(),
    )

    class FailingProvider:
        def capabilities(self) -> dict[str, object]:
            return {
                "provider": "openai_compatible",
                "model": "test-model",
                "vision": True,
            }

        def generate_multimodal_structured(self, **_kwargs: object) -> object:
            raise ProviderExecutionError(
                "LLM_PROVIDER_TIMEOUT",
                kind=ProviderFailureKind.TRANSIENT,
            )

    monkeypatch.setattr(correction, "render_pdf_page_images", lambda *_args, **_kwargs: (b"png",))

    with pytest.raises(ProviderExecutionError, match="LLM_PROVIDER_TIMEOUT") as captured:
        correction.OcrCorrectionPlanner(
            FailingProvider(),
            propagate_provider_errors=True,
        ).correct(source, native)

    assert captured.value.kind is ProviderFailureKind.TRANSIENT


def test_repair_prompt_binding_controls_trusted_reuse() -> None:
    from ard_ossie.semantic.correction import (
        _trusted_binding_matches,
        make_page_snapshot,
        validate_page_corrections,
    )

    span = _span()
    snapshot = make_page_snapshot(
        source_hash=SOURCE_HASH,
        page=1,
        page_image=b"\x89PNG\r\n\x1a\nfixture",
        spans=(span,),
        provider="openai_compatible",
        model="gpt-5.6-terra",
    )
    application = validate_page_corrections(
        snapshot,
        {
            "request_hash": snapshot.request_hash,
            "patches": [
                {
                    "span_id": span.span_id,
                    "original_text_hash": span.text_hash,
                    "corrected_text": "개인정보",
                    "correction_kind": "spacing",
                    "bbox": span.bbox.model_dump(mode="json"),
                    "confidence": 0.99,
                }
            ],
        },
        repair_count=1,
        repair_validation_codes=("LLM_INVALID_JSON",),
    )

    assert application.audit.repair_validation_codes == ["LLM_INVALID_JSON"]
    assert _trusted_binding_matches(snapshot, application.audit)
    assert not _trusted_binding_matches(
        snapshot,
        application.audit.model_copy(update={"effective_prompt_hash": "0" * 64}),
    )
