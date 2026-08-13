from __future__ import annotations

import json
import os
from collections import ChainMap
from typing import Annotated

import typer

from ard_ossie.application.processing import provider_from_environment
from ard_ossie.llm import (
    LLMProfileRegistry,
    LLMService,
    ProviderExecutionError,
    ProviderFailureKind,
)

app = typer.Typer(no_args_is_help=True)


@app.callback()
def llm_group() -> None:
    """Inspect, validate, and smoke-test trusted LLM profiles."""


@app.command("profiles")
def profiles() -> None:
    registry = LLMProfileRegistry.load_packaged()
    typer.echo(json.dumps(registry.safe_profiles(), sort_keys=True))


@app.command("validate")
def validate_profile(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    registry = LLMProfileRegistry.load_packaged()
    profile_name = profile or os.environ.get("ARD_LLM_PROFILE")
    if not profile_name:
        _emit_error("LLM_PROFILE_NOT_SELECTED")
    selected = registry.resolve(profile_name)
    names = selected.required_environment_names()
    present = {name: bool(os.environ.get(name)) for name in names}
    credential_names = {
        name for name in names if name.endswith("API_KEY") or name.endswith("CREDENTIALS_JSON")
    }
    credential_check = (
        "available"
        if credential_names and all(present[name] for name in credential_names)
        else "unavailable"
    )
    typer.echo(
        json.dumps(
            {
                "credential_check": credential_check,
                "profile": profile_name,
                "provider": selected.provider,
                "runtime_values": "complete" if all(present.values()) else "incomplete",
                "status": "valid",
            },
            sort_keys=True,
        )
    )


@app.command("smoke-test")
def smoke_test(
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    profile_name = profile or os.environ.get("ARD_LLM_PROFILE")
    if not profile_name:
        _emit_error("LLM_PROFILE_NOT_SELECTED")
    try:
        service = _service_for_profile(profile_name)
        text_result = service.generate_text(
            messages=[{"role": "user", "content": "Reply with OK."}]
        )
        structured_result = service.generate_structured(
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            messages=[
                {
                    "role": "user",
                    "content": "Return a JSON object with ok set to true.",
                }
            ],
        )
    except ProviderExecutionError as error:
        typer.echo(
            json.dumps(
                {"error_code": error.code, "profile": profile_name, "status": "failure"},
                sort_keys=True,
            )
        )
        raise typer.Exit(1) from None
    metadata = structured_result.metadata
    text_success = bool(text_result.text)
    structured_success = structured_result.structured == {"ok": True}
    typer.echo(
        json.dumps(
            {
                "elapsed_ms": text_result.metadata.elapsed_ms + metadata.elapsed_ms,
                "input_tokens": _sum_optional(
                    text_result.metadata.input_tokens,
                    metadata.input_tokens,
                ),
                "model": metadata.model,
                "output_tokens": _sum_optional(
                    text_result.metadata.output_tokens,
                    metadata.output_tokens,
                ),
                "profile": metadata.profile,
                "provider": metadata.provider,
                "request_id": metadata.request_id,
                "status": "success" if text_success and structured_success else "failure",
                "structured_success": structured_success,
                "text_success": text_success,
            },
            sort_keys=True,
        )
    )
    if not text_success or not structured_success:
        raise typer.Exit(1)


def _service_for_profile(profile: str) -> LLMService:
    environment = ChainMap({"ARD_LLM_PROFILE": profile}, os.environ)
    service = provider_from_environment(environment=environment)
    if not isinstance(service, LLMService):
        raise ProviderExecutionError(
            "LLM_PROFILE_NOT_SELECTED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    return service


def _sum_optional(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


def _emit_error(code: str) -> None:
    typer.echo(json.dumps({"error_code": code, "status": "failure"}, sort_keys=True))
    raise typer.Exit(2)
