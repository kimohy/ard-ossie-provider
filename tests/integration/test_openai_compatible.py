from __future__ import annotations

import json

import httpx
from openai import OpenAI
from pydantic import SecretStr

from ard_ossie.llm import OpenAICompatibleProvider


def test_real_openai_sdk_contract_uses_compatible_chat_completions_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "compatible-model"
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-contract",
                "object": "chat.completion",
                "created": 1_723_078_800,
                "model": "compatible-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"terms":["revenue"]}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk_client = OpenAI(
        base_url="https://compatible.example/v1",
        api_key="contract-secret",
        http_client=http_client,
    )
    provider = OpenAICompatibleProvider(
        base_url="https://compatible.example/v1",
        api_key=SecretStr("contract-secret"),
        model="compatible-model",
        client=sdk_client,
    )

    result = provider.generate_structured(
        schema={
            "type": "object",
            "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
            "required": ["terms"],
            "additionalProperties": False,
        },
        messages=[{"role": "user", "content": "extract"}],
    )

    assert result.structured == {"terms": ["revenue"]}
