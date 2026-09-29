# External Model Safety Boundary

Provider-agnostic trust boundary for every external LLM call.

## Trust boundary

```
Application
  -> Interview Engine
    -> Model Router (tier, fallback, circuit breaker)
      -> External Model Safety Boundary
        -> Provider Adapter (transport + auth only)
          -> External Model
```

Adapters are untrusted transport. They must not define security policy.
Policy lives only in this boundary and in explicit configuration (`models.yaml`, approved Settings fields).

## Allowed data (may cross the boundary toward a provider)

- Task name from `LLMTask`
- Authorized provider/model policy for that task
- System prompt and task instructions
- Untrusted user content required for the task (resume, JD, answer, metrics)
- Generation limits: temperature, max_tokens, timeout
- The single credential belonging to the selected provider, used only as an HTTP auth header

## Forbidden data (must never cross toward a provider)

- Other providers' credentials
- Application API keys (`API_KEYS`)
- Webhook signing secrets
- Database credentials or paths used as secrets
- Stripe, Supabase, cloud, or deployment credentials
- Arbitrary environment variables or Settings objects
- Internal auth tokens
- Account, billing, or unrelated interview metadata
- Tool/shell/filesystem/database capability

## Credential rules

- Credentials come only from approved Settings fields: `openai_api_key`, `deepseek_api_key`, `anthropic_api_key`.
- Do not discover keys by scanning the environment for arbitrary `*_API_KEY` names.
- A provider is configured only when its approved key is non-empty and its endpoint is allowlisted.
- Partial provider availability is required: missing keys skip that provider; they do not fail the process.
- Credentials must never appear in prompts, model context, model output, logs, API responses, database records, or webhook payloads.
- Provider A must never receive provider B's credential.

## Provider rules

- Selection comes from explicit policy: `models.yaml` candidates for the task tier, intersected with the request's authorized provider set.
- Do not silently inherit session/global "whatever is configured" outside that policy.
- Do not add provider-specific security exceptions (`if provider == "deepseek": allow ...`).
- Endpoints must be HTTPS and match the approved host list for that provider. Misconfigured endpoints disable the provider; they are not called.
- Adding a future provider requires an adapter plus configuration, not a new security model.

## Router / fallback rules

- Fallback may only use a candidate that satisfies the same request policy.
- Never fall back to "any available provider."
- If no authorized, configured, healthy provider remains, fail safely (`AllProvidersFailedError` / policy denial).
- Circuit-breaker and retry behavior stay in the router; each attempt still goes through the boundary.

## Request contract

Callers must not pass arbitrary application dictionaries to models.

A `ModelRequest` may contain only:

- `task`
- `messages` (role + content strings)
- `authorized_providers` (and optional per-provider model allowlist)
- `temperature`, `max_tokens`, `timeout_seconds`
- `expect_json`

Untrusted resume/JD/answer text is data, not authority. It cannot change provider authorization, credentials, tools, configuration, or execution.

## Response validation

- Treat adapter SDK/HTTP payloads, errors, and model text as untrusted.
- Enforce maximum response size.
- When structured JSON is expected, parse and validate against the task schema.
- Reject missing fields, invalid types, non-objects, and excessively large payloads.
- Model output is data only. It must never become shell, SQL, filesystem paths, env vars, credentials, HTTP destinations, or deployment actions.

## Logging rules

- Redact credentials, Authorization headers, `x-api-key`, webhook secrets, and known secret values.
- Do not log full resumes, answers, or raw provider bodies.
- Store only redacted error strings in events and router health.

## Failure behavior

- Timeouts, rate limits, auth errors, malformed responses, policy denials, and circuit-open events fail the model task.
- Deterministic Python analytics/scoring must continue (existing graceful degradation).
- Question generation remains a hard dependency of interview creation.
- Per-answer analysis, coaching, final scoring, tips, and narrative degrade to heuristics when the model path fails.

## Security invariants

- I1 No bypass: every external model invocation passes through this boundary (runtime permit + static audit).
- I2 Provider-independent policy.
- I3 Credential isolation.
- I4 No application secrets across the boundary.
- I5 Explicit request contract.
- I6 Data minimization.
- I7 Explicit provider authorization.
- I8 Safe fallback.
- I9 Adapters are untrusted.
- I10 Structured output validation.
- I11 Prompt injection is untrusted data (enforced structurally).
- I12 No arbitrary tool authority; this service must not add tool execution.
- I13 Model output is data, never execution.
- I14 Logging safety.
- I15 Failure isolation.
- I16 Configuration safety.

## Future provider requirements

1. Implement a transport adapter with `configured` and `async complete(...)`.
2. Call `require_boundary_permit()` at the start of `complete`.
3. Register the adapter in `ModelRouter._build_providers` using an approved Settings key and base URL.
4. Add the provider host to the shared HTTPS allowlist (same check for every provider).
5. Add candidates in `models.yaml`.
6. Do not add provider-specific security branches.
7. Extend tests with synthetic credentials only; never make live model calls in CI.
