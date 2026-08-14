# OpenAI Model-Maximum Output and Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the selected OpenAI-compatible model choose its own output maximum and emit actionable, content-safe diagnostics when it returns no structured output.

**Architecture:** Represent the explicit profile value `model_maximum` separately from inherited numeric defaults, translate it to an omitted OpenAI request parameter, and leave all other profiles numerically bounded. Classify empty responses from safe API metadata and log only allowlisted identifiers, finish reason, and token counts—never prompts, response bodies, endpoints, or credentials.

**Tech Stack:** Python 3.12, Pydantic v2, OpenAI Python SDK, pytest, Ruff.

## Global Constraints

- `openai-compatible-default` uses the model maximum by omitting `max_completion_tokens`/`max_output_tokens`.
- Other provider profiles retain the numeric default of 4,096 output tokens.
- Detailed diagnostics may include only profile, provider, model, safe request ID, safe finish reason, and nonnegative token counts.
- Do not log prompt text, response content, refusal content, endpoint URLs, credentials, or raw provider exceptions.
- Run only focused LLM profile/factory/adapter tests and Ruff; do not run a local full suite.

---

### Task 1: Explicit model-maximum profile

**Files:**
- Modify: `config/llm-profiles.yaml`
- Modify: `src/ard_ossie/llm/profiles.py`
- Modify: `src/ard_ossie/llm/factory.py`
- Modify: `src/ard_ossie/llm/openai_adapters.py`
- Test: `tests/unit/test_llm_profiles.py`
- Test: `tests/unit/test_llm_factory.py`
- Test: `tests/unit/test_llm.py`

**Interfaces:**
- Consumes: `OpenAICompatibleProfile.max_output_tokens` as an integer, inherited default, or literal `model_maximum`.
- Produces: `OpenAICompatibleProvider.max_output_tokens: int | None`; `None` causes both OpenAI API request styles to omit their explicit output-token parameter.

- [x] **Step 1: Write and verify RED tests**

Update the packaged-profile assertion to require `model_maximum`, update the factory assertion to require `max_output_tokens is None`, and add adapter tests asserting an uncapped request omits the token parameter and a length-ended empty response raises `LLM_OUTPUT_TOKEN_LIMIT_EXCEEDED` with safe diagnostic metadata in the log.

Run the four exact affected tests and confirm they fail for the expected missing behavior.

- [x] **Step 2: Implement explicit model maximum**

Allow only the OpenAI-compatible profile type to accept the literal `model_maximum`, configure the default profile with that value, translate it to `None` in the factory, and omit the OpenAI request token key when the provider limit is `None`.

- [x] **Step 3: Implement safe detailed empty-response diagnostics**

Classify `length`/`max_output_tokens`, `content_filter`, refusal, and missing choices into distinct codes. Emit one error-level log record containing only the allowlisted safe metadata before raising `ProviderExecutionError`.

- [x] **Step 4: Focused verification**

Run `tests/unit/test_llm_profiles.py`, `tests/unit/test_llm_factory.py`, and the directly affected adapter tests in `tests/unit/test_llm.py`, then Ruff on the changed Python files. Do not run the local full suite.

- [ ] **Step 5: Publish and reprocess**

Commit, open a dedicated PR, wait for repository CI, merge only when all checks pass, and reapply `ard:approved` to Issue #3 once. Verify PR #5 receives regenerated HTML-free Markdown and detailed diagnostics are available if the provider still fails.
