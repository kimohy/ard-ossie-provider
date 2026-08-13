from __future__ import annotations

import json
import random
import time
from collections.abc import Callable

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate

from ard_ossie.llm.contracts import (
    LLMProvider,
    LLMResult,
    ProviderExecutionError,
    ProviderFailureKind,
)

_MAX_TOTAL_ATTEMPTS = 3
_MAX_REPAIRS = 2
_MAX_BACKOFF_SECONDS = 8.0


class LLMService:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self._sleep = sleep
        self._jitter = jitter

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> LLMResult:
        result, retries = self._retry(lambda: self.provider.generate_text(messages=messages))
        return _with_counts(result, retries=retries, repairs=0)

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        repairs = 0
        retries = 0
        active_messages = messages
        for attempt in range(_MAX_TOTAL_ATTEMPTS):
            try:
                result = self.provider.generate_structured(
                    schema=schema,
                    messages=active_messages,
                )
            except ProviderExecutionError as error:
                if (
                    error.kind is ProviderFailureKind.TRANSIENT
                    and attempt < _MAX_TOTAL_ATTEMPTS - 1
                ):
                    self._sleep_before_retry(retries)
                    retries += 1
                    continue
                if (
                    error.kind is not ProviderFailureKind.OUTPUT
                    or not isinstance(error.rejected_result, LLMResult)
                    or repairs >= _MAX_REPAIRS
                    or attempt == _MAX_TOTAL_ATTEMPTS - 1
                ):
                    raise
                repairs += 1
                active_messages = _repair_messages(messages, schema, error.code)
                continue
            try:
                structured = _validate_result(result, schema)
            except ProviderExecutionError as error:
                if (
                    error.kind is not ProviderFailureKind.OUTPUT
                    or repairs >= _MAX_REPAIRS
                    or attempt == _MAX_TOTAL_ATTEMPTS - 1
                ):
                    raise
                repairs += 1
                active_messages = _repair_messages(messages, schema, error.code)
                continue
            normalized = result.model_copy(update={"structured": structured})
            return _with_counts(
                normalized,
                retries=retries,
                repairs=repairs,
            )
        raise AssertionError("unreachable")

    def _retry(
        self,
        operation: Callable[[], LLMResult],
    ) -> tuple[LLMResult, int]:
        for attempt in range(_MAX_TOTAL_ATTEMPTS):
            try:
                return operation(), attempt
            except ProviderExecutionError as error:
                if (
                    error.kind is not ProviderFailureKind.TRANSIENT
                    or attempt == _MAX_TOTAL_ATTEMPTS - 1
                ):
                    raise
                self._sleep_before_retry(attempt)
        raise AssertionError("unreachable")

    def _sleep_before_retry(self, retry_number: int) -> None:
        delay = min(
            _MAX_BACKOFF_SECONDS,
            (2**retry_number) + max(0.0, self._jitter()),
        )
        self._sleep(delay)


def _validate_result(
    result: LLMResult,
    schema: dict[str, object],
) -> dict[str, object]:
    if result.structured is not None:
        candidate: object = result.structured
    else:
        content = _strip_json_fence(result.text)
        try:
            candidate = json.loads(content)
        except json.JSONDecodeError:
            raise _output_error("LLM_INVALID_JSON") from None
    if not isinstance(candidate, dict):
        raise _output_error("LLM_RESPONSE_NOT_OBJECT")
    try:
        validate(instance=candidate, schema=schema)
    except JsonSchemaValidationError:
        raise _output_error("LLM_SCHEMA_VIOLATION") from None
    return candidate


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        return stripped[8:-4].strip()
    return stripped


def _repair_messages(
    original: list[dict[str, str]],
    schema: dict[str, object],
    code: str,
) -> list[dict[str, str]]:
    instruction = {
        "validation_code": code,
        "required_schema": schema,
        "rules": [
            "Return only one JSON object.",
            "Correct only content that violates the schema or validation code.",
            "Do not add unsupported facts.",
        ],
    }
    return [
        {
            "role": "system",
            "content": json.dumps(
                instruction,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        *original,
    ]


def _with_counts(result: LLMResult, *, retries: int, repairs: int) -> LLMResult:
    metadata = result.metadata.model_copy(
        update={
            "retry_count": min(retries, 2),
            "repair_count": repairs,
        }
    )
    return result.model_copy(update={"metadata": metadata})


def _output_error(code: str) -> ProviderExecutionError:
    return ProviderExecutionError(code, kind=ProviderFailureKind.OUTPUT)
