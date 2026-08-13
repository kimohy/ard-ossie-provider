from __future__ import annotations

from collections.abc import Callable

import pytest

from ard_ossie.llm import (
    LLMMetadata,
    LLMResult,
    LLMService,
    ProviderExecutionError,
    ProviderFailureKind,
)


def metadata() -> LLMMetadata:
    return LLMMetadata(
        profile="safe-profile",
        provider="openai_compatible",
        model="safe-model",
        elapsed_ms=1,
    )


def raw_result(text: str, structured: dict[str, object] | None = None) -> LLMResult:
    return LLMResult(text=text, structured=structured, metadata=metadata())


def transient_error() -> ProviderExecutionError:
    return ProviderExecutionError(
        "LLM_PROVIDER_TIMEOUT",
        kind=ProviderFailureKind.TRANSIENT,
    )


class SequenceProvider:
    def __init__(self, values: list[LLMResult | ProviderExecutionError]) -> None:
        self.values = iter(values)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    def capabilities(self) -> dict[str, str]:
        return {"structured_output": "json_schema"}

    def generate_text(self, *, messages: list[dict[str, str]]) -> LLMResult:
        return self._next(messages)

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult:
        return self._next(messages)

    def _next(self, messages: list[dict[str, str]]) -> LLMResult:
        self.calls += 1
        self.messages.append(messages)
        value = next(self.values)
        if isinstance(value, ProviderExecutionError):
            raise value
        return value


def closed_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }


def user_messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "produce JSON"}]


def test_transient_failure_retries_three_total_attempts_same_provider() -> None:
    provider = SequenceProvider([transient_error(), transient_error(), raw_result('{"ok":true}')])
    sleeps: list[float] = []
    service = LLMService(provider, sleep=sleeps.append, jitter=lambda: 0)

    output = service.generate_structured(schema=closed_schema(), messages=user_messages())

    assert output.structured == {"ok": True}
    assert output.metadata.retry_count == 2
    assert provider.calls == 3
    assert sleeps == [1.0, 2.0]


def test_non_retryable_failure_is_raised_immediately() -> None:
    error = ProviderExecutionError(
        "LLM_PROVIDER_AUTHENTICATION_FAILED",
        kind=ProviderFailureKind.CONFIGURATION,
    )
    provider = SequenceProvider([error])
    sleeps: list[float] = []

    with pytest.raises(ProviderExecutionError, match=error.code):
        LLMService(provider, sleep=sleeps.append).generate_text(messages=user_messages())

    assert provider.calls == 1
    assert sleeps == []


def test_markdown_json_fence_is_removed_without_repair() -> None:
    provider = SequenceProvider([raw_result('```json\n{"ok":true}\n```')])

    output = LLMService(provider).generate_structured(
        schema=closed_schema(),
        messages=user_messages(),
    )

    assert output.structured == {"ok": True}
    assert output.metadata.repair_count == 0
    assert provider.calls == 1


def test_invalid_output_uses_two_repairs_then_fails_without_body() -> None:
    provider = SequenceProvider([raw_result("bad"), raw_result("bad-2"), raw_result("bad-3")])
    service = LLMService(provider, sleep=lambda _: None, jitter=lambda: 0)

    with pytest.raises(ProviderExecutionError, match="LLM_INVALID_JSON") as captured:
        service.generate_structured(schema=closed_schema(), messages=user_messages())

    assert provider.calls == 3
    assert "bad-3" not in repr(captured.value)
    assert provider.messages[1][0]["role"] == "system"
    assert "LLM_INVALID_JSON" in provider.messages[1][0]["content"]
    assert "bad" not in provider.messages[1][0]["content"]


def test_successful_repair_records_count_and_keeps_same_metadata() -> None:
    provider = SequenceProvider([raw_result("bad"), raw_result('{"ok":true}')])

    output = LLMService(provider).generate_structured(
        schema=closed_schema(),
        messages=user_messages(),
    )

    assert output.structured == {"ok": True}
    assert output.metadata.profile == "safe-profile"
    assert output.metadata.model == "safe-model"
    assert output.metadata.repair_count == 1


def test_adapter_output_error_can_be_repaired_without_exposing_rejected_body() -> None:
    rejected = raw_result("rejected-sensitive-body")
    error = ProviderExecutionError(
        "LLM_INVALID_JSON",
        kind=ProviderFailureKind.OUTPUT,
        rejected_result=rejected,
    )
    provider = SequenceProvider([error, raw_result('{"ok":true}')])

    output = LLMService(provider).generate_structured(
        schema=closed_schema(),
        messages=user_messages(),
    )

    assert output.structured == {"ok": True}
    assert output.metadata.repair_count == 1
    assert "rejected-sensitive-body" not in repr(error)
    assert "rejected-sensitive-body" not in provider.messages[1][0]["content"]


def test_structured_retries_and_repairs_share_three_attempt_budget() -> None:
    provider = SequenceProvider(
        [
            transient_error(),
            raw_result("bad"),
            raw_result("bad-again"),
            raw_result('{"ok":true}'),
        ]
    )
    sleeps: list[float] = []

    with pytest.raises(ProviderExecutionError, match="LLM_INVALID_JSON"):
        LLMService(
            provider,
            sleep=sleeps.append,
            jitter=lambda: 0,
        ).generate_structured(schema=closed_schema(), messages=user_messages())

    assert provider.calls == 3
    assert sleeps == [1.0]


def test_retry_backoff_is_bounded() -> None:
    provider = SequenceProvider([transient_error(), transient_error(), transient_error()])
    sleeps: list[float] = []
    jitter_values: list[Callable[[], float]] = [lambda: 100.0]

    with pytest.raises(ProviderExecutionError, match="LLM_PROVIDER_TIMEOUT"):
        LLMService(
            provider,
            sleep=sleeps.append,
            jitter=jitter_values[0],
        ).generate_text(messages=user_messages())

    assert provider.calls == 3
    assert all(delay <= 8.0 for delay in sleeps)
