from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from ard_ossie.llm import (
    LLMProfileRegistry,
    LLMProviderFactory,
    ProviderExecutionError,
    ProviderFailureKind,
)


class RecordingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.reads.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("factory must not enumerate environment variables")

    def __len__(self) -> int:
        raise AssertionError("factory must not inspect environment size")


def migration_profile():
    return LLMProfileRegistry.load_packaged().resolve("openai-compatible-default")


def test_factory_reads_only_selected_profile_environment() -> None:
    reads = RecordingEnvironment(
        {
            "ARD_LLM_BASE_URL": "https://example.test/v1",
            "ARD_LLM_API_KEY": "secret",
            "ARD_VERTEX_CREDENTIALS_JSON": "must-not-be-read",
        }
    )
    captured: dict[str, object] = {}

    class Provider:
        def capabilities(self) -> dict[str, str | bool]:
            return {"structured_output": "json_schema", "vision": True}

    def construct(**kwargs: object) -> object:
        captured.update(kwargs)
        return Provider()

    provider = LLMProviderFactory(openai_constructor=construct).create(
        "openai-compatible-default",
        migration_profile(),
        reads,
    )

    assert provider is not None
    assert reads.reads == ["ARD_LLM_BASE_URL", "ARD_LLM_API_KEY"]
    assert captured["model"] == "gpt-5.6-terra"
    assert captured["profile"] == "openai-compatible-default"
    assert captured["max_output_tokens"] is None
    assert captured["vision"] is True
    assert captured["api_key"].get_secret_value() == "secret"


def test_factory_rejects_missing_selected_value_without_value_leak() -> None:
    environment = RecordingEnvironment({"ARD_LLM_BASE_URL": "https://example.test/v1"})

    with pytest.raises(
        ProviderExecutionError,
        match="LLM_PROVIDER_CONFIGURATION_FAILED",
    ) as captured:
        LLMProviderFactory().create(
            "openai-compatible-default",
            migration_profile(),
            environment,
        )

    assert captured.value.kind is ProviderFailureKind.CONFIGURATION
    assert environment.reads == ["ARD_LLM_BASE_URL", "ARD_LLM_API_KEY"]
    assert "https://example.test/v1" not in repr(captured.value)


def test_factory_rejects_selected_adapter_capability_mismatch() -> None:
    class BadProvider:
        def capabilities(self) -> dict[str, str]:
            return {"structured_output": "prompt_json"}

    environment = RecordingEnvironment(
        {
            "ARD_LLM_BASE_URL": "https://example.test/v1",
            "ARD_LLM_API_KEY": "secret",
        }
    )

    with pytest.raises(
        ProviderExecutionError,
        match="LLM_PROVIDER_CAPABILITY_UNSUPPORTED",
    ):
        LLMProviderFactory(openai_constructor=lambda **_: BadProvider()).create(
            "openai-compatible-default",
            migration_profile(),
            environment,
        )
