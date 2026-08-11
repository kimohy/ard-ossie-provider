# Safe LLM Failure Diagnostics Design

## Context

Issue #3 reaches the protected `ard-llm` job with `ARD_LLM_API_KEY` present, but
the provider call fails. The current pipeline converts every API, transport, and
output-validation exception into `LLM_PROVIDER_FAILURE`. The reusable workflow
then runs the partial-publication reconciler for every failed processor step;
when no commit was published, that reconciler replaces the useful first error
with `PROCESSING_RECONCILE_RESULT_NOT_PARTIAL`.

## Goals

- Preserve one safe, actionable error code for authentication, authorization,
  model access, malformed requests, quota/rate limits, timeout, connection,
  provider server failures, and invalid structured output.
- Preserve retryability and the existing workflow exit-code contract.
- Make a non-partial process failure finish with the original code and exit code,
  rather than a reconcile-specific secondary error.
- Keep API keys, authorization headers, prompts, response bodies, and exception
  messages out of logs, GitHub outputs, summaries, and uploaded artifacts.
- Keep Chat Completions and the configured model unchanged while diagnosing the
  active failure.

## Approaches Considered

### 1. Typed provider failures plus recorded-failure replay (selected)

Normalize OpenAI SDK exceptions at the provider boundary into a closed set of
codes and a failure kind: `configuration`, `transient`, or `output`. Convert the
kind into the existing workflow configuration, transient, or validation exit
codes. Record the numeric exit code in the trusted process result envelope. The
reconciler continues its current recovery only for
`PROCESSING_POST_COMMIT_FAILED`; for every other valid process failure it
re-emits the recorded code and exit code.

This keeps the existing reusable-workflow control flow and partial-publication
recovery while making the observed failure deterministic and safe.

### 2. Log sanitized HTTP metadata only

Logging status and request IDs could help debugging, but it does not preserve
machine-readable retryability or stop reconcile from masking the error. It also
creates a larger log-redaction surface. This is not selected.

### 3. Migrate to the Responses API while adding diagnostics

This may be valuable later, but it changes the request protocol while the actual
failure is still unknown. It would prevent attributing the next result to one
change. This is deferred.

## Architecture

### Provider boundary

`ard_ossie.llm` owns a `ProviderExecutionError` with a stable `code` and
`ProviderFailureKind`. `OpenAICompatibleProvider.generate_structured()` maps
OpenAI SDK exception classes without copying their messages or bodies:

| Condition | Safe code | Kind |
| --- | --- | --- |
| Invalid/revoked key | `LLM_PROVIDER_AUTHENTICATION_FAILED` | configuration |
| Permission denied | `LLM_PROVIDER_PERMISSION_DENIED` | configuration |
| Model/resource not found | `LLM_PROVIDER_MODEL_NOT_FOUND` | configuration |
| Bad or unprocessable request | `LLM_PROVIDER_REQUEST_REJECTED` | configuration |
| Exhausted credit/spend/usage quota | `LLM_PROVIDER_QUOTA_EXHAUSTED` | configuration |
| Ordinary rate limit | `LLM_PROVIDER_RATE_LIMITED` | transient |
| SDK timeout or HTTP 408 | `LLM_PROVIDER_TIMEOUT` | transient |
| Connection failure | `LLM_PROVIDER_CONNECTION_FAILED` | transient |
| Provider 5xx | `LLM_PROVIDER_SERVER_ERROR` | transient |
| Unknown provider exception | `LLM_PROVIDER_FAILURE` | transient |
| Empty/non-JSON/non-object/schema-invalid output | Existing specific `LLM_*` code | output |
| Invalid suggestion/evidence model | Specific `LLM_*` code or `LLM_OUTPUT_VALIDATION_FAILED` | output |

Only an allowlisted provider error-code field is inspected to distinguish
non-retryable quota exhaustion from an ordinary 429. The provider response body
is never emitted or stored. Client-construction errors use the safe
`LLM_PROVIDER_CONFIGURATION_FAILED` code. Sanitized boundary exceptions suppress
their raw causes so formatted tracebacks cannot reveal provider messages or
rejected response instances.

### Pipeline and workflow mapping

`process_product()` preserves already-normalized provider errors. Errors raised
while validating semantic suggestions become output failures; unexpected custom
provider exceptions become the generic transient code. `ProcessingService`
maps configuration failures to exit 20, transient failures to exit 30, and
output failures to exit 10.

Before execution, the CLI publisher invalidates any prior result envelope. It
adds a trusted `failure_exit_code` scalar to failures and a GitHub run/attempt
`invocation_id` to every process envelope. No secret or exception text is
added. `ProcessingReconcileService` accepts only an envelope whose invocation
matches the current job:

- `PROCESSING_POST_COMMIT_FAILED`: perform the existing idempotent status and
  dispatch reconciliation.
- Any other valid `workflow.process` failure: validate the recorded code and
  exit code, then re-raise that same failure.
- Malformed, wrong-command, successful, or untrusted-path envelopes: retain the
  existing security/validation failures.

Thus the workflow may keep `continue-on-error` for recoverable partial
publication, while a non-partial failure ends with its original diagnosis.

## Security and Privacy

- Exception `str()` values from providers are never copied into codes, messages,
  outputs, findings, summaries, or artifacts.
- Error codes must match the existing uppercase identifier grammar.
- Process invocation identifiers use a closed scalar grammar and must match the
  current GitHub run and attempt.
- Replayed exit codes must be members of the existing `ExitCode` enum and may
  not be success or partial unless the envelope is the recognized partial case.
- A partial envelope must contain exactly one
  `PROCESSING_POST_COMMIT_FAILED` finding and exit code 70.
- The result path remains fixed below `.ard/run` and resolved through the
  filesystem port.
- The API key remains scoped to the protected `process` job.

## Testing

- Unit-test every SDK exception mapping, including quota-vs-rate-limit behavior,
  using real OpenAI exception classes and synthetic `httpx` responses.
- Assert that exception messages and response-body text cannot appear in the
  normalized error representation or a formatted traceback.
- Unit-test structured-output failure codes and pipeline suggestion-validation
  classification.
- Integration-test CLI failure envelopes for `failure_exit_code`.
- Integration-test stale-envelope invalidation and invocation mismatch rejection.
- Unit-test non-partial replay and existing partial reconciliation behavior.
- Run the focused tests RED before implementation, then the full pytest suite,
  Ruff, YAML parsing, actionlint when available, and wheel build.

## Success Criteria

After merge and Issue #3 replay, the protected job either succeeds or exposes
one actionable first failure code. It must not expose a secret, prompt, response
body, or `PROCESSING_RECONCILE_RESULT_NOT_PARTIAL` for a valid non-partial
provider failure.
