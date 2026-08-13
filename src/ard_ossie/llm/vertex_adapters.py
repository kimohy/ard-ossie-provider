from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import types
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from pydantic import JsonValue, SecretStr

from ard_ossie.llm.contracts import (
    LLMMetadata,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
    ProviderName,
)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_FINISH_REASON = re.compile(r"^[a-z0-9_:-]{1,64}$")


class VertexGeminiProvider:
    provider_name: ProviderName = "vertex_gemini"

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        credentials_json: SecretStr,
        profile: str,
        timeout_seconds: int = 120,
        max_output_tokens: int = 4096,
        temperature: float = 0,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.project = project
        self.location = location
        self.model = model
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._clock = clock
        if client is not None:
            self._client = client
            return
        credentials = _service_account_credentials(credentials_json)
        try:
            self._client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                credentials=credentials,
                http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
            )
        except Exception:
            raise _configuration_error() from None

    def __repr__(self) -> str:
        return (
            f"VertexGeminiProvider(project={self.project!r}, location={self.location!r}, "
            f"model={self.model!r}, profile={self.profile!r})"
        )

    def health_check(self) -> bool:
        self.generate_text(messages=[{"role": "user", "content": "Reply OK."}])
        return True

    def capabilities(self) -> dict[str, JsonValue]:
        return {"api_style": "generate_content", "structured_output": "json_schema"}

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

    def _generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, object] | None,
    ) -> LLMResult:
        system, contents = _gemini_messages(messages)
        config_values: dict[str, object] = {
            "system_instruction": system,
            "max_output_tokens": self.max_output_tokens,
        }
        if self.temperature:
            config_values["temperature"] = self.temperature
        if schema is not None:
            config_values["response_mime_type"] = "application/json"
            config_values["response_json_schema"] = schema
        started = self._clock()
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**config_values),
            )
        except Exception as error:
            raise _classify_vertex_error(error) from None
        content = getattr(response, "text", None)
        if not isinstance(content, str) or not content:
            raise _output_error("LLM_EMPTY_RESPONSE")
        candidates = getattr(response, "candidates", None)
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        usage = getattr(response, "usage_metadata", None)
        return LLMResult(
            text=content,
            metadata=LLMMetadata(
                profile=self.profile,
                provider=self.provider_name,
                model=self.model,
                request_id=_safe_value(
                    getattr(response, "response_id", None),
                    _SAFE_REQUEST_ID,
                ),
                input_tokens=_safe_count(getattr(usage, "prompt_token_count", None)),
                output_tokens=_safe_count(getattr(usage, "candidates_token_count", None)),
                finish_reason=_safe_finish_reason(getattr(candidate, "finish_reason", None)),
                elapsed_ms=_elapsed_ms(started, self._clock),
            ),
        )


class VertexClaudeProvider:
    provider_name: ProviderName = "vertex_claude"

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        credentials_json: SecretStr,
        profile: str,
        timeout_seconds: int = 120,
        max_output_tokens: int = 4096,
        temperature: float = 0,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.project = project
        self.location = location
        self.model = model
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._clock = clock
        if client is not None:
            self._client = client
            return
        credentials = _service_account_credentials(credentials_json)
        try:
            from anthropic import AnthropicVertex

            self._client = AnthropicVertex(
                project_id=project,
                region=location,
                credentials=credentials,
                timeout=timeout_seconds,
                max_retries=0,
            )
        except Exception:
            raise _configuration_error() from None

    def __repr__(self) -> str:
        return (
            f"VertexClaudeProvider(project={self.project!r}, location={self.location!r}, "
            f"model={self.model!r}, profile={self.profile!r})"
        )

    def health_check(self) -> bool:
        self.generate_text(messages=[{"role": "user", "content": "Reply OK."}])
        return True

    def capabilities(self) -> dict[str, JsonValue]:
        return {"api_style": "messages", "structured_output": "prompt_json"}

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

    def _generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, object] | None,
    ) -> LLMResult:
        system, request_messages = _claude_messages(messages)
        if schema is not None:
            schema_instruction = (
                "Return only one JSON object that validates against this JSON Schema: "
                f"{json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            )
            system = "\n\n".join(part for part in (system, schema_instruction) if part)
        request: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": request_messages,
        }
        if system:
            request["system"] = system
        if self.temperature:
            request["temperature"] = self.temperature
        started = self._clock()
        try:
            response = self._client.messages.create(**request)
        except Exception as error:
            raise _classify_vertex_error(error) from None
        content = _claude_text(getattr(response, "content", None))
        if not content:
            raise _output_error("LLM_EMPTY_RESPONSE")
        usage = getattr(response, "usage", None)
        return LLMResult(
            text=content,
            metadata=LLMMetadata(
                profile=self.profile,
                provider=self.provider_name,
                model=self.model,
                request_id=_safe_value(getattr(response, "id", None), _SAFE_REQUEST_ID),
                input_tokens=_safe_count(getattr(usage, "input_tokens", None)),
                output_tokens=_safe_count(getattr(usage, "output_tokens", None)),
                finish_reason=_safe_finish_reason(getattr(response, "stop_reason", None)),
                elapsed_ms=_elapsed_ms(started, self._clock),
            ),
        )


def _service_account_credentials(raw: SecretStr) -> object:
    try:
        from google.oauth2 import service_account

        payload = json.loads(raw.get_secret_value())
        if not isinstance(payload, dict):
            raise TypeError
        return service_account.Credentials.from_service_account_info(
            payload,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception:
        raise _configuration_error() from None


def _gemini_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[types.Content]]:
    system_parts: list[str] = []
    contents: list[types.Content] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            raise _configuration_error()
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            contents.append(
                types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part.from_text(text=content)],
                )
            )
        else:
            raise _configuration_error()
    return ("\n\n".join(system_parts) or None, contents)


def _claude_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    system_parts: list[str] = []
    output: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            raise _configuration_error()
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            output.append({"role": role, "content": content})
        else:
            raise _configuration_error()
    return ("\n\n".join(system_parts) or None, output)


def _claude_text(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    parts = [
        item.text
        for item in content
        if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str)
    ]
    return "".join(parts) or None


def _parse_structured(
    content: str,
    schema: dict[str, object],
    *,
    rejected_result: LLMResult,
) -> dict[str, object]:
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise _output_error("LLM_INVALID_JSON", rejected_result) from None
    if not isinstance(result, dict):
        raise _output_error("LLM_RESPONSE_NOT_OBJECT", rejected_result)
    try:
        validate(instance=result, schema=schema)
    except JsonSchemaValidationError:
        raise _output_error("LLM_SCHEMA_VIOLATION", rejected_result) from None
    return result


def _classify_vertex_error(error: Exception) -> ProviderExecutionError:
    if isinstance(error, ProviderExecutionError):
        return error
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        genai_code = getattr(error, "code", None)
        status = genai_code if isinstance(genai_code, int) else None
    if status == 401:
        return _configuration_error("LLM_PROVIDER_AUTHENTICATION_FAILED")
    if status == 403:
        return _configuration_error("LLM_PROVIDER_PERMISSION_DENIED")
    if status == 404:
        return _configuration_error("LLM_PROVIDER_MODEL_NOT_FOUND")
    if status == 400 or status == 422:
        return _configuration_error("LLM_PROVIDER_REQUEST_REJECTED")
    if status == 408:
        return _transient_error("LLM_PROVIDER_TIMEOUT")
    if status == 409:
        return _transient_error("LLM_PROVIDER_CONFLICT")
    if status == 429:
        return _transient_error("LLM_PROVIDER_RATE_LIMITED")
    if isinstance(status, int) and status >= 500:
        return _transient_error("LLM_PROVIDER_SERVER_ERROR")
    if isinstance(error, TimeoutError):
        return _transient_error("LLM_PROVIDER_TIMEOUT")
    if isinstance(error, OSError):
        return _transient_error("LLM_PROVIDER_CONNECTION_FAILED")
    return _transient_error("LLM_PROVIDER_FAILURE")


def _configuration_error(
    code: str = "LLM_PROVIDER_CONFIGURATION_FAILED",
) -> ProviderExecutionError:
    return ProviderExecutionError(code, kind=ProviderFailureKind.CONFIGURATION)


def _transient_error(code: str) -> ProviderExecutionError:
    return ProviderExecutionError(code, kind=ProviderFailureKind.TRANSIENT)


def _output_error(
    code: str,
    rejected_result: LLMResult | None = None,
) -> ProviderExecutionError:
    return ProviderExecutionError(
        code,
        kind=ProviderFailureKind.OUTPUT,
        rejected_result=rejected_result,
    )


def _safe_value(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _safe_finish_reason(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).lower()
    if normalized.startswith("finishreason."):
        normalized = normalized.partition(".")[2]
    return normalized if _SAFE_FINISH_REASON.fullmatch(normalized) else None


def _safe_count(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _elapsed_ms(started: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - started) * 1000))
