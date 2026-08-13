"""Bounded, image-grounded correction for authoritative PDF OCR spans."""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from ard_ossie.canonical import canonical_hash
from ard_ossie.ingestion import SourceFile
from ard_ossie.llm import (
    STRUCTURED_REPAIR_PROMPT_VERSION,
    LLMImagePart,
    LLMMultimodalMessage,
    LLMProvider,
    LLMResult,
    LLMTextPart,
    ProviderExecutionError,
    structured_repair_prompt_contract_hash,
)
from ard_ossie.semantic.models import (
    ExtractionMode,
    NativeDocument,
    OcrCorrectionPageAudit,
    OcrCorrectionPatchAudit,
    OcrCorrectionPlan,
    SemanticFidelityReport,
    SourceBox,
    SourceSpan,
)

OCR_CORRECTION_PROMPT_VERSION = "semantic-ocr-correction-v1"
OCR_RENDER_SCALE = 2.0
MAX_OCR_PAGES = 200
MAX_PAGE_PIXELS = 16_000_000
MAX_PAGE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PAGE_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PAGE_SPANS = 2_000
MAX_CORRECTED_GROWTH = 2
MAX_CHARACTER_CORRECTIONS = 4
MAX_CHARACTER_CORRECTION_RATIO = 0.15
MIN_CORRECTION_CONFIDENCE = 0.80

_PROMPT = (
    "Inspect the supplied page image and OCR span catalog. Return only sparse patches for "
    "character-recognition or spacing errors that are visibly supported by the image. Do not "
    "summarize, paraphrase, translate, add, delete, reorder, or restructure content. Omit spans "
    "that need no correction and copy each span ID, original hash, and bounding box exactly."
)
_RAW_HTML = re.compile(
    r"<(?:(?:/?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*?)?\s*/?)|(?:!--.*?--)|(?:![A-Z][^<>]*)|(?:\?.*?\?))>",
    re.DOTALL,
)
_SPAN_ID = re.compile(r"^span_[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SemanticOcrCorrectionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OcrPageSnapshot:
    source_hash: str
    page: int
    page_image: bytes = field(repr=False)
    page_image_hash: str
    spans: tuple[SourceSpan, ...]
    ocr_catalog_hash: str
    prompt_hash: str
    schema_hash: str
    repair_prompt_hash: str
    provider: str
    model: str
    request_hash: str


@dataclass(frozen=True)
class PageCorrectionValidation:
    spans: tuple[SourceSpan, ...]
    audit: OcrCorrectionPageAudit
    warning_codes: tuple[str, ...]


@dataclass(frozen=True)
class OcrCorrectionApplication:
    document: NativeDocument
    audits: tuple[OcrCorrectionPageAudit, ...]
    warning_codes: tuple[str, ...]


def semantic_ocr_correction_schema() -> dict[str, object]:
    return OcrCorrectionPlan.model_json_schema()


def make_page_snapshot(
    *,
    source_hash: str,
    page: int,
    page_image: bytes,
    spans: tuple[SourceSpan, ...],
    provider: str,
    model: str,
) -> OcrPageSnapshot:
    if page < 1 or len(spans) > MAX_PAGE_SPANS:
        raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_PAGE_LIMIT_EXCEEDED")
    if not page_image or len(page_image) > MAX_PAGE_IMAGE_BYTES:
        raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_IMAGE_LIMIT_EXCEEDED")
    if any(span.page != page or span.bbox is None for span in spans):
        raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_EVIDENCE_INVALID")
    schema = semantic_ocr_correction_schema()
    schema_hash = canonical_hash(schema)
    repair_prompt_hash = structured_repair_prompt_contract_hash(schema)
    prompt_hash = hashlib.sha256(_PROMPT.encode("utf-8")).hexdigest()
    page_image_hash = hashlib.sha256(page_image).hexdigest()
    ocr_catalog_hash = canonical_hash(
        [
            {
                "span_id": span.span_id,
                "text_hash": span.text_hash,
                "bbox": span.bbox.model_dump(mode="json") if span.bbox is not None else None,
            }
            for span in spans
        ]
    )
    request_hash = canonical_hash(
        {
            "source_hash": source_hash,
            "page": page,
            "page_image_hash": page_image_hash,
            "ocr_catalog_hash": ocr_catalog_hash,
            "prompt_version": OCR_CORRECTION_PROMPT_VERSION,
            "prompt_hash": prompt_hash,
            "schema_hash": schema_hash,
            "repair_prompt_version": STRUCTURED_REPAIR_PROMPT_VERSION,
            "repair_prompt_hash": repair_prompt_hash,
            "provider": provider,
            "model": model,
        }
    )
    return OcrPageSnapshot(
        source_hash=source_hash,
        page=page,
        page_image=page_image,
        page_image_hash=page_image_hash,
        spans=spans,
        ocr_catalog_hash=ocr_catalog_hash,
        prompt_hash=prompt_hash,
        schema_hash=schema_hash,
        repair_prompt_hash=repair_prompt_hash,
        provider=provider,
        model=model,
        request_hash=request_hash,
    )


def validate_page_corrections(
    snapshot: OcrPageSnapshot,
    response: object,
    *,
    outcome: Literal["applied", "reused"] = "applied",
    retry_count: int = 0,
    repair_count: int = 0,
    repair_validation_codes: tuple[str, ...] = (),
) -> PageCorrectionValidation:
    raw_patches = _raw_patches(response)
    code = _page_validation_code(snapshot, response, raw_patches)
    if code is not None:
        rejected = [_rejected_patch_audit(value, code) for value in raw_patches]
        return PageCorrectionValidation(
            spans=snapshot.spans,
            audit=_page_audit(
                snapshot,
                outcome="rejected",
                patches=rejected,
                retry_count=retry_count,
                repair_count=repair_count,
                repair_validation_codes=repair_validation_codes,
            ),
            warning_codes=(code,),
        )

    parsed = OcrCorrectionPlan.model_validate(response)
    catalog = {span.span_id: span for span in snapshot.spans}
    replacements: dict[str, SourceSpan] = {}
    audits: list[OcrCorrectionPatchAudit] = []
    for patch in parsed.patches:
        source_span = catalog[patch.span_id]
        corrected_hash = hashlib.sha256(patch.corrected_text.encode("utf-8")).hexdigest()
        replacements[patch.span_id] = source_span.model_copy(
            update={"text": patch.corrected_text, "text_hash": corrected_hash}
        )
        audits.append(
            OcrCorrectionPatchAudit(
                span_id=patch.span_id,
                original_text_hash=patch.original_text_hash,
                corrected_text=patch.corrected_text,
                corrected_text_hash=corrected_hash,
                bbox=patch.bbox,
                correction_kind=patch.correction_kind,
                confidence=patch.confidence,
                outcome=outcome,
            )
        )
    return PageCorrectionValidation(
        spans=tuple(replacements.get(span.span_id, span) for span in snapshot.spans),
        audit=_page_audit(
            snapshot,
            outcome=outcome,
            patches=audits,
            retry_count=retry_count,
            repair_count=repair_count,
            repair_validation_codes=repair_validation_codes,
        ),
        warning_codes=(),
    )


class OcrCorrectionPlanner:
    def __init__(self, provider: LLMProvider | None) -> None:
        self.provider = provider

    def correct(
        self,
        source: SourceFile,
        native: NativeDocument,
        *,
        trusted_fidelity: SemanticFidelityReport | None = None,
        pdfium: Any | None = None,
    ) -> OcrCorrectionApplication:
        if native.extraction_mode is not ExtractionMode.OCR:
            return OcrCorrectionApplication(native, (), ())
        page_images = render_pdf_page_images(source, pdfium=pdfium)
        if len(page_images) != native.page_count:
            raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_PAGE_COUNT_MISMATCH")
        catalog = native.span_catalog()
        audits: list[OcrCorrectionPageAudit] = []
        warnings: list[str] = []
        provider_identity = _provider_identity(self.provider)
        document_evidence_invalid = any(
            span.page is None or span.page < 1 or span.page > len(page_images)
            for span in native.spans
        )
        if document_evidence_invalid:
            return OcrCorrectionApplication(
                document=native,
                audits=(),
                warning_codes=("SEMANTIC_OCR_CORRECTION_EVIDENCE_UNAVAILABLE",),
            )

        for page, page_image in enumerate(page_images, start=1):
            all_page_spans = tuple(
                span
                for span in sorted(native.spans, key=lambda item: item.ordinal)
                if span.page == page
            )
            page_spans = tuple(span for span in all_page_spans if span.bbox is not None)
            if not page_spans or len(page_spans) != len(all_page_spans):
                provider_name, model = provider_identity or ("none", "none")
                snapshot = make_page_snapshot(
                    source_hash=native.source_hash,
                    page=page,
                    page_image=page_image,
                    spans=page_spans,
                    provider=provider_name,
                    model=model,
                )
                code = "SEMANTIC_OCR_CORRECTION_EVIDENCE_UNAVAILABLE"
                audits.append(
                    _page_audit(
                        snapshot,
                        outcome="unavailable",
                        provider_error_code=code,
                    )
                )
                warnings.append(code)
                continue

            trusted = _trusted_page(
                trusted_fidelity,
                source_hash=native.source_hash,
                page=page,
            )
            if trusted is not None:
                trusted_snapshot = make_page_snapshot(
                    source_hash=native.source_hash,
                    page=page,
                    page_image=page_image,
                    spans=page_spans,
                    provider=trusted.provider,
                    model=trusted.model,
                )
                if _trusted_binding_matches(trusted_snapshot, trusted):
                    reused = validate_page_corrections(
                        trusted_snapshot,
                        _trusted_response(trusted),
                        outcome="reused",
                        retry_count=trusted.retry_count,
                        repair_count=trusted.repair_count,
                        repair_validation_codes=tuple(trusted.repair_validation_codes),
                    )
                    if not reused.warning_codes:
                        for span in reused.spans:
                            catalog[span.span_id] = span
                        audits.append(reused.audit)
                        continue

            if provider_identity is None:
                snapshot = make_page_snapshot(
                    source_hash=native.source_hash,
                    page=page,
                    page_image=page_image,
                    spans=page_spans,
                    provider="none",
                    model="none",
                )
                code = "SEMANTIC_OCR_CORRECTION_UNAVAILABLE"
                audits.append(
                    _page_audit(
                        snapshot,
                        outcome="unavailable",
                        provider_error_code=code,
                    )
                )
                warnings.append(code)
                continue

            provider_name, model = provider_identity
            snapshot = make_page_snapshot(
                source_hash=native.source_hash,
                page=page,
                page_image=page_image,
                spans=page_spans,
                provider=provider_name,
                model=model,
            )
            try:
                result = self.provider.generate_multimodal_structured(
                    schema=semantic_ocr_correction_schema(),
                    messages=_correction_messages(snapshot),
                )
            except ProviderExecutionError as error:
                audits.append(
                    _page_audit(
                        snapshot,
                        outcome="provider_failed",
                        provider_error_code=error.code,
                    )
                )
                warnings.append(error.code)
                continue
            if not isinstance(result, LLMResult) or result.structured is None:
                code = "SEMANTIC_OCR_CORRECTION_OUTPUT_INVALID"
                audits.append(
                    _page_audit(
                        snapshot,
                        outcome="provider_failed",
                        provider_error_code=code,
                    )
                )
                warnings.append(code)
                continue
            if result.metadata.provider != provider_name or result.metadata.model != model:
                code = "SEMANTIC_OCR_CORRECTION_IDENTITY_MISMATCH"
                audits.append(
                    _page_audit(
                        snapshot,
                        outcome="provider_failed",
                        provider_error_code=code,
                        retry_count=result.metadata.retry_count,
                        repair_count=result.metadata.repair_count,
                        repair_validation_codes=tuple(result.repair_validation_codes),
                    )
                )
                warnings.append(code)
                continue
            validated = validate_page_corrections(
                snapshot,
                result.structured,
                retry_count=result.metadata.retry_count,
                repair_count=result.metadata.repair_count,
                repair_validation_codes=tuple(result.repair_validation_codes),
            )
            for span in validated.spans:
                catalog[span.span_id] = span
            audits.append(validated.audit)
            warnings.extend(validated.warning_codes)

        corrected = native.model_copy(
            update={
                "spans": tuple(catalog[span.span_id] for span in native.spans),
            }
        )
        return OcrCorrectionApplication(
            document=corrected,
            audits=tuple(audits),
            warning_codes=tuple(dict.fromkeys(warnings)),
        )


def render_pdf_page_images(
    source: SourceFile,
    *,
    pdfium: Any | None = None,
) -> tuple[bytes, ...]:
    if pdfium is None:
        import pypdfium2

        pdfium = pypdfium2
    document = None
    try:
        document = pdfium.PdfDocument(source.snapshot)
        page_count = len(document)
        if page_count < 1 or page_count > MAX_OCR_PAGES:
            raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_PAGE_LIMIT_EXCEEDED")
        images: list[bytes] = []
        total_encoded_bytes = 0
        for page_index in range(page_count):
            page = document[page_index]
            bitmap = None
            image = None
            try:
                bitmap = page.render(scale=OCR_RENDER_SCALE)
                image = bitmap.to_pil()
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PAGE_PIXELS:
                    raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_PIXEL_LIMIT_EXCEEDED")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=False, compress_level=9)
                encoded = buffer.getvalue()
                if len(encoded) > MAX_PAGE_IMAGE_BYTES:
                    raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_IMAGE_LIMIT_EXCEEDED")
                total_encoded_bytes += len(encoded)
                if total_encoded_bytes > MAX_TOTAL_PAGE_IMAGE_BYTES:
                    raise SemanticOcrCorrectionError(
                        "SEMANTIC_OCR_CORRECTION_TOTAL_IMAGE_LIMIT_EXCEEDED"
                    )
                images.append(encoded)
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
        return tuple(images)
    except SemanticOcrCorrectionError:
        raise
    except Exception as error:
        raise SemanticOcrCorrectionError("SEMANTIC_OCR_CORRECTION_IMAGE_UNREADABLE") from error
    finally:
        if document is not None:
            document.close()


def has_raw_html(value: str) -> bool:
    return _RAW_HTML.search(value) is not None


def _raw_patches(response: object) -> list[object]:
    if not isinstance(response, dict):
        return []
    patches = response.get("patches")
    return list(patches) if isinstance(patches, list) else []


def _page_validation_code(
    snapshot: OcrPageSnapshot,
    response: object,
    raw_patches: list[object],
) -> str | None:
    if not isinstance(response, dict) or set(response) != {"request_hash", "patches"}:
        return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
    if response.get("request_hash") != snapshot.request_hash:
        return "SEMANTIC_OCR_CORRECTION_REQUEST_MISMATCH"
    if not isinstance(response.get("patches"), list) or len(raw_patches) > MAX_PAGE_SPANS:
        return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
    raw_ids = [patch.get("span_id") for patch in raw_patches if isinstance(patch, dict)]
    if len(raw_ids) != len(raw_patches):
        return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
    if len(raw_ids) != len(set(raw_ids)):
        return "SEMANTIC_OCR_CORRECTION_DUPLICATE_SPAN"

    catalog = {span.span_id: span for span in snapshot.spans}
    expected_keys = {
        "span_id",
        "original_text_hash",
        "corrected_text",
        "correction_kind",
        "bbox",
        "confidence",
    }
    for raw_patch in raw_patches:
        if not isinstance(raw_patch, dict) or set(raw_patch) != expected_keys:
            return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
        span_id = raw_patch.get("span_id")
        if not isinstance(span_id, str) or span_id not in catalog:
            return "SEMANTIC_OCR_CORRECTION_UNKNOWN_SPAN"
        source_span = catalog[span_id]
        if raw_patch.get("original_text_hash") != source_span.text_hash:
            return "SEMANTIC_OCR_CORRECTION_HASH_MISMATCH"
        try:
            bbox = SourceBox.model_validate(raw_patch.get("bbox"))
        except ValidationError:
            return "SEMANTIC_OCR_CORRECTION_BOX_MISMATCH"
        if bbox != source_span.bbox:
            return "SEMANTIC_OCR_CORRECTION_BOX_MISMATCH"
        corrected = raw_patch.get("corrected_text")
        if not isinstance(corrected, str):
            return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
        if not corrected:
            return "SEMANTIC_OCR_CORRECTION_TEXT_EMPTY"
        if len(corrected) > len(source_span.text) * MAX_CORRECTED_GROWTH:
            return "SEMANTIC_OCR_CORRECTION_TEXT_GROWTH_EXCEEDED"
        if corrected.count("\n") != source_span.text.count("\n"):
            return "SEMANTIC_OCR_CORRECTION_LINE_BLOCK_CHANGE"
        correction_kind = raw_patch.get("correction_kind")
        if correction_kind not in {"character_recognition", "spacing"}:
            return "SEMANTIC_OCR_CORRECTION_KIND_INVALID"
        if has_raw_html(corrected) or any(
            unicodedata.category(character) == "Cc" and character != "\n" for character in corrected
        ):
            return "SEMANTIC_OCR_CORRECTION_TEXT_UNSAFE"
        text_code = _correction_text_code(
            source_span.text,
            corrected,
            correction_kind=correction_kind,
        )
        if text_code is not None:
            return text_code
        confidence = raw_patch.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
        ):
            return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
        if confidence < MIN_CORRECTION_CONFIDENCE:
            return "SEMANTIC_OCR_CORRECTION_CONFIDENCE_LOW"
    try:
        OcrCorrectionPlan.model_validate(response)
    except ValidationError:
        return "SEMANTIC_OCR_CORRECTION_SCHEMA_INVALID"
    return None


def _correction_text_code(
    source: str,
    corrected: str,
    *,
    correction_kind: str,
) -> str | None:
    if correction_kind == "spacing":
        if source == corrected or _without_whitespace(source) != _without_whitespace(corrected):
            return "SEMANTIC_OCR_CORRECTION_SPACING_INVALID"
        return None

    if len(source) != len(corrected) or any(
        source_character.isspace() != corrected_character.isspace()
        or (source_character.isspace() and source_character != corrected_character)
        for source_character, corrected_character in zip(source, corrected, strict=True)
    ):
        return "SEMANTIC_OCR_CORRECTION_RECOGNITION_INVALID"
    changed = sum(
        source_character != corrected_character
        for source_character, corrected_character in zip(source, corrected, strict=True)
    )
    non_whitespace_count = sum(not character.isspace() for character in source)
    allowed = max(
        1,
        min(
            MAX_CHARACTER_CORRECTIONS,
            int(non_whitespace_count * MAX_CHARACTER_CORRECTION_RATIO),
        ),
    )
    if changed == 0 or changed > allowed:
        return "SEMANTIC_OCR_CORRECTION_RECOGNITION_INVALID"
    return None


def _without_whitespace(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _rejected_patch_audit(
    raw_patch: object,
    code: str,
) -> OcrCorrectionPatchAudit:
    value = raw_patch if isinstance(raw_patch, dict) else {}
    span_id = value.get("span_id")
    original_hash = value.get("original_text_hash")
    corrected = value.get("corrected_text")
    bbox: SourceBox | None = None
    with suppress(ValidationError):
        bbox = SourceBox.model_validate(value.get("bbox"))
    kind = value.get("correction_kind")
    confidence = value.get("confidence")
    return OcrCorrectionPatchAudit(
        span_id=(
            span_id
            if isinstance(span_id, str) and _SPAN_ID.fullmatch(span_id)
            else "span_0000000000000000"
        ),
        original_text_hash=(
            original_hash
            if isinstance(original_hash, str) and _SHA256.fullmatch(original_hash)
            else "0" * 64
        ),
        rejected_text_hash=hashlib.sha256(str(corrected).encode("utf-8")).hexdigest(),
        bbox=bbox,
        correction_kind=(kind if kind in {"character_recognition", "spacing"} else None),
        confidence=(
            float(confidence)
            if isinstance(confidence, int | float)
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
            else None
        ),
        outcome="rejected",
        validation_code=code if _ERROR_CODE.fullmatch(code) else "SEMANTIC_OCR_CORRECTION_REJECTED",
    )


def _page_audit(
    snapshot: OcrPageSnapshot,
    *,
    outcome: Literal["applied", "reused", "rejected", "provider_failed", "unavailable"],
    patches: list[OcrCorrectionPatchAudit] | None = None,
    provider_error_code: str | None = None,
    retry_count: int = 0,
    repair_count: int = 0,
    repair_validation_codes: tuple[str, ...] = (),
) -> OcrCorrectionPageAudit:
    return OcrCorrectionPageAudit(
        source_hash=snapshot.source_hash,
        page=snapshot.page,
        page_image_hash=snapshot.page_image_hash,
        ocr_catalog_hash=snapshot.ocr_catalog_hash,
        request_hash=snapshot.request_hash,
        prompt_version=OCR_CORRECTION_PROMPT_VERSION,
        prompt_hash=snapshot.prompt_hash,
        schema_hash=snapshot.schema_hash,
        repair_prompt_version=STRUCTURED_REPAIR_PROMPT_VERSION,
        repair_prompt_hash=snapshot.repair_prompt_hash,
        effective_prompt_hash=_effective_prompt_hash(
            snapshot,
            repair_validation_codes,
        ),
        repair_validation_codes=list(repair_validation_codes),
        provider=snapshot.provider,
        model=snapshot.model,
        outcome=outcome,
        patches=patches or [],
        provider_error_code=provider_error_code,
        retry_count=retry_count,
        repair_count=repair_count,
    )


def _provider_identity(provider: LLMProvider | None) -> tuple[str, str] | None:
    if provider is None:
        return None
    try:
        capabilities = provider.capabilities()
    except Exception:
        return None
    provider_name = capabilities.get("provider")
    model = capabilities.get("model")
    if capabilities.get("vision") is not True:
        return None
    if not isinstance(provider_name, str) or not provider_name:
        return None
    if not isinstance(model, str) or not model:
        return None
    return provider_name, model


def _correction_messages(snapshot: OcrPageSnapshot) -> list[LLMMultimodalMessage]:
    request = {
        "request_hash": snapshot.request_hash,
        "source_hash": snapshot.source_hash,
        "page": snapshot.page,
        "page_image_hash": snapshot.page_image_hash,
        "spans": [
            {
                "span_id": span.span_id,
                "text": span.text,
                "original_text_hash": span.text_hash,
                "bbox": span.bbox.model_dump(mode="json") if span.bbox is not None else None,
            }
            for span in snapshot.spans
        ],
    }
    return [
        LLMMultimodalMessage(
            role="system",
            content=(LLMTextPart(text=_PROMPT),),
        ),
        LLMMultimodalMessage(
            role="user",
            content=(
                LLMTextPart(
                    text=json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
                LLMImagePart(mime_type="image/png", data=snapshot.page_image),
            ),
        ),
    ]


def _trusted_page(
    report: SemanticFidelityReport | None,
    *,
    source_hash: str,
    page: int,
) -> OcrCorrectionPageAudit | None:
    if report is None or report.source_hash != source_hash:
        return None
    return next(
        (
            item
            for item in report.ocr_corrections
            if item.page == page and item.outcome in {"applied", "reused"}
        ),
        None,
    )


def _trusted_binding_matches(
    snapshot: OcrPageSnapshot,
    audit: OcrCorrectionPageAudit,
) -> bool:
    return (
        audit.source_hash == snapshot.source_hash
        and audit.page == snapshot.page
        and audit.page_image_hash == snapshot.page_image_hash
        and audit.ocr_catalog_hash == snapshot.ocr_catalog_hash
        and audit.request_hash == snapshot.request_hash
        and audit.prompt_version == OCR_CORRECTION_PROMPT_VERSION
        and audit.prompt_hash == snapshot.prompt_hash
        and audit.schema_hash == snapshot.schema_hash
        and audit.repair_prompt_version == STRUCTURED_REPAIR_PROMPT_VERSION
        and audit.repair_prompt_hash == snapshot.repair_prompt_hash
        and audit.effective_prompt_hash
        == _effective_prompt_hash(snapshot, tuple(audit.repair_validation_codes))
        and audit.provider == snapshot.provider
        and audit.model == snapshot.model
    )


def _effective_prompt_hash(
    snapshot: OcrPageSnapshot,
    repair_validation_codes: tuple[str, ...],
) -> str:
    return canonical_hash(
        {
            "prompt_hash": snapshot.prompt_hash,
            "repair_prompt_version": STRUCTURED_REPAIR_PROMPT_VERSION,
            "repair_prompt_hash": snapshot.repair_prompt_hash,
            "repair_validation_codes": list(repair_validation_codes),
        }
    )


def _trusted_response(audit: OcrCorrectionPageAudit) -> dict[str, object]:
    return {
        "request_hash": audit.request_hash,
        "patches": [
            {
                "span_id": patch.span_id,
                "original_text_hash": patch.original_text_hash,
                "corrected_text": patch.corrected_text,
                "correction_kind": patch.correction_kind,
                "bbox": patch.bbox.model_dump(mode="json") if patch.bbox is not None else None,
                "confidence": patch.confidence,
            }
            for patch in audit.patches
            if patch.outcome in {"applied", "reused"}
        ],
    }
