from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import JsonValue, SecretStr

from ard_ossie.llm.contracts import (
    LLMImagePart,
    LLMMetadata,
    LLMMultimodalMessage,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
    ProviderName,
)

APIStyle = Literal["chat_completions", "responses"]
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_FINISH_REASON = re.compile(r"^[a-z0-9_:-]{1,64}$")
_SAFE_PROFILE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_MODEL = re.compile(r"^[^\x00\r\n]{1,200}$")
_LOGGER = logging.getLogger(__name__)
_QUOTA_CODES = frozenset(
    {
        "credit_balance_exhausted",
        "insufficient_quota",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "project_spend_limit_exceeded",
    }
)


class OpenAICompatibleProvider:
    provider_name: ProviderName = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        profile: str = "openai-compatible-legacy",
        api: APIStyle = "chat_completions",
        client: Any | None = None,
        timeout_seconds: int = 120,
        max_output_tokens: int | None = 4096,
        temperature: float = 0,
        vision: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.profile = profile
        self.api = api
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.vision = vision
        self._clock = clock
        if api not in ("chat_completions", "responses"):
            raise ProviderExecutionError(
                "LLM_API_STYLE_UNSUPPORTED",
                kind=ProviderFailureKind.CONFIGURATION,
            )
        if client is not None:
            self._client = client
        else:
            try:
                self._client = _new_openai_client(
                    base_url=self.base_url,
                    api_key=api_key.get_secret_value(),
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                raise ProviderExecutionError(
                    "LLM_PROVIDER_CONFIGURATION_FAILED",
                    kind=ProviderFailureKind.CONFIGURATION,
                ) from None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, profile={self.profile!r}, "
            f"api={self.api!r}, timeout_seconds={self.timeout_seconds})"
        )

    def health_check(self) -> bool:
        try:
            self._client.models.list()
        except Exception as error:
            raise _classify_provider_error(error) from None
        return True

    def capabilities(self) -> dict[str, JsonValue]:
        return {
            "api_style": self.api,
            "structured_output": "json_schema",
            "provider": self.provider_name,
            "model": self.model,
            "vision": self.vision,
        }

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> LLMResult:
        return self._generate(messages=messages, schema=None)

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        result = self._generate(messages=messages, schema=schema)
        structured = _parse_structured(result.text, schema, rejected_result=result)
        return result.model_copy(update={"structured": structured})

    def generate_multimodal_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[LLMMultimodalMessage],
    ) -> LLMResult:
        result = self._generate(
            messages=_openai_multimodal_messages(messages, api=self.api),
            schema=schema,
        )
        structured = _parse_structured(result.text, schema, rejected_result=result)
        return result.model_copy(update={"structured": structured})

    def _generate(
        self,
        *,
        messages: list[dict[str, object]],
        schema: dict[str, object] | None,
    ) -> LLMResult:
        started = self._clock()
        try:
            if self.api == "responses":
                response = self._responses_request(messages=messages, schema=schema)
                content = getattr(response, "output_text", None)
                incomplete_details = getattr(response, "incomplete_details", None)
                finish_reason = getattr(incomplete_details, "reason", None) or getattr(
                    response, "status", None
                )
                refusal = _responses_has_refusal(response)
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", None)
                output_tokens = getattr(usage, "output_tokens", None)
            else:
                response = self._chat_request(messages=messages, schema=schema)
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "prompt_tokens", None)
                output_tokens = getattr(usage, "completion_tokens", None)
                try:
                    choice = response.choices[0]
                    content = choice.message.content
                except (AttributeError, IndexError, TypeError):
                    raise self._output_error(
                        code="LLM_RESPONSE_CHOICES_MISSING",
                        response=response,
                        finish_reason=None,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ) from None
                finish_reason = getattr(choice, "finish_reason", None)
                refusal = getattr(choice.message, "refusal", None)
        except Exception as error:
            raise _classify_provider_error(error) from None
        if not isinstance(content, str) or not content:
            raise self._output_error(
                code=_empty_output_code(
                    finish_reason=finish_reason,
                    refused=bool(refusal),
                ),
                response=response,
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        elapsed_ms = max(0, round((self._clock() - started) * 1000))
        metadata = LLMMetadata(
            profile=self.profile,
            provider=self.provider_name,
            model=self.model,
            request_id=_safe_value(getattr(response, "id", None), _SAFE_REQUEST_ID),
            input_tokens=_safe_count(input_tokens),
            output_tokens=_safe_count(output_tokens),
            finish_reason=_safe_value(finish_reason, _SAFE_FINISH_REASON),
            elapsed_ms=elapsed_ms,
        )
        return LLMResult(text=content, metadata=metadata)

    def _output_error(
        self,
        *,
        code: str,
        response: object,
        finish_reason: object,
        input_tokens: object,
        output_tokens: object,
    ) -> ProviderExecutionError:
        _LOGGER.error(
            "LLM output rejected: code=%s profile=%s provider=%s model=%s "
            "request_id=%s finish_reason=%s input_tokens=%s output_tokens=%s",
            code,
            _safe_value(self.profile, _SAFE_PROFILE),
            self.provider_name,
            _safe_value(self.model, _SAFE_MODEL),
            _safe_value(getattr(response, "id", None), _SAFE_REQUEST_ID),
            _safe_value(finish_reason, _SAFE_FINISH_REASON),
            _safe_count(input_tokens),
            _safe_count(output_tokens),
        )
        return ProviderExecutionError(code, kind=ProviderFailureKind.OUTPUT)

    def _chat_request(
        self,
        *,
        messages: list[dict[str, object]],
        schema: dict[str, object] | None,
    ) -> object:
        request: dict[str, object] = {
            "model": self.model,
            "messages": messages,
        }
        if self.max_output_tokens is not None:
            request["max_completion_tokens"] = self.max_output_tokens
        if self.temperature:
            request["temperature"] = self.temperature
        if schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ard_semantic_extraction",
                    "strict": True,
                    "schema": schema,
                },
            }
        return self._client.chat.completions.create(**request)

    def _responses_request(
        self,
        *,
        messages: list[dict[str, object]],
        schema: dict[str, object] | None,
    ) -> object:
        request: dict[str, object] = {
            "model": self.model,
            "input": messages,
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if self.temperature:
            request["temperature"] = self.temperature
        if schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "ard_semantic_extraction",
                    "strict": True,
                    "schema": schema,
                }
            }
        return self._client.responses.create(**request)


class AzureOpenAIProvider(OpenAICompatibleProvider):
    provider_name: ProviderName = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: SecretStr,
        deployment: str,
        profile: str,
        api: APIStyle = "chat_completions",
        timeout_seconds: int = 120,
        max_output_tokens: int = 4096,
        temperature: float = 0,
        vision: bool = False,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        base_url = f"{endpoint.rstrip('/')}/openai/v1"
        if client is None:
            try:
                client = _new_openai_client(
                    base_url=base_url,
                    api_key=api_key.get_secret_value(),
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                raise ProviderExecutionError(
                    "LLM_PROVIDER_CONFIGURATION_FAILED",
                    kind=ProviderFailureKind.CONFIGURATION,
                ) from None
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=deployment,
            profile=profile,
            api=api,
            client=client,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            vision=vision,
            clock=clock,
        )


def _openai_multimodal_messages(
    messages: list[LLMMultimodalMessage],
    *,
    api: APIStyle,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for message in messages:
        content: list[dict[str, object]] = []
        for part in message.content:
            if isinstance(part, LLMImagePart):
                encoded = base64.b64encode(part.data).decode("ascii")
                data_url = f"data:{part.mime_type};base64,{encoded}"
                content.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                    if api == "chat_completions"
                    else {"type": "input_image", "image_url": data_url}
                )
            else:
                content.append(
                    {
                        "type": "text" if api == "chat_completions" else "input_text",
                        "text": part.text,
                    }
                )
        output.append({"role": message.role, "content": content})
    return output


def _parse_structured(
    content: str,
    schema: dict[str, object],
    *,
    rejected_result: LLMResult,
) -> dict[str, object]:
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise ProviderExecutionError(
            "LLM_INVALID_JSON",
            kind=ProviderFailureKind.OUTPUT,
            rejected_result=rejected_result,
        ) from None
    if not isinstance(result, dict):
        raise ProviderExecutionError(
            "LLM_RESPONSE_NOT_OBJECT",
            kind=ProviderFailureKind.OUTPUT,
            rejected_result=rejected_result,
        )
    try:
        validate(instance=result, schema=schema)
    except JsonSchemaValidationError:
        raise ProviderExecutionError(
            "LLM_SCHEMA_VIOLATION",
            kind=ProviderFailureKind.OUTPUT,
            rejected_result=rejected_result,
        ) from None
    return result


def _new_openai_client(*, base_url: str, api_key: str, timeout_seconds: int) -> Any:
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)


def _safe_value(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _safe_count(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _empty_output_code(*, finish_reason: object, refused: bool) -> str:
    if refused:
        return "LLM_RESPONSE_REFUSED"
    if finish_reason in {"length", "max_output_tokens"}:
        return "LLM_OUTPUT_TOKEN_LIMIT_EXCEEDED"
    if finish_reason == "content_filter":
        return "LLM_RESPONSE_FILTERED"
    return "LLM_EMPTY_RESPONSE"


def _responses_has_refusal(response: object) -> bool:
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return False
    for item in output:
        if getattr(item, "type", None) == "refusal":
            return True
        content = getattr(item, "content", None)
        if isinstance(content, list) and any(
            getattr(part, "type", None) == "refusal" for part in content
        ):
            return True
    return False


def _classify_provider_error(error: Exception) -> ProviderExecutionError:
    if isinstance(error, ProviderExecutionError):
        return error
    if isinstance(error, AuthenticationError):
        return ProviderExecutionError(
            "LLM_PROVIDER_AUTHENTICATION_FAILED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, PermissionDeniedError):
        return ProviderExecutionError(
            "LLM_PROVIDER_PERMISSION_DENIED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, NotFoundError):
        return ProviderExecutionError(
            "LLM_PROVIDER_MODEL_NOT_FOUND",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, (BadRequestError, UnprocessableEntityError)):
        return ProviderExecutionError(
            "LLM_PROVIDER_REQUEST_REJECTED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, RateLimitError):
        if _api_error_code(error) in _QUOTA_CODES:
            return ProviderExecutionError(
                "LLM_PROVIDER_QUOTA_EXHAUSTED",
                kind=ProviderFailureKind.CONFIGURATION,
            )
        return ProviderExecutionError(
            "LLM_PROVIDER_RATE_LIMITED",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APITimeoutError):
        return ProviderExecutionError(
            "LLM_PROVIDER_TIMEOUT",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APIConnectionError):
        return ProviderExecutionError(
            "LLM_PROVIDER_CONNECTION_FAILED",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, InternalServerError):
        return ProviderExecutionError(
            "LLM_PROVIDER_SERVER_ERROR",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, ConflictError):
        return ProviderExecutionError(
            "LLM_PROVIDER_CONFLICT",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status == 408:
            return ProviderExecutionError(
                "LLM_PROVIDER_TIMEOUT",
                kind=ProviderFailureKind.TRANSIENT,
            )
        if status >= 500:
            return ProviderExecutionError(
                "LLM_PROVIDER_SERVER_ERROR",
                kind=ProviderFailureKind.TRANSIENT,
            )
        return ProviderExecutionError(
            "LLM_PROVIDER_REQUEST_REJECTED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, APIError):
        return ProviderExecutionError(
            "LLM_PROVIDER_FAILURE",
            kind=ProviderFailureKind.TRANSIENT,
        )
    return ProviderExecutionError(
        "LLM_PROVIDER_FAILURE",
        kind=ProviderFailureKind.TRANSIENT,
    )


def _api_error_code(error: APIStatusError) -> str | None:
    body = error.body
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    source = nested if isinstance(nested, Mapping) else body
    code = source.get("code")
    return code if isinstance(code, str) else None
