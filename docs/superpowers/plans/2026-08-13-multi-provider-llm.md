# Multi-Provider LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an administrator select one repository-defined LLM profile for an ARD processing run and execute it through OpenAI-compatible, Azure OpenAI, Vertex Gemini, or Vertex Claude adapters without weakening existing trust, diagnostics, or source-fidelity boundaries.

**Architecture:** Convert `ard_ossie.llm` into a compatibility-exporting package whose focused modules own contracts, suggestions, profiles, provider adapters, factory construction, and shared retry/repair behavior. The processing application resolves one trusted `ARD_LLM_PROFILE`, constructs one provider, and keeps that provider for the run. Provider adapters return normalized `LLMResult` values; the shared service validates structured content and exposes only the existing provider-neutral dictionary to the pipeline.

**Tech Stack:** Python 3.12, Pydantic 2, PyYAML, jsonschema, OpenAI Python SDK 2.46, Google Gen AI SDK 1.x, Anthropic Python SDK with Vertex support, google-auth 2.x, httpx, Typer, pytest, GitHub Actions, uv.

## Global Constraints

- One processing run resolves exactly one profile; retries and repairs never change profile, provider, model, endpoint, project, or region.
- Supported providers are exactly `openai_compatible`, `azure_openai`, `vertex_gemini`, and `vertex_claude`.
- The initial active profile is `openai-compatible-default` using `gpt-5.6-terra`, `chat_completions`, `ARD_LLM_BASE_URL`, and `ARD_LLM_API_KEY`.
- Profile configuration comes from trusted default-branch code/configuration, never the candidate checkout or Issue/dispatch content.
- Only environment names matching `^ARD_[A-Z0-9_]+$` and the provider-specific allowlist may be read.
- Raw prompts, provider bodies, credentials, headers, SDK objects, and exception messages must not enter logs, outputs, summaries, or artifacts.
- Source-facing semantic and dictionary artifacts remain authoritative and must not be rewritten by provider output.
- Transient retries use at most three total attempts; structured repair uses at most two repair calls.
- Ordinary pull request tests use fake clients/transports and never contact a paid provider.
- Existing `ProviderExecutionError`, `ProviderFailureKind`, workflow exit-code mapping, numeric versioning, quality gates, and atomic promotion remain compatible.

---

### Task 1: Split the LLM module without changing behavior

**Files:**
- Delete: `src/ard_ossie/llm.py`
- Create: `src/ard_ossie/llm/__init__.py`
- Create: `src/ard_ossie/llm/contracts.py`
- Create: `src/ard_ossie/llm/suggestions.py`
- Create: `src/ard_ossie/llm/openai_adapters.py`
- Modify: `tests/unit/test_llm.py`
- Test: `tests/unit/test_llm_compatibility.py`

**Interfaces:**
- Consumes: current public imports from `ard_ossie.llm`.
- Produces: unchanged exports for `AISuggestion`, `MetricSuggestion`, `ProductFactSuggestion`, `ProviderExecutionError`, `ProviderFailureKind`, `LLMProvider`, `OpenAICompatibleProvider`, `semantic_extraction_schema`, and `validate_semantic_suggestions`.

- [ ] **Step 1: Write the compatibility test before moving code**

```python
def test_llm_package_preserves_public_exports() -> None:
    from ard_ossie.llm import (
        AISuggestion,
        LLMProvider,
        MetricSuggestion,
        OpenAICompatibleProvider,
        ProductFactSuggestion,
        ProviderExecutionError,
        ProviderFailureKind,
        semantic_extraction_schema,
        validate_semantic_suggestions,
    )

    assert AISuggestion.__name__ == "AISuggestion"
    assert MetricSuggestion.__name__ == "MetricSuggestion"
    assert ProductFactSuggestion.__name__ == "ProductFactSuggestion"
    assert OpenAICompatibleProvider.__name__ == "OpenAICompatibleProvider"
    assert ProviderExecutionError.__name__ == "ProviderExecutionError"
    assert ProviderFailureKind.TRANSIENT == "transient"
    assert callable(semantic_extraction_schema)
    assert callable(validate_semantic_suggestions)
    assert LLMProvider is not None
```

- [ ] **Step 2: Run the new and existing tests before the split**

Run: `uv run pytest tests/unit/test_llm.py tests/unit/test_llm_compatibility.py -q`

Expected: PASS before the move, establishing the compatibility baseline.

- [ ] **Step 3: Move contracts, suggestion models, and the OpenAI-compatible adapter into focused modules**

```python
# src/ard_ossie/llm/__init__.py
from ard_ossie.llm.contracts import (
    LLMProvider,
    ProviderExecutionError,
    ProviderFailureKind,
    ProviderName,
)
from ard_ossie.llm.openai_adapters import OpenAICompatibleProvider
from ard_ossie.llm.suggestions import (
    AISuggestion,
    MetricSuggestion,
    ProductFactSuggestion,
    semantic_extraction_schema,
    validate_semantic_suggestions,
)

__all__ = [
    "AISuggestion",
    "LLMProvider",
    "MetricSuggestion",
    "OpenAICompatibleProvider",
    "ProductFactSuggestion",
    "ProviderExecutionError",
    "ProviderFailureKind",
    "ProviderName",
    "semantic_extraction_schema",
    "validate_semantic_suggestions",
]
```

Define `ProviderName = Literal["openai_compatible", "azure_openai", "vertex_gemini", "vertex_claude"]` in `contracts.py`. Move the remaining code without semantic changes. Update private monkeypatch targets from `ard_ossie.llm._new_client` to `ard_ossie.llm.openai_adapters._new_openai_client`.

- [ ] **Step 4: Run the focused compatibility suite**

Run: `uv run pytest tests/unit/test_llm.py tests/unit/test_llm_compatibility.py tests/integration/test_openai_compatible.py -q`

Expected: PASS with the same public results and safe error codes.

- [ ] **Step 5: Commit the behavior-preserving split**

```bash
git add src/ard_ossie/llm tests/unit/test_llm.py tests/unit/test_llm_compatibility.py
git commit -m "refactor: split LLM provider boundary"
```

### Task 2: Add strict repository-owned profile configuration

**Files:**
- Create: `config/llm-profiles.yaml`
- Create: `src/ard_ossie/llm/profiles.py`
- Modify: `src/ard_ossie/llm/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tests/integration/test_wheel_assets.py`
- Test: `tests/unit/test_llm_profiles.py`

**Interfaces:**
- Consumes: YAML bytes or a trusted `Path`.
- Produces: `LLMProfileRegistry.load(path: Path) -> LLMProfileRegistry`, `LLMProfileRegistry.load_packaged() -> LLMProfileRegistry`, and `registry.resolve(name: str) -> LLMProfile`.

- [ ] **Step 1: Write strict profile and packaged-asset tests**

```python
def test_registry_rejects_unknown_keys_and_unsafe_environment_names(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(
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
        encoding="utf-8",
    )
    with pytest.raises(ProviderExecutionError, match="LLM_PROFILE_INVALID"):
        LLMProfileRegistry.load(path)


def test_packaged_registry_resolves_migration_profile() -> None:
    profile = LLMProfileRegistry.load_packaged().resolve("openai-compatible-default")
    assert profile.provider == "openai_compatible"
    assert profile.model == "gpt-5.6-terra"
    assert profile.api == "chat_completions"
    assert profile.base_url_env == "ARD_LLM_BASE_URL"
    assert profile.api_key_env == "ARD_LLM_API_KEY"
```

- [ ] **Step 2: Run tests and confirm missing implementation/assets**

Run: `uv run pytest tests/unit/test_llm_profiles.py tests/integration/test_wheel_assets.py -q`

Expected: FAIL because profile models and the packaged registry do not exist.

- [ ] **Step 3: Implement discriminated strict profile models and registry loading**

```python
StructuredOutput = Literal["native", "prompt_json"]


class ProfileDefaults(StrictModel):
    timeout_seconds: int = Field(default=120, gt=0, le=600)
    max_output_tokens: int = Field(default=4096, gt=0, le=65536)
    temperature: float = Field(default=0, ge=0, le=2)


class LLMProfileRegistry(StrictModel):
    version: Literal[1]
    defaults: ProfileDefaults = Field(default_factory=ProfileDefaults)
    profiles: dict[str, LLMProfile]

    def resolve(self, name: str) -> LLMProfile:
        try:
            return self.profiles[name].with_defaults(self.defaults)
        except KeyError:
            raise ProviderExecutionError(
                "LLM_PROFILE_NOT_FOUND", kind=ProviderFailureKind.CONFIGURATION
            ) from None
```

Use a `model_validator` to reject duplicate YAML keys through a strict YAML loader, reject profile names outside `^[a-z0-9]+(?:-[a-z0-9]+)*$`, reject environment names outside `^ARD_[A-Z0-9_]+$`, and enforce provider-specific environment allowlists.

- [ ] **Step 4: Add the canonical migration registry and package it**

```yaml
version: 1
defaults:
  timeout_seconds: 120
  max_output_tokens: 4096
  temperature: 0
profiles:
  openai-compatible-default:
    provider: openai_compatible
    model: gpt-5.6-terra
    structured_output: native
    api: chat_completions
    base_url_env: ARD_LLM_BASE_URL
    api_key_env: ARD_LLM_API_KEY
```

Add `"config/llm-profiles.yaml" = "ard_ossie/assets/config/llm-profiles.yaml"` under `[tool.hatch.build.targets.wheel.force-include]`.

- [ ] **Step 5: Run profile, wheel, and lint checks**

Run: `uv run pytest tests/unit/test_llm_profiles.py tests/integration/test_wheel_assets.py -q`

Expected: PASS, including `ard_ossie/assets/config/llm-profiles.yaml` in the wheel.

Run: `uv run ruff check src/ard_ossie/llm tests/unit/test_llm_profiles.py tests/integration/test_wheel_assets.py`

Expected: PASS.

- [ ] **Step 6: Commit the registry**

```bash
git add config/llm-profiles.yaml pyproject.toml src/ard_ossie/llm tests/unit/test_llm_profiles.py tests/integration/test_wheel_assets.py uv.lock
git commit -m "feat: add strict LLM profile registry"
```

### Task 3: Normalize results and add OpenAI-compatible and Azure adapters

**Files:**
- Modify: `src/ard_ossie/llm/contracts.py`
- Modify: `src/ard_ossie/llm/openai_adapters.py`
- Modify: `src/ard_ossie/llm/__init__.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/unit/test_llm.py`
- Modify: `tests/unit/test_pipeline.py`
- Test: `tests/unit/test_openai_adapters.py`

**Interfaces:**
- Consumes: common `messages: list[dict[str, str]]` and optional JSON Schema.
- Produces: `LLMResult(text: str, structured: dict[str, object] | None, metadata: LLMMetadata)` from both adapters.

- [ ] **Step 1: Write result-safety and adapter contract tests**

```python
def test_llm_result_repr_contains_no_generated_body() -> None:
    result = LLMResult(
        text="sensitive generated body",
        structured={"answer": "sensitive generated body"},
        metadata=LLMMetadata(
            profile="safe-profile",
            provider="openai_compatible",
            model="safe-model",
            request_id="req_123",
            input_tokens=4,
            output_tokens=2,
            finish_reason="stop",
            elapsed_ms=8,
        ),
    )
    assert "sensitive generated body" not in repr(result)


@pytest.mark.parametrize("provider_class", [OpenAICompatibleProvider, AzureOpenAIProvider])
def test_openai_family_generates_text_and_normalizes_metadata(provider_class) -> None:
    client = fake_openai_client(content="hello", request_id="req_123")
    provider = make_provider(provider_class, client)
    result = provider.generate_text(messages=[{"role": "user", "content": "hi"}])
    assert result.text == "hello"
    assert result.metadata.request_id == "req_123"
    assert result.metadata.finish_reason == "stop"
```

- [ ] **Step 2: Run the contract tests and observe missing result/adapter behavior**

Run: `uv run pytest tests/unit/test_llm.py tests/unit/test_openai_adapters.py -q`

Expected: FAIL because normalized results, text generation, Responses support, and Azure adapter are absent.

- [ ] **Step 3: Implement bounded metadata and result contracts**

```python
class LLMMetadata(StrictModel):
    profile: str
    provider: ProviderName
    model: str
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = Field(default=None, pattern=r"^[a-z0-9_:-]{1,64}$")
    elapsed_ms: int = Field(ge=0)


class LLMResult(StrictModel):
    text: str = Field(repr=False)
    structured: dict[str, object] | None = Field(default=None, repr=False)
    metadata: LLMMetadata
```

Extend `LLMProvider` with `generate_text` and make `generate_structured` return `LLMResult`.

Preserve existing injected test providers during the transition by unwrapping normalized results at the pipeline boundary:

```python
response = provider.generate_structured(schema=schema, messages=messages)
payload = response.structured if isinstance(response, LLMResult) else response
batch = SuggestionBatch.model_validate(payload)
```

- [ ] **Step 4: Implement OpenAI Chat Completions/Responses and Azure construction**

```python
class AzureOpenAIProvider(OpenAICompatibleProvider):
    provider_name = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: SecretStr,
        deployment: str,
        profile: str,
        api: APIStyle,
        timeout_seconds: int,
        max_output_tokens: int,
        temperature: float,
        client: Any | None = None,
    ):
        client = client or OpenAI(
            base_url=f"{endpoint.rstrip('/')}/openai/v1/",
            api_key=api_key.get_secret_value(),
            timeout=timeout_seconds,
        )
        super().__init__(
            base_url=endpoint,
            api_key=api_key,
            model=deployment,
            profile=profile,
            api=api,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            client=client,
        )
```

Keep all SDK exception mapping in `openai_adapters.py`; never include `str(error)` or response bodies in raised errors. Preserve all current safe codes and add Responses parsing through injected fake clients.

- [ ] **Step 5: Run OpenAI-family unit/integration tests**

Run: `uv run pytest tests/unit/test_llm.py tests/unit/test_openai_adapters.py tests/unit/test_pipeline.py tests/integration/test_openai_compatible.py -q`

Expected: PASS for text, structured JSON, usage, finish reason, empty bodies, auth, permission, not-found, 408, 429/quota, conflict, and 5xx cases.

- [ ] **Step 6: Commit the normalized OpenAI-family adapters**

```bash
git add src/ard_ossie/llm src/ard_ossie/pipeline.py tests/unit/test_llm.py tests/unit/test_openai_adapters.py tests/unit/test_pipeline.py tests/integration/test_openai_compatible.py
git commit -m "feat: normalize OpenAI and Azure LLM adapters"
```

### Task 4: Add Vertex authentication and Gemini/Claude adapters

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/ard_ossie/llm/vertex_adapters.py`
- Modify: `src/ard_ossie/llm/__init__.py`
- Test: `tests/unit/test_vertex_adapters.py`

**Interfaces:**
- Consumes: project, location, model, `SecretStr` service-account JSON, and injected Google Gen AI/Anthropic Vertex clients.
- Produces: the same `LLMResult` and safe `ProviderExecutionError` contracts as Task 3.

- [ ] **Step 1: Add failing fake-transport contract tests**

```python
@pytest.mark.parametrize("provider_class", [VertexGeminiProvider, VertexClaudeProvider])
def test_vertex_adapter_uses_injected_official_client(provider_class) -> None:
    client = fake_vertex_client(provider_class, text="hello")
    provider = make_vertex_provider(provider_class, client=client)
    result = provider.generate_text(messages=[{"role": "user", "content": "hi"}])
    assert result.text == "hello"
    assert client.calls[0]["model"] == "test-model"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "LLM_PROVIDER_AUTHENTICATION_FAILED"),
        (403, "LLM_PROVIDER_PERMISSION_DENIED"),
        (404, "LLM_PROVIDER_MODEL_NOT_FOUND"),
        (429, "LLM_PROVIDER_RATE_LIMITED"),
        (503, "LLM_PROVIDER_SERVER_ERROR"),
    ],
)
def test_vertex_status_mapping_is_safe(status: int, code: str) -> None:
    provider = make_vertex_provider(VertexGeminiProvider, status=status, body="hostile secret body")
    with pytest.raises(ProviderExecutionError, match=code) as captured:
        provider.generate_text(messages=[{"role": "user", "content": "hi"}])
    assert "hostile secret body" not in repr(captured.value)
```

- [ ] **Step 2: Run tests and confirm missing Vertex implementation**

Run: `uv run pytest tests/unit/test_vertex_adapters.py -q`

Expected: FAIL because the adapters do not exist.

- [ ] **Step 3: Add explicit Google authentication dependency**

Run: `uv add 'google-genai>=1,<2' 'anthropic[vertex]>=0.70,<1' 'google-auth[requests]>=2.40,<3'`

Expected: `pyproject.toml` and `uv.lock` explicitly contain the official Google Gen AI and Anthropic Vertex clients plus Google credential transport support.

- [ ] **Step 4: Implement in-memory credentials and direct Vertex transports**

```python
def credentials_from_service_account(raw: SecretStr) -> Credentials:
    try:
        payload = json.loads(raw.get_secret_value())
        if not isinstance(payload, dict):
            raise TypeError
        return service_account.Credentials.from_service_account_info(
            payload, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except Exception:
        raise ProviderExecutionError(
            "LLM_PROVIDER_CONFIGURATION_FAILED",
            kind=ProviderFailureKind.CONFIGURATION,
        ) from None
```

Construct Gemini with `genai.Client(vertexai=True, project=project, location=location, credentials=credentials)` and call `client.models.generate_content`; use `types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=schema)` for native structured output. Construct Claude with `AnthropicVertex(project_id=project, region=location, credentials=credentials)` and call `client.messages.create`; support `prompt_json` structured mode and normalize only text, usage, stop reason, and request ID. Credentials remain in memory and are never serialized by ARD.

- [ ] **Step 5: Run all fake-transport Vertex tests**

Run: `uv run pytest tests/unit/test_vertex_adapters.py -q`

Expected: PASS without external network calls or credentials.

Run: `uv run ruff check src/ard_ossie/llm/vertex_adapters.py tests/unit/test_vertex_adapters.py`

Expected: PASS.

- [ ] **Step 6: Commit Vertex adapters**

```bash
git add pyproject.toml uv.lock src/ard_ossie/llm tests/unit/test_vertex_adapters.py
git commit -m "feat: add Vertex Gemini and Claude adapters"
```

### Task 5: Add selected-only factory, retry, and bounded repair

**Files:**
- Create: `src/ard_ossie/llm/factory.py`
- Create: `src/ard_ossie/llm/service.py`
- Modify: `src/ard_ossie/llm/__init__.py`
- Test: `tests/unit/test_llm_factory.py`
- Test: `tests/unit/test_llm_service.py`

**Interfaces:**
- Consumes: `LLMProfile`, `Mapping[str, str]`, optional adapter constructors, and `LLMProvider`.
- Produces: `LLMProviderFactory.create(profile, environment) -> LLMProvider`, `LLMService.generate_text(...) -> LLMResult`, and `LLMService.generate_structured(...) -> LLMResult`.

- [ ] **Step 1: Write selected-only lookup and factory-selection tests**

```python
def test_factory_reads_only_selected_profile_environment() -> None:
    reads: list[str] = []
    env = RecordingEnvironment(
        {"ARD_LLM_BASE_URL": "https://example.test/v1", "ARD_LLM_API_KEY": "secret"},
        reads,
    )
    profile = migration_profile()
    provider = LLMProviderFactory(openai_constructor=fake_openai_constructor).create(profile, env)
    assert provider is not None
    assert reads == ["ARD_LLM_BASE_URL", "ARD_LLM_API_KEY"]
    assert "ARD_VERTEX_CREDENTIALS_JSON" not in reads
```

- [ ] **Step 2: Write retry/repair tests with deterministic sleep and random sources**

```python
def test_transient_failure_retries_three_total_attempts_same_provider() -> None:
    provider = SequenceProvider([transient_error(), transient_error(), result({"ok": True})])
    sleeps: list[float] = []
    service = LLMService(provider, sleep=sleeps.append, jitter=lambda: 0)
    output = service.generate_structured(schema=closed_schema(), messages=user_messages())
    assert output.structured == {"ok": True}
    assert provider.calls == 3
    assert sleeps == [1.0, 2.0]


def test_invalid_output_uses_two_repairs_then_fails_without_body() -> None:
    provider = SequenceProvider([raw_result("bad"), raw_result("bad-2"), raw_result("bad-3")])
    service = LLMService(provider, sleep=lambda _: None, jitter=lambda: 0)
    with pytest.raises(ProviderExecutionError, match="LLM_SCHEMA_VIOLATION") as captured:
        service.generate_structured(schema=closed_schema(), messages=user_messages())
    assert provider.calls == 3
    assert "bad-3" not in repr(captured.value)
```

- [ ] **Step 3: Run focused tests and confirm missing factory/service**

Run: `uv run pytest tests/unit/test_llm_factory.py tests/unit/test_llm_service.py -q`

Expected: FAIL because factory and service are absent.

- [ ] **Step 4: Implement provider-specific allowlists and construction**

```python
_ALLOWED_ENV = {
    "openai_compatible": frozenset({"ARD_LLM_BASE_URL", "ARD_LLM_API_KEY"}),
    "azure_openai": frozenset({"ARD_AZURE_OPENAI_ENDPOINT", "ARD_AZURE_OPENAI_API_KEY"}),
    "vertex_gemini": frozenset({"ARD_GCP_PROJECT_ID", "ARD_VERTEX_CREDENTIALS_JSON"}),
    "vertex_claude": frozenset({"ARD_GCP_PROJECT_ID", "ARD_VERTEX_CREDENTIALS_JSON"}),
}


class LLMProviderFactory:
    def create(self, profile: LLMProfile, environment: Mapping[str, str]) -> LLMProvider:
        values = {
            name: self._required(environment, name) for name in profile.required_environment_names()
        }
        return self._construct(profile, values)
```

Do not iterate over unrelated environment variables. Convert credential strings to `SecretStr` immediately.

- [ ] **Step 5: Implement retry and repair in one shared service**

```python
class LLMService:
    def generate_structured(
        self, *, schema: dict[str, object], messages: list[dict[str, str]]
    ) -> LLMResult:
        initial = self._retry(
            lambda: self.provider.generate_structured(schema=schema, messages=messages)
        )
        for repair_number in range(3):
            try:
                return self._validated(initial, schema)
            except ProviderExecutionError as error:
                if error.kind is not ProviderFailureKind.OUTPUT or repair_number == 2:
                    raise
                initial = self._retry(
                    lambda: self.provider.generate_structured(
                        schema=schema,
                        messages=self._repair_messages(messages, schema, error.code),
                    )
                )
        raise AssertionError("unreachable")
```

Retry only `ProviderFailureKind.TRANSIENT`, cap backoff at 8 seconds, and never include rejected text in a raised exception or representation. Deterministic cleanup may remove only a single enclosing Markdown JSON fence before `json.loads` and `jsonschema.validate`.

- [ ] **Step 6: Run factory/service tests**

Run: `uv run pytest tests/unit/test_llm_factory.py tests/unit/test_llm_service.py -q`

Expected: PASS for selected-only access, all four constructors, capability rejection, retry exhaustion, non-retryable errors, fence cleanup, schema validation, and two-repair limit.

- [ ] **Step 7: Commit factory and shared behavior**

```bash
git add src/ard_ossie/llm tests/unit/test_llm_factory.py tests/unit/test_llm_service.py
git commit -m "feat: add LLM factory retry and repair service"
```

### Task 6: Integrate the profile service with processing and safe provenance

**Files:**
- Modify: `src/ard_ossie/application/processing.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `src/ard_ossie/models.py`
- Modify: `schemas/reports/quality-report.schema.json`
- Modify: `tests/unit/test_processing_service.py`
- Modify: `tests/unit/test_pipeline.py`
- Test: `tests/integration/test_multi_provider_pipeline.py`

**Interfaces:**
- Consumes: trusted packaged registry plus `ARD_LLM_PROFILE` and selected profile environment values.
- Produces: one `LLMService` per run and bounded `llm_provenance` scalars in trusted quality metadata.

- [ ] **Step 1: Write migration and source-fidelity integration tests**

```python
@pytest.mark.parametrize(
    "provider_name", ["openai_compatible", "azure_openai", "vertex_gemini", "vertex_claude"]
)
def test_fake_provider_keeps_source_facing_artifacts_unchanged(
    tmp_path: Path, provider_name: str
) -> None:
    product = copy_synthetic_product(tmp_path)
    semantic_before = (product / "draft" / "data-semantic.md").read_bytes()
    dictionary_before = (product / "draft" / "data-dictionary.json").read_bytes()
    process_product(
        product, registry_root=tmp_path / "registry", provider=fake_service(provider_name)
    )
    assert (product / "draft" / "data-semantic.md").read_bytes() == semantic_before
    assert (product / "draft" / "data-dictionary.json").read_bytes() == dictionary_before


def test_provider_from_environment_ignores_legacy_model_and_style(monkeypatch) -> None:
    monkeypatch.setenv("ARD_LLM_PROFILE", "openai-compatible-default")
    monkeypatch.setenv("ARD_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("ARD_LLM_API_KEY", "secret")
    monkeypatch.setenv("ARD_LLM_MODEL", "must-not-be-read")
    monkeypatch.setenv("ARD_LLM_API_STYLE", "must-not-be-read")
    service = provider_from_environment()
    assert service.provider.model == "gpt-5.6-terra"
```

- [ ] **Step 2: Run integration tests and confirm old environment construction fails expectations**

Run: `uv run pytest tests/unit/test_processing_service.py tests/unit/test_pipeline.py tests/integration/test_multi_provider_pipeline.py -q`

Expected: FAIL because processing still reads `ARD_LLM_MODEL`/`ARD_LLM_API_STYLE` and adapters return unnormalized dictionaries.

- [ ] **Step 3: Resolve one profile before processing and adapt pipeline calls**

```python
def provider_from_environment(
    *,
    registry: LLMProfileRegistry | None = None,
    environment: Mapping[str, str] | None = None,
) -> LLMService:
    registry = registry or LLMProfileRegistry.load_packaged()
    environment = environment or os.environ
    profile_name = required_environment_value(environment, "ARD_LLM_PROFILE")
    profile = registry.resolve(profile_name)
    return LLMService(LLMProviderFactory().create(profile, environment))
```

Change the pipeline to read `result.structured` and retain its current semantic/evidence validators. Do not expose adapter instances or SDK types to pipeline code.

- [ ] **Step 4: Add bounded safe provenance fields**

```python
class LLMProvenance(StrictModel):
    profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    provider: ProviderName
    model: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0, le=2)
    repair_count: int = Field(default=0, ge=0, le=2)
    elapsed_ms: int = Field(ge=0)
```

Update the JSON Schema and serialization tests. Keep generated text, prompts, validation text, and raw exceptions out of provenance.

- [ ] **Step 5: Run processing, pipeline, and hostile-text tests**

Run: `uv run pytest tests/unit/test_processing_service.py tests/unit/test_pipeline.py tests/integration/test_multi_provider_pipeline.py -q`

Expected: PASS with all fake adapters, unchanged source-facing artifacts, failed-repair non-promotion, and no hostile body leakage.

- [ ] **Step 6: Commit processing integration**

```bash
git add src/ard_ossie/application/processing.py src/ard_ossie/pipeline.py src/ard_ossie/models.py schemas/reports/quality-report.schema.json tests/unit/test_processing_service.py tests/unit/test_pipeline.py tests/integration/test_multi_provider_pipeline.py
git commit -m "feat: integrate profile-selected LLM processing"
```

### Task 7: Add administrator CLI and protected live smoke workflow

**Files:**
- Create: `src/ard_ossie/cli/llm.py`
- Modify: `src/ard_ossie/cli/root.py`
- Modify: `tests/unit/test_cli_structure.py`
- Test: `tests/unit/test_llm_cli.py`
- Create: `.github/workflows/ard-llm-smoke.yml`
- Modify: `tests/integration/test_workflow_contracts.py`

**Interfaces:**
- Consumes: packaged registry, optional `ARD_LLM_PROFILE`, and protected Environment values.
- Produces: `ard llm profiles`, `ard llm validate`, and `ard llm smoke-test` with JSON containing safe identifiers/status only.

- [ ] **Step 1: Write CLI behavior and no-credential listing tests**

```python
def test_profiles_lists_safe_metadata_without_reading_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ARD_LLM_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["llm", "profiles"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["name"] == "openai-compatible-default"
    assert "api_key" not in result.stdout.lower()


def test_validate_without_protected_environment_reports_credentials_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ARD_LLM_PROFILE", "openai-compatible-default")
    monkeypatch.delenv("ARD_LLM_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["llm", "validate"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["credential_check"] == "unavailable"
```

- [ ] **Step 2: Run CLI/workflow tests and confirm missing commands**

Run: `uv run pytest tests/unit/test_cli_structure.py tests/unit/test_llm_cli.py tests/integration/test_workflow_contracts.py -q`

Expected: FAIL because the CLI group and workflow are absent.

- [ ] **Step 3: Implement the Typer command group**

```python
app = typer.Typer(no_args_is_help=True)


@app.command("profiles")
def profiles() -> None:
    registry = LLMProfileRegistry.load_packaged()
    typer.echo(json.dumps(registry.safe_profiles(), sort_keys=True))


@app.command("validate")
def validate_profile(profile: str | None = None) -> None:
    result = validate_runtime_profile(profile or os.environ.get("ARD_LLM_PROFILE"), os.environ)
    typer.echo(json.dumps(result.model_dump(mode="json"), sort_keys=True))


@app.command("smoke-test")
def smoke_test(profile: str | None = None) -> None:
    result = run_safe_smoke_test(profile or required_profile_name())
    typer.echo(json.dumps(result.model_dump(mode="json"), sort_keys=True))
```

The smoke result includes only profile/provider/model, boolean text/structured success, elapsed time, token counts, and safe error code.

- [ ] **Step 4: Add a read-only protected manual workflow**

```yaml
name: ARD LLM smoke test
on:
  workflow_dispatch:
    inputs:
      profile:
        description: Repository-defined profile name
        required: true
        type: string
permissions:
  contents: read
jobs:
  smoke:
    runs-on: ubuntu-24.04
    environment: ard-llm
```

Pin checkout/setup actions to the same SHAs used by `ard-process.yml`, check out `main`, and execute only `uv run --frozen ard llm smoke-test --profile "$ARD_LLM_PROFILE"`. Map the fixed provider Variables/Secrets, never accept model/endpoint/credential inputs, and upload no provider output artifact.

- [ ] **Step 5: Run CLI and workflow-contract tests**

Run: `uv run pytest tests/unit/test_cli_structure.py tests/unit/test_llm_cli.py tests/integration/test_workflow_contracts.py -q`

Expected: PASS, including SHA pinning, read-only permission, `ard-llm` environment, and safe command/output assertions.

- [ ] **Step 6: Commit CLI and smoke workflow**

```bash
git add src/ard_ossie/cli .github/workflows/ard-llm-smoke.yml tests/unit/test_cli_structure.py tests/unit/test_llm_cli.py tests/integration/test_workflow_contracts.py
git commit -m "feat: add LLM profile CLI and smoke workflow"
```

### Task 8: Migrate protected processing configuration and bootstrap/docs

**Files:**
- Modify: `.github/workflows/ard-process.yml`
- Modify: `src/ard_ossie/application/github_bootstrap.py`
- Modify: `src/ard_ossie/cli/github.py`
- Modify: `tests/unit/test_github_bootstrap_service.py`
- Modify: `tests/unit/test_workflow_secret_contract.py`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `docs/github-actions-setup.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `ARD_LLM_PROFILE`, fixed provider Variables, and fixed provider Secrets in `environment: ard-llm`.
- Produces: migration-safe workflow environment and bootstrap drift plan that never reads or overwrites existing Secret values unless explicitly requested.

- [ ] **Step 1: Write workflow/bootstrap migration tests**

```python
def test_processor_maps_only_fixed_multi_provider_environment_names() -> None:
    workflow = load_workflow("ard-process.yml")
    env = workflow["jobs"]["process"]["env"]
    assert env["ARD_LLM_PROFILE"] == "${{ vars.ARD_LLM_PROFILE }}"
    assert env["ARD_LLM_BASE_URL"].startswith("${{ secrets.ARD_LLM_BASE_URL ||")
    assert env["ARD_AZURE_OPENAI_ENDPOINT"] == "${{ vars.ARD_AZURE_OPENAI_ENDPOINT }}"
    assert env["ARD_GCP_PROJECT_ID"] == "${{ vars.ARD_GCP_PROJECT_ID }}"
    assert env["ARD_AZURE_OPENAI_API_KEY"] == "${{ secrets.ARD_AZURE_OPENAI_API_KEY }}"
    assert env["ARD_VERTEX_CREDENTIALS_JSON"] == "${{ secrets.ARD_VERTEX_CREDENTIALS_JSON }}"
    assert "ARD_LLM_MODEL" not in env
    assert "ARD_LLM_API_STYLE" not in env


def test_bootstrap_requires_default_profile_but_not_unselected_provider_secrets() -> None:
    config = BootstrapConfig(
        profile="openai-compatible-default",
        base_url="https://api.openai.com/v1",
    )
    assert config.variables()["ARD_LLM_PROFILE"] == "openai-compatible-default"
    assert "ARD_LLM_MODEL" not in config.variables()
```

- [ ] **Step 2: Run migration tests and confirm old variable contract fails**

Run: `uv run pytest tests/unit/test_github_bootstrap_service.py tests/unit/test_workflow_secret_contract.py tests/integration/test_workflow_contracts.py -q`

Expected: FAIL because the old model/style variables remain.

- [ ] **Step 3: Update process workflow and bootstrap configuration**

```python
class BootstrapConfig(StrictModel):
    profile: str = "openai-compatible-default"
    base_url: str = "https://api.openai.com/v1"
    azure_endpoint: str | None = None
    gcp_project_id: str | None = None
    max_attachment_bytes: int = 52_428_800

    def variables(self) -> dict[str, str]:
        values = {
            "ARD_LLM_PROFILE": self.profile,
            "ARD_LLM_BASE_URL": self.base_url,
            "ARD_MAX_ATTACHMENT_BYTES": str(self.max_attachment_bytes),
        }
        if self.azure_endpoint:
            values["ARD_AZURE_OPENAI_ENDPOINT"] = self.azure_endpoint
        if self.gcp_project_id:
            values["ARD_GCP_PROJECT_ID"] = self.gcp_project_id
        return values
```

Keep `ARD_LLM_BASE_URL` Secret precedence. Treat Azure/Vertex Secrets as optional until selected. The bootstrap plan may report their names as absent but must neither retrieve nor replace their values.

- [ ] **Step 4: Update setup and local-run documentation**

Document the exact Variable/Secret table, migration from `ARD_LLM_MODEL`/`ARD_LLM_API_STYLE`, the default profile value, protected smoke-test prerequisite, and provider-specific examples. Explicitly state that production processing cannot take a profile from Issue content or workflow input.

- [ ] **Step 5: Run workflow/bootstrap/document contract tests**

Run: `uv run pytest tests/unit/test_github_bootstrap_service.py tests/unit/test_workflow_secret_contract.py tests/integration/test_workflow_contracts.py -q`

Expected: PASS with selected-only credential requirements and trusted checkout boundaries intact.

- [ ] **Step 6: Commit workflow and operator migration**

```bash
git add .github/workflows/ard-process.yml src/ard_ossie/application/github_bootstrap.py src/ard_ossie/cli/github.py tests/unit/test_github_bootstrap_service.py tests/unit/test_workflow_secret_contract.py tests/integration/test_workflow_contracts.py docs/github-actions-setup.md README.md
git commit -m "feat: migrate protected LLM profile configuration"
```

### Task 9: Complete regression verification and prepare the draft PR for review

**Files:**
- Modify only files identified by verification failures.
- Review: `docs/superpowers/specs/2026-08-13-multi-provider-llm-design.md`
- Review: `docs/superpowers/plans/2026-08-13-multi-provider-llm.md`

**Interfaces:**
- Consumes: the complete feature branch.
- Produces: a clean branch, passing local checks, a draft PR with evidence, and an explicit note that live provider smoke tests remain an administrator-controlled post-configuration gate.

- [ ] **Step 1: Run formatting and static checks**

Run: `uv run ruff format --check .`

Expected: PASS.

Run: `uv run ruff check .`

Expected: PASS.

- [ ] **Step 2: Run the complete automated test suite**

Run: `uv run pytest -q`

Expected: PASS with no paid-provider network call.

- [ ] **Step 3: Build and inspect the wheel**

Run: `uv build --wheel --no-build-isolation`

Expected: PASS.

Run: `unzip -l dist/ard_ossie_provider-*.whl | rg 'ard_ossie/assets/config/llm-profiles.yaml'`

Expected: exactly one packaged profile registry entry.

- [ ] **Step 4: Scan changed files for secret and placeholder leakage**

Run: `git diff main...HEAD -- . ':!uv.lock' | rg -n 'TB[D]|TO[D]O|sk-[A-Za-z0-9]|BEGIN PRIVATE KEY|hostile secret body'`

Expected: no production secret, placeholder, private-key material, or test hostile-body literal outside intentional test fixtures/assertions.

Run: `git status --short`

Expected: clean working tree after committing any verification-only fixes.

- [ ] **Step 5: Review the branch diff against the approved scope**

Run: `git diff --stat main...HEAD && git log --oneline main..HEAD`

Expected: changes are limited to LLM profiles/adapters/service, processing integration, CLI/workflows/bootstrap, tests, and documentation; no user-level selection, automatic routing, cross-provider failover, or source-facing rewrite appears.

- [ ] **Step 6: Push and open a draft PR**

```bash
git push -u origin agent/multi-llm-provider-design
gh pr create --draft --base main --head agent/multi-llm-provider-design --title "feat: add administrator-selected multi-provider LLM profiles" --body-file /tmp/ard-multi-llm-pr.md
```

The PR body must include the four providers, configuration migration, trust/security boundaries, exact local verification commands/results, and a checklist showing that Azure/Gemini/Claude live smoke runs are pending until the administrator provides exact profiles and credentials.

- [ ] **Step 7: Inspect PR checks and perform a review-readiness pass**

Run: `gh pr checks --watch`

Expected: all available required checks pass; protected live smoke tests are not ordinary PR checks.

Run: `gh pr diff --check`

Expected: no whitespace errors.

Review the PR patch for correctness, security, backward compatibility, test quality, and design-scope compliance. Leave the PR as draft if any automated check fails or required reviewer evidence is missing; otherwise report that it is ready for human review without self-approving or merging.
