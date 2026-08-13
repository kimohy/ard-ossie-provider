from __future__ import annotations

import json

from typer.testing import CliRunner

from ard_ossie.cli import app
from ard_ossie.llm import LLMMetadata, LLMResult


def test_profiles_lists_safe_metadata_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ARD_LLM_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["llm", "profiles"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == [
        {
            "model": "gpt-5.6-terra",
            "name": "openai-compatible-default",
            "provider": "openai_compatible",
            "structured_output": "native",
        }
    ]
    assert "api_key" not in result.stdout.lower()


def test_validate_without_credential_reports_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("ARD_LLM_PROFILE", "openai-compatible-default")
    monkeypatch.setenv("ARD_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("ARD_LLM_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["llm", "validate"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "valid"
    assert payload["profile"] == "openai-compatible-default"
    assert payload["credential_check"] == "unavailable"
    assert payload["runtime_values"] == "incomplete"


def test_smoke_test_outputs_only_safe_metadata(monkeypatch) -> None:
    metadata = LLMMetadata(
        profile="openai-compatible-default",
        provider="openai_compatible",
        model="gpt-5.6-terra",
        request_id="req_123",
        input_tokens=3,
        output_tokens=1,
        finish_reason="stop",
        elapsed_ms=4,
    )

    class FakeService:
        def generate_text(self, *, messages):
            return LLMResult(text="private-text-body", metadata=metadata)

        def generate_structured(self, *, schema, messages):
            return LLMResult(
                text='{"ok":true,"private":"body"}',
                structured={"ok": True},
                metadata=metadata,
            )

    monkeypatch.setattr(
        "ard_ossie.cli.llm._service_for_profile",
        lambda profile: FakeService(),
    )

    result = CliRunner().invoke(
        app,
        ["llm", "smoke-test", "--profile", "openai-compatible-default"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["text_success"] is True
    assert payload["structured_success"] is True
    assert payload["profile"] == "openai-compatible-default"
    assert payload["provider"] == "openai_compatible"
    assert payload["request_id"] == "req_123"
    assert "private-text-body" not in result.stdout
    assert '"private"' not in result.stdout
