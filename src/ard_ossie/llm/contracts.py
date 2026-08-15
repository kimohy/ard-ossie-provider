from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from ard_ossie.models import StrictModel

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

ProviderName = Literal[
    "openai_compatible",
    "azure_openai",
    "vertex_gemini",
    "vertex_claude",
]


class ProviderFailureKind(StrEnum):
    CONFIGURATION = "configuration"
    TRANSIENT = "transient"
    OUTPUT = "output"


class ProviderExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        kind: ProviderFailureKind,
        rejected_result: object | None = None,
        retry_count: int = 0,
        repair_count: int = 0,
    ) -> None:
        if _ERROR_CODE.fullmatch(code) is None:
            raise ValueError("INVALID_PROVIDER_ERROR_CODE")
        if not 0 <= retry_count <= 2 or not 0 <= repair_count <= 2:
            raise ValueError("INVALID_PROVIDER_ATTEMPT_COUNT")
        super().__init__(code)
        self.code = code
        self.kind = kind
        self.rejected_result = rejected_result
        self.retry_count = retry_count
        self.repair_count = repair_count


class LLMMetadata(StrictModel):
    profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    provider: ProviderName
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$",
    )
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_:-]{1,64}$",
    )
    elapsed_ms: int = Field(ge=0)
    retry_count: int = Field(default=0, ge=0, le=2)
    repair_count: int = Field(default=0, ge=0, le=2)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in normalized for character in "\x00\r\n"):
            raise ValueError("invalid model")
        return normalized


class LLMResult(StrictModel):
    text: str = Field(repr=False)
    structured: dict[str, object] | None = Field(default=None, repr=False)
    metadata: LLMMetadata
    repair_validation_codes: list[str] = Field(default_factory=list, max_length=2, repr=False)

    @field_validator("repair_validation_codes")
    @classmethod
    def validate_repair_codes(cls, value: list[str]) -> list[str]:
        if any(_ERROR_CODE.fullmatch(code) is None for code in value):
            raise ValueError("invalid repair validation codes")
        return value


class LLMTextPart(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=200_000)


class LLMImagePart(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["image"] = "image"
    mime_type: Literal["image/png", "image/jpeg"]
    data: bytes = Field(min_length=1, max_length=8 * 1024 * 1024, repr=False)


class LLMMultimodalMessage(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: tuple[LLMTextPart | LLMImagePart, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_image_boundary(self) -> LLMMultimodalMessage:
        images = [part for part in self.content if isinstance(part, LLMImagePart)]
        if len(images) > 1:
            raise ValueError("LLM_MULTIMODAL_IMAGE_LIMIT_EXCEEDED")
        if images and self.role != "user":
            raise ValueError("LLM_MULTIMODAL_IMAGE_ROLE_INVALID")
        return self


class LLMProvider(Protocol):
    def health_check(self) -> bool: ...

    def capabilities(self) -> dict[str, JsonValue]: ...

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> LLMResult: ...

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult: ...

    def generate_multimodal_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[LLMMultimodalMessage],
    ) -> LLMResult: ...
