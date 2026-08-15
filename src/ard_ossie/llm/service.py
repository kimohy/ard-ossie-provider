from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import TypeVar

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from pydantic import JsonValue

from ard_ossie.canonical import canonical_hash
from ard_ossie.llm.contracts import (
    LLMMultimodalMessage,
    LLMProvider,
    LLMResult,
    LLMTextPart,
    ProviderExecutionError,
    ProviderFailureKind,
)

_MAX_TOTAL_ATTEMPTS = 3
_MAX_REPAIRS = 2
_MAX_BACKOFF_SECONDS = 8.0
STRUCTURED_REPAIR_PROMPT_VERSION = "structured-output-repair-v1"
_Messages = TypeVar("_Messages")


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

    def health_check(self) -> bool:
        return self.provider.health_check()

    def capabilities(self) -> dict[str, JsonValue]:
        return self.provider.capabilities()

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> LLMResult:
        result, retries = self._retry(lambda: self.provider.generate_text(messages=messages))
        return _with_counts(result, retries=retries, repairs=0, repair_codes=())

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        return self._generate_structured(
            schema=schema,
            messages=messages,
            generate=lambda active: self.provider.generate_structured(
                schema=schema,
                messages=active,
            ),
            repair=_repair_messages,
        )

    def generate_multimodal_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[LLMMultimodalMessage],
    ) -> LLMResult:
        return self._generate_structured(
            schema=schema,
            messages=messages,
            generate=lambda active: self.provider.generate_multimodal_structured(
                schema=schema,
                messages=active,
            ),
            repair=_repair_multimodal_messages,
        )

    def _generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[_Messages],
        generate: Callable[[list[_Messages]], LLMResult],
        repair: Callable[[list[_Messages], dict[str, object], str], list[_Messages]],
    ) -> LLMResult:
        repairs = 0
        retries = 0
        repair_codes: list[str] = []
        active_messages = messages
        for attempt in range(_MAX_TOTAL_ATTEMPTS):
            try:
                result = generate(active_messages)
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
                    raise _with_attempt_counts(
                        error,
                        retries=retries,
                        repairs=repairs,
                    ) from None
                repairs += 1
                repair_codes.append(error.code)
                active_messages = repair(messages, schema, error.code)
                continue
            try:
                structured = _validate_result(result, schema)
            except ProviderExecutionError as error:
                if (
                    error.kind is not ProviderFailureKind.OUTPUT
                    or repairs >= _MAX_REPAIRS
                    or attempt == _MAX_TOTAL_ATTEMPTS - 1
                ):
                    raise _with_attempt_counts(
                        error,
                        retries=retries,
                        repairs=repairs,
                    ) from None
                repairs += 1
                repair_codes.append(error.code)
                active_messages = repair(messages, schema, error.code)
                continue
            normalized = result.model_copy(update={"structured": structured})
            return _with_counts(
                normalized,
                retries=retries,
                repairs=repairs,
                repair_codes=tuple(repair_codes),
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


def _with_attempt_counts(
    error: ProviderExecutionError,
    *,
    retries: int,
    repairs: int,
) -> ProviderExecutionError:
    return ProviderExecutionError(
        error.code,
        kind=error.kind,
        rejected_result=error.rejected_result,
        retry_count=retries,
        repair_count=repairs,
    )


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


def _repair_multimodal_messages(
    original: list[LLMMultimodalMessage],
    schema: dict[str, object],
    code: str,
) -> list[LLMMultimodalMessage]:
    return [
        LLMMultimodalMessage(
            role="system",
            content=(
                LLMTextPart(
                    text=json.dumps(
                        _repair_instruction(schema, code),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                ),
            ),
        ),
        *original,
    ]


def _repair_instruction(schema: dict[str, object], code: str) -> dict[str, object]:
    return {
        "validation_code": code,
        "required_schema": schema,
        "rules": [
            "Return only one JSON object.",
            "Correct only content that violates the schema or validation code.",
            "Do not add unsupported facts.",
        ],
    }


def structured_repair_prompt_contract_hash(schema: dict[str, object]) -> str:
    """Hash the complete repair template independently of a runtime error code."""
    return canonical_hash(
        {
            "version": STRUCTURED_REPAIR_PROMPT_VERSION,
            "instruction": _repair_instruction(schema, "{VALIDATION_CODE}"),
        }
    )


def _with_counts(
    result: LLMResult,
    *,
    retries: int,
    repairs: int,
    repair_codes: tuple[str, ...],
) -> LLMResult:
    metadata = result.metadata.model_copy(
        update={
            "retry_count": min(retries, 2),
            "repair_count": repairs,
        }
    )
    return result.model_copy(
        update={
            "metadata": metadata,
            "repair_validation_codes": list(repair_codes),
        }
    )


def _output_error(code: str) -> ProviderExecutionError:
    return ProviderExecutionError(code, kind=ProviderFailureKind.OUTPUT)
