from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import SecretStr

from ard_ossie.llm.contracts import (
    LLMProvider,
    ProviderExecutionError,
    ProviderFailureKind,
)
from ard_ossie.llm.openai_adapters import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from ard_ossie.llm.profiles import (
    AzureOpenAIProfile,
    LLMProfile,
    OpenAICompatibleProfile,
    VertexClaudeProfile,
    VertexGeminiProfile,
)
from ard_ossie.llm.vertex_adapters import (
    VertexClaudeProvider,
    VertexGeminiProvider,
)

ProviderConstructor = Callable[..., LLMProvider]


class LLMProviderFactory:
    def __init__(
        self,
        *,
        openai_constructor: ProviderConstructor = OpenAICompatibleProvider,
        azure_constructor: ProviderConstructor = AzureOpenAIProvider,
        gemini_constructor: ProviderConstructor = VertexGeminiProvider,
        claude_constructor: ProviderConstructor = VertexClaudeProvider,
    ) -> None:
        self._openai_constructor = openai_constructor
        self._azure_constructor = azure_constructor
        self._gemini_constructor = gemini_constructor
        self._claude_constructor = claude_constructor

    def create(
        self,
        profile_name: str,
        profile: LLMProfile,
        environment: Mapping[str, str],
    ) -> LLMProvider:
        values = {
            name: _required_value(environment, name)
            for name in profile.required_environment_names()
        }
        common: dict[str, Any] = {
            "profile": profile_name,
            "timeout_seconds": profile.timeout_seconds,
            "max_output_tokens": (
                None
                if isinstance(profile, OpenAICompatibleProfile)
                and profile.max_output_tokens == "model_maximum"
                else profile.max_output_tokens
            ),
            "temperature": profile.temperature,
            "vision": profile.vision,
        }
        if isinstance(profile, OpenAICompatibleProfile):
            provider = self._openai_constructor(
                base_url=values[profile.base_url_env],
                api_key=SecretStr(values[profile.api_key_env]),
                model=profile.model,
                api=profile.api,
                **common,
            )
        elif isinstance(profile, AzureOpenAIProfile):
            provider = self._azure_constructor(
                endpoint=values[profile.endpoint_env],
                api_key=SecretStr(values[profile.api_key_env]),
                deployment=profile.model,
                api=profile.api,
                **common,
            )
        elif isinstance(profile, VertexGeminiProfile):
            provider = self._gemini_constructor(
                project=values[profile.project_env],
                location=profile.location,
                model=profile.model,
                credentials_json=SecretStr(values[profile.credentials_env]),
                **common,
            )
        elif isinstance(profile, VertexClaudeProfile):
            provider = self._claude_constructor(
                project=values[profile.project_env],
                location=profile.location,
                model=profile.model,
                credentials_json=SecretStr(values[profile.credentials_env]),
                **common,
            )
        else:
            raise _configuration_error()
        _validate_capability(profile, provider)
        return provider


def _required_value(environment: Mapping[str, str], name: str) -> str:
    try:
        value = environment[name]
    except KeyError:
        raise _configuration_error() from None
    if not isinstance(value, str) or not value.strip():
        raise _configuration_error()
    return value.strip()


def _validate_capability(profile: LLMProfile, provider: LLMProvider) -> None:
    try:
        capabilities = provider.capabilities()
        capability = capabilities.get("structured_output")
        vision = capabilities.get("vision")
    except Exception:
        raise _configuration_error("LLM_PROVIDER_CAPABILITY_UNSUPPORTED") from None
    expected = "json_schema" if profile.structured_output == "native" else "prompt_json"
    if capability != expected or vision is not profile.vision:
        raise _configuration_error("LLM_PROVIDER_CAPABILITY_UNSUPPORTED")


def _configuration_error(
    code: str = "LLM_PROVIDER_CONFIGURATION_FAILED",
) -> ProviderExecutionError:
    return ProviderExecutionError(code, kind=ProviderFailureKind.CONFIGURATION)
