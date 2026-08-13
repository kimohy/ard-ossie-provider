from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.llm import ProviderExecutionError
from ard_ossie.llm.profiles import LLMProfileRegistry


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_packaged_registry_resolves_migration_profile() -> None:
    profile = LLMProfileRegistry.load_packaged().resolve("openai-compatible-default")

    assert profile.provider == "openai_compatible"
    assert profile.model == "gpt-5.6-terra"
    assert profile.api == "chat_completions"
    assert profile.base_url_env == "ARD_LLM_BASE_URL"
    assert profile.api_key_env == "ARD_LLM_API_KEY"
    assert profile.timeout_seconds == 120
    assert profile.max_output_tokens == 4096
    assert profile.temperature == 0
    assert profile.vision is True


def test_registry_rejects_unknown_keys_and_unsafe_environment_names(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """version: 1
defaults: {timeout_seconds: 120, max_output_tokens: 4096}
profiles:
  unsafe:
    provider: openai_compatible
    model: model
    structured_output: native
    api: chat_completions
    base_url_env: HOME
    api_key_env: ARD_LLM_API_KEY
    surprise: true
""",
    )

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_registry_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """version: 1
version: 1
profiles: {}
""",
    )

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_registry_rejects_unknown_provider(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """version: 1
profiles:
  bad:
    provider: unknown
    model: model
    structured_output: native
""",
    )

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_registry_rejects_provider_environment_name_outside_allowlist(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """version: 1
profiles:
  bad:
    provider: azure_openai
    model: deployment
    structured_output: native
    api: chat_completions
    endpoint_env: ARD_LLM_BASE_URL
    api_key_env: ARD_AZURE_OPENAI_API_KEY
""",
    )

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_vertex_claude_requires_prompt_json_mode(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """version: 1
profiles:
  claude:
    provider: vertex_claude
    model: claude-model
    structured_output: native
    project_env: ARD_GCP_PROJECT_ID
    location: global
    credentials_env: ARD_VERTEX_CREDENTIALS_JSON
""",
    )

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_resolve_rejects_unknown_profile_without_listing_available_names() -> None:
    registry = LLMProfileRegistry.load_packaged()

    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_NOT_FOUND") as captured:
        registry.resolve("missing")

    assert "openai-compatible-default" not in repr(captured.value)
