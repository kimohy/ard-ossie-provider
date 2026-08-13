# Multi-Provider LLM Profiles and Native Adapters Design

## Status

Approved on 2026-08-13. This design adds administrator-selected LLM providers without adding
user selection, automatic routing, or cross-provider failover.

## Context

ARD Ossie currently constructs one `OpenAICompatibleProvider` from
`ARD_LLM_API_KEY`, `ARD_LLM_BASE_URL`, `ARD_LLM_MODEL`, and
`ARD_LLM_API_STYLE=chat_completions`. The provider implements the existing `LLMProvider`
protocol, validates strict JSON Schema output, and normalizes provider failures into the safe
`ProviderExecutionError` taxonomy.

The administrator now needs to select one default model profile for an entire protected processing
run. The supported provider families are:

- OpenAI and OpenAI-compatible endpoints;
- Azure OpenAI;
- Gemini through Google Cloud Vertex AI;
- Claude through Google Cloud Vertex AI.

The profile definitions belong in the repository. The active profile belongs in a GitHub
Environment Variable. Credentials remain in GitHub Environment Secrets.

This design preserves the following existing contracts:

- [Trusted Processing Boundary](2026-08-10-trusted-processing-boundary-design.md): only code and
  configuration from the default branch may execute in the `ard-llm` job.
- [Safe LLM Failure Diagnostics](2026-08-12-llm-failure-diagnostics-design.md): provider messages,
  response bodies, prompts, authorization data, and raw exceptions do not enter logs, outputs,
  summaries, or artifacts.
- [Source Document Fidelity](2026-08-12-source-document-fidelity-design.md): provider changes do not
  authorize LLM output to rewrite source-faithful semantic or dictionary content.

The earlier diagnostics design deliberately kept Chat Completions and the active model unchanged
while diagnosing one incident. This approved feature supersedes only that temporary protocol/model
freeze. It retains the error taxonomy, retryability mapping, and redaction requirements.

## Goals

1. Select one administrator-managed LLM profile for each protected processing run.
2. Support the four provider families through native adapters behind one application contract.
3. Preserve the existing OpenAI-compatible configuration as a migration-safe profile.
4. Validate the selected profile and only its required runtime values before provider execution.
5. Keep provider-specific SDK types and response shapes out of the ARD pipeline.
6. Retry transient failures and repair invalid structured output with bounded, same-model behavior.
7. Record safe provider provenance sufficient to reproduce and compare generated results.
8. Test all provider adapters without requiring paid APIs in ordinary pull request checks.

## Non-goals

- User-level or issue-level model selection.
- Task-specific model routing.
- Cost-, latency-, or quality-based automatic model choice.
- Automatic failover from one provider or model to another.
- A separate LLM proxy, gateway, or always-on service.
- Feature parity beyond text generation and structured JSON generation.
- Changing the authority of source documents, Registry records, deterministic validators, or Ossie
  schema validation.

## Decision

Use a repository-owned profile registry and provider-native adapters. A small application service
resolves the active profile, creates exactly one provider instance, and presents a provider-neutral
interface to the pipeline.

This approach is preferred over a third-party unified SDK because the project already has a strict
provider boundary and security taxonomy. Native adapters keep authentication, request conversion,
error mapping, and capability differences explicit and independently testable. It is preferred
over an OpenAI-compatible proxy because the CLI must continue to run without an additional service.

## Architecture

### `LLMProfileRegistry`

`LLMProfileRegistry` loads a versioned YAML document, validates it with strict Pydantic models, and
resolves the name in `ARD_LLM_PROFILE`. Unknown keys, duplicate profile names, unknown providers,
invalid environment-variable names, and conflicting fields fail before credentials are read.

The canonical source is `config/llm-profiles.yaml`. Packaging must include that file in the wheel as
`ard_ossie/assets/config/llm-profiles.yaml`. The installed CLI uses the packaged copy by default.
Repository workflows execute trusted default-branch code and the default-branch profile registry;
they never load a profile registry from the candidate checkout.

### `LLMProviderFactory`

`LLMProviderFactory` receives one validated profile and an allowlisted environment reader. It
constructs one of:

- `OpenAICompatibleProvider`;
- `AzureOpenAIProvider`;
- `VertexGeminiProvider`;
- `VertexClaudeProvider`.

The factory reads only environment variables declared by the selected profile. Environment names
must match `^ARD_[A-Z0-9_]+$` and must also be present in a provider-specific allowlist. A candidate
document cannot supply a profile name, provider name, model name, endpoint, region, credential
reference, or API style.

### `LLMProvider` contract

The existing protocol remains the adapter boundary and is extended without exposing SDK objects:

```python
class LLMProvider(Protocol):
    def health_check(self) -> bool: ...
    def capabilities(self) -> dict[str, JsonValue]: ...
    def generate_text(self, *, messages: list[dict[str, str]]) -> LLMResult: ...
    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> LLMResult: ...
```

`LLMResult` contains normalized content plus only safe metadata:

- profile, provider, and model identifiers;
- sanitized provider request ID when available;
- input and output token counts when available;
- normalized finish reason;
- elapsed milliseconds.

It does not contain an SDK response object, credentials, headers, raw error text, or rejected
response body. The pipeline parses `LLMResult.structured` rather than provider-specific fields.

### `LLMService`

`LLMService` owns behavior shared by all adapters:

- same-provider transient retry;
- output parsing and schema validation;
- bounded output repair;
- normalized safe execution metadata;
- final propagation of `ProviderExecutionError`.

ARD product, semantic, dictionary, metric, relationship, and Ossie logic depend on `LLMService` or
the `LLMProvider` protocol, not on OpenAI, Azure, Google, or Anthropic SDK classes.

## Profile registry

The YAML document has `version`, `defaults`, and `profiles`. Common profile fields are:

| Field | Required | Meaning |
| --- | --- | --- |
| `provider` | yes | Closed enum selecting one native adapter |
| `model` | yes | Provider model ID or Azure deployment name committed in the profile |
| `timeout_seconds` | no | Positive request timeout; inherits the registry default |
| `max_output_tokens` | no | Positive output bound; inherits the registry default |
| `temperature` | no | Provider-neutral sampling value where supported |
| `structured_output` | yes | `native` or `prompt_json` |

Provider-specific fields are:

| Provider | Fields |
| --- | --- |
| `openai_compatible` | `api`, `base_url_env`, `api_key_env` |
| `azure_openai` | `api`, `endpoint_env`, `api_key_env` |
| `vertex_gemini` | `project_env`, `location`, `credentials_env` |
| `vertex_claude` | `project_env`, `location`, `credentials_env` |

`api` is `chat_completions` or `responses`. The first migration profile remains
`chat_completions` so moving the current OpenAI-compatible path behind the registry does not also
change its request protocol. A later repository profile may select `responses` after its target
endpoint passes adapter contract and live smoke tests.

`structured_output=native` uses the provider's schema-constrained API. `prompt_json` is an explicit
compatibility mode for a model that lacks native JSON Schema. It never activates implicitly. A
profile declaring a capability unsupported by its adapter fails validation or smoke testing.

Exact production model IDs, Azure deployment names, and supported Vertex regions are operational
inputs committed by the administrator through a reviewed code PR. They are not accepted from Issue
content or workflow-dispatch input. Model availability and account access are confirmed by the
protected smoke workflow before a profile becomes the default.

The initial registry contains a concrete `openai-compatible-default` profile using the currently
approved `gpt-5.6-terra` model, `chat_completions`, `ARD_LLM_BASE_URL`, and
`ARD_LLM_API_KEY`. This profile is the migration reference against which existing output and error
behavior are regression-tested. `ARD_LLM_MODEL` and `ARD_LLM_API_STYLE` stop being runtime inputs
after this profile is active; their values move into reviewed repository configuration.

## GitHub Variables and Secrets

The protected `ard-llm` Environment contains the active selection and credentials.

### Variables

- `ARD_LLM_PROFILE`: required active profile name;
- `ARD_LLM_BASE_URL`: retained for the current OpenAI-compatible profile;
- `ARD_AZURE_OPENAI_ENDPOINT`: Azure resource endpoint;
- `ARD_GCP_PROJECT_ID`: Vertex AI project ID.

### Secrets

- `ARD_LLM_API_KEY`: retained for the current OpenAI-compatible profile;
- `ARD_AZURE_OPENAI_API_KEY`: Azure OpenAI API key;
- `ARD_VERTEX_CREDENTIALS_JSON`: service-account JSON shared by Vertex Gemini and Vertex Claude.

The existing optional `ARD_LLM_BASE_URL` Secret remains accepted during migration with its current
precedence over the Variable of the same name. Trusted workflow expressions resolve either source
into the single allowlisted `ARD_LLM_BASE_URL` process environment variable. Bootstrap never reads
or copies the Secret value. After the operator deliberately migrates the endpoint to a Variable,
the redundant Secret may be removed through the existing secret-management procedure.

The workflow exposes fixed, allowlisted names only in the protected `process` job. Validation before
the Environment remains credential-free. Configuration validation outside the Environment can
validate repository syntax, provider names, and field relationships but must not claim that a
credential exists.

The selected provider validates its required values. Missing values for unselected providers do not
fail processing. Vertex credentials are parsed into an in-memory credential object where the SDK
allows it. If an SDK requires `GOOGLE_APPLICATION_CREDENTIALS`, trusted code writes a mode-0600 file
under `RUNNER_TEMP`, registers cleanup before client construction, and removes it on success or
failure. It never writes credentials below the repository, candidate checkout, `.ard`, quality, or
artifact directories.

Future migration to GitHub OIDC and Google Workload Identity Federation requires a separate design;
it is not mixed into this API-key/service-account feature.

## Provider behavior

### OpenAI-compatible

The existing `OpenAICompatibleProvider` remains the compatibility baseline. It supports the profile's
explicit Chat Completions or Responses API style and preserves `ARD_LLM_API_KEY` and
`ARD_LLM_BASE_URL`. Strict structured output continues to use the existing closed JSON Schema when
the profile declares native support.

### Azure OpenAI

`AzureOpenAIProvider` treats `model` as the Azure deployment name and uses the configured Azure
endpoint. Phase 1 authenticates with `ARD_AZURE_OPENAI_API_KEY`. Microsoft Entra ID is intentionally
deferred. Although it may share the OpenAI Python SDK internally, it remains a distinct adapter so
endpoint construction, authentication, deployment semantics, and error mapping are explicit.

### Vertex Gemini

`VertexGeminiProvider` uses the Google Gen AI SDK in Vertex mode with the configured project,
location, model, and Google credentials. It converts the common messages and JSON Schema into Gemini
content and response-schema types, then normalizes generated content and usage metadata.

### Vertex Claude

`VertexClaudeProvider` calls Claude through Vertex AI, not the direct Anthropic API. It uses the same
Google credential source as Gemini but retains Claude request/response conversion and partner-model
region validation in its own adapter. Its profile model ID identifies the Vertex-hosted Claude
model.

## Execution and recovery

One protected processing run resolves one profile before the first provider call and retains it for
the entire run. No retry or repair may change the profile, provider, model, endpoint, project, or
region.

1. Resolve and validate `ARD_LLM_PROFILE` from trusted configuration.
2. Validate required runtime values for the selected profile.
3. Construct one provider and record safe profile provenance.
4. Perform deterministic source extraction before LLM enrichment.
5. Generate text or structured suggestions.
6. Validate provider-neutral output and existing ARD evidence rules.
7. Retry or repair only under the policies below.
8. Promote artifacts only after all existing quality and atomic-promotion checks pass.

### Transient retry

Timeouts, HTTP 408, ordinary rate limits, connection failures, conflicts, and provider 5xx failures
are retried up to three total attempts with bounded exponential backoff and jitter. All attempts use
the same provider and model. Exhaustion re-raises the normalized transient error so the existing
workflow exit-30 contract still applies.

Authentication, permission, invalid profile, missing selected credential, model-not-found, invalid
request, and exhausted quota failures are not retried. The existing safe error codes and
`ProviderFailureKind` mapping remain authoritative.

### Structured-output repair

After a successful provider response, deterministic cleanup may remove transport-neutral wrappers
such as a Markdown JSON fence. It may not invent, summarize, or alter semantic values.

If parsing, JSON Schema, Pydantic, suggestion, or evidence validation still fails, `LLMService` may
make up to two repair calls to the same model. A repair request contains the required schema, a safe
machine-generated validation description, and the minimum source evidence required to correct the
invalid section. It instructs the model to change only invalid content and never add unsupported
facts.

The rejected output and repair prompts exist only in process memory. They are not committed,
uploaded, printed, placed in a result envelope, or attached to a GitHub summary. Failed repair emits
safe codes, attempt counts, and hashes where useful; it does not weaken the existing no-provider-body
diagnostics contract.

### Source-fidelity boundary

This feature does not replace the source-fidelity design. Deterministically extracted semantic text
and dictionary values remain authoritative. Provider output may enrich only fields already permitted
by the current pipeline and must pass evidence validation. It cannot mutate source-facing semantic
or dictionary drafts. Any future LLM reconstruction of those source-facing artifacts requires a
separate approved design.

## Provenance and observability

The trusted result/quality metadata may record:

- profile, provider, and model;
- sanitized provider request ID;
- start time and elapsed time;
- token usage when the provider exposes it;
- retry count and repair count;
- normalized validation outcome and safe error code.

Provider payloads, prompts, generated response bodies, credentials, exception messages, and SDK
objects remain forbidden. Provenance fields use bounded scalar grammars and are covered by secret
and hostile-provider-text tests.

A profile change is a code/configuration change and does not directly increment a product or table
version. If a later processing run produces accepted content changes, the existing content-based
versioning rules determine the numeric version.

## CLI and workflows

The existing executable name is `ard`. Add an `llm` command group:

```text
ard llm profiles
ard llm validate
ard llm smoke-test
```

- `profiles` lists safe repository profile metadata without reading credentials.
- `validate` validates the selected profile. Outside the protected Environment it reports credential
  checks as unavailable rather than successful.
- `smoke-test` makes the smallest real text and structured-output requests and emits only safe
  pass/fail metadata.

`.github/workflows/ard-process.yml` adds `ARD_LLM_PROFILE` and the provider-specific fixed
Variables/Secrets to the protected `process` job. It keeps the current credential-free `validate`
job and trusted/candidate checkout separation.

Add an administrator-only `workflow_dispatch` workflow for live smoke testing. It loads code and
profiles from `main`, uses `environment: ard-llm`, accepts a profile name solely for smoke testing,
and has no write permission. The production processor does not accept a profile input and uses only
`ARD_LLM_PROFILE`.

Repository bootstrap and `docs/github-actions-setup.md` must converge the new Variables and Secret
names without reading, replacing, or logging existing Secret values. Missing optional provider
Secrets remain allowed until that provider is selected.

## Testing

### Unit tests

- Strict profile schema and version validation.
- Unknown profile/provider and environment-name allowlist rejection.
- Selected-only environment lookup and missing-value behavior.
- Provider factory selection and capability validation.
- Safe `LLMResult` normalization and secret-safe representations.
- Existing and provider-specific exception mapping.
- Same-model retry count, backoff boundary, and non-retryable failures.
- Deterministic cleanup and two-attempt repair limits.

### Adapter contract tests

Use injected fake clients/transports for every adapter. Each provider must pass the same contract for
text output, structured output, empty responses, schema violations, usage metadata, finish reasons,
timeouts, rate limits, authentication failures, permissions, model-not-found, and 5xx failures.

No ordinary pull request test contacts a paid provider or requires a Secret.

### Pipeline integration tests

- A fixed synthetic product fixture produces schema-valid provider-neutral suggestions through each
  fake adapter.
- Source-facing semantic and dictionary artifacts remain unchanged across provider adapters.
- Missing, duplicate, unsupported, and evidence-invalid suggestions trigger bounded repair or the
  existing validation failure.
- Failed repair never promotes generated or Registry state.
- Safe provenance is recorded, and hostile provider text cannot appear in logs, tracebacks, result
  envelopes, summaries, or artifacts.
- Existing numeric versioning, Ossie compilation, quality gates, and partial-publication
  reconciliation remain unchanged.

### Live smoke tests

Run each configured profile through the protected manual smoke workflow after its exact model ID,
endpoint/project, region, account access, and Secret are available. Record run URLs and safe outcomes
without copying prompts or responses. A profile cannot become `ARD_LLM_PROFILE` until both text and
structured smoke checks pass.

## Rollout

1. Add strict profile models, packaged registry loading, `LLMResult`, factory, and service with tests.
2. Move the existing OpenAI-compatible Chat Completions path behind the default compatibility
   profile without changing output or current Secret names.
3. Add Azure OpenAI, Vertex Gemini, and Vertex Claude adapters and fake-client contract tests.
4. Add the CLI management commands and protected manual smoke workflow.
5. Update `ard-process.yml`, repository bootstrap, setup documentation, and safe provenance.
6. Before merging the workflow change, converge the protected Environment so
   `ARD_LLM_PROFILE=openai-compatible-default`; the current workflow safely ignores this new
   Variable until the change reaches `main`.
7. Run the full repository checks and the existing OpenAI-compatible live smoke test.
8. After the administrator supplies exact provider configuration and credentials, run Azure,
   Vertex Gemini, and Vertex Claude smoke tests individually.
9. Change `ARD_LLM_PROFILE` only after the selected profile passes live smoke testing.

## Acceptance criteria

- The administrator selects one default profile with `ARD_LLM_PROFILE`.
- OpenAI-compatible, Azure OpenAI, Vertex Gemini, and Vertex Claude satisfy the common adapter
  contract.
- The current `ARD_LLM_API_KEY` and `ARD_LLM_BASE_URL` path remains operational through the legacy
  compatibility profile.
- Only runtime values required by the selected profile are mandatory.
- Candidate content cannot choose or alter provider configuration.
- Transient failures use at most three same-model attempts and then retain the existing exit-30
  behavior.
- Invalid structured output uses at most two same-model repair calls and never promotes invalid
  artifacts.
- Provider response bodies, prompts, credentials, and raw exception messages never reach persistent
  outputs.
- Safe provenance identifies the profile, provider, and model used for every LLM-backed processing
  run.
- All unit, adapter contract, pipeline integration, Ruff, schema, workflow, package, checksum, and
  secret-scan checks pass.
- Each production-enabled provider profile passes the protected live text and structured-output
  smoke tests before selection.

## Deferred work

Automatic failover, per-task routing, quality/cost selection, Microsoft Entra ID, GitHub OIDC with
Google Workload Identity Federation, and provider quality benchmarking require separate designs.
