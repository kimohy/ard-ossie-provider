from __future__ import annotations

from collections.abc import Mapping

from ard_ossie.application.processing import provider_from_environment
from ard_ossie.llm import LLMProfileRegistry, LLMProviderFactory, LLMService


class FakeProvider:
    def __init__(self, **kwargs: object) -> None:
        self.model = kwargs["model"]
        self.api = kwargs["api"]
        self.profile = kwargs["profile"]

    def capabilities(self) -> dict[str, str]:
        return {"structured_output": "json_schema"}


def environment(**overrides: str) -> Mapping[str, str]:
    return {
        "ARD_LLM_PROFILE": "openai-compatible-default",
        "ARD_LLM_BASE_URL": "https://example.test/v1",
        "ARD_LLM_API_KEY": "secret",
        **overrides,
    }


def test_provider_from_environment_resolves_repository_profile() -> None:
    factory = LLMProviderFactory(openai_constructor=FakeProvider)

    service = provider_from_environment(
        registry=LLMProfileRegistry.load_packaged(),
        environment=environment(),
        factory=factory,
    )

    assert isinstance(service, LLMService)
    assert service.provider.model == "gpt-5.6-terra"
    assert service.provider.api == "chat_completions"
    assert service.provider.profile == "openai-compatible-default"


def test_provider_from_environment_ignores_legacy_model_and_api_style() -> None:
    factory = LLMProviderFactory(openai_constructor=FakeProvider)

    service = provider_from_environment(
        registry=LLMProfileRegistry.load_packaged(),
        environment=environment(
            ARD_LLM_MODEL="must-not-be-read",
            ARD_LLM_API_STYLE="must-not-be-read",
        ),
        factory=factory,
    )

    assert isinstance(service, LLMService)
    assert service.provider.model == "gpt-5.6-terra"
    assert service.provider.api == "chat_completions"


def test_provider_from_environment_keeps_deterministic_local_mode_without_profile() -> None:
    service = provider_from_environment(
        registry=LLMProfileRegistry.load_packaged(),
        environment={
            "ARD_LLM_MODEL": "legacy-model",
            "ARD_LLM_API_STYLE": "chat_completions",
        },
        factory=LLMProviderFactory(openai_constructor=FakeProvider),
    )

    assert service is None
