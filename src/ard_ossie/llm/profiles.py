from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from ard_ossie.llm.contracts import (
    ProviderExecutionError,
    ProviderFailureKind,
    ProviderName,
)
from ard_ossie.models import StrictModel

_PROFILE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MODEL_NAME = re.compile(r"^[^\x00\r\n]{1,200}$")
_LOCATION = re.compile(r"^(?:global|us|eu|[a-z]+-[a-z]+[0-9])$")

APIStyle = Literal["chat_completions", "responses"]
StructuredOutput = Literal["native", "prompt_json"]


class ProfileDefaults(StrictModel):
    timeout_seconds: int = Field(default=120, gt=0, le=600)
    max_output_tokens: int = Field(default=4096, gt=0, le=65_536)
    temperature: float = Field(default=0, ge=0, le=2)


class _BaseProfile(StrictModel):
    provider: ProviderName
    model: str
    structured_output: StructuredOutput
    timeout_seconds: int | None = Field(default=None, gt=0, le=600)
    max_output_tokens: int | None = Field(default=None, gt=0, le=65_536)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if _MODEL_NAME.fullmatch(normalized) is None:
            raise ValueError("invalid model")
        return normalized

    def with_defaults(self, defaults: ProfileDefaults) -> Self:
        return self.model_copy(
            update={
                "timeout_seconds": self.timeout_seconds or defaults.timeout_seconds,
                "max_output_tokens": self.max_output_tokens or defaults.max_output_tokens,
                "temperature": (
                    defaults.temperature if self.temperature is None else self.temperature
                ),
            }
        )

    def required_environment_names(self) -> tuple[str, ...]:
        raise NotImplementedError


class OpenAICompatibleProfile(_BaseProfile):
    provider: Literal["openai_compatible"]
    api: APIStyle
    base_url_env: Literal["ARD_LLM_BASE_URL"]
    api_key_env: Literal["ARD_LLM_API_KEY"]

    def required_environment_names(self) -> tuple[str, ...]:
        return (self.base_url_env, self.api_key_env)


class AzureOpenAIProfile(_BaseProfile):
    provider: Literal["azure_openai"]
    api: APIStyle
    endpoint_env: Literal["ARD_AZURE_OPENAI_ENDPOINT"]
    api_key_env: Literal["ARD_AZURE_OPENAI_API_KEY"]

    def required_environment_names(self) -> tuple[str, ...]:
        return (self.endpoint_env, self.api_key_env)


class _VertexProfile(_BaseProfile):
    project_env: Literal["ARD_GCP_PROJECT_ID"]
    location: str
    credentials_env: Literal["ARD_VERTEX_CREDENTIALS_JSON"]

    @field_validator("location")
    @classmethod
    def validate_location(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _LOCATION.fullmatch(normalized) is None:
            raise ValueError("invalid Vertex location")
        return normalized

    def required_environment_names(self) -> tuple[str, ...]:
        return (self.project_env, self.credentials_env)


class VertexGeminiProfile(_VertexProfile):
    provider: Literal["vertex_gemini"]


class VertexClaudeProfile(_VertexProfile):
    provider: Literal["vertex_claude"]

    @model_validator(mode="after")
    def require_prompt_json(self) -> VertexClaudeProfile:
        if self.structured_output != "prompt_json":
            raise ValueError("Vertex Claude requires prompt_json")
        return self


LLMProfile = Annotated[
    OpenAICompatibleProfile | AzureOpenAIProfile | VertexGeminiProfile | VertexClaudeProfile,
    Field(discriminator="provider"),
]


class LLMProfileRegistry(StrictModel):
    version: Literal[1]
    defaults: ProfileDefaults = Field(default_factory=ProfileDefaults)
    profiles: dict[str, LLMProfile]

    @field_validator("profiles")
    @classmethod
    def validate_profile_names(
        cls,
        value: dict[str, LLMProfile],
    ) -> dict[str, LLMProfile]:
        if not value:
            raise ValueError("profile registry must not be empty")
        if any(_PROFILE_NAME.fullmatch(name) is None for name in value):
            raise ValueError("invalid profile name")
        return value

    @classmethod
    def load(cls, path: Path) -> LLMProfileRegistry:
        try:
            return cls._load_text(path.read_text(encoding="utf-8"))
        except ProviderExecutionError:
            raise
        except (OSError, UnicodeError):
            raise _invalid_profile_error() from None

    @classmethod
    def load_packaged(cls) -> LLMProfileRegistry:
        resource = files("ard_ossie").joinpath("assets/config/llm-profiles.yaml")
        try:
            if resource.is_file():
                return cls._load_text(resource.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            raise _invalid_profile_error() from None
        source_path = Path(__file__).resolve().parents[3] / "config" / "llm-profiles.yaml"
        return cls.load(source_path)

    @classmethod
    def _load_text(cls, content: str) -> LLMProfileRegistry:
        try:
            payload = yaml.load(content, Loader=_UniqueKeyLoader)
            if not isinstance(payload, dict):
                raise TypeError
            return cls.model_validate(payload)
        except (ConstructorError, TypeError, ValueError, ValidationError, yaml.YAMLError):
            raise _invalid_profile_error() from None

    def resolve(self, name: str) -> LLMProfile:
        try:
            profile = self.profiles[name]
        except KeyError:
            raise ProviderExecutionError(
                "LLM_PROFILE_NOT_FOUND",
                kind=ProviderFailureKind.CONFIGURATION,
            ) from None
        return profile.with_defaults(self.defaults)

    def safe_profiles(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "provider": profile.provider,
                "model": profile.model,
                "structured_output": profile.structured_output,
            }
            for name, profile in sorted(self.profiles.items())
        ]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(None, None, "duplicate key", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _invalid_profile_error() -> ProviderExecutionError:
    return ProviderExecutionError(
        "LLM_PROFILE_INVALID",
        kind=ProviderFailureKind.CONFIGURATION,
    )
